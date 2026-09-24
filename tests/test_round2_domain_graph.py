"""Offline cross-industry graph vocabulary: temporary catalogs, no live model/API.

Vocab is matching metadata, not an authorization grant or a source fact. Dense
path tests use a deterministic in-memory double, never a deployed embedding.
"""
from copy import deepcopy
import hashlib
import json
import sqlite3

import pytest

import enterprise.domain_graph as dg
from enterprise.domain_vocabulary import (
    ENTITY_TYPES, VOCABULARY_SCHEMA, build_graph_vocabulary,
    validate_graph_vocabulary, vocabulary_fingerprint,
)
from enterprise.knowledge import Repository, KnowledgeError, KnowledgeAccessError, scope_permits
import enterprise.knowledge_release as kr
from enterprise.security import Principal

ADMIN = Principal('round2-admin', 'Offline reviewer', ('knowledge_admin',), ('*',), ('*',))
INDUSTRIES = [
    ('machinery', '减速箱', '合金钢', '铣削', '装配', '加工中心', '表面粗糙度'),
    ('auto_parts', '制动盘', '铸铁', '铸造', '精加工', '铸造机', '尺寸偏差'),
    ('chemicals', '水性涂料', '树脂', '分散', '调配', '分散机', '黏度'),
    ('electronics', '控制板', '铜箔', '贴片', '回流焊', '贴片机', '缺陷率'),
]


def vocabulary(industry=INDUSTRIES[0], *, alias=None):
    domain, product, material, process, following, equipment, metric = industry
    entities = {kind: {} for kind in ENTITY_TYPES}
    for kind, names in [('product', [product]), ('material', [material]),
                        ('process', [process, following]), ('equipment', [equipment]), ('metric', [metric])]:
        entities[kind] = {name: [name] for name in names}
    if alias:
        entities['process'][process].append(alias)
    return validate_graph_vocabulary({'schema_version': VOCABULARY_SCHEMA, 'domain_id': domain,
        'source_config_fingerprint': hashlib.sha256(domain.encode()).hexdigest(), 'entities': entities,
        'process_metric_bindings': [{'process': process, 'metric': metric}]})


def source_row(text, vocab=None, *, chunk='c', version='v', product='减速箱', offset=17, title='制造说明'):
    return {'chunk_id': chunk, 'version_id': version, 'text': text, 'meta': {
        'doc_id': 'doc-' + version, 'version_id': version, 'filename': 'source.txt',
        'sha256': 'a' * 64, 'offset': offset, 'end_offset': offset + len(text), 'title': title,
        'scope_products': [product], 'scope_factories': ['测试一厂'],
        'business_metadata': {'graph_vocabulary': vocab} if vocab is not None else {}}}


def semantics(graph):
    return [r for r in graph.relations if r['semantic_status'] == 'semantic_template_supported']


def stage(repo, text, *, vocab=None, product='减速箱', title='制造工艺', metadata=None):
    meta = metadata if metadata is not None else {'evidence_role': 'document_basis'}
    if vocab is not None:
        meta = {**meta, 'graph_vocabulary': vocab, 'graph_vocabulary_review': {'reason': '离线测试审核词表'}}
    return repo.stage(text.encode(), title + '.txt', title, [product], '2026-01-01', '生产工艺', ADMIN.user_id,
        scope_factories=['测试一厂'], visibility='scoped', metadata=meta, principal=ADMIN)


def confirm(repo, text, **kwargs):
    pending = stage(repo, text, **kwargs)
    assert not pending['errors'], pending['errors']
    return repo.commit(pending['stage_id'], ADMIN.user_id, '隔离测试确认', principal=ADMIN)


