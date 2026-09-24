"""Real temporary repository/service integration; never production data or models.

All observations use labelled SIMULATION fixtures. Controlled retriever/run_stage
patches below are explicitly test doubles, not claims of real RAG/model acceptance.
The ordinary fallback tests use the real empty temporary knowledge repository.
"""
from contextlib import closing
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import socket
import sqlite3
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from enterprise.manufacturing_repository import ManufacturingConflict, ManufacturingRepository, canonical, digest
from enterprise.manufacturing_service import ManufacturingService
from enterprise.security import Principal

PERIODS = ['2026-05', '2026-06']
FAMILIES = ('actual', 'budget', 'materials', 'labor', 'overhead')
ADMIN = Principal('mfg-integration-admin', 'Synthetic admin', ('system_admin', 'supervisor'), ('*',), ('*',))
ANALYST = Principal('mfg-integration-analyst', 'Synthetic analyst', ('analyst',), ('*',), ('*',))
AUDITOR = Principal('mfg-integration-auditor', 'Synthetic auditor', ('auditor',), ('*',), ('*',))


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setenv('COST_TENANT_ID', 'default')
    def forbidden(*args, **kwargs):
        raise AssertionError('Offline repository/service test must never perform network I/O')
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    # Real models are forbidden. Specific model-spy tests replace this test guard.
    monkeypatch.setattr('attribution_runtime.run_stage', forbidden)


def example(industry='machinery'):
    folder = ROOT / 'config' / 'manufacturing_examples' / industry
    profile = json.loads((folder / 'domain.json').read_text(encoding='utf-8-sig'))
    adapter = json.loads((folder / 'adapter.json').read_text(encoding='utf-8-sig'))
    files = {family: (family + '.csv', (folder / (family + '.csv')).read_bytes()) for family in FAMILIES}
    return profile, adapter, files


def configured(tmp_path, industry='machinery'):
    profile, adapter, files = example(industry)
    root = tmp_path / industry
    repo = ManufacturingRepository(root, ADMIN)
    repo.install_profile(profile, adapter, expected_version=0, reason='SIMULATION isolated fixture')
    return root, repo, profile, adapter, files


def confirmed(tmp_path, industry='machinery'):
    root, repo, profile, adapter, files = configured(tmp_path, industry)
    stage = repo.stage(profile['id'], files, periods=PERIODS, expected_revision=0)
    repo.confirm(stage['stage_id'], expected_revision=0, reason='SIMULATION reviewed fixture')
    return root, repo, profile, adapter, files, stage


def query(profile, month='2026-06'):
    item = profile['products'][0]
    return {'product': item['name'], 'specification': item['specification'], 'month': month}


def analyze(tmp_path, *, industry='machinery', month='2026-06', use_llm=False):
    root, repo, profile, adapter, files, stage = confirmed(tmp_path, industry)
    service = ManufacturingService(root, ADMIN)
    result = service.analyze(profile['id'], **query(profile, month), use_llm=use_llm)
    return root, repo, service, profile, adapter, files, stage, result


def change_scope(profile, adapter):
    p, a = deepcopy(profile), deepcopy(adapter)
    p['version'] += '-new-scope'
    p['factories'] = {'home': 'SIMULATION New Home Factory', 'peer': 'SIMULATION New Peer Factory'}
    a['factories'] = list(p['factories'].values())
    return p, a


def table_count(db, table):
    with closing(sqlite3.connect(db)) as con:
        return con.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0]


def no_knowledge_test_double(monkeypatch, *, hook=None, release='SIMULATION_TEST_DOUBLE_RELEASE'):
    """Explicit controlled retriever test double: no fabricated K documents."""
    from enterprise.analysis_service import EvidenceList
    calls = []
    def retrieve(principal, product, specification, months, root=None, **kwargs):
        calls.append({'principal': principal, 'product': product, 'specification': specification,
                      'months': list(months), 'root': root, 'kwargs': deepcopy(kwargs)})
        if hook:
            hook(len(calls))
        return EvidenceList([], {'degraded': False, 'release_id': release, 'mode': 'TEST_DOUBLE_NOT_REAL_HYBRID'})
    monkeypatch.setattr('enterprise.analysis_service.report_evidence', retrieve)
    return calls


