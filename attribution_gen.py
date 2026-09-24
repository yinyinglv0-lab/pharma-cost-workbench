# -*- coding: utf-8 -*-
"""5.2.3: audited facts + validated model explanations + deterministic report rendering."""
from __future__ import annotations
import json
import re
import sys
import os
import time

from dashboard.data_layer import build_dashboard_data, load_cost_data
from attribution_facts import build_facts
from paths import DASHSCOPE_API_KEY

MOM_ALERT_THRESHOLD = 10.0
ALERT_METRICS = ['单位成本', '材料', '人工', '制费']
LABELS = {'材料': '直接材料', '人工': '直接人工', '制费': '制造费用'}
ACTIONS = {
    '材料': '建议采购部核对采购合同、结算单及调价条款，生产部核对领退料记录、批次投料和收率记录，以区分采购价格与实际耗用的影响。',
    '人工': '建议生产部核对考勤、工时归集和产出记录，财务部复核工资分配、加班及跨期计提，确认人工支出与生产活动的对应关系。',
    '制费': '建议财务部核对制造费用分配基数和凭证期间，设备及生产部门检查能源计量、设备运行和维修记录，确认费用波动是否与实际生产活动一致。',
}


def build_attribution_payload(data, product, month, d=None, *, market=None):
    """Build facts; standalone default retains legacy market, business calls pass empty."""
    if d is None:
        d = load_cost_data()
    facts = build_facts(data, product, month, d)
    ac = next((a for a in data['amount_change'] if a['month'] == month), {})
    mom = next((a for a in data['mom'] if a['month'] == month), {})
    yoy = next((a for a in data['yoy'] if a['month'] == month), {})
    payload = {
        'product': product, 'month': month, 'facts': facts,
        '金额口径': {k: ac.get(k) for k in ('上月总成本','材料变动额','人工变动额','制费变动额','本月总成本','总变动额','勾稽差额')},
        '贡献度_金额口径_百分比': ac.get('贡献度', {}),
        '环比_单位成本口径_百分比': {k: mom.get(k) for k in ALERT_METRICS},
        '同比_单位成本口径_百分比': {k: yoy.get(k) for k in ALERT_METRICS},
        '告警_环比超正负10%': facts.get('alerts', []),
        '材料明细_元每盒': facts.get('elements', {}).get('材料', {}).get('detail', []),
        '数据限制': '材料明细为单位消耗成本，不是采购单价；缺少实际采购单价、实物耗用量和采购合同，不能确认价格或耗用根因。',
        '拆分公式': '产量影响=(本月产量-上月产量)×上月要素单位成本；单位成本影响=(本月要素单位成本-上月要素单位成本)×本月产量。交互项归入单位成本影响，不是采购量价分解。',
    }
    from attribution_decomposition import build_decomposition
    payload['elements'] = build_decomposition(facts, market=market)
    return payload


def rag_evidence(query, top_k=4):
    from kb_search import get_engine, result_to_api
    engine = get_engine()
    final, stats = engine.search(query, mode='formal', top_k=top_k, use_rerank=bool(DASHSCOPE_API_KEY))
    if stats.get('no_answer'):
        return []
    return [result_to_api(x) for x in final]


# Identify a responsible organization/role, not a business noun such as
# 采购合同 or 设备运行. This is deliberately narrower than a semantic classifier.
_RESPONSIBLE_PARTY = re.compile(
    r'(?:财务|采购|生产|设备(?:管理)?|能源(?:管理)?|人力资源|人事|制造|质量(?:管理|控制)?|成本(?:管理)?|经营|核算)'
    r'(?:管理)?(?:部门|部|科|处|组|车间|人员|负责人|主管|经理|专员|会计)|成本会计|车间(?:主任|负责人)'
)


def _model_numeric_feedback(text, known_ids):
    """Show bounded offending tokens, never alter prose or exempt evidence IDs."""
    identifiers = sorted((ident for ident in known_ids
                          if re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,63}', ident)),
                         key=lambda ident: (-len(ident), ident))
    id_pattern = '|'.join(re.escape(ident) for ident in identifiers)
    tokens = re.findall(
        (r'(?<![A-Za-z0-9_-])(?:' + id_pattern + r')(?![A-Za-z0-9_-])|' if id_pattern else '')
        + r'[A-Z]{1,4}[0-9０-９]+|[≥≤><=±]?[0-9０-９]+(?:[.．][0-9０-９]+)?[%％]?|'
          r'[零〇一二三四五六七八九十百千万亿两]+(?:个)?(?:百分点|百分比|毫克|克|升|人|天|次|项|种|条|个)|'
          r'[零〇一二三四五六七八九十百千万亿两]+(?:点|成|倍|元|盒|公斤|千克|小时|%|％)|百分之|[%％]', text)
    shown = list(dict.fromkeys(tokens))[:16]
    return ('；该正文实际禁用片段=' + json.dumps(shown, ensure_ascii=False)
            + '。数字、工艺参数和证据ID均不得出现在正文；ID只保留于evidence_ids，正文用业务凭证或工艺名称。')


