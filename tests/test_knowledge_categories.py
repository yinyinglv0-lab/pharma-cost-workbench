"""All routes share category-scoped authorized rows; fixtures stay in tmp_path."""
from dataclasses import dataclass, replace
import json
import sqlite3

import pytest

from enterprise.knowledge import Repository, KnowledgeError
from enterprise.knowledge_release import ReleaseRepository, ControlledSearchEngine, result_to_api
from enterprise.knowledge_langchain import retrieve_rows, ControlledKnowledgeRetriever


@dataclass(frozen=True)
class Principal:
    user_id: str = 'category-auditor'
    display_name: str = '类别测试'
    roles: tuple = ('knowledge_admin',)
    factories: tuple = ('*',)
    products: tuple = ('*',)
    tenant_id: str = 'default'


ADMIN = Principal()
READER = replace(ADMIN, roles=('analyst',), factories=('一厂',), products=('甲产品',))


def confirm(repo, title, category, *, text='甲产品 提取 金银花 蒸汽 工艺设备', factory='一厂', product='甲产品',
            filename=None, effective_from='2026-01-01', effective_to=None):
    staged = repo.stage(text.encode(), filename or title + '.txt', title, [product], effective_from,
        category, ADMIN.user_id, principal=ADMIN, scope_factories=[factory], visibility='scoped',
        effective_to=effective_to, metadata={'evidence_role': 'document_basis'})
    assert not staged['errors'], staged
    return repo.commit(staged['stage_id'], ADMIN.user_id, '测试核准', principal=ADMIN)


def release(repo):
    result = ReleaseRepository(repository=repo).publish(principal=ADMIN, embedding_model_path='')
    assert result['status'] == 'published', result
    return result


def search(engine, **kwargs):
    arguments = dict(principal=READER, product='甲产品', factory='一厂', as_of='2026-06-30')
    arguments.update(kwargs)
    return engine.search('甲产品 提取 金银花 蒸汽', **arguments)


def test_category_filter_before_all_candidate_routes_and_no_graph_trace_leak(tmp_path, monkeypatch):
    repo = Repository(tmp_path / 'managed')
    allowed = confirm(repo, '生产工艺', '生产工艺')
    excluded = confirm(repo, '配方', '产品配方')
    secret = confirm(repo, '外厂设备', '生产工艺', factory='二厂')
    confirm(repo, '其他产品', '生产工艺', product='乙产品')
    confirm(repo, '已失效', '生产工艺', effective_to='2026-02-01')
    confirm(repo, '未来', '生产工艺', effective_from='2027-01-01')
    release(repo)
    engine = ControlledSearchEngine(repository=repo, embedding_model_path='')
    observed = {}
    for name in ('_bm25', '_graph', '_domain_graph'):
        original = getattr(engine, name)
        def capture(query, rows, *args, _name=name, _original=original):
            observed[_name] = {row['version_id'] for row in rows}
            ids = {row['chunk_id'] for row in rows}
            if args:
                graph = args[0]
                assert all(chunks <= ids for chunks in graph.values())
                assert all(m['chunk_id'] in ids for m in graph.mentions)
                assert all(r['support']['chunk_id'] in ids for r in graph.relations)
            return _original(query, rows, *args)
        monkeypatch.setattr(engine, name, capture)
    rows, stats = search(engine, knowledge_types=['process'])
    assert {row['version_id'] for row in rows} == {allowed['version_id']}
    assert set(observed) == {'_bm25', '_graph', '_domain_graph'}
    assert all(ids == {allowed['version_id']} for ids in observed.values())
    assert excluded['version_id'] not in json.dumps(stats['domain_trace'])
    assert secret['version_id'] not in json.dumps(stats)
    assert rows[0]['meta']['knowledge_type'] == 'process'
    assert result_to_api(rows[0])['knowledge_type'] == 'process'
    # Vector route consumes the identical narrowed row list, including the case
    # where vectors exist. No embedding is loaded by this fixture.
    selected, graph = engine._load_authorized(engine.releases._raw_release(), {allowed['version_id'], excluded['version_id']})
    selected, graph = engine._restrict_candidates(selected, graph, ('process',), '2026-06-30')
    for row in selected: row['vector'] = [1.0, 0.0]
    assert {chunk for chunk, score in engine._vector([1.0, 0.0], selected)} == {row['chunk_id'] for row in rows}


