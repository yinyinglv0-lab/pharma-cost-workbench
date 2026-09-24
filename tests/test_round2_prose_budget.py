"""Portable offline timing contracts for the server-owned prose correction budget.

Clocks and request/stage boundaries are explicit test doubles. Actual context,
validation, routing and audit code run; no sleeping, subprocess, cloud or source
corpus mutation is needed to exercise the 95/45-second distinction.
"""
from copy import deepcopy
import socket

import pytest

import attribution_gen as ag
from enterprise import benchmark_ai as bg
from enterprise.model_gateway import ModelConfiguration
from enterprise.prose_contract import PROSE_MODE, model_stage_budget
from tests.test_round2_prose_integration import case, validate
from tests.test_model_caller_followup import contract as legacy_contract


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('Budget tests cannot call real networking or workers')
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    monkeypatch.setattr(socket, 'create_connection', forbidden)
    monkeypatch.setattr('enterprise.model_gateway.generate_json', forbidden)
    monkeypatch.setattr('attribution_runtime.run_stage', forbidden)


class Clock:
    def __init__(self):
        self.value = 0.0
    def __call__(self):
        return self.value
    def advance(self, seconds):
        self.value += seconds


def config(timeout=40):
    return ModelConfiguration('https://offline.invalid/v1', 'synthetic-budget-model',
                              'test-only', False, timeout)


def run(mode, payload, sources, request, clock, **kwargs):
    fn = ag._llm_generate if mode == 'attribution' else bg._llm_generate
    return fn(payload, sources, request_fn=request, clock=clock, **kwargs)


@pytest.mark.parametrize('payload,expected', [
    (None, 45), ([], 45), ({}, 45),
    ({'prose_mode': None}, 45), ({'prose_mode': True}, 45),
    ({'prose_mode': 'bound-numeric-prose/2'}, 45),
    ({'prose_mode': PROSE_MODE + ' '}, 45),
    ({'facts': {'prose_mode': PROSE_MODE}}, 45),
    ({'metadata': {'prose_mode': PROSE_MODE}, 'budget': 9999, 'timeout': 9999}, 45),
    ({'prose_mode': PROSE_MODE}, 95),
    ({'prose_mode': PROSE_MODE, 'budget': 1, 'timeout': 1, 'hard_budget_seconds': 9999}, 95),
])
def test_only_exact_top_level_server_prose_flag_selects_budget(payload, expected, monkeypatch):
    monkeypatch.setenv('COST_LLM_TIMEOUT', '9999')
    monkeypatch.setenv('COST_MODEL_STAGE_BUDGET', '9999')
    original = deepcopy(payload)
    assert model_stage_budget(payload) == expected
    assert payload == original


@pytest.mark.parametrize('mode', ['attribution', 'benchmark'])
def test_thirty_three_second_rejection_still_allows_thirty_five_second_correction(mode):
    payload, sources, context, valid = case(mode)
    assert not validate(mode, valid, sources, payload, context)
    invalid = deepcopy(valid)
    invalid['elements']['材料']['hypothesis'] += '本期金额为999999.00元。'
    before = deepcopy((payload, sources, valid, invalid))
    clock, requests = Clock(), []
    def request(instruction, data, **kwargs):
        requests.append((deepcopy(data), kwargs['config']))
        clock.advance(33 if len(requests) == 1 else 35)
        return deepcopy(invalid if len(requests) == 1 else valid)
    result = run(mode, payload, sources, request, clock, config=config(), deadline=95)
    assert clock() == 68 and len(requests) == 2
    assert result['hard_budget_seconds'] == 95
    assert result['candidate'] == valid and result['failure_type'] is None
    assert [item['status'] for item in result['attempts']] == ['rejected', 'validated']
    assert [item['used'] for item in result['attempts']] == [False, True]
    assert result['correction'] == {'attempted': True, 'status': 'validated'}
    assert requests[1][0]['previous_candidate'] == invalid
    assert any(item['rule_id'] == 'BOUND_NUMERIC_OUTSIDE_STATEMENT'
               for item in requests[1][0]['validation_diagnostics'])
    assert [item['elapsed_seconds'] for item in result['attempts']] == [33, 35]
    assert [item['request_timeout_seconds'] for item in result['attempts']] == [40, 40]
    assert all(item[1].timeout == 40 for item in requests)
    assert all('prose/1.4-bound-numeric-compact' in item['prompt_version'] for item in result['attempts'])
    assert (payload, sources, valid, invalid) == before