@pytest.mark.parametrize('industry', INDUSTRIES)
def test_four_industries_extract_concrete_predicates_not_pharma(industry):
    domain, product, material, process, following, equipment, metric = industry
    vocab = vocabulary(industry)
    text = (f'{product}生产工艺\n{product}包含{material}\n{material}经过{process}\n'
            f'{process}→{following}\n{process}使用{equipment}\n{process}\t{metric}≤5%\n'
            '附录：银黄口服液 金银花 提取收率 灌封一体机')
    row = source_row(text, vocab, product=product)
    graph = dg.build_domain_graph([row])
    assert {r['relation_type'] for r in semantics(graph)} == set(dg.SEMANTIC_TYPES)
    assert not {'银黄口服液', '金银花', '提取', '提取收率', '灌封一体机'} & {
        entity['name'] for entity in graph.entities.values()}
    for mention in graph.mentions:
        assert mention['quote'] == text[mention['start'] - 17:mention['end'] - 17]
        assert mention['source']['graph_vocabulary_fingerprint'] == vocabulary_fingerprint(vocab)
    for relation in semantics(graph):
        support = relation['support']
        assert support['quote'] == text[support['offset'] - 17:support['end_offset'] - 17]
        assert support['quote_sha256'] == hashlib.sha256(support['quote'].encode()).hexdigest()
        assert support['source_config_fingerprint'] == vocab['source_config_fingerprint']
        assert support['document_id'] == 'doc-v' and support['version_id'] == 'v'
        assert support['evidence_scope']['products'] == [product]


@pytest.mark.parametrize('text', [
    '减速箱 合金钢 铣削 装配 加工中心 表面粗糙度',
    '铣削 表面粗糙度', '表面粗糙度≤5%', '铣削≤5% 表面粗糙度',
    '装配 表面粗糙度≤5%', '铣削与装配的表面粗糙度≤5%',
    '铣削；表面粗糙度≤5%', '铣削\n\n表面粗糙度≤5%',
    '铣削不得使用加工中心', '铣削不使用加工中心', '减速箱不含合金钢',
    '合金钢和铣削', '铣削→未知工序→装配',
    '铣削 表面粗糙度是回顾性描述。另附参考值≤5%',
])
def test_config_and_cooccurrence_alone_never_create_semantic_edges(text):
    graph = dg.build_domain_graph([source_row(text, vocabulary())])
    assert not semantics(graph)
    assert all(r['semantic_status'] == 'co_occurrence_only' for r in graph.relations)


def test_metric_name_prefix_is_not_binding_or_process_occurrence():
    vocab = vocabulary()
    vocab['entities']['metric'] = {'铣削合格率': ['铣削合格率']}
    vocab['process_metric_bindings'] = []
    assert not semantics(dg.build_domain_graph([source_row('铣削 铣削合格率≥98%', vocab)]))
    vocab['process_metric_bindings'] = [{'process': '铣削', 'metric': '铣削合格率'}]
    assert not semantics(dg.build_domain_graph([source_row('铣削合格率≥98%', vocab)]))
    assert any(r['relation_type'] == 'process_has_controlled_metric' for r in
               semantics(dg.build_domain_graph([source_row('铣削 铣削合格率≥98%', vocab)])))


@pytest.mark.parametrize('label', ['BOM', '物料清单', '材料清单'])
def test_explicit_bom_labels_quantities_with_separate_source_title(label):
    vocab = vocabulary()
    title = source_row('减速箱', vocab, chunk='title', title=label)
    table = source_row(f'{label}\n材料名称\t用量\n合金钢\t2.5 kg', vocab, chunk='body', title=label)
    edges = [r for r in semantics(dg.build_domain_graph([title, table]))
             if r['relation_type'] == 'product_contains_material']
    assert edges and edges[0]['support']['context_sources'][0]['quote'] == '减速箱'
    assert edges[0]['support']['context_sources'][0]['chunk_id'] == 'title'
    assert edges[0]['support']['context_sources'][1]['quote'] == label
    assert edges[0]['support']['evidence_scope']['products'] == ['减速箱']
    assert not semantics(dg.build_domain_graph([source_row('合金钢 2.5 kg', vocab)]))
    # A metadata title plus an unrelated product mention is not a product title
    # in the original source, and a negative/listed absence is not a BOM row.
    unrelated = source_row('减速箱已经停售', vocab, chunk='title', title=label)
    assert not semantics(dg.build_domain_graph([unrelated, table]))
    negative = source_row(f'{label}\n不含合金钢 2.5 kg', vocab, chunk='body', title=label)
    assert not semantics(dg.build_domain_graph([title, negative]))


def test_vocabulary_does_not_supply_document_scope_or_product_mentions():
    row = source_row('铣削→装配', vocabulary())
    row['meta']['scope_products'] = ['无关产品甲', '无关产品乙']
    graph = dg.build_domain_graph([row])
    assert not semantics(graph)
    assert not any(e['entity_type'] == 'product' for e in graph.entities.values())
    assert graph.diagnostics[0]['reason'] == 'ambiguous_product_section'
    missing = source_row('银黄口服液 金银花 提取', None)
    missing['meta']['business_metadata']['domain_id'] = 'machinery'
    assert not dg.build_domain_graph([missing]).entities


