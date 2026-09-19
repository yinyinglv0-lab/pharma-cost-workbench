"""Isolated release, authorization, bitemporal and offline retrieval acceptance.

Every artifact and original is under tmp_path; no real managed/kb directory or
model is written or loaded. The dense-path tests inject a small deterministic
encoder so a test run can never initiate a model download.
"""
from dataclasses import dataclass, replace
import hashlib
import json
import os
import time
from pathlib import Path
import sqlite3
import threading

import pytest

from enterprise.knowledge import Repository, KnowledgeAccessError, KnowledgeError
from enterprise.knowledge_context import context
from enterprise.knowledge_release import (
    ReleaseRepository, ControlledSearchEngine, get_search_engine,
    bootstrap_official, result_to_api,
)


@dataclass(frozen=True)
class Principal:
    user_id: str = 'approver'
    display_name: str = '审核人'
    roles: tuple = ('knowledge_admin',)
    factories: tuple = ('*',)
    products: tuple = ('*',)
    tenant_id: str = 'default'


ADMIN = Principal()
READER = Principal('a', '一厂分析员', ('analyst',), ('一厂',), ('甲产品',))


@pytest.fixture
def repo(tmp_path):
    return Repository(tmp_path / 'isolated-managed')


def confirmed(repo, title='工艺', text='甲产品 提取收率 工艺记录', **kwargs):
    args = dict(filename=title + '.txt', title=title, scope_products=['甲产品'],
                scope_factories=['一厂'], visibility='scoped', effective_from='2026-01-01',
                category='工艺', actor=ADMIN.user_id, principal=ADMIN)
    args.update(kwargs)
    pending = repo.stage(text.encode('utf-8'), **args)
    assert not pending['errors'], pending
    return repo.commit(pending['stage_id'], 'cannot-spoof-this', '核对原件', principal=ADMIN)


def publish(repo, **kwargs):
    options = {'principal': ADMIN, 'embedding_model_path': ''}
    options.update(kwargs)
    result = ReleaseRepository(repository=repo).publish(**options)
    assert result['status'] == 'published', result
    return result


def search(repo, query='提取收率', **kwargs):
    options = {'principal': READER, 'product': '甲产品', 'factory': '一厂', 'as_of': '2026-06-30'}
    options.update(kwargs)
    return get_search_engine(repository=repo, embedding_model_path='').search(query, **options)


def test_read_only_empty_no_import_or_publication(repo):
    engine = get_search_engine(repository=repo, embedding_model_path='')
    assert engine is get_search_engine(repository=repo, embedding_model_path='')
    result, stats = search(repo)
    assert result == [] and stats['reason'] == 'no_published_release'
    assert ReleaseRepository(repository=repo).history(principal=ADMIN) == []
    assert not repo.root.exists()


def test_publication_manifest_persistent_chunks_and_offline_result(repo):
    version = confirmed(repo)
    assert search(repo)[0] == []
    record = publish(repo)
    manifest = record['manifest']
    assert manifest['input_version_ids'] == [version['version_id']]
    assert manifest['published_at'] and manifest['status'] == 'published'
    assert all(len(manifest[key]['fingerprint']) == 64 for key in ('parse', 'chunker', 'embedding', 'bm25', 'graph'))
    assert manifest['embedding']['status'] == 'disabled'
    assert manifest['artifacts']['index.sqlite']['generation'] == record['release_id']
    assert len(manifest['chunk_ids']) == manifest['chunk_count'] == 1
    assert ReleaseRepository(repository=repo).get_release(principal=ADMIN) == record
    rows, stats = search(repo)
    assert rows and stats['degraded'] and stats['retrieval_mode'] == 'bm25_graph_fallback'
    assert stats['bm25_n'] and stats['graph_n'] and stats['vector_n'] == 0
    assert set(stats['route_generations'].values()) == {record['release_id']}
    assert rows[0]['version_id'] == version['version_id']
    assert result_to_api(rows[0])['scope_unknown'] is False
    refs, context_stats = context('甲产品', '2026-06', '提取收率', repository=repo,
                                  principal=READER, factory='一厂', return_stats=True)
    assert refs[0]['chunk_id'] == rows[0]['chunk_id']
    assert refs[0]['index_release_id'] == context_stats['release_id'] == record['release_id']
    # Terminal history is immutable even through ordinary direct SQL writes.
    with sqlite3.connect(ReleaseRepository(repository=repo).db_path) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE releases SET status='failed'")


def test_context_and_api_preserve_evidence_limitations(repo):
    metadata = {'evidence_role': 'context_only', 'authority': 'summary',
                'known_conflicts': ['配方数字与原件不一致'], 'limitations': ['不得用作权威法规条款']}
    confirmed(repo, metadata=metadata)
    publish(repo)
    rows, _ = search(repo)
    api = result_to_api(rows[0])
    refs = context('甲产品', '2026-06', '提取收率', repository=repo, principal=READER, factory='一厂')
    for result in (api, refs[0]):
        assert result['business_metadata'] == metadata
        assert result['evidence_role'] == 'context_only' and result['authority'] == 'summary'
        assert result['known_conflicts'] == metadata['known_conflicts']
        assert result['limitations'] == metadata['limitations']


