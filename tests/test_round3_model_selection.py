"""Synthetic-only registry, worker identity and timeout contracts. No paid calls."""
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import socket
from types import SimpleNamespace

import pytest

from enterprise import model_gateway as gateway, model_registry as registry
from enterprise.security import Principal
from enterprise.prose_contract import PROSE_MODE, model_stage_budget
import attribution_gen as ag
import attribution_runtime as runtime

SECRET = 'round3-synthetic-deepseek-secret'
QWEN_SECRET = 'round3-synthetic-dashscope-secret'
LEGACY_SECRET = 'round3-synthetic-legacy-secret'


@pytest.fixture(autouse=True)
def offline(monkeypatch, tmp_path):
    for name in ('COST_LLM_BASE_URL', 'COST_LLM_MODEL', 'COST_LLM_API_KEY', 'COST_LLM_TIMEOUT',
                 'COST_LLM_APPROVED_CLOUD', 'DASHSCOPE_BASE_URL', 'DASHSCOPE_API_KEY',
                 'DASHSCOPE_APPROVED_CLOUD', 'DEEPSEEK_BASE_URL', 'DEEPSEEK_API_KEY',
                 'DEEPSEEK_APPROVED_CLOUD', 'DEEPSEEK_MODEL', 'ATTRIBUTION_RUNTIME_TEST_MODE'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv('COST_LLM_CONFIG_FILE', str(tmp_path / 'synthetic-model.json'))
    import enterprise.model_settings as model_settings
    monkeypatch.setattr(model_settings, 'CREDENTIALS_FILE', tmp_path / 'synthetic-keys.json')
    import httpx
    def forbidden(*args, **kwargs):
        pytest.fail('No real network in model selection tests')
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    monkeypatch.setattr(socket, 'create_connection', forbidden)
    monkeypatch.setattr(httpx.HTTPTransport, 'handle_request', forbidden)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, 'handle_async_request', forbidden)


def admin():
    return Principal('test-admin', 'Test admin', ('system_admin',), ('*',), ('*',))


def local_config(**extra):
    data = {'base_url': 'http://127.0.0.1:12345/v1', 'model': 'legacy-qwen',
            'api_key': LEGACY_SECRET, 'approved_cloud': False,
            'task_routing': {'enabled': True, 'profiles': {'fast': {'model': 'legacy-fast', 'timeout': 23}},
                             'tasks': {'task': 'fast'}},
            'unrelated_policy': {'keep': [1, 2]}}
    data.update(extra)
    gateway.config_path().write_text(json.dumps(data), encoding='utf-8')
    return data


def select(monkeypatch, registry_id='deepseek', **extra):
    monkeypatch.setenv('DEEPSEEK_API_KEY', SECRET)
    monkeypatch.setenv('DASHSCOPE_API_KEY', QWEN_SECRET)
    monkeypatch.setenv('DEEPSEEK_APPROVED_CLOUD', 'true')
    monkeypatch.setenv('DASHSCOPE_APPROVED_CLOUD', 'true')
    local_config(**extra)
    registry.save_selection(registry_id, principal=admin())
    return gateway.configuration(task='attribution')


def rows():
    return {row['registry_id']: row for row in registry.selection_state()['entries']}


def test_default_stays_legacy_and_listing_is_safe_read_only():
    original = local_config()
    before = gateway.config_path().read_bytes()
    state = registry.selection_state()
    assert state['selected_registry_id'] == 'legacy'
    assert state['scope'] == 'deployment_all_users'
    assert gateway.configuration('attribution').model == 'legacy-qwen'
    assert gateway.configuration('attribution').timeout == 40
    assert gateway.configuration('task').model == 'legacy-fast'
    assert rows()['deepseek']['model'] == 'deepseek-flash'
    assert rows()['deepseek']['timeout_seconds'] == 80
    assert rows()['qwen-turbo']['timeout_seconds'] == 40
    assert all(row['last_observed_latency_seconds'] is None for row in state['entries'])
    encoded = json.dumps(state)
    assert LEGACY_SECRET not in encoded and 'base_url' not in encoded and '127.0.0.1' not in encoded
    assert gateway.config_path().read_bytes() == before
    assert not registry._observation_path().exists()
    assert json.loads(before) == original


