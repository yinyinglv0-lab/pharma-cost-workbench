"""Round-two offline-only contract gates; no provider, configuration or DB writes."""
from copy import deepcopy
import json
import re

import pytest

import attribution_gen as m2
from enterprise import benchmark_ai as m3
from enterprise.analysis_contract import (
    SAFE_RECORD_CATEGORIES, action_availability, action_diagnostics, diagnostic,
    domain_descriptors, legacy_errors, observation_diagnostics,
)
from enterprise.model_gateway import ModelConfiguration, ModelUnavailable


@pytest.fixture(params=['m2', 'm3'])
def contract(request):
    module = m2 if request.param == 'm2' else m3
    example = deepcopy(module.M2_FEWSHOT if request.param == 'm2' else module.M3_FEWSHOT)
    validate = m2.model_diagnostics if request.param == 'm2' else m3.validate_explanations_structured
    return request.param, module, example, validate


def test_complete_fewshot_runs_all_production_guards(contract):
    mode, module, example, validate = contract
    original = deepcopy(example)
    assert set(example['input']['tasks_by_element']) == {'材料', '人工', '制费'}
    assert set(example['output']['elements']) == {'材料', '人工', '制费'}
    assert validate(example['output'], example['evidence'], context=example['input']) == []
    assert example == original
    instruction = module.M2_INSTRUCTION if mode == 'm2' else module.BENCHMARK_PROMPT
    assert len(re.findall(r'^\d+\. ', instruction, flags=re.M)) == 10
    assert '制药成本' not in instruction
    assert instruction.count('正文禁止数字') == 1
    assert 'NO_NUMERIC_IN_PROSE' in instruction
    for row in example['output']['elements'].values():
        assert any(ident.startswith('K') for ident in row['evidence_ids'])
        assert not any(ident.startswith(('R', 'M')) for ident in row['evidence_ids'])


@pytest.mark.parametrize('rule,field,replacement', [
    ('NO_NUMERIC_IN_PROSE', 'hypothesis', '制费的三项明细已定位，具体计提与分配原因尚不能确认。'),
    ('PROSE_LENGTH', 'hypothesis', '待核查'),
    ('UNCERTAINTY_REQUIRED', 'hypothesis', '原料单位消耗成本变化已定位，结算计价与实物耗用属于不同口径。'),
    ('RESPONSIBLE_ROLE', 'recommendation', '核对原料成本明细与结算单，复核本期计价和分配口径。'),
    ('VERIFICATION_ACTION', 'recommendation', '请财务部阅读原料成本明细与结算单，了解本期计价和分配口径。'),
    ('UNVERIFIED_CONCLUSION', 'hypothesis', '原料单位消耗成本变化已定位，主要原因是费用归集，具体影响尚不能判断。'),
    ('NO_MARKUP', 'hypothesis', '原料单位消耗成本变化已定位，尚不能确认变化的实际原因。<b>'),
    ('NATURAL_PROSE', 'hypothesis', '证据边界：原料单位消耗成本变化已定位，尚不能确认实际采购原因。'),
    ('ACCOUNTING_CAUSALITY', 'hypothesis', '本期产量减少导致固定成本摊薄，单位费用可能因此下降，实际原因待核查。'),
    ('MECHANISM_DOCUMENT', 'hypothesis', '原料费用变化可能涉及设备故障，实际业务原因尚不能确认。'),
    ('RECORD_NAME_SCOPE', 'recommendation', '请财务部核对返工记录，复核成本明细与当前费用归集口径。'),
    ('ACTION_NOT_GATED_BY_MISSING', 'recommendation', '请财务部核对本期费用归集口径，需补齐结算单后完成核对。'),
    ('NO_INVENTED_AVAILABILITY', 'recommendation', '请财务部核对已提供的结算单，复核成本明细与当前计价口径。'),
])
def test_one_mutation_maps_directly_to_structured_rule(rule, field, replacement):
    example = deepcopy(m2.M2_FEWSHOT)
    example['output']['elements']['材料'][field] = replacement
    result = m2.model_diagnostics(example['output'], example['evidence'])
    assert {item['rule_id'] for item in result} == {rule}, result
    assert all(item['field'] == 'elements.材料.' + field for item in result)
    assert all(item['offending'] and item['expected'] for item in result)
    assert all(set(item) == {'rule_id', 'field', 'offending', 'expected', 'message'} for item in result)
    if rule == 'NO_NUMERIC_IN_PROSE':
        assert result[0]['offending'] == ['三项']