@pytest.mark.parametrize('industry', ['pharma', 'machinery', 'auto_parts', 'chemicals', 'electronics'])
def test_real_repository_complete_bundle_replay_and_legacy_files_untouched(tmp_path, industry):
    root = tmp_path / industry
    root.mkdir()
    sentinels = {'cost_versions.db': b'LEGACY COST DATABASE SENTINEL',
                 'legacy-data.csv': b'LEGACY DATA SENTINEL', 'model_config.json': b'{"fixture":"unchanged"}'}
    for name, value in sentinels.items():
        (root / name).write_bytes(value)
    _, repo, profile, adapter, files, stage = confirmed(tmp_path, industry)
    current = repo.current(profile['id'])
    assert current['revision'] == 1
    assert current['runtime'].verify(profile=profile, adapter_config=adapter).fingerprint == stage['runtime_fingerprint']
    for source in stage['tables'].values():
        family = source['source_id'].split(':')[0]
        assert source['source_sha256'] == hashlib.sha256(files[family][1]).hexdigest()
    for name, value in sentinels.items():
        assert (root / name).read_bytes() == value
    assert current['runtime'].analysis(**query(profile))['data_classification'] == 'simulation'
    current['profile']['label'] = 'caller mutation'
    assert repo.current(profile['id'])['profile']['label'] == profile['label']
    assert not (root / 'task_workflow.db').exists()


def test_two_stages_compare_and_swap_and_reinstall_invalidates_pending_stage(tmp_path):
    _, repo, profile, adapter, files = configured(tmp_path)
    one = repo.stage(profile['id'], files, periods=PERIODS, expected_revision=0)
    two = repo.stage(profile['id'], files, periods=PERIODS, expected_revision=0)
    repo.confirm(one['stage_id'], expected_revision=0, reason='first wins')
    with pytest.raises(ManufacturingConflict):
        repo.confirm(two['stage_id'], expected_revision=0, reason='stale denied')
    assert table_count(repo.db, 'manufacturing_revisions') == 1
    pending = repo.stage(profile['id'], files, periods=PERIODS, expected_revision=1)
    changed = deepcopy(profile)
    changed['version'] += '-changed'
    repo.install_profile(changed, adapter, expected_version=1, reason='new domain version')
    with pytest.raises(ManufacturingConflict):
        repo.confirm(pending['stage_id'], expected_revision=1, reason='old binding denied')
    with pytest.raises(ManufacturingConflict):
        repo.current(profile['id'])
    fresh = repo.stage(profile['id'], files, periods=PERIODS, expected_revision=1)
    repo.confirm(fresh['stage_id'], expected_revision=1, reason='new binding reviewed')
    assert repo.current(profile['id'])['revision'] == 2


@pytest.mark.parametrize('principal', [
    Principal('home-only', 'Home only', ('supervisor',), ('SIMULATION Machinery Home Factory',), ('*',)),
    Principal('one-product', 'One product', ('supervisor',), ('*',), ('SIMULATION Centrifugal Pump',)),
    Principal('wrong-tenant', 'Wrong tenant', ('system_admin', 'supervisor'), ('*',), ('*',), tenant_id='other'),
])
def test_complete_bundle_scope_not_granted_by_configuration(tmp_path, principal):
    root, _, profile, _, files, stage = confirmed(tmp_path)
    limited = ManufacturingRepository(root, principal)
    for operation in (
        lambda: limited.configuration(profile['id']), lambda: limited.current(profile['id']),
        lambda: limited._stage_view(stage['stage_id']),
        lambda: limited.stage(profile['id'], files, periods=PERIODS, expected_revision=1),
        lambda: limited.confirm(stage['stage_id'], expected_revision=1, reason='denied'),
    ):
        with pytest.raises(PermissionError):
            operation()


def test_no_analyst_confirmation_or_role_install_and_no_schema_binding_grant(tmp_path):
    root, repo, profile, adapter, files = configured(tmp_path)
    analyst = ManufacturingRepository(root, ANALYST)
    stage = analyst.stage(profile['id'], files, periods=PERIODS, expected_revision=0)
    with pytest.raises(PermissionError):
        analyst.confirm(stage['stage_id'], expected_revision=0, reason='analyst cannot confirm')
    with pytest.raises(PermissionError):
        analyst.install_profile(profile, adapter, expected_version=1, reason='analyst cannot configure')
    bad = deepcopy(adapter)
    bad['currency']['target'] = 'USD'
    with pytest.raises(ValueError):
        repo.install_profile(profile, bad, expected_version=1, reason='bad binding')
    assert repo.configuration(profile['id'])['version'] == 1