def test_selected_credential_diagnostic_uses_only_its_provider(monkeypatch):
    cfg = select(monkeypatch)
    expected = {'configured': True, 'registry_id': 'deepseek', 'source': 'environment:DEEPSEEK_API_KEY'}
    assert runtime.credential_status(task='attribution') == expected
    assert runtime.credential_status(config=cfg) == expected
    assert 'length' not in expected and SECRET not in json.dumps(expected)


def test_save_selection_preserves_fields_and_only_selected_tasks(monkeypatch):
    select(monkeypatch)
    saved = json.loads(gateway.config_path().read_text())
    assert saved['api_key'] == LEGACY_SECRET and saved['approved_cloud'] is False
    assert saved['unrelated_policy'] == {'keep': [1, 2]}
    assert saved['task_routing']['tasks'] == {'task': 'fast'}
    for task in ('attribution', 'benchmark', 'report'):
        cfg = gateway.configuration(task)
        assert cfg.registry_id == 'deepseek' and cfg.api_key == SECRET and cfg.timeout == 80
        assert cfg.routing_reason == 'selected_registry'
        assert model_stage_budget({'prose_mode': PROSE_MODE}, config=cfg) == 175
    assert gateway.configuration().model == 'legacy-qwen'
    assert gateway.configuration('task').model == 'legacy-fast'
    assert registry.selection_state()['selected_registry_id'] == 'deepseek'
    assert not registry._observation_path().exists()


@pytest.mark.parametrize('selected', ['unknown', 'https://attacker.invalid/v1', {'model': 'qwen-plus'}, None])
def test_unknown_selection_cannot_override_server_config(selected):
    local_config()
    before = gateway.config_path().read_bytes()
    with pytest.raises(gateway.ModelUnavailable):
        registry.save_selection(selected, principal=admin())
    assert gateway.config_path().read_bytes() == before


def test_unauthorized_save_rejected_before_config_read(monkeypatch):
    actor = Principal('analyst', 'Analyst', ('analyst',), ('*',), ('*',))
    monkeypatch.setattr(gateway, '_read_local', lambda: pytest.fail('No config read before authorization'))
    with pytest.raises(PermissionError):
        registry.save_selection('deepseek', principal=actor)


@pytest.mark.parametrize('choice,key,consent', [('deepseek', 'DEEPSEEK_API_KEY', 'DEEPSEEK_APPROVED_CLOUD'),
                                             ('qwen-plus', 'DASHSCOPE_API_KEY', 'DASHSCOPE_APPROVED_CLOUD')])
def test_no_cross_provider_or_legacy_key_fallback(monkeypatch, choice, key, consent):
    local_config()
    monkeypatch.setenv('COST_LLM_API_KEY', LEGACY_SECRET)
    monkeypatch.setenv('DASHSCOPE_API_KEY' if choice == 'deepseek' else 'DEEPSEEK_API_KEY', 'other-provider-key')
    monkeypatch.setenv(consent, 'true')
    before = gateway.config_path().read_bytes()
    assert rows()[choice]['configured'] is False
    with pytest.raises(gateway.ModelUnavailable):
        registry.save_selection(choice, principal=admin())
    assert gateway.config_path().read_bytes() == before
    monkeypatch.setenv(key, 'correct-provider-key')
    monkeypatch.delenv(consent)
    assert rows()[choice]['configured'] and not rows()[choice]['approved_cloud']
    with pytest.raises(gateway.ModelUnavailable):
        registry.save_selection(choice, principal=admin())
    assert gateway.config_path().read_bytes() == before