@pytest.mark.parametrize('mutate', [
    lambda v: v.update(roles=['knowledge_admin']),
    lambda v: v.update(code='print(1)'),
    lambda v: v['entities'].update(scope_products={'*': ['*']}),
    lambda v: v['entities']['process'].update({'*': ['*']}),
    lambda v: v['entities']['process'].update({'x': ['__import__("os")']}),
    lambda v: v['entities']['process'].update({'x': ['a' * 161]}),
    lambda v: v['entities']['process'].update({'x': ['a\nb']}),
    lambda v: v['entities']['process'].update({'x': ['铣削']}),
    lambda v: v['process_metric_bindings'].append({'process': '未知', 'metric': '表面粗糙度'}),
    lambda v: v['process_metric_bindings'].append({'process': '铣削', 'metric': '表面粗糙度', 'grant': True}),
    lambda v: v.update(source_config_fingerprint='not-a-sha'),
])
def test_closed_bounded_vocabulary_rejects_ids_code_grants_and_ambiguous_aliases(mutate):
    value = vocabulary()
    mutate(value)
    with pytest.raises(ValueError):
        validate_graph_vocabulary(value)


def test_review_is_authenticated_not_self_declared_and_scope_stays_separate(tmp_path):
    repo = Repository(tmp_path)
    meta = {'graph_vocabulary': vocabulary(), 'graph_vocabulary_review': {'reason': 'review', 'reviewed_by': 'forged'}}
    result = stage(repo, '铣削', metadata=meta)
    assert result['errors'] and not result['stage_id']
    meta['graph_vocabulary_review'] = {'reason': 'review', 'reviewed_at': '1900-01-01'}
    result = stage(repo, '铣削', metadata=meta)
    assert not result['errors']
    review = result['business_metadata']['graph_vocabulary_review']
    assert review['reviewed_by'] == ADMIN.user_id and review['reviewed_at'] != '1900-01-01'
    value = repo.commit(result['stage_id'], ADMIN.user_id, 'confirm', principal=ADMIN)
    assert value['scope_products'] == ['减速箱'] and value['business_metadata']['graph_vocabulary_review'] == review
    repeat = stage(repo, '铣削', metadata=meta)
    assert not repeat['errors'] and repeat['change']['requires_confirmation'] is False
    with pytest.raises(KnowledgeAccessError):
        Repository(tmp_path / 'anonymous').stage(b'text', 'text.txt', 'no authenticated reviewer', ['减速箱'],
            '2026-01-01', 'process', 'self-claimed-admin', scope_factories=['测试一厂'], metadata=meta)
    limited = Principal('limited', 'Limited', ('knowledge_admin',), ('测试一厂',), ('其他产品',))
    with pytest.raises(KnowledgeAccessError):
        repo.stage(b'text', 'text.txt', 'cannot grant', ['减速箱'], '2026-01-01', 'process', limited.user_id,
            scope_factories=['测试一厂'], metadata=meta, principal=limited)


def canonical_profile():
    return {'schema_version': 'manufacturing-domain/2', 'id': 'machinery', 'version': '2',
        'industry': 'machinery', 'label': '机械', 'status': 'active', 'data_classification': 'operator_supplied',
        'source_dataset': 'offline-example', 'reporting_unit': 'piece', 'currency': 'CNY',
        'factories': {'home': '测试一厂', 'peer': '测试二厂'},
        'labels': {k: k for k in ('home', 'peer', 'material', 'labor', 'overhead', 'unitcost', 'output', 'total')},
        'products': [{'id': 'p', 'name': '减速箱', 'specification': 'A型', 'category': '机械',
            'reporting_unit': 'piece', 'materials': ['合金钢'],
            'bom': [{'material_id': 'm', 'quantity_per_reporting_unit': '2.5', 'unit': 'kg'}],
            'process_ids': ['pr'], 'equipment_ids': ['eq']}],
        'materials': [{'id': 'm', 'name': '合金钢', 'unit': 'kg'}],
        'processes': [{'id': 'pr', 'name': '铣削', 'product_ids': ['p'], 'metric_ids': ['mt']}],
        'equipment': [{'id': 'eq', 'name': '加工中心', 'process_ids': ['pr']}],
        'industry_category': '机械', 'knowledge_types': ['process'], 'limitations': ['参考不是实际耗用'],
        'reference_metrics': [{'id': 'mt', 'source_name': '表面粗糙度', 'unit': 'μm',
            'direction': 'context_only', 'calculation': 'source_only', 'element': 'none', 'process_ids': ['pr']}]}


