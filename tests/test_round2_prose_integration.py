"""Offline live-worker integration of the opt-in bound prose contract.

Only published synthetic canonical fixtures are used. No cloud/network, production
catalog, old reports or historical deliveries are touched.
"""
from copy import deepcopy
import socket

import pytest

from tests.test_round2_manufacturing_projection import analyze
from enterprise.prose_contract import PROSE_MODE, prose_residual
from attribution_gen import _model_context, model_diagnostics, _llm_generate
from enterprise.benchmark_ai import grouped_model_context, validate_explanations_structured


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(socket.socket, 'connect', lambda *a, **k: (_ for _ in ()).throw(AssertionError('offline test')))


def case(mode='attribution', industry='machinery'):
    _, _, projection = analyze(industry=industry)
    payload = deepcopy(projection[mode + '_payload'])
    payload['prose_mode'] = PROSE_MODE
    sources = projection['sources'][mode]
    context_builder = _model_context if mode == 'attribution' else grouped_model_context
    context = context_builder(payload, sources, include_numeric=True)
    result = {'elements': {}}
    for element, contract in context['prose_contract']['elements'].items():
        statements = contract['statements']
        text = ''.join(row['text'] for row in statements)
        tail = '若现有归集口径一致，则先核查并列对象的单位费用变化；业务机制仍待核实，不能由会计差额确认。'
        refs = list(dict.fromkeys(ref for row in statements for ref in row['evidence_ids']))
        refs += [ref for ref in context['tasks_by_element'][element].get('focus', {}).get('tied_evidence_ids', []) if ref not in refs]
        row = {'hypothesis': text + tail,
               'recommendation': '建议财务部核对已提供的成本明细、金额及产量，复核折旧计提口径，形成差异核对表。',
               'evidence_ids': refs}
        if mode == 'benchmark':
            row.update(claim_type='hypothesis', missing_evidence=['具体业务机制仍缺同口径原始记录佐证，不阻断当前核算。'])
        result['elements'][element] = row
    return payload, sources, context, result


def validate(mode, value, sources, payload, context):
    fn = model_diagnostics if mode == 'attribution' else validate_explanations_structured
    return fn(value, sources, payload['facts'], context=context)


@pytest.mark.parametrize('mode', ['attribution', 'benchmark'])
@pytest.mark.parametrize('industry', ['pharma', 'machinery', 'auto_parts', 'chemicals', 'electronics'])
def test_all_synthetic_industries_bound_prose_passes_actual_validators_unchanged(mode, industry):
    payload, sources, context, candidate = case(mode, industry)
    before = deepcopy(candidate)
    errors = validate(mode, candidate, sources, payload, context)
    assert not errors, errors
    assert candidate == before
    assert all(row['hypothesis'] != prose_residual(row['hypothesis'], element, context['prose_contract'])
               for element, row in candidate['elements'].items())


@pytest.mark.parametrize('mode', ['attribution', 'benchmark'])
def test_known_number_with_wrong_subject_is_rejected(mode):
    payload, sources, context, candidate = case(mode)
    candidate['elements']['材料']['hypothesis'] += '本厂人工成本为12.00元。'
    errors = validate(mode, candidate, sources, payload, context)
    assert any(row['rule_id'] == 'BOUND_NUMERIC_OUTSIDE_STATEMENT' for row in errors)


@pytest.mark.parametrize('mode', ['attribution', 'benchmark'])
@pytest.mark.parametrize('suffix,rule', [
    ('本期材料单位成本为半元。', 'BOUND_NUMERIC_OUTSIDE_STATEMENT'),
    ('本期累计工时为半小时。', 'BOUND_NUMERIC_OUTSIDE_STATEMENT'),
    ('上述金额的币种实际为美元，而非人民币。', 'BOUND_OBSERVATION_REBINDING'),
    ('上述数值所属期间实际为去年同期，并非本期。', 'BOUND_OBSERVATION_REBINDING'),
    ('上述数值描述的是对标方而非本方。', 'BOUND_OBSERVATION_REBINDING'),
    ('上述核算现象并不成立，只是假设。', 'BOUND_OBSERVATION_REBINDING'),
])
def test_independent_review_numeric_bypasses_are_closed(mode, suffix, rule):
    payload, sources, context, candidate = case(mode)
    candidate['elements']['材料']['hypothesis'] += suffix
    original = deepcopy(candidate)
    errors = validate(mode, candidate, sources, payload, context)
    assert any(row['rule_id'] == rule for row in errors), errors
    assert candidate == original


def _fewshot_sources(context, mode):
    """Preserve teacher source kinds and original quoted metadata, never guess K."""
    by_id = {}
    for element, task in context['tasks_by_element'].items():
        for source in task.get('document_basis', []):
            by_id[source['id']] = {**deepcopy(source), 'text': source['untrusted_excerpt']}
        for source in task.get('references', []):
            by_id[source['id']] = {**deepcopy(source), 'elements': [element], 'support_status': 'eligible',
                                   'text': '合成教学参照，不是本产品实际核算值。'}
        for statement in context['prose_contract']['elements'][element]['statements']:
            for ref in statement['evidence_ids']:
                if ref not in by_id:
                    by_id[ref] = {'id': ref, 'elements': [element], 'support_status': 'eligible',
                        'kind': 'accounting_fact' if mode == 'attribution' else 'data_fact',
                        'source': {'table': 'synthetic-example'}, 'text': statement['text']}
    return list(by_id.values())