def test_registered_cloud_consent_is_not_bypassed_by_loopback_override(monkeypatch):
    local_config(selected_registry_id='deepseek')
    monkeypatch.setenv('DEEPSEEK_API_KEY', SECRET)
    monkeypatch.setenv('DEEPSEEK_BASE_URL', 'https://127.0.0.1/v1')
    assert rows()['deepseek']['selectable'] is False
    with pytest.raises(gateway.ModelUnavailable):
        registry.save_selection('deepseek', principal=admin())
    with gateway.capture_model_calls() as traces, pytest.raises(gateway.ModelUnavailable):
        gateway.generate_json('contract', {}, task='attribution')
    assert not traces[0]['request_attempted']
    assert not registry._observation_path().exists()


@pytest.mark.parametrize('endpoint', ['http://api.deepseek.com/v1', 'https://u:secret@api.deepseek.com/v1',
    'https://api.deepseek.com/v1?key=x', 'https://api.deepseek.com/v1#x', 'file:///tmp/model'])
def test_server_registered_endpoints_fail_closed(monkeypatch, endpoint):
    local_config(selected_registry_id='deepseek')
    monkeypatch.setenv('DEEPSEEK_BASE_URL', endpoint)
    with pytest.raises(gateway.ModelUnavailable):
        gateway.configuration('attribution')


@pytest.mark.parametrize('timeout', [0, 121, True, float('inf'), 'nan'])
def test_registry_timeout_validation(timeout):
    local_config(selected_registry_id='deepseek', model_registry={'deepseek': {'timeout': timeout}})
    with pytest.raises(gateway.ModelUnavailable):
        gateway.configuration('attribution')


def test_custom_admin_model_id_and_legacy_alias_are_not_rewritten(monkeypatch):
    cfg = select(monkeypatch, 'approved-deepseek', model_registry={'approved-deepseek': {
        'provider': 'deepseek', 'model': 'deepseek-enterprise-snapshot', 'timeout': 120,
        'approved_cloud': True}})
    assert cfg.model == 'deepseek-enterprise-snapshot'
    assert cfg.timeout == 120 and model_stage_budget({}, config=cfg) == 255
    monkeypatch.setenv('DEEPSEEK_MODEL', 'deepseek-chat')
    assert gateway.configuration('attribution').model == 'deepseek-chat'
    # Explicit operator model IDs are passed exactly, without promising availability.


def test_atomic_save_failure_keeps_old_config(monkeypatch):
    select(monkeypatch, 'qwen-plus')
    before = gateway.config_path().read_bytes()
    def fail(*args):
        raise OSError('synthetic atomic replacement failure')
    monkeypatch.setattr(gateway.os, 'replace', fail)
    with pytest.raises(OSError):
        registry.save_selection('deepseek', principal=admin())
    assert gateway.config_path().read_bytes() == before
    assert not list(gateway.config_path().parent.glob('.model-*'))


def test_budget_ignores_all_business_overrides(monkeypatch):
    cfg = select(monkeypatch)
    payload = {'model': 'anything', 'registry_id': 'deepseek', 'config': cfg.identity(),
               'timeout': 120, 'hard_budget_seconds': 99999, '_deadline': 99999,
               'model_task': 'report', 'budget_contract': registry.REGISTRY_CONTRACT}
    assert model_stage_budget(payload) == 45
    assert model_stage_budget(payload, config=cfg.public()) == 45
    assert model_stage_budget(payload, config=cfg) == 175
    legacy = gateway.ModelConfiguration('https://offline.invalid', 'x', 'fixture', False, 120)
    assert model_stage_budget(payload, config=legacy) == 45
    payload['prose_mode'] = PROSE_MODE
    assert model_stage_budget(payload, config=legacy) == 95
    assert model_stage_budget(payload, config=cfg) == 175


class Clock:
    def __init__(self): self.value = 0.0
    def __call__(self): return self.value
    def advance(self, seconds): self.value += seconds