def test_converter_validates_v2_and_projects_no_reference_bom_edges():
    profile = canonical_profile()
    value = build_graph_vocabulary(profile)
    assert value['entities']['material'] == {'合金钢': ['合金钢']}
    assert value['process_metric_bindings'] == [{'process': '铣削', 'metric': '表面粗糙度'}]
    assert set(value) == {'schema_version', 'domain_id', 'source_config_fingerprint', 'entities', 'process_metric_bindings'}
    assert not semantics(dg.build_domain_graph([source_row('减速箱 合金钢 铣削 表面粗糙度', value)]))
    profile['products'][0]['bom'][0]['quantity_per_reporting_unit'] = '9'
    other = build_graph_vocabulary(profile)
    assert value['entities'] == other['entities']
    assert value['source_config_fingerprint'] != other['source_config_fingerprint']
    profile['roles'] = ['knowledge_admin']
    with pytest.raises(ValueError):
        build_graph_vocabulary(profile)


@pytest.mark.parametrize('mutation', ['fingerprint', 'binding', 'entity', 'profile', 'config_hash', 'profile_hash', 'missing_graph', 'reviewer'])
def test_catalog_rejects_frozen_profile_vocabulary_binding_mismatch(tmp_path, mutation):
    profile = canonical_profile()
    vocab = build_graph_vocabulary(profile)
    metadata = {'manufacturing_domain_profile': profile, 'graph_vocabulary': vocab,
                'manufacturing_config_hash': vocab['source_config_fingerprint'],
                'graph_vocabulary_review': {'reason': 'operator reviewed profile'}}
    if mutation == 'fingerprint':
        vocab['source_config_fingerprint'] = 'b' * 64
    elif mutation == 'binding':
        vocab['process_metric_bindings'] = []
    elif mutation == 'entity':
        vocab['entities']['material']['凭空材料'] = ['凭空材料']
    elif mutation == 'profile':
        profile['products'][0]['bom'][0]['quantity_per_reporting_unit'] = '17'
    elif mutation == 'config_hash':
        metadata['manufacturing_config_hash'] = 'bad-hash'
    elif mutation == 'profile_hash':
        metadata['manufacturing_profile_sha256'] = 'c' * 64
    elif mutation == 'missing_graph':
        del metadata['graph_vocabulary']
    elif mutation == 'reviewer':
        metadata['graph_vocabulary_review']['reviewed_by'] = 'untrusted-upload'
    pending = stage(Repository(tmp_path), '减速箱生产工艺\n铣削 表面粗糙度≤5%', metadata=metadata)
    assert pending['errors'] and not pending['stage_id']


def test_catalog_freezes_normalized_profile_and_graph_without_expanding_scopes(tmp_path):
    profile = canonical_profile()
    profile['products'][0]['bom'][0]['quantity_per_reporting_unit'] = '2.500'
    vocab = build_graph_vocabulary(profile)
    metadata = {'manufacturing_domain_profile': profile, 'graph_vocabulary': vocab,
                'manufacturing_config_hash': 'c' * 64,
                'graph_vocabulary_review': {'reason': 'operator reviewed profile'}}
    repo = Repository(tmp_path)
    pending = stage(repo, '减速箱生产工艺\n铣削 表面粗糙度≤5%', metadata=metadata)
    assert not pending['errors']
    version = repo.commit(pending['stage_id'], ADMIN.user_id, 'confirm', principal=ADMIN)
    assert version['scope_factories'] == ['测试一厂']  # profile also names peer; no grant
    assert version['scope_products'] == ['减速箱']
    assert version['business_metadata']['graph_vocabulary_review']['reviewed_by'] == ADMIN.user_id
    assert build_graph_vocabulary(version['business_metadata']['manufacturing_domain_profile']) == vocab
    profile['products'][0]['bom'][0]['quantity_per_reporting_unit'] = '999'
    assert build_graph_vocabulary(version['business_metadata']['manufacturing_domain_profile']) == vocab


