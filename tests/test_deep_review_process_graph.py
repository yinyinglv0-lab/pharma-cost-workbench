"""Offline text-flow acceptance: no OCR, dense model, live DB or publication.

The optional supplied-PDF test reads original bytes and publishes ONLY inside a
pytest temporary root; its engine explicitly opts into lexical graph testing.
"""
from __future__ import annotations

from collections import Counter
import hashlib
from pathlib import Path
import sqlite3

import pytest

from enterprise.domain_graph import (
    DOMAIN_GRAPH, DOMAIN_GRAPH_V2, SEMANTIC_TYPES, build_domain_graph, load_domain_graph,
)
from enterprise.knowledge import Repository, preview_file
from enterprise.knowledge_release import CHUNKER, ControlledSearchEngine, ReleaseRepository, _chunks
from enterprise.security import Principal

ADMIN = Principal('process-test-admin', '隔离图测试', ('knowledge_admin',), ('*',), ('*',))
PRODUCTS = ['银黄口服液', '板蓝根颗粒', '六味地黄胶囊']


def row(text, *, products=None, offset=19):
    return {'chunk_id': 'c', 'version_id': 'v', 'text': text, 'meta': {
        'doc_id': 'd', 'filename': 'flow.txt', 'sha256': 'a' * 64,
        'offset': offset, 'end_offset': offset + len(text),
        'scope_products': products or ['银黄口服液'], 'scope_factories': ['中药一厂'],
    }}


def flows(graph):
    return [r for r in graph.relations if r['relation_type'] == 'process_precedes_process']


def names(graph):
    return {(graph.entities[e['source_entity_id']]['name'], graph.entities[e['target_entity_id']]['name'])
            for e in flows(graph)}


def test_v3_is_additive_and_old_v2_definition_is_unchanged():
    assert DOMAIN_GRAPH['version'] == 3 and DOMAIN_GRAPH_V2['version'] == 2
    assert 'process_precedes_process' not in DOMAIN_GRAPH_V2['relation_types']
    assert SEMANTIC_TYPES['process_precedes_process'] == ('process', 'process')
    assert set(DOMAIN_GRAPH['relation_types']) - set(DOMAIN_GRAPH_V2['relation_types']) == {'process_precedes_process'}
    # Old/no-domain SQLite artifacts remain readable and aren't rewritten.
    with sqlite3.connect(':memory:') as conn:
        assert not load_domain_graph(conn, ['v']).relations


@pytest.mark.parametrize('arrow', ['→', '->', '⇒'])
def test_only_explicit_connected_right_arrows_have_exact_hashed_support(arrow):
    source = row(f'银黄口服液生产工艺\n提取(100℃) {arrow} 浓缩 {arrow} 配制\n')
    graph = build_domain_graph([source])
    assert names(graph) == {('提取', '浓缩'), ('浓缩', '配制')}
    for edge in flows(graph):
        support = edge['support']
        assert support['evidence_scope']['products'] == ['银黄口服液']
        assert source['text'][support['offset'] - 19:support['end_offset'] - 19] == support['quote']
        assert support['quote_sha256'] == hashlib.sha256(support['quote'].encode()).hexdigest()
        assert arrow in support['quote']
        for endpoint in ('source_process_span', 'target_process_span'):
            span = support[endpoint]
            assert source['text'][span['offset'] - 19:span['end_offset'] - 19] == span['quote']


@pytest.mark.parametrize('text', [
    '提取 浓缩 配制', '1. 提取\n2. 浓缩', '提取+浓缩',
    '提取→未知处理→浓缩', '提取液→浓缩', '多功能提取罐→浓缩',
    '干燥→流化床干燥机→混合', '提取←浓缩', '提取 <- 浓缩',
    '提取→浓缩←干燥', '提取\n\n↓\n浓缩',
    '提取→未知处理\n↓\n浓缩', '提取→多功能提取罐\n↓\n浓缩',
    '未知→提取\n↓\n浓缩',
    '提取\n│    │\n↓    ↓\n浓缩 混合', '提取→\n二、其他章节\n浓缩',
])
def test_proximity_numbers_equipment_unknown_nodes_and_ambiguous_branches_do_not_order(text):
    assert not flows(build_domain_graph([row(text)]))


def test_vertical_flow_single_column_and_left_arrow_diagnostics():
    graph = build_domain_graph([row('配制\n│\n↓\n过滤\n↓\n灌装\n提取←浓缩')])
    assert names(graph) == {('配制', '过滤'), ('过滤', '灌装')}
    assert any(d['reason'] == 'unsupported_left_arrow_layout' for d in graph.diagnostics)


