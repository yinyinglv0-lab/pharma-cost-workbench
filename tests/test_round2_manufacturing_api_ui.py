"""Real temporary canonical repository + HTTP boundaries; UI uses offline mocks."""
from pathlib import Path
import json

from fastapi.testclient import TestClient
import pytest
from streamlit.testing.v1 import AppTest

from enterprise.security import Principal

ROOT = Path(__file__).resolve().parents[1]
ADMIN = Principal('manufacturing-admin', 'Fixture admin', ('system_admin', 'supervisor'), ('*',), ('*',))
ANALYST = Principal('manufacturing-analyst', 'Fixture analyst', ('analyst',), ('*',), ('*',))
PERIODS = ['2026-05', '2026-06']


def example(industry='machinery'):
    folder = ROOT / 'config' / 'manufacturing_examples' / industry
    return {'profile_json': (folder / 'domain.json').read_text(encoding='utf-8-sig'),
            'adapter_json': (folder / 'adapter.json').read_text(encoding='utf-8-sig'),
            'expected_version': 0, 'reason': 'isolated synthetic fixture'}, {
        family: (family + '.csv', (folder / (family + '.csv')).read_bytes(), 'text/csv')
        for family in ('actual', 'budget', 'materials', 'labor', 'overhead')}


@pytest.fixture
def api(tmp_path, monkeypatch):
    import backend_api
    identity = {'principal': ADMIN}
    monkeypatch.setattr(backend_api, 'MANAGED_DIR', tmp_path / 'manufacturing-http')
    monkeypatch.setattr(backend_api, 'api_principal', lambda request: identity['principal'])
    # No TestClient lifespan context: no model prewarm/server/network call.
    client = TestClient(backend_api.app, raise_server_exceptions=False)
    yield client, identity, backend_api.MANAGED_DIR
    client.close()


def install(client):
    body, files = example()
    response = client.post('/api/manufacturing/profiles', json=body)
    assert response.status_code == 200, response.text
    return response.json()['profile_id'], body, files


def stage(client, profile_id, files, revision=0):
    return client.post(f'/api/manufacturing/profiles/{profile_id}/stage', files=files,
                       data={'periods_json': json.dumps(PERIODS), 'expected_revision': str(revision)})


def test_real_import_preview_and_cas_preserve_legacy_data(api, monkeypatch):
    client, _, root = api
    assert client.get('/api/manufacturing/profiles').json() == {'profiles': []}
    assert not root.exists()
    profile_id, body, files = install(client)
    listed = client.get('/api/manufacturing/profiles').json()['profiles']
    assert listed[0]['profile_id'] == profile_id and listed[0]['data_revision'] == 0
    staged = stage(client, profile_id, files)
    assert staged.status_code == 200, staged.text
    assert staged.json()['status'] == 'staged_not_active'
    identifier = staged.json()['stage_id']
    request = {'expected_revision': 0, 'reason': 'reviewed synthetic snapshot'}
    confirmed = client.post(f'/api/manufacturing/stages/{identifier}/confirm', json=request)
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()['legacy_data_changed'] is False
    replay = client.post(f'/api/manufacturing/stages/{identifier}/confirm', json=request)
    assert replay.status_code == 409
    assert client.post('/api/manufacturing/profiles', json=body).status_code == 409
    current = client.get(f'/api/manufacturing/profiles/{profile_id}/current')
    assert current.status_code == 200 and current.json()['periods'] == PERIODS
    assert 'runtime' not in current.json()
    profile = json.loads(body['profile_json'])
    item = profile['products'][0]
    scope = {'product': item['name'], 'specification': item['specification'], 'month': PERIODS[-1]}
    preview = client.post(f'/api/manufacturing/profiles/{profile_id}/preview', json=scope)
    assert preview.status_code == 200, preview.text
    assert preview.json()['effects'] == {'model_calls': 0, 'writes': False, 'task_created': False, 'task_sent': False}
    assert preview.json()['facts']['data_classification'] == 'simulation'
    assert not (root / 'costs.db').exists()
    calls = []
    def analyze(self, selected, **kwargs):
        calls.append((self.root, self.principal.user_id, selected, kwargs))
        return {'analysis_run_id': 'ma_' + 'a' * 32, 'use_llm': kwargs['use_llm']}
    monkeypatch.setattr('enterprise.manufacturing_service.ManufacturingService.analyze', analyze)
    analyzed = client.post(f'/api/manufacturing/profiles/{profile_id}/analyze', json=scope)
    assert analyzed.status_code == 200 and analyzed.json()['use_llm'] is False
    assert calls[0][0] == root and calls[0][1] == ADMIN.user_id


