"""Synthetic-only tests of bounded M2 correction; no source/customer corpus/network."""
from copy import deepcopy
import json
import time

import pytest

import attribution_gen as ag
from enterprise.model_gateway import ModelConfiguration, ModelUnavailable
from attribution_runtime import StageExecutionError, run_stage


class Clock:
    def __init__(self):
        self.value = 0.0
    def __call__(self):
        return self.value
    def advance(self, seconds):
        self.value += seconds


@pytest.fixture
def contract():
    refs = {key: 'F00' + str(index) for index, key in enumerate(('材料', '人工', '制费'), 1)}
    evidence = [{'id': ident, 'kind': 'accounting_fact', 'elements': [key],
                 'source': {'table': 'synthetic'}, 'text': '同要素本期与前期的核算记录。'}
                for key, ident in refs.items()]
    candidate = {'elements': {key: {
        'hypothesis': '该项成本变化可能涉及业务投入与费用归集，具体经营原因尚待核查。',
        'recommendation': '建议财务部核对本期原始凭证与成本归集记录，复核计价和分配口径。',
        'evidence_ids': [ident],
    } for key, ident in refs.items()}}
    return {'fixture': 'synthetic-only'}, evidence, candidate


@pytest.fixture
def config():
    return ModelConfiguration('https://fixture.invalid/v1', 'test-model', 'test-secret', True, 40)


def run(contract, config, request, **kwargs):
    payload, evidence, _ = contract
    return ag._llm_generate(payload, evidence, config=config, request_fn=request, **kwargs)


def test_first_valid_response_makes_one_call_and_preserves_input(contract, config):
    calls = []
    payload, evidence, candidate = contract
    original = deepcopy(contract)
    def request(instruction, data, **kwargs):
        calls.append((instruction, data, kwargs))
        return deepcopy(candidate)
    result = run(contract, config, request)
    assert len(calls) == 1 and result['candidate'] == candidate
    assert result['attempts'][0]['status'] == 'validated' and result['attempts'][0]['used']
    assert result['correction'] == {'attempted': False, 'status': 'not_needed'}
    assert len(result['attempts'][0]['response_sha256']) == 64
    assert 'test-secret' not in json.dumps(result)
    assert contract == original


def test_rejected_json_has_one_feedback_call_using_same_evidence(contract, config):
    clock, calls, progress = Clock(), [], []
    invalid = deepcopy(contract[2])
    invalid['elements']['材料']['recommendation'] = '核对本期采购合同及入库记录，检查计价口径与结算日期。'
    def request(instruction, data, **kwargs):
        calls.append((instruction, deepcopy(data), kwargs['config'].timeout))
        clock.advance(10 if len(calls) == 1 else 8)
        return invalid if len(calls) == 1 else deepcopy(contract[2])
    result = run(contract, config, request, clock=clock, deadline=45, attempt_recorder=progress.append)
    assert len(calls) == 2 and result['correction']['status'] == 'validated'
    assert calls[1][1]['previous_candidate'] == invalid
    assert calls[1][1]['证据'] == contract[1]
    assert calls[1][1]['看板波动数据'] == contract[0]
    assert any('责任部门' in error for error in calls[1][1]['validation_errors'])
    assert calls[1][2] == 33 and clock() == 18
    assert [item['status'] for item in result['attempts']] == ['rejected', 'validated']
    assert [item['used'] for item in result['attempts']] == [False, True]
    assert result['attempts'][0]['diagnostics']
    assert all('candidate' not in row for row in progress)
    assert invalid['elements']['材料']['recommendation'].startswith('核对本期采购合同')


def test_two_invalid_responses_stop_without_cleaning_or_third_call(contract, config):
    calls = []
    invalid = deepcopy(contract[2])
    invalid['elements']['人工']['hypothesis'] = '该项成本变化可能与加班记录有关，但现有核算表不足以确认。'
    def request(*args, **kwargs):
        calls.append(True)
        return deepcopy(invalid)
    result = run(contract, config, request)
    assert len(calls) == 2 and result['candidate'] == invalid
    assert result['correction']['status'] == 'rejected'
    assert all(not item['used'] and item['status'] == 'rejected' for item in result['attempts'])
    assert all('文档依据' in item['diagnostics'][0] for item in result['attempts'])


