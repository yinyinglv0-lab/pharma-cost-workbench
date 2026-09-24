"""Validated AI hypotheses on top of deterministic same-scope benchmark facts.

No model client or implicit network/RAG access. The authorized application injects
model_fn(payload, evidence). Model prose may propose checks, never numeric facts.
"""
from __future__ import annotations

from calendar import monthrange
from copy import deepcopy
from datetime import date
import json
import math
import re
import time

from enterprise.numeric import format_number, format_percent
from .benchmark import build_benchmark, load_merged_tables, ELEMENTS

SCHEMA_VERSION = "benchmark-explanations/1.0"
PROMPT_VERSION = "benchmark-hypotheses/4.0-grounded-contract"
CALCULATION_VERSION = "same-scope-normalized-cost/1.0"
FIELDS = {"claim_type", "hypothesis", "recommendation", "evidence_ids", "missing_evidence"}
from enterprise.analysis_contract import contract_prompt, golden_example, CORRECTION_INSTRUCTION

M3_FEWSHOT = golden_example('benchmark')
EXPLANATION_PROMPT = contract_prompt('benchmark', M3_FEWSHOT)
BENCHMARK_PROMPT = EXPLANATION_PROMPT
MODEL_RUN_SCHEMA = 'benchmark-model-run/1.0'
ZERO_HYPOTHESIS = '本项单位成本无差异，保留同口径记录以便后续比较。'


def _is_zero_gap(value):
    from decimal import Decimal, InvalidOperation
    try:
        return not isinstance(value, bool) and Decimal(str(value)).is_finite() and Decimal(str(value)) == 0
    except (InvalidOperation, TypeError, ValueError):
        return False


def _zero_explanation(row):
    return {'claim_type': 'no_difference', 'hypothesis': ZERO_HYPOTHESIS,
            'recommendation': '', 'evidence_ids': [row['evidence_id']], 'missing_evidence': []}


def grouped_model_context(payload, evidence, *, include_numeric=False):
    from enterprise.evidence_references import reference_model_context
    facts = payload['facts']
    tasks = {}
    for row in facts['elements']:
        element = row['element']
        eligible = [source for source in evidence if element in source.get('elements', [])
                    and source.get('kind') in ('data_fact', 'document_basis')
                    and source.get('support_status', 'eligible') == 'eligible']
        branch = facts.get('paired_drilldown', {}).get(element, {})
        from attribution_gen import _model_direction, _model_label
        task = {'mode': 'no_difference' if _is_zero_gap(row['unit_gap']) else 'explain_difference',
                'facts': {'element': element, 'evidence_id': row['evidence_id'],
                          'home_vs_peer': _model_direction(row['unit_gap']),
                          'boundary': 'accounting_gap_not_confirmed_business_cause'},
                'paired_coverage': deepcopy(branch.get('coverage', {})),
                'detail': [{'name': _model_label(detail['name']), 'status': detail['status'],
                            'home_provided': detail.get('home') is not None,
                            'peer_provided': detail.get('peer') is not None,
                            'unit_gap_direction': _model_direction(detail.get('unit_gap'))}
                           for detail in branch.get('rows', [])[:4]],
                'eligible_evidence_ids': [source['id'] for source in eligible],
                'data_fact': [], 'document_basis': [], 'available_document_ids': [],
                 'cite_at_least_one_document_id_from': [],
                'references': reference_model_context(evidence, element, month=payload.get('month'))}
        for source in eligible:
            item = {key: deepcopy(source[key]) for key in
                    ('id', 'text', 'kind', 'elements', 'claim_boundary', 'limitations', 'scope') if key in source}
            if source['kind'] == 'document_basis':
                from attribution_narrative import cited_quote
                quote = cited_quote([source], [source['id']], element)
                if not quote:
                    task['eligible_evidence_ids'].remove(source['id'])
                    continue
                item['text'] = quote['quote']
                task['available_document_ids'].append(source['id'])
            task[source['kind']].append(item)
        if task['mode'] == 'no_difference':
            task['no_difference_result'] = _zero_explanation(row)
        from enterprise.analysis_contract import action_availability
        task['action_availability'] = action_availability(payload, evidence, element)
        tasks[element] = task
    from enterprise.analysis_contract import domain_descriptors
    context = {'domain_descriptors': domain_descriptors(payload),
               'product': payload['product'], 'specification': payload['specification'],
               'month': payload['month'], 'tasks_by_element': tasks}
    if include_numeric:
        from enterprise.analysis_context import benchmark_numeric_context
        from enterprise.prose_contract import extend_context
        return extend_context(benchmark_numeric_context(payload, context), payload, evidence, 'benchmark')
    return context


class BenchmarkValidationError(ValueError):
    pass