def test_optional_filter_backward_compatible_and_langchain_passes_type(tmp_path):
    repo = Repository(tmp_path / 'managed')
    confirm(repo, '工艺', '生产工艺'); confirm(repo, '配方', '产品配方'); release(repo)
    engine = ControlledSearchEngine(repository=repo, embedding_model_path='')
    rows, _ = search(engine)
    assert {r['meta']['knowledge_type'] for r in rows} == {'formula', 'process'}
    rows, stats = retrieve_rows('甲产品 提取', principal=READER, repository=repo, engine=engine,
        product='甲产品', factory='一厂', as_of='2026-06-30', knowledge_types=['formula'])
    assert rows and all(r['meta']['knowledge_type'] == 'formula' for r in rows)
    assert stats['knowledge_types'] == ['formula']


@pytest.mark.parametrize('types', [[], '', 'process', ['UNKNOWN'], ['process', None], 3])
def test_unknown_filter_is_rejected_before_retrieval(tmp_path, types):
    engine = ControlledSearchEngine(root=tmp_path / 'empty', embedding_model_path='')
    with pytest.raises(KnowledgeError, match='knowledge_types'):
        search(engine, knowledge_types=types)
    with pytest.raises(KnowledgeError, match='knowledge_types'):
        ControlledKnowledgeRetriever(principal=READER, engine=engine, knowledge_types=types)


def test_revocation_during_candidate_generation_still_blocks_selected_type(tmp_path, monkeypatch):
    repo = Repository(tmp_path / 'managed')
    allowed = confirm(repo, '工艺', '生产工艺'); release(repo)
    engine = ControlledSearchEngine(repository=repo, embedding_model_path='')
    original = engine._bm25
    def revoke(query, rows):
        result = original(query, rows)
        engine.releases.revoke_document(allowed['doc_id'], principal=ADMIN, reason='fixture revoke')
        return result
    monkeypatch.setattr(engine, '_bm25', revoke)
    rows, stats = search(engine, knowledge_types=['process'])
    assert rows == []
    assert not stats['domain_trace']['matched_entities']
    assert search(engine, knowledge_types=['process'])[0] == []


def test_2026_csv_not_retrievable_in_2025_even_with_legacy_effective_date(tmp_path):
    repo = Repository(tmp_path / 'managed')
    text = '药材名称,规格等级,单位,1月价格,2月价格,3月价格,4月价格,5月价格,6月价格,价格来源,趋势分析\n金银花,统货,元/kg,1,1,1,1,1,1,市场,参考'
    confirm(repo, '市场', '市场参考', filename='行情_2026上半年.csv', text=text, effective_from='2025-01-01')
    record = release(repo)
    engine = ControlledSearchEngine(repository=repo, embedding_model_path='')
    assert search(engine, knowledge_types=['market_prices'], as_of='2025-06-30')[0] == []
    rows, _ = search(engine, knowledge_types=['market_prices'])
    assert len(rows) == 1 and rows[0]['meta']['table_row']['record_number'] == 2
    assert search(engine, knowledge_types=['formula'])[0] == []
    assert search(engine, knowledge_types=['market_prices'], as_of='2027-06-30')[0] == []
    with sqlite3.connect(repo.root / 'knowledge_releases' / record['release_id'] / 'index.sqlite') as conn:
        source = conn.execute('select text,metadata from chunks').fetchone()
    assert source[0] == text.split('\n')[1]
    assert json.loads(source[1])['business_metadata']['evidence_role'] == 'market_reference'