@pytest.mark.parametrize('failure', [ModelUnavailable, TimeoutError, ValueError])
def test_provider_failure_does_not_retry_or_persist_message(contract, config, failure):
    calls = []
    def request(*args, **kwargs):
        calls.append(True)
        raise failure('Authorization sk-secret https://credential.invalid')
    result = run(contract, config, request)
    assert len(calls) == 1 and result['candidate'] is None
    assert result['failure_type'] == failure.__name__
    assert result['attempts'][0]['failure_type'] == failure.__name__
    assert result['attempts'][0]['response_sha256'] is None
    assert 'sk-secret' not in json.dumps(result) and 'credential.invalid' not in json.dumps(result)


@pytest.mark.parametrize('raw', ['not JSON', '[1,2]', ['list-not-object']])
def test_unparsed_or_nonobject_provider_result_is_not_repairable(contract, config, raw):
    calls = []
    def request(*args, **kwargs):
        calls.append(True)
        return raw
    result = run(contract, config, request)
    assert calls == [True] and result['failure_type'] == 'TypeError'


def test_failed_correction_keeps_initial_rejection_audit(contract, config):
    calls = []
    bad = deepcopy(contract[2]); bad['elements']['人工']['hypothesis'] += '3%'
    def request(*args, **kwargs):
        calls.append(True)
        if len(calls) == 1:
            return bad
        raise ModelUnavailable('private provider details')
    result = run(contract, config, request)
    assert len(calls) == 2
    assert result['candidate'] == bad
    assert result['attempts'][0]['status'] == 'rejected' and result['attempts'][0]['diagnostics']
    assert result['attempts'][1]['status'] == 'unavailable'
    assert result['correction']['status'] == 'failed_unavailable'
    assert all(not item['used'] for item in result['attempts'])
    assert 'private provider' not in json.dumps(result)


def test_insufficient_remaining_budget_skips_correction_explicitly(contract, config):
    clock, calls = Clock(), []
    bad = deepcopy(contract[2]); bad['elements']['人工']['hypothesis'] += '3%'
    def request(*args, **kwargs):
        calls.append(kwargs['config'].timeout)
        clock.advance(33)
        return bad
    result = run(contract, config, request, clock=clock, deadline=45)
    assert calls == [40] and result['candidate'] == bad
    assert result['correction'] == {'attempted': False, 'status': 'skipped_insufficient_budget'}
    assert not result['attempts'][0]['used']


def test_late_candidate_is_not_adopted_even_if_valid(contract, config):
    clock = Clock()
    def request(*args, **kwargs):
        clock.advance(46)
        return deepcopy(contract[2])
    result = run(contract, config, request, clock=clock, deadline=45)
    assert result['failure_type'] == 'BudgetExhausted' and result['candidate'] is None
    assert not result['attempts'][0]['used']


@pytest.mark.parametrize('role', ['财务部', '采购部门', '生产车间', '设备管理部', '能源管理部门', '人力资源部', '人事部', '成本会计', '财务人员', '生产负责人'])
def test_explicit_responsible_roles_are_supported(contract, role):
    candidate = deepcopy(contract[2])
    candidate['elements']['材料']['recommendation'] = f'请{role}核对本期原始凭证与成本归集记录，复核计价和分配口径。'
    assert ag._model_errors(candidate, contract[1]) == []


@pytest.mark.parametrize('object_name', ['采购合同', '设备运行', '生产记录'])
def test_business_objects_are_not_responsible_roles(contract, object_name):
    candidate = deepcopy(contract[2])
    candidate['elements']['材料']['recommendation'] = f'核对本期{object_name}与成本归集记录，复核计价和分配口径。'
    assert any('明确责任部门或角色' in error for error in ag._model_errors(candidate, contract[1]))


def test_unambiguous_reduced_output_fixed_cost_dilution_claim_is_rejected(contract):
    candidate = deepcopy(contract[2])
    candidate['elements']['制费']['hypothesis'] = '本期产量减少导致固定成本摊薄，单位费用可能因此下降，实际原因待核查。'
    assert any('不能将产量减少解释为固定成本摊薄' in error for error in ag._model_errors(candidate, contract[1]))
    candidate['elements']['制费']['hypothesis'] = '不能将单位费用下降直接解释为产量减少导致固定成本摊薄，实际原因仍待核查。'
    assert ag._model_errors(candidate, contract[1]) == []