def test_metadata_only_revision_changes_chunk_identity_and_keeps_history(repo):
    first = confirmed(repo)
    one = publish(repo)
    before = search(repo)[0][0]['chunk_id']
    second = confirmed(repo, doc_id=first['doc_id'], metadata={'owner': '质量部'})
    two = publish(repo)
    assert two['manifest']['input_version_ids'] == sorted([first['version_id'], second['version_id']])
    current, _ = search(repo)
    assert current[0]['chunk_id'] != before
    assert current[0]['version_id'] == second['version_id']
    original, old_stats = search(repo, known_at=one['published_at'])
    assert original[0]['chunk_id'] == before and old_stats['release_id'] == one['release_id']
    assert len(list(repo.blob_dir.iterdir())) == 1
    assert len(ReleaseRepository(repository=repo).history(principal=ADMIN)) == 2


def test_failed_build_retains_previous_pointer_and_auditable_record(repo, monkeypatch):
    confirmed(repo)
    good = publish(repo)
    releases = ReleaseRepository(repository=repo)
    def broken(*args):
        raise OSError('simulated disk full')
    monkeypatch.setattr(releases, '_build_artifact', broken)
    bad = releases.publish(principal=ADMIN, embedding_model_path='')
    assert bad['status'] == 'failed' and 'simulated disk full' in bad['error']
    assert bad['published_at'] is None
    assert releases.get_release(principal=ADMIN)['release_id'] == good['release_id']
    assert {r['status'] for r in releases.history(principal=ADMIN)} == {'published', 'failed'}
    assert search(repo)[1]['release_id'] == good['release_id']
    with pytest.raises(KnowledgeError, match='published'):
        search(repo, release_id=bad['release_id'])


def test_corrupt_original_fails_publication_without_switch(repo):
    version = confirmed(repo)
    good = publish(repo)
    (repo.blob_dir / version['sha256']).write_bytes(b'corrupted')
    bad = ReleaseRepository(repository=repo).publish(principal=ADMIN, embedding_model_path='')
    assert bad['status'] == 'failed' and '哈希校验失败' in bad['error']
    assert search(repo)[1]['release_id'] == good['release_id']


def test_strict_vector_requirement_fails_when_local_model_missing(repo, tmp_path):
    confirmed(repo)
    record = ReleaseRepository(repository=repo).publish(principal=ADMIN,
        embedding_model_path=tmp_path / 'missing', require_embeddings=True)
    assert record['status'] == 'failed'
    assert search(repo)[0] == []


def test_artifact_tampering_fails_closed(repo):
    confirmed(repo)
    record = publish(repo)
    path = repo.root / 'knowledge_releases' / record['release_id'] / 'index.sqlite'
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE chunks SET text='伪造知识'")
    with pytest.raises(KnowledgeError, match='哈希校验失败'):
        search(repo)


def test_unauthorized_unknown_future_expired_and_demo_never_become_candidates(repo, monkeypatch):
    allowed = confirmed(repo)
    secret = confirmed(repo, title='别厂机密', text='提取收率 绝密配方', scope_factories=['二厂'])
    confirmed(repo, title='其他产品', text='提取收率 乙产品机密', scope_products=['乙产品'])
    confirmed(repo, title='未来工艺', text='提取收率 未来技术', effective_from='2027-01-01')
    confirmed(repo, title='过期工艺', text='提取收率 已失效', effective_to='2026-02-01')
    confirmed(repo, title='演示', text='提取收率 演示案例', metadata={'is_demo': True})
    public = confirmed(repo, title='公开GMP', text='提取收率 公共法规', scope_products=[], scope_factories=[], visibility='public')
    pending = repo.stage('提取收率 未知范围'.encode(), '未知.txt', '未知', [], '2026-01-01', '未知', 'internal')
    unknown = repo.commit(pending['stage_id'], 'internal', '待治理')
    record = publish(repo)
    assert unknown['version_id'] not in record['manifest']['input_version_ids']
    engine = get_search_engine(repository=repo, embedding_model_path='')
    observed = {}
    old_bm25, old_graph = engine._bm25, engine._graph
    def bm25(query, rows):
        observed['bm25'] = {r['version_id'] for r in rows}
        return old_bm25(query, rows)
    def graph(query, rows, edges):
        observed['graph'] = {r['version_id'] for r in rows}
        assert all(chunks <= {r['chunk_id'] for r in rows} for chunks in edges.values())
        return old_graph(query, rows, edges)
    monkeypatch.setattr(engine, '_bm25', bm25)
    monkeypatch.setattr(engine, '_graph', graph)
    rows, stats = search(repo)
    expected = {allowed['version_id'], public['version_id']}
    assert observed == {'bm25': expected, 'graph': expected}
    assert {r['version_id'] for r in rows} == expected
    assert set(stats['authorized_version_ids']) == expected
    assert secret['version_id'] not in json.dumps(stats)
    # The full manifest is also scope-controlled, not a backdoor catalog dump.
    with pytest.raises(KnowledgeAccessError):
        ReleaseRepository(repository=repo).get_release(principal=READER)