@pytest.mark.parametrize('ref,expected', [('F002', 'EVIDENCE_ELEMENT'), ('missing', 'EVIDENCE_EXISTS')])
def test_cross_element_and_unknown_evidence_are_separate_rules(ref, expected):
    example = deepcopy(m2.M2_FEWSHOT)
    example['output']['elements']['材料']['evidence_ids'] = [ref]
    assert {item['rule_id'] for item in m2.model_diagnostics(example['output'], example['evidence'])} == {expected}


def test_reference_role_cannot_be_laundered_as_cause():
    example = deepcopy(m2.M2_FEWSHOT)
    source = next(row for row in example['evidence'] if row['id'] == 'K001')
    source['kind'] = 'market_reference'
    assert {item['rule_id'] for item in m2.model_diagnostics(example['output'], example['evidence'])} == {'EVIDENCE_KIND'}
    source['kind'], source['evidence_role'] = 'document_basis', 'context_only'
    assert {item['rule_id'] for item in m2.model_diagnostics(example['output'], example['evidence'])} == {'EVIDENCE_AUTHORITY'}


def test_declared_records_are_exact_scoped_names_not_excerpt_guesses():
    sources = [{'id': 'K', 'kind': 'document_basis', 'elements': ['材料'], 'text': '返工记录和审批单'}]
    inventory = action_availability({}, sources, '材料')
    assert inventory['inventory_status'] == 'not_declared'
    assert inventory['available_record_names'] == []
    assert inventory['generic_request_categories'] == list(SAFE_RECORD_CATEGORIES)
    row = {'recommendation': '请财务部核对返工记录，复核已有原料费用归集明细的对应口径。'}
    assert action_diagnostics(row, '材料', inventory)[0]['rule_id'] == 'RECORD_NAME_SCOPE'
    inventory = action_availability({'available_record_names': {'材料': ['返工记录']}}, sources, '材料')
    assert action_diagnostics(row, '材料', inventory) == []
    assert action_availability({'available_record_names': {'人工': ['返工记录']}}, sources, '材料')['available_record_names'] == []


def test_planned_generic_records_do_not_claim_inventory_or_block_current_data():
    availability = action_availability({}, [], '材料')
    row = {'recommendation': '请采购部核对已有成本明细的计价口径，并核查结算单与领退料单。'}
    assert action_diagnostics(row, '材料', availability) == []
    row['missing_evidence'] = ['尚未提供的结算单', '尚未提供的领退料单']
    assert action_diagnostics(row, '材料', availability, benchmark=True) == []


def test_provided_record_is_not_a_missing_record():
    availability = action_availability({'available_record_names': ['结算单']}, [], '材料')
    result = action_diagnostics({'missing_evidence': ['本期结算单']}, '材料', availability, benchmark=True)
    assert [item['rule_id'] for item in result] == ['MISSING_NOT_PROVIDED']


def test_m3_missing_schema_and_zero_branch_are_structured():
    example = deepcopy(m3.M3_FEWSHOT)
    example['output']['elements']['材料']['missing_evidence'] = []
    assert {item['rule_id'] for item in m3.validate_explanations_structured(example['output'], example['evidence'])} == {'MISSING_EVIDENCE_SCHEMA'}
    fact = {'elements': [{'element': '材料', 'unit_gap': 0, 'evidence_id': 'B001'}]}
    example['output']['elements']['材料'] = m3._zero_explanation(fact['elements'][0])
    assert m3.validate_explanations_structured(example['output'], example['evidence'], fact) == []
    example['output']['elements']['材料']['recommendation'] = '不应出现的任务'
    assert {item['rule_id'] for item in m3.validate_explanations_structured(example['output'], example['evidence'], fact)} == {'NO_DIFFERENCE_BRANCH'}


def test_full_count_guard_does_not_ban_unrelated_chinese_words():
    example = deepcopy(m2.M2_FEWSHOT)
    example['output']['elements']['材料']['hypothesis'] = '原料单位消耗成本变化已定位，统一计价与一致口径仍待核查，不能据此认定根因。'
    assert m2.model_diagnostics(example['output'], example['evidence']) == []


def _payload(mode):
    if mode == 'm2':
        return {'product': '示例制造品', 'month': '2026-05', 'facts': {'elements': {key: {'evidence_ids': [f'F{index:03d}']}
                 for index, key in enumerate(('材料', '人工', '制费'), 1)}}, 'elements': {}}
    return {'product': '示例制造品', 'specification': '示例规格', 'month': '2026-05',
            'facts': {'elements': [{'element': key, 'evidence_id': f'B{index:03d}', 'unit_gap': 1}
                       for index, key in enumerate(('材料', '人工', '制费'), 1)]}}


