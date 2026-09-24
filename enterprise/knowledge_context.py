"""Report evidence adapter for the published, preauthorized knowledge engine."""
from calendar import monthrange
from copy import deepcopy
from pathlib import PureWindowsPath
import re

from enterprise.knowledge import KnowledgeError, Repository, require_principal
from enterprise.knowledge_release import get_search_engine
from enterprise.knowledge_langchain import retrieve_rows
from enterprise.knowledge_applicability import evidence_policy


def context(product, month, query='', repository=None, top_k=4, *, principal=None,
            factory=None, specification=None, known_at=None, allowed_scopes=None, release_id=None,
            return_stats=False):
    """Return versioned evidence only from a successfully published generation.

    No identity, unknown scope, or an unpublished catalog never triggers legacy
    filename compensation. ``return_stats=True`` exposes downgrade/readiness to
    the application, which must retain it alongside the analysis evidence.
    """
    repo = repository or Repository()
    principal = principal if principal is not None else repo.principal
    require_principal(principal)
    if not isinstance(month, str) or not re.fullmatch(r'\d{4}-(0[1-9]|1[0-2])', month):
        raise KnowledgeError('月份须为YYYY-MM')
    year, mon = map(int, month.split('-'))
    rows, stats = retrieve_rows(query or product or '知识资料', principal=principal,
        repository=repo, engine=get_search_engine(repository=repo),
        product=product, factory=factory, as_of=f'{month}-{monthrange(year, mon)[1]:02d}',
        known_at=known_at, allowed_scopes=allowed_scopes, release_id=release_id, top_k=top_k)
    sources, excluded = [], []
    for row in rows:
        meta = row['meta']
        policy = evidence_policy(meta, product, specification)
        if not policy['included']:
            excluded.append({'chunk_id': row['chunk_id'], 'reason': policy['applicability_status']})
            continue
        sources.append({'id': f'K{len(sources) + 1:03}', 'text': row['text'],
            **{key: value for key, value in policy.items() if key != 'included'},
            'source': {'file': meta['filename'],
                     'page': meta.get('page_hint'), 'pages': meta.get('pages', []),
                     'section': meta.get('section', ''), 'offset': meta.get('offset'),
                     'end_offset': meta.get('end_offset', meta.get('offset', 0) + len(row['text'])),
                     'key': {'文档版本': meta['version'],
                             '生效日期': meta['effective_from'], '文档片段': meta['ordinal'],
                             '发布版本': row['release_id'], '正文起始字符': meta['offset']}},
             'retrieval_scores': {'rrf_score': row.get('score'),
                                  'rerank_score': row.get('rerank_score'),
                                  'vector_score': (row.get('route_scores') or {}).get('vector'),
                                  'bm25_score': (row.get('route_scores') or {}).get('bm25'),
                                  'graph_score': (row.get('route_scores') or {}).get('graph'),
                                  'domain_graph_score': (row.get('route_scores') or {}).get('domain_graph')},
             'rerank_details': row.get('rerank_details'),
             'domain_evidence': row.get('domain_evidence', []),
            'document_id': row['document_id'], 'version_id': row['version_id'],
            'document_sha256': meta['sha256'], 'chunk_id': row['chunk_id'],
            'release_id': row['release_id'], 'index_release_id': row['release_id'],
            'generation': row['generation'], 'kb': '受控知识文档', 'type': meta['category'],
            'business_metadata': meta['business_metadata'],
            'authority': meta['business_metadata'].get('authority', 'unreviewed'),
            'limitations': meta['business_metadata'].get('limitations', []),
            'known_conflicts': meta['business_metadata'].get('known_conflicts', []),
            'retrieval': stats['retrieval_mode'], 'retrieval_degraded': stats['degraded'],
            'retrieval_framework': stats['framework'],
            'degradation_reasons': stats['degradation_reasons'],
            'scope': '所有候选生成前验证身份范围、业务有效日期、确认时间及发布版本；经营原因仍需复核'})
    stats = deepcopy(stats)
    stats.update(applicability_excluded=excluded, returned_n=len(sources), no_answer=not sources)
    if rows and not sources:
        stats['reason'] = 'no_applicable_evidence'
    return (sources, stats) if return_stats else sources


def managed_names(repository=None, *, principal=None):
    """Compatibility metadata listing only; never an authorization or search filter."""
    repo = repository or Repository()
    principal = principal if principal is not None else repo.principal
    require_principal(principal)
    return {PureWindowsPath(row['filename']).name for row in repo.history(principal=principal) if row.get('filename')}
