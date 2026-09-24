"""Non-secret deployment identity is distinct from application Python identity."""
import pytest

from enterprise import build_info


@pytest.mark.parametrize('name', build_info.DEPLOYMENT_FILES + build_info.DEPLOYMENT_SCRIPTS)
def test_each_reviewed_deployment_input_changes_identity(tmp_path, monkeypatch, name):
    monkeypatch.setattr(build_info, 'BASE_DIR', tmp_path)
    source = build_info.source_fingerprint()
    initial = build_info.deployment_fingerprint()
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b'example-one')
    first = build_info.deployment_fingerprint()
    path.write_bytes(b'example-two')
    second = build_info.deployment_fingerprint()
    assert len({initial, first, second}) == 3
    assert build_info.source_fingerprint() == source


def test_deployment_identity_never_reads_secret_or_managed_state(tmp_path, monkeypatch):
    monkeypatch.setattr(build_info, 'BASE_DIR', tmp_path)
    initial = build_info.deployment_fingerprint()
    for name in ('.env', '.streamlit/secrets.toml', '.local/llm.json', 'managed/tasks.db',
                 'deploy/authorization.json', 'weights/model.bin', 'assets/random.txt'):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('PRIVATE DATA SHOULD NOT PARTICIPATE', encoding='utf-8')
    assert build_info.deployment_fingerprint() == initial


def test_python_change_affects_both_identities(tmp_path, monkeypatch):
    monkeypatch.setattr(build_info, 'BASE_DIR', tmp_path)
    before = build_info.source_fingerprint(), build_info.deployment_fingerprint()
    (tmp_path / 'enterprise_app.py').write_text('version = 3\n', encoding='utf-8')
    after = build_info.source_fingerprint(), build_info.deployment_fingerprint()
    assert before[0] != after[0] and before[1] != after[1]


def test_health_exposes_both_fingerprints():
    import backend_api
    value = backend_api.health()
    assert value['deployment_fingerprint_schema'] == 'deployment-files/1.0'
    assert value['deployment_fingerprint'] == backend_api._DEPLOYMENT_FINGERPRINT
    assert len(value['source_fingerprint']) == len(value['deployment_fingerprint']) == 64