def test_rejected_csv_is_atomic_no_sources_stages_revisions(tmp_path):
    _, repo, profile, _, files = configured(tmp_path)
    bad = deepcopy(files)
    bad['actual'] = ('actual.csv', files['actual'][1].replace(b'2000', b'2001', 1))
    with pytest.raises(ValueError):
        repo.stage(profile['id'], bad, periods=PERIODS, expected_revision=0)
    for table in ('manufacturing_sources', 'manufacturing_stages', 'manufacturing_revisions'):
        assert table_count(repo.db, table) == 0


@pytest.mark.parametrize('table,column', [
    ('manufacturing_profiles', 'content'), ('manufacturing_stages', 'content'),
    ('manufacturing_revisions', 'content'), ('manufacturing_sources', 'content'),
    ('manufacturing_events', 'detail'),
])
def test_persisted_repository_records_are_sqlite_append_only(tmp_path, table, column):
    _, repo, _, _, _, _ = confirmed(tmp_path)
    with closing(sqlite3.connect(repo.db)) as con:
        with pytest.raises(sqlite3.DatabaseError, match='immutable|append'):
            con.execute('UPDATE ' + table + ' SET ' + column + '=' + column)
        con.rollback()
        with pytest.raises(sqlite3.DatabaseError, match='immutable|append'):
            con.execute('DELETE FROM ' + table)
        con.rollback()


def remove_immutability_for_corruption_test(con, table):
    """Simulate offline database corruption, not an application-supported mutation."""
    triggers = con.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name=?", (table,)).fetchall()
    for name, in triggers:
        assert name.replace('_', '').isalnum()
        con.execute('DROP TRIGGER ' + name)


@pytest.mark.parametrize('target', ['source_bytes', 'stage_digest', 'stage_payload_with_rehashed_digest'])
def test_confirm_replays_original_bytes_and_rejects_corruption(tmp_path, target):
    _, repo, profile, _, files = configured(tmp_path)
    stage = repo.stage(profile['id'], files, periods=PERIODS, expected_revision=0)
    with closing(sqlite3.connect(repo.db)) as con, con:
        if target == 'source_bytes':
            remove_immutability_for_corruption_test(con, 'manufacturing_sources')
            con.execute('UPDATE manufacturing_sources SET content=? WHERE sha256=?',
                        (files['actual'][1].replace(b'2000', b'2001', 1), stage['tables']['actual']['source_sha256']))
        else:
            remove_immutability_for_corruption_test(con, 'manufacturing_stages')
            if target == 'stage_digest':
                con.execute('UPDATE manufacturing_stages SET sha256=? WHERE id=?', ('0' * 64, stage['stage_id']))
            else:
                payload = json.loads(con.execute('SELECT content FROM manufacturing_stages WHERE id=?', (stage['stage_id'],)).fetchone()[0])
                payload['tables']['actual']['rows'][0][-1] = 'CORRUPTED SOURCE CELL'
                con.execute('UPDATE manufacturing_stages SET content=?,sha256=? WHERE id=?',
                            (canonical(payload), digest(payload), stage['stage_id']))
    with pytest.raises(ValueError):
        repo.confirm(stage['stage_id'], expected_revision=0, reason='must reject corrupt proof')
    assert table_count(repo.db, 'manufacturing_revisions') == 0


def test_current_rechecks_source_bytes_after_confirmation(tmp_path):
    _, repo, profile, _, files, stage = confirmed(tmp_path)
    with closing(sqlite3.connect(repo.db)) as con, con:
        remove_immutability_for_corruption_test(con, 'manufacturing_sources')
        con.execute('UPDATE manufacturing_sources SET content=? WHERE sha256=?',
                    (b'corrupt', stage['tables']['actual']['source_sha256']))
    with pytest.raises(ValueError):
        repo.current(profile['id'])