@pytest.mark.parametrize('mode', ['attribution', 'benchmark'])
@pytest.mark.parametrize('name,seconds,budget', [('qwen-plus', 40, 95), ('deepseek', 80, 175)])
def test_registered_initial_and_correction_use_same_full_timeout(mode, name, seconds, budget, monkeypatch):
    from tests.test_round2_prose_integration import case
    select(monkeypatch, name)
    config = gateway.configuration(mode)
    payload, sources, _, candidate = case(mode)
    invalid = deepcopy(candidate)
    invalid['elements']['材料']['hypothesis'] += '本期金额为999999.00元。'
    clock, attempts = Clock(), []
    def request(instruction, data, **kwargs):
        attempts.append(kwargs['config'])
        clock.advance(seconds - 2)
        return deepcopy(invalid if len(attempts) == 1 else candidate)
    result = ag._llm_generate(payload, sources, request_fn=request, config=config, clock=clock, deadline=budget)
    assert result['hard_budget_seconds'] == budget
    assert len(attempts) == 2 and [cfg.timeout for cfg in attempts] == [seconds, seconds]
    assert [cfg.fingerprint() for cfg in attempts] == [config.fingerprint()] * 2
    assert result['candidate'] == candidate and result['failure_type'] is None
    assert [row['status'] for row in result['attempts']] == ['rejected', 'validated']
    assert all('prose/1.4-' in row['prompt_version'] for row in result['attempts'])


@pytest.mark.parametrize('mode', ['attribution', 'benchmark'])
def test_registered_late_result_not_adopted_or_retried(mode, monkeypatch):
    from tests.test_round2_prose_integration import case
    select(monkeypatch)
    payload, sources, _, candidate = case(mode)
    clock, calls = Clock(), []
    def late(*args, **kwargs):
        calls.append(kwargs['config'].timeout)
        clock.advance(175)
        return deepcopy(candidate)
    result = ag._llm_generate(payload, sources, request_fn=late, config=gateway.configuration(mode), clock=clock)
    assert calls == [80] and result['candidate'] is None
    assert result['failure_type'] == 'BudgetExhausted'
    assert not result['attempts'][0]['used']


@pytest.mark.parametrize('mode', ['attribution', 'benchmark'])
def test_registered_rejections_have_two_attempt_cap_and_errors_have_one(mode, monkeypatch):
    from tests.test_round2_prose_integration import case
    select(monkeypatch)
    payload, sources, _, candidate = case(mode)
    candidate['elements']['材料']['hypothesis'] += '本期金额为999999.00元。'
    calls = []
    def rejected(*args, **kwargs):
        calls.append(kwargs['config'].model)
        return deepcopy(candidate)
    result = ag._llm_generate(payload, sources, request_fn=rejected, config=gateway.configuration(mode), clock=lambda: 0)
    assert len(calls) == 2 and result['correction']['status'] == 'rejected'
    assert all(not item['used'] for item in result['attempts'])
    calls.clear()
    def failed(*args, **kwargs):
        calls.append(True)
        raise RuntimeError('synthetic provider failure')
    result = ag._llm_generate(payload, sources, request_fn=failed, config=gateway.configuration(mode), clock=lambda: 0)
    assert calls == [True] and not result['correction']['attempted']


def test_frozen_identity_rejects_selection_change_before_spawn_and_in_child(monkeypatch, tmp_path):
    import attribution_worker
    select(monkeypatch)
    args, budget = runtime.prepare_model_stage({}, [], task='attribution')
    identity = deepcopy(args.identity)
    assert SECRET not in json.dumps(identity) and 'base_url' not in identity
    registry.save_selection('qwen-plus', principal=admin())
    monkeypatch.setattr(runtime.subprocess, 'Popen', lambda *a, **kw: pytest.fail('No stale worker spawn'))
    with pytest.raises(gateway.ModelUnavailable, match='已更改'):
        runtime.run_stage('model', args, timeout=budget)
    inp, out = tmp_path / 'worker-in.json', tmp_path / 'worker-out.json'
    inp.write_text(json.dumps({'payload': {}, 'evidence': [], '_model_identity': identity}), encoding='utf-8')
    monkeypatch.setattr(ag, '_llm_generate', lambda *a, **kw: pytest.fail('No stale provider call'))
    assert attribution_worker.main(['worker', 'model', str(inp), str(out)]) == 1
    assert json.loads(out.read_text())['error_type'] == 'ModelUnavailable'


