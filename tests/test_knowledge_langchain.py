"""Synthetic, isolated acceptance of the actual LangChain formal retrieval path.

No model, real managed root, HTTP service, RPA endpoint, or remote trace is used.
"""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar
from dataclasses import FrozenInstanceError
from datetime import date as RealDate
import json
import socket
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from langchain_core.callbacks.base import BaseCallbackHandler
from langchain_core.callbacks.manager import CallbackManager
from langchain_core.documents import Document
from langchain_core.retrievers import BaseRetriever
from langchain_core.runnables.config import var_child_runnable_config
from langchain_core.tracers import context as tracer_context
from langsmith.run_helpers import get_tracing_context, tracing_context

from enterprise.knowledge import KnowledgeAccessError, KnowledgeError, Repository
from enterprise.knowledge_context import context
from enterprise.knowledge_langchain import ControlledKnowledgeRetriever, RetrievedDocuments, retrieve_rows
from enterprise.knowledge_release import ReleaseRepository, get_search_engine
from enterprise.security import Principal

ADMIN = Principal('admin', '知识管理员', ('knowledge_admin',), ('*',), ('*',))
READER = Principal('reader', '一厂分析员', ('analyst',), ('中药一厂',), ('甲产品',))


@pytest.fixture(autouse=True)
def no_model_loading(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('Synthetic LangChain acceptance must never load a model')
    monkeypatch.setattr('enterprise.knowledge_release._load_local_bge', forbidden)


@pytest.fixture
def repo(tmp_path):
    return Repository(tmp_path / 'managed')


def confirm(repo, title='受控工艺', text='甲产品 提取工艺 收率记录 原材料耗用核查', **kwargs):
    options = {'filename': title + '.txt', 'title': title, 'scope_products': ['甲产品'],
               'scope_factories': ['中药一厂'], 'visibility': 'scoped', 'effective_from': '2026-01-01',
               'category': '工艺', 'actor': 'ignored', 'principal': ADMIN,
               'metadata': {'evidence_role': 'document_basis', 'authority': 'primary',
                            'limitations': ['机制资料不证明本期发生异常']}}
    options.update(kwargs)
    staged = repo.stage(text.encode(), **options)
    assert not staged['errors'], staged
    return repo.commit(staged['stage_id'], 'ignored', '测试审核', principal=ADMIN)


def publish(repo):
    result = ReleaseRepository(repository=repo).publish(principal=ADMIN, embedding_model_path='')
    assert result['status'] == 'published'
    return result


def retriever(repo, **kwargs):
    options = {'principal': READER, 'repository': repo, 'product': '甲产品', 'factory': '中药一厂',
               'as_of': '2026-06-30', 'engine': get_search_engine(repository=repo, embedding_model_path='')}
    options.update(kwargs)
    return ControlledKnowledgeRetriever(**options)


def test_real_base_retriever_returns_actual_chunk_documents_and_stats(repo):
    version = confirm(repo)
    release = publish(repo)
    adapter = retriever(repo)
    assert isinstance(adapter, BaseRetriever)
    documents = adapter.invoke('提取工艺 收率记录')
    assert isinstance(documents, RetrievedDocuments) and documents
    assert all(isinstance(document, Document) for document in documents)
    first = documents[0]
    assert first.page_content == version['text']
    assert first.id == first.metadata['chunk_id']
    assert first.metadata['version_id'] == version['version_id']
    assert first.metadata['catalog']['sha256'] == version['sha256']
    assert first.metadata['catalog']['business_metadata'] == version['business_metadata']
    assert first.metadata['release_id'] == first.metadata['generation'] == release['release_id']
    assert set(documents.stats['route_generations'].values()) == {release['release_id']}
    assert documents.stats['framework']['name'] == 'langchain-core'
    assert documents.stats['framework']['version'] == '1.6.2'
    assert documents.stats['framework']['tracing'] == 'disabled'
    assert documents.stats['retrieval_mode'] == 'bm25_graph_fallback'
    assert 'principal' not in first.metadata and 'roles' not in first.metadata


def test_all_candidate_acl_unknown_expired_future_and_scope_restrictions_remain(repo):
    good = confirm(repo)
    confirm(repo, title='二厂机密', scope_factories=['中药二厂'])
    confirm(repo, title='其他产品', scope_products=['乙产品'])
    confirm(repo, title='未来', effective_from='2027-01-01')
    confirm(repo, title='过期', effective_to='2026-01-31')
    staged = repo.stage('甲产品 收率 未知范围'.encode(), 'unknown.txt', 'unknown', [], '2026-01-01', '未治理', 'internal')
    repo.commit(staged['stage_id'], 'internal', '待治理')
    publish(repo)
    result = retriever(repo).invoke('甲产品 收率')
    assert {document.metadata['version_id'] for document in result} == {good['version_id']}
    with pytest.raises(KnowledgeAccessError):
        retriever(repo, factory='中药二厂')
    with pytest.raises(KnowledgeAccessError):
        retriever(repo, allowed_scopes={'products': ['*'], 'factories': ['*']})


def test_bound_identity_and_query_context_cannot_be_overridden(repo):
    confirm(repo)
    publish(repo)
    mutable_identity = SimpleNamespace(**READER.__dict__)
    scopes = {'products': ['甲产品'], 'factories': ['中药一厂']}
    adapter = retriever(repo, principal=mutable_identity, allowed_scopes=scopes)
    mutable_identity.products = ('*',)
    scopes['products'].append('乙产品')
    assert adapter.principal.products == ('甲产品',)
    assert adapter.query_context.allowed_products == ('甲产品',)
    with pytest.raises((ValidationError, FrozenInstanceError)):
        adapter.principal = ADMIN
    with pytest.raises(FrozenInstanceError):
        adapter.query_context.product = '乙产品'
    invalid_identity = SimpleNamespace(**READER.__dict__)
    invalid_identity.factories = '*'
    with pytest.raises(KnowledgeAccessError):
        retriever(repo, principal=invalid_identity)
    for config in ({'principal': ADMIN}, {'configurable': {'principal': ADMIN}},
                   {'configurable': {'factory': '中药二厂'}}, {'metadata': {'api_key': 'sentinel'}}):
        with pytest.raises(KnowledgeAccessError):
            adapter.invoke('收率', config=config)
    with pytest.raises(KnowledgeAccessError):
        adapter.invoke('收率', principal=ADMIN)
    with pytest.raises(KnowledgeAccessError):
        adapter.bind(factory='中药二厂').invoke('收率')
    with pytest.raises(KnowledgeAccessError):
        adapter.configurable_fields(principal='anything')
    with pytest.raises(KnowledgeAccessError):
        adapter.model_copy(update={'principal': ADMIN})
    with pytest.raises(KnowledgeAccessError):
        adapter.copy(update={'query_context': {'factory': '中药二厂'}})
    assert adapter.model_copy().principal == adapter.principal


def test_metadata_revision_historical_cutoff_and_current_revocation_no_document_cache(repo):
    old = confirm(repo)
    first_release = publish(repo)
    current_adapter = retriever(repo)
    assert current_adapter.invoke('提取工艺')[0].metadata['version_id'] == old['version_id']
    new = confirm(repo, doc_id=old['doc_id'], text='甲产品 提取工艺 新版收率记录')
    newest = publish(repo)
    assert current_adapter.invoke('提取工艺')[0].metadata['version_id'] == new['version_id']
    historical = retriever(repo, release_id=first_release['release_id'], known_at=first_release['published_at'])
    assert historical.invoke('提取工艺')[0].metadata['version_id'] == old['version_id']
    assert current_adapter.invoke('提取工艺').stats['release_id'] == newest['release_id']
    ReleaseRepository(repository=repo).revoke_document(old['doc_id'], principal=ADMIN, reason='撤销资料')
    # These same retriever instances previously returned Documents. No cached
    # list is reused, and even a historical release/cutoff cannot bypass revoke.
    assert current_adapter.invoke('提取工艺') == []
    assert historical.invoke('提取工艺') == []


def test_effective_expiry_is_rechecked_on_same_retriever_with_current_date(repo, monkeypatch):
    import enterprise.knowledge_release as release_module
    confirm(repo, effective_to='2026-06-30')
    publish(repo)
    current = [RealDate(2026, 6, 30)]
    class ClockDate:
        @classmethod
        def today(cls): return current[0]
    monkeypatch.setattr(release_module, 'date', ClockDate)
    adapter = retriever(repo, as_of=None)
    assert adapter.invoke('收率')
    current[0] = RealDate(2026, 7, 1)
    assert adapter.invoke('收率') == []


def test_stats_are_per_invocation_and_documents_cannot_poison_next_call(repo):
    value = confirm(repo)
    publish(repo)
    adapter = retriever(repo)
    first = adapter.invoke('收率')
    first[0].page_content = 'tampered response'
    first[0].metadata['catalog']['filename'] = 'tampered.txt'
    first.stats['framework']['name'] = 'tampered'
    second = adapter.invoke('收率')
    assert second[0].page_content == value['text']
    assert second[0].metadata['catalog']['filename'] == value['filename']
    assert second.stats['framework']['name'] == 'langchain-core'
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(adapter.invoke, ['收率', 'zzzxxyynothing']))
    assert results[0] and results[0].stats['no_answer'] is False
    assert results[1] == [] and results[1].stats['no_answer'] is True
    assert not hasattr(adapter, 'last_documents')