def test_real_frozen_analysis_read_and_actor_bound_draft_without_model(api, monkeypatch):
    client, identity, root = api
    profile_id, body, files = install(client)
    staged = stage(client, profile_id, files).json()
    response = client.post(f"/api/manufacturing/stages/{staged['stage_id']}/confirm",
        json={'expected_revision': 0, 'reason': 'complete isolated snapshot'})
    assert response.status_code == 200
    from enterprise.knowledge import KnowledgeError
    def unavailable(*args, **kwargs):
        raise KnowledgeError('Synthetic offline fixture has no published hybrid index')
    monkeypatch.setattr('enterprise.analysis_service.report_evidence', unavailable)
    monkeypatch.setattr('enterprise.manufacturing_service.ManufacturingService._model',
                        lambda *args: pytest.fail('Default deterministic analyze must not call model'))
    definition = json.loads(body['profile_json'])['products'][0]
    scope = {'product': definition['name'], 'specification': definition['specification'], 'month': PERIODS[-1]}
    frozen = client.post(f'/api/manufacturing/profiles/{profile_id}/analyze', json=scope)
    assert frozen.status_code == 200, frozen.text
    result = frozen.json()
    assert result['data_classification'] == 'simulation'
    assert all(not analysis['used_llm'] for analysis in result['analyses'].values())
    identifier = result['analysis_run_id']
    assert client.get('/api/manufacturing/runs/' + identifier).json() == result
    identity['principal'] = ANALYST
    drafted = client.post('/api/manufacturing/runs/' + identifier + '/task-draft',
        json={'kind': 'attribution', 'element': '材料'})
    assert drafted.status_code == 200, drafted.text
    assert drafted.json()['sent'] is False and drafted.json()['approved'] is False
    assert drafted.json()['analysis_hash'] == result['analysis_hash']
    # Current authorized analyst, not original run creator or client text, owns
    # the audit write. No notification/approval endpoint is called by this flow.
    task = drafted.json()['task']
    assert task.get('status') == 'draft'
    assert ANALYST.user_id in json.dumps(task, ensure_ascii=False)


@pytest.mark.parametrize('value', ['false', 'true', 0, 1, None, [], {}])
def test_analyze_rejects_coerced_model_flags(api, value):
    client, _, _ = api
    response = client.post('/api/manufacturing/profiles/synthetic/analyze', json={
        'product': 'fixture', 'specification': 'S', 'month': '2026-06', 'use_llm': value})
    assert response.status_code == 422


@pytest.mark.parametrize('field', ['profile_json', 'adapter_json'])
def test_raw_json_duplicate_and_nonfinite_rejected_before_install(api, field):
    client, _, root = api
    body, _ = example()
    body[field] = '{"schema_version":"one","schema_version":"two"}'
    response = client.post('/api/manufacturing/profiles', json=body)
    assert response.status_code == 422
    assert not (root / 'manufacturing.db').exists()
    body[field] = '{"a":NaN}'
    assert client.post('/api/manufacturing/profiles', json=body).status_code == 422


@pytest.mark.parametrize('field,value', [('expected_version', True), ('expected_version', '0'),
                                       ('profile_json', {}), ('actor', 'spoof'), ('root', 'C:/other')])