@pytest.mark.parametrize('changed', ['model', 'timeout', 'approved_cloud', 'base_url'])
def test_frozen_identity_rejects_registered_policy_change(monkeypatch, changed):
    select(monkeypatch)
    args, _ = runtime.prepare_model_stage({}, [], task='report')
    updated = json.loads(gateway.config_path().read_text())
    updated['model_registry'] = {'deepseek': {changed: {'model': 'new-model', 'timeout': 90,
        'approved_cloud': False, 'base_url': 'https://new-provider.invalid/v1'}[changed]}}
    if changed == 'approved_cloud':
        monkeypatch.delenv('DEEPSEEK_APPROVED_CLOUD')
    gateway.config_path().write_text(json.dumps(updated), encoding='utf-8')
    with pytest.raises(gateway.ModelUnavailable):
        runtime.resolve_worker_configuration(args.identity)


def test_worker_file_only_gets_safe_identity_and_payload_cannot_change_it(monkeypatch):
    select(monkeypatch)
    captured = []
    class FakeProcess:
        def __init__(self, argv, **kwargs):
            value = json.loads(Path(argv[3]).read_text(encoding='utf-8'))
            captured.append(value)
            Path(argv[4]).write_text(json.dumps({'ok': True, 'result': {'safe': True}}), encoding='utf-8')
        def wait(self, timeout): return 0
    monkeypatch.setattr(runtime.subprocess, 'Popen', FakeProcess)
    args, budget = runtime.prepare_model_stage({'model_task': 'report', 'timeout': 9999}, [], task='attribution')
    assert runtime.run_stage('model', args, timeout=budget) == {'safe': True}
    assert captured[0]['_model_identity']['task'] == 'attribution'
    assert captured[0]['_model_identity']['registry_id'] == 'deepseek'
    encoded = json.dumps(captured[0])
    assert SECRET not in encoded and QWEN_SECRET not in encoded and 'api_key' not in encoded
    runtime.run_stage('model', {'payload': {}, 'evidence': [], '_model_identity': {'registry_id': 'attacker'},
                               '_deadline': 99999999}, timeout=1)
    assert captured[1]['_model_identity'] == args.identity


@pytest.fixture
def provider(monkeypatch):
    import openai
    class Provider:
        inits, calls = [], []
        model = 'deepseek-flash'
        model_version = None
        error = False
        def __init__(self, **kwargs):
            self.inits.append(kwargs)
            self.chat = SimpleNamespace(completions=self)
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def create(self, **kwargs):
            self.calls.append(kwargs)
            if self.error:
                raise RuntimeError(SECRET)
            return SimpleNamespace(model=self.model, model_version=self.model_version,
                id='synthetic-response', _request_id='synthetic-request', usage={},
                choices=[SimpleNamespace(message=SimpleNamespace(content='{"ok":true}'))])
    monkeypatch.setattr(openai, 'OpenAI', Provider)
    return Provider