def test_scope_change_and_expiry_never_resurrect_an_old_version(repo):
    first = confirmed(repo)
    one = publish(repo)
    confirmed(repo, doc_id=first['doc_id'], scope_products=['乙产品'], effective_from='2026-05-01', effective_to='2026-05-31')
    publish(repo)
    assert search(repo, as_of='2026-05-15')[0] == []
    assert search(repo, as_of='2026-06-30')[0] == []
    assert search(repo, as_of='2026-04-30')[0][0]['version_id'] == first['version_id']
    assert search(repo, known_at=one['published_at'])[0][0]['version_id'] == first['version_id']


def test_new_unpublished_version_does_not_use_old_published_text(repo):
    first = confirmed(repo)
    publish(repo)
    confirmed(repo, doc_id=first['doc_id'], text='提取收率 新版审核依据')
    rows, stats = search(repo)
    assert rows == [] and stats['reason'] == 'no_authorized_effective_published_versions'


def test_supervisor_can_confirm_publish_but_cannot_stage(repo):
    supervisor = replace(ADMIN, roles=('supervisor',))
    with pytest.raises(KnowledgeAccessError, match='knowledge_admin'):
        repo.stage('新资料'.encode(), '规则.txt', '规则', ['甲产品'], '2026-01-01', '规则', 'ignored',
                   scope_factories=['一厂'], principal=supervisor)
    pending = repo.stage('甲产品 提取收率'.encode(), '规则.txt', '规则', ['甲产品'], '2026-01-01', '规则', 'ignored',
                         scope_factories=['一厂'], principal=ADMIN)
    version = repo.commit(pending['stage_id'], 'spoofed', '批准', principal=supervisor)
    assert version['confirmed_by'] == supervisor.user_id
    assert ReleaseRepository(repository=repo).publish(principal=supervisor, embedding_model_path='')['status'] == 'published'


def test_selected_local_model_failure_retains_previous_even_without_strict_flag(repo, tmp_path, monkeypatch):
    import enterprise.knowledge_release as kr
    confirmed(repo)
    first = publish(repo)
    path = tmp_path / 'invalid-bge'; path.mkdir(); (path / 'config.json').write_text('{}')
    def broken(path):
        raise RuntimeError('invalid local weights')
    monkeypatch.setattr(kr, '_load_local_bge', broken)
    failed = ReleaseRepository(repository=repo).publish(principal=ADMIN, embedding_model_path=path)
    assert failed['status'] == 'failed' and failed['manifest']['embedding']['status'] == 'failed'
    assert search(repo)[1]['release_id'] == first['release_id']


def test_query_timeout_is_explicit_and_queued_work_is_cancelled(repo, tmp_path, monkeypatch):
    import enterprise.knowledge_release as kr
    confirmed(repo)
    path = tmp_path / 'timeout-bge'; path.mkdir(); (path / 'config.json').write_text('{}')
    block, finished = threading.Event(), threading.Event()
    calls = []
    class FakeBGE:
        def encode(self, texts, batch_size):
            calls.append(texts)
            if block.is_set():
                finished.wait(3)
            return {'dense_vecs': [[1.0] for _ in texts]}
    monkeypatch.setattr(kr, '_load_local_bge', lambda p: FakeBGE())
    publish(repo, embedding_model_path=path, require_embeddings=True)
    engine = ControlledSearchEngine(repository=repo, embedding_model_path=path)
    engine.warmup(principal=READER, wait=True)
    block.set()
    try:
        for _ in range(3):
            rows, stats = engine.search('提取收率', principal=READER, as_of='2026-06-30', vector_timeout=.02)
            assert rows and stats['degraded']
            assert 'query_embedding_timeout' in stats['degradation_reasons']
        assert len(calls) == 2  # Publication plus only the running query; queued timeouts cancelled.
    finally:
        finished.set()


def test_no_future_release_at_known_at_and_timezone_normalization(repo):
    confirmed(repo)
    published = publish(repo)
    assert search(repo, known_at='2000-01-01')[0] == []
    assert search(repo, known_at='2000-01-01', release_id=published['release_id'])[0] == []
    assert search(repo, known_at=published['published_at'])[0]
    with pytest.raises(KnowledgeError, match='时区'):
        search(repo, known_at='2026-01-01T00:00:00')