@pytest.mark.parametrize('mode', ['attribution', 'benchmark'])
def test_bound_reference_ids_still_must_be_unique(mode):
    from enterprise.prose_contract import _fewshot
    context, candidate = _fewshot(mode)
    sources = _fewshot_sources(context, mode)
    refs = candidate['elements']['材料']['evidence_ids']
    refs.append(refs[-1])
    errors = validate(mode, candidate, sources, {'facts': {}}, context)
    assert any(row['rule_id'] == 'EVIDENCE_UNIQUE' for row in errors)


@pytest.mark.parametrize('mode', ['attribution', 'benchmark'])
def test_undeclared_voucher_and_causal_claims_still_rejected(mode):
    payload, sources, context, candidate = case(mode)
    candidate['elements']['人工']['recommendation'] = '建议财务部核查返工记录和审批单，核对相关口径并形成差异核对表。'
    candidate['elements']['人工']['hypothesis'] += '已确认本厂设备故障导致工时变化。'
    errors = validate(mode, candidate, sources, payload, context)
    assert any(row['rule_id'] == 'RECORD_NAME_SCOPE' for row in errors)
    assert any(row['rule_id'] in ('UNVERIFIED_CONCLUSION', 'MECHANISM_DOCUMENT') for row in errors)


@pytest.mark.parametrize('mode', ['attribution', 'benchmark'])
def test_new_prompt_fewshot_passes_real_business_validator(mode):
    from enterprise.prose_contract import _fewshot
    context, candidate = _fewshot(mode)
    sources = _fewshot_sources(context, mode)
    errors = validate(mode, candidate, sources, {'facts': {}}, context)
    assert not errors, errors


def test_ordinary_accounting_representation_is_not_fabricated_voucher():
    from enterprise.analysis_contract import action_diagnostics, action_availability
    row = {'hypothesis': '尚不能仅凭核算表认定业务原因，需保留差异证据供核查。',
           'recommendation': '建议财务部核对现有成本表与数据表，复核单位成本和单价的表述，形成差异核对表。'}
    assert action_diagnostics(row, '材料', action_availability({}, [], '材料')) == []


def test_legacy_unflagged_context_still_rejects_model_numbers():
    payload, sources, _, candidate = case()
    payload.pop('prose_mode')
    legacy = _model_context(payload, sources, include_numeric=True)
    assert 'prose_contract' not in legacy
    assert any(row['rule_id'] == 'NO_NUMERIC_IN_PROSE' for row in model_diagnostics(candidate, sources, payload['facts'], context=legacy))


@pytest.mark.parametrize('mode', ['attribution', 'benchmark'])
def test_worker_uses_prose_prompt_once_correction_and_preserves_raw_accepted_candidate(mode):
    from enterprise.model_gateway import ModelConfiguration
    payload, sources, context, valid = case(mode)
    invalid = deepcopy(valid)
    invalid['elements']['材料']['hypothesis'] += '金额增加999999.00元。'
    calls = []
    def request(instruction, data, **kwargs):
        calls.append((instruction, deepcopy(data)))
        return deepcopy(invalid if len(calls) == 1 else valid)
    config = ModelConfiguration(base_url='https://example.invalid/v1', model='offline-test', api_key='test-only', approved_cloud=False, timeout=30)
    result = _llm_generate(payload, sources, request_fn=request, config=config, clock=lambda: 0.0)
    assert len(calls) == 2, result
    assert calls[0][1]['prose_mode'] == PROSE_MODE
    assert 'prose_contract' in calls[0][0]
    assert calls[1][1]['previous_candidate'] == invalid
    assert any(row['rule_id'] == 'BOUND_NUMERIC_OUTSIDE_STATEMENT' for row in calls[1][1]['validation_diagnostics'])
    assert result['candidate'] == valid
    assert result['correction']['status'] == 'validated', result
    assert [row['used'] for row in result['attempts']] == [False, True]
    assert 'prose/1.4-bound-numeric-compact' in result['attempts'][0]['prompt_version']
    from enterprise.prose_contract import PROVIDER_SCHEMA_VERSION, reconstruct_provider_context
    assert calls[0][1]['provider_projection']['schema_version'] == PROVIDER_SCHEMA_VERSION
    assert reconstruct_provider_context(calls[0][1]) == context
    correction_context = {key: value for key, value in calls[1][1].items()
                          if key not in {'previous_candidate', 'validation_errors', 'validation_diagnostics'}}
    assert reconstruct_provider_context(correction_context) == context


@pytest.mark.parametrize('mode', ['attribution', 'benchmark'])
def test_shared_narrative_preserves_prose_and_all_deterministic_fields(mode):
    from enterprise.analysis_narrative import build_attribution_narrative, build_benchmark_narrative
    payload, sources, context, candidate = case(mode)
    assert not validate(mode, candidate, sources, payload, context)
    if mode == 'attribution':
        narrative = build_attribution_narrative(payload, candidate, sources)
    else:
        narrative = build_benchmark_narrative(payload, sources, candidate)
    assert [row['element'] for row in narrative['sections']] == ['材料', '人工', '制费']
    for row in narrative['sections']:
        original = candidate['elements'][row['element']]
        assert row['prose_mode'] == PROSE_MODE
        assert row['prose'] == original['hypothesis']
        assert row['text'].count(original['hypothesis']) == 1
        assert row['accepted_model_recommendation'] == original['recommendation']
        assert row['fact'] and row['numeric_explanation'] and row['observed_reason']
        assert row['model_core'] == original