def test_correction_receives_structured_actual_diagnostics_and_unchanged_candidate(contract):
    mode, module, example, validate = contract
    bad = deepcopy(example['output'])
    bad['elements']['制费']['hypothesis'] += '三项'
    pristine_bad = deepcopy(bad)
    calls = []
    def request_fn(instruction, data, **kwargs):
        calls.append((instruction, deepcopy(data)))
        return bad if len(calls) == 1 else deepcopy(example['output'])
    configuration = ModelConfiguration('https://fixture.invalid/v1', 'offline-only', 'not-a-key', True, 40)
    result = module._llm_generate(_payload(mode), example['evidence'], request_fn=request_fn, config=configuration)
    assert len(calls) == 2
    assert result['candidate'] == example['output']
    assert calls[1][1]['previous_candidate'] == pristine_bad == bad
    structured = calls[1][1]['validation_diagnostics']
    assert structured == result['attempts'][0]['validation_diagnostics']
    assert any(item['rule_id'] == 'NO_NUMERIC_IN_PROSE' and item['offending'] == ['三项'] for item in structured)
    assert calls[1][1]['validation_errors'] == legacy_errors(structured)
    assert 'rule_id' in calls[1][0]
    assert result['correction']['status'] == 'validated'
    assert 'action_availability' in calls[0][1]['tasks_by_element']['材料']
    assert 'domain_descriptors' in calls[0][1]


def test_provider_failure_is_not_retried(contract):
    mode, module, example, _ = contract
    calls = []
    def request_fn(*args, **kwargs):
        calls.append(1)
        raise ModelUnavailable('private credentials must not escape')
    configuration = ModelConfiguration('https://fixture.invalid/v1', 'offline-only', 'not-a-key', True, 40)
    result = module._llm_generate(_payload(mode), example['evidence'], request_fn=request_fn, config=configuration)
    assert len(calls) == 1 and len(result['attempts']) == 1
    assert result['candidate'] is None
    assert not result['correction']['attempted']
    assert 'private credentials' not in json.dumps(result)


def test_two_rejections_never_rewrite_or_make_third_call(contract):
    mode, module, example, _ = contract
    candidate = deepcopy(example['output'])
    candidate['elements']['制费']['hypothesis'] += '三项'
    original, calls = deepcopy(candidate), []
    def request_fn(*args, **kwargs):
        calls.append(1)
        return candidate
    result = module._llm_generate(_payload(mode), example['evidence'], request_fn=request_fn,
        config=ModelConfiguration('https://fixture.invalid/v1', 'offline-only', 'not-a-key', True, 40))
    assert len(calls) == 2 and result['candidate'] == candidate == original
    assert result['correction']['status'] == 'rejected'
    assert all(not row['used'] for row in result['attempts'])


def test_direction_diagnostics_use_configured_subjects():
    context = {'domain_descriptors': {'factories': {'home': '装配甲厂', 'peer': '装配乙厂'}},
        'tasks_by_element': {'材料': {'observed_comparison': {'home_unit_cost': 2},
            'required_observations': {'home_vs_peer': '高于', 'home_label': '本厂', 'peer_label': '对标厂'}}}}
    row = {'elements': {'材料': {'hypothesis': '本厂单位费用高于对标厂，尚不能确认业务原因。', 'recommendation': '请财务部核查已有原料成本明细。'}}}
    assert observation_diagnostics(row, context) == []
    row['elements']['材料']['hypothesis'] = '装配甲厂单位费用高于装配乙厂，尚不能确认业务原因。'
    assert observation_diagnostics(row, context) == []
    row['elements']['材料']['hypothesis'] = '本厂单位费用低于对标厂，尚不能确认业务原因。'
    assert observation_diagnostics(row, context)[0]['rule_id'] == 'COMPARISON_DIRECTION'