def test_empty_publication_has_framework_stats_without_implicit_writes(repo):
    result = retriever(repo).invoke('收率')
    assert result == [] and result.stats['reason'] == 'no_published_release'
    assert result.stats['framework']['name'] == 'langchain-core'
    assert not repo.root.exists()


def test_metadata_secret_fields_redacted_without_losing_authorized_provenance(repo):
    governance = {'evidence_role': 'document_basis', 'authority': 'primary',
                  'api_key': 'DO_NOT_EXPOSE', 'nested': {'client_secret': 'DO_NOT_EXPOSE'},
                  'known_conflicts': ['核对原始凭证'], 'limitations': ['仅作为工艺背景']}
    confirmed = confirm(repo, metadata=governance)
    publish(repo)
    document = retriever(repo).invoke('收率')[0]
    encoded = json.dumps(document.metadata, ensure_ascii=False)
    assert 'DO_NOT_EXPOSE' not in encoded
    assert document.metadata['business_metadata']['known_conflicts'] == ['核对原始凭证']
    assert document.metadata['document_sha256'] == confirmed['sha256']
    assert document.metadata['metadata_redactions'] == ['catalog.business_metadata.api_key',
                                                       'catalog.business_metadata.nested.client_secret']


def test_ambient_tracing_callbacks_hooks_debug_and_socket_never_receive_evidence(repo, monkeypatch):
    import httpx
    import requests
    import urllib.request
    from langchain_core.globals import get_debug, set_debug
    confirm(repo)
    publish(repo)
    adapter = retriever(repo)
    calls = []
    class Spy(BaseCallbackHandler):
        def on_retriever_start(self, *args, **kwargs): calls.append('callback_start')
        def on_retriever_end(self, *args, **kwargs): calls.append('callback_end')
    spy = Spy()
    def no_network(*args, **kwargs):
        calls.append('network')
        raise AssertionError('controlled retrieval must never open a remote trace socket')
    for key in ('LANGSMITH_TRACING', 'LANGSMITH_TRACING_V2', 'LANGCHAIN_TRACING', 'LANGCHAIN_TRACING_V2', 'LANGCHAIN_HANDLER'):
        monkeypatch.setenv(key, 'true')
    monkeypatch.setenv('LANGSMITH_API_KEY', 'SENTINEL_NOT_A_REAL_KEY')
    monkeypatch.setenv('LANGSMITH_ENDPOINT', 'https://untrusted.invalid')
    monkeypatch.setattr(socket, 'create_connection', no_network)
    monkeypatch.setattr(socket.socket, 'connect', no_network)
    monkeypatch.setattr(httpx.Client, 'send', no_network)
    monkeypatch.setattr(requests.Session, 'request', no_network)
    monkeypatch.setattr(urllib.request.OpenerDirector, 'open', no_network)
    monkeypatch.setattr(CallbackManager, 'configure', classmethod(lambda *a, **k: no_network()))
    variable = ContextVar('untrusted_retrieval_hook', default=spy)
    monkeypatch.setattr(tracer_context, '_configure_hooks', [(variable, True, None, None)])
    callbacks_token = var_child_runnable_config.set({'callbacks': [spy], 'metadata': {'api_key': 'SENTINEL'}})
    tracer_token = tracer_context.tracing_v2_callback_var.set(spy)
    previous_debug = get_debug()
    try:
        set_debug(True)
        with tracing_context(enabled=True, metadata={'api_key': 'AMBIENT_SENTINEL'}):
            result = adapter.invoke('收率', config={'callbacks': [], 'metadata': {}, 'tags': []})
            assert result and get_tracing_context()['enabled'] is True
        assert calls == []
        assert 'SENTINEL' not in json.dumps(result[0].metadata)
        with pytest.raises(KnowledgeAccessError):
            adapter.invoke('收率', config={'callbacks': [spy]})
        with pytest.raises(KnowledgeAccessError):
            adapter.with_config(callbacks=[spy]).invoke('收率')
        assert calls == []
    finally:
        set_debug(previous_debug)
        var_child_runnable_config.reset(callbacks_token)
        tracer_context.tracing_v2_callback_var.reset(tracer_token)


