"""Bounded, numeric-aware analytical context; not a second calculation engine.

Only allowlisted values from server-calculated facts reach the model. Original
sources, rows, hashes and raw business notes stay in the audit evidence. Reading
numbers is allowed; numeric report prose is still rendered from validated facts.
"""
from __future__ import annotations
from decimal import Decimal, InvalidOperation
from copy import deepcopy
import math


def _number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    if not number.is_finite():
        return None
    # Model-visible precision only. The original exact values are not modified.
    result = float(number)
    return result if math.isfinite(result) else None


def _numbers(row, names):
    return {name: _number(row.get(name)) for name in names}


def _magnitude(value):
    number = _number(value)
    return abs(number) if number is not None else -1


def _units(payload):
    """Use only operator-validated domain descriptors, never arbitrary unit text."""
    profile = payload.get('domain_config')
    if profile is None:
        return {'amount': '元', 'quantity': '盒', 'home': '一厂', 'peer': '二厂'}
    from enterprise.domain_profiles import validate_domain_profile
    profile = validate_domain_profile(profile)
    definition = next((row for row in profile['products']
                       if row['name'] == payload.get('product')
                       and row['specification'] == payload.get('specification')), None)
    if definition is None:
        raise ValueError('分析产品规格未在受控领域配置中登记')
    return {'amount': profile['currency'],
            'quantity': definition.get('reporting_unit', profile['reporting_unit']),
            'home': '本厂', 'peer': '对标厂'}


def attribution_numeric_context(payload, base):
    """Upgrade qualitative context without sending arbitrary payload fields."""
    result = deepcopy(base)
    result['schema_version'] = 'attribution-model-context/2.1'
    units = _units(payload)
    facts = payload.get('facts') or {}
    import re
    result['analysis_period'] = {key: value for key, value in (
        ('current_month', payload.get('month')), ('previous_month', facts.get('previous_month')))
        if isinstance(value, str) and re.fullmatch(r'\d{4}-(0[1-9]|1[0-2])', value)}
    result['analysis_question'] = ('解释所选产品本期相对上期的成本变化：先按已计算差额定位对象和主要核算驱动，'
        '再用适用知识说明核查路径，最后给出负责部门与基于已有资料可立即执行的核查动作；缺失依据单列，统一完成要求由程序展示。')
    result['numeric_contract'] = {
        'input': 'server_calculated_read_only', 'output': 'program_renders_numbers',
        'amount_unit': units['amount'], 'unit_cost_unit': units['amount'] + '/' + units['quantity'],
        'volume_unit': units['quantity'],
        'contribution_denominator': '总成本金额净变动，不是单位成本变动',
        'material_semantics': '单位消耗成本不是采购单价；无实际实物耗用/采购计价则不能拆采购量价',
        'knowledge_semantics': '工艺标准或机制不是本期实际事件；不得为了引用而强套不相关机制',
    }
    result['observed_totals'] = {
        'current': _numbers(facts.get('current') or {}, ('volume', 'unit_cost', 'total_cost')),
        'previous': _numbers(facts.get('previous') or {}, ('volume', 'unit_cost', 'total_cost')),
        **_numbers(facts, ('amount_delta', 'unit_cost_change')),
    }
    for element, task in result.get('tasks_by_element', {}).items():
        fact = facts.get('elements', {}).get(element) or {}
        ids = set(task.get('accounting_fact_ids', []))
        task['observed_costs'] = _numbers(fact, ('unit_before','unit_after','unit_delta','mom_pct',
            'amount_delta','contribution','volume_effect','unit_effect'))
        details = [row for row in fact.get('detail', []) if isinstance(row,dict)
                   and row.get('evidence_id') in ids]
        # Carry the same admitted facts, independently rank unit vs amount effect.
        details.sort(key=lambda row: (-_magnitude(row.get('unit_effect')), -_magnitude(row.get('amount_delta'))))
        visible = {row['id']:row for row in task.get('detail', [])}
        task['observed_details'] = [
            {'evidence_id':row['evidence_id'], 'name':visible.get(row['evidence_id'],{}).get('name','未标注项目'),
             **_numbers(row, ('unit_before','unit_after','unit_delta','amount_before','amount_after',
                              'amount_delta','volume_effect','unit_effect'))}
            for row in details[:4]]
        focus = task['observed_details'][0] if task['observed_details'] else None
        tied = [row for row in task['observed_details'] if focus is not None
                and _magnitude(row['unit_effect']) == _magnitude(focus['unit_effect'])]
        task['detail_window'] = {'provided_count':len(details), 'shown_count':len(task['observed_details']),
                                 'complete':len(details)<=4,
                                 'boundary':'所列为核查优先级窗口，不是全部项目；不得把窗口长度写成主材/费用总数'}
        task['focus'] = {'selection':'largest_absolute_unit_cost_effect_among_admitted_details',
                         'evidence_id':focus['evidence_id'] if focus else (task.get('accounting_fact_ids') or [None])[0],
                         'name':focus['name'] if focus else element,
                         'tied_objects':[row['name'] for row in tied],
                         'tied_evidence_ids':[row['evidence_id'] for row in tied],
                         'is_tied':len(tied)>1 and _magnitude(focus['unit_effect'])>0,
                         'boundary':'核算驱动排序，不证明业务事件；并列项必须并列说明，不把金额排序用作单位影响独占主因'}
        if element == '人工':
            labor = fact.get('labor_factors') or {}
            if labor.get('available'):
                task['observed_labor'] = {
                    **_numbers(labor, ('hours_before','hours_after','hours_delta','cost_per_hour_before',
                        'cost_per_hour_after','hours_effect','rate_effect','hours_per_box_before',
                        'hours_per_box_after','output_per_hour_before','output_per_hour_after',
                        'unit_hours_effect','unit_rate_effect')),
                    'evidence_id':labor.get('evidence_id'),
                    'boundary':'归集人工费用/小时不是个人时薪；汇总产出/工时不是已证明的岗位效率'}
                def bridge_observation(hours, rate, *, unit=False):
                    hours, rate = _number(hours), _number(rate)
                    if hours is None or rate is None or not hours*rate < 0:
                        return None
                    noun = ('每' + units['quantity'] + '工时') if unit else '总工时'
                    net = hours + rate
                    net_text = '增加' if net > 0 else '减少' if net < 0 else '不变'
                    bigger = noun if abs(hours) > abs(rate) else '小时归集费用' if abs(rate) > abs(hours) else '双方'
                    return (noun + ('增加' if hours>0 else '减少') + '的影响与小时归集费用'
                        + ('增加' if rate>0 else '减少') + '的影响方向相反；两者抵消后'
                        + ('单位人工费用' if unit else '人工总金额') + net_text + '，绝对影响较大项为' + bigger + '。')
                task['observed_labor']['amount_bridge_observation'] = bridge_observation(labor.get('hours_effect'), labor.get('rate_effect'))
                task['observed_labor']['unit_bridge_observation'] = bridge_observation(labor.get('unit_hours_effect'), labor.get('unit_rate_effect'), unit=True)
        task['reasoning_priority'] = ('先解释observed_costs、focus及反向抵消；不能把任意可用工艺句作为最大明细的原因。'
            '没有与focus匹配的机制时，明确核算已定位但业务原因待核查；仍须给具体可执行建议。'
            '劳动工时减少或单位产出工时减少时，不用返工增加单独解释人工成本增加。'
            '折旧费是最大驱动时优先核查计提/资产/分配，蒸汽机制只能用于另列的动力费，不得偷换。')
    return result