def test_provider_trace_capture_keeps_observed_identity_usage_and_low_temperature(contract):
    from attribution_runtime import sanitize_model_calls
    from enterprise.model_gateway import _emit_trace
    mode, module, example, _ = contract
    observed = {'schema': 'enterprise-model-call/1.0', 'call_id': 'offline-call',
                'task': 'attribution' if mode == 'm2' else 'benchmark', 'model': 'actual-response-model',
                'requested_model': 'requested-model', 'request_id': 'provider-request',
                'completion_id': 'provider-completion', 'source': 'provider_response',
                'status': 'returned_json', 'request_attempted': True, 'response_observed': True,
                'request_id_source': 'http_request_id', 'temperature': .1,
                'usage': {'prompt_tokens': 123, 'completion_tokens': 45, 'total_tokens': 168}}
    def request_fn(*args, **kwargs):
        _emit_trace(observed)
        return deepcopy(example['output'])
    result = module._llm_generate(_payload(mode), example['evidence'], request_fn=request_fn,
        config=ModelConfiguration('https://fixture.invalid/v1', 'requested-model', 'not-a-key', True, 40))
    assert len(result['attempts']) == 1
    assert result['model_calls'] == sanitize_model_calls([observed], secret='not-a-key')
    assert result['model_calls'][0]['model'] == 'actual-response-model'
    assert result['model_calls'][0]['usage'] == observed['usage']
    assert result['model_calls'][0]['temperature'] == .1


def test_m3_fallback_uses_shared_action_without_missing_completion_prerequisite(monkeypatch):
    from enterprise.benchmark import build_benchmark
    from decimal import Decimal
    import pandas as pd
    def cost_row(factory, material, labor, overhead, volume):
        elements = [Decimal(str(value)) for value in (material, labor, overhead)]
        unit = sum(elements)
        return {'工厂': factory, '产品名称': '测试产品', '产品规格': 'S', '月份': '2026-05',
                '产量(盒)': volume, '直接材料(元/盒)': str(elements[0]), '直接人工(元/盒)': str(elements[1]),
                '制造费用(元/盒)': str(elements[2]), '单位成本(元/盒)': str(unit),
                '总成本(元)': str(unit * Decimal(str(volume))), '_source_file': f'{factory}.csv'}
    tables = {'cost26': pd.DataFrame([cost_row('中药一厂', 7, 2, 3, 100)]),
              'erchang26': pd.DataFrame([cost_row('中药二厂', 8, 3, 4, 100)]),
              'material': pd.DataFrame()}
    evidence = [{'id': 'K101', 'kind': 'document_basis', 'evidence_role': 'document_basis',
                 'elements': ['材料'], 'text': '材料耗用应按批次核对，原料损耗影响材料成本。',
                 'source': {'file': 'synthetic_process.txt'},
                 'scope': {'product': '测试产品', 'specification': 'S', 'month': '2026-05'}}]
    facts = build_benchmark('测试产品', 'S', '2026-05', tables)
    result = m3.generate_benchmark_analysis('测试产品', 'S', '2026-05', tables,
                                           evidence=evidence, use_llm=False)
    assert result['facts'] == facts
    assert not result['used_llm'] and result['model_explanations'] is None
    assert '需补齐' not in result['text'] and '后完成核对' not in result['text']
    assert result['followup_criteria']
    assert all(section['recommendation'] == section['immediate_action'] for section in result['sections'])
    assert 'K101' in result['report_explanations']['elements']['材料']['evidence_ids']
    assert all(not ident.startswith('K') for element in ('人工', '制费')
               for ident in result['report_explanations']['elements'][element]['evidence_ids'])


def test_coded_tied_object_names_require_ids_without_forcing_numeric_prose():
    context = {'tasks_by_element': {'材料': {'focus': {'is_tied': True,
        'tied_objects': ['SIMULATION P4 Binder', 'SIMULATION P4 Powder Blend'],
        'tied_evidence_ids': ['F001', 'F002']}}}}
    candidate = {'elements': {'材料': {'hypothesis': '并列材料对象的核算影响相当，实际业务原因尚不能确认。',
        'recommendation': '请采购部核对已有材料明细的计价口径。', 'evidence_ids': ['F001', 'F002']}}}
    assert observation_diagnostics(candidate, context) == []
    candidate['elements']['材料']['evidence_ids'].pop()
    assert observation_diagnostics(candidate, context)[0]['rule_id'] == 'TIED_CODED_OBJECTS'
    candidate['elements']['材料']['evidence_ids'].append('F002')
    candidate['elements']['材料']['hypothesis'] = '材料对象的核算影响相当，实际业务原因尚不能确认。'
    assert observation_diagnostics(candidate, context)[0]['rule_id'] == 'TIED_CODED_OBJECTS'