def test_async_batch_and_stream_use_same_controlled_invocations(repo):
    value = confirm(repo)
    release = publish(repo)
    adapter = retriever(repo)
    async_result = asyncio.run(adapter.ainvoke('收率'))
    assert async_result[0].metadata['version_id'] == value['version_id']
    batches = adapter.batch(['收率', 'zzzxxyynothing'], config={'max_concurrency': 2})
    assert batches[0] and batches[1] == []
    assert batches[0].stats['generation'] == release['release_id']
    assert list(adapter.stream('收率'))[0][0].id == async_result[0].id
    with pytest.raises(KnowledgeAccessError):
        list(adapter.stream_events('收率'))


def test_formal_knowledge_context_and_report_evidence_execute_adapter_invoke(repo, monkeypatch):
    from enterprise.analysis_service import report_evidence
    good = confirm(repo)
    release = publish(repo)
    calls = []
    original = ControlledKnowledgeRetriever.invoke
    def observed(self, query, config=None, **kwargs):
        calls.append((self.principal.user_id, self.query_context.as_of, self.query_context.release_id))
        return original(self, query, config, **kwargs)
    monkeypatch.setattr(ControlledKnowledgeRetriever, 'invoke', observed)
    refs, stats = context('甲产品', '2026-05', '提取工艺 收率', repository=repo,
                           principal=READER, factory='中药一厂', return_stats=True)
    assert refs[0]['version_id'] == good['version_id'] and stats['framework']['name'] == 'langchain-core'
    evidence = report_evidence(READER, '甲产品', '测试规格', ['2026-05', '2026-06'], root=repo.root)
    assert evidence and evidence[0]['version_id'] == good['version_id']
    assert evidence[0]['retrieval_framework']['name'] == 'langchain-core'
    assert evidence[0]['scope']['months'] == ['2026-05', '2026-06']
    assert len(calls) == 3
    assert calls[2][2] == release['release_id']


