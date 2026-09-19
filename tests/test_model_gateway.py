import json
from types import SimpleNamespace
import pytest

from enterprise.model_gateway import (ModelConfiguration, ModelUnavailable, configuration,
    generate_json, save_local_configuration)
from enterprise.security import Principal


def admin():
    return Principal('settings-admin', '管理员', ('system_admin',), ('*',), ('*',))


def test_config_secret_is_never_returned_and_external_consent_required(tmp_path, monkeypatch):
    path = tmp_path / '.local' / 'model.json'
    monkeypatch.setenv('COST_LLM_CONFIG_FILE', str(path))
    with pytest.raises(ValueError):
        save_local_configuration('https://provider.invalid/v1', 'test', 'secret', False, principal=admin())
    assert not path.exists()
    result = save_local_configuration('https://provider.invalid/v1', 'test', 'super-secret', True, principal=admin())
    assert result == {'configured': True, 'model': 'test'}
    assert configuration().api_key == 'super-secret'
    assert 'super-secret' not in json.dumps(configuration().public())
    assert not list(path.parent.glob('.model-*'))


@pytest.mark.parametrize('address', ['https://username:secret@provider.invalid/v1',
    'https://provider.invalid/v1?key=secret', 'http://provider.invalid/v1', 'file:///etc/passwd'])
def test_invalid_endpoint_is_rejected_before_writing(tmp_path, monkeypatch, address):
    monkeypatch.setenv('COST_LLM_CONFIG_FILE', str(tmp_path / 'config.json'))
    with pytest.raises(ValueError):
        save_local_configuration(address, 'm', 'key', True, principal=admin())
    assert not (tmp_path / 'config.json').exists()


def test_analyst_cannot_replace_model_credentials(tmp_path):
    actor = Principal('analyst', '财务', ('analyst',), ('*',), ('*',))
    with pytest.raises(PermissionError):
        save_local_configuration('https://provider.invalid', 'm', 'key', True, principal=actor)


class FakeOpenAI:
    text = '{}'
    error = None
    calls = []
    def __init__(self, **kwargs):
        self.chat = SimpleNamespace(completions=self)
    def __enter__(self): return self
    def __exit__(self, *args): return False
    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error: raise self.error
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=self.text))])


@pytest.mark.parametrize('value', ['{"ok":true,"ok":false}', '{"x":NaN}', '{"x":Infinity}', '[]', 'broken'])
def test_model_json_duplicates_nonfinite_and_wrong_shape_fail_closed(monkeypatch, value):
    import openai
    monkeypatch.setattr(openai, 'OpenAI', FakeOpenAI)
    monkeypatch.setattr(FakeOpenAI, 'text', value)
    cfg = ModelConfiguration('https://provider.invalid/v1', 'model', 'secret', True)
    with pytest.raises(ModelUnavailable):
        generate_json('contract', {}, config=cfg)


@pytest.mark.parametrize('content', [None, '', '   '])
def test_empty_provider_message_is_not_a_repairable_json_object(monkeypatch, content):
    import openai
    monkeypatch.setattr(openai, 'OpenAI', FakeOpenAI)
    monkeypatch.setattr(FakeOpenAI, 'text', content)
    monkeypatch.setattr(FakeOpenAI, 'calls', [])
    cfg = ModelConfiguration('https://provider.invalid/v1', 'model', 'secret', True)
    with pytest.raises(ModelUnavailable, match='ValueError'):
        generate_json('contract', {}, config=cfg)
    assert len(FakeOpenAI.calls) == 1
    monkeypatch.setattr(FakeOpenAI, 'text', '{}')
    assert generate_json('contract', {}, config=cfg) == {}
    assert len(FakeOpenAI.calls) == 2


def test_validated_call_preserves_json_and_caps_output(monkeypatch):
    import openai
    monkeypatch.setattr(openai, 'OpenAI', FakeOpenAI)
    monkeypatch.setattr(FakeOpenAI, 'text', '{"ok":true}')
    monkeypatch.setattr(FakeOpenAI, 'calls', [])
    cfg = ModelConfiguration('http://127.0.0.1:12345/v1', 'model', 'test', False)
    assert generate_json('contract', {'input': '资料'}, config=cfg, max_tokens=100000) == {'ok': True}
    assert FakeOpenAI.calls[0]['max_tokens'] == 5000
    assert FakeOpenAI.calls[0]['messages'][0]['content'] == 'contract'


def test_provider_error_does_not_expose_key_prompt_or_url(monkeypatch):
    import openai
    monkeypatch.setattr(openai, 'OpenAI', FakeOpenAI)
    monkeypatch.setattr(FakeOpenAI, 'error', RuntimeError('secret-private-prompt https://secret.invalid'))
    cfg = ModelConfiguration('https://provider.invalid/v1', 'model', 'secret-private-prompt', True)
    with pytest.raises(ModelUnavailable) as exc:
        generate_json('private input', {}, config=cfg)
    assert str(exc.value) == '模型调用未完成：RuntimeError'


@pytest.mark.parametrize('value', ['0', '-1', 'nan', 'inf', '121'])
def test_timeout_configuration_fails_closed(monkeypatch, value):
    monkeypatch.setenv('COST_LLM_TIMEOUT', value)
    with pytest.raises(ModelUnavailable): configuration()