@pytest.mark.parametrize('overrides', [
    {'principal': None}, {'principal': replace(READER, tenant_id='other')},
    {'principal': replace(READER, factories=())}, {'principal': replace(READER, products=())},
    {'principal': replace(READER, roles=('system_admin',))},
    {'factory': '二厂'}, {'product': '乙产品'},
    {'allowed_scopes': {'factories': ['*'], 'products': ['甲产品']}},
    {'allowed_scopes': {'factories': [], 'products': ['甲产品']}},
])
def test_query_rejects_missing_identity_and_scope_escalation(repo, overrides):
    confirmed(repo)
    publish(repo)
    with pytest.raises(KnowledgeAccessError):
        search(repo, **overrides)


def test_whole_document_aggregate_requires_all_scopes(repo):
    confirmed(repo, scope_products=['甲产品', '乙产品'])
    publish(repo)
    assert search(repo)[0] == []
    assert search(repo, principal=ADMIN)[0]
    assert search(repo, principal=ADMIN, allowed_scopes={'factories': ['一厂'], 'products': ['甲产品']})[0] == []


def test_each_catalog_read_path_and_commit_rechecks_scope(repo):
    visible = confirmed(repo)
    hidden = confirmed(repo, title='机密', scope_factories=['二厂'])
    bound = Repository(repo.root, principal=READER)
    assert bound.get(version_id=visible['version_id'])['version_id'] == visible['version_id']
    assert [v['version_id'] for v in bound.history()] == [visible['version_id']]
    assert [v['version_id'] for v in bound.list_documents()] == [visible['version_id']]
    assert all(e['doc_id'] == visible['doc_id'] for e in bound.events())
    with pytest.raises(KnowledgeAccessError):
        bound.get(hidden['doc_id'])
    with pytest.raises(KnowledgeAccessError):
        bound.read_blob(hidden['sha256'], version_id=hidden['version_id'])
    # A staged document cannot be confirmed by a manager whose scopes changed.
    pending = repo.stage(b'private', 'new.txt', 'new', ['甲产品'], '2026-01-01', '规则', 'ignored',
                         scope_factories=['二厂'], principal=ADMIN)
    with pytest.raises(KnowledgeAccessError):
        repo.commit(pending['stage_id'], 'spoofed', '确认', principal=replace(READER, roles=('knowledge_admin',)))
    assert visible['confirmed_by'] == ADMIN.user_id


def test_immediate_revocation_and_changed_principal_invalidate_resident_engine(repo):
    value = confirmed(repo)
    record = publish(repo)
    assert search(repo)[0]
    assert search(repo, principal=replace(READER, factories=('二厂',)), factory='二厂')[0] == []
    ReleaseRepository(repository=repo).revoke_document(value['doc_id'], principal=ADMIN, reason='撤回资料')
    assert search(repo)[0] == []
    assert search(repo, known_at=record['published_at'], release_id=record['release_id'])[0] == []
    with pytest.raises(KnowledgeAccessError):
        repo.get(version_id=value['version_id'], principal=READER)
    with pytest.raises(KnowledgeAccessError):
        repo.read_blob(value['sha256'], version_id=value['version_id'], principal=READER)
    assert repo.history(principal=READER) == []
    assert repo.list_documents(principal=READER) == []
    assert repo.events(principal=READER) == []
    with pytest.raises(KnowledgeAccessError):
        ReleaseRepository(repository=repo).get_release(record['release_id'], principal=READER)
    # Trusted local backup storage retains originals; there is no delete in a revocation.
    assert repo.read_blob(value['sha256'])


def test_persisted_vectors_share_preapproved_ids_and_model_reuses(repo, tmp_path, monkeypatch):
    import enterprise.knowledge_release as kr
    model_path = tmp_path / 'local-bge-test'
    model_path.mkdir()
    (model_path / 'config.json').write_text('{}', encoding='utf-8')
    (model_path / 'weights.bin').write_bytes(b'test-only')
    loads, calls = [], []
    class FakeBGE:
        def encode(self, texts, batch_size):
            calls.append(list(texts))
            return {'dense_vecs': [[1.0, 0.25] for _ in texts]}
    def load(path):
        loads.append(path)
        return FakeBGE()
    monkeypatch.setattr(kr, '_load_local_bge', load)
    good = confirmed(repo)
    hidden = confirmed(repo, title='二厂机密', text='提取收率 机密', scope_factories=['二厂'])
    record = publish(repo, embedding_model_path=model_path, require_embeddings=True)
    artifact = repo.root / 'knowledge_releases' / record['release_id'] / 'index.sqlite'
    with sqlite3.connect(artifact) as conn:
        assert conn.execute('SELECT COUNT(*) FROM chunks WHERE vector IS NOT NULL').fetchone()[0] == 2
    engine = ControlledSearchEngine(repository=repo, embedding_model_path=model_path)
    assert engine.warmup(principal=READER, wait=True)['state'] == 'ready'
    observed = []
    original = engine._vector
    def vector(query, rows):
        observed.append({row['version_id'] for row in rows})
        return original(query, rows)
    monkeypatch.setattr(engine, '_vector', vector)
    for _ in range(2):
        rows, stats = engine.search('提取收率', principal=READER, product='甲产品', factory='一厂', as_of='2026-06-30')
        assert rows and stats['retrieval_mode'] == 'vector_bm25_graph' and not stats['degraded']
        assert stats['vector_n'] == 1
    assert observed == [{good['version_id']}, {good['version_id']}]
    assert len(loads) == 1
    assert hidden['version_id'] not in {r['version_id'] for r in rows}
    assert set(stats['route_generations'].values()) == {record['release_id']}