@pytest.fixture
def parent_service(monkeypatch, contract):
    payload, evidence, candidate = contract
    facts = {'available': True, 'elements': {key: {'evidence_ids': row['evidence_ids']} for key, row in candidate['elements'].items()},
             'evidence': evidence}
    value = {'facts': facts, 'elements': {}, '告警_环比超正负10%': [], '数据限制': 'synthetic-only'}
    monkeypatch.setattr(ag, 'build_dashboard_data', lambda *args: {})
    monkeypatch.setattr(ag, 'build_attribution_payload', lambda *args: deepcopy(value))
    monkeypatch.setattr(ag, 'render_report', lambda *args: 'deterministic synthetic report')
    import attribution_narrative
    import enterprise.snapshots
    import enterprise.model_gateway
    monkeypatch.setattr(attribution_narrative, 'render', lambda *args: ('overview', [], 'text'))
    monkeypatch.setattr(enterprise.snapshots, 'current_provenance', lambda: {})
    monkeypatch.setattr(enterprise.model_gateway, 'configuration', lambda: type('Configuration', (), {'api_key': 'fixture'})())
    return lambda **kwargs: ag.generate_attribution('synthetic', '2026-05', d={}, **kwargs)


def test_parent_revalidates_worker_candidate_and_preserves_failed_attempt(parent_service, monkeypatch, contract, config):
    calls = []
    bad = deepcopy(contract[2]); bad['elements']['材料']['hypothesis'] += '3%'
    def provider(*args, **kwargs):
        calls.append(True)
        return bad if len(calls) == 1 else deepcopy(contract[2])
    result = run(contract, config, provider)
    monkeypatch.setattr(ag, '_execute_stage', lambda stage, args, timeout: result)
    final = parent_service()
    assert final['used_llm'] and final['model_run']['provider_call_count'] == 2
    assert any('模型尝试1' in reason for reason in final['validation']['diagnostics'])
    assert final['model_run']['attempts'][0]['used'] is False
    assert final['review_status'] == 'needs_review'
    result['candidate'] = bad
    final = parent_service()
    assert not final['used_llm'] and final['generation_status'] == 'model_rejected'
    assert all(not attempt['used'] for attempt in final['model_run']['attempts'])


def test_injected_callback_cannot_impersonate_trusted_worker(parent_service, contract, config):
    calls = []
    envelope = run(contract, config, lambda *args, **kwargs: deepcopy(contract[2]))
    def injected(*args):
        calls.append(True)
        return envelope
    final = parent_service(model_fn=injected)
    assert calls == [True] and final['generation_status'] == 'model_rejected'
    assert not final['used_llm'] and final['model_run']['attempts'] == []


def test_callback_exception_cannot_inject_worker_audit_or_fake_timeout(parent_service):
    secret = 'private-provider-and-credential-body'
    def callback(*args):
        error = StageExecutionError('model', secret, secret, timed_out=True,
                                    model_run={'attempts': [{'attempt': 1, 'diagnostics': [secret]}],
                                               'correction': {'status': secret}, 'raw_provider': secret})
        raise error
    result = parent_service(model_fn=callback)
    assert result['generation_status'] == 'model_unavailable'
    assert result['model_run']['execution'] == 'injected_single_call'
    assert result['model_run']['attempts'] == []
    assert secret not in json.dumps(result, ensure_ascii=False)
    assert not any('请求进程已终止' in item for item in result['validation']['diagnostics'])
    assert any('StageExecutionError' in item for item in result['validation']['diagnostics'])


def test_trusted_worker_timeout_preserves_only_audit_keys(parent_service, monkeypatch):
    def timeout(*args):
        raise StageExecutionError('model', 'TimeoutError', 'safe', timed_out=True, model_run={
            'attempts': [{'attempt': 1, 'diagnostics': ['初次候选被拒绝'], 'used': False},
                         {'attempt': 2, 'diagnostics': [], 'status': 'interrupted', 'used': False}],
            'correction': {'attempted': True, 'status': 'interrupted'}, 'other': 'not-copied'})
    monkeypatch.setattr(ag, '_execute_stage', timeout)
    result = parent_service()
    assert result['model_run']['provider_call_count'] == 2
    assert result['model_run']['correction']['status'] == 'interrupted'
    assert 'other' not in result['model_run']
    assert any('初次候选被拒绝' in item for item in result['validation']['diagnostics'])
    assert any('请求进程已终止' in item for item in result['validation']['diagnostics'])