def test_full_profile_metadata_requires_whole_profile_read_scope(tmp_path):
    profile = canonical_profile()
    other = deepcopy(profile['products'][0])
    other.update(id='private', name='保密齿轮箱')
    profile['products'].append(other)
    profile['processes'][0]['product_ids'].append('private')
    vocab = build_graph_vocabulary(profile)
    metadata = {'manufacturing_domain_profile': profile, 'graph_vocabulary': vocab,
                'graph_vocabulary_review': {'reason': 'whole-profile metadata review'}}
    repo = Repository(tmp_path)
    version = confirm(repo, '减速箱生产工艺\n铣削使用加工中心', metadata=metadata)
    release = kr.ReleaseRepository(repository=repo).publish(principal=ADMIN,
        embedding_model_path='', require_embeddings=False)
    assert release['status'] == 'published'
    single = Principal('single', 'Single product', ('analyst',), ('测试一厂', '测试二厂'), ('减速箱',))
    one_factory = Principal('one-factory', 'One factory', ('analyst',), ('测试一厂',), ('减速箱', '保密齿轮箱'))
    broad = Principal('broad', 'Whole profile', ('analyst',), ('测试一厂', '测试二厂'), ('减速箱', '保密齿轮箱'))
    engine = kr.ControlledSearchEngine(repository=repo, embedding_model_path='')
    for reader in (single, one_factory):
        with pytest.raises(KnowledgeAccessError):
            repo.get(version['doc_id'], principal=reader)
        rows, stats = engine.search('铣削', principal=reader, product='减速箱', factory='测试一厂', as_of='2026-06-01')
        assert not rows and stats['authorized_version_ids'] == []
        assert '保密齿轮箱' not in json.dumps(stats, ensure_ascii=False)
    value = repo.get(version['doc_id'], principal=broad)
    assert value['business_metadata']['manufacturing_domain_profile']['products'][1]['name'] == '保密齿轮箱'
    rows, _ = engine.search('铣削', principal=broad, product='减速箱', factory='测试一厂', as_of='2026-06-01')
    assert rows
    narrow_scope = {'products': {'减速箱'}, 'factories': {'测试一厂', '测试二厂'}}
    public = {**version, 'visibility': 'public', 'scope_products': [], 'scope_factories': []}
    assert not scope_permits(public, narrow_scope)  # public flag cannot reveal aggregate
    malformed = deepcopy(version)
    malformed['business_metadata']['manufacturing_domain_profile']['roles'] = ['admin']
    assert not scope_permits(malformed, {'products': {'*'}, 'factories': {'*'}})
    legacy = {**version, 'business_metadata': {}}
    assert scope_permits(legacy, narrow_scope)


def test_published_vocabulary_frozen_manifest_index_query_and_dense_fail_closed(tmp_path, monkeypatch):
    repo = Repository(tmp_path)
    vocab = vocabulary(alias='精密铣')
    version = confirm(repo, '减速箱生产工艺\n铣削使用加工中心\n铣削 表面粗糙度≤5%', vocab=vocab)
    releases = kr.ReleaseRepository(repository=repo)
    release = releases.publish(principal=ADMIN, embedding_model_path='', require_embeddings=False)
    assert release['status'] == 'published'
    spec = release['manifest']['domain_graph']
    assert spec['version'] == 4 and spec['vocabulary']['schema_version'] == VOCABULARY_SCHEMA
    frozen = spec['vocabulary']
    assert frozen['entries'][vocabulary_fingerprint(vocab)] == vocab
    assert frozen['by_version'][version['version_id']]['source_config_fingerprint'] == vocab['source_config_fingerprint']
    artifact = releases.artifact_dir / release['release_id'] / 'index.sqlite'
    before = hashlib.sha256(artifact.read_bytes()).hexdigest()
    with sqlite3.connect(artifact) as connection:
        assert json.loads(connection.execute('SELECT configuration FROM graph_configuration').fetchone()[0]) == frozen
    monkeypatch.setattr(dg, 'ENTITY_ALIASES', {})
    import enterprise.domain_profiles as profiles
    monkeypatch.setattr(profiles, 'load_domain_profile', lambda *a, **kw: pytest.fail('query consulted current profile'))
    monkeypatch.setattr(kr, 'expand_query', lambda *a: pytest.fail('V4 consulted pharmaceutical global synonyms'))
    vocab['entities']['process']['铣削'] = ['runtime-mutated-alias']
    engine = kr.ControlledSearchEngine(repository=repo, embedding_model_path='')
    args = dict(principal=ADMIN, product='减速箱', factory='测试一厂', as_of='2026-06-01')
    rows, stats = engine.search('精密铣', **args)
    assert rows and stats['domain_graph_n'] > 0
    assert stats['graph_vocabulary_version'] == VOCABULARY_SCHEMA
    assert stats['graph_vocabulary_fingerprint'] == frozen['fingerprint']
    assert stats['source_config_fingerprints'] == [version['business_metadata']['graph_vocabulary']['source_config_fingerprint']]
    assert stats['keyword_expansions'] == []
    empty, strict = engine.search('精密铣', **args, require_hybrid=True)
    assert not empty and strict['reason'] == 'hybrid_unavailable' and strict['degraded']
    assert strict['graph_vocabulary_fingerprint'] == frozen['fingerprint']
    assert hashlib.sha256(artifact.read_bytes()).hexdigest() == before
    # Confirmation alone must not switch/rebuild an existing publication.
    confirm(repo, '另一份证据', vocab=vocabulary(), title='另一文档')
    assert releases.get_release(principal=ADMIN)['release_id'] == release['release_id']
    assert hashlib.sha256(artifact.read_bytes()).hexdigest() == before