def test_install_strict_types_no_actor_or_path_override(api, field, value):
    client, _, _ = api
    body, _ = example()
    body[field] = value
    assert client.post('/api/manufacturing/profiles', json=body).status_code == 422


def test_roles_and_scope_do_not_expand_with_profile_install(api):
    client, identity, _ = api
    body, _ = example()
    identity['principal'] = ANALYST
    assert client.post('/api/manufacturing/profiles', json=body).status_code == 403
    identity['principal'] = Principal('limited', 'Limited', ('system_admin', 'supervisor'), ('unrelated',), ('*',))
    assert client.post('/api/manufacturing/profiles', json=body).status_code == 403
    identity['principal'] = ADMIN
    profile_id, _, files = install(client)
    staged = stage(client, profile_id, files)
    assert staged.status_code == 200
    identity['principal'] = ANALYST
    assert client.post(f"/api/manufacturing/stages/{staged.json()['stage_id']}/confirm",
                       json={'expected_revision': 0, 'reason': 'denied'}).status_code == 403
    identity['principal'] = Principal('limited', 'Limited', ('analyst',), ('unrelated',), ('*',))
    assert client.get('/api/manufacturing/profiles').json() == {'profiles': []}
    assert stage(client, profile_id, files).status_code == 403


@pytest.mark.parametrize('revision', ['true', '-1', '1.0', '00'])
def test_stage_rejects_invalid_revision_and_missing_table(api, revision):
    client, _, _ = api
    profile_id, _, files = install(client)
    response = client.post(f'/api/manufacturing/profiles/{profile_id}/stage', files=files,
        data={'periods_json': json.dumps(PERIODS), 'expected_revision': revision})
    assert response.status_code == 422
    files.pop('labor')
    assert stage(client, profile_id, files).status_code == 422


def test_task_endpoint_binds_authenticated_actor_and_has_no_send_route(api, monkeypatch):
    client, identity, root = api
    seen = []
    def draft(self, run, **kwargs):
        seen.append((self.root, self.principal.user_id, run, kwargs))
        return {'approved': False, 'sent': False}
    monkeypatch.setattr('enterprise.manufacturing_service.ManufacturingService.task_draft', draft)
    identity['principal'] = ANALYST
    url = '/api/manufacturing/runs/ma_' + 'b' * 32
    response = client.post(url + '/task-draft', json={'kind': 'benchmark', 'element': '材料'})
    assert response.status_code == 200 and response.json() == {'approved': False, 'sent': False}
    assert seen == [(root, ANALYST.user_id, 'ma_' + 'b' * 32, {'kind': 'benchmark', 'element': '材料'})]
    assert client.post(url + '/task-draft', json={'kind': 'benchmark', 'element': '材料', 'actor': 'spoof'}).status_code == 422
    assert client.post(url + '/send', json={}).status_code == 404