def observation_alignment_errors(candidate, context):
    """Check explicit observed-direction/object coverage, not semantic accuracy.

    Only used by the numeric-aware live worker. A passing result is not a human
    attribution score and cannot certify the truth of a business hypothesis.
    """
    import re
    errors = []
    if not isinstance(candidate, dict) or not isinstance(candidate.get('elements'), dict):
        return errors
    for element, task in context.get('tasks_by_element', {}).items():
        row = candidate['elements'].get(element)
        if not isinstance(row, dict) or row.get('claim_type') == 'no_difference':
            continue
        hypothesis = row.get('hypothesis', '')
        advice = row.get('recommendation', '')
        if not isinstance(hypothesis, str) or not isinstance(advice, str):
            continue
        observation = task.get('required_observations')
        if observation and task.get('observed_comparison', {}).get('home_unit_cost') is not None:
            expected = observation['home_vs_peer']
            if expected in ('高于', '低于') and not re.search(r'一厂.{0,25}' + expected + r'.{0,8}二厂', hypothesis):
                errors.append(element + '需先明确一厂本项单位费用' + expected + '二厂，再说明机制，不得仅复述工艺。')
            if observation['net_effect'] == '反向抵消' and '抵消' not in hypothesis:
                errors.append(element + '本项与净差额反向，需说明抵消其他要素形成的差额，不能只复述工艺。')
            names = observation.get('priority_objects', [])
            if element == '制费' and any('折旧' in name for name in names) and '折旧' not in advice:
                errors.append(element + '已有主要折旧费用对象，建议须核对折旧计提与分配，不能只查蒸汽。')
        focus = task.get('focus') or {}
        if focus.get('is_tied'):
            names = focus.get('tied_objects', [])
            missing = [name for name in names if name not in hypothesis
                       and re.split(r'[（(]', name)[0].rstrip('费') not in hypothesis]
            if missing:
                errors.append(element + '单位成本影响存在并列项，假设须同时衔接：' + '、'.join(names) + '，不能把排序首项当独占主因。')
        if task.get('observed_labor'):
            labor = task['observed_labor']
            hours, rate = labor.get('hours_effect'), labor.get('rate_effect')
            if hours and rate and hours*rate < 0 and not re.search(r'抵消|超过|大于|小于|覆盖|相反', hypothesis):
                errors.append(element + '工时影响与小时归集费用影响相反，须解释抵消与净方向，不能列变化后改套返工机制。'
                              + str(labor.get('amount_bridge_observation') or '')
                              + str(labor.get('unit_bridge_observation') or ''))
    return errors