def test_authorized_aliases_do_not_leak_from_same_named_private_entity(tmp_path):
    repo = Repository(tmp_path)
    public_vocab = vocabulary(alias='可见别名')
    private_vocab = vocabulary(alias='秘密别名')
    private_vocab['source_config_fingerprint'] = 'b' * 64
    first = confirm(repo, '减速箱生产工艺\n铣削使用加工中心', vocab=public_vocab)
    confirm(repo, '私有产品生产工艺\n铣削使用加工中心', vocab=private_vocab,
            product='私有产品', title='私有工艺')
    release = kr.ReleaseRepository(repository=repo).publish(principal=ADMIN, embedding_model_path='', require_embeddings=False)
    limited = Principal('reader', 'Scoped reader', ('analyst',), ('测试一厂',), ('减速箱',))
    engine = kr.ControlledSearchEngine(repository=repo, embedding_model_path='')
    rows, stats = engine.search('秘密别名', principal=limited, product='减速箱', factory='测试一厂', as_of='2026-06-01')
    assert not rows and stats['authorized_version_ids'] == [first['version_id']]
    loaded_rows, graph = engine._load_authorized(release, [first['version_id']])
    assert graph.query_entities('可见别名') and not graph.query_entities('秘密别名')
    assert '秘密别名' not in json.dumps(stats, ensure_ascii=False)
    all_rows, all_graph = engine._load_authorized(release, release['manifest']['input_version_ids'])
    _, narrowed = engine._restrict_candidates(all_rows, all_graph, None, '2026-06-01', '减速箱', '测试一厂')
    assert not narrowed.query_entities('秘密别名')


def test_mixed_v4_release_freezes_legacy_aliases_without_global_rebuild(tmp_path, monkeypatch):
    repo = Repository(tmp_path)
    confirm(repo, '减速箱生产工艺\n铣削使用加工中心', vocab=vocabulary())
    legacy = confirm(repo, '银黄口服液生产工艺\n混合使用三维混合机', product='银黄口服液', title='旧行业')
    releases = kr.ReleaseRepository(repository=repo)
    release = releases.publish(principal=ADMIN, embedding_model_path='', require_embeddings=False)
    assert release['status'] == 'published'
    frozen = release['manifest']['domain_graph']['vocabulary']
    assert frozen['by_version'][legacy['version_id']]['mode'] == 'legacy_pharma_vocabulary'
    assert frozen['legacy_pharma_aliases']['process']['混合'] == ['混合', '总混']
    artifact = releases.artifact_dir / release['release_id'] / 'index.sqlite'
    monkeypatch.setattr(dg, 'ENTITY_ALIASES', {})
    monkeypatch.setattr(kr, 'ENTITY_ALIASES', {})
    with sqlite3.connect(artifact) as connection:
        graph = dg.load_domain_graph(connection, [legacy['version_id']])
    assert graph.query_entities('总混')
    releases._validate_artifact(artifact, release['manifest'])


