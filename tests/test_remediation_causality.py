"""Regression and negative controls for the observed validated causal error."""
from copy import deepcopy
from decimal import Decimal
import pytest

from enterprise.causal_guard import validate_cost_causality
from attribution_gen import _model_errors


@pytest.mark.parametrize('text', [
    '人工单位成本下降可能源于产量减少导致固定人工分摊降低，实际原因待核查。',
    '制费单位成本下降可能由产量减少导致固定费用分摊降低，仍需核查。',
    '生产部应核查产量减少被动摊薄对单位人工成本的影响。',
    '减产使得每盒固定费用下降，实际原因待核查。',
    '产出量缩减带来固定工资负担减轻，可能解释本期单位成本下降。',
    '本期产量减少导致固定成本摊薄，实际原因待核查。',
    '不能确认原因，但产量减少导致固定人工分摊降低。',
    '产量减少导致固定人工分摊降低，不能确认采购价格。',
    '制费单位成本下降主要受产量减少影响。',
])
def test_wrong_direction_rejected_even_with_uncertainty(text):
    assert validate_cost_causality(text)
    # The premise underlying every volume-only claim gives the opposite sign.
    assert Decimal('84800') / Decimal('35000') > Decimal('84800') / Decimal('40000')


@pytest.mark.parametrize('text', [
    '不能将单位费用下降直接解释为产量减少导致固定成本摊薄，实际原因仍待核查。',
    '产量减少不能据此解释为固定费用摊薄。',
    '不能仅凭产量减少认定固定人工分摊降低。',
    '本期产量减少导致总金额下降，单位成本另行分析。',
    '产量减少，人工单位成本下降，两项变化需结合工资归集凭证核查。',
    '在固定支出总额不变时，产量减少会提高每盒分摊。',
    '产量增加导致固定费用摊薄，但实际固定支出总額仍需核对。',
    '财务部应核对本期工资归集和每工时费用，不能把费用下降当作效率改善。',
])
def test_mathematically_valid_observations_and_denials_not_rejected(text):
    assert validate_cost_causality(text) == []


def _fixture():
    sources=[{'id':f'F{i}', 'kind':'accounting_fact', 'elements':[element]} for i,element in enumerate(('材料','人工','制费'))]
    candidate={'elements':{element:{'hypothesis':'现有核算仅反映费用差异，实际经营原因尚不能确认。',
        'recommendation':'财务部应核对本期费用原始凭证与分配基数，复核归集期间。',
        'evidence_ids':[f'F{i}']} for i,element in enumerate(('材料','人工','制费'))}}
    return sources,candidate


@pytest.mark.parametrize('field', ['hypothesis','recommendation'])
def test_every_model_prose_field_obeys_same_guard(field):
    sources,candidate=_fixture()
    assert _model_errors(candidate,sources)==[]
    candidate['elements']['人工'][field]='生产部应核查产量减少被动摊薄导致固定人工分摊降低的可能影响。'
    assert any('不能将产量减少' in e for e in _model_errors(candidate,sources))


def test_factual_efficiency_decline_is_a_structured_constraint():
    facts={'elements':{'人工':{'labor_factors':{'available':True,'output_per_hour_change_pct':-12.5}}}}
    assert validate_cost_causality('人工单位成本下降可能源于总体效率提升。',facts)
    assert not validate_cost_causality('现有数据不支持总体效率提升。',facts)


def test_available_hours_are_not_denied_but_original_attendance_can_be_missing():
    facts={'labor_factors':{'available':True,'output_per_hour_change_pct':None}}
    assert validate_cost_causality('由于缺少工时数据，暂时无法拆解。',facts)
    assert not validate_cost_causality('仍需核查员工原始考勤和加班审批记录。',facts)
    assert not validate_cost_causality('当前并非缺少工时数据，仍需核对原始记录。',facts)
    assert not validate_cost_causality('缺少工时数据。',{'labor_factors':{'available':False}})
    assert not validate_cost_causality('两厂人工单位成本不同。',{'elements':[{'element':'人工'}]})