def benchmark_numeric_context(payload, base):
    """Keep home/peer, amount and unit differences separate in the actual prompt."""
    result=deepcopy(base)
    result['schema_version']='benchmark-model-context/2.1'
    units = _units(payload)
    result['numeric_contract'] = {'amount_unit': units['amount'],
        'unit_cost_unit': units['amount'] + '/' + units['quantity'], 'volume_unit': units['quantity']}
    result['analysis_question']=('按找差异→拆结构→拆原因分析同产品同规格同月两厂成本；'
        '保留低于/高于方向和抵消项，先给能够计算的结构原因，再给有证据的机制核查及可执行建议。')
    facts=payload.get('facts') or {}
    result['comparison_totals']={
        'home_factory':facts.get('home_factory'), 'peer_factory':facts.get('peer_factory'),
        'home':_numbers(facts.get('home') or {},('volume','unit_cost','total_cost')),
        'peer':_numbers(facts.get('peer') or {},('volume','unit_cost','total_cost')),
        **_numbers(facts,('unit_gap','gap_pct','normalized_amount')),
        'formula':f"标准化金额差=({units['home']}单位成本-{units['peer']}单位成本)×{units['home']}当月产量",
        'boundary':'实际总额受两厂产量影响，不是效率对比；标准化差额不是已实现节约',
    }
    rows={row.get('element'):row for row in facts.get('elements',[]) if isinstance(row,dict)}
    for element,task in result.get('tasks_by_element',{}).items():
        row=rows.get(element) or {}
        task['observed_comparison']={**_numbers(row,('home_unit_cost','peer_unit_cost','unit_gap',
            'gap_pct','normalized_amount','contribution_pct','home_share_pct','peer_share_pct')),
            'evidence_id':row.get('evidence_id'),
            'unit':units['amount'] + '/' + units['quantity'] + '；normalized_amount为' + units['amount'] + '；贡献度为净标准化差额贡献%'}
        branch=facts.get('paired_drilldown',{}).get(element,{})
        label_rows={item.get('name'):item for item in task.get('detail',[])}
        details=[]
        eligible=set(task.get('eligible_evidence_ids',[]))
        for detail in branch.get('rows',[]):
            if not isinstance(detail,dict) or detail.get('name') not in label_rows:
                continue
            def side(value):
                if not isinstance(value,dict): return None
                return {**_numbers(value,('unit_cost','amount','volume')),
                        'metrics':_numbers(value.get('metrics') or {},
                            ('hours','hours_per_box','cost_per_hour','output_per_hour')),
                        'metric_units':{'hours':'小时','hours_per_box':'小时/' + units['quantity'],
                            'cost_per_hour':'归集人工' + units['amount'] + '/小时（非个人工资）','output_per_hour':units['quantity'] + '/小时（汇总指标）'},
                        'evidence_id':value.get('evidence_id') if value.get('evidence_id') in eligible else None}
            details.append({'name':label_rows[detail['name']]['name'],'status':detail.get('status'),
                            'home':side(detail.get('home')),'peer':side(detail.get('peer')),
                            **_numbers(detail,('unit_gap','normalized_amount'))})
        task['observed_paired_details']=details[:4]
        task['detail_window']={'provided_count':len(branch.get('rows', [])), 'shown_count':len(details[:4]),
                              'complete':len(branch.get('rows', []))<=len(details[:4]),
                              'boundary':'优先级窗口不是全部项目，不把窗口长度说成全部主材数量'}
        gap, total_gap = _number(row.get('unit_gap')), _number(facts.get('unit_gap'))
        task['required_observations'] = {
            'home_label': units['home'], 'peer_label': units['peer'],
            'home_vs_peer': '高于' if gap is not None and gap > 0 else '低于' if gap is not None and gap < 0 else '相同',
            'net_effect': '反向抵消' if gap and total_gap and gap*total_gap < 0 else '同向贡献',
            'priority_objects': [item['name'] for item in details[:2]],
            'paired_detail_available': any(item['status']=='paired' for item in details),
            'writing_order':f"先说明{units['home']}单位费用高于或低于{units['peer']}、该项同向或反向影响；再说明已有明细能定位的核查对象与缺少的配对记录；最后才是文档可支持的局部机制。",
            'action':'建议要覆盖priority_objects；有折旧与动力时均须核对，不只因蒸汽原文而忽略折旧。'}
        task['reasoning_priority']=('材料/人工/制费的高低和份额变化是已计算事实；贡献度负值可表示抵消，不能改为绝对值。'
            '只有两厂真实配对明细才能归属跨厂项目差；仅一厂明细可定核查对象，不证明另一厂价格/耗用/效率。'
            '不把一厂环比变化当作同期跨厂原因；不以通用工艺机制猜测低成本厂实际更优。')
    return result
