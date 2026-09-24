"""Reading actions tied to observed drivers, without altering model claims."""
from copy import deepcopy
from decimal import Decimal
import re

from .claims import default_actions


def _driver_names(facts, element):
    details = facts['elements'][element].get('details', [])
    names = []
    for key in ('amount_delta', 'unit_effect'):
        comparable = [row for row in details if row.get(key) is not None and Decimal(str(row[key])) != 0]
        if comparable:
            row = max(comparable, key=lambda item: abs(Decimal(str(item[key]))))
            if row['name'] not in names:
                names.append(row['name'])
    return names


def _detail_action(facts, element, name):
    action = default_actions(facts, element, obj=name)[0]
    if element != '制费' or '折旧' in name:
        return action
    if '检验' in name:
        action.update(department='质量部、财务部',
            action='核对检验批次、检验项目及费用计提记录，区分批次与产量变化，复核费用期间归属和分配基数',
            documents=['检验批次及项目记录', '检验耗材与外检结算凭证', '检验费用计提及分配底稿'])
    elif any(word in name for word in ('动力', '水电', '能源')):
        return action
    elif '人工' in name:
        action.update(department='生产部、财务部',
            action='核对间接岗位工时、工资计提及分配记录，复核费用期间归属和分配基数，登记未解释差额',
            documents=['间接岗位工时及考勤记录', '工资计提与分配记录', '制造费用分配底稿'])
    else:
        action.update(action='核对该费用分项的原始凭证、计提记录及归属期间，复核分配基数与产出记录，登记未解释差额',
                      documents=['费用分项原始凭证', '期间计提及结算记录', '分配基数与产出记录'])
    return action


def complete_reading_actions(facts, element, adopted_actions):
    """Keep validated actions; add missing largest amount/unit drivers first.

    These are program-authored requests for verification, never new model claims
    or assertions of actual business causes. The model request/contract is intact.
    """
    original = {action['object']: deepcopy(action) for action in adopted_actions}
    result, supplements = [], []
    for name in _driver_names(facts, element):
        if name in original:
            result.append(original.pop(name))
        else:
            result.append(_detail_action(facts, element, name))
            supplements.append({'object': name, 'origin': 'program_observed_driver',
                                'basis': 'largest_absolute_amount_delta_or_unit_effect',
                                'asserts_business_cause': False})
    result.extend(original.values())
    return result, supplements


def action_core(action):
    """Project an admitted action for reading; keep the original audit object.

    Only standalone boilerplate clauses are omitted. Object-specific checks,
    voucher directions and cautions remain; no model text or audit fields change.
    """
    text = action['action'].strip().rstrip('。')
    common = re.compile(
        r'^(?:并|再|同时|逐项|统一)*(?:'
        r'(?:形成|编制|提交|交付|输出|整理).*(?:核对表|差异表|对照表|清单|交付物)|'
        r'(?:与|按)(?:已有|现有|原始|全部|各项)*(?:总账|汇总|明细|产品归集|归集金额|原始凭证|原凭证).*勾稽.*|'
        r'(?:单列|登记|说明|记录)(?:未闭合|未解释|未配对|未取得|差额|缺口).*|'
        r'证据不足项保留待核查状态|按以下条件复核.*|按以下判据接受.*|验收.*)$')
    clauses = [part.strip() for part in re.split('[，；。]', text) if part.strip()]
    core = '，'.join(part for part in clauses if not common.fullmatch(part))
    return core or '核对已有数据中该对象的核算口径与期间归属'


def action_core_text(actions, *, peer=False):
    """Specific object/actions only: delivery and acceptance are common once."""
    return '\n'.join(
        f"建议{action['department']}针对{'两厂' if peer else ''}{action['object']}，{action_core(action)}。"
        for action in actions)


def peer_action_text(actions):
    """Compact paired-scope proposal; the caller supplies shared boundaries once.

    ``documents/deliverable/acceptance`` stay intact on each structured action,
    but are not expanded into identical suffixes for every visible object.
    Frozen reports replay their saved text and never call this projection.
    """
    return action_core_text(actions, peer=True)


def peer_action_boundary():
    """One section-level status/availability boundary, not a per-object deadline."""
    return ('当前先用已有逐月同口径汇总复算单位差及标准化金额差，未配对明细单列证据缺口；'
            '以上为后续核实方向，缺失凭证不阻断本轮核对。责任人和期限待审批确认，尚未分派、未发送；'
            '共同完成口径见6.3，逐对象凭证需求、交付物及验收字段保留在冻结任务与审计记录。')