def test_comparative_guard_accepts_exact_decimal_string_but_not_invalid_direction():
    sentence = '一厂人工成本低于二厂，可能由于本厂返工工时增加导致，待核查。'
    assert m3.validate_comparative_mechanisms(sentence, '人工', unit_gap='-1.25')
    assert m3.validate_comparative_mechanisms(sentence, '人工', unit_gap='NaN')
    fact = {'element': '材料', 'unit_gap': '0.000', 'evidence_id': 'B001'}
    example = deepcopy(m3.M3_FEWSHOT)
    example['output']['elements']['材料'] = m3._zero_explanation(fact)
    assert m3.validate_explanations_structured(example['output'], example['evidence'], {'elements': [fact]}) == []


def test_record_parser_cannot_hide_record_before_later_action_and_does_not_match_units():
    availability = action_availability({}, [], '材料')
    bad = {'recommendation': '请财务部核对返工记录后复核单位成本明细与原料单耗口径。'}
    assert action_diagnostics(bad, '材料', availability)[0]['rule_id'] == 'RECORD_NAME_SCOPE'
    good = {'recommendation': '请财务部核对单位成本和原料单价口径，表明金额差额，再复核现有数据。'}
    assert action_diagnostics(good, '材料', availability) == []


@pytest.mark.parametrize('rule,mutate', [
    ('ROOT_SCHEMA', lambda item: item.update(elements=[])),
    ('ELEMENT_COVERAGE', lambda item: item['elements'].pop('人工')),
    ('ELEMENT_FIELDS', lambda item: item['elements']['材料'].update(extra=True)),
    ('EVIDENCE_UNIQUE', lambda item: item['elements']['材料'].update(evidence_ids=['F001', 'F001'])),
])
def test_schema_rules_have_distinct_structured_diagnostics(rule, mutate):
    sample = deepcopy(m2.M2_FEWSHOT)
    mutate(sample['output'])
    assert {item['rule_id'] for item in m2.model_diagnostics(sample['output'], sample['evidence'])} == {rule}


def test_duplicate_source_and_context_exclusions_emit_specific_rule():
    sample = deepcopy(m2.M2_FEWSHOT)
    duplicate = sample['evidence'] + [deepcopy(sample['evidence'][0])]
    assert m2.model_diagnostics(sample['output'], duplicate)[0]['rule_id'] == 'SOURCE_ID_UNIQUE'
    sample['input']['tasks_by_element']['材料']['eligible_evidence_ids'] = ['F001']
    result = m2.model_diagnostics(sample['output'], sample['evidence'], context=sample['input'])
    assert {item['rule_id'] for item in result} == {'EVIDENCE_CONTEXT_SCOPE'}


@pytest.mark.parametrize('rule,task,field,text', [
    ('NET_OFFSET', {'required_observations': {'home_vs_peer': '高于', 'net_effect': '反向抵消'},
                    'observed_comparison': {'home_unit_cost': 2}}, 'hypothesis', '本厂本项单位费用高于对标厂，实际原因尚不能确认。'),
    ('ACTION_FOCUS', {'required_observations': {'home_vs_peer': '高于', 'priority_objects': ['折旧费']},
                      'observed_comparison': {'home_unit_cost': 2}}, 'recommendation', '请财务部核对本期动力费明细与费用分摊表。'),
    ('TIED_FOCUS', {'focus': {'is_tied': True, 'tied_objects': ['折旧费', '动力费']}}, 'hypothesis', '折旧费变化已定位，实际业务原因尚不能确认。'),
    ('LABOR_OFFSET', {'observed_labor': {'hours_effect': '-10', 'rate_effect': '20',
                       'amount_bridge_observation': '正负抵消后总额增加'}}, 'hypothesis', '人工变化已定位，实际业务原因尚不能确认。'),
])
def test_observation_rules_point_to_actual_field(rule, task, field, text):
    row = {'hypothesis': '本厂本项单位费用高于对标厂，尚不能确认具体业务原因。',
           'recommendation': '请财务部核对折旧明细与费用分摊表。'}
    row[field] = text
    result = observation_diagnostics({'elements': {'制费': row}}, {'tasks_by_element': {'制费': task}})
    assert {item['rule_id'] for item in result} == {rule}
    assert result[0]['field'] == 'elements.制费.' + field


def test_legacy_validators_still_return_strings():
    for module, sample, validate in [(m2, m2.M2_FEWSHOT, m2._model_errors), (m3, m3.M3_FEWSHOT, m3.validate_explanations)]:
        bad = deepcopy(sample['output'])
        bad['elements']['材料']['hypothesis'] += '3%'
        result = validate(bad, sample['evidence'])
        assert result and all(isinstance(item, str) for item in result)
        assert any('自行书写的数值' in item for item in result)
