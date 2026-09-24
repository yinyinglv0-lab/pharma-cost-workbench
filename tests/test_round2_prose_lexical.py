"""Pure lexical regression fixtures; no model, worker, source data or network."""
import pytest

from enterprise.analysis_contract import (
    SAFE_RECORD_CATEGORIES,
    _record_mentions,
    action_availability,
    action_diagnostics,
    causal_diagnostics,
    prose_diagnostics,
    role_action_diagnostics,
)


@pytest.mark.parametrize('text', [
    '源记录',
    '但源记录',
    '并同步调取其源记录',
])
def test_exact_generic_source_captures_are_not_new_voucher_titles(text):
    assert _record_mentions(text, SAFE_RECORD_CATEGORIES) == []


@pytest.mark.parametrize('text', [
    '维修源记录',
    '返工源记录',
    '但维修源记录',
    '并同步调取其维修源记录',
    '源记录中的返工记录',
    '采购源记录',
    '但源记录审批单',
    '工时记录',
    '出勤记录',
    '维修工单',
])
def test_generic_source_exception_does_not_allow_compound_or_unknown_vouchers(text):
    assert _record_mentions(text, SAFE_RECORD_CATEGORIES) == [text]
    diagnostics = action_diagnostics(
        {'missing_evidence': [text]}, '材料', action_availability({}, [], '材料'))
    assert {item['rule_id'] for item in diagnostics} == {'RECORD_NAME_SCOPE'}


def test_generic_source_phrase_preserves_independent_inventory_guard():
    row = {'recommendation': '请财务部核对已有成本明细，并同步调取其源记录；核对已提供的结算单。'}
    diagnostics = action_diagnostics(row, '材料', action_availability({}, [], '材料'))
    assert {item['rule_id'] for item in diagnostics} == {'NO_INVENTED_AVAILABILITY'}


@pytest.mark.parametrize('term', [
    '无法确认', '无法认定', '不能据此认定', '不能据此确认', '不证明',
])
def test_closed_epistemic_boundaries_are_recognized_without_other_exemptions(term):
    row = {
        'hypothesis': f'甲料单位消耗成本差异已定位，现有归集数据{term}价格或耗用层面的实际业务原因。',
        'recommendation': '请财务部核对已有材料成本明细，复核费用归集口径。',
    }
    assert role_action_diagnostics(row, '材料') == []
    assert prose_diagnostics(row['hypothesis'], 'elements.材料.hypothesis') == []
    assert causal_diagnostics(row['hypothesis'], 'elements.材料.hypothesis') == []
    assert action_diagnostics(row, '材料', action_availability({}, [], '材料')) == []


@pytest.mark.parametrize('hypothesis', [
    '甲料与乙料均呈现本厂单位费用高于对标厂，但源记录仅支持归集金额与产出匹配，无法确认价格、耗量或收率层面的物理动因。',
    '工时台账与归集比率仅反映会计分摊结果，不证明岗位配置、技能水平或现场作业效率差异。',
])
def test_bounded_provider_failure_shapes_have_clear_evidence_boundaries(hypothesis):
    row = {'hypothesis': hypothesis,
           'recommendation': '请财务部核对已有费用归集明细，复核同口径产出。'}
    assert role_action_diagnostics(row, '材料') == []
    assert action_diagnostics(row, '材料', action_availability({}, [], '材料')) == []


@pytest.mark.parametrize('hypothesis', [
    '甲料成本差异已经定位，费用归集与实际耗用属于不同业务口径。',
    '甲料成本差异主要原因是采购价格上涨，归集金额证明了实际业务原因。',
])
def test_affirmative_or_merely_descriptive_text_still_requires_uncertainty(hypothesis):
    row = {'hypothesis': hypothesis,
           'recommendation': '请财务部核对已有费用归集明细，复核计价口径。'}
    assert {item['rule_id'] for item in role_action_diagnostics(row, '材料')} == {'UNCERTAINTY_REQUIRED'}


@pytest.mark.parametrize('term', [
    '无法确认', '无法认定', '不能据此认定', '不能据此确认', '不证明',
])
def test_new_epistemic_terms_do_not_bypass_role_or_known_false_causality(term):
    row = {
        'hypothesis': f'本期产量减少导致固定成本摊薄，单位费用因此下降，现有数据{term}其他业务原因。',
        'recommendation': '采购合同核对现有成本明细，复核归集金额与产出口径。',
    }
    assert {item['rule_id'] for item in role_action_diagnostics(row, '制费')} == {'RESPONSIBLE_ROLE'}
    assert any(item['rule_id'] == 'ACCOUNTING_CAUSALITY' for item in
               causal_diagnostics(row['hypothesis'], 'elements.制费.hypothesis'))


def test_clear_boundary_does_not_authorize_asserted_cause_or_numeric_prose():
    hypothesis = '甲料的主要原因是采购价格上涨，现有数据无法确认其他经营原因，差额为12元。'
    assert {item['rule_id'] for item in prose_diagnostics(
        hypothesis, 'elements.材料.hypothesis')} == {'NO_NUMERIC_IN_PROSE', 'UNVERIFIED_CONCLUSION'}