def test_old_stage_and_history_not_leaked_to_new_profile_scope(tmp_path):
    root, repo, profile, adapter, _, stage = confirmed(tmp_path)
    changed, mapped = change_scope(profile, adapter)
    repo.install_profile(changed, mapped, expected_version=1, reason='explicit new factory scope')
    principal = Principal('new-scope', 'New scope only', ('supervisor',),
                          tuple(changed['factories'].values()), tuple(p['name'] for p in changed['products']))
    limited = ManufacturingRepository(root, principal)
    assert limited.configuration(profile['id'])['version'] == 2
    with pytest.raises(PermissionError):
        limited._stage_view(stage['stage_id'])
    # History is a filtered collection: inaccessible pinned-scope rows disappear.
    assert limited.history(profile['id']) == []
    with pytest.raises((PermissionError, ManufacturingConflict)):
        limited.current(profile['id'])


@pytest.mark.parametrize('industry', ['pharma', 'machinery', 'auto_parts', 'chemicals', 'electronics'])
def test_real_service_offline_hybrid_unavailable_is_honest_and_no_fake_k(tmp_path, industry):
    root, _, service, profile, _, _, _, result = analyze(tmp_path, industry=industry)
    assert result['retrieval_diagnostics']['degraded'] is True
    assert result['retrieval_diagnostics']['formal_model_blocked'] is True
    assert result['retrieval_diagnostics']['reason'] == 'hybrid_unavailable'
    assert result['data_classification'] == 'simulation'
    assert not result['effects']['task_created'] and not result['effects']['task_sent']
    for branch in result['analyses'].values():
        assert not branch['used_llm'] and branch['model_explanations'] is None
        assert branch['model_run']['provider_call_count'] == 0
        assert branch['sources']
        assert all(source['kind'] in {'accounting_fact', 'data_fact'} for source in branch['sources'])
        assert not any(source['id'].startswith('K') for source in branch['sources'])
        assert '盒' not in branch['narrative']['text']
        assert profile['factories']['home'] in json.dumps(branch, ensure_ascii=False)
    assert not (root / 'task_workflow.db').exists()
    frozen = service.get_run(result['analysis_run_id'])
    assert frozen['analysis_hash'] == result['analysis_hash']
    assert frozen['measurement'] == result['measurement']


