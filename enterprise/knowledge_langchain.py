"""Local LangChain-Core retriever for the authoritative controlled search engine.

The framework adapter is used by formal analysis/report retrieval. It creates
real LangChain Documents from actual authorized chunks; it is neither a second
index nor a cache. Every invoke executes the controlled engine again, including
current revocations and the complete temporal/generation/embedding checks.

LangChain's automatic CallbackManager.configure inherits environment tracers
and registered configure hooks even when callbacks=[] is supplied. This adapter
therefore owns its invoke lifecycle using an explicitly empty LangChain callback
manager, a fresh contextvars.Context and tracing_context(enabled=False). It does
not alter process environment variables, global tracing settings, or other
requests. It accepts no caller callbacks, configurable identity, or metadata.
"""
from __future__ import annotations

import asyncio
from contextvars import Context
from copy import deepcopy
from dataclasses import dataclass
import importlib.metadata
import math
import re
from typing import Any

from langchain_core.callbacks.manager import CallbackManager, CallbackManagerForRetrieverRun
from langchain_core.documents import Document
from langchain_core.retrievers import BaseRetriever
from langsmith.run_helpers import tracing_context
from pydantic import ConfigDict, Field, PrivateAttr

from enterprise.knowledge import (
    KnowledgeAccessError, KnowledgeError, Repository, _date, authorize_query,
    normalize_known_at, require_principal,
)
from enterprise.knowledge_release import get_search_engine
from enterprise.tabular_knowledge import normalize_knowledge_types

FRAMEWORK_VERSION = importlib.metadata.version('langchain-core')
ADAPTER_NAME = 'ControlledKnowledgeRetriever'
_SECRET_KEYS = frozenset({
    'apikey', 'token', 'accesstoken', 'refreshtoken', 'idtoken', 'password', 'passwd',
    'secret', 'clientsecret', 'authorization', 'cookie', 'setcookie', 'privatekey',
    'credential', 'credentials', 'requestheaders', 'responseheaders',
})


@dataclass(frozen=True)
class BoundPrincipal:
    user_id: str
    display_name: str
    roles: tuple[str, ...]
    factories: tuple[str, ...]
    products: tuple[str, ...]
    tenant_id: str

    @classmethod
    def capture(cls, principal):
        require_principal(principal)
        # Validate grants before freezing them: a string '*' must not become
        # an apparently valid tuple ('*',) by coercion.
        authorize_query(principal)
        return cls(str(principal.user_id), str(getattr(principal, 'display_name', principal.user_id)), tuple(principal.roles),
                   tuple(principal.factories), tuple(principal.products), str(principal.tenant_id))


@dataclass(frozen=True)
class RetrievalContext:
    product: str | None
    factory: str | None
    as_of: str | None
    known_at: str | None
    release_id: str | None
    allowed_products: tuple[str, ...]
    allowed_factories: tuple[str, ...]
    top_k: int
    vector_timeout: float
    knowledge_types: tuple[str, ...] | None = None
    require_hybrid: bool = False

    def search_arguments(self):
        result = {'product': self.product, 'factory': self.factory, 'as_of': self.as_of,
                'known_at': self.known_at, 'release_id': self.release_id, 'top_k': self.top_k,
                'vector_timeout': self.vector_timeout,
                'allowed_scopes': {'products': list(self.allowed_products), 'factories': list(self.allowed_factories)}}
        if self.knowledge_types is not None:
            result['knowledge_types'] = list(self.knowledge_types)
        if self.require_hybrid:
            result['require_hybrid'] = True
        return result


class RetrievedDocuments(list[Document]):
    """One invocation's Documents plus its stats, including when the list is empty.

    There is intentionally no retriever.last_results/last_stats state. Responses
    are independent snapshots, never a source for a later authorized retrieval.
    """
    __slots__ = ('stats',)

    def __init__(self, documents, stats):
        super().__init__(documents)
        self.stats = deepcopy(stats)


def _without_secrets(value, path='', redactions=None):
    redactions = [] if redactions is None else redactions
    if isinstance(value, dict):
        cleaned = {}
        for key, item in value.items():
            location = f'{path}.{key}' if path else str(key)
            if re.sub('[^a-z0-9]', '', str(key).lower()) in _SECRET_KEYS:
                redactions.append(location)
                continue
            cleaned[key] = _without_secrets(item, location, redactions)
        return cleaned
    if isinstance(value, (tuple, list)):
        return [_without_secrets(item, f'{path}[{index}]', redactions) for index, item in enumerate(value)]
    return deepcopy(value)


