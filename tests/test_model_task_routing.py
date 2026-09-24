"""Offline MM-01--06 gateway contracts; no actual transport, models or credentials."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import asyncio
import json
import subprocess
import sys
from types import SimpleNamespace

import pytest

from enterprise import model_gateway as gateway

SECRET = 'offline-fixture-secret-never-log'


@pytest.fixture(autouse=True)
def isolated_gateway(monkeypatch, tmp_path):
    for name in ('COST_LLM_BASE_URL', 'DASHSCOPE_BASE_URL', 'COST_LLM_MODEL',
                 'COST_LLM_API_KEY', 'DASHSCOPE_API_KEY', 'COST_LLM_TIMEOUT',
                 'COST_LLM_APPROVED_CLOUD'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv('COST_LLM_CONFIG_FILE', str(tmp_path / 'model-fixture.json'))
    import httpx
    def forbidden(*args, **kwargs):
        pytest.fail('Tests must never use a real HTTP transport')
    monkeypatch.setattr(httpx.HTTPTransport, 'handle_request', forbidden)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, 'handle_async_request', forbidden)


def configured(tmp_path, monkeypatch, routing=None, **extra):
    value = {'base_url': 'http://127.0.0.1:12345/v1', 'model': 'legacy-default',
             'api_key': SECRET, 'approved_cloud': False}
    value.update(extra)
    if routing is not None:
        value['task_routing'] = routing
    path = tmp_path / 'model-fixture.json'
    path.write_text(json.dumps(value), encoding='utf-8')
    monkeypatch.setenv('COST_LLM_CONFIG_FILE', str(path))
    return value


def routes():
    return {'enabled': True,
            'profiles': {'fast': {'model': 'fixture-small', 'timeout': 17},
                         'narrative': {'model': 'fixture-report'}},
            'tasks': {'attribution': 'fast', 'benchmark': 'fast', 'report': 'narrative',
                      'task': 'fast', 'agent_proposal': 'fast', 'summary': 'fast'}}


@pytest.fixture
def fake_provider(monkeypatch):
    import openai
    class FakeOpenAI:
        calls = []
        initializations = []
        response_model = 'actual-snapshot-v20260922'
        response_id = 'chatcmpl-fixture'
        request_id = 'req-fixture'
        usage = {'prompt_tokens': 12, 'completion_tokens': 5, 'total_tokens': 17,
                 'prompt_tokens_details': {'cached_tokens': 3}}
        content = '{"answer":"fixture"}'
        error = None
        def __init__(self, **kwargs):
            self.initializations.append(kwargs)
            self.chat = SimpleNamespace(completions=self)
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def create(self, **kwargs):
            self.calls.append(kwargs)
            if self.error:
                raise self.error
            return SimpleNamespace(model=self.response_model, id=self.response_id,
                                   _request_id=self.request_id, usage=self.usage,
                                   choices=[SimpleNamespace(message=SimpleNamespace(content=self.content))])
    monkeypatch.setattr(openai, 'OpenAI', FakeOpenAI)
    return FakeOpenAI


def test_five_positional_arguments_and_safe_legacy_public_fields():
    config = gateway.ModelConfiguration('http://localhost:8000/v1', 'old-model', SECRET, False, 35)
    assert config.timeout == 35 and config.task is None
    assert config.public()['base_url'] == 'http://localhost:8000/v1'
    assert config.public()['model'] == 'old-model'
    assert SECRET not in repr(config)
    assert SECRET not in json.dumps(config.public())
    assert replace(config, timeout=10).api_key == SECRET


@pytest.mark.parametrize('task', [None, *sorted(gateway.TASKS)])
def test_absent_routing_keeps_existing_single_model(tmp_path, monkeypatch, task):
    configured(tmp_path, monkeypatch)
    config = gateway.configuration(task)
    assert config.model == 'legacy-default'
    assert config.task == task
    assert not config.routing_enabled and not config.routing_fallback


def test_disabled_profiles_never_activate_more_expensive_model(tmp_path, monkeypatch):
    routing = routes()
    routing['enabled'] = False
    configured(tmp_path, monkeypatch, routing)
    assert gateway.configuration('report').model == 'legacy-default'


def test_existing_environment_precedence_and_legacy_provenance(tmp_path, monkeypatch):
    configured(tmp_path, monkeypatch)
    monkeypatch.setenv('COST_LLM_MODEL', 'environment-model')
    monkeypatch.setenv('COST_LLM_TIMEOUT', '23')
    result = gateway.provenance('legacy positional instruction')
    assert result['model'] == result['requested_model'] == 'environment-model'
    assert result['source'] == 'configured_only'
    assert not result['response_observed'] and not result['request_attempted']
    assert result['provider_endpoint'] == 'http://127.0.0.1:12345/v1'
    assert gateway.configuration().timeout == 23
    assert 'legacy positional instruction' not in json.dumps(result)


@pytest.mark.parametrize('task', sorted(gateway.TASKS))
def test_only_server_whitelisted_profiles_route(tmp_path, monkeypatch, task):
    configured(tmp_path, monkeypatch, routes())
    config = gateway.configuration(task)
    assert config.model == ('fixture-report' if task == 'report' else 'fixture-small')
    assert config.base_url == 'http://127.0.0.1:12345/v1'
    assert config.api_key == SECRET and config.approved_cloud is False
    assert config.routing_reason == 'task_profile' and not config.routing_fallback
    if task != 'report':
        assert config.timeout == 17


@pytest.mark.parametrize('mapping,reason,fallback', [({}, 'task_unmapped', True),
    ({'report': 'nonexistent'}, 'profile_missing', True),
    ({'report': 'default'}, 'default_profile', False)])
def test_route_fallback_is_explicit_and_stays_default(tmp_path, monkeypatch, mapping, reason, fallback):
    configured(tmp_path, monkeypatch, {'enabled': True, 'profiles': {}, 'tasks': mapping})
    config = gateway.configuration('report')
    assert config.model == 'legacy-default'
    assert config.routing_reason == reason and config.routing_fallback is fallback


@pytest.mark.parametrize('field,value', [('base_url', 'https://attacker.invalid/v1'),
    ('api_key', 'evil-secret'), ('approved_cloud', True), ('endpoint', 'https://evil.invalid')])
def test_profiles_cannot_override_endpoint_credentials_or_consent(tmp_path, monkeypatch, field, value):
    routing = routes()
    routing['profiles']['fast'][field] = value
    configured(tmp_path, monkeypatch, routing)
    with pytest.raises(gateway.ModelUnavailable):
        gateway.configuration('attribution')


@pytest.mark.parametrize('task', ['unknown', 'https://attacker.invalid', {'model': 'override'}, [], 1])
def test_request_task_is_only_fixed_enum(task):
    with pytest.raises(gateway.ModelUnavailable, match='白名单'):
        gateway.configuration(task)


@pytest.mark.parametrize('routing', [{'enabled': 'true'}, {'enabled': True, 'profiles': []},
    {'enabled': True, 'tasks': {'arbitrary': 'default'}},
    {'enabled': True, 'profiles': {'fast': {'model': 'm', 'timeout': 'bad'}}},
    {'enabled': True, 'profiles': {'fast': {'model': 'm', 'timeout': float('nan')}}},
    {'enabled': True, 'profiles': {'fast': {'model': 'm', 'timeout': True}}}])
def test_malformed_server_policy_fails_closed(tmp_path, monkeypatch, routing):
    configured(tmp_path, monkeypatch, routing)
    with pytest.raises(gateway.ModelUnavailable):
        gateway.configuration('report')


def test_actual_provider_identity_is_not_requested_alias(tmp_path, monkeypatch, fake_provider):
    configured(tmp_path, monkeypatch, routes())
    with gateway.capture_model_calls() as calls:
        result = gateway.generate_json('private business instruction', {'private': 'business data'}, task='report')
    assert type(result) is dict and set(result) == {'answer'}
    assert fake_provider.calls[0]['model'] == 'fixture-report'
    assert fake_provider.initializations[0]['max_retries'] == 0
    trace = calls[0]
    assert trace['model'] == 'actual-snapshot-v20260922'
    assert trace['requested_model'] == 'fixture-report'
    assert trace['model'] != trace['requested_model']
    assert trace['request_id'] == 'req-fixture'
    assert trace['request_id_source'] == 'http_request_id'
    assert trace['completion_id'] == 'chatcmpl-fixture'
    assert trace['usage']['total_tokens'] == 17
    assert trace['task'] == 'report' and trace['duration_seconds'] >= 0
    assert trace['response_observed'] and trace['request_attempted']
    assert trace['status'] == 'returned_json'
    assert trace['source'] == 'provider_response'
    assert json.loads(json.dumps(trace, allow_nan=False)) == trace
    serialized = json.dumps(trace)
    for secret in (SECRET, 'private business instruction', 'business data', '127.0.0.1', 'messages'):
        assert secret not in serialized
    monkeypatch.setattr(gateway, 'configuration', lambda *a, **k: pytest.fail('Do not reread configuration'))
    observed = gateway.provenance(trace=trace, task='report')
    assert observed == trace
    observed['usage']['total_tokens'] = 999
    assert calls[0]['usage']['total_tokens'] == 17


def test_missing_provider_fields_are_not_fabricated(tmp_path, monkeypatch, fake_provider):
    configured(tmp_path, monkeypatch)
    fake_provider.response_model = fake_provider.response_id = fake_provider.request_id = fake_provider.usage = None
    with gateway.capture_model_calls() as calls:
        gateway.generate_json('contract', {}, task='attribution')
    trace = calls[0]
    assert trace['model'] is None and trace['usage'] is None and trace['request_id'] is None
    assert trace['requested_model'] == 'legacy-default' and trace['response_observed']


def test_completion_id_is_labelled_fallback_not_http_request_id(tmp_path, monkeypatch, fake_provider):
    configured(tmp_path, monkeypatch)
    fake_provider.request_id = None
    with gateway.capture_model_calls() as calls:
        gateway.generate_json('contract', {})
    assert calls[0]['request_id'] == 'chatcmpl-fixture'
    assert calls[0]['request_id_source'] == 'response_id'


def test_usage_and_provider_metadata_cannot_echo_secret_or_prompt(tmp_path, monkeypatch, fake_provider):
    configured(tmp_path, monkeypatch)
    fake_provider.usage = {'total_tokens': 10, 'api_key': SECRET, 'prompt': 'complete business prompt',
                           'completion_tokens': True, 'input_tokens': 'not-an-integer',
                           'completion_tokens_details': {'reasoning_tokens': 2, 'text': SECRET}}
    fake_provider.response_model = 'Bearer ' + SECRET
    fake_provider.request_id = SECRET
    fake_provider.response_id = 'https://secret.invalid/?key=' + SECRET
    with gateway.capture_model_calls() as calls:
        gateway.generate_json('complete business prompt', {}, task='task')
    trace = calls[0]
    assert trace['usage'] == {'total_tokens': 10, 'completion_tokens_details': {'reasoning_tokens': 2}}
    assert trace['model'] is None and trace['request_id'] is None
    assert SECRET not in json.dumps(trace) and 'complete business prompt' not in json.dumps(trace)


@pytest.mark.parametrize('bad', ['{"a":1,"a":2}', '{"a":NaN}', '[]', '', None])
def test_invalid_business_json_retains_observed_identity(tmp_path, monkeypatch, fake_provider, bad):
    configured(tmp_path, monkeypatch)
    fake_provider.content = bad
    with gateway.capture_model_calls() as calls, pytest.raises(gateway.ModelUnavailable) as exc:
        gateway.generate_json('contract', {}, task='benchmark')
    assert len(fake_provider.calls) == 1
    assert calls[0]['status'] == 'invalid_response'
    assert calls[0]['model'] == 'actual-snapshot-v20260922'
    assert exc.value.metadata == calls[0]


def test_provider_failure_is_not_route_fallback_and_does_not_retry(tmp_path, monkeypatch, fake_provider):
    configured(tmp_path, monkeypatch, routes())
    error = RuntimeError(SECRET + ' full-business-prompt https://private.invalid')
    error.request_id = 'req-error'
    fake_provider.error = error
    with gateway.capture_model_calls() as calls, pytest.raises(gateway.ModelUnavailable) as exc:
        gateway.generate_json('contract', {}, task='report')
    assert len(fake_provider.calls) == 1 and len(calls) == 1
    assert calls[0]['request_attempted'] and not calls[0]['response_observed']
    assert not calls[0]['routing_fallback']
    assert calls[0]['request_id_source'] == 'http_error_request_id'
    assert calls[0]['model'] is None and calls[0]['status'] == 'unavailable'
    assert str(exc.value) == '模型调用未完成：RuntimeError'
    assert SECRET not in str(exc.value) + json.dumps(calls)


def test_disabled_cloud_and_bad_request_config_never_call_provider(tmp_path, monkeypatch, fake_provider):
    configured(tmp_path, monkeypatch, routes(), base_url='https://provider.invalid/v1')
    with gateway.capture_model_calls() as calls, pytest.raises(gateway.ModelUnavailable):
        gateway.generate_json('contract', {}, task='report')
    assert not fake_provider.calls and not calls[0]['request_attempted']
    with gateway.capture_model_calls() as calls, pytest.raises(gateway.ModelUnavailable):
        gateway.generate_json('contract', {}, config={'base_url': 'https://attacker.invalid', 'api_key': SECRET})
    assert not fake_provider.calls and SECRET not in json.dumps(calls)


def test_explicit_config_task_guard_and_dataclass_replace(tmp_path, monkeypatch, fake_provider):
    configured(tmp_path, monkeypatch, routes())
    config = gateway.configuration('report')
    with pytest.raises(gateway.ModelUnavailable, match='不一致'):
        gateway.generate_json('contract', {}, config=config, task='task')
    with pytest.raises(gateway.ModelUnavailable, match='不一致'):
        gateway.generate_json('contract', {}, config=gateway.configuration(), task='report')
    assert fake_provider.calls == []
    with gateway.capture_model_calls() as calls:
        gateway.generate_json('contract', {}, config=replace(config, timeout=9), task='report')
    assert calls[0]['task'] == 'report' and calls[0]['requested_model'] == 'fixture-report'
    assert fake_provider.initializations[0]['timeout'] == 9


def test_fallback_reason_reaches_actual_call_trace(tmp_path, monkeypatch, fake_provider):
    configured(tmp_path, monkeypatch, {'enabled': True, 'tasks': {}, 'profiles': {}})
    with gateway.capture_model_calls() as calls:
        gateway.generate_json('contract', {}, task='report')
    assert calls[0]['routing_fallback'] and calls[0]['routing_reason'] == 'task_unmapped'
    assert calls[0]['requested_model'] == 'legacy-default'


def test_nested_and_sequential_captures_do_not_reuse_stale_response(tmp_path, monkeypatch, fake_provider):
    configured(tmp_path, monkeypatch)
    with gateway.capture_model_calls() as outer:
        with gateway.capture_model_calls() as inner:
            gateway.generate_json('first', {}, task='report')
        gateway.generate_json('second', {}, task='task')
    assert len(inner) == 1 and len(outer) == 2
    assert outer[0]['call_id'] == inner[0]['call_id']
    inner[0]['usage']['total_tokens'] = 999
    assert outer[0]['usage']['total_tokens'] == 17
    with gateway.capture_model_calls() as empty:
        pass
    assert empty == [] and len(outer) == 2


def test_thread_contexts_are_isolated(tmp_path, monkeypatch, fake_provider):
    configured(tmp_path, monkeypatch)
    def invoke(task):
        with gateway.capture_model_calls() as calls:
            gateway.generate_json('contract', {}, task=task)
        return calls
    with ThreadPoolExecutor(max_workers=2) as pool:
        report, task = list(pool.map(invoke, ['report', 'task']))
    assert [r['task'] for r in report] == ['report']
    assert [r['task'] for r in task] == ['task']
    assert report[0]['call_id'] != task[0]['call_id']


def test_async_contexts_are_isolated(tmp_path, monkeypatch, fake_provider):
    configured(tmp_path, monkeypatch)
    async def invoke(task):
        with gateway.capture_model_calls() as calls:
            await asyncio.sleep(0)
            gateway.generate_json('contract', {}, task=task)
        return calls
    async def together():
        return await asyncio.gather(invoke('report'), invoke('task'))
    records = asyncio.run(together())
    assert [[row['task'] for row in calls] for calls in records] == [['report'], ['task']]


def test_trace_cross_process_serialization(tmp_path, monkeypatch, fake_provider):
    configured(tmp_path, monkeypatch)
    with gateway.capture_model_calls() as calls:
        gateway.generate_json('contract', {}, task='report')
    source, target = tmp_path / 'trace-input.json', tmp_path / 'trace-output.json'
    source.write_text(json.dumps(calls, allow_nan=False), encoding='utf-8')
    # An independent interpreter verifies JSON IPC without opening a named pipe.
    # stdio is inherited; this works inside the Windows file sandbox too.
    code = ('import json,sys; from pathlib import Path; '
            'from enterprise.model_gateway import provenance; '
            'records=json.loads(Path(sys.argv[1]).read_text(encoding="utf-8")); '
            'out=[provenance(trace=r) for r in records]; '
            'Path(sys.argv[2]).write_text(json.dumps(out,allow_nan=False),encoding="utf-8")')
    process = subprocess.run([sys.executable, '-c', code, str(source), str(target)],
                             check=True, timeout=20)
    assert process.returncode == 0
    assert json.loads(target.read_text(encoding='utf-8')) == calls


def test_provenance_rejects_mismatched_trace(tmp_path, monkeypatch, fake_provider):
    configured(tmp_path, monkeypatch)
    with gateway.capture_model_calls() as calls:
        gateway.generate_json('contract', {}, task='report')
    with pytest.raises(ValueError):
        gateway.provenance(trace=calls[0], task='task')
    with pytest.raises(ValueError):
        gateway.provenance(trace={'model': 'invented'})


def test_invalid_endpoint_preflight_does_not_echo_credentials(tmp_path, monkeypatch):
    configured(tmp_path, monkeypatch, base_url='https://user:' + SECRET + '@provider.invalid/v1')
    assert gateway.provenance('contract')['provider_endpoint'] is None
    assert SECRET not in json.dumps(gateway.configuration().public())


@pytest.mark.parametrize('max_tokens', [0, -1, True, None, '3000'])
def test_bad_output_budget_does_not_claim_provider_attempt(tmp_path, monkeypatch, fake_provider, max_tokens):
    configured(tmp_path, monkeypatch)
    with gateway.capture_model_calls() as calls, pytest.raises(gateway.ModelUnavailable):
        gateway.generate_json('contract', {}, max_tokens=max_tokens, task='report')
    assert calls[0]['request_attempted'] is False
    assert fake_provider.calls == []


def test_fixture_demonstrates_two_tasks_routed_without_claiming_live_models(tmp_path, monkeypatch, fake_provider):
    configured(tmp_path, monkeypatch, routes())
    with gateway.capture_model_calls() as calls:
        fake_provider.response_model = 'fixture-small-snapshot'
        gateway.generate_json('contract', {}, task='attribution')
        fake_provider.response_model = 'fixture-narrative-snapshot'
        gateway.generate_json('contract', {}, task='report')
    assert [(row['task'], row['requested_model'], row['model']) for row in calls] == [
        ('attribution', 'fixture-small', 'fixture-small-snapshot'),
        ('report', 'fixture-report', 'fixture-narrative-snapshot')]
    assert [call['model'] for call in fake_provider.calls] == ['fixture-small', 'fixture-report']


def test_profile_name_cannot_leak_same_named_credential(tmp_path, monkeypatch, fake_provider):
    routing = {'enabled': True, 'profiles': {SECRET: {'model': 'fixture-small'}},
               'tasks': {'report': SECRET}}
    configured(tmp_path, monkeypatch, routing)
    assert SECRET not in json.dumps(gateway.configuration('report').public())
    assert SECRET not in json.dumps(gateway.provenance(task='report'))
    with gateway.capture_model_calls() as calls:
        gateway.generate_json('contract', {}, task='report')
    assert calls[0]['profile'] is None and SECRET not in json.dumps(calls)


def test_data_cannot_select_endpoint_model_or_task(tmp_path, monkeypatch, fake_provider):
    configured(tmp_path, monkeypatch, routes())
    data = {'model': 'unapproved-model', 'task': 'report',
            'base_url': 'https://attacker.invalid', 'api_key': 'not-a-config'}
    with gateway.capture_model_calls() as calls:
        gateway.generate_json('contract', data, task='summary')
    assert calls[0]['requested_model'] == 'fixture-small'
    assert calls[0]['task'] == 'summary'
    assert fake_provider.initializations[0]['base_url'] == 'http://127.0.0.1:12345/v1'
    assert fake_provider.initializations[0]['api_key'] == SECRET