def test_cold_model_query_returns_explicit_fallback_without_wait(repo, tmp_path, monkeypatch):
    import enterprise.knowledge_release as kr
    confirmed(repo)
    # Build a vector release, then represent a newly started request process
    # whose warmup is still running, without actually loading any real model.
    path = tmp_path / 'tiny-bge'
    path.mkdir(); (path / 'config.json').write_text('{}')
    class FakeBGE:
        def encode(self, texts, batch_size):
            return {'dense_vecs': [[1.0] for _ in texts]}
    monkeypatch.setattr(kr, '_load_local_bge', lambda p: FakeBGE())
    publish(repo, embedding_model_path=path, require_embeddings=True)
    engine = ControlledSearchEngine(repository=repo, embedding_model_path=path)
    monkeypatch.setattr(engine, 'warmup', lambda **kw: {'state': 'loading'})
    rows, stats = engine.search('提取收率', principal=READER, as_of='2026-06-30')
    assert rows and stats['degraded'] and 'embedding_loading' in stats['degradation_reasons']


def test_bootstrap_is_explicit_idempotent_and_rejects_paths(repo, tmp_path):
    originals = tmp_path / 'originals'
    originals.mkdir()
    original = originals / '制度.csv'
    original.write_text('条款,说明\n1,甲产品提取收率\n', encoding='utf-8')
    specs = [{'filename': '制度.csv', 'title': '核准规则', 'scope_products': ['甲产品'],
              'scope_factories': ['一厂'], 'visibility': 'scoped', 'effective_from': '2026-01-01',
              'category': '工艺', 'sha256': hashlib.sha256(original.read_bytes()).hexdigest()}]
    first = bootstrap_official(originals, repository=repo, principal=ADMIN, documents=specs, reason='核准官方数据')
    assert first['status'] == 'confirmed_not_published' and search(repo)[0] == []
    second = bootstrap_official(originals, repository=repo, principal=ADMIN, documents=specs,
                                reason='再次核对', publish=True, embedding_model_path='')
    assert second['versions'][0]['version_id'] == first['versions'][0]['version_id']
    assert len(repo.history()) == 1 and search(repo)[0]
    with pytest.raises(KnowledgeError, match='源目录'):
        bootstrap_official(originals, repository=repo, principal=ADMIN,
                           documents=[{**specs[0], 'filename': '../outside.csv'}], reason='核准')
    assert original.read_bytes().decode('utf-8').startswith('条款,说明')


def test_revocation_during_candidate_scoring_blocks_final_evidence(repo, monkeypatch):
    value = confirmed(repo)
    publish(repo)
    engine = get_search_engine(repository=repo, embedding_model_path='')
    original = engine._bm25
    def revoke(query, rows):
        result = original(query, rows)
        ReleaseRepository(repository=repo).revoke_document(value['doc_id'], principal=ADMIN, reason='检索途中撤回')
        return result
    monkeypatch.setattr(engine, '_bm25', revoke)
    rows, stats = search(repo)
    assert rows == [] and stats['no_answer'] and stats['authorized_version_ids'] == []


def test_bootstrap_reviews_same_original_legacy_unknown_document(repo, tmp_path):
    originals = tmp_path / 'approved-originals'; originals.mkdir()
    content = '条款,说明\n1,提取收率\n'.encode('utf-8')
    (originals / '旧规则.csv').write_bytes(content)
    stage = repo.stage(content, '旧规则.csv', '旧规则', [], '2026-01-01', '工艺', 'legacy')
    old = repo.commit(stage['stage_id'], 'legacy', '旧登记')
    specs = [{'filename': '旧规则.csv', 'title': '旧规则', 'scope_products': ['甲产品'],
              'scope_factories': ['一厂'], 'visibility': 'scoped', 'effective_from': '2026-01-01', 'category': '工艺'}]
    result = bootstrap_official(originals, repository=repo, principal=ADMIN, documents=specs,
                                reason='核对相同官方原件后明确范围', publish=True, embedding_model_path='')
    new = result['versions'][0]
    assert new['doc_id'] == old['doc_id'] and new['version'] == 2
    assert new['sha256'] == old['sha256']
    assert search(repo)[0][0]['version_id'] == new['version_id']