def model_diagnostics(value, sources, facts=None, *, context=None):
    """Validate without modifying candidate text; emit rule-local structured errors."""
    if (context or {}).get('prose_mode') == 'bound-numeric-prose/1':
        from enterprise.prose_validation import validate_bound_prose
        return validate_bound_prose(value, sources, facts, context, model_diagnostics, mode='attribution')
    from enterprise.analysis_contract import (diagnostic, prose_diagnostics, role_action_diagnostics,
        causal_diagnostics, action_diagnostics, action_availability, observation_diagnostics)
    if not isinstance(value, dict) or set(value) != {'elements'} or not isinstance(value.get('elements'), dict):
        actual = type(value.get('elements')).__name__ if isinstance(value, dict) else type(value).__name__
        return [diagnostic('ROOT_SCHEMA', 'elements', value, {'elements': dict.fromkeys(LABELS)},
                           '模型结果必须仅包含 elements 对象；当前elements类型为' + actual
                           + '，不能是数组或materials/labor/manufacturing_overhead等翻译后的要素键')]
    if set(value['elements']) != set(LABELS):
        return [diagnostic('ELEMENT_COVERAGE', 'elements', list(value['elements']), list(LABELS),
                           '模型未完整覆盖材料、人工、制费')]
    errors, source_by_id = [], {}
    for source in sources:
        if not isinstance(source, dict) or not isinstance(source.get('id'), str):
            continue
        if source['id'] in source_by_id:
            return [diagnostic('SOURCE_ID_UNIQUE', 'evidence', source['id'], '唯一的来源ID', '证据ID重复，不能确定引用版本')]
        source_by_id[source['id']] = source
    known = set(source_by_id)
    for key, row in value['elements'].items():
        path = f'elements.{key}'
        fields = {'hypothesis', 'recommendation', 'evidence_ids'}
        if not isinstance(row, dict) or set(row) != fields:
            errors.append(diagnostic('ELEMENT_FIELDS', path, row, sorted(fields), f'{key}字段不完整')); continue
        for field in ('hypothesis', 'recommendation'):
            errors.extend(prose_diagnostics(row[field], path + '.' + field, known))
            errors.extend(causal_diagnostics(row[field], path + '.' + field, facts))
        errors.extend(role_action_diagnostics(row, key))
        availability = (context or {}).get('tasks_by_element', {}).get(key, {}).get('action_availability')
        errors.extend(action_diagnostics(row, key, availability or action_availability({'facts': facts or {}}, sources, key)))
        refs = row['evidence_ids']
        eligible = [ident for ident, source in source_by_id.items()
                    if key in source.get('elements', []) and source.get('kind') in ('accounting_fact', 'document_basis')
                    and source.get('evidence_role') != 'context_only' and source.get('support_status', 'eligible') == 'eligible']
        task = (context or {}).get('tasks_by_element', {}).get(key, {})
        if 'eligible_evidence_ids' in task:
            eligible = [ident for ident in eligible if ident in task['eligible_evidence_ids']]
        if not isinstance(refs, list) or not refs or any(not isinstance(ref, str) or ref not in known for ref in refs):
            errors.append(diagnostic('EVIDENCE_EXISTS', path + '.evidence_ids', refs, eligible, f'{key}引用不存在或为空'))
            continue
        if len(set(refs)) != len(refs):
            errors.append(diagnostic('EVIDENCE_UNIQUE', path + '.evidence_ids', refs, '非空且无重复的证据ID', f'{key}引用重复'))
        selected = [source_by_id[ref] for ref in refs]
        for source in selected:
            ident = source['id']
            if key not in source.get('elements', []):
                errors.append(diagnostic('EVIDENCE_ELEMENT', path + '.evidence_ids', ident, eligible, f'{key}引用了其他成本要素的证据'))
            elif source.get('evidence_role') == 'context_only' or source.get('support_status', 'eligible') != 'eligible':
                errors.append(diagnostic('EVIDENCE_AUTHORITY', path + '.evidence_ids', ident, eligible, f'{key}将未核准机制用途或有冲突的背景资料当作归因依据'))
            elif source.get('kind') not in ('accounting_fact', 'document_basis'):
                errors.append(diagnostic('EVIDENCE_KIND', path + '.evidence_ids', ident, eligible,
                    f'{key}仅凭市场参考资料不能支持本厂经营归因' if all(item.get('kind') == 'market_reference' for item in selected)
                    else f'{key}行业与市场参考资料仅供参照，不能作为原因证据ID'))
            elif ident not in eligible:
                errors.append(diagnostic('EVIDENCE_CONTEXT_SCOPE', path + '.evidence_ids', ident, eligible, f'{key}引用不在本次输入允许的证据范围'))
        hypothesis = row['hypothesis'] if isinstance(row['hypothesis'], str) else ''
        for match in re.finditer(r'设备故障|停机|加班|工资(?:上调|上涨)|收率(?:下降|降低)|采购(?:提价|降价)|工艺(?:变更|调整)|新增资产|新增转固|计薪规则(?:调整|变更)|折旧政策(?:调整|变更)', hypothesis):
            event = match.group()
            if not any(source.get('kind') == 'document_basis' and source['id'] in eligible and event in source.get('text', '') for source in selected):
                errors.append(diagnostic('MECHANISM_DOCUMENT', path + '.hypothesis', event,
                    '只使用本项实际引用且原文明示的相关机制，否则仅说明核算对象及证据边界',
                    f'{key}具体经营机制缺少相应受控文档依据：{event}'))
    if context:
        errors.extend(observation_diagnostics(value, context))
    return errors