@pytest.mark.parametrize('mode', ['attribution', 'benchmark'])
def test_legacy_thirty_three_second_rejection_keeps_original_insufficient_budget(mode):
    payload, sources, valid = legacy_contract(mode)
    payload.update(timeout=9999, hard_budget_seconds=9999, _deadline=9999,
                   facts={**payload['facts'], 'prose_mode': PROSE_MODE})
    invalid = deepcopy(valid)
    invalid['elements']['材料']['hypothesis'] += '金额为999999.00元。'
    clock, calls = Clock(), []
    def request(*args, **kwargs):
        calls.append(kwargs['config'].timeout)
        clock.advance(33)
        return deepcopy(invalid)
    result = run(mode, payload, sources, request, clock, config=config(), deadline=9999)
    assert calls == [40] and clock() == 33 and result['hard_budget_seconds'] == 45
    assert result['candidate'] == invalid and result['attempts'][0]['status'] == 'rejected'
    assert not result['attempts'][0]['used']
    assert result['correction'] == {'attempted': False, 'status': 'skipped_insufficient_budget'}


@pytest.mark.parametrize('mode', ['attribution', 'benchmark'])
@pytest.mark.parametrize('explicit,finish', [(None, 91), (None, 92), (95, 93), (95, 94), (9999, 95)])
def test_default_93_and_explicit_95_deadlines_never_adopt_late_valid_candidates(mode, explicit, finish):
    payload, sources, context, valid = case(mode)
    assert not validate(mode, valid, sources, payload, context)
    payload.update(timeout=9999, hard_budget_seconds=9999, budget=9999)
    clock = Clock()
    def request(*args, **kwargs):
        assert kwargs['config'].timeout == 40
        clock.advance(finish)
        return deepcopy(valid)
    result = run(mode, payload, sources, request, clock, config=config(), deadline=explicit)
    limit = 92 if explicit is None else 94
    assert len(result['attempts']) == 1 and result['hard_budget_seconds'] == 95
    if finish >= limit:
        assert result['candidate'] is None and result['failure_type'] == 'BudgetExhausted'
        assert result['attempts'][0]['status'] == 'budget_exhausted' and not result['attempts'][0]['used']
    else:
        assert result['candidate'] == valid and result['attempts'][0]['used']


@pytest.mark.parametrize('mode', ['attribution', 'benchmark'])
@pytest.mark.parametrize('timeout', [7, 20, 40, 120])
def test_each_request_respects_configured_timeout_and_forty_second_cap(mode, timeout):
    payload, sources, _, valid = case(mode)
    clock, calls = Clock(), []
    def request(*args, **kwargs):
        calls.append(kwargs['config'].timeout)
        return deepcopy(valid)
    result = run(mode, payload, sources, request, clock, config=config(timeout))
    assert calls == [min(timeout, 40)]
    assert result['attempts'][0]['request_timeout_seconds'] == min(timeout, 40)
    assert result['candidate'] == valid


@pytest.mark.parametrize('mode', ['attribution', 'benchmark'])
def test_new_budget_keeps_two_attempt_cap_and_never_retries_provider_failure(mode):
    payload, sources, _, valid = case(mode)
    bad = deepcopy(valid)
    bad['elements']['材料']['hypothesis'] += '本期金额为999999.00元。'
    clock, calls = Clock(), []
    def rejected(*args, **kwargs):
        calls.append(True)
        clock.advance(1)
        return deepcopy(bad)
    result = run(mode, payload, sources, rejected, clock, config=config())
    assert len(calls) == 2 and result['candidate'] == bad
    assert result['correction']['status'] == 'rejected'
    assert all(not attempt['used'] for attempt in result['attempts'])
    calls.clear()
    def failed(*args, **kwargs):
        calls.append(True)
        raise RuntimeError('private provider detail test-only')
    result = run(mode, payload, sources, failed, Clock(), config=config())
    assert calls == [True] and result['candidate'] is None
    assert result['failure_type'] == 'RuntimeError' and not result['correction']['attempted']
    assert 'private provider detail' not in str(result) and 'test-only' not in str(result)


