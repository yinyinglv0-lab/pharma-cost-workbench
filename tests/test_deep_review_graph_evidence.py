"""Portable bounded evidence-display tests; synthetic graph and lexical release.

No repository originals, production DB, model download/encoding, LLM or API
server are used. Increasing displayed evidence must not change ranking math.
"""
from copy import deepcopy
import hashlib

import pytest

from enterprise.domain_graph import DomainGraphIndex
from enterprise.knowledge import Repository
import enterprise.knowledge_release as kr
from enterprise.security import Principal

ADMIN = Principal('evidence-test', 'Synthetic evidence test', ('knowledge_admin',), ('*',), ('*',))
DISPLAY_FIELDS = {'domain_evidence', 'domain_evidence_count', 'domain_evidence_limit',
                  'domain_evidence_truncated'}


def fixture_graph(count):
    graph = DomainGraphIndex()
    source = graph.add_entity('process', '灌装')
    graph.chunk_entities['flow'].add(source)
    graph.chunk_entities['other'].add(source)
    labels = ['过滤', '灭菌', '包装', '灌封一体机', '浓缩', '配制', '灯检', '制粒', '干燥', '分装', '混合']
    lines, offset = [], 0
    for index, label in enumerate(labels[:count]):
        equipment = index == 3
        target = graph.add_entity('equipment' if equipment else 'process', label)
        relation_type = 'process_uses_equipment' if equipment else 'process_precedes_process'
        quote = f'灌装使用{label}' if equipment else f'灌装→{label}'
        support = {'chunk_id': 'flow', 'version_id': 'v', 'offset': offset,
                   'end_offset': offset + len(quote), 'quote': quote,
                   'quote_sha256': hashlib.sha256(quote.encode()).hexdigest(),
                   'evidence_scope': {'products': ['银黄口服液'], 'factories': ['中药一厂']},
                   'applicability': {'kind': 'product', 'products': ['银黄口服液']}}
        graph.add_relation({'relation_id': f'evidence-{index}', 'source_entity_id': source,
                            'target_entity_id': target, 'relation_type': relation_type,
                            'source_type': 'process', 'target_type': 'equipment' if equipment else 'process',
                            'semantic_status': 'semantic_template_supported', 'support': support})
        graph.chunk_entities['flow'].add(target)
        lines.append(quote)
        offset += len(quote) + 1
    rows = [{'chunk_id': 'flow', 'text': '\n'.join(lines) or '灌装'},
            {'chunk_id': 'other', 'text': '灌装仅供索引'}]
    return graph, rows


def rerank(graph, rows):
    return kr.ControlledSearchEngine._controlled_rerank(
        '灌装', [('other', .9), ('flow', .8)], rows, graph,
        legacy_order=['other', 'flow'])


@pytest.mark.parametrize('count', [0, 3, 4, 8, 11])
def test_evidence_count_limit_truncation_and_unchanged_raw_support(count):
    graph, rows = fixture_graph(count)
    before = deepcopy(graph.relations)
    ranked, _ = rerank(graph, rows)
    detail = next(detail for chunk, score, detail in ranked if chunk == 'flow')
    assert kr.DOMAIN_EVIDENCE_LIMIT == 8
    assert detail['relation_count'] == count
    assert detail['domain_evidence_count'] == min(count, 8)
    assert detail['domain_evidence_limit'] == 8
    assert detail['domain_evidence_truncated'] is (count > 8)
    expected = graph.evidence_for_chunk('flow', '灌装')[:8]
    assert detail['domain_evidence'] == expected
    assert graph.relations == before
    for edge in detail['domain_evidence']:
        support = edge['support']
        assert rows[0]['text'][support['offset']:support['end_offset']] == support['quote']
        assert hashlib.sha256(support['quote'].encode()).hexdigest() == support['quote_sha256']
        assert support['evidence_scope']['products'] == ['银黄口服液']
    if count >= 4:
        assert detail['domain_evidence'][3]['relation_type'] == 'process_uses_equipment'