def test_three_routes_share_authorized_generation_and_fingerprint_failure_is_preserved(repo, tmp_path, monkeypatch):
    # Synthetic two-dimensional vectors test the adapter contract. This is not
    # a model-quality benchmark; no external model or actual weights are read.
    import enterprise.knowledge_release as releases
    path = tmp_path / 'synthetic-encoder'
    path.mkdir()
    (path / 'config.json').write_text('{}', encoding='utf-8')
    class SyntheticEncoder:
        def encode(self, texts, batch_size):
            return {'dense_vecs': [[1.0, 0.25] for _ in texts]}
    monkeypatch.setattr(releases, '_load_local_bge', lambda _path: SyntheticEncoder())
    visible = confirm(repo)
    hidden = confirm(repo, title='其他工厂机密', scope_factories=['中药二厂'])
    release = ReleaseRepository(repository=repo).publish(principal=ADMIN, embedding_model_path=path, require_embeddings=True)
    assert release['status'] == 'published'
    engine = get_search_engine(repository=repo, embedding_model_path=path)
    assert engine.warmup(principal=READER, wait=True)['state'] == 'ready'
    adapter = retriever(repo, engine=engine)
    result = adapter.invoke('收率')
    assert {item.metadata['version_id'] for item in result} == {visible['version_id']}
    assert hidden['version_id'] not in result.stats['authorized_version_ids']
    assert result.stats['retrieval_mode'] == 'vector_bm25_graph'
    assert result.stats['vector_n'] == 1 and not result.stats['degraded']
    assert set(result.stats['route_generations'].values()) == {release['release_id']}
    monkeypatch.setattr(engine, 'warmup', lambda **kwargs: {'state': 'ready', 'fingerprint': {'invalid': True}})
    fallback = adapter.invoke('收率')
    assert fallback and fallback.stats['degraded']
    assert fallback.stats['vector_n'] == 0
    assert 'embedding_fingerprint_mismatch' in fallback.stats['degradation_reasons']
    assert 'embedding_fingerprint_mismatch' in fallback[0].metadata['degradation_reasons']


def test_new_unpublished_revision_and_corrupted_artifact_cannot_resurface_prior_documents(repo):
    old = confirm(repo)
    release = publish(repo)
    adapter = retriever(repo)
    assert adapter.invoke('收率')
    confirm(repo, doc_id=old['doc_id'], metadata={'authority': 'revision_not_published'})
    assert adapter.invoke('收率') == []
    historical = retriever(repo, known_at=release['published_at'], release_id=release['release_id'])
    assert historical.invoke('收率')[0].metadata['version_id'] == old['version_id']
    artifact = repo.root / 'knowledge_releases' / release['release_id'] / 'index.sqlite'
    artifact.write_bytes(b'integrity_failure')
    with pytest.raises(KnowledgeError):
        historical.invoke('收率')


def test_domain_bridge_preserves_engine_stats_and_never_accepts_old_documents(repo):
    confirm(repo)
    publish(repo)
    rows, stats = retrieve_rows('收率', principal=READER, repository=repo, product='甲产品',
                               factory='中药一厂', as_of='2026-06-30')
    assert rows[0]['meta']['scope_products'] == ['甲产品']
    assert rows[0]['release_id'] == stats['release_id']
    assert stats['degraded'] and stats['degradation_reasons']
    with pytest.raises(KnowledgeAccessError):
        retriever(repo).invoke('收率', config={'documents': rows})