def test_knowledge_stage_is_scoped_pending_and_rejects_duplicates(api):
    client, identity, root = api
    profile_id, body, _ = install(client)
    profile = json.loads(body['profile_json'])
    product_id = profile['products'][0]['id']
    form = {'title': '合成机械工艺', 'product_ids_json': json.dumps([product_id]),
            'category': 'process', 'effective_from': '2026-01-01', 'reason': 'synthetic scope review'}
    file = {'file': ('synthetic-process.txt', '合成工艺仅供隔离测试，机加工后实施质量检查。'.encode(), 'text/plain')}
    url = f'/api/manufacturing/profiles/{profile_id}/knowledge/stage'
    identity['principal'] = ANALYST
    assert client.post(url, data=form, files=file).status_code == 403
    identity['principal'] = Principal('knowledge-fixture', 'Knowledge fixture',
                                     ('system_admin', 'supervisor', 'knowledge_admin'), ('*',), ('*',))
    pending = client.post(url, data=form, files=file)
    assert pending.status_code == 200, pending.text
    assert pending.json()['publication_status'] == 'not_published'
    assert pending.json()['model_called'] is False
    assert pending.json()['manufacturing_profile_id'] == profile_id
    from enterprise.knowledge import Repository
    assert Repository(root, principal=identity['principal']).list_documents() == []
    duplicate = {**form, 'product_ids_json': json.dumps([product_id, product_id])}
    assert client.post(url, data=duplicate, files=file).status_code == 422
    multiple = {**form, 'product_ids_json': json.dumps([item['id'] for item in profile['products']])}
    assert client.post(url, data=multiple, files=file).status_code == 422
    assert client.post(url, data={**form, 'category': 'made_up'}, files=file).status_code == 422
    assert not pending.json().get('errors'), pending.text
    committed = client.post(f'/api/manufacturing/profiles/{profile_id}/knowledge/confirm',
        json={'stage_id': pending.json()['stage_id'], 'reason': 'reviewed original synthetic process'})
    assert committed.status_code == 200, committed.text
    assert committed.json()['publication_status'] == 'catalog_only'
    assert committed.json()['index_publication_required'] is True
    assert committed.json()['model_called'] is False
    assert client.post(url, data={**form, 'actor': 'spoof'}, files=file).status_code == 422
    assert client.post(url, data={**form, 'root': 'C:/other'}, files=file).status_code == 422
    # A later declarative configuration invalidates an uncommitted preview.
    later_pending = client.post(url, data={**form, 'title': '另一合成资料'},
        files={'file': ('other-process.txt', '仅供测试，机加工后需要质量检查。'.encode(), 'text/plain')})
    assert later_pending.status_code == 200 and not later_pending.json().get('errors')
    changed = dict(body, expected_version=1, reason='synthetic config revision')
    new_profile = json.loads(changed['profile_json'])
    new_profile['version'] += '-reviewed'
    changed['profile_json'] = json.dumps(new_profile)
    assert client.post('/api/manufacturing/profiles', json=changed).status_code == 200
    stale = client.post(f'/api/manufacturing/profiles/{profile_id}/knowledge/confirm',
        json={'stage_id': later_pending.json()['stage_id'], 'reason': 'must refuse stale config'})
    assert stale.status_code == 409


