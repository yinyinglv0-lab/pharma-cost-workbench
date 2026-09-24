"""Strict hybrid failure boundaries; no model download or fake-vector proof.

These lightweight failure-injection tests use lexical fixtures. The actual dense
model/integrity/graph evidence comes only from deep_review_hybrid_candidate.py.
"""
from concurrent.futures import TimeoutError as FutureTimeout
from copy import deepcopy

import pytest

from enterprise.knowledge import KnowledgeError, Repository
from enterprise.knowledge_release import ControlledSearchEngine, ReleaseRepository
from enterprise.knowledge_runtime import begin_warmup, readiness
from enterprise.security import Principal

ADMIN = Principal('hybrid-test-admin', 'Test', ('knowledge_admin',), ('*',), ('*',))
READER = Principal('hybrid-test-reader', 'Test', ('analyst',), ('中药一厂',), ('银黄口服液',))


def confirmed(repo, title='工艺', category='生产工艺', **extra):
    options = dict(filename=title + '.txt', title=title,
                   scope_products=['银黄口服液'], scope_factories=['中药一厂'],
                   visibility='scoped', effective_from='2026-01-01', category=category,
                   actor=ADMIN.user_id, principal=ADMIN)
    options.update(extra)
    pending = repo.stage(('银黄口服液 金银花 提取收率 ' + title).encode('utf-8'), **options)
    assert not pending['errors']
    return repo.commit(pending['stage_id'], ADMIN.user_id, 'isolated test fixture', principal=ADMIN)


@pytest.fixture
def lexical(tmp_path):
    repo = Repository(tmp_path / 'isolated')
    confirmed(repo)
    release = ReleaseRepository(repository=repo).publish(principal=ADMIN,
        embedding_model_path='', require_embeddings=False)
    assert release['status'] == 'published'
    engine = ControlledSearchEngine(repository=repo, embedding_model_path='')
    return repo, release, engine


def options(**extra):
    return dict(principal=READER, product='银黄口服液', factory='中药一厂',
                as_of='2026-06-30', **extra)


def test_lexical_ready_is_not_hybrid_ready(lexical):
    repo, release, engine = lexical
    legacy = engine.readiness(principal=READER)
    strict = engine.readiness(principal=READER, require_hybrid=True)
    assert legacy['ready'] and legacy['state'] == 'ready_lexical'
    assert not strict['ready'] and strict['state'] == 'hybrid_required_no_vectors'
    assert strict['release_id'] == release['release_id']
    assert not readiness(READER, repository=repo)['ready']
    assert readiness(READER, repository=repo, require_hybrid=False)['ready']
    assert not begin_warmup(READER, repository=repo)['ready']


def test_strict_lexical_request_does_not_generate_candidates(lexical, monkeypatch):
    _, _, engine = lexical
    monkeypatch.setattr(engine, '_load_authorized', lambda *a: pytest.fail('candidate generation forbidden'))
    rows, stats = engine.search('金银花提取收率', **options(require_hybrid=True))
    assert rows == [] and stats['no_answer']
    assert stats['reason'] == 'hybrid_unavailable'
    assert stats['retrieval_mode'] == 'unavailable'
    assert stats['vector_n'] == stats['bm25_n'] == stats['fused_n'] == 0
    assert stats['require_hybrid'] is True


@pytest.mark.parametrize('invalid', [1, 0, None, 'true', [], {}])
def test_require_hybrid_must_be_boolean(lexical, invalid):
    _, _, engine = lexical
    with pytest.raises(KnowledgeError, match='require_hybrid'):
        engine.readiness(principal=READER, require_hybrid=invalid)
    with pytest.raises(KnowledgeError, match='require_hybrid'):
        engine.search('金银花', **options(require_hybrid=invalid))


def test_missing_model_strict_publication_preserves_active(lexical, tmp_path):
    repo, release, _ = lexical
    releases = ReleaseRepository(repository=repo)
    failed = releases.publish(principal=ADMIN, embedding_model_path=tmp_path / 'missing',
                              require_embeddings=True)
    assert failed['status'] == 'failed'
    assert releases.get_release(principal=ADMIN)['release_id'] == release['release_id']