def _strict_json(candidate):
    def object_pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise BenchmarkValidationError("模型JSON存在重复字段")
            value[key] = item
        return value
    if isinstance(candidate, str):
        try:
            candidate = json.loads(candidate, object_pairs_hook=object_pairs,
                                   parse_constant=lambda value: (_ for _ in ()).throw(BenchmarkValidationError("模型JSON含非有限数字")))
        except (ValueError, TypeError, RecursionError) as exc:
            raise BenchmarkValidationError("模型结果不是严格JSON对象") from exc
    def walk(value, depth=0):
        if depth > 15:
            raise BenchmarkValidationError("模型JSON嵌套过深")
        if value is None or type(value) in (str, bool, int):
            return
        if type(value) is float:
            if not math.isfinite(value):
                raise BenchmarkValidationError("模型JSON含非有限数字")
            return
        if type(value) is dict and all(type(key) is str for key in value):
            for item in value.values():
                walk(item, depth + 1)
            return
        if type(value) is list:
            for item in value:
                walk(item, depth + 1)
            return
        raise BenchmarkValidationError("模型JSON含不支持的类型")
    walk(candidate)
    if type(candidate) is not dict:
        raise BenchmarkValidationError("模型结果必须为JSON对象")
    return candidate


def _valid_source(value):
    if not isinstance(value, dict):
        return False
    named = any(isinstance(value.get(key), str) and value[key].strip() for key in ("file", "table", "document_id"))
    records = value.get("records")
    return named or (isinstance(records, list) and bool(records) and all(isinstance(row, dict) for row in records))


def validate_comparative_mechanisms(text, element, *, unit_gap=None, home_labels=(), peer_labels=()):
    """Reject explicit cost-driver direction and metric-dimension mismatches.

    This narrow guard does not establish either factory's operating conditions.
    A disclaimer that an invalid relationship is uncertain does not make it valid.
    """
    from decimal import Decimal, InvalidOperation
    if unit_gap is not None:
        try:
            if isinstance(unit_gap, bool):
                raise InvalidOperation
            unit_gap = Decimal(str(unit_gap))
            if not unit_gap.is_finite():
                raise InvalidOperation
        except (InvalidOperation, ValueError, TypeError):
            return ['单位差方向输入须为有限十进制数，不能据无效数据核查机制']
    homes = {value for value in home_labels if isinstance(value, str) and value} | {'一厂', '本厂', '本方'}
    peers = {value for value in peer_labels if isinstance(value, str) and value} | {'二厂', '对标厂', '对标方'}
    home_pattern = '(?:' + '|'.join(map(re.escape, sorted(homes, key=len, reverse=True))) + ')'
    peer_pattern = '(?:' + '|'.join(map(re.escape, sorted(peers, key=len, reverse=True))) + ')'
    errors = []
    for sentence in re.split(r'[。！？;；\n]+', str(text)):
        for segment in re.split(r'但是|然而|不过|但(?!位)', sentence):
            if not segment:
                continue
            dimension_patterns = (
                r'(?:蒸汽(?:耗量|消耗|用量)?|能源耗量|能耗|动力消耗|耗电量).{0,18}(?:符合|满足|遵循|不超过|低于|高于).{0,10}(?:浓缩)?(?:损耗|收率).{0,8}(?:约束|标准|限值|要求|阈值)',
                r'(?:用|以|依据|根据|按)(?:浓缩)?(?:损耗|收率).{0,8}(?:约束|标准|限值|要求|阈值).{0,12}(?:判断|判定|衡量|评估|核验).{0,12}(?:蒸汽|能耗|动力|耗电)',
            )
            denied = re.search(
                r'(?:不能|不可(?:以)?|不得|不应|不等于|不代表|不能据此|不能直接).{0,24}(?:用|将|把|以|依据|作为|判断|证明|认定)|'
                r'(?:分别|分开).{0,12}(?:核查|核对|验证).{0,12}(?:物料|损耗|收率).{0,20}(?:蒸汽|能源|能耗)',
                segment)
            for pattern in dimension_patterns:
                mismatch = re.search(pattern, segment)
                if mismatch and not denied:
                    errors.append('物料损耗或收率限值不能作为蒸汽或能源耗量的达标标准；应分别核查物料、能源与费用分配记录')
                    break
            if element != '人工' or not re.search(r'可能与|可能因|源于|归因|原因|由于|因为|导致|造成|从而', segment):
                continue
            if re.search(r'不能(?:将|把|用|据此)|不能归因|不应|不等于|不代表|不足以|不得', segment):
                continue
            if re.search(r'(?:小时(?:归集)?(?:费用|费率)|归集费率).{0,12}(?:抵消|覆盖|主导)|(?:抵消|覆盖|主导).{0,12}(?:小时(?:归集)?(?:费用|费率)|归集费率)', sentence):
                # Multiple opposing factors require their own paired accounting
                # review; this single-driver rule cannot decide that net effect.
                continue
            explicit_lower = bool(re.search(home_pattern + r'.{0,15}(?:人工|用工).{0,12}(?:低于|较低|更低|降低)', segment))
            explicit_higher = bool(re.search(home_pattern + r'.{0,15}(?:人工|用工).{0,12}(?:高于|较高|更高|提高)', segment))
            # A standalone documented mechanism can be stated without attributing
            # the observed gap to it; a direct comparative attribution cannot.
            if re.search(r'(?:工艺|文档|原文|标准|文件|记录).{0,8}(?:提示|记载|规定|指出|写明)', segment) and not (explicit_lower or explicit_higher):
                continue
            lower = explicit_lower or (unit_gap is not None and unit_gap < 0)
            higher = explicit_higher or (unit_gap is not None and unit_gap > 0)
            for direction, pattern in (
                (1, r'(?:返工(?:频次|频率|工时)?|加班(?:工时)?|单位工时).{0,10}(?:增加|增多|更多|较多|提高|上升)'),
                (-1, r'(?:返工(?:频次|频率|工时)?|加班(?:工时)?|单位工时).{0,10}(?:减少|更少|较少|降低|下降)'),
            ):
                for driver in re.finditer(pattern, segment):
                    prefix = re.split(r'[,，]', segment[:driver.start()])[-1]
                    peer_subject = bool(re.search(peer_pattern + r'(?:(?!' + home_pattern + r').){0,16}$', prefix))
                    expected = -direction if peer_subject else direction
                    if (lower and expected > 0) or (higher and expected < 0):
                        errors.append('人工成本差异与返工或工时方向未衔接；须明确两厂主语和配对方向，或仅说明机制并待核查两厂记录')
    return list(dict.fromkeys(errors))