def test_dense_v4_all_routes_share_authorized_set_and_fail_closed(tmp_path, monkeypatch):
    repo = Repository(tmp_path / 'catalog')
    model_path = tmp_path / 'fake-local-encoder'
    model_path.mkdir()
    (model_path / 'config.json').write_text('{}', encoding='utf-8')
    (model_path / 'weights.bin').write_bytes(b'offline-double')
    class FakeEncoder:
        def encode(self, texts, batch_size):
            return {'dense_vecs': [[1.0, 0.25] for _ in texts]}
    monkeypatch.setattr(kr, '_load_local_bge', lambda path: FakeEncoder())
    visible = confirm(repo, '减速箱生产工艺\n铣削使用加工中心', vocab=vocabulary())
    confirm(repo, '私有产品生产工艺\n铣削使用加工中心', vocab=vocabulary(), product='私有产品', title='私有工艺')
    releases = kr.ReleaseRepository(repository=repo)
    release = releases.publish(principal=ADMIN, embedding_model_path=model_path, require_embeddings=True)
    assert release['status'] == 'published' and not release['manifest']['degraded']
    limited = Principal('reader', 'Scoped reader', ('analyst',), ('测试一厂',), ('减速箱',))
    engine = kr.ControlledSearchEngine(repository=repo, embedding_model_path=model_path)
    observed = []
    original = engine._vector
    def vector(query, rows):
        observed.append({row['version_id'] for row in rows})
        return original(query, rows)
    monkeypatch.setattr(engine, '_vector', vector)
    args = dict(principal=limited, product='减速箱', factory='测试一厂', as_of='2026-06-01',
                require_hybrid=True, require_ready=True)
    rows, stats = engine.search('铣削 加工中心', **args)
    assert rows and stats['degraded'] is False and not stats['degradation_reasons']
    assert stats['retrieval_mode'] == 'vector_bm25_graph'
    assert stats['graph_vocabulary_version'] == VOCABULARY_SCHEMA
    assert set(rows[0]['route_scores']) == {'bm25', 'vector', 'graph', 'domain_graph'}
    assert observed == [{visible['version_id']}]
    assert set(stats['route_generations'].values()) == {release['release_id']}
    def broken(*args, **kwargs):
        raise RuntimeError('offline simulated dense failure')
    monkeypatch.setattr(engine._handle, 'encode', broken)
    rows, failed = engine.search('铣削', **args)
    assert not rows and failed['reason'] == 'hybrid_unavailable' and failed['degraded']
    assert 'query_embedding_failed:RuntimeError' in failed['degradation_reasons']
    assert failed['graph_vocabulary_fingerprint'] == stats['graph_vocabulary_fingerprint']