def test_limited_manager_cannot_change_or_revoke_public_document(repo):
    value = confirmed(repo, scope_products=[], scope_factories=[], visibility='public')
    manager = replace(READER, roles=('knowledge_admin',))
    with pytest.raises(KnowledgeAccessError):
        repo.stage(value['text'].encode(), value['filename'], value['title'], ['甲产品'], '2026-01-01',
                   '工艺', 'ignored', doc_id=value['doc_id'], scope_factories=['一厂'], principal=manager)
    with pytest.raises(KnowledgeAccessError):
        ReleaseRepository(repository=repo).revoke_document(value['doc_id'], principal=manager, reason='局部撤销')
    with pytest.raises(KnowledgeAccessError):
        ReleaseRepository(repository=repo).publish(principal=manager, embedding_model_path='')
    assert repo.get(value['doc_id'])['visibility'] == 'public'


def test_publication_timestamp_is_after_validation_not_build_start(repo, monkeypatch):
    import enterprise.knowledge_release as kr
    first = confirmed(repo)
    original = publish(repo)
    confirmed(repo, doc_id=first['doc_id'], text='提取收率 新证据')
    releases = ReleaseRepository(repository=repo)
    cutoffs = []
    validator = releases._validate_artifact
    # Windows wall-clock reads may share a microsecond. Advance a controlled
    # clock to test call order, rather than the host timer's resolution.
    from datetime import datetime, timedelta, timezone
    clock = [datetime.now(timezone.utc)]
    def ordered_now():
        clock[0] = max(datetime.now(timezone.utc), clock[0] + timedelta(microseconds=1))
        return clock[0].isoformat(timespec='microseconds')
    monkeypatch.setattr(kr, '_now', ordered_now)
    def pause(path, manifest):
        cutoffs.append(kr._now())
        validator(path, manifest)
    monkeypatch.setattr(releases, '_validate_artifact', pause)
    newest = releases.publish(principal=ADMIN, embedding_model_path='')
    assert newest['status'] == 'published'
    assert newest['published_at'] > cutoffs[0]
    assert search(repo, known_at=cutoffs[0])[1]['release_id'] == original['release_id']
    assert search(repo)[1]['release_id'] == newest['release_id']
    assert not (repo.root / 'knowledge_releases' / newest['release_id'] / 'manifest.json').exists()


def test_warm_engine_rechecks_content_even_when_stat_restored(repo):
    confirmed(repo)
    record = publish(repo)
    rows, _ = search(repo)
    artifact = repo.root / 'knowledge_releases' / record['release_id'] / 'index.sqlite'
    before = artifact.stat()
    with sqlite3.connect(artifact) as conn:
        conn.execute('UPDATE chunks SET text=?', ('乙' * len(rows[0]['text']),))
    os.utime(artifact, ns=(before.st_atime_ns, before.st_mtime_ns))
    with pytest.raises(KnowledgeError, match='哈希校验失败'):
        search(repo)


def test_explicit_unknown_scope_review_preserves_old_version(repo):
    pending = repo.stage('甲产品 提取收率 旧资料'.encode(), '旧资料.txt', '旧资料', [], '2026-01-01', '工艺', 'internal')
    old = repo.commit(pending['stage_id'], 'internal', '旧系统登记')
    with pytest.raises(KnowledgeAccessError):
        repo.get(version_id=old['version_id'], principal=ADMIN)
    options = dict(expected_version_id=old['version_id'], expected_sha256=old['sha256'],
                   scope_products=['甲产品'], scope_factories=['一厂'], visibility='scoped',
                   effective_from='2026-01-01', review_reason='逐件核对原件与工厂范围')
    with pytest.raises(KnowledgeAccessError):
        repo.stage_scope_review(old['doc_id'], principal=replace(READER, roles=('knowledge_admin',)), **options)
    review = repo.stage_scope_review(old['doc_id'], principal=ADMIN, **options)
    assert review['change']['kind'] == 'metadata_revision' and not review['errors']
    new = repo.commit(review['stage_id'], 'ignored', '批准范围', principal=ADMIN)
    assert new['version'] == 2 and new['sha256'] == old['sha256']
    assert repo.get(version_id=old['version_id'])['visibility'] == 'unknown'
    assert new['doc_id'] == old['doc_id']
    publish(repo)
    assert search(repo)[0][0]['version_id'] == new['version_id']


def test_build_timeout_fails_and_releases_global_write_lock(repo, tmp_path, monkeypatch):
    import enterprise.knowledge_release as kr
    from enterprise.operations import write_guard
    confirmed(repo)
    old = publish(repo)
    path = tmp_path / 'blocked-bge'; path.mkdir(); (path / 'config.json').write_text('{}')
    entered, finish = threading.Event(), threading.Event()
    class SlowBGE:
        def encode(self, texts, batch_size):
            entered.set()
            finish.wait(3)
            return {'dense_vecs': [[1.0] for _ in texts]}
    monkeypatch.setattr(kr, '_load_local_bge', lambda p: SlowBGE())
    try:
        failed = ReleaseRepository(repository=repo).publish(principal=ADMIN,
            embedding_model_path=path, require_embeddings=True, build_timeout=.05)
        assert failed['status'] == 'failed'
        assert search(repo)[1]['release_id'] == old['release_id']
        with write_guard(repo.root, timeout=.1):
            pass
    finally:
        finish.set()


