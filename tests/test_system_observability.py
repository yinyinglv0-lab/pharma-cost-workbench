"""Operational information must use server identity and avoid credential disclosure."""
import json
from fastapi.testclient import TestClient

import backend_api
from enterprise.security import Principal


def test_status_requires_system_permission_and_returns_sanitized_metrics(monkeypatch, tmp_path):
    identity = [Principal('analyst', 'a', ('analyst',), ('*',), ('*',))]
    monkeypatch.setattr(backend_api, 'api_principal', lambda request: identity[0])
    from enterprise import model_gateway
    config_path = tmp_path / 'secret-model.json'
    config_path.write_text(json.dumps({'api_key': 'private-value-must-not-leak', 'model': 'demo',
                                      'base_url': 'http://127.0.0.1:9000/v1'}), encoding='utf-8')
    monkeypatch.setenv('COST_LLM_CONFIG_FILE', str(config_path))
    with TestClient(backend_api.app, base_url='http://127.0.0.1', client=('127.0.0.1', 51300)) as client:
        assert client.get('/api/system/status').status_code == 403
        identity[0] = Principal('ops', 'o', ('system_admin',), (), ())
        response = client.get('/api/system/status')
        assert response.status_code == 200
        assert response.json()['model']['configured'] is True
        assert 'private-value' not in response.text
        assert 'api_key' not in response.text
        assert response.json()['http_status_counts']['403'] >= 1
        assert 'outbox_status_counts' in response.json()['operations']
        assert response.headers['cache-control'] == 'no-store'
        health = client.get('/api/health').json()
        assert len(health['source_fingerprint']) == 64
        assert health['source_fingerprint'] == backend_api._BUILD_FINGERPRINT
        identity[0] = Principal('auditor', 'r', ('auditor',), ('*',), ('*',))
        summary = client.get('/api/tasks/summary')
        assert summary.status_code == 200
        assert summary.json()['generated'] == 0
        assert summary.json()['simulated'] is True


def test_legacy_admin_entry_uses_governed_navigation():
    from pathlib import Path
    from streamlit.testing.v1 import AppTest
    entry = Path(__file__).resolve().parents[1] / 'admin_web.py'
    app = AppTest.from_file(str(entry), default_timeout=45).run()
    assert not app.exception
    assert app.title[0].value == '成本分析工作台'
    assert app.session_state['enterprise_mode'] is True
    assert not any(button.label == '上传并计算' for button in app.button)


def test_legacy_admin_entry_denies_remote_local_identity(monkeypatch):
    from pathlib import Path
    from streamlit.testing.v1 import AppTest
    import streamlit as st
    monkeypatch.setattr(type(st.context), 'ip_address', property(lambda self: '192.0.2.20'))
    app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / 'admin_web.py'), default_timeout=45).run()
    assert not app.exception
    assert app.error and '拒绝非本机访问' in app.error[0].value
    assert not app.get('file_uploader')