@pytest.mark.parametrize('mode', ['attribution', 'benchmark'])
@pytest.mark.parametrize('prose', [False, True])
def test_canonical_manufacturing_stage_and_audit_use_exact_selected_budget(mode, prose, monkeypatch):
    from enterprise.manufacturing_service import ManufacturingService
    payload, sources, _, _ = case(mode)
    if not prose:
        payload.pop('prose_mode')
    original = deepcopy(payload)
    calls = []
    def stage(name, args, *, timeout):
        calls.append((name, timeout, deepcopy(args)))
        return {'schema': 'invalid-fixture-envelope'}
    monkeypatch.setattr('attribution_runtime.run_stage', stage)
    audit, candidate, status = ManufacturingService._model(mode, payload, sources)
    expected = 95 if prose else 45
    assert len(calls) == 1 and calls[0][:2] == ('model', expected)
    assert audit['hard_budget_seconds'] == expected and candidate is None and status == 'model_unavailable'
    assert payload == original


@pytest.mark.parametrize('prose', [False, True])
def test_validated_benchmark_adapter_uses_selected_budget_without_mutating_payload(prose, monkeypatch):
    from enterprise.analysis_service import validated_model
    payload, sources, _ = legacy_contract('benchmark')
    if prose:
        payload['prose_mode'] = PROSE_MODE
    payload['timeout'] = 9999
    before = deepcopy(payload)
    calls = []
    def stage(name, args, *, timeout):
        calls.append((name, timeout))
        assert args == [before, sources]
        return {'sentinel': True}
    monkeypatch.setattr('attribution_runtime.run_stage', stage)
    assert validated_model(payload, sources) == {'sentinel': True}
    assert calls == [('model', 95 if prose else 45)] and payload == before


def test_m2_default_stage_uses_95_but_injected_callback_stays_single_call(tmp_path, monkeypatch):
    from tests.test_round2_prose_integration import case
    payload, sources, _, _ = case()
    payload.pop('prose_mode')
    payload['elements'] = {}
    payload['facts']['evidence'] = deepcopy(sources)
    payload.setdefault('告警_环比超正负10%', [])
    payload['数据限制'] = 'Synthetic cost records do not confirm business mechanisms.'
    monkeypatch.setattr(ag, 'build_attribution_payload', lambda *a, **kw: deepcopy(payload))
    monkeypatch.setattr(ag, 'build_dashboard_data', lambda *a, **kw: {})
    monkeypatch.setattr('enterprise.model_gateway.configuration', lambda **kw: config())
    monkeypatch.setattr('enterprise.snapshots.current_provenance', lambda: {})
    calls = []
    def stage(name, args, timeout):
        calls.append((name, timeout))
        assert args[0]['prose_mode'] == PROSE_MODE
        return {'schema': ag.M2_MODEL_RUN_SCHEMA, 'candidate': None, 'attempts': [],
                'correction': {'attempted': False, 'status': 'not_requested'}, 'failure_type': 'SyntheticOffline'}
    monkeypatch.setattr(ag, '_execute_stage', stage)
    generated = ag.generate_attribution(payload['product'], payload['month'], True, d={}, root=tmp_path)
    assert calls == [('model', 95)] and generated['model_run']['hard_budget_seconds'] == 95
    observed = []
    def injected(data, evidence):
        observed.append(deepcopy(data))
        return {'invalid': True}
    generated = ag.generate_attribution(payload['product'], payload['month'], True, d={}, root=tmp_path, model_fn=injected)
    assert len(observed) == 1 and calls == [('model', 95)]
    assert 'prose_mode' not in observed[0]
    assert generated['model_run']['execution'] == 'injected_single_call'
    assert generated['model_run']['hard_budget_seconds'] is None