@pytest.mark.parametrize('failure', ['none', 'runtime', 'timeout', 'dimension', 'zero', 'cold', 'fingerprint'])
def test_empty_authorized_type_set_is_healthy_only_after_valid_dense_query(tmp_path, monkeypatch, failure):
    repo = Repository(tmp_path / 'catalog')
    model_path = tmp_path / 'empty-query-encoder'
    model_path.mkdir()
    (model_path / 'config.json').write_text('{}', encoding='utf-8')
    (model_path / 'weights.bin').write_bytes(b'offline-double')
    calls = []
    class FakeEncoder:
        def encode(self, texts, batch_size):
            calls.extend(texts)
            return {'dense_vecs': [[1.0, 0.25] for _ in texts]}
    monkeypatch.setattr(kr, '_load_local_bge', lambda path: FakeEncoder())
    visible = confirm(repo, '减速箱制造参考\n铣削使用加工中心', vocab=vocabulary())
    release = kr.ReleaseRepository(repository=repo).publish(principal=ADMIN,
        embedding_model_path=model_path, require_embeddings=True)
    assert release['status'] == 'published'
    engine = kr.ControlledSearchEngine(repository=repo, embedding_model_path=model_path)
    assert engine.warmup(principal=ADMIN, wait=True)['state'] == 'ready'
    calls.clear()
    if failure == 'runtime':
        def broken(*args, **kwargs):
            raise RuntimeError('query failure despite warm encoder')
        monkeypatch.setattr(engine._handle, 'encode', broken)
    elif failure == 'timeout':
        def timeout(*args, **kwargs):
            raise kr.FutureTimeout()
        monkeypatch.setattr(engine._handle, 'encode', timeout)
    elif failure in {'dimension', 'zero'}:
        monkeypatch.setattr(engine._handle, 'encode', lambda *a, **kw: [[1.0]] if failure == 'dimension' else [[0.0, 0.0]])
    elif failure in {'cold', 'fingerprint'}:
        monkeypatch.setattr(engine, 'warmup', lambda **kw: {'state': 'loading' if failure == 'cold' else 'ready',
                                                         'fingerprint': 'not-the-release'})
    rows, stats = engine.search('absent formula query', principal=ADMIN, product='减速箱', factory='测试一厂',
        as_of='2026-06-01', require_ready=True, require_hybrid=True, knowledge_types=['formula'])
    assert not rows and stats['no_answer']
    assert stats['generation'] == release['release_id']
    assert stats['graph_vocabulary_fingerprint'] == release['manifest']['domain_graph']['vocabulary']['fingerprint']
    if failure == 'none':
        assert calls == ['absent formula query']  # empty is not a skip-dense shortcut
        assert stats['degraded'] is False and stats['degradation_reasons'] == []
        assert stats['reason'] == 'no_matching_knowledge_type_or_source_period'
        assert stats['retrieval_mode'] == 'vector_bm25_graph'
        assert stats['readiness']['ready'] and stats['readiness']['state'] == 'ready'
        assert stats['readiness']['fingerprint'] == release['manifest']['embedding']['fingerprint']
        assert stats['authorized_version_ids'] == [visible['version_id']]
        assert stats['candidate_filter']['permitted_chunks'] == 0
        assert stats['vector_n'] == stats['bm25_n'] == stats['graph_n'] == stats['domain_graph_n'] == 0
    else:
        assert stats['degraded'] and stats['degradation_reasons']
        assert stats['reason'] == 'hybrid_unavailable' and stats['retrieval_mode'] == 'unavailable'


def test_empty_type_set_on_lexical_release_remains_explicitly_degraded(tmp_path):
    repo = Repository(tmp_path)
    confirm(repo, '减速箱生产工艺\n铣削使用加工中心', vocab=vocabulary())
    kr.ReleaseRepository(repository=repo).publish(principal=ADMIN, embedding_model_path='', require_embeddings=False)
    rows, stats = kr.ControlledSearchEngine(repository=repo, embedding_model_path='').search(
        'missing formula', principal=ADMIN, product='减速箱', factory='测试一厂', as_of='2026-06-01',
        knowledge_types=['formula'], require_hybrid=False)
    assert not rows and stats['degraded'] and stats['reason'] == 'no_matching_knowledge_type_or_source_period'
    assert stats['retrieval_mode'] == 'bm25_graph_fallback' and 'release_has_no_vectors' in stats['degradation_reasons']


@pytest.mark.parametrize('version', [2, 3])
def test_historical_stated_graph_versions_load_without_rebuild_or_hash_change(tmp_path, monkeypatch, version):
    repo = Repository(tmp_path)
    confirm(repo, '银黄口服液生产工艺\n提取使用多功能提取罐', product='银黄口服液')
    spec = dg.DOMAIN_GRAPH_V2 if version == 2 else dg.DOMAIN_GRAPH_V3
    monkeypatch.setattr(kr, 'DOMAIN_GRAPH', spec)
    release = kr.ReleaseRepository(repository=repo).publish(principal=ADMIN, embedding_model_path='', require_embeddings=False)
    assert release['manifest']['domain_graph']['version'] == version
    assert 'vocabulary' not in release['manifest']['domain_graph']
    artifact = kr.ReleaseRepository(repository=repo).artifact_dir / release['release_id'] / 'index.sqlite'
    before = hashlib.sha256(artifact.read_bytes()).hexdigest()
    monkeypatch.setattr(kr, 'build_domain_graph', lambda *a: pytest.fail('historical graph rebuilt'))
    engine = kr.ControlledSearchEngine(repository=repo, embedding_model_path='')
    rows, stats = engine.search('提取', principal=ADMIN, product='银黄口服液', factory='测试一厂', as_of='2026-06-01')
    assert rows and stats['domain_graph_version'] == version
    assert stats['graph_vocabulary_version'] is None
    assert hashlib.sha256(artifact.read_bytes()).hexdigest() == before