def test_real_service_llm_requested_but_no_hybrid_never_calls_model(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr('attribution_runtime.run_stage', lambda *a, **k: calls.append((a, k)))
    *_, result = analyze(tmp_path, use_llm=True)
    assert not calls
    assert all(branch['generation_status'] == 'retrieval_unavailable' for branch in result['analyses'].values())


def test_preview_has_no_model_analysis_or_task_writes(tmp_path):
    root, _, profile, _, _, _ = confirmed(tmp_path)
    service = ManufacturingService(root, ANALYST)
    preview = service.preview(profile['id'], **query(profile))
    assert preview['effects'] == {'model_calls': 0, 'writes': False, 'task_created': False, 'task_sent': False}
    assert not service.db.exists() and not (root / 'task_workflow.db').exists()


def test_frozen_run_reauthorizes_original_factories_after_config_scope_change(tmp_path):
    root, repo, service, profile, adapter, _, _, result = analyze(tmp_path)
    changed, mapped = change_scope(profile, adapter)
    repo.install_profile(changed, mapped, expected_version=1, reason='replacement factory scope')
    new_scope = Principal('new-only', 'New only', ('supervisor',), tuple(changed['factories'].values()), ('*',))
    with pytest.raises(PermissionError):
        ManufacturingService(root, new_scope).get_run(result['analysis_run_id'])
    # Original scoped auditor can read the frozen original without masquerading as new scope.
    old_scope = Principal('old-auditor', 'Original auditor', ('auditor',), tuple(profile['factories'].values()), tuple(p['name'] for p in profile['products']))
    frozen = ManufacturingService(root, old_scope).get_run(result['analysis_run_id'])
    assert frozen['profile']['factories'] == profile['factories']
    assert frozen['analysis_hash'] == result['analysis_hash']
    with pytest.raises(PermissionError):
        ManufacturingService(root, old_scope).task_draft(result['analysis_run_id'], kind='attribution', element='材料')


def test_product_subset_auditor_cannot_read_full_frozen_profile_or_other_bom(tmp_path):
    root, _, _, profile, _, _, _, result = analyze(tmp_path)
    selected_only = Principal('selected-product-auditor', 'Selected product only', ('auditor',),
        tuple(profile['factories'].values()), (profile['products'][0]['name'],))
    assert result['scope']['product'] == profile['products'][0]['name']
    assert len(result['profile']['products']) == 2
    with pytest.raises(PermissionError):
        ManufacturingService(root, selected_only).get_run(result['analysis_run_id'])
    full_scope = Principal('full-profile-auditor', 'Whole profile auditor', ('auditor',),
        tuple(profile['factories'].values()), tuple(p['name'] for p in profile['products']))
    allowed = ManufacturingService(root, full_scope).get_run(result['analysis_run_id'])
    assert allowed['profile'] == result['profile']


def test_frozen_analysis_sqlite_append_only_and_corruption_detected(tmp_path):
    _, _, service, _, _, _, _, result = analyze(tmp_path)
    with closing(sqlite3.connect(service.db)) as con:
        with pytest.raises(sqlite3.DatabaseError, match='immutable|append'):
            con.execute('UPDATE runs SET payload=payload')
        con.rollback()
        with pytest.raises(sqlite3.DatabaseError, match='immutable|append'):
            con.execute('DELETE FROM runs')
        con.rollback()
        remove_immutability_for_corruption_test(con, 'runs')
        con.execute('UPDATE runs SET sha256=?', ('0' * 64,))
        con.commit()
    with pytest.raises(ValueError, match='完整性'):
        service.get_run(result['analysis_run_id'])


@pytest.mark.parametrize('kind', ['attribution', 'benchmark'])
def test_real_task_normalizer_persists_only_draft_with_frozen_evidence(tmp_path, kind):
    root, _, service, profile, _, _, _, result = analyze(tmp_path)
    draft = service.task_draft(result['analysis_run_id'], kind=kind, element='材料')
    task = draft['task']
    assert draft['sent'] is draft['approved'] is False
    assert task['workflow_status'] == 'draft' and task['dispatch_status'] == 'not_sent'
    assert task['outbox'] is None and task['approved_version'] is None
    assert task['content']['assignee'] == {'name': '', 'department': '', 'role': ''}
    assert task['content']['analysis_run_id'] == result['analysis_run_id']
    assert set(task['content']['factories']) == set(profile['factories'].values())
    assert task['content']['source']['product'] == profile['products'][0]['name']
    assert task['content']['action_plan']['actions']
    assert task['content']['analysis_period']['months'] == ['2026-06']
    assert task['content']['source']['analysis_type'] == ('专题分析' if kind == 'benchmark' else '月度成本分析')
    sources = {row['id']: row for row in result['analyses'][kind]['sources']}
    for identifier, sha in task['content']['evidence_hashes'].items():
        assert sha == digest(sources[identifier])
    with closing(sqlite3.connect(root / 'task_workflow.db')) as con:
        assert con.execute('SELECT COUNT(*) FROM outbox').fetchone()[0] == 0
        assert con.execute('SELECT COUNT(*) FROM task_receipts').fetchone()[0] == 0
        assert json.loads(con.execute('SELECT created_by FROM tasks').fetchone()[0])['user_id'] == ADMIN.user_id


def test_first_month_no_comparator_cannot_become_attribution_task(tmp_path):
    root, _, service, _, _, _, _, result = analyze(tmp_path, month='2026-05')
    assert result['facts']['period']['available'] is False
    assert not result['analyses']['attribution']['used_llm']
    with pytest.raises(ValueError):
        service.task_draft(result['analysis_run_id'], kind='attribution', element='材料')
    assert not (root / 'task_workflow.db').exists()


def test_model_test_double_bounded_calls_preserve_scope_measurements_and_disable_retry(tmp_path, monkeypatch):
    retrieval = no_knowledge_test_double(monkeypatch)
    calls = []
    def worker(stage, args, *, timeout):
        calls.append((stage, deepcopy(args), timeout))
        # Explicit rejection test double, NOT a model-validated result.
        return {'schema': 'INVALID_TEST_DOUBLE_SCHEMA', 'candidate': {'invented': True}}
    monkeypatch.setattr('attribution_runtime.run_stage', worker)
    *_, result = analyze(tmp_path, use_llm=True)
    assert len(calls) == 2
    assert len(retrieval) == 2
    assert retrieval[0]['kwargs']['require_hybrid'] is True
    assert retrieval[1]['kwargs']['purposes'] == ['market_reference']
    for stage, (payload, sources), timeout in calls:
        assert stage == 'model' and timeout == 95
        assert payload['prose_mode'] == 'bound-numeric-prose/1'
        assert payload['measurement'] == result['measurement']
        assert payload['product'] == result['scope']['product']
        assert payload['month'] == result['scope']['month']
        assert payload['data_classification'] == 'simulation'
        assert sources and not any(source['id'].startswith('K') for source in sources)
        assert '中药一厂' not in json.dumps(payload, ensure_ascii=False)
    assert all(not branch['used_llm'] and branch['model_explanations'] is None for branch in result['analyses'].values())


def test_first_month_model_spy_never_receives_unavailable_attribution(tmp_path, monkeypatch):
    no_knowledge_test_double(monkeypatch)
    calls = []
    def worker(stage, args, *, timeout):
        calls.append(deepcopy(args[0]))
        return {'schema': 'INVALID_TEST_DOUBLE_SCHEMA'}
    monkeypatch.setattr('attribution_runtime.run_stage', worker)
    *_, result = analyze(tmp_path, month='2026-05', use_llm=True)
    assert result['facts']['period']['available'] is False
    assert len(calls) == 1, 'Only benchmark may invoke model when prior-period attribution is unavailable'
    assert calls[0]['facts'].get('available') is not False


def test_no_difference_does_not_mask_zero_net_labor_factor_offsets():
    from attribution_facts import labor_factor_bridge
    factors = labor_factor_bridge(200, 200, 100, 80, 100, 100)
    payload = {'product': 'SIMULATION offset product', 'specification': 'SIM-OFFSET', 'month': '2026-06',
        'facts': {'available': True, 'amount_delta': 0,
            'previous': {'volume': 100}, 'current': {'volume': 100},
            'elements': {'人工': {'unit_before': 2, 'unit_after': 2, 'amount_delta': 0,
                'volume_effect': 0, 'unit_effect': 0, 'contribution': None, 'labor_factors': factors}}}}
    assert ManufacturingService._no_difference('attribution', payload) is False
    payload['facts']['elements']['人工'].pop('labor_factors')
    assert ManufacturingService._no_difference('attribution', payload) is True


def test_no_difference_does_not_mask_zero_net_observed_quantity_price_offsets():
    detail = {'name': 'SIMULATION observed material', 'unit_before': 1, 'unit_after': 1,
        'amount_before': '100', 'amount_after': '100', 'amount_delta': 0, 'volume_effect': 0,
        'unit_effect': 0, 'evidence_id': 'Fobserved',
        'physical_observations': {
            'previous': {'quantity': '10', 'quantity_unit': 'kg', 'unit_price': '10',
                'quantity_basis': 'source_observed', 'price_basis': 'source_observed'},
            'current': {'quantity': '20', 'quantity_unit': 'kg', 'unit_price': '5',
                'quantity_basis': 'source_observed', 'price_basis': 'source_observed'}},
        'observed_quantity_price_bridge': {'available': True, 'quantity_effect': '100',
            'price_effect': '-100', 'amount_delta': '0', 'quantity_unit': 'kg',
            'quantity_basis': 'source_observed_not_bom_or_amount_divided_by_reference_price'}}
    payload = {'product': 'SIMULATION offset product', 'specification': 'SIM-OFFSET', 'month': '2026-06',
        'facts': {'available': True, 'amount_delta': 0,
            'previous': {'volume': 100}, 'current': {'volume': 100},
            'elements': {'材料': {'unit_before': 1, 'unit_after': 1, 'amount_delta': 0,
                'volume_effect': 0, 'unit_effect': 0, 'contribution': None,
                'evidence_ids': ['Fobserved'], 'detail': [detail]}}}}
    sources = [{'id': 'Fobserved', 'kind': 'accounting_fact', 'evidence_role': 'accounting_fact',
        'support_status': 'eligible', 'elements': ['材料'], 'source': {'file': 'SIMULATION.csv', 'record_number': 1}}]
    assert ManufacturingService._no_difference('attribution', payload, sources=sources) is False
    detail.pop('physical_observations')
    detail.pop('observed_quantity_price_bridge')
    assert ManufacturingService._no_difference('attribution', payload, sources=sources) is True


def test_data_revision_change_during_retrieval_prevents_freezing_stale_analysis(tmp_path, monkeypatch):
    root, repo, profile, _, files, _ = confirmed(tmp_path)
    def bump(call):
        if call == 1:
            staged = repo.stage(profile['id'], files, periods=PERIODS, expected_revision=1)
            repo.confirm(staged['stage_id'], expected_revision=1, reason='concurrent revision TEST')
    no_knowledge_test_double(monkeypatch, hook=bump)
    service = ManufacturingService(root, ADMIN)
    with pytest.raises(ValueError, match='数据|配置'):
        service.analyze(profile['id'], **query(profile), use_llm=False)
    assert not service.db.exists()


def test_profile_change_during_retrieval_prevents_freezing_stale_analysis(tmp_path, monkeypatch):
    root, repo, profile, adapter, _, _ = confirmed(tmp_path)
    def bump(call):
        if call == 1:
            changed = deepcopy(profile)
            changed['version'] += '-concurrent'
            repo.install_profile(changed, adapter, expected_version=1, reason='concurrent config TEST')
    no_knowledge_test_double(monkeypatch, hook=bump)
    service = ManufacturingService(root, ADMIN)
    with pytest.raises(ValueError):
        service.analyze(profile['id'], **query(profile), use_llm=False)
    assert not service.db.exists()


def test_frozen_run_revalidates_real_knowledge_version_and_revocation(tmp_path, monkeypatch):
    from enterprise.analysis_service import EvidenceList
    from enterprise.knowledge import Repository
    from enterprise.knowledge_release import ReleaseRepository
    root, _, profile, _, _, _ = confirmed(tmp_path)
    principal = Principal('knowledge-integration-admin', 'Synthetic knowledge admin',
        ('system_admin', 'supervisor', 'knowledge_admin'), ('*',), ('*',))
    knowledge = Repository(root, principal=principal)
    product = profile['products'][0]
    text = 'SIMULATION controlled reference. Materials require source records; process yield is a reference, not a current event.'
    staged = knowledge.stage(text.encode('utf-8'), 'simulation-reference.txt', 'SIMULATION integration reference',
        [product['name']], '2026-01-01', 'process', principal.user_id,
        scope_factories=list(profile['factories'].values()), visibility='scoped',
        metadata={'evidence_role': 'document_basis'}, principal=principal)
    assert not staged['errors']
    version = knowledge.commit(staged['stage_id'], principal.user_id, 'SIMULATION reference reviewed', principal=principal)
    # Retrieval is explicitly a test double, but version persistence and get-run
    # authorization/revocation use the real knowledge repository.
    source = {'id': 'Ksimulation_controlled_reference', 'kind': 'document_basis',
        'evidence_role': 'document_basis', 'support_status': 'eligible', 'elements': ['材料'],
        'text': text, 'version_id': version['version_id'], 'document_id': version['doc_id'],
        'scope': {'product': product['name'], 'specification': product['specification'], 'months': PERIODS},
        'source': {'file': 'simulation-reference.txt', 'sha256': version['sha256'], 'quote': text}}
    def retrieve(*args, **kwargs):
        return EvidenceList([deepcopy(source)], {'degraded': False, 'release_id': 'TEST_DOUBLE_CONTROLLED_RELEASE'})
    monkeypatch.setattr('enterprise.analysis_service.report_evidence', retrieve)
    service = ManufacturingService(root, principal)
    result = service.analyze(profile['id'], **query(profile), use_llm=False)
    assert service.get_run(result['analysis_run_id'])['analysis_hash'] == result['analysis_hash']
    release = ReleaseRepository(repository=knowledge)
    release.revoke_document(version['doc_id'], principal=principal, reason='SIMULATION revoked for integration check')
    with pytest.raises(PermissionError):
        service.get_run(result['analysis_run_id'])
    with pytest.raises(PermissionError):
        service.task_draft(result['analysis_run_id'], kind='attribution', element='材料')
    assert not (root / 'task_workflow.db').exists()


def test_real_service_knowledge_stage_commit_binds_profile_without_publishing(tmp_path):
    from enterprise.domain_profiles import profile_fingerprint
    root, repo, profile, _, _, _ = confirmed(tmp_path)
    principal = Principal('mfg-knowledge', 'Synthetic knowledge manager',
        ('system_admin', 'supervisor', 'knowledge_admin'), ('*',), ('*',))
    service = ManufacturingService(root, principal)
    product = profile['products'][0]
    staged = service.stage_knowledge(profile['id'], content=b'SIMULATION reference only, not an actual event.',
        filename='simulation-bound.txt', title='SIMULATION profile-bound process reference',
        product_ids=[product['id']], effective_from='2026-01-01', category='process', reason='review fixture semantics')
    assert not staged['errors']
    assert staged['publication_status'] == 'not_published' and staged['model_called'] is False
    assert staged['scope_products'] == [product['name']]
    assert set(staged['scope_factories']) == set(profile['factories'].values())
    metadata = staged['business_metadata']
    assert metadata['manufacturing_profile_sha256'] == profile_fingerprint(profile)
    assert metadata['manufacturing_config_hash'] == repo.configuration(profile['id'])['config_hash']
    assert metadata['graph_vocabulary_review']['reviewed_by'] == principal.user_id
    assert metadata['data_classification'] == 'simulation'
    committed = service.commit_knowledge(profile['id'], stage_id=staged['stage_id'], reason='reviewed fixture text')
    assert committed['publication_status'] == 'catalog_only'
    assert committed['index_publication_required'] and not committed['model_called']
    assert committed['version']['scope_products'] == [product['name']]
    assert committed['version']['business_metadata']['manufacturing_profile_sha256'] == profile_fingerprint(profile)
    assert not service.db.exists() and not (root / 'task_workflow.db').exists()


def test_knowledge_commit_rejects_changed_bound_config_and_ambiguous_mechanism_scope(tmp_path):
    root, repo, profile, adapter, _, _ = confirmed(tmp_path)
    principal = Principal('mfg-knowledge-drift', 'Synthetic knowledge manager',
        ('system_admin', 'supervisor', 'knowledge_admin'), ('*',), ('*',))
    service = ManufacturingService(root, principal)
    args = {'content': b'SIMULATION profile-bound reference.', 'filename': 'simulation-drift.txt',
            'title': 'SIMULATION drift reference', 'effective_from': '2026-01-01',
            'category': 'process', 'reason': 'review fixture'}
    with pytest.raises(ValueError):
        service.stage_knowledge(profile['id'], product_ids=[p['id'] for p in profile['products']], **args)
    staged = service.stage_knowledge(profile['id'], product_ids=[profile['products'][0]['id']], **args)
    assert not staged['errors']
    changed = deepcopy(profile)
    changed['version'] += '-changed-before-knowledge-commit'
    repo.install_profile(changed, adapter, expected_version=1, reason='test changed config binding')
    with pytest.raises(ManufacturingConflict):
        service.commit_knowledge(profile['id'], stage_id=staged['stage_id'], reason='must not commit stale binding')
    assert table_count(root / 'knowledge.db', 'versions') == 0


def test_hybrid_release_switch_clears_documents_and_blocks_model(tmp_path, monkeypatch):
    from enterprise.analysis_service import EvidenceList
    queries, models = [], []
    def changed_release(*args, **kwargs):
        queries.append(1)
        return EvidenceList([], {'degraded': False, 'release_id': 'TEST_DOUBLE_RELEASE_' + str(len(queries))})
    monkeypatch.setattr('enterprise.analysis_service.report_evidence', changed_release)
    monkeypatch.setattr('attribution_runtime.run_stage', lambda *a, **kw: models.append(1))
    *_, result = analyze(tmp_path, use_llm=True)
    assert len(queries) == 2 and not models
    assert result['retrieval_diagnostics']['formal_model_blocked']
    assert all(not branch['used_llm'] for branch in result['analyses'].values())


def test_knowledge_permission_error_never_becomes_silent_fallback(tmp_path, monkeypatch):
    root, _, profile, _, _, _ = confirmed(tmp_path)
    def denied(*args, **kwargs):
        raise PermissionError('TEST_DOUBLE revoked knowledge scope')
    monkeypatch.setattr('enterprise.analysis_service.report_evidence', denied)
    service = ManufacturingService(root, ADMIN)
    with pytest.raises(PermissionError):
        service.analyze(profile['id'], **query(profile), use_llm=False)
    assert not service.db.exists()