def _model_errors(value, sources, facts=None):
    """Historical string-error API; correction uses model_diagnostics directly."""
    from enterprise.analysis_contract import legacy_errors
    return legacy_errors(model_diagnostics(value, sources, facts))


def _model_direction(value, before=0):
    from decimal import Decimal, InvalidOperation
    try:
        after, prior = Decimal(str(value)), Decimal(str(before))
        if not after.is_finite() or not prior.is_finite():
            return 'unavailable'
        return 'increase' if after > prior else 'decrease' if after < prior else 'unchanged'
    except (InvalidOperation, ValueError, TypeError):
        return 'unavailable'


def _model_label(value):
    # Labels identify source rows, never carry file notes or free-form instructions.
    from attribution_narrative import _QUOTE_INSTRUCTION
    if (not isinstance(value, str) or not 1 <= len(value) <= 80
            or _QUOTE_INSTRUCTION.search(value)
            or not re.fullmatch(r'[\w\u3400-\u9fff ()（）/+×.%-]+', value)):
        return '未标注项目'
    return value


def _model_context(payload, evidence, *, include_numeric=False):
    """Bounded projection; actual generation receives validated numeric context.

    The default qualitative view remains for historical inspection compatibility.
    Live generation explicitly requests the numeric-aware v2 view below.
    """
    from attribution_narrative import cited_quote
    from enterprise.evidence_references import reference_model_context
    facts = payload.get('facts') or {}
    tasks = {}
    for key in LABELS:
        fact = facts.get('elements', {}).get(key, {})
        decomposition = payload.get('elements', {}).get(key, {})
        eligible = {source['id']: source for source in evidence
                    if isinstance(source.get('id'), str)
                    and re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,63}', source['id'])
                    and key in source.get('elements', [])
                    and source.get('evidence_role') != 'context_only'
                    and source.get('support_status', 'eligible') == 'eligible'}
        fact_ids = [ident for ident, source in eligible.items() if source.get('kind') == 'accounting_fact']
        task = {
            'analysis_level': 'brief' if decomposition.get('analysis_level') == 'brief' else 'detailed',
            'accounting_fact_ids': fact_ids,
            'accounting_directions': {name: _model_direction(fact.get(field)) for name, field in (
                ('amount', 'amount_delta'), ('unit_cost', 'unit_delta'),
                ('amount_output_effect', 'volume_effect'), ('amount_unit_cost_effect', 'unit_effect'))},
            'amount_dominant_driver': decomposition.get('dominant_driver')
                if decomposition.get('dominant_driver') in ('output', 'unit_cost', 'balanced', 'none') else 'unavailable',
            'detail': [], 'market_reference': [], 'document_basis': [],
            'references': reference_model_context(evidence, key, month=payload.get('month')),
        }
        details = fact.get('detail', [])
        selected = list(details[:3])
        comparable = [row for row in details if isinstance(row.get('unit_effect'), (int, float))]
        if comparable:
            focus = max(comparable, key=lambda row: abs(row['unit_effect']))
            if focus not in selected:
                selected.append(focus)
        for row in selected:
            if row.get('evidence_id') not in fact_ids:
                continue
            task['detail'].append({'id': row['evidence_id'], 'name': _model_label(row.get('name')),
                                   'status': row.get('status') if row.get('status') in ('complete', 'missing_before', 'missing_after') else 'unavailable',
                                   'amount_direction': _model_direction(row.get('amount_delta')),
                                   'unit_cost_direction': _model_direction(row.get('unit_delta')),
                                   'unit_effect_direction': _model_direction(row.get('unit_effect'))})
        if key == '人工':
            labor = fact.get('labor_factors') or {}
            task['labor_factors'] = {'available': labor.get('available') is True,
                                     'unit_available': labor.get('unit_available') is True}
            if task['labor_factors']['available']:
                task['labor_factors'].update({name: _model_direction(labor.get(field)) for name, field in (
                    ('hours_direction', 'hours_delta'), ('hourly_allocated_cost_direction', 'cost_per_hour_change_pct'),
                    ('hours_amount_effect_direction', 'hours_effect'), ('rate_amount_effect_direction', 'rate_effect'))})
                if task['labor_factors']['unit_available']:
                    task['labor_factors'].update({name: _model_direction(labor.get(field)) for name, field in (
                        ('hours_per_box_direction', 'hours_per_box_change_pct'),
                        ('output_per_hour_direction', 'output_per_hour_change_pct'),
                        ('hours_unit_effect_direction', 'unit_hours_effect'),
                        ('rate_unit_effect_direction', 'unit_rate_effect'))})
        requested_fact_ids = [*fact.get('evidence_ids', [])[:1], *[row['id'] for row in task['detail']]]
        if key == '人工':
            requested_fact_ids.append((fact.get('labor_factors') or {}).get('evidence_id'))
        task['accounting_fact_ids'] = list(dict.fromkeys(ident for ident in requested_fact_ids if ident in fact_ids)) or fact_ids[:1]
        if key == '材料':
            for material in decomposition.get('top_materials', [])[:2]:
                ident = material.get('reference_evidence_id')
                if eligible.get(ident, {}).get('kind') == 'market_reference':
                    task['market_reference'].append({'id': ident, 'name': _model_label(material.get('name')),
                        'reference_price_direction': _model_direction(material.get('reference_price_after'), material.get('reference_price_before'))})
        for ident, source in eligible.items():
            if source.get('kind') != 'document_basis':
                continue
            quote = cited_quote([source], [ident], key)
            if quote:
                task['document_basis'].append({'id': ident, 'untrusted_excerpt': quote['quote']})
            if len(task['document_basis']) == 2:
                break
        task['eligible_evidence_ids'] = task['accounting_fact_ids'] + [row['id'] for row in task['document_basis']]
        from enterprise.analysis_contract import action_availability
        task['action_availability'] = action_availability(payload, evidence, key)
        tasks[key] = task
    from enterprise.analysis_contract import domain_descriptors
    context = {'schema_version': 'attribution-model-context/1.0',
               'domain_descriptors': domain_descriptors(payload),
               'product': _model_label(payload.get('product')), 'period': 'selected_month_vs_previous_month',
               'volume_direction': _model_direction(facts.get('current', {}).get('volume'), facts.get('previous', {}).get('volume')),
               'tasks_by_element': tasks}
    if include_numeric:
        from enterprise.analysis_context import attribution_numeric_context
        from enterprise.analysis_contract import NUMERIC_PATTERN
        context = attribution_numeric_context(payload, context)
        for task in context.get('tasks_by_element', {}).values():
            focus = task.get('focus') or {}
            if any(NUMERIC_PATTERN.search(name) for name in focus.get('tied_objects', [])):
                focus['coded_name_prose_policy'] = '含编号的名称由程序原样渲染；正文称并列对象，并在evidence_ids保留全部tied_evidence_ids'
        from enterprise.prose_contract import extend_context
        return extend_context(context, payload, evidence, 'attribution')
    return context