def test_updated_local_model_rewarms_existing_engine(repo, tmp_path, monkeypatch):
    import enterprise.knowledge_release as kr
    confirmed(repo)
    path = tmp_path / 'replaceable-bge'; path.mkdir()
    (path / 'config.json').write_text('{}')
    weights = path / 'weights.bin'; weights.write_bytes(b'v1')
    class FakeBGE:
        def encode(self, texts, batch_size):
            return {'dense_vecs': [[1.0, 1.0] for _ in texts]}
    loads = []
    def load(p):
        loads.append(p)
        return FakeBGE()
    monkeypatch.setattr(kr, '_load_local_bge', load)
    one = publish(repo, embedding_model_path=path, require_embeddings=True)
    engine = ControlledSearchEngine(repository=repo, embedding_model_path=path)
    engine.warmup(principal=READER, wait=True)
    weights.write_bytes(b'version-two')
    two = publish(repo, embedding_model_path=path, require_embeddings=True)
    assert one['manifest']['embedding']['fingerprint'] != two['manifest']['embedding']['fingerprint']
    rows, _ = engine.search('提取收率', principal=READER, as_of='2026-06-30')
    # First request initiated nonblocking fingerprint-based reload.
    engine.warmup(principal=READER, wait=True)
    rows, stats = engine.search('提取收率', principal=READER, as_of='2026-06-30')
    assert rows and not stats['degraded'] and len(loads) == 2


def test_interrupted_build_has_explicit_recovery_without_pointer_switch(repo):
    import enterprise.knowledge_release as kr
    confirmed(repo)
    record = publish(repo)
    releases = ReleaseRepository(repository=repo)
    orphan = {**record['manifest'], 'release_id': 'orphan', 'generation': 'orphan', 'status': 'building', 'published_at': None}
    with sqlite3.connect(releases.db_path) as conn:
        conn.execute('INSERT INTO releases VALUES(?,?,?,?,?,?,?,?,?)', ('orphan', 'building', kr._now(), None,
            ADMIN.user_id, record['release_id'], kr.canonical_json(orphan), kr._digest(orphan), ''))
    assert releases.recover_interrupted_builds(principal=ADMIN) == ['orphan']
    recovered = releases.get_release('orphan', principal=ADMIN)
    assert recovered['status'] == 'failed' and recovered['error'] == 'interrupted_before_publication'
    assert search(repo)[1]['release_id'] == record['release_id']


def test_embedding_cpu_configuration_changes_manifest_and_model_cache(repo, tmp_path, monkeypatch):
    import enterprise.knowledge_release as kr
    monkeypatch.delenv('COST_EMBED_THREADS', raising=False)
    monkeypatch.delenv('COST_EMBED_BATCH_SIZE', raising=False)
    model_path = tmp_path / 'runtime-config-model'; model_path.mkdir()
    (model_path / 'config.json').write_text('{}')
    loads, batches = [], []
    class FakeBGE:
        def encode(self, texts, batch_size):
            batches.append(batch_size)
            return {'dense_vecs': [[1.0] for _ in texts]}
    def load(path):
        loads.append(path)
        return FakeBGE()
    monkeypatch.setattr(kr, '_load_local_bge', load)
    confirmed(repo)
    first = publish(repo, embedding_model_path=model_path, require_embeddings=True)
    first_model = first['manifest']['embedding']['model']
    assert (first_model['threads'], first_model['batch_size'], first_model['max_length']) == (4, 2, 1024)
    assert first_model['overflow'] == 'reject_without_truncation' and batches == [2]
    monkeypatch.setenv('COST_EMBED_THREADS', '2')
    monkeypatch.setenv('COST_EMBED_BATCH_SIZE', '1')
    second = publish(repo, embedding_model_path=model_path, require_embeddings=True)
    assert first['manifest']['embedding']['fingerprint'] != second['manifest']['embedding']['fingerprint']
    assert second['manifest']['embedding']['model']['sha256'] == first_model['sha256']
    assert batches == [2, 1] and len(loads) == 2
    monkeypatch.setenv('COST_EMBED_THREADS', '0')
    with pytest.raises(KnowledgeError, match='COST_EMBED_THREADS'):
        kr._embedding_config()