@pytest.mark.parametrize('format,mime', [('docx', 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'),
                                        ('pdf', 'application/pdf')])
def test_export_route_uses_authenticated_service_and_private_response(api, monkeypatch, format, mime):
    client, identity, root = api
    calls = []
    identity['principal'] = ANALYST
    def export(self, identifier, selected_format):
        calls.append((self.root, self.principal, identifier, selected_format))
        return b'frozen-test-bytes'
    monkeypatch.setattr('enterprise.manufacturing_service.ManufacturingService.export_report', export)
    identifier = 'ma_' + 'a' * 32
    response = client.get(f'/api/manufacturing/runs/{identifier}/export/{format}')
    assert response.status_code == 200
    assert response.content == b'frozen-test-bytes'
    assert response.headers['content-type'] == mime
    assert response.headers['cache-control'] == 'no-store'
    assert response.headers['content-disposition'] == f'attachment; filename="{identifier}.{format}"'
    assert calls == [(root, ANALYST, identifier, format)]
    assert client.get(f'/api/manufacturing/runs/{identifier}/export/html').status_code == 422


def test_export_route_retains_denial_and_controls_font_failure(api, monkeypatch):
    client, _, _ = api
    def denied(*args):
        raise PermissionError('denied frozen scope')
    monkeypatch.setattr('enterprise.manufacturing_service.ManufacturingService.export_report', denied)
    url = '/api/manufacturing/runs/ma_' + 'a' * 32 + '/export/pdf'
    assert client.get(url).status_code == 403
    def missing_font(*args):
        raise RuntimeError('secret internal font path must not leak')
    monkeypatch.setattr('enterprise.manufacturing_service.ManufacturingService.export_report', missing_font)
    response = client.get(url)
    assert response.status_code == 503
    assert 'secret' not in response.text


def test_export_service_checks_authorization_before_and_after_render(tmp_path, monkeypatch):
    from enterprise.manufacturing_service import ManufacturingService
    calls = []
    service = ManufacturingService(tmp_path, ANALYST)
    frozen = {'frozen': 'snapshot'}
    def get(identifier):
        calls.append(('authorize', identifier))
        return frozen
    def render(value, format):
        assert value is frozen
        calls.append(('render', format))
        return b'bytes'
    monkeypatch.setattr(service, 'get_run', get)
    monkeypatch.setattr('enterprise.manufacturing_report.export_manufacturing_report', render)
    assert service.export_report('ma_test', 'pdf') == b'bytes'
    assert calls == [('authorize', 'ma_test'), ('render', 'pdf'), ('authorize', 'ma_test')]
    calls.clear()
    def revocable(identifier):
        calls.append(('authorize', identifier))
        if len(calls) > 1:
            raise PermissionError('revoked during render')
        return frozen
    monkeypatch.setattr(service, 'get_run', revocable)
    with pytest.raises(PermissionError, match='revoked'):
        service.export_report('ma_test', 'pdf')
    assert calls[-1] == ('authorize', 'ma_test')


def _ui_entry(page_path):
    import streamlit as st
    from pathlib import Path
    knowledge = str(Path(page_path).with_name('knowledge.py'))
    page = st.navigation([st.Page(page_path, title='制造业配置与分析', default=True),
                          st.Page(knowledge, title='知识文档库')])
    page.run()


def test_ui_import_has_no_automatic_preview_generation_or_write(tmp_path, monkeypatch):
    from enterprise.application import Application
    import app_pages._shared as shared
    body, _ = example()
    profile = json.loads(body['profile_json'])
    calls = []
    class Repo:
        def __init__(self, root, principal):
            calls.append(('repo', principal.user_id))
        def profiles(self):
            calls.append(('profiles',))
            return [{'profile_id': profile['id'], 'profile': profile, 'version': 1,
                     'config_hash': 'a' * 64, 'data_revision': 1}]
        def current(self, identifier):
            calls.append(('current', identifier))
            return {'profile_id': identifier, 'profile': profile, 'periods': PERIODS,
                    'sha256': 'b' * 64, 'config_hash': 'a' * 64, 'revision': 1}
    class Service:
        def __init__(self, root, principal):
            calls.append(('service', principal.user_id))
        def analyze(self, identifier, **scope):
            raise AssertionError('No model or persisted analysis without explicit click')
        def preview(self, identifier, **scope):
            raise AssertionError('No expensive preview without explicit click')
    monkeypatch.setattr(shared, 'page_context', lambda action: (ANALYST, Application(ANALYST, root=tmp_path)))
    monkeypatch.setattr('enterprise.manufacturing_repository.ManufacturingRepository', Repo)
    monkeypatch.setattr('enterprise.manufacturing_service.ManufacturingService', Service)
    # Exercise the page from a real navigation entrypoint, not as a promoted
    # standalone page; only the external identity/service boundaries are mocked.
    app = AppTest.from_function(_ui_entry, args=(str(ROOT / 'app_pages/manufacturing.py'),), default_timeout=20).run()
    assert not app.exception
    assert not any(row[0] == 'current' for row in calls)
    assert not tmp_path.joinpath('manufacturing.db').exists()
    app.button(key='mfg_load_snapshot').click().run()
    assert not app.exception
    assert app.checkbox(key='mfg_use_llm').value is False
    assert [row[0] for row in calls].count('current') == 1
    assert any(item.value == '制造业配置与分析' for item in app.title)
    assert not app.button(key='mfg_analyze').disabled
    app.checkbox(key='mfg_use_llm').check().run()
    assert not app.exception and app.checkbox(key='mfg_use_llm').value is True
    app.selectbox(key='mfg_month').select(PERIODS[0]).run()
    assert not app.exception
    assert app.checkbox(key='mfg_use_llm').value is False  # No carried paid consent.