def test_scope_crossings_never_become_order_and_acl_is_not_section_scope():
    text = '二、银黄口服液生产工艺\n提取→浓缩\n三、板蓝根颗粒生产工艺\n混合→制粒\n'
    graph = build_domain_graph([row(text, products=PRODUCTS)])
    assert names(graph) == {('提取', '浓缩'), ('混合', '制粒')}
    scopes = {e['support']['quote']: e['support']['evidence_scope']['products'] for e in flows(graph)}
    assert scopes['提取→浓缩'] == ['银黄口服液']
    assert scopes['混合→制粒'] == ['板蓝根颗粒']
    unknown = build_domain_graph([row('提取→浓缩', products=PRODUCTS)])
    assert not flows(unknown)
    assert unknown.diagnostics[0]['reason'] == 'ambiguous_product_section'


def test_real_supplied_pdf_routes_and_lexical_release_retrieval(tmp_path):
    path = Path(__file__).resolve().parents[1] / '生产工艺文档_中药一厂.pdf'
    if not path.is_file():
        pytest.skip('Supplied original PDF not present in portable source bundle')
    raw = path.read_bytes()
    raw_hash = hashlib.sha256(raw).hexdigest()
    parsed = preview_file(raw, path.name)
    assert not parsed['errors'] and parsed['parser'] == 'pypdf'
    from pypdf import PdfReader
    assert sum(len(page.images) for page in PdfReader(path).pages) == 0
    repo = Repository(tmp_path / 'isolated-process-graph')
    staged = repo.stage(raw, path.name, '中药一厂生产工艺', PRODUCTS, '2025-06-01',
        '生产工艺', ADMIN.user_id, scope_factories=['中药一厂'], visibility='scoped',
        metadata={'authority': 'supplied-source-test', 'evidence_role': 'document_basis'}, principal=ADMIN)
    assert not staged['errors'], staged
    version = repo.commit(staged['stage_id'], ADMIN.user_id, '仅隔离离线测试', principal=ADMIN)
    chunks = list(_chunks(version, CHUNKER))
    graph = build_domain_graph(chunks)
    by_product = {}
    for edge in flows(graph):
        support = edge['support']
        product, = support['evidence_scope']['products']
        by_product.setdefault(product, set()).add((graph.entities[edge['source_entity_id']]['name'],
                                                  graph.entities[edge['target_entity_id']]['name']))
        assert version['text'][support['offset']:support['end_offset']] == support['quote']
        assert support['quote_sha256'] == hashlib.sha256(support['quote'].encode()).hexdigest()
        assert support['document_sha256'] == raw_hash
        assert support['pages']
    assert ('过滤', '灌装') in by_product['银黄口服液']
    assert ('制粒', '干燥') in by_product['板蓝根颗粒']
    assert ('填充', '抛光') in by_product['六味地黄胶囊']
    # Never reconstruct the displaced silver-product page2 tail, merge branches,
    # or aliases collapsed to one node as a claimed complete route.
    assert ('灭菌', '灯检') not in by_product['银黄口服液']
    assert any(d['reason'] == 'unsupported_left_arrow_layout' for d in graph.diagnostics)
    record = ReleaseRepository(repository=repo).publish(principal=ADMIN,
        embedding_model_path='', require_embeddings=False)
    assert record['status'] == 'published'
    assert record['manifest']['domain_graph']['version'] == 3
    assert record['manifest']['embedding']['status'] == 'disabled'
    engine = ControlledSearchEngine(repository=repo, embedding_model_path='')
    for product, query in [('银黄口服液', '过滤 灌装'), ('板蓝根颗粒', '制粒 干燥'), ('六味地黄胶囊', '填充 抛光')]:
        results, stats = engine.search(query, principal=ADMIN, product=product, factory='中药一厂',
            as_of='2026-06-30', top_k=5, require_hybrid=False)
        assert results and stats['domain_graph_n'] > 0
        assert any(edge['relation_type'] == 'process_precedes_process'
                   for result in results for edge in result['domain_evidence'])
        loaded_rows, loaded_graph = engine._load_authorized(record, stats['authorized_version_ids'])
        result_ids = {result['chunk_id'] for result in results}
        assert any(e['support']['chunk_id'] in result_ids and
                   e['support']['evidence_scope']['products'] == [product]
                   for e in flows(loaded_graph))
        assert any(result['route_scores'].get('domain_graph', 0) > 0 for result in results)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == raw_hash
    print({'flow_edges': len(flows(graph)), 'routes': {p: sorted(v) for p, v in by_product.items()},
           'diagnostics': dict(Counter(d['reason'] for d in graph.diagnostics)), 'pdf_sha256': raw_hash})