def test_trace_latency_unknown_version_and_paid_baseline_are_configuration_scoped(monkeypatch, provider):
    cfg = select(monkeypatch)
    clock = Clock()
    monkeypatch.setattr(gateway.time, 'monotonic', clock)
    original = provider.create
    def call(self, **kwargs):
        clock.advance(12.5)
        return original(self, **kwargs)
    monkeypatch.setattr(provider, 'create', call)
    with gateway.capture_model_calls() as traces:
        assert gateway.generate_json('private instruction', {'private': 'input'}, task='attribution') == {'ok': True}
    trace, = runtime.sanitize_model_calls(traces)
    assert trace['registry_id'] == 'deepseek'
    assert trace['requested_model'] == trace['returned_model'] == 'deepseek-flash'
    assert trace['model_version'] == 'unknown'
    assert trace['config_sha256'] == trace['paid_baseline_key'] == cfg.fingerprint()
    assert len(trace['request_sha256']) == 64 and trace['duration_seconds'] == 12.5
    assert provider.inits[0]['timeout'] == 80 and provider.inits[0]['max_retries'] == 0
    assert provider.calls[0]['extra_body'] == {'thinking': {'type': 'disabled'}}
    assert cfg.fingerprint() != replace(cfg, thinking_mode=None).fingerprint()
    assert rows()['deepseek']['last_observed_latency_seconds'] == 12.5
    assert rows()['qwen-plus']['last_observed_latency_seconds'] is None
    stored = registry._observation_path().read_text()
    for private in (SECRET, 'private instruction', 'input', 'base_url', 'api_key', 'request_sha256'):
        assert private not in stored
    registry.save_selection('qwen-plus', principal=admin())
    qwen = gateway.configuration('attribution')
    assert qwen.fingerprint() != cfg.fingerprint()
    updated = json.loads(gateway.config_path().read_text())
    updated['model_registry'] = {'deepseek': {'model': 'new-deepseek-model'}}
    gateway.config_path().write_text(json.dumps(updated), encoding='utf-8')
    assert rows()['deepseek']['last_observed_latency_seconds'] is None


def test_actual_version_only_comes_from_response_and_secret_metadata_is_sanitized(monkeypatch, provider):
    select(monkeypatch)
    provider.model_version = 'snapshot-20260924'
    with gateway.capture_model_calls() as traces:
        gateway.generate_json('contract', {}, task='benchmark')
    assert traces[0]['model_version'] == 'snapshot-20260924'
    provider.model = SECRET
    provider.model_version = SECRET
    with gateway.capture_model_calls() as traces:
        gateway.generate_json('contract', {}, task='benchmark')
    safe = runtime.sanitize_model_calls(traces, secret=SECRET)[0]
    assert safe['returned_model'] is None and safe['model_version'] == 'unknown'
    assert SECRET not in json.dumps(safe)


def test_qwen_has_no_deepseek_options_and_business_fields_cannot_enable_them(monkeypatch, provider):
    select(monkeypatch, 'qwen-plus')
    with gateway.capture_model_calls() as traces:
        gateway.generate_json('contract', {'extra_body': {'thinking': {'type': 'enabled'}},
                                          'timeout': 9999, 'model': 'deepseek-flash'}, task='attribution')
    assert 'extra_body' not in provider.calls[0]
    assert provider.calls[0]['model'] == 'qwen-plus'
    assert provider.inits[0]['timeout'] == 40
    assert traces[0]['registry_id'] == 'qwen-plus'


def test_no_transport_retry_or_other_model_fallback(monkeypatch, provider):
    select(monkeypatch)
    provider.error = True
    with gateway.capture_model_calls() as traces, pytest.raises(gateway.ModelUnavailable):
        gateway.generate_json('contract', {}, task='attribution')
    assert len(provider.calls) == 1 and provider.calls[0]['model'] == 'deepseek-flash'
    assert traces[0]['status'] == 'unavailable' and not traces[0]['routing_fallback']
    assert SECRET not in json.dumps(traces)


@pytest.mark.parametrize('task', ['attribution', 'benchmark'])
def test_manufacturing_and_benchmark_adapter_forward_matching_budget(task, monkeypatch):
    from enterprise.manufacturing_service import ManufacturingService
    from enterprise.analysis_service import validated_model
    from tests.test_round2_prose_integration import case
    select(monkeypatch)
    payload, sources, _, _ = case(task)
    calls = []
    def stage(name, args, *, timeout):
        calls.append((name, args.identity, timeout))
        assert isinstance(args, runtime.ModelStageArguments)
        assert runtime.resolve_worker_configuration(args.identity).timeout == 80
        return {'invalid': True}
    monkeypatch.setattr(runtime, 'run_stage', stage)
    audit, candidate, status = ManufacturingService._model(task, payload, sources)
    assert calls[-1][2] == audit['hard_budget_seconds'] == 175
    assert status == 'model_unavailable' and candidate is None
    if task == 'benchmark':
        assert validated_model(payload, sources) == {'invalid': True}
        assert calls[-1][2] == 175