from enterprise.analysis_contract import contract_prompt, golden_example, CORRECTION_INSTRUCTION

M2_PROMPT_VERSION = 'attribution-hypotheses/4.0-grounded-contract'
M2_FEWSHOT = golden_example('attribution')
M2_INSTRUCTION = contract_prompt('attribution', M2_FEWSHOT)


M2_MODEL_RUN_SCHEMA = 'attribution-model-run/1.0'
M2_CORRECTION_VERSION = 'attribution-feedback/2.0-structured'
M2_CORRECTION_INSTRUCTION = CORRECTION_INSTRUCTION


def _model_hash(value):
    import hashlib
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()


def _llm_generate(payload, evidence, *, deadline=None, attempt_recorder=None,
                  request_fn=None, config=None, clock=None):
    """Trusted worker result, with at most one validation-feedback correction.

    Production always runs inside run_stage's one hard deadline. A direct test
    injection is cooperative only; neither HTTP phase timeouts nor threads replace
    that killable boundary. Provider failures/invalid JSON never trigger a retry.
    """
    if payload.get('schema_version') == 'benchmark-explanations/1.0':
        from enterprise.benchmark_ai import _llm_generate as benchmark_generate
        return benchmark_generate(payload, evidence, deadline=deadline, attempt_recorder=attempt_recorder,
                                  request_fn=request_fn, config=config, clock=clock)
    from copy import deepcopy
    from dataclasses import replace
    import hashlib
    import math
    from enterprise.model_gateway import capture_model_calls, configuration, generate_json
    from attribution_runtime import sanitize_model_calls
    clock = clock or time.monotonic
    started = clock()
    from enterprise.prose_contract import model_stage_budget
    # Report identity comes from the trusted worker configuration, not payload flags.
    task = 'report' if config is not None and config.task == 'report' else 'attribution'
    config = config or configuration(task=task)
    if config.task is None and not config.routing_enabled:
        config = replace(config, task=task)
    if config.configured_timeout is None:
        config = replace(config, configured_timeout=config.timeout)
    # 多模型分工：初稿用已选模型，修正轮可用独立注册模型（未配置则沿用初稿模型）。
    correction_cfg = None
    if config.registry_id != 'legacy':
        from enterprise.model_registry import correction_configuration
        try:
            correction_cfg = correction_configuration(task)
        except Exception:
            correction_cfg = None   # 修正模型配置无效时沿用初稿模型，不阻断生成
    hard_budget = model_stage_budget(payload, config=config)
    deadline = min(float(deadline), started + hard_budget) if deadline is not None else started + hard_budget - 2.0
    if not math.isfinite(deadline):
        raise ValueError('模型执行期限无效')
    if request_fn is None:
        from functools import partial
        request_fn = partial(generate_json, task=task)
    result = {'schema': M2_MODEL_RUN_SCHEMA, 'candidate': None, 'attempts': [],
              'correction': {'attempted': False, 'status': 'not_needed'}, 'failure_type': None,
              'model_calls': [], 'hard_budget_seconds': hard_budget, 'model_identity': config.identity()}
    from enterprise.prose_contract import is_prose_mode, prose_prompt, provider_prose_context
    prose_enabled = is_prose_mode(payload)
    base_instruction = prose_prompt('attribution') if prose_enabled else M2_INSTRUCTION
    instruction = base_instruction
    prompt_version = 'attribution-prose/1.4-bound-numeric-compact' if prose_enabled else M2_PROMPT_VERSION
    context = _model_context(payload, evidence, include_numeric=True)
    # Transport deduplicates exact copies only; validators keep the full context.
    provider_context = provider_prose_context(context)
    data = deepcopy(provider_context)

    def record():
        result['model_calls'] = [call for attempt in result['attempts'] for call in attempt['model_calls']]
        if attempt_recorder is not None:
            attempt_recorder(deepcopy({key: result[key] for key in ('attempts', 'correction', 'model_calls')}))

    for number in (1, 2):
        # Leave time for validation, result serialization and the parent process.
        remaining = deadline - clock() - 2.0
        if remaining < (12.0 if number == 2 else 1.0):
            if number == 2:
                result['correction']['status'] = 'skipped_insufficient_budget'
            else:
                result['failure_type'] = 'BudgetExhausted'
            record()
            break
        if number == 2:
            result['correction'] = {'attempted': True, 'status': 'running'}
            instruction = base_instruction + '\n\n' + M2_CORRECTION_INSTRUCTION
            data = {**deepcopy(provider_context), 'previous_candidate': deepcopy(result['candidate']),
                    'validation_errors': list(result['attempts'][0]['diagnostics']),
                    'validation_diagnostics': deepcopy(result['attempts'][0]['validation_diagnostics'])}
        from enterprise.model_registry import REGISTRY_CONTRACT
        use_config = (correction_cfg or config) if number == 2 else config
        request_cap = use_config.timeout if use_config.budget_contract == REGISTRY_CONTRACT and use_config.registry_id != 'legacy' else min(use_config.timeout, 40.0)
        request_timeout = min(request_cap, remaining)
        attempt = {'attempt': number, 'kind': 'initial' if number == 1 else 'correction',
                   'prompt_version': prompt_version if number == 1 else prompt_version + '/correction-1' if prose_enabled else M2_CORRECTION_VERSION,
                   'instruction_sha256': hashlib.sha256(instruction.encode('utf-8')).hexdigest(),
                   'request_sha256': _model_hash(data), 'response_sha256': None,
                   'status': 'running', 'diagnostics': [], 'elapsed_seconds': None, 'failure_type': None,
                   'request_timeout_seconds': round(request_timeout, 3), 'used': False, 'model_calls': [],
                   'registry_id': use_config.registry_id, 'requested_model': use_config.model}
        result['attempts'].append(attempt)
        record()
        attempt_started = clock()
        try:
            with capture_model_calls() as calls:
                try:
                    candidate = request_fn(instruction, deepcopy(data), max_tokens=5000 if prose_enabled else 2000,
                                           config=replace(use_config, timeout=request_timeout))
                finally:
                    attempt['model_calls'] = sanitize_model_calls(calls, secret=use_config.api_key)
                    record()
            # generate_json returns a parsed object; strings/lists are not a
            # repairable provider response, even through a trusted test hook.
            if type(candidate) is not dict:
                raise TypeError('模型网关没有返回JSON对象')
            attempt['response_sha256'] = _model_hash(candidate)
            from enterprise.analysis_contract import legacy_errors
            structured = model_diagnostics(candidate, evidence, payload.get('facts'), context=context)
            errors = legacy_errors(structured)
        except Exception as exc:
            # Do not persist provider messages, URLs, raw response bodies or keys.
            attempt.update(status='unavailable', diagnostics=['模型调用未完成：' + type(exc).__name__],
                           failure_type=type(exc).__name__, elapsed_seconds=round(max(0.0, clock() - attempt_started), 3))
            result['failure_type'] = type(exc).__name__
            if number == 2:
                result['correction']['status'] = 'failed_unavailable'
            record()
            break
        attempt.update(status='rejected' if errors else 'validated', diagnostics=errors,
                       validation_diagnostics=structured,
                       elapsed_seconds=round(max(0.0, clock() - attempt_started), 3))
        if clock() >= deadline - 1.0:
            # A provider may ignore its cooperative timeout; never accept a late
            # result, even if a synthetic/direct call lacks the outer process.
            attempt.update(status='budget_exhausted', used=False)
            result['failure_type'] = 'BudgetExhausted'
            if number == 2:
                result['correction']['status'] = 'failed_budget'
            record()
            break
        result['candidate'] = deepcopy(candidate)
        if not errors:
            attempt['used'] = True
            if number == 2:
                result['correction']['status'] = 'validated'
            record()
            break
        if number == 2:
            result['correction']['status'] = 'rejected'
        record()
    return result