def _local_config(config, kwargs):
    if kwargs and (set(kwargs) != {'verbose'} or kwargs['verbose'] is not False):
        raise KnowledgeAccessError('受控检索不可由调用参数覆盖身份、范围或注入回调')
    if config is None:
        return
    if not isinstance(config, dict):
        raise KnowledgeAccessError('仅允许本地空Runnable配置')
    for key, value in config.items():
        if key in {'callbacks', 'metadata', 'tags', 'configurable'} and (value is None or value == [] or value == {}):
            continue
        # Standard batch/async Runnable bookkeeping; never used as search args.
        if key in {'max_concurrency', 'recursion_limit'} and isinstance(value, int) and not isinstance(value, bool) and 0 < value <= 100:
            continue
        if value is None:
            continue
        raise KnowledgeAccessError('受控检索禁止外部callbacks、metadata、configurable或查询范围覆盖')


class ControlledKnowledgeRetriever(BaseRetriever):
    """A real BaseRetriever bound to a server-authenticated identity and context.

    Construct per request; reuse the resident search engine, not Documents.
    Invoke accepts only a query. Async/batch/default stream delegate to the same
    safe invoke and preserve the LangChain list[Document] contract.
    """
    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True, extra='forbid')
    principal: BoundPrincipal = Field(exclude=True, repr=False)
    query_context: RetrievalContext = Field(exclude=True, repr=False)
    _engine: Any = PrivateAttr()

    def __init__(self, *, principal, repository=None, product=None, factory=None, as_of=None,
                 known_at=None, allowed_scopes=None, release_id=None, top_k=10,
                 vector_timeout=3.0, engine=None, knowledge_types=None, require_hybrid=False):
        bound = BoundPrincipal.capture(principal)
        scopes = authorize_query(bound, product=product, factory=factory, allowed_scopes=allowed_scopes)
        if as_of is not None:
            as_of = _date(as_of, '业务日期')
        if known_at is not None:
            known_at = normalize_known_at(known_at)
        if release_id is not None and (not isinstance(release_id, str) or not release_id):
            raise KnowledgeError('release_id须为明确发布ID')
        if isinstance(top_k, bool) or not isinstance(top_k, int) or not 1 <= top_k <= 100:
            raise KnowledgeError('top_k须为1至100')
        if isinstance(vector_timeout, bool) or not isinstance(vector_timeout, (int, float)) or not math.isfinite(vector_timeout) or not 0 < vector_timeout <= 30:
            raise KnowledgeError('vector_timeout须在0至30秒之间')
        if not isinstance(require_hybrid, bool):
            raise KnowledgeError('require_hybrid须为布尔值')
        context = RetrievalContext(product, factory, as_of, known_at, release_id,
            tuple(sorted(scopes['products'])), tuple(sorted(scopes['factories'])), top_k, float(vector_timeout),
            normalize_knowledge_types(knowledge_types), require_hybrid)
        super().__init__(principal=bound, query_context=context, tags=None, metadata=None)
        self._engine = engine if engine is not None else get_search_engine(repository=repository or Repository())

    def _get_relevant_documents(self, query: str, *, run_manager: CallbackManagerForRetrieverRun) -> list[Document]:
        # run_manager has no handlers; authorization happens inside engine.search
        # afresh for every call, not against an earlier Document collection.
        rows, stats = self._engine.search(query, principal=self.principal, **self.query_context.search_arguments())
        stats = deepcopy(stats)
        stats['framework'] = {'name': 'langchain-core', 'version': FRAMEWORK_VERSION,
                              'retriever': ADAPTER_NAME, 'tracing': 'disabled', 'callbacks': 'disabled'}
        documents = []
        for row in rows:
            redactions = []
            catalog = _without_secrets(row['meta'], 'catalog', redactions)
            provenance = {
                'catalog': catalog, 'document_id': row['document_id'], 'version_id': row['version_id'],
                'chunk_id': row['chunk_id'], 'release_id': row['release_id'],
                'index_release_id': row['release_id'], 'generation': row['generation'],
                'document_sha256': catalog['sha256'], 'source': catalog['filename'],
                'scope_products': catalog['scope_products'], 'scope_factories': catalog['scope_factories'],
                'effective_from': catalog['effective_from'], 'effective_to': catalog['effective_to'],
                'confirmed_at': catalog['confirmed_at'], 'business_metadata': catalog['business_metadata'],
                'score': row['score'], 'route_scores': deepcopy(row['route_scores']),
                'rerank_score': row.get('rerank_score'),
                 'retrieval_scores': {'rrf_score': row['score'],
                                      'rerank_score': row.get('rerank_score'),
                                      'vector_score': row.get('route_scores', {}).get('vector'),
                                      'bm25_score': row.get('route_scores', {}).get('bm25'),
                                      'graph_score': row.get('route_scores', {}).get('graph'),
                                      'domain_graph_score': row.get('route_scores', {}).get('domain_graph')},
                 'rerank_details': deepcopy(row.get('rerank_details')),
                 'domain_evidence': deepcopy(row.get('domain_evidence', [])),
                 'as_of': stats['as_of'], 'known_at': stats['known_at'],
                'retrieval_mode': stats['retrieval_mode'], 'retrieval_degraded': stats['degraded'],
                'degradation_reasons': deepcopy(stats['degradation_reasons']),
                'framework': deepcopy(stats['framework']), 'metadata_redactions': redactions,
            }
            documents.append(Document(id=row['chunk_id'], page_content=row['text'], metadata=provenance))
        return RetrievedDocuments(documents, stats)

    def invoke(self, input: str, config=None, **kwargs) -> RetrievedDocuments:
        _local_config(config, kwargs)
        if not isinstance(input, str):
            raise KnowledgeError('受控Retriever输入必须为查询字符串')

        def local_invoke():
            with tracing_context(enabled=False, parent=False, client=None, tags=[], metadata={}, replicas=[]):
                # Constructor, not configure(): never inherits env tracers,
                # configure hooks, ambient metadata or a parent's callbacks.
                manager = CallbackManager(handlers=[], inheritable_handlers=[])
                run = manager.on_retriever_start(None, input, name=ADAPTER_NAME)
                try:
                    result = self._get_relevant_documents(input, run_manager=run)
                except BaseException as exc:
                    run.on_retriever_error(exc)
                    raise
                run.on_retriever_end(result)
                return result

        return Context().run(local_invoke)

    async def ainvoke(self, input: str, config=None, **kwargs) -> RetrievedDocuments:
        _local_config(config, kwargs)
        return await asyncio.to_thread(self.invoke, input, config, **kwargs)

    def model_copy(self, *, update=None, deep=False):
        # Pydantic's frozen flag intentionally does not validate model_copy
        # updates. Do not expose that public API as an identity-rebinding path.
        if update is not None:
            raise KnowledgeAccessError('受控身份与查询上下文不可通过复制覆盖')
        return super().model_copy(deep=deep)

    def copy(self, *args, **kwargs):
        raise KnowledgeAccessError('受控身份与查询上下文不可通过旧版复制API覆盖')

    def configurable_fields(self, **kwargs):
        raise KnowledgeAccessError('受控身份与查询上下文不可配置覆盖；须由服务端创建新Retriever')

    def configurable_alternatives(self, *args, **kwargs):
        raise KnowledgeAccessError('受控检索不允许替换授权或检索实现')

    def with_listeners(self, **kwargs):
        raise KnowledgeAccessError('受控知识检索禁止外部回调监听')

    def with_alisteners(self, **kwargs):
        raise KnowledgeAccessError('受控知识检索禁止外部回调监听')

    def stream_events(self, *args, **kwargs):
        raise KnowledgeAccessError('受控知识检索禁止事件追踪')
        yield  # Preserve the Runnable iterator contract without executing a tracer.

    async def astream_events(self, *args, **kwargs):
        raise KnowledgeAccessError('受控知识检索禁止事件追踪')
        yield

    async def astream_log(self, *args, **kwargs):
        raise KnowledgeAccessError('受控知识检索禁止事件追踪')
        yield


def retrieve_rows(query, *, principal, repository=None, engine=None, **query_context):
    """Formal domain bridge: invoke LangChain, then adapt that fresh response.

    The bridge never accepts previously returned/cached Documents as inputs.
    Statistics remain available for an empty result and match the same invoke.
    """
    retriever = ControlledKnowledgeRetriever(principal=principal, repository=repository,
        engine=engine, **query_context)
    documents = retriever.invoke(query)
    rows = []
    for document in documents:
        metadata = document.metadata
        rows.append({'text': document.page_content, 'chunk_id': document.id,
                     'document_id': metadata['document_id'], 'version_id': metadata['version_id'],
                     'release_id': metadata['release_id'], 'generation': metadata['generation'],
                     'meta': deepcopy(metadata['catalog']), 'score': metadata['score'],
                     'route_scores': deepcopy(metadata['route_scores']), 'rerank_score': metadata['rerank_score'],
                      'retrieval_scores': deepcopy(metadata.get('retrieval_scores', {})),
                      'rerank_details': deepcopy(metadata.get('rerank_details')),
                      'domain_evidence': deepcopy(metadata.get('domain_evidence', []))})
    return rows, deepcopy(documents.stats)