def test_fourth_equipment_relation_survives_without_changing_rank_math(monkeypatch):
    graph, rows = fixture_graph(11)
    reranker_contract = deepcopy(kr.RERANKER)
    monkeypatch.setattr(kr, 'DOMAIN_EVIDENCE_LIMIT', 3)
    old_ranked, old_trace = rerank(graph, rows)
    monkeypatch.setattr(kr, 'DOMAIN_EVIDENCE_LIMIT', 8)
    new_ranked, new_trace = rerank(graph, rows)
    assert [(chunk, score) for chunk, score, _ in old_ranked] == [
        (chunk, score) for chunk, score, _ in new_ranked]
    assert old_trace == new_trace
    for old, new in zip(old_ranked, new_ranked):
        assert {k: v for k, v in old[2].items() if k not in DISPLAY_FIELDS} == {
            k: v for k, v in new[2].items() if k not in DISPLAY_FIELDS}
    old = next(detail for chunk, _, detail in old_ranked if chunk == 'flow')
    new = next(detail for chunk, _, detail in new_ranked if chunk == 'flow')
    assert not any(edge['relation_type'] == 'process_uses_equipment' for edge in old['domain_evidence'])
    assert any(edge['relation_type'] == 'process_uses_equipment' for edge in new['domain_evidence'])
    assert kr.RERANKER == reranker_contract  # display is not an immutable manifest/ranking change


def test_actual_lexical_search_and_api_adapter_return_consistent_metadata(tmp_path, monkeypatch):
    repo = Repository(tmp_path / 'isolated-evidence')
    text = '银黄口服液生产工艺\n配制→过滤→灌装→灭菌\n灌装使用灌封一体机\n'
    staged = repo.stage(text.encode(), 'synthetic-flow.txt', 'Synthetic flow evidence',
        ['银黄口服液'], '2026-01-01', '生产工艺', ADMIN.user_id,
        scope_factories=['中药一厂'], visibility='scoped', principal=ADMIN)
    assert not staged['errors']
    repo.commit(staged['stage_id'], ADMIN.user_id, 'Isolated lexical evidence-display test', principal=ADMIN)
    release = kr.ReleaseRepository(repository=repo).publish(
        principal=ADMIN, embedding_model_path='', require_embeddings=False)
    assert release['status'] == 'published' and release['manifest']['embedding']['status'] == 'disabled'
    engine = kr.ControlledSearchEngine(repository=repo, embedding_model_path='')
    query = '配制 过滤 灌装 灭菌 灌封一体机'
    kwargs = dict(principal=ADMIN, product='银黄口服液', factory='中药一厂',
                  as_of='2026-06-30', top_k=4, require_hybrid=False)
    monkeypatch.setattr(kr, 'DOMAIN_EVIDENCE_LIMIT', 3)
    old_rows, old_stats = engine.search(query, **kwargs)
    monkeypatch.setattr(kr, 'DOMAIN_EVIDENCE_LIMIT', 8)
    rows, stats = engine.search(query, **kwargs)
    assert rows and stats['retrieval_mode'] == 'bm25_graph_fallback'
    assert [(r['chunk_id'], r['score'], r['rerank_score'], r['route_scores']) for r in old_rows] == [
        (r['chunk_id'], r['score'], r['rerank_score'], r['route_scores']) for r in rows]
    assert old_stats['rerank_trace'] == stats['rerank_trace']
    item = rows[0]
    assert item['rerank_details']['relation_count'] == 4
    assert len(item['domain_evidence']) == 4
    assert {e['relation_type'] for e in item['domain_evidence']} == {
        'process_precedes_process', 'process_uses_equipment'}
    converted = kr.result_to_api(item)
    for field in DISPLAY_FIELDS:
        assert item[field] == item['rerank_details'][field] == converted[field]
    assert item['domain_evidence_count'] == 4 and item['domain_evidence_truncated'] is False
    assert item['domain_evidence_limit'] == 8
    for edge in item['domain_evidence']:
        support = edge['support']
        assert text[support['offset']:support['end_offset']] == support['quote']
    disabled, _ = engine.search(query, **kwargs, use_rerank=False)
    assert disabled and disabled[0]['rerank_details'] is None
    assert disabled[0]['domain_evidence'] == [] and disabled[0]['domain_evidence_count'] == 0
    assert disabled[0]['domain_evidence_truncated'] is False
    assert kr.result_to_api(disabled[0])['domain_evidence_limit'] == 8