def test_main_m2_generation_freezes_selected_config_once(monkeypatch, tmp_path):
    from tests.test_round2_prose_integration import case
    select(monkeypatch)
    payload, sources, _, _ = case()
    payload.pop('prose_mode')
    payload['elements'] = {}
    payload['facts']['evidence'] = deepcopy(sources)
    payload.setdefault('告警_环比超正负10%', [])
    payload['数据限制'] = 'Synthetic only.'
    monkeypatch.setattr(ag, 'build_attribution_payload', lambda *a, **kw: deepcopy(payload))
    monkeypatch.setattr(ag, 'build_dashboard_data', lambda *a, **kw: {})
    monkeypatch.setattr('enterprise.snapshots.current_provenance', lambda: {})
    original = gateway.configuration
    resolutions, stages = [], []
    def resolve(*args, **kwargs):
        resolutions.append(kwargs.get('task'))
        return original(*args, **kwargs)
    monkeypatch.setattr(gateway, 'configuration', resolve)
    def stage(name, args, timeout):
        stages.append((args.identity, timeout))
        return {'schema': ag.M2_MODEL_RUN_SCHEMA, 'candidate': None, 'attempts': [],
                'correction': {'attempted': False, 'status': 'not_requested'}, 'failure_type': 'Offline'}
    monkeypatch.setattr(ag, '_execute_stage', stage)
    result = ag.generate_attribution(payload['product'], payload['month'], True, d={}, root=tmp_path)
    assert resolutions == ['attribution']
    assert len(stages) == 1 and stages[0][0]['registry_id'] == 'deepseek'
    assert stages[0][1] == result['model_run']['hard_budget_seconds'] == 175
    assert not result['used_llm']


def test_main_m3_generation_freezes_same_identity_and_budget(monkeypatch):
    from enterprise.benchmark_ai import generate_benchmark_analysis, MODEL_RUN_SCHEMA
    from enterprise.analysis_service import validated_model
    from tests.test_model_caller_followup import benchmark_tables, PRODUCT, SPEC, MONTH
    select(monkeypatch)
    calls = []
    def stage(name, args, *, timeout):
        calls.append((args.identity, timeout))
        return {'schema': MODEL_RUN_SCHEMA, 'candidate': None, 'attempts': [],
                'correction': {'attempted': False, 'status': 'not_requested'}, 'failure_type': 'Offline'}
    monkeypatch.setattr(runtime, 'run_stage', stage)
    result = generate_benchmark_analysis(PRODUCT, SPEC, MONTH, benchmark_tables(), model_fn=validated_model)
    assert len(calls) == 1 and calls[0][1] == result['model_run']['hard_budget_seconds'] == 175
    assert calls[0][0]['task'] == 'benchmark' and calls[0][0]['registry_id'] == 'deepseek'
    assert not result['used_llm']


def test_settings_has_name_selection_no_secret_or_endpoint_widgets_or_probes(monkeypatch):
    from streamlit.testing.v1 import AppTest
    import app_pages._shared as shared
    select(monkeypatch, 'qwen-plus')
    monkeypatch.setattr(shared, 'page_context', lambda *args: (admin(), None))
    monkeypatch.setattr(gateway, 'generate_json', lambda *a, **kw: pytest.fail('Settings cannot call models'))
    app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / 'app_pages/settings.py')).run()
    assert not app.exception
    assert len(app.text_input) == 0 and len(app.selectbox) == 1 and len(app.checkbox) == 0
    assert len(app.button) == 1
    assert '整个部署共用' in app.subheader[0].value
    app.selectbox[0].select('deepseek')
    app.button[0].click().run()
    assert not app.exception
    assert registry.selection_state()['selected_registry_id'] == 'deepseek'
    assert not registry._observation_path().exists()