def test_real_worker_single_deadline_retains_partial_audit_and_terminates(monkeypatch):
    monkeypatch.setenv('ATTRIBUTION_RUNTIME_TEST_MODE', '1')
    audit = {'attempts': [{'attempt': 1, 'status': 'rejected', 'used': False,
                           'diagnostics': ['缺少责任主体'], 'response_sha256': 'a' * 64},
                          {'attempt': 2, 'status': 'running', 'used': False}],
             'correction': {'attempted': True, 'status': 'running'},
             'candidate': {'must_not_escape': 'private-body'}}
    start = time.monotonic()
    with pytest.raises(StageExecutionError) as caught:
        run_stage('model', {'_test_model_run': audit, '_test_sleep': 30, '_deadline': start + 9999}, timeout=1.5)
    assert caught.value.timed_out and time.monotonic() - start < 6
    assert caught.value.model_run['attempts'][0]['diagnostics'] == ['缺少责任主体']
    assert caught.value.model_run['attempts'][1]['status'] == 'interrupted'
    assert caught.value.model_run['correction']['status'] == 'interrupted'
    assert 'private-body' not in json.dumps(caught.value.model_run)


def _pid_running(pid):
    import os
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            error = ctypes.get_last_error()
            if error == 87:  # ERROR_INVALID_PARAMETER: this PID no longer exists.
                return False
            raise ctypes.WinError(error)
        try:
            code = wintypes.DWORD()
            if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)):
                raise ctypes.WinError(ctypes.get_last_error())
            return code.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def test_actual_worker_pid_is_gone_after_hard_timeout(monkeypatch, tmp_path):
    import os
    import subprocess
    import attribution_runtime as runtime
    monkeypatch.setenv('ATTRIBUTION_RUNTIME_TEST_MODE', '1')
    started = {}
    original_popen = runtime.subprocess.Popen
    def observe_popen(*args, **kwargs):
        process = original_popen(*args, **kwargs)
        started.update(popen_pid=process.pid, executable=args[0][0])
        original_wait = process.wait
        def observe_wait(*wait_args, **wait_kwargs):
            try:
                return original_wait(*wait_args, **wait_kwargs)
            except runtime.subprocess.TimeoutExpired:
                if pid_file.is_file():
                    started['worker_alive_before_kill'] = _pid_running(int(pid_file.read_text(encoding='ascii')))
                raise
        process.wait = observe_wait
        return process
    monkeypatch.setattr(runtime.subprocess, 'Popen', observe_popen)
    pid_file = tmp_path / 'actual-worker.pid'
    identity_file = tmp_path / 'actual-worker-identity.json'
    with pytest.raises(StageExecutionError) as caught:
        run_stage('model', {'_test_pid_file': str(pid_file), '_test_identity_file': str(identity_file), '_test_sleep': 30}, timeout=1.5)
    assert caught.value.timed_out and pid_file.is_file()
    pid = int(pid_file.read_text(encoding='ascii'))
    lingering = _pid_running(pid)
    print(json.dumps({**started, 'worker_pid': pid, 'worker_still_active': lingering,
                      'worker_identity': json.loads(identity_file.read_text(encoding='utf-8'))}))
    try:
        assert started.get('worker_alive_before_kill') is True
        assert not lingering, f'owned actual worker PID {pid} survived the hard deadline'
    finally:
        # Clean up this exact recorded test-owned process if testing an older,
        # broken redirector-only termination implementation. Never sweep workers.
        if lingering:
            if os.name == 'nt':
                subprocess.run(['taskkill', '/PID', str(pid), '/T', '/F'], stdin=subprocess.DEVNULL,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5, check=True)
            else:
                import signal
                os.kill(pid, signal.SIGKILL)


def test_runtime_timeout_kills_and_reaps_worker(monkeypatch):
    import attribution_runtime as runtime
    actions = []
    class Process:
        def wait(self, timeout):
            actions.append(('wait', timeout))
            if len(actions) == 1:
                raise runtime.subprocess.TimeoutExpired('synthetic-worker', timeout)
        def kill(self):
            actions.append(('kill', None))
    monkeypatch.setattr(runtime.subprocess, 'Popen', lambda *args, **kwargs: Process())
    with pytest.raises(StageExecutionError):
        runtime.run_stage('model', {}, timeout=1)
    assert [action[0] for action in actions] == ['wait', 'kill', 'wait']