def _num(v, signed=False):
    from enterprise.numeric import format_number
    return format_number(v, signed=signed)


def render_report(payload, explanations=None, sources=None):
    from attribution_narrative import render
    return render(payload, explanations, sources, detailed=True)[2]


SHORT_ACTIONS = {
    '材料': '建议采购部核对合同及结算单，生产部核查领退料和收率记录，以区分采购价格与耗用影响。',
    '人工': '建议生产部核对考勤及工时记录，财务部复核工资分配和跨期计提，确认人工支出归属。',
    '制费': '建议财务部复核费用分配和凭证期间，设备部门核查能源计量及维修记录，确认波动原因。',
}
SHORT_HYPOTHESES = {
    '材料': '明细仅反映单位消耗成本，尚不能确认采购涨价或实物单耗变化。',
    '人工': '人工变化可能涉及生产安排或费用归集，需结合工时与工资记录核实。',
    '制费': '费用变化可能涉及生产负荷或分配方式，具体原因仍待凭证核查。',
}


def _short_field(text, fallback, kind, limit=65):
    """Select complete, self-contained sentences; never slice a sentence or remove uncertainty."""
    text = str(text or '').strip()
    candidates = [text] + re.findall(r'[^。！？\n]+[。！？]?', text)
    for sentence in candidates:
        sentence = sentence.strip()
        if not sentence or len(sentence) > limit:
            continue
        if kind == 'hypothesis':
            valid = any(w in sentence for w in ('可能', '待核查', '待核实', '尚不能', '不足以'))
        else:
            valid = (any(w in sentence for w in ('部', '车间', '财务', '采购', '设备', '生产'))
                     and any(w in sentence for w in ('核对', '核查', '复核', '检查', '排查')))
        if valid:
            return sentence if sentence[-1] in '。！？' else sentence + '。'
    return fallback