def validate_explanations_structured(candidate, evidence, facts=None, *, context=None):
    """Actual benchmark validator; each rejecting predicate owns its diagnostic."""
    if (context or {}).get('prose_mode') == 'bound-numeric-prose/1':
        from enterprise.prose_validation import validate_bound_prose
        try:
            value = _strict_json(candidate)
        except BenchmarkValidationError:
            return validate_explanations_structured(candidate, evidence, facts, context=None)
        return validate_bound_prose(value, evidence, facts, context,
                                    validate_explanations_structured, mode='benchmark')
    from enterprise.analysis_contract import (diagnostic, prose_diagnostics, role_action_diagnostics,
        causal_diagnostics, action_diagnostics, action_availability, observation_diagnostics)
    try:
        value = _strict_json(candidate)
    except BenchmarkValidationError as exc:
        return [diagnostic('STRICT_JSON', '$', type(candidate).__name__, '严格JSON对象', str(exc))]
    if set(value) != {'elements'} or type(value.get('elements')) is not dict:
        return [diagnostic('ROOT_SCHEMA', 'elements', value, {'elements': dict.fromkeys(ELEMENTS)}, '模型结果必须仅包含elements对象')]
    if set(value['elements']) != set(ELEMENTS):
        return [diagnostic('ELEMENT_COVERAGE', 'elements', list(value['elements']), list(ELEMENTS), '模型未完整覆盖材料、人工、制费')]
    sources, errors = {}, []
    for source in evidence:
        if not isinstance(source, dict) or not isinstance(source.get('id'), str):
            continue
        if source['id'] in sources:
            return [diagnostic('SOURCE_ID_UNIQUE', 'evidence', source['id'], '唯一的来源ID', '证据ID重复，不能确定引用版本')]
        sources[source['id']] = source
    fact_rows = (facts or {}).get('elements', [])
    if not isinstance(fact_rows, list):
        fact_rows = []
    for element, row in value['elements'].items():
        path = f'elements.{element}'
        if type(row) is not dict or set(row) != FIELDS:
            errors.append(diagnostic('ELEMENT_FIELDS', path, row, sorted(FIELDS), element + '字段不完整或包含额外字段')); continue
        fact = next((item for item in fact_rows if isinstance(item, dict) and item.get('element') == element), None)
        descriptors = (context or {}).get('domain_descriptors') or {}
        factories = descriptors.get('factories') or {}
        if row['claim_type'] == 'no_difference':
            expected = _zero_explanation(fact) if fact and _is_zero_gap(fact.get('unit_gap')) else {'claim_type': 'hypothesis'}
            if not fact or not _is_zero_gap(fact.get('unit_gap')) or row != expected:
                errors.append(diagnostic('NO_DIFFERENCE_BRANCH', path, row, expected, element + '无差异分支与实际单位差或固定字段不一致'))
            elif fact['evidence_id'] not in sources or element not in sources[fact['evidence_id']].get('elements', []):
                errors.append(diagnostic('EVIDENCE_EXISTS', path + '.evidence_ids', row['evidence_ids'], [fact['evidence_id']], element + '无差异分支缺少实际事实引用'))
            continue
        if row['claim_type'] != 'hypothesis':
            errors.append(diagnostic('CLAIM_TYPE', path + '.claim_type', row['claim_type'], 'hypothesis', element + '仅允许待核查假设'))
        for field in ('hypothesis', 'recommendation'):
            text = row[field]
            errors.extend(prose_diagnostics(text, path + '.' + field, sources, benchmark=True))
            errors.extend(causal_diagnostics(text, path + '.' + field, facts))
            if isinstance(text, str):
                for sentence in re.split(r'(?<=[。！？；;])|\n', text):
                    for message in validate_comparative_mechanisms(sentence, element,
                        unit_gap=(fact or {}).get('unit_gap') if field == 'hypothesis' else None,
                        home_labels=(factories.get('home'), (facts or {}).get('home_factory')),
                        peer_labels=(factories.get('peer'), (facts or {}).get('peer_factory'))):
                        errors.append(diagnostic('COMPARATIVE_MECHANISM', path + '.' + field, sentence, message, element + '/' + field + message))
        errors.extend(role_action_diagnostics(row, element))
        availability = (context or {}).get('tasks_by_element', {}).get(element, {}).get('action_availability')
        errors.extend(action_diagnostics(row, element, availability or action_availability({'facts': facts or {}}, evidence, element), benchmark=True))
        refs, usable = row['evidence_ids'], []
        eligible = [ref for ref, source in sources.items() if _valid_source(source.get('source'))
                    and source.get('kind') in ('data_fact', 'document_basis') and element in source.get('elements', [])
                    and source.get('support_status', 'eligible') == 'eligible' and source.get('evidence_role') != 'context_only']
        task = (context or {}).get('tasks_by_element', {}).get(element, {})
        if 'eligible_evidence_ids' in task:
            eligible = [ident for ident in eligible if ident in task['eligible_evidence_ids']]
        if type(refs) is not list or not refs or any(type(ref) is not str for ref in refs):
            errors.append(diagnostic('EVIDENCE_EXISTS', path + '.evidence_ids', refs, eligible, element + '引用必须为非空字符串列表'))
        elif len(set(refs)) != len(refs):
            errors.append(diagnostic('EVIDENCE_UNIQUE', path + '.evidence_ids', refs, '非空且无重复的证据ID', element + '引用重复'))
        else:
            for ref in refs:
                source = sources.get(ref)
                if ref not in eligible:
                    rule = ('EVIDENCE_EXISTS' if not source or not _valid_source(source.get('source')) else
                            'EVIDENCE_ELEMENT' if element not in source.get('elements', []) else
                            'EVIDENCE_KIND' if source.get('kind') not in ('data_fact', 'document_basis') else 'EVIDENCE_AUTHORITY')
                    errors.append(diagnostic(rule, path + '.evidence_ids', ref, eligible,
                        element + '引用不存在、要素不匹配或不能支持原因：' + ref))
                else:
                    usable.append(source)
        missing = row['missing_evidence']
        if type(missing) is not list or not 1 <= len(missing) <= 8 or any(not isinstance(item, str) or not 2 <= len(item.strip()) <= 160 for item in missing):
            errors.append(diagnostic('MISSING_EVIDENCE_SCHEMA', path + '.missing_evidence', missing,
                '非空数组，至多八项未提供凭证，每项二至一百六十字', element + '必须列出缺失的实际凭证或生产记录'))
        else:
            for index, item in enumerate(missing):
                match = re.search(r'<[^>]+>|https?://|\{\{|```', item)
                if match:
                    errors.append(diagnostic('NO_MARKUP', path + f'.missing_evidence[{index}]', match.group(), '纯文本凭证名称', element + '证据缺口含非授权标记'))
        hypothesis = row['hypothesis'] if isinstance(row['hypothesis'], str) else ''
        for mechanism in ('设备故障', '泄漏', '事故', '停机', '工艺变更', '配方变更', '违规', '合同违约', '新增转固', '计薪规则调整', '折旧政策调整'):
            if mechanism in hypothesis and not any(source['kind'] == 'document_basis' and mechanism in str(source.get('text', '')) for source in usable):
                errors.append(diagnostic('MECHANISM_DOCUMENT', path + '.hypothesis', mechanism,
                    '只用本项实际引用且原文明示的相关机制，否则仅写证据边界', element + '具体机制缺少文档依据：' + mechanism))
        claims = [phrase for phrase in ('采购价上涨', '采购价下降', '实物单耗上升', '实物单耗下降', '收率下降', '收率上升') if phrase in hypothesis]
        if claims and not any(term in hypothesis for term in ('尚不能确认', '尚不能认定', '待核查', '待核实')):
            errors.append(diagnostic('ACTUAL_CLAIM_BOUNDARY', path + '.hypothesis', claims,
                '尚不能确认或待核查实际价格、耗用和收率', element + '实际价格/耗用结论缺少明确核查限定'))
    if context:
        errors.extend(observation_diagnostics(value, context))
    return errors