def test_dense_encoder_rejects_overflow_without_truncating_or_running_model(tmp_path, monkeypatch):
    import sys
    from contextlib import nullcontext
    from types import SimpleNamespace
    import enterprise.knowledge_release as kr
    observed = {'threads': [], 'tokenizer': [], 'model_calls': []}
    class FakeModel:
        def float(self): return self
        def eval(self): return self
        def to(self, device): return self
        def __call__(self, **tokens):
            observed['model_calls'].append(True)
            raise AssertionError('overlength input must be rejected before dense inference')
    class FakeTokenizer:
        def __call__(self, texts, **kwargs):
            observed['tokenizer'].append((texts, kwargs))
            return {'input_ids': SimpleNamespace(shape=(len(texts), 1025))}
    class TokenizerLoader:
        @staticmethod
        def from_pretrained(*args, **kwargs):
            assert kwargs['local_files_only'] is True
            return FakeTokenizer()
    class ModelLoader:
        @staticmethod
        def from_pretrained(*args, **kwargs):
            assert kwargs['local_files_only'] is True
            return FakeModel()
    monkeypatch.setenv('COST_EMBED_THREADS', '4')
    monkeypatch.setenv('COST_EMBED_BATCH_SIZE', '2')
    monkeypatch.setitem(sys.modules, 'torch', SimpleNamespace(set_num_threads=observed['threads'].append,
                                                            inference_mode=nullcontext))
    monkeypatch.setitem(sys.modules, 'transformers', SimpleNamespace(AutoTokenizer=TokenizerLoader, AutoModel=ModelLoader))
    model = kr._load_local_bge(tmp_path)
    text = '原文正文必须完整保留' * 200
    with pytest.raises(KnowledgeError, match='未截断正文'):
        model.encode([text])
    assert observed['threads'] == [4, 4]  # Both loader and actual inference worker set the bound.
    assert observed['tokenizer'][0][0] == [text]
    assert observed['tokenizer'][0][1]['truncation'] is False
    assert observed['model_calls'] == []


def test_bootstrap_build_timeout_reaches_publish(repo, tmp_path, monkeypatch):
    originals = tmp_path / 'bootstrap-timeout'; originals.mkdir()
    (originals / 'original.csv').write_text('条款,说明\n1,提取收率\n', encoding='utf-8')
    specs = [{'filename': 'original.csv', 'title': '核准规则', 'scope_products': ['甲产品'],
              'scope_factories': ['一厂'], 'visibility': 'scoped', 'effective_from': '2026-01-01', 'category': '工艺'}]
    seen = []
    def capture(self, **kwargs):
        seen.append(kwargs)
        return {'status': 'failed', 'error': 'test-timeout'}
    monkeypatch.setattr(ReleaseRepository, 'publish', capture)
    result = bootstrap_official(originals, repository=repo, principal=ADMIN, documents=specs,
        reason='测试超时透传', publish=True, embedding_model_path='', build_timeout=1800)
    assert result['status'] == 'failed' and seen[0]['build_timeout'] == 1800
    with pytest.raises(KnowledgeError, match='build_timeout'):
        bootstrap_official(originals, repository=repo, principal=ADMIN, documents=specs,
                           reason='测试非法时限', publish=True, build_timeout=3601)


def test_official_wrapper_forwards_build_timeout(repo, tmp_path, monkeypatch):
    import enterprise.bootstrap as wrapper
    import enterprise.knowledge_release as kr
    from enterprise.security import Principal as SecurityPrincipal
    principal = SecurityPrincipal('test', 'test', ('knowledge_admin',), ('*',), ('*',))
    seen = []
    monkeypatch.setattr(wrapper, 'official_document_specs', lambda source: [])
    def capture(source, **kwargs):
        seen.append(kwargs)
        return {'status': 'failed'}
    monkeypatch.setattr(kr, 'bootstrap_official', capture)
    wrapper.bootstrap_knowledge(principal=principal, source_root=tmp_path, root=repo.root, build_timeout=1800)
    assert seen[0]['build_timeout'] == 1800


def test_bootstrap_cli_propagates_timeout_and_exits_nonzero_on_failed_release(monkeypatch, capsys):
    import enterprise.bootstrap as bootstrap
    import enterprise.security as security
    from scripts.bootstrap_system import main
    called = []
    monkeypatch.setattr(security, 'auth_mode', lambda: 'local')
    monkeypatch.setattr(security, 'local_principal', lambda: ADMIN)
    def fail(**kwargs):
        called.append(kwargs)
        return {'status': 'failed', 'versions': [], 'release': {'status': 'failed', 'release_id': 'test',
                'error': 'TimeoutError', 'manifest': {}}}
    monkeypatch.setattr(bootstrap, 'bootstrap_knowledge', fail)
    assert main(['--dense', '--build-timeout', '1800']) == 1
    assert called[0]['build_timeout'] == 1800 and called[0]['dense'] is True
    assert json.loads(capsys.readouterr().out)['status'] == 'failed'
    for value in ('0', '3601', 'nan'):
        with pytest.raises(SystemExit) as exc:
            main(['--build-timeout', value])
        assert exc.value.code == 2
    assert len(called) == 1


def test_read_context_cannot_silently_bypass_identity(repo):
    confirmed(repo)
    publish(repo)
    with pytest.raises(KnowledgeAccessError):
        context('甲产品', '2026-06', '提取收率', repository=repo)