def organize_source_documents(sources):
    """Group source leaves by file, retaining record keys and reference IDs outside the prose."""
    from pathlib import PureWindowsPath
    documents = {}

    def add(source, evidence_id):
        if isinstance(source, dict) and source.get('records'):
            for record in source['records']:
                if record:
                    add(record, evidence_id)
            return
        if isinstance(source, dict):
            full_name = str(source.get('file') or source.get('table') or '未标注来源')
            location = {'table': source.get('table'), 'key': source.get('key', {}), 'line': source.get('line'),
                        'record_number': source.get('record_number'), 'sheet': source.get('sheet'),
                        'sha256': source.get('sha256'), 'page': source.get('page'),
                        'offset': source.get('offset'), 'end_offset': source.get('end_offset')}
        else:
            full_name = str(source or '未标注来源')
            location = {'table': None, 'key': {}, 'line': None}
        document = documents.setdefault(full_name, {
            'file': PureWindowsPath(full_name).name, 'locations': [], 'evidence_ids': []})
        if location not in document['locations']:
            document['locations'].append(location)
        if evidence_id and evidence_id not in document['evidence_ids']:
            document['evidence_ids'].append(evidence_id)

    for source in sources:
        add(source.get('source'), source.get('id'))
    return list(documents.values())


def render_concise(payload, explanations=None, sources=None):
    """The same accounting bridges and citations as the longer reading version."""
    from attribution_narrative import render
    return render(payload, explanations, sources)


def _execute_stage(stage, args, timeout):
    from attribution_runtime import run_stage
    return run_stage(stage, args, timeout=timeout)