def validate_explanations(candidate, evidence, facts=None):
    """Compatibility API: strings for old consumers, never parsed for correction."""
    from enterprise.analysis_contract import legacy_errors
    return legacy_errors(validate_explanations_structured(candidate, evidence, facts))


def parse_explanations(candidate, evidence, facts=None, *, context=None):
    from enterprise.analysis_contract import legacy_errors
    errors = legacy_errors(validate_explanations_structured(candidate, evidence, facts, context=context))
    if errors:
        raise BenchmarkValidationError("；".join(errors))
    return deepcopy(_strict_json(candidate))


def filter_evidence(evidence, scope):
    """Keep applicable authorized document evidence; authorization remains APP's job.

    A document scope must include exact product/specification or an explicit v2
    applicability declaration, and all selected months or effective dates covering
    the whole period. Unknown metadata never implies global applicability.
    """
    allowed, diagnostics, seen = [], [], set()
    if not isinstance(evidence, (list, tuple)) or not isinstance(scope, dict):
        return [], ["证据与适用范围结构无效"]
    months = scope.get("months") or [scope.get("month")]
    if not isinstance(months, (list, tuple)) or not months or any(not isinstance(month, str) or not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month) for month in months):
        return [], ["证据筛选缺少有效分析期间"]
    start = date.fromisoformat(min(months) + "-01")
    year, month = map(int, max(months).split("-"))
    end = date(year, month, monthrange(year, month)[1])
    for index, row in enumerate(evidence):
        reason = None
        if not isinstance(row, dict):
            diagnostics.append(f"外部证据第{index + 1}项不是对象，未采用")
            continue
        ident, declared = row.get("id"), row.get("scope")
        reference_kind = row.get('kind') in {'industry_reference', 'market_reference'}
        applicable = isinstance(declared, dict) and declared.get('product') == scope.get('product') and declared.get('specification') == scope.get('specification')
        applicability = row.get('applicability')
        if reference_kind:
            from enterprise.evidence_references import reference_source_reason
            applicable = isinstance(declared, dict) and all(
                reference_source_reason(row, scope.get('product'), scope.get('specification'), month) is None
                for month in months)
        elif applicability is not None:
            from enterprise.knowledge_applicability import applicability_reason
            applicable = (isinstance(declared, dict) and isinstance(applicability, dict)
                          and applicability.get('schema_version') == 2
                          and applicability_reason(applicability, scope.get('product'), scope.get('specification')) is None)
        if not isinstance(ident, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", ident) or re.fullmatch(r"[BR]\d+", ident) or ident in seen:
            reason = "ID无效、冲突或使用保留ID"
        elif row.get("kind") not in ("document_basis", "industry_reference", "market_reference") or not _valid_source(row.get("source")):
            reason = "缺少可定位来源依据或未知证据类型"
        elif row.get("kind") in ("industry_reference", "market_reference") and row.get('support_status', 'eligible') != 'eligible':
            reason = "参考证据未通过授权准入"
        elif not isinstance(row.get("text"), str) or not 1 <= len(row["text"]) <= 12000:
            reason = "正文缺失或超长"
        elif type(row.get("elements")) is not list or not row["elements"] or any(not isinstance(element, str) or element not in ELEMENTS for element in row["elements"]):
            reason = "未声明有效要素范围"
        elif row.get("is_demo") or row.get("is_sim_case") or row.get("status") in ("draft", "revoked", "expired"):
            reason = "模拟、草稿或已撤销证据不适用于正式报告"
        elif not applicable:
            reason = "产品/规格适用范围不明确或不一致"
        else:
            declared_months = declared.get("months") or ([declared["month"]] if declared.get("month") else [])
            if declared_months:
                if not isinstance(declared_months, list) or any(not isinstance(value, str) for value in declared_months) or not set(months).issubset(declared_months):
                    reason = "文档适用月份不覆盖完整分析期"
            else:
                try:
                    effective = date.fromisoformat(declared.get("effective_from") or declared.get("valid_from") or "")
                    until = date.fromisoformat(declared.get("effective_to") or declared.get("valid_to") or "9999-12-31")
                    if effective > start or until < end:
                        reason = "文档生效范围不覆盖完整分析期"
                except (ValueError, TypeError):
                    reason = "缺少明确文档有效日期"
        if reason:
            diagnostics.append(f"外部证据{ident or index + 1}未采用：{reason}")
            continue
        seen.add(ident)
        allowed.append({**deepcopy(row), "support_status": "eligible"})
    return allowed, diagnostics


def _facts_evidence(benchmark):
    from .benchmark import benchmark_evidence
    return deepcopy(benchmark.get('evidence') or benchmark_evidence(benchmark))


def _llm_generate(payload, evidence, *, deadline=None, attempt_recorder=None,
                  request_fn=None, config=None, clock=None):
    """Run once plus at most one correction inside M2's killable 45-second worker."""
    from dataclasses import replace
    from attribution_gen import _model_hash
    from enterprise.model_gateway import capture_model_calls, configuration, generate_json
    from attribution_runtime import sanitize_model_calls
    import hashlib
    clock = clock or time.monotonic
    started = clock()
    from enterprise.prose_contract import model_stage_budget
    config = config or configuration(task='benchmark')
    if config.task is None and not config.routing_enabled:
        config = replace(config, task='benchmark')
    if config.configured_timeout is None:
        config = replace(config, configured_timeout=config.timeout)
    # 多模型分工：初稿用已选模型，修正轮可用独立注册模型（未配置则沿用初稿模型）。
    correction_cfg = None
    if config.registry_id != 'legacy':
        from enterprise.model_registry import correction_configuration
        try:
            correction_cfg = correction_configuration(task='benchmark')
        except Exception:
            correction_cfg = None
    hard_budget = model_stage_budget(payload, config=config)
    deadline = min(float(deadline), started + hard_budget) if deadline is not None else started + hard_budget - 2
    if not math.isfinite(deadline):
        raise ValueError('模型执行期限无效')
    if request_fn is None:
        from functools import partial
        request_fn = partial(generate_json, task='benchmark')
    result = {'schema': MODEL_RUN_SCHEMA, 'candidate': None, 'attempts': [],
              'correction': {'attempted': False, 'status': 'not_needed'}, 'failure_type': None,
              'model_calls': [], 'hard_budget_seconds': hard_budget, 'model_identity': config.identity()}
    grouped = grouped_model_context(payload, evidence, include_numeric=True)
    from enterprise.prose_contract import is_prose_mode, prose_prompt, provider_prose_context
    prose_enabled = is_prose_mode(payload)
    base_instruction = prose_prompt('benchmark') if prose_enabled else BENCHMARK_PROMPT
    prompt_version = 'benchmark-prose/1.4-bound-numeric-compact' if prose_enabled else PROMPT_VERSION
    # Keep grouped intact for validation and compact only the provider transport.
    provider_context = provider_prose_context(grouped)

    def record():
        result['model_calls'] = [call for attempt in result['attempts'] for call in attempt['model_calls']]
        if attempt_recorder:
            attempt_recorder(deepcopy({key: result[key] for key in ('attempts', 'correction', 'model_calls')}))

    for number in (1, 2):
        remaining = deadline-clock()-2
        if remaining < (12 if number == 2 else 1):
            if number == 2:
                result['correction']['status'] = 'skipped_insufficient_budget'
            else:
                result['failure_type'] = 'BudgetExhausted'
            record()
            break
        instruction, data = base_instruction, deepcopy(provider_context)
        if number == 2:
            result['correction'] = {'attempted': True, 'status': 'running'}
            instruction += '\n' + CORRECTION_INSTRUCTION
            data.update(previous_candidate=deepcopy(result['candidate']), validation_errors=list(result['attempts'][0]['diagnostics']),
                        validation_diagnostics=deepcopy(result['attempts'][0]['validation_diagnostics']))
        from enterprise.model_registry import REGISTRY_CONTRACT
        use_config = (correction_cfg or config) if number == 2 else config
        request_cap = use_config.timeout if use_config.budget_contract == REGISTRY_CONTRACT and use_config.registry_id != 'legacy' else min(40, use_config.timeout)
        timeout = min(request_cap, remaining)
        attempt = {'attempt': number, 'kind': 'initial' if number == 1 else 'correction',
                   'prompt_version': prompt_version if number == 1 else prompt_version+'/feedback',
                   'instruction_sha256': hashlib.sha256(instruction.encode()).hexdigest(),
                   'request_sha256': _model_hash(data), 'response_sha256': None,
                   'status': 'running', 'diagnostics': [], 'elapsed_seconds': None,
                   'request_timeout_seconds': round(timeout, 3), 'used': False, 'failure_type': None, 'model_calls': [],
                   'registry_id': use_config.registry_id, 'requested_model': use_config.model}
        result['attempts'].append(attempt)
        record()
        call_started = clock()
        try:
            with capture_model_calls() as calls:
                try:
                    candidate = request_fn(instruction, data, max_tokens=3500 if prose_enabled else 2300, config=replace(use_config, timeout=timeout))
                finally:
                    attempt['model_calls'] = sanitize_model_calls(calls, secret=use_config.api_key)
                    record()
            if type(candidate) is not dict:
                raise TypeError('模型网关没有返回JSON对象')
            attempt['response_sha256'] = _model_hash(candidate)
            from enterprise.analysis_contract import legacy_errors
            structured = validate_explanations_structured(candidate, evidence, payload['facts'], context=grouped)
            errors = legacy_errors(structured)
        except Exception as exc:
            attempt.update(status='unavailable', diagnostics=['模型调用未完成：'+type(exc).__name__],
                           failure_type=type(exc).__name__, elapsed_seconds=round(max(0, clock()-call_started), 3))
            result['failure_type'] = type(exc).__name__
            if number == 2:
                result['correction']['status'] = 'failed_unavailable'
            record()
            break
        attempt.update(status='rejected' if errors else 'validated', diagnostics=errors,
                       validation_diagnostics=structured,
                       elapsed_seconds=round(max(0, clock()-call_started), 3))
        if clock() >= deadline-1:
            attempt['status'] = 'budget_exhausted'
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


def generate_benchmark_analysis(product, specification, month, tables=None, *, evidence=None,
                                model_fn=None, model_version="not_configured", use_llm=True, versions=None):
    tables = load_merged_tables() if tables is None else tables
    if not isinstance(use_llm, bool):
        raise ValueError("use_llm必须为布尔值")
    if model_fn is not None and model_version == "not_configured":
        model_version = "injected_version_unspecified"
    benchmark = build_benchmark(product, specification, month, tables)
    from .cost_imports import digest, records
    result = {**benchmark, "schema_version": SCHEMA_VERSION, "facts": deepcopy(benchmark), "evidence": [], "sections": [],
              "text": benchmark.get("reason"), "assumptions": [], "used_llm": False,
              "generation_status": "insufficient_data", "fallback_reason": benchmark.get("reason"),
              "review_status": "needs_review", "input_data_hash": digest(records(tables)),
              "retrieval_diagnostics": deepcopy(getattr(evidence, 'diagnostics', {})),
              "versions": {"schema": SCHEMA_VERSION, "calculation": CALCULATION_VERSION,
                           "model": model_version, "prompt": PROMPT_VERSION,
                           "template": "benchmark-three-steps/2.0", "renderer": "natural-cited-narrative/2.0",
                           "upstream": deepcopy(versions or {})}, "validation": {"diagnostics": []}}
    allowed, diagnostics = filter_evidence(evidence or [], {"product": product, "specification": specification, "month": month})
    from enterprise.industry_benchmark import build_industry_comparison
    result['industry_comparison'] = build_industry_comparison(product, specification, month, tables, allowed)
    if not benchmark["available"]:
        result['validation']['diagnostics'] = diagnostics
        return result
    sources = _facts_evidence(benchmark)
    sources.extend(allowed)
    payload = {"product": product, "specification": specification, "month": month, "analysis_type": "跨厂对标",
               "facts": deepcopy(benchmark), "schema_version": SCHEMA_VERSION, "prompt_version": PROMPT_VERSION,
               "instruction": BENCHMARK_PROMPT, "versions": result["versions"]}
    payload['tasks_by_element'] = grouped_model_context(payload, sources)['tasks_by_element']
    explanations = None
    from enterprise.model_gateway import capture_model_calls
    from attribution_runtime import sanitize_model_calls
    model_run = {'schema': MODEL_RUN_SCHEMA, 'attempts': [], 'provider_call_count': 0, 'model_calls': [],
                 'correction': {'attempted': False, 'status': 'not_requested'}}
    status = "deterministic_requested" if not use_llm else "model_not_configured"
    fallback = "请求仅使用确定性分析" if not use_llm else "未注入已授权模型调用函数"
    has_difference = any(row['unit_gap'] != 0 for row in benchmark['elements'])
    if not has_difference:
        status, fallback = 'no_difference', None
    if model_fn is not None and use_llm and has_difference:
        from enterprise.analysis_service import validated_model
        trusted_worker = model_fn is validated_model
        if trusted_worker:
            payload['prose_mode'] = 'bound-numeric-prose/1'
            payload['prompt_version'] = 'benchmark-prose/1.4-bound-numeric-compact'
            from enterprise.prose_contract import prose_prompt
            payload['instruction'] = prose_prompt('benchmark')
            result['versions']['prompt'] = payload['prompt_version']
        model_run.update(execution='bounded_worker' if trusted_worker else 'injected_single_call',
                         hard_budget_seconds=None)
        try:
            model_kwargs = {}
            if trusted_worker:
                from enterprise.model_gateway import configuration
                from attribution_runtime import prepare_model_stage
                resolved_config = configuration(task='benchmark')
                stage_args, stage_budget = prepare_model_stage(payload, sources, task='benchmark', config=resolved_config)
                model_run.update(hard_budget_seconds=stage_budget, model_identity=stage_args.identity)
                model_kwargs['model_config'] = resolved_config
            with capture_model_calls() as calls:
                try:
                    candidate = model_fn(deepcopy(payload), deepcopy(sources), **model_kwargs)
                finally:
                    model_run['model_calls'] = sanitize_model_calls(calls)
            if trusted_worker:
                if not isinstance(candidate, dict) or candidate.get('schema') != MODEL_RUN_SCHEMA or not isinstance(candidate.get('attempts'), list):
                    raise ValueError('受控对标worker返回结构无效')
                model_run.update(attempts=candidate['attempts'], correction=candidate['correction'],
                                 provider_call_count=len(candidate['attempts']))
                for attempt in candidate['attempts']:
                    if attempt.get('status') != 'validated':
                        diagnostics.extend(f"模型尝试{attempt['attempt']}：{error}" for error in attempt.get('diagnostics', []))
                if candidate.get('failure_type'):
                    from attribution_runtime import StageExecutionError
                    raise StageExecutionError('model', candidate['failure_type'], '对标调用未完成', model_run=model_run)
                candidate = candidate.get('candidate')
            else:
                model_run['provider_call_count'] = 1
            explanations = parse_explanations(candidate, sources, benchmark,
                context=grouped_model_context(payload, sources, include_numeric=trusted_worker))
            status, fallback = "model_validated", None
        except BenchmarkValidationError as exc:
            status, fallback = "model_rejected", "模型输出未通过结构或证据校验"
            diagnostics.extend(str(exc).split("；"))
            for attempt in model_run['attempts']:
                attempt['used'] = False
        except Exception as exc:
            from attribution_runtime import StageExecutionError
            error_type = exc.error_type if trusted_worker and isinstance(exc, StageExecutionError) else type(exc).__name__
            if trusted_worker and isinstance(exc, StageExecutionError) and exc.model_run:
                model_run.update({key: exc.model_run[key] for key in ('attempts', 'correction') if key in exc.model_run})
                model_run['provider_call_count'] = len(model_run['attempts'])
            for attempt in model_run['attempts']:
                attempt['used'] = False
            status, fallback = "model_unavailable", "模型调用失败：" + error_type
            diagnostics.append(fallback)
    if model_run.get('execution') == 'bounded_worker':
        model_run['model_calls'] = [call for attempt in model_run['attempts']
                                   for call in sanitize_model_calls(attempt.get('model_calls'))]
    from enterprise.analysis_narrative import build_benchmark_narrative
    from enterprise.domain_profiles import load_domain_profile, product_definition
    profile = load_domain_profile()
    definition = product_definition(product, specification, profile)
    narrative = build_benchmark_narrative(benchmark, sources, explanations,
        reporting_unit=profile['reporting_unit'], industry_comparison=result['industry_comparison'],
        config={'industry_category': definition['category'] if definition else None},
        prose_mode=payload.get('prose_mode'))
    sections = narrative['sections']
    assumptions = [{'element': row['element'], 'claim_type': 'hypothesis', 'text': row['hypothesis'],
                    'evidence_ids': deepcopy(row['evidence_ids']), 'missing_evidence': deepcopy(row['missing_evidence'])}
                   for row in sections if row['claim_type'] != 'no_difference']
    # Report-facing prose includes only actually rendered, same-element mechanisms.
    # Keep model_explanations unchanged: deterministic K grounding is not a model claim.
    report_explanations = {'elements': {}}
    by_id = {source['id']: source for source in sources}
    for section in sections:
        supplement = {key: deepcopy(section[key]) for key in FIELDS}
        supplement['evidence_ids'] = [ident for ident in section['evidence_ids']
            if ident in by_id and by_id[ident].get('kind') in ('data_fact', 'document_basis')
            and section['element'] in by_id[ident].get('elements', [])]
        report_explanations['elements'][section['element']] = supplement
    for suggestion in result["suggestions"]:
        key = next((key for key in ELEMENTS if key in suggestion["title"]), None)
        if key:
            section = next(row for row in sections if row["element"] == key)
            suggestion["action"] = section["recommendation"]
            suggestion["evidence_ids"] = section["evidence_ids"]
        suggestion["delivery_status"] = "未送达"
        suggestion["approval_status"] = "待审批"
    result.update(evidence=sources, sections=sections, assumptions=assumptions, used_llm=explanations is not None,
                  generation_status=status, fallback_reason=fallback, payload=payload, model_run=model_run,
                  analysis_method="确定性会计对标＋经结构校验的AI假设" if explanations else "确定性会计对标",
                  text=narrative['text'], overview=narrative['overview'],
                  followup_criteria=narrative['followup_criteria'], narrative=narrative,
                  model_explanations=deepcopy(explanations), report_explanations=report_explanations,
                  validation={"numeric_facts": "program_rendered", "model_explanations": "passed" if explanations else "not_used",
                              "diagnostics": diagnostics, "semantic_review": "required"})
    return result