def test_fingerprint_mismatch_is_explicit_not_ready(lexical, monkeypatch):
    _, release, engine = lexical
    record = deepcopy(release)
    record['manifest']['embedding'] = {'status': 'ready', 'fingerprint': 'expected'}
    monkeypatch.setattr(engine.releases, '_raw_release', lambda *a: record)
    monkeypatch.setattr(engine, 'warmup', lambda **kw: {'state': 'ready', 'fingerprint': 'other'})
    result = engine.readiness(principal=READER, require_hybrid=True)
    assert not result['ready'] and result['state'] == 'embedding_fingerprint_mismatch'
    rows, stats = engine.search('金银花', **options(require_hybrid=True))
    assert rows == [] and stats['reason'] == 'hybrid_unavailable'


@pytest.mark.parametrize('failure', [FutureTimeout, RuntimeError])
def test_failed_dense_query_never_returns_lexical_success(lexical, monkeypatch, failure):
    _, release, engine = lexical
    # Failure injection only: use actual lexical rows; no fabricated dense vector.
    original_loader = engine._load_authorized
    record = deepcopy(release)
    record['manifest']['embedding'] = {'status': 'ready', 'fingerprint': 'failure-test'}
    record['manifest']['degradation_reasons'] = []
    monkeypatch.setattr(engine.releases, '_raw_release', lambda *a: record)
    monkeypatch.setattr(engine, '_load_authorized', lambda r, ids: original_loader(release, ids))
    monkeypatch.setattr(engine, 'warmup', lambda **kw: {'state': 'ready', 'fingerprint': 'failure-test'})

    class FailingHandle:
        def encode(self, *args, **kwargs):
            raise failure('injected query failure')
    engine._handle = FailingHandle()
    rows, stats = engine.search('金银花提取收率', **options(require_hybrid=True))
    assert rows == [] and stats['no_answer'] and stats['degraded']
    assert stats['retrieval_mode'] == 'unavailable' and stats['reason'] == 'hybrid_unavailable'
    expected = 'query_embedding_timeout' if failure is FutureTimeout else 'query_embedding_failed:RuntimeError'
    assert expected in stats['degradation_reasons']
    legacy_rows, legacy = engine.search('金银花提取收率', **options(require_hybrid=False))
    assert legacy_rows and legacy['degraded'] and legacy['retrieval_mode'] == 'bm25_graph_fallback'


def test_acl_type_and_dates_filter_before_routes_even_on_dense_failure(tmp_path, monkeypatch):
    repo = Repository(tmp_path / 'isolated-filters')
    allowed = confirmed(repo)
    confirmed(repo, title='别厂机密', scope_factories=['中药二厂'])
    confirmed(repo, title='未来工艺', effective_from='2027-01-01')
    confirmed(repo, title='过期工艺', effective_to='2026-02-01')
    confirmed(repo, title='配方资料', category='产品配方')
    release = ReleaseRepository(repository=repo).publish(principal=ADMIN, embedding_model_path='', require_embeddings=False)
    engine = ControlledSearchEngine(repository=repo, embedding_model_path='')
    original_loader, original_bm25 = engine._load_authorized, engine._bm25
    record = deepcopy(release)
    record['manifest']['embedding'] = {'status': 'ready', 'fingerprint': 'failure-test'}
    record['manifest']['degradation_reasons'] = []
    monkeypatch.setattr(engine.releases, '_raw_release', lambda *a: record)
    monkeypatch.setattr(engine, '_load_authorized', lambda r, ids: original_loader(release, ids))
    monkeypatch.setattr(engine, 'warmup', lambda **kw: {'state': 'ready', 'fingerprint': 'failure-test'})
    seen = []
    def capture(query, rows):
        seen.extend(rows)
        return original_bm25(query, rows)
    monkeypatch.setattr(engine, '_bm25', capture)
    class FailingHandle:
        def encode(self, *a, **kw):
            raise FutureTimeout()
    engine._handle = FailingHandle()
    rows, stats = engine.search('金银花提取收率', **options(require_hybrid=True, knowledge_types=['process']))
    assert not rows and stats['reason'] == 'hybrid_unavailable'
    assert seen and {r['version_id'] for r in seen} == {allowed['version_id']}
    assert stats['candidate_filter']['excluded_type_or_period'] == 1
    assert stats['category_filter_stage'] == 'after_authorization_before_all_candidates'