def generate_attribution(product, month, use_llm=True, d=None, progress=None, *, principal=None, root=None, model_fn=None, require_hybrid=False):
    if not re.fullmatch(r'\d{4}-(0[1-9]|1[0-2])', month):
        raise ValueError('月份必须为YYYY-MM')
    if not isinstance(require_hybrid, bool):
        raise ValueError('require_hybrid须为布尔值')
    from enterprise.security import Principal, require
    if principal is not None:
        require(principal, 'analysis.generate', factory='中药一厂', product=product)
    from enterprise.snapshots import current_provenance
    input_provenance = current_provenance()
    d = load_cost_data() if d is None else d
    from enterprise.cost_imports import digest, records
    input_data_hash = digest(records(d))
    data = build_dashboard_data(product, d)
    authenticated = isinstance(principal, Principal)
    if authenticated:
        import pandas as pd
        # Business evidence must come from the authorized release, never the
        # ungoverned local market CSV used by the legacy standalone calculation.
        payload = build_attribution_payload(data, product, month, d, market=pd.DataFrame())
    else:
        payload = build_attribution_payload(data, product, month, d)
    facts = payload['facts']
    memberships = {}
    for element, details in facts.get('elements', {}).items():
        for ident in details.get('evidence_ids', []):
            memberships.setdefault(ident, []).append(element)
    sources = [{**source, 'kind': 'accounting_fact',
                'elements': memberships.get(source['id'], list(LABELS))}
               for source in facts.get('evidence', [])]
    for element in payload['elements'].values():
        sources.extend({**source, 'kind': 'market_reference', 'elements': ['材料']}
                       for source in element.get('evidence', []))
    from enterprise.knowledge_context import context
    from enterprise.knowledge import Repository
    from enterprise.model_gateway import capture_model_calls, configuration
    from attribution_runtime import sanitize_model_calls
    diagnostics, explanations = [], None
    model_run = {'schema': M2_MODEL_RUN_SCHEMA, 'attempts': [],
                 'correction': {'attempted': False, 'status': 'not_requested'},
                 'hard_budget_seconds': 45, 'provider_call_count': 0, 'model_calls': []}
    started = time.monotonic()
    retrieval_stats = {'retrieval_mode': 'not_requested', 'degraded': True,
                       'no_answer': True, 'returned_n': 0, 'applicability_excluded': []}
    reference_evidence = []
    retrieval_blocked = False
    try:
        if principal is None:
            managed_sources = []
            diagnostics.append('内部无身份计算仅使用结构化事实；业务入口须提供已认证身份检索知识')
        else:
            from enterprise.domain_profiles import product_definition
            specification = (facts.get('current') or {}).get('source', {}).get('key', {}).get('产品规格')
            # Typed reference governance applies even to deterministic/no-key
            # business runs. The injected callback path is a legacy test hook only.
            partitioned = (authenticated and model_fn is None
                           and product_definition(product, specification) is not None)
            if partitioned:
                from enterprise.analysis_service import report_evidence
                managed_sources = report_evidence(principal, product, specification, [month], root, facts=facts,
                    **({'require_hybrid': True} if require_hybrid else {}))
                retrieval_stats = dict(managed_sources.diagnostics)
                # Keep the public retrieval summary while preserving typed query
                # diagnostics and treating excluded background as no answer.
                retrieval_stats.update(no_answer=not managed_sources, returned_n=len(managed_sources),
                    applicability_excluded=[{'chunk_id': candidate['chunk_id'], 'reason': reason}
                        for candidate in retrieval_stats.get('candidates', [])
                        if not candidate.get('eligible')
                        for reason in candidate.get('reasons', [])])
                # Preserve the adapter's exact kind/scope/quote authority; references
                # must not be relabelled as documents or promoted into cause IDs.
                sources.extend(managed_sources)
                reference_evidence = [source for source in managed_sources
                                      if source.get('kind') in ('industry_reference', 'market_reference')
                                      and source.get('support_status') == 'eligible']
            else:
                if require_hybrid and model_fn is None:
                    raise ValueError('当前产品规格尚未配置分类型混合检索，请先登记产品知识映射。')
                managed_sources, retrieval_stats = context(product, month,
                    f'{product} 成本 原材料 工艺 收率 人工 制造费用',
                    repository=Repository(root, principal=principal), principal=principal,
                    factory='中药一厂', specification=specification, return_stats=True)
                from enterprise.knowledge_applicability import element_terms
                sources.extend({**source, 'evidence_role': source.get('evidence_role', 'context_only'),
                                'kind': 'document_basis' if source.get('evidence_role') == 'document_basis' else 'context_only',
                                'elements': list(element_terms(source['text'])) or list(LABELS)}
                               for source in managed_sources)
        if retrieval_stats.get('degraded'):
            diagnostics.extend(retrieval_stats.get('degradation_reasons', []))
    except Exception as exc:
        retrieval_blocked = bool(require_hybrid)
        diagnostics.append(f'受控知识读取失败：{type(exc).__name__}，仅使用已验证成本；未用裸行情文件补回')
        if retrieval_blocked:
            diagnostics.append('正式模型分析要求向量语义＋BM25混合检索；本次未调用模型，待发布/预热完成后重试。')
            retrieval_stats.update(reason='hybrid_unavailable', require_hybrid=True, no_answer=True)
    from enterprise.evidence_references import reference_model_context
    reference_context = {element: reference_model_context(reference_evidence, element, month=month)
                         for element in LABELS}
    from enterprise.industry_benchmark import build_industry_comparison
    from enterprise.domain_profiles import load_domain_profile, product_definition
    profile = load_domain_profile()
    specification = (facts.get('current') or {}).get('source', {}).get('key', {}).get('产品规格')
    definition = product_definition(product, specification, profile)
    payload['industry_comparison'] = build_industry_comparison(product, specification, month, d, reference_evidence, profile=profile)
    payload['industry_comparison']['product_category'] = definition['category'] if definition else None
    timings = {'retrieval_seconds': round(time.monotonic() - started, 2)}
    def notify(message):
        if progress:
            progress(message)
    def configured_for_attribution():
        from inspect import signature
        # Legacy test/infrastructure configuration hooks take no arguments. Bind
        # before calling so errors from inside the routed resolver are not hidden.
        try:
            signature(configuration).bind(task='attribution')
        except TypeError:
            return configuration()
        return configuration(task='attribution')
    status = 'deterministic_requested' if not use_llm else ('retrieval_unavailable' if retrieval_blocked else 'no_api_key')
    resolved_config = (configured_for_attribution() if facts.get('available') and use_llm
                       and not retrieval_blocked and model_fn is None else None)
    if facts.get('available') and use_llm and not retrieval_blocked and (model_fn is not None or resolved_config.api_key):
        notify('正在生成分析建议；成本数值已完成，知识依据来自受控发布版本')
        stage_started = time.monotonic()
        try:
            if model_fn is not None:
                # The public injection contract is a plain candidate, never a
                # privileged worker envelope. No retries of arbitrary callbacks.
                model_run.update(execution='injected_single_call', hard_budget_seconds=None,
                                 provider_call_count=1)
                with capture_model_calls() as calls:
                    try:
                        candidate = model_fn(payload, sources)
                    finally:
                        model_run['model_calls'] = sanitize_model_calls(calls)
            else:
                # Only the controlled live path selects the new server-owned
                # contract; existing injected/frozen hypothesis contracts replay.
                payload['prose_mode'] = 'bound-numeric-prose/1'
                from attribution_runtime import prepare_model_stage
                stage_args, stage_budget = prepare_model_stage(payload, sources, task='attribution', config=resolved_config)
                model_run['hard_budget_seconds'] = stage_budget
                model_run['model_identity'] = stage_args.identity
                worker_result = _execute_stage('model', stage_args, stage_budget)
                if (not isinstance(worker_result, dict) or worker_result.get('schema') != M2_MODEL_RUN_SCHEMA
                        or not isinstance(worker_result.get('attempts'), list)):
                    raise ValueError('受控模型worker返回结构无效')
                model_run.update({key: worker_result[key] for key in ('attempts', 'correction')})
                model_run.update(execution='bounded_worker', provider_call_count=len(model_run['attempts']))
                # Preserve initial failures even when the correction succeeds.
                for attempt in model_run['attempts']:
                    if attempt.get('status') != 'validated':
                        diagnostics.extend(f"模型尝试{attempt['attempt']}：{reason}" for reason in attempt.get('diagnostics', []))
                if worker_result.get('failure_type'):
                    from attribution_runtime import StageExecutionError
                    raise StageExecutionError('model', worker_result['failure_type'], '受控模型调用未完成')
                candidate = worker_result.get('candidate')
            from enterprise.analysis_contract import legacy_errors
            errors = legacy_errors(model_diagnostics(candidate, sources, facts,
                context=_model_context(payload, sources, include_numeric=model_fn is None)))
            if errors:
                diagnostics.extend(errors); status = 'model_rejected'
                for attempt in model_run['attempts']:
                    attempt['used'] = False
            else:
                explanations = candidate; status = 'model_validated'
        except Exception as exc:
            from attribution_runtime import StageExecutionError
            trusted_worker_error = model_fn is None and isinstance(exc, StageExecutionError)
            partial = exc.model_run if trusted_worker_error else {}
            if partial:
                model_run.update({key: partial[key] for key in ('attempts', 'correction') if key in partial})
                model_run.update(execution='bounded_worker', provider_call_count=len(model_run['attempts']))
                for attempt in model_run['attempts']:
                    diagnostics.extend(f"模型尝试{attempt['attempt']}：{reason}" for reason in attempt.get('diagnostics', []))
            for attempt in model_run['attempts']:
                attempt['used'] = False
            error_type = exc.error_type if trusted_worker_error else type(exc).__name__
            timed_out = exc.timed_out if trusted_worker_error else False
            diagnostics.append(f"模型调用失败：{error_type}" + ('（已到时限，请求进程已终止）' if timed_out else '')); status = 'model_unavailable'
        timings['model_seconds'] = round(time.monotonic() - stage_started, 2)
    if model_fn is None:
        model_run['model_calls'] = [call for attempt in model_run['attempts']
                                   for call in sanitize_model_calls(attempt.get('model_calls'))]
    timings['total_seconds'] = round(time.monotonic() - started, 2)
    notify('分析已完成' if explanations else '数据分析已完成；模型未使用，具体原因见生成状态')
    if not facts.get('available'):
        status = 'insufficient_data'
    from enterprise.analysis_narrative import build_attribution_narrative
    from enterprise.prose_contract import build_prose_contract, is_prose_mode
    narrative_contract = build_prose_contract(payload, sources, 'attribution') if is_prose_mode(payload) else None
    full_narrative = build_attribution_narrative(payload, explanations, sources, detailed=True,
                                                 prose_contract=narrative_contract)
    narrative = build_attribution_narrative(payload, explanations, sources,
                                            prose_contract=narrative_contract)
    text = full_narrative['text']
    overview, sections, concise_text = narrative['overview'], narrative['sections'], narrative['text']
    return {'text': text, 'overview': overview, 'sections': sections, 'concise_text': concise_text,
            'followup_criteria': narrative['followup_criteria'],
            'narrative_schema': narrative['schema_version'],
            'industry_comparisons': narrative['industry_comparisons'],
            'yield_comparisons': narrative['yield_comparisons'],
            'source_documents': organize_source_documents(sources), 'used_llm': explanations is not None,
            'alerts': payload['告警_环比超正负10%'], 'sources': sources, 'payload': payload,
            'generation_status': status, 'review_status': 'needs_review', 'timings': timings,
            'model_run': model_run, 'model_explanations': explanations,
            'input_provenance': input_provenance, 'input_data_hash': input_data_hash,
            'retrieval_stats': retrieval_stats,
            'reference_evidence': reference_evidence, 'reference_context': reference_context,
            'index_release_id': retrieval_stats.get('release_id'),
            'validation': {'numeric_facts': 'program_rendered', 'model_explanations': 'passed' if explanations else 'not_used',
                           'diagnostics': diagnostics},
            'limitations': [payload['数据限制'], '自动检查不代替专业审核，模型解释仍须业务人员核实。']}


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--product', default='银黄口服液')
    parser.add_argument('--month', default='2026-05')
    parser.add_argument('--no-llm', action='store_true')
    args = parser.parse_args()
    result = generate_attribution(args.product, args.month, not args.no_llm)
    print(result['generation_status'])
    print(result['text'])
