"""Pure, deterministic narrative contract shared by attribution, benchmark and reports.

Inputs are already-authorized facts/evidence, NOT retrieval requests. No model,
file, configuration loader, network or database is called. Numeric prose is built
only from structured observations; document text is quoted, never used as an
observed value. Optional units/actors/category mappings are supplied by callers.

Public builders return ``overview, sections, text, evidence_ids,
followup_criteria``. Sections retain the legacy ``fact, hypothesis,
recommendation, missing_evidence, text`` keys while adding distinct observed,
mechanism, action, gap and provenance fields. ``model_core`` is retained for audit;
it cannot replace deterministic numeric observations or immediate actions.
"""
from __future__ import annotations

from copy import deepcopy
from decimal import Decimal, InvalidOperation, localcontext
import re

from enterprise.numeric import format_number, format_percent

SCHEMA_VERSION = "grounded-analysis-narrative/2.1"
LABELS = {"材料": "直接材料", "人工": "直接人工", "制费": "制造费用"}
_KIND = {"材料": "材料", "material": "材料", "materials": "材料",
         "人工": "人工", "labor": "人工", "labour": "人工",
         "制费": "制费", "overhead": "制费", "manufacturing": "制费"}
_ID = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,63}\Z")
_ACCOUNTING_KINDS = {"accounting_fact", "data_fact", "observed_fact"}
_REFERENCE_KINDS = {"market_reference", "industry_reference"}
_ZERO = Decimal(0)


def _d(value):
    if isinstance(value, dict):
        value = value.get("exact", value.get("value"))
    if value is None or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return result if result.is_finite() and abs(result) <= Decimal("1e30") else None


def _get(row, *keys):
    """First supplied numeric value, with explicit exact fields preferred."""
    for key in keys:
        if key in row:
            return _d(row[key])
    return None


def _n(value, *, signed=False, places=2):
    return format_number(value, signed=signed, places=places)


def _pct(before, after):
    if before is None or after is None or before == 0:
        return None
    with localcontext() as context:
        context.prec = 60
        return (after-before)/before*100


def _ids(values):
    return list(dict.fromkeys(value for value in values if isinstance(value, str) and _ID.fullmatch(value)))


def _marks(values):
    return " ".join("[" + value + "]" for value in _ids(values))


def _kind(element, config):
    return (config.get("element_kinds") or {}).get(element, _KIND.get(element, element))


def _element_matches(source, element, config):
    """Compare semantic aliases without mutating frozen evidence membership."""
    declared = source.get("elements") or []
    return not declared or any(_kind(value, config) == _kind(element, config) for value in declared)


def _settings(config, amount_unit, reporting_unit):
    settings = dict(config or {})
    settings["amount_unit"] = settings.get("amount_unit", amount_unit) or "元"
    settings["reporting_unit"] = settings.get("reporting_unit", reporting_unit) or "盒"
    settings["unit_cost_unit"] = settings.get("unit_cost_unit") or settings["amount_unit"] + "/" + settings["reporting_unit"]
    return settings


def common_action_criteria(config=None):
    """One shared completion statement, never a prerequisite for starting analysis."""
    return ((config or {}).get("followup_criteria") or
            "共同完成口径：以现有数据形成差异核对表，与已有汇总及明细归集金额勾稽，"
            "单列未闭合项；外部凭证取得后再复核业务机制，不以缺失凭证阻断当前核算分析，"
            "未核实前不确认实际节约或经营根因。")


def _scope_matches(source, context):
    scope = source.get("scope") or {}
    if not isinstance(scope, dict):
        return False
    for key, plural in (("product", "products"), ("specification", "specifications")):
        expected = context.get(key)
        if expected is None:
            continue
        declared = scope.get(plural)
        if declared is None and scope.get(key) is not None:
            declared = [scope[key]]
        if declared and expected not in declared and "*" not in declared:
            return False
    periods = scope.get("periods", scope.get("months"))
    requested = [context.get("period"), *context.get("months", [])]
    if periods and any(requested) and not any(period in periods for period in requested if period) and "*" not in periods:
        return False
    for key in ("factory", "process"):
        if context.get(key) and scope.get(key) and context[key] != scope[key]:
            return False
    return True


def _source_index(sources, context=None):
    """Conflicting duplicate IDs are unusable; exact duplicates are harmless."""
    result, conflicts = {}, set()
    for source in sources or []:
        if not isinstance(source, dict):
            continue
        ident = source.get("id")
        if not isinstance(ident, str) or not _ID.fullmatch(ident):
            continue
        source_context = context or {}
        if source.get("kind") == "market_reference":
            adjacent = []
            for period in source_context.get("months") or [source_context.get("period")]:
                if re.fullmatch(r"\d{4}-(?:0[1-9]|1[0-2])", str(period)):
                    year, month = map(int, period.split("-"))
                    adjacent.append(f"{year:04d}-{month-1:02d}" if month > 1 else f"{year-1:04d}-12")
            source_context = {**source_context, "months": [*source_context.get("months", []),
                                                          *source_context.get("previous_months", []), *adjacent]}
        if (source.get("support_status", "eligible") != "eligible"
                or source.get("evidence_role") == "context_only"
                or not _scope_matches(source, source_context)):
            continue
        if ident in result and result[ident] != source:
            conflicts.add(ident)
        else:
            result[ident] = source
    return {ident: source for ident, source in result.items() if ident not in conflicts}


def _admitted_sources(facts, sources, projections=None):
    if sources is not None:
        return list(sources)
    result = [{"kind": "accounting_fact", **deepcopy(row)} for row in facts.get("evidence", [])]
    for projection in (projections or {}).values():
        result.extend({"kind": "market_reference", "evidence_role": "market_reference", **deepcopy(row)}
                      for row in projection.get("evidence", []))
    return result


def _fact_ids(row):
    return _ids([row.get("evidence_id"), *row.get("evidence_ids", [])])


def _provenance(refs, index, *, accounting_ids=()):
    result = []
    for ident in _ids(refs):
        source = index.get(ident, {})
        kind = source.get("kind", "accounting_fact" if ident in accounting_ids else "unclassified")
        result.append({"id": ident, "kind": kind,
                       "evidence_role": source.get("evidence_role", kind),
                       "source": deepcopy(source.get("source")),
                       "scope": deepcopy(source.get("scope"))})
    return result


def _related_quote(quote, element, details, config):
    """Do not let an energy quote explain a depreciation-only observation."""
    kind = _kind(element, config)
    if kind != "制费" or not details:
        return True
    text = quote["quote"]
    focus, _ = _detail_focus(details)
    names = "、".join(str(row.get("name", "")) for row in (focus or details))
    families = ((r"折旧|计提|资产", r"折旧|计提|资产"),
                (r"蒸汽|能耗|能源|动力|电力|燃料", r"蒸汽|能耗|能源|动力|电|燃料"),
                (r"间接人工|工资|工时", r"间接人工|工资|工时"),
                (r"维修|维护", r"维修|维护"))
    matched = [pattern for pattern, _ in families if re.search(pattern, text)]
    if not matched:
        return True
    return any(re.search(pattern, text) and re.search(objects, names) for pattern, objects in families)


def select_mechanism(sources, element, *, preferred_ids=(), details=(), config=None, context=None):
    """Select a real, relevant, substantive K excerpt, including deterministic fallback.

    No quota is enforced when nothing is eligible. References and document bases
    are deliberately disjoint even if an upstream row has a misleading ``kind``.
    """
    from attribution_narrative import cited_quote
    settings = config or {}
    index = _source_index(sources, context)
    candidates = [source for source in index.values()
                  if source.get("kind") == "document_basis"
                  and source.get("evidence_role", "document_basis") == "document_basis"
                  and source["id"].startswith("K")
                  and source.get("elements") and _element_matches(source, element, settings)]
    preferred = list(preferred_ids)
    candidates.sort(key=lambda row: (row["id"] not in preferred,
                                    preferred.index(row["id"]) if row["id"] in preferred else 0))
    semantic_element = _kind(element, settings)
    for source in candidates:
        # The public quote helper expects native element keys. Alias only its
        # membership projection, never source text, ID or stored provenance.
        projected = {**source, "elements": [semantic_element]}
        quote = cited_quote([projected], [source["id"]], semantic_element)
        if quote and _related_quote(quote, element, details, settings):
            return {**quote, "kind": "document_basis", "evidence_role": "document_basis",
                    "scope": deepcopy(source.get("scope"))}
    return None


def _mechanism_text(quote, *, benchmark=False):
    if not quote:
        return "未提供适用的实质知识片段，尚不能从已有核算差额确认业务机制。"
    boundary = ("该原文仅支持核查方向；尚不能确认任一主体实际发生或解释主体间差异。" if benchmark else
                "该原文仅支持可能机制与核查方向；尚不能证明本期发生，也不能由费用倒推实际收率。")
    excerpt = quote['quote']
    if re.search(r"RSD|相对标准偏差|混合均匀", excerpt, re.I):
        clue = "若同口径检验记录显示偏离原文均匀性要求，则核查对应批次与成本归集；不能将均匀性指标当作实物耗用或收率。"
    elif re.search(r"收率|损耗", excerpt):
        metric = "收率与损耗" if "收率" in excerpt and "损耗" in excerpt else "收率" if "收率" in excerpt else "损耗"
        clue = f"若同工序实际{metric}记录显示原文所述偏离，则核查其与材料费用的对应关系；实际记录未提供时不认定发生。"
    elif re.search(r"蒸汽|能耗|能源", excerpt):
        clue = "若对应计量记录显示原文所述能源变化，则核查其与费用的对应关系；未有计量记录时不认定实际能耗异常。"
    else:
        clue = "若已有同口径记录与原文所述核对要求相关，则据此核查；原文没有支持的具体事件不作推断。"
    # Preserve the exact source excerpt; the only added mechanism is a conditional check.
    return f"知识资料原文“{excerpt}”[{quote['id']}]。条件核查线索：{clue}" + boundary


def _model_core(explanations, element, index, accounting_ids):
    row = ((explanations or {}).get("elements") or {}).get(element) or {}
    if not isinstance(row, dict):
        return {}
    refs = _ids(row.get("evidence_ids", []))
    if any(ident not in index and ident not in accounting_ids for ident in refs):
        return {}
    # The caller validates prose; retain all admitted references, not one per kind.
    return {key: deepcopy(row[key]) for key in
            ("hypothesis", "recommendation", "missing_evidence", "claim_type", "evidence_ids") if key in row}


def _detail_focus(details):
    comparable = [row for row in details if _get(row, "unit_effect_exact", "unit_effect") is not None]
    field = ("unit_effect_exact", "unit_effect")
    if not comparable:
        comparable = [row for row in details if _get(row, "amount_delta_exact", "amount_delta") is not None]
        field = ("amount_delta_exact", "amount_delta")
    largest = max((_get(row, *field).copy_abs() for row in comparable), default=_ZERO)
    return [row for row in comparable if largest and _get(row, *field).copy_abs() == largest], field


def comparison_label(payload, facts=None):
    """License 环比 only for explicitly comparable adjacent calendar periods."""
    facts = facts or payload.get("facts", payload)
    if facts.get("comparable") is False or payload.get("comparable") is False:
        return "较基期"
    before, after = facts.get("previous") or {}, facts.get("current") or {}
    if (before.get("factory") and after.get("factory") and before["factory"] != after["factory"]):
        return "较基期"
    def months(values, label):
        if values:
            raw = list(values)
        elif re.fullmatch(r"\d{4}-(?:0[1-9]|1[0-2])", str(label)):
            raw = [label]
        else:
            match = re.fullmatch(r"(\d{4})(?:-?Q|年第?)([1-4一二三四])(?:季度)?", str(label), re.I)
            if not match:
                return []
            quarter = "1234一二三四".index(match[2]) % 4
            raw = [f"{match[1]}-{quarter*3+month:02d}" for month in (1, 2, 3)]
        if any(not re.fullmatch(r"\d{4}-(?:0[1-9]|1[0-2])", str(value)) for value in raw):
            return []
        return [int(value[:4])*12 + int(value[5:])-1 for value in raw]
    current = months(payload.get("months") or facts.get("months") or after.get("months"),
                     payload.get("period", payload.get("current_period", payload.get("month", facts.get("period", facts.get("month"))))))
    previous = months(payload.get("previous_months") or facts.get("previous_months") or before.get("months"),
                      payload.get("previous_period", facts.get("previous_period", facts.get("previous_month", before.get("month")))))
    size = len(current)
    calendar = (size == 1 or size == 3 and current[0] % 12 in (0, 3, 6, 9)) if current else False
    if (calendar and len(previous) == size and current == list(range(current[0], current[0]+size))
            and previous == list(range(current[0]-size, current[0]))):
        return "环比"
    return "较基期"


def _accounting_bridge(output, unit, settings):
    """Rank signed accounting components, never rank unobserved business causes."""
    a = settings["amount_unit"]
    if output is None or unit is None:
        return "", {"available": False}
    components = [{"name": "产量", "amount": str(output)}, {"name": "单位成本", "amount": str(unit)}]
    ranked = sorted(components, key=lambda item: _d(item["amount"]).copy_abs(), reverse=True)
    with localcontext() as arithmetic:
        arithmetic.prec = 512
        net = output+unit
    if output == unit == 0:
        text = "产量与单位成本会计影响均为零，不指定主要来源。"
        relationship = "zero"
    elif output.copy_abs() == unit.copy_abs():
        text = (f"产量影响{_n(output, signed=True)}{a}与单位成本影响{_n(unit, signed=True)}{a}绝对值并列，"
                "属于并列会计来源，不指定唯一主要来源。")
        relationship = "tied"
    else:
        text = (f"按影响绝对值，主要会计来源为{ranked[0]['name']}影响{_n(_d(ranked[0]['amount']), signed=True)}{a}，"
                f"次要会计来源为{ranked[1]['name']}影响{_n(_d(ranked[1]['amount']), signed=True)}{a}。")
        relationship = "ranked"
    if output*unit < 0:
        text += f"两项方向相反、相互抵消，桥接净影响{_n(net, signed=True)}{a}，不能将金额方向替代单位成本方向。"
    text += "该排序仅解释会计桥接，不确认业务根因。"
    return text, {"available": True, "components": components, "ranked_components": ranked,
                  "relationship": relationship, "opposing": output*unit < 0,
                  "net_amount": str(net), "amount_unit": a}


def labor_ratio_boundary(hours_before, hours_after, volume_before, volume_after):
    """Aggregate arithmetic is not proof of either efficiency improvement or loss."""
    h0, h1, q0, q1 = map(_d, (hours_before, hours_after, volume_before, volume_after))
    if None in (h0, h1, q0, q1) or min(h0, h1, q0, q1) <= 0:
        return "汇总比率本身不能认定岗位效率提高或下降。"
    if h0 == h1 and q1 != q0:
        return ("总工时固定、产量减少，每单位工时上升和每工时产出下降是产量分母变化的机械结果；"
                if q1 < q0 else "总工时固定、产量增加，每单位工时下降和每工时产出上升是产量变化的机械结果；") + "不能据此认定岗位效率提高或下降。"
    if h0 != h1:
        return "总工时并非固定，单位工时与每工时产出同时受工时和产量变化影响；不能据汇总比率认定岗位效率提高或下降。"
    return "工时与产量均未变化，汇总比率持平不证明岗位效率完全相同。"


def _actor(element, settings):
    return (settings.get("actors") or {}).get(element, "财务分析人员")


def _immediate_action(element, details, settings, *, benchmark=False, no_difference=False, observed=None):
    if no_difference:
        return ""
    actor = _actor(element, settings)
    kind = _kind(element, settings)
    focus, _ = _detail_focus(details)
    names = "、".join(dict.fromkeys(str(item.get("name", "")) for item in (focus or list(details)[:2]) if item.get("name")))
    if kind == "材料":
        if not details:
            return f"建议{actor}先用已提供的材料成本汇总和产量复算单位成本与金额差额，区分规模和单位费用影响；未提供材料明细时不指定具体物料原因，不把市场报价当作结算价。同步核查当期采购合同是否触发调价条款，领退料与批次投料记录是否与归集金额一致。"
        if (observed or {}).get("physical_available"):
            return f"建议{actor}先复算已提供的{names or '材料'}成本明细，并对有源记录实物量及单位价格的项目分别核对量价桥接；未提供量价的项目保持空缺，工艺原因另行核查。同步核查{names or '材料'}当期采购合同调价条款、领退料单与批次投料记录，确认计价与实际耗用是否与归集金额一致。"
        return f"建议{actor}先用已提供的{names or '材料'}成本明细复算单位成本与金额差额，按现有产量区分规模和单位费用影响；采购计价与实物耗用另列待证，不把市场报价当作结算价。同步核查{names or '材料'}当期采购合同是否触发调价条款，领退料与批次投料记录是否与归集金额一致。"
    if kind == "人工":
        if (observed or {}).get("labor_available"):
            return f"建议{actor}复算已有人工归集金额、工时和产出的比率，分别核对工时强度与小时归集费用的净影响，不将归集比率解释为个人工资。同步调取班次考勤、加班与薪酬归集依据，核查工时口径与费率变动是否与实际用工安排一致。"
        return f"建议{actor}先复算已提供的人工单位成本、归集金额与产量差异；缺少可比工时时不拆分工时强度和小时费用，也不推定岗位效率或个人工资变化。同步调取班次与薪酬归集依据，核查是否存在口径调整。"
    if kind == "制费":
        if not details:
            return f"建议{actor}先用已提供的制造费用汇总和产量复算金额与单位费用差额；费用项目明细未提供，不指定折旧、动力等具体项目，也不推定分配基数原因。同步调取计提计算表与费用分摊底稿，核查计提期间与分配口径。"
        return f"建议{actor}按已提供的{names or '费用项目'}重排单位费用差额，核对金额、产量及现有分配口径，区分费用总额变化与分配基数影响。同步调取{names or '费用项目'}计提计算表与分摊底稿，核查计提期间、分配基数与总账勾稽是否闭合。"
    basis = f"已提供的{names or element}明细" if details else f"已提供的{element}汇总和产量"
    return f"建议{actor}按{basis}复算同口径金额及单位成本差额，区分已有核算观察与尚未证实的业务机制。"


def _gaps(element, settings, *, benchmark=False, coverage=None, details=None, observed=None):
    kind = _kind(element, settings)
    common = {"材料": ["实际采购计价与实物耗用原因仍需可核验的结算和领退料配对依据；现有成本汇总本身不能证明"],
              "人工": ["岗位效率或个人工资变化仍需可核验的班次与薪酬归集依据；现有归集比率本身不能证明"],
              "制费": ["具体经营事件仍需可核验的计提、计量及分配依据；现有费用差额本身不能证明"]}
    gaps = list(common.get(kind, ["支持具体业务原因的原始记录未提供"]))
    if kind == "材料" and (observed or {}).get("physical_available"):
        gaps = ["仅对已提供源记录量价的项目确认观察及恒等桥接；未提供项目不补零，采购条款、工艺事件和收率因果仍待原始依据核查"]
    if details is not None and not details:
        gaps.insert(0, "对应成本项目明细未提供，当前仅可完成汇总金额、单位成本及产量口径复核")
    if benchmark and not (coverage or {}).get("complete", False):
        gaps.insert(0, "可比主体的同口径明细尚未完整配对，不能把单方明细变动归为主体间原因")
    return gaps


def _section(element, label, fact, reason, details_text, mechanism, action, gaps, refs,
             index, accounting_ids, *, model=None, analysis_level="detailed", no_difference=False,
             prose_mode=None, prose_contract=None):
    model = model or {}
    if not no_difference:
        gaps = list(dict.fromkeys([*gaps, *[item for item in model.get("missing_evidence", []) if isinstance(item, str) and item]]))
    hypothesis = "本项单位成本无差异，保留同口径记录以便后续比较。" if no_difference else model.get("hypothesis", "")
    hypothesis_text = hypothesis + _marks(model.get("evidence_ids", [])) if hypothesis else ""
    accepted_action = "" if no_difference else model.get("recommendation", "")
    model_action_text = ("已校验补充建议：" + accepted_action + _marks(model.get("evidence_ids", []))) if accepted_action else ""
    recommendation = "\n".join(part for part in (action, model_action_text) if part)
    prose = hypothesis if prose_mode == 'bound-numeric-prose/1' and model and not no_difference else ''
    structure_mode = bool(prose and prose_contract is not None)
    if structure_mode:
        # 结构区契约（归因）：数值句已由程序渲染；散文只保留模型自己的解释（残差）
        from enterprise.prose_contract import prose_residual
        prose = prose_residual(hypothesis, element, prose_contract).strip()
    if no_difference:
        pieces = [fact, reason, *details_text, hypothesis]
    elif structure_mode:
        # 结构区事实恒渲染 + 模型残差散文；残差为空时不重复渲染 hypothesis
        pieces = [fact, reason, *details_text]
        if prose:
            pieces.append(prose)
        pieces.extend([mechanism if not prose or mechanism not in prose else '',
                       action, model_action_text])
        if gaps:
            pieces.append("证据缺口（不阻断当前核算分析）：" + "；".join(gaps).rstrip('。') + "。")
    elif prose:
        # 旧 prose 契约（对标）：模型散文整体呈现（含逐字复述句），不再重复结构事实
        pieces = [hypothesis_text]
        pieces.extend([mechanism if mechanism not in prose else '', '', action, model_action_text])
        if gaps:
            pieces.append("证据缺口（不阻断当前核算分析）：" + "；".join(gaps).rstrip('。') + "。")
    else:
        pieces = [fact, reason, *details_text, hypothesis_text, mechanism, action, model_action_text]
        if gaps:
            pieces.append("证据缺口（不阻断当前核算分析）：" + "；".join(gaps).rstrip('。') + "。")
    text = "\n".join(piece for piece in pieces if piece)
    refs = _ids([*refs, *model.get("evidence_ids", [])])
    return {"element": element, "title": label, "fact": fact, "observed_reason": reason,
            "reading_style": "contextual-reading/1.4",
            "numeric_explanation": "\n".join([fact, reason, *details_text]).strip(),
            "detail_breakdown": "\n".join(details_text),
            "comparison_explanation": "\n".join([reason, *details_text]).strip(),
            "mechanism_note": "" if no_difference else mechanism,
            "immediate_action": action, "evidence_gaps": gaps,
            "hypothesis": hypothesis or ("" if no_difference else mechanism),
            "recommendation": recommendation, "missing_evidence": gaps,
            "accepted_model_recommendation": accepted_action, "model_followup_action": accepted_action,
            "model_action_evidence_ids": _ids(model.get("evidence_ids", [])) if accepted_action else [],
            "claim_type": "no_difference" if no_difference else "hypothesis",
            "model_core": deepcopy(model), "text": text, "evidence_ids": refs,
             **({"prose": prose, "prose_mode": prose_mode} if prose_mode == 'bound-numeric-prose/1' and model and not no_difference else {}),
            "analysis_level": analysis_level, "provenance": _provenance(refs, index, accounting_ids=accounting_ids)}


def _labor_observation(factor, settings):
    if not factor.get("available"):
        return [], []
    exact = factor.get("exact") or {}
    value = lambda key: _d(exact[key] if key in exact else factor.get(key))
    required = [value(key) for key in ("hours_before", "hours_after", "cost_per_hour_before", "cost_per_hour_after", "hours_effect", "rate_effect")]
    if any(item is None for item in required):
        return [], []
    h0, h1, r0, r1, he, reffect = required
    a, u = settings["amount_unit"], settings["reporting_unit"]
    ident = factor.get("evidence_id")
    rate_pct = _pct(r0, r1)
    text = (f"总工时由{_n(h0)}变为{_n(h1)}小时，归集人工费用/小时由{_n(r0)}变为{_n(r1)}{a}/小时"
            + (f"（{format_percent(rate_pct, signed=True)}%）" if rate_pct is not None else "（前期小时费用为零，变化率无定义）")
            + f"；总工时影响{_n(he, signed=True)}{a}，小时归集费用影响{_n(reffect, signed=True)}{a}。")
    if he*reffect < 0:
        text += f"两项方向相反、相互抵消，净金额影响{_n(he+reffect, signed=True)}{a}。"
    if factor.get("unit_available"):
        keys = ("hours_per_box_before", "hours_per_box_after", "unit_hours_effect", "unit_rate_effect")
        values = [value(key) for key in keys]
        if all(item is not None for item in values):
            i0, i1, ue, ur = values
            text += (f"每{u}工时由{_n(i0, places=4)}变为{_n(i1, places=4)}小时/{u}"
                     + (f"（{format_percent(_pct(i0, i1), signed=True)}%）" if i0 else "")
                     + f"，对应单位人工影响{_n(ue, places=4, signed=True)}{a}/{u}；"
                     + f"小时归集费用变化影响{_n(ur, places=4, signed=True)}{a}/{u}。")
            if ue*ur < 0:
                text += f"单位桥接两项方向相反、相互抵消，净单位影响{_n(ue+ur, places=4, signed=True)}{a}/{u}。"
            q0, q1 = value("volume_before"), value("volume_after")
            if q0 is not None and q1 is not None and h0 > 0 and h1 > 0:
                with localcontext() as arithmetic:
                    arithmetic.prec = 60
                    p0, p1 = q0/h0, q1/h1
                text += (f"每工时产出由{_n(p0, places=4)}变为{_n(p1, places=4)}{u}/小时"
                         + (f"（{format_percent(_pct(p0, p1), signed=True)}%）" if p0 else "") + "。")
            text += labor_ratio_boundary(h0, h1, q0, q1)
    text += "小时归集费用不是个人工资或合同工资率。" + _marks([ident])
    return [text], _ids([ident])


def _physical_value(observation):
    """Accept explicit source observations only; never use BOM/reference ratios."""
    if not isinstance(observation, dict) or observation.get("quantity_basis") != "source_observed":
        return None
    quantity = _d(observation.get("quantity"))
    unit = observation.get("quantity_unit")
    if quantity is None or quantity < 0 or not isinstance(unit, str) or not unit.strip():
        return None
    price = _d(observation.get("unit_price")) if observation.get("price_basis") == "source_observed" else None
    if price is not None and price < 0:
        return None
    return {"quantity": quantity, "unit": unit, "price": price}


def _physical_reference_ids(row, index, element):
    refs = _fact_ids(row)
    if not refs or any(index.get(ref, {}).get("kind") not in _ACCOUNTING_KINDS for ref in refs):
        return []
    if any(index[ref].get("elements") and element not in index[ref]["elements"] for ref in refs):
        return []
    return refs


def _observed_material_notes(detail, index, element, settings, *, volumes=None, actor=None):
    """Render temporal pairs or one benchmark side, without inferring an actual.

    Returns text, exact IDs, and detached structured observed facts. A temporal
    bridge is admitted only when the supplied bridge exactly reconciles to its
    source-observed quantity/price operands and supplied detail amount values.
    """
    refs = _physical_reference_ids(detail, index, element)
    physical = detail.get("physical_observations") or {}
    if not refs or not isinstance(physical, dict):
        return [], [], []
    name = str(detail.get("name", element))
    currency, output_unit = settings["amount_unit"], settings["reporting_unit"]
    notes, observations = [], []
    temporal = "previous" in physical or "current" in physical
    slots = [("previous", physical.get("previous")), ("current", physical.get("current"))] if temporal else [("observed", physical)]
    available = {}
    for side, raw in slots:
        values = _physical_value(raw)
        if values is None:
            continue
        quantity, price, unit = values["quantity"], values["price"], values["unit"]
        amount = _get(detail, "amount_before_exact", "amount_before") if side == "previous" else _get(detail, "amount_after_exact", "amount_after") if side == "current" else _get(detail, "amount_exact", "amount")
        with localcontext() as arithmetic:
            arithmetic.prec = 512
            if price is not None and amount is not None and quantity*price != amount:
                continue
        available[side] = values
        subject = ("基期" if side == "previous" else "本期") if temporal else (actor or detail.get("factory") or "已提供主体")
        text = f"{subject}{name}源记录实际提供实物量{_n(quantity, places=4)}{unit}"
        if price is not None:
            text += f"、单位价格{_n(price, places=4)}{currency}/{unit}"
        else:
            text += "；实际单位价格未提供，不以归集成本或市场报价补算"
        text += "。" + _marks(refs)
        notes.append(text)
        record = {"name": name, "side": side, "actor": actor or detail.get("factory"),
                  "quantity": str(quantity), "quantity_unit": unit,
                  "unit_price": str(price) if price is not None else None,
                  "price_unit": currency + "/" + unit,
                  "quantity_basis": "source_observed", "price_basis": "source_observed" if price is not None else "not_provided",
                  "evidence_ids": refs, "evidence_role": "observed_physical_fact"}
        ratio = detail.get(side + "_observed_quantity_per_reporting_unit") if temporal else None
        if isinstance(ratio, dict) and ratio.get("available"):
            numerator, denominator, scale = _d(ratio.get("numerator")), _d(ratio.get("denominator")), _d(ratio.get("scale", 1))
            expected_output = (volumes or {}).get(side)
            if (numerator == quantity and denominator is not None and denominator > 0 and scale == 1
                    and expected_output is not None and denominator == expected_output
                    and ratio.get("unit") == unit + "/" + output_unit):
                with localcontext() as arithmetic:
                    arithmetic.prec = 512
                    value = numerator/denominator
                ratio_text = (f"{subject}{name}按源记录实物量及同口径产出计算的每{output_unit}实物量为{_n(value, places=6)}{unit}/{output_unit}；"
                              "这是实际记录比率，不是工艺收率或理论配方用量。" + _marks(refs))
                notes.append(ratio_text)
                record["quantity_per_reporting_unit"] = {"numerator": str(numerator), "denominator": str(denominator),
                                                          "unit": unit + "/" + output_unit, "exact": str(value)}
        observations.append(record)
    bridge = detail.get("observed_quantity_price_bridge") or {}
    if temporal and bridge.get("available") and {"previous", "current"} <= set(available):
        before, after = available["previous"], available["current"]
        effects = [_d(bridge.get(field)) for field in ("quantity_effect", "price_effect", "amount_delta")]
        b0, b1 = _get(detail, "amount_before_exact", "amount_before"), _get(detail, "amount_after_exact", "amount_after")
        conditions = (before["unit"] == after["unit"] == bridge.get("quantity_unit")
                      and before["price"] is not None and after["price"] is not None
                      and all(value is not None for value in [*effects, b0, b1])
                      and bridge.get("quantity_basis") == "source_observed_not_bom_or_amount_divided_by_reference_price")
        if conditions:
            with localcontext() as arithmetic:
                arithmetic.prec = 512
                quantity_effect = before["price"]*(after["quantity"]-before["quantity"])
                price_effect = after["quantity"]*(after["price"]-before["price"])
                valid = effects == [quantity_effect, price_effect, b1-b0] and quantity_effect+price_effect == b1-b0
            if valid:
                text = (f"{name}实际量价的核算原因是源记录实物量由{_n(before['quantity'], places=4)}变为{_n(after['quantity'], places=4)}{before['unit']}、"
                        f"单位价格由{_n(before['price'], places=4)}变为{_n(after['price'], places=4)}{currency}/{before['unit']}；"
                        f"按先实物量后单位价格桥接，实物量影响{_n(quantity_effect, signed=True)}{currency}、"
                        f"单位价格影响{_n(price_effect, signed=True)}{currency}，净金额影响{_n(b1-b0, signed=True)}{currency}。")
                if quantity_effect*price_effect < 0:
                    text += "两项方向相反、相互抵消。"
                text += "该恒等核算桥接不直接证明采购条款变化、损耗事件或收率变化。" + _marks(refs)
                notes.append(text)
                observations.append({"name": name, "evidence_role": "observed_quantity_price_bridge",
                    "quantity_effect": str(quantity_effect), "price_effect": str(price_effect), "amount_delta": str(b1-b0),
                    "quantity_unit": before["unit"], "amount_unit": currency, "evidence_ids": refs,
                    "quantity_basis": bridge["quantity_basis"]})
    return notes, refs if observations else [], observations


def _market_notes(sources, projections, *, context, element, settings, period_rows=(), focus_details=()):
    from enterprise.evidence_references import reference_source_reason
    from enterprise.tabular_knowledge import is_generic_table
    target_periods = context.get("months") or [context.get("period")]
    adjacent = {}
    for period in target_periods:
        if re.fullmatch(r"\d{4}-(?:0[1-9]|1[0-2])", str(period)):
            year, month = map(int, period.split("-"))
            adjacent[period] = f"{year:04d}-{month-1:02d}" if month > 1 else f"{year-1:04d}-12"
    index = _source_index(sources, {**context, "previous_months": [*context.get("previous_months", []), *adjacent.values()]})
    notes, provenance = [], []
    candidates, generic_groups = [], {}
    # Structured retrieved rows take precedence over cached/model projections.
    for source in index.values():
        if (source.get("kind") != "market_reference"
                or source.get("evidence_role", "market_reference") not in {"market_reference", "external_reference"}
                or not _element_matches(source, element, settings)):
            continue
        table = source.get("table_row") or {}
        columns = table.get("columns") or {}
        period = context.get("period", "")
        if "price" in columns:
            own_month = columns.get("month")
            if not is_generic_table(table):
                continue
            if reference_source_reason(source, context.get("product"), context.get("specification"), own_month,
                                       profile=settings.get("reference_profile")) is not None:
                continue
            identity = (tuple(columns.get(key) for key in ("material", "grade", "unit", "source_market"))
                        + (source["reference_profile"]["profile_sha256"], source.get("index_release_id")))
            generic_groups.setdefault(identity, {}).setdefault(own_month, []).append(source)
            continue
        if columns and re.fullmatch(r"\d{4}-\d{2}", period):
            month = int(period[-2:])
            previous = _d(columns.get(f"{month-1}月价格")) if month > 1 else None
            current = _d(columns.get(f"{month}月价格"))
            candidates.append({"name": columns.get("药材名称"), "before": previous, "after": current,
                               "unit": columns.get("单位"), "id": source["id"]})
        else:
            for row in source.get("market_observations", []):
                if row.get("month", row.get("period")) != period:
                    continue
                candidates.append({"name": row.get("material", row.get("name")),
                                   "before": _get(row, "previous_price_exact", "previous_price"),
                                   "after": _get(row, "current_price_exact", "current_price"),
                                   "unit": row.get("unit"), "id": source["id"]})
    # Generic long-form market observations require both independently admitted
    # rows. A projected previous_price never stands in for the missing source.
    for identity, observations in generic_groups.items():
        for period in target_periods:
            current_rows = observations.get(period, [])
            previous_rows = observations.get(adjacent.get(period), [])
            if len(current_rows) != 1 or len(previous_rows) != 1:
                continue
            current_source, previous_source = current_rows[0], previous_rows[0]
            candidates.append({"name": identity[0], "unit": identity[2], "period": period,
                "before": _d(previous_source["table_row"]["columns"]["price"]),
                "after": _d(current_source["table_row"]["columns"]["price"]),
                "id": current_source["id"], "evidence_ids": [previous_source["id"], current_source["id"]]})
    # A period adapter may supply one real row per included month. Preserve its
    # own month; do not average prices or present the last month as a quarter.
    for row in period_rows or []:
        period = row.get("month", row.get("period"))
        row_refs = _ids([row.get("evidence_id"), *row.get("evidence_ids", [])])
        ident = row_refs[0] if row_refs else None
        source = index.get(ident, {})
        if (period not in (context.get("months") or [context.get("period")])
                or not row_refs or any(index.get(ref, {}).get("kind") != "market_reference" for ref in row_refs)
                or any(index.get(ref, {}).get("evidence_role", "market_reference") not in {"market_reference", "external_reference"} for ref in row_refs)
                or any(not _element_matches(index.get(ref, {}), element, settings) for ref in row_refs)):
            continue
        columns = (source.get("table_row") or {}).get("columns") or {}
        if "price" in columns:
            # Already rendered above from two actual long-form source rows.
            continue
        if columns and re.fullmatch(r"\d{4}-\d{2}", str(period)):
            month = int(period[-2:])
            before = _d(columns.get(f"{month-1}月价格")) if month > 1 else None
            after = _d(columns.get(f"{month}月价格"))
            name, price_unit = columns.get("药材名称"), columns.get("单位")
        else:
            before, after = _get(row, "previous_price_exact", "previous_price"), _get(row, "current_price_exact", "current_price")
            name, price_unit = row.get("material", row.get("name")), row.get("unit")
        candidates.append({"name": name, "before": before, "after": after,
                           "unit": price_unit, "id": ident, "evidence_ids": row_refs, "period": period})
    seen = {row["id"] for row in candidates}
    for row in projections or []:
        ident = row.get("reference_evidence_id")
        source = index.get(ident, {})
        if (ident in seen or source.get("kind") != "market_reference"
                or "price" in ((source.get("table_row") or {}).get("columns") or {})
                or not _element_matches(source, element, settings)
                or source.get("evidence_role", "market_reference") not in {"market_reference", "external_reference"}):
            continue
        candidates.append({"name": row.get("name"), "before": _d(row.get("reference_price_before")),
                           "after": _d(row.get("reference_price_after")), "unit": row.get("price_unit"), "id": ident})
    rendered = set()
    for row in candidates:
        identity = (row["id"], row.get("period", context.get("period")), row["name"])
        if identity in rendered:
            continue
        rendered.add(identity)
        p0, p1, unit = row["before"], row["after"], row["unit"]
        if p0 is None or p1 is None or min(p0, p1) < 0 or not unit or not row["name"]:
            continue
        change = _pct(p0, p1)
        trend = "上行" if p1 > p0 else "下行" if p1 < p0 else "持平"
        text = (f"{row.get('period', context.get('period', '所选期间'))} {row['name']}同期市场参考价由{_n(p0)}变为{_n(p1)}{unit}"
                + (f"（{format_percent(change, signed=True)}%）" if change is not None else "（参考基期为零，变化率无定义）")
                + f"。若实际结算价同向{trend}且等级、期间相同，才可进一步检验价格因素；"
                "该参考趋势不证明实际采购价格、实物耗用或收率，也不证明它是主要驱动。" + _marks(row.get("evidence_ids", [row["id"]])))
        focus = next((item for item in focus_details if item.get("name") == row["name"]), None)
        if focus:
            text += (f"对焦点{row['name']}，若同口径实际结算价与参考价同向变化，才可核查价格因素；"
                     "若实际结算价未同向变化，则不能以该参考趋势解释单位消耗成本差额，仍须分别核查计价与实际实物耗用记录；"
                     "无源记录时不把费用除以参考价所得比率当作实际耗用。")
        notes.append(text)
        for ident in row.get("evidence_ids", [row["id"]]):
            provenance.append({"id": ident, "name": row["name"], "focus_relevant": focus is not None,
                                "text": text, "kind": "market_reference", "evidence_role": "market_reference",
                               "before": str(p0), "after": str(p1), "unit": unit,
                               "period": row.get("period", context.get("period")),
                               "source": deepcopy(index[ident].get("source"))})
    return notes, provenance


def render_yield_comparisons(observed_yields, sources, *, context=None):
    """Compare ONLY explicitly measured yields to a real, compatible baseline.

    Each pair has ``observation`` (measurement_type='actual_measured', value,
    metric, unit, process, scope, period, evidence_id) and ``baseline`` with the
    same metric/unit/process/scope, is_baseline=True, baseline_type in
    {'standard','actual_measured'}, evidence_id and either value or lower/upper.
    A standard must explicitly list valid_for_periods; an earlier measured
    baseline must list comparable_periods. Nothing is derived from cost/price.
    """
    index = _source_index(sources, context)
    result = []
    for pair in observed_yields or []:
        if not isinstance(pair, dict):
            continue
        observed, baseline = pair.get("observation") or {}, pair.get("baseline") or {}
        if observed.get("measurement_type") != "actual_measured" or baseline.get("is_baseline") is not True:
            continue
        if baseline.get("baseline_type") not in {"standard", "actual_measured"}:
            continue
        if any(not observed.get(key) or observed.get(key) != baseline.get(key) for key in ("metric", "unit", "process", "scope")):
            continue
        period = observed.get("period")
        if not period or ((context or {}).get("period") and period != context["period"]):
            continue
        permitted = baseline.get("valid_for_periods") if baseline["baseline_type"] == "standard" else baseline.get("comparable_periods")
        if period not in (permitted or []):
            continue
        observed_source = index.get(observed.get("evidence_id"), {})
        baseline_source = index.get(baseline.get("evidence_id"), {})
        if observed_source.get("kind") not in _ACCOUNTING_KINDS:
            continue
        # Source-bound measurements are required; merely labelling a calculated
        # cost/market-price ratio 'actual_measured' cannot license a comparison.
        measured = observed_source.get("measurement") or observed_source.get("observation") or {}
        source_baseline = baseline_source.get("baseline") or {}
        observed_keys = ("measurement_type", "metric", "unit", "process", "scope", "period")
        baseline_keys = ("is_baseline", "baseline_type", "metric", "unit", "process", "scope")
        if (any(measured.get(key) != observed.get(key) for key in observed_keys)
                or _d(measured.get("value")) != _d(observed.get("value"))
                or any(source_baseline.get(key) != baseline.get(key) for key in baseline_keys)
                or any(_d(source_baseline.get(key)) != _d(baseline.get(key)) for key in ("value", "lower", "upper"))):
            continue
        period_key = "valid_for_periods" if baseline["baseline_type"] == "standard" else "comparable_periods"
        if period not in source_baseline.get(period_key, []):
            continue
        if baseline["baseline_type"] == "standard":
            if (baseline_source.get("kind") != "document_basis"
                    or baseline_source.get("evidence_role", "document_basis") != "document_basis"):
                continue
        elif baseline_source.get("kind") not in _ACCOUNTING_KINDS or baseline.get("measurement_type") != "actual_measured":
            continue
        value = _d(observed.get("value"))
        lower = _get(baseline, "lower", "value")
        upper = _get(baseline, "upper", "value")
        if value is None or value < 0 or (lower is None and upper is None) or (lower is not None and upper is not None and lower > upper):
            continue
        unit = observed["unit"]
        if unit in {"%", "％"} and any(x is not None and (x < 0 or x > 100) for x in (value, lower, upper)):
            continue
        within = (lower is None or value >= lower) and (upper is None or value <= upper)
        interval = (f"{_n(lower)}—{_n(upper)}{unit}" if lower is not None and upper is not None and lower != upper else
                    f"{_n(lower)}{unit}" if lower is not None and lower == upper else
                    f"不低于{_n(lower)}{unit}" if lower is not None else f"不高于{_n(upper)}{unit}")
        refs = _ids([observed["evidence_id"], baseline["evidence_id"]])
        text = (f"{period}已实测的{observed['process']}{observed['metric']}为{_n(value)}{unit}，"
                f"同指标、单位、工序及范围的真实基准为{interval}，"
                + ("在所给基准范围内" if within else "偏离所给基准，待核查")
                + "；该比较不单独证明成本变化的因果。" + _marks(refs))
        result.append({"text": text, "within_baseline": within, "evidence_ids": refs,
                       "observation": deepcopy(observed), "baseline": deepcopy(baseline),
                       "evidence_role": "measured_yield_comparison"})
    return result


def render_industry_comparisons(comparison, sources, *, context=None, config=None):
    """Render supplied mapped actual values versus P50; never fetch an observation.

    Accepts existing industry-comparison/1 rows. Requires explicit category mapping
    and a matching selected month/period. ``source_reported_home`` is intentionally
    ignored: it is not an actual selected-product observation.
    """
    if not isinstance(comparison, dict) or not comparison.get("available"):
        return []
    settings, context = config or {}, context or {}
    category = settings.get("industry_category", comparison.get("category", comparison.get("product_category")))
    period = context.get("period")
    declared_period = comparison.get("period", comparison.get("month"))
    if not category or not period or declared_period != period:
        return []
    if context.get("product") and comparison.get("product") not in (None, context["product"]):
        return []
    if context.get("specification") and comparison.get("specification") not in (None, context["specification"]):
        return []
    index = _source_index(sources, context)
    results = []
    for row in comparison.get("rows", []):
        source = index.get(row.get("evidence_id"), {})
        if (row.get("category") != category or source.get("kind") != "industry_reference"
                or source.get("evidence_role", "industry_reference") not in {"industry_reference", "benchmark_reference", "external_reference"}):
            continue
        unit = row.get("unit")
        if not unit or not row.get("metric"):
            continue
        columns = (source.get("table_row") or {}).get("columns") or {}
        if columns:
            if "p50" in columns:
                if (columns.get("category") != category or columns.get("metric") != row["metric"]
                        or columns.get("unit") != unit):
                    continue
                median = _d(columns.get("p50"))
            else:
                if columns.get("产品类别") != category or columns.get("指标") != row["metric"]:
                    continue
                raw = str(columns.get("行业P50", ""))
                if unit == "%" and not raw.endswith("%"):
                    continue
                median = _d(raw[:-1] if unit == "%" else raw)
        else:
            # An unbound cached P50 is not a reference observation.
            continue
        if median is None:
            continue
        for side in ("home", "peer"):
            observed = row.get(side) or {}
            value = _d(observed)
            measurement_scope = observed.get("measurement_scope")
            source_key = (observed.get("source") or {}).get("key", {})
            source_key = source_key if isinstance(source_key, dict) else {}
            source_period = source_key.get("month", source_key.get("period", source_key.get("月份")))
            source_product = source_key.get("product", source_key.get("产品名称"))
            explicit_scope = measurement_scope in {"selected_product_specification_month", "selected_product_specification_period"}
            bound_scope = (source_period == period and (not context.get("product") or source_product == context["product"]))
            if (value is None or not observed.get("source") or not observed.get("factory")
                    or not (explicit_scope or bound_scope)):
                continue
            gap = value-median
            difference_unit = "个百分点" if unit == "%" else unit
            reference_period = str(row.get("reference_year", "")) + "年" if row.get("reference_year") else row.get("reference_period", "文档参照期")
            identity = _marks([row.get("evidence_id"), *observed.get("evidence_ids", [])])
            def _row_value(raw):
                if isinstance(raw, dict):
                    return _d(raw.get("value"))
                return _d(raw)
            side_p25, side_p75 = _row_value(row.get("p25")), _row_value(row.get("p75"))
            position_label = str(observed.get("position_label") or "")
            from enterprise.industry_benchmark import POSITION_LEVEL
            level = POSITION_LEVEL.get(observed.get("position"), "")
            text = (f"{observed['factory']} {period}的{row['metric']}为{_n(value)}{unit}，"
                    f"对应已映射类别“{category}”{reference_period}"
                    + (f"行业P25为{_n(side_p25)}{unit}、" if side_p25 is not None else "")
                    + f"P50为{_n(median)}{unit}"
                    + (f"、P75为{_n(side_p75)}{unit}" if side_p75 is not None else "")
                    + (("，处于中位参考位置" if gap == 0
                        else f"，{'高于' if gap > 0 else '低于'}中位参考值{_n(abs(gap))}{difference_unit}"))
                    + (f"，处于{position_label}（{level}）" if position_label else "")
                    + "。这是所选期间实际核算值与类别参考的比较，行业统计窗口未必相同，不能据此认定效率优劣、业务原因或可节约金额。")
            evaluation = str(row.get("source_evaluation") or "").strip()
            if evaluation:
                evaluation_boundary = str(row.get("source_evaluation_boundary") or "原文件评价，未经独立验证")
                text += f"原文件对标评价：{evaluation}（{evaluation_boundary}）。"
            text += identity
            results.append({"text": text, "element": row.get("element"), "side": side,
                            "metric": row["metric"], "observed": str(value), "p50": str(median),
                            "gap": str(gap), "unit": unit, "period": period, "category": category,
                            "evidence_ids": _ids([row["evidence_id"], *observed.get("evidence_ids", [])]),
                            "evidence_role": "industry_reference", "observed_source": deepcopy(observed["source"]),
                            "reference_source": deepcopy(source.get("source"))})
    return results


def _finish(overview, sections, sources, context, settings, industry_comparison, observed_yields):
    industry = render_industry_comparisons(industry_comparison, sources, context=context, config=settings)
    yields = render_yield_comparisons(observed_yields, sources, context=context)
    followup = common_action_criteria(settings) if any(row["immediate_action"] for row in sections) else ""
    parts = [overview, *[row["text"] for row in sections], *[row["text"] for row in industry], *[row["text"] for row in yields], followup]
    refs = _ids([*re.findall(r"\[([A-Za-z][A-Za-z0-9_-]{0,63})\]", overview),
                 *[ref for row in [*sections, *industry, *yields] for ref in row["evidence_ids"]]])
    index = _source_index(sources, context)
    result = {"schema_version": SCHEMA_VERSION, "overview": overview, "sections": sections,
              "text": "\n\n".join(part for part in parts if part), "followup_criteria": followup,
              "industry_comparisons": industry, "yield_comparisons": yields,
              "evidence_ids": refs, "provenance": _provenance(refs, index),
              "sources": [deepcopy(index[ident]) for ident in refs if ident in index]}
    return result


def build_attribution_narrative(payload, explanations=None, sources=None, *, detailed=False,
                                amount_unit="元", reporting_unit="盒", industry_comparison=None,
                                observed_yields=None, config=None, prose_contract=None):
    """Render canonical monthly OR aggregated-period facts and legacy projections.

    Canonical ``facts.elements`` wins over ``payload.elements`` projections. A
    report adapter supplies the same fact fields after aggregating its own period;
    this function never averages monthly ratios or assumes a month-end snapshot.
    """
    settings = _settings(config, amount_unit, reporting_unit)
    facts = payload.get("facts", payload)
    if "reference_profile" not in settings and isinstance(payload.get("domain_config"), dict):
        settings["reference_profile"] = payload["domain_config"]
    period = str(payload.get("period", payload.get("current_period", payload.get("month", facts.get("period", facts.get("month", "所选期间"))))))
    previous_periods = list(payload.get("previous_months", []))
    previous_period = payload.get("previous_period", facts.get("previous_period", facts.get("previous_month")))
    if previous_period:
        previous_periods.append(previous_period)
    context = {"period": period, "months": list(payload.get("months", [])), "previous_months": previous_periods,
               "product": payload.get("product", facts.get("product")),
               "specification": payload.get("specification", facts.get("specification"))}
    if not facts.get("available", True):
        note = f"{context['product'] or ''} {period}：{facts.get('reason', '缺少可比事实')}。不生成跨期归因。"
        return _finish(note, [], [], context, settings, None, None)
    elements = facts.get("elements") or {}
    if not isinstance(elements, dict):
        raise ValueError("attribution facts.elements must be a mapping")
    projections = payload.get("elements", {}) if payload is not facts else {}
    admitted = _admitted_sources(facts, sources, projections)
    index = _source_index(admitted, context)
    a, u, cu = settings["amount_unit"], settings["reporting_unit"], settings["unit_cost_unit"]
    labels = {**LABELS, **settings.get("element_labels", {})}
    before, after = facts.get("previous") or {}, facts.get("current") or {}
    comparison = comparison_label(payload, facts)
    q0, q1 = _d(before.get("volume")), _d(after.get("volume"))
    total = _get(facts, "amount_delta_exact", "amount_delta")
    if total is None:
        total = _d((payload.get("金额口径") or {}).get("总变动额"))
    values = {key: _get(row, "amount_delta_exact", "amount_delta", "change_amount") for key, row in elements.items()}
    if total is None and values and all(value is not None for value in values.values()):
        total = sum(values.values(), _ZERO)
    largest = max((abs(value) for value in values.values() if value is not None), default=_ZERO)
    leaders = [labels.get(key, key) for key, value in values.items() if value is not None and largest and abs(value) == largest]
    overview = f"{period} {context['product'] or ''}总成本变动{_n(total, signed=True)}{a}。"
    if q0 is not None and q1 is not None:
        overview += f"产量由{_n(q0)}变为{_n(q1)}{u}。"
    outputs = [_get(row, "volume_effect_exact", "volume_effect", "output_effect") for row in elements.values()]
    units = [_get(row, "unit_effect_exact", "unit_effect", "unit_cost_effect") for row in elements.values()]
    if outputs and all(value is not None for value in [*outputs, *units]):
        output_total, unit_total = sum(outputs, _ZERO), sum(units, _ZERO)
        overview += f"其中产量影响{_n(output_total, signed=True)}{a}、单位成本影响{_n(unit_total, signed=True)}{a}。"
        if total:
            with localcontext() as arithmetic:
                arithmetic.prec = 60
                overview += (f"产量和单位成本影响分别占净变动的{format_percent(output_total/total*100)}%"
                             f"和{format_percent(unit_total/total*100)}%。")
    if leaders:
        overview += "、".join(leaders) + ("是金额变动绝对值最大的要素。" if len(leaders) == 1 else "的金额变动绝对值并列最大，不能只选排序首项。")
    elif values and all(value == 0 for value in values.values()):
        overview += "各要素金额均无净变动，不指定唯一主因。"
    if total == 0:
        overview += "总净变动为零，贡献度无定义；仍分别展示可能存在的正反向抵消。"
    if q0 is not None and q1 is not None and q1 < q0:
        overview += "产量减少导致的支出下降不等于成本管控节约。"
    overview += _marks([*_fact_ids(before), *_fact_ids(after)])
    sections = []
    ordered = ([key for key in LABELS if key in elements] + [key for key in elements if key not in LABELS]
               if payload.get('prose_mode') == 'bound-numeric-prose/1' else
               sorted(elements, key=lambda key: -(abs(values[key]) if values[key] is not None else _ZERO)))
    for key in ordered:
        row, projection = elements[key], projections.get(key, {})
        delta = values[key]
        unit0, unit1 = _get(row, "unit_before_exact", "unit_before"), _get(row, "unit_after_exact", "unit_after")
        output, unit = _get(row, "volume_effect_exact", "volume_effect", "output_effect"), _get(row, "unit_effect_exact", "unit_effect", "unit_cost_effect")
        ratio = _pct(unit0, unit1)
        contribution = None if total == 0 else _get(row, "contribution_exact", "contribution", "contribution_pct")
        refs = _fact_ids(row)[:1]
        accounting_ids = _ids([*_fact_ids(row), *[item.get("evidence_id") for item in row.get("detail", [])], (row.get("labor_factors") or {}).get("evidence_id")])
        fact = (f"{labels.get(key, key)}单位成本由{_n(unit0)}变为{_n(unit1)}{cu}"
                + (f"（{comparison}{format_percent(ratio, signed=True)}%）" if ratio is not None else "（基期为零或未提供，变化率无定义）")
                + f"，金额变动{_n(delta, signed=True)}{a}，"
                + (f"金额贡献度{format_percent(contribution)}%。" if contribution is not None else "贡献度无定义或未提供。") + _marks(refs))
        bridge_text, accounting_bridge = _accounting_bridge(output, unit, settings)
        accounting_bridge.update(volume_before=str(q0) if q0 is not None else None,
                                 volume_after=str(q1) if q1 is not None else None,
                                 unit_cost_before=str(unit0) if unit0 is not None else None,
                                 unit_cost_after=str(unit1) if unit1 is not None else None,
                                 formula="(Q1-Q0)*U0 + Q1*(U1-U0)")
        reason_parts = [bridge_text] if bridge_text else []
        if delta is not None and total is not None and delta*total < 0:
            reason_parts.append("本项与总净变动方向相反，负贡献表示抵消而非反转为正贡献。")
        details = [item for item in row.get("detail", []) if isinstance(item, dict)]
        focus, focus_fields = _detail_focus(details)
        market, market_provenance = _market_notes(admitted, projection.get("top_materials", []), context=context,
            element=key, settings=settings, period_rows=(payload.get("market_reference") or {}).get("rows", []),
            focus_details=focus) if _kind(key, settings) == "材料" else ([], [])
        focus_market = {item["text"] for item in market_provenance if item.get("focus_relevant")}
        focus_blocks = []
        if focus:
            focus_name = "单位成本" if focus_fields[0].startswith("unit") else "金额"
            reason_parts.append(f"{focus_name}影响绝对值{'并列最大' if len(focus) > 1 else '最大'}的明细核算对象为" + "；".join(
                f"{item['name']}（{_n(_get(item, *focus_fields), signed=True)}{a}）" + _marks(_fact_ids(item)) for item in focus) + "，应先定位这些已观察差额，尚不能直接确认为实际采购、耗用或经营事件。")
            for item in focus:
                before_cost, after_cost = _get(item, "unit_before_exact", "unit_before"), _get(item, "unit_after_exact", "unit_after")
                block = ""
                if before_cost is not None and after_cost is not None:
                    block = (f"{item['name']}的直接核算差异是单位{'消耗成本' if _kind(key, settings) == '材料' else '费用'}由{_n(before_cost)}变为{_n(after_cost)}{cu}，"
                             f"形成上述{_n(_get(item, *focus_fields), signed=True)}{a}影响；这是费用记录的变化，不是已经证实的物理耗用或市场结算原因。" + _marks(_fact_ids(item)))
                matching = list(dict.fromkeys(record["text"] for record in market_provenance
                                             if record.get("focus_relevant") and record["name"] == item.get("name")))
                block += "".join(matching)
                if block:
                    reason_parts.append(block)
                    focus_blocks.append({"text": block, "kind": "market_reference" if matching else "observed_focus",
                                         "required": bool(matching)})
            refs.extend(ref for item in focus for ref in _fact_ids(item))
        reason = "".join(reason_parts) + (_marks(_fact_ids(row)[:1]) if reason_parts else "")
        selected = list(details[:3 if detailed else 2])
        for item in focus:
            if item not in selected:
                selected.append(item)
        details_text = []
        for item in selected:
            if _get(item, "amount_delta_exact", "amount_delta") is None:
                continue
            text = (f"{item.get('name', key)}金额变动{_n(_get(item, 'amount_delta_exact', 'amount_delta'), signed=True)}{a}，"
                    f"单位{'消耗成本' if _kind(key, settings) == '材料' else '费用'}由{_n(item.get('unit_before'))}变为{_n(item.get('unit_after'))}{cu}。")
            if _get(item, "volume_effect") is not None:
                text += f"其产量影响{_n(_get(item, 'volume_effect'), signed=True)}{a}。"
            details_text.append(text + _marks(_fact_ids(item)))
            refs.extend(_fact_ids(item))
        physical_observations = []
        if _kind(key, settings) == "材料":
            for item in details:
                physical_text, physical_refs, observations = _observed_material_notes(
                    item, index, key, settings, volumes={"previous": q0, "current": q1})
                details_text.extend(physical_text)
                refs.extend(physical_refs)
                physical_observations.extend(observations)
        if _kind(key, settings) == "人工":
            labor_text, labor_refs = _labor_observation(row.get("labor_factors") or {}, settings)
            details_text.extend(labor_text)
            refs.extend(labor_refs)
        details_text.extend(line for line in market if line not in focus_market)
        refs.extend(item["id"] for item in market_provenance)
        core = _model_core(explanations, key, index, accounting_ids)
        quote = select_mechanism(admitted, key, preferred_ids=core.get("evidence_ids", []), details=details, config=settings, context=context)
        if quote:
            refs.append(quote["id"])
        labor_factor = row.get("labor_factors") or {}
        factor_effects = [((labor_factor.get("exact") or {}).get(name, labor_factor.get(name)))
                          for name in ("hours_effect", "rate_effect", "unit_hours_effect", "unit_rate_effect")]
        physical_change = any(record.get("evidence_role") == "observed_quantity_price_bridge"
                              and any(_d(record.get(field)) not in (None, 0) for field in ("quantity_effect", "price_effect"))
                              for record in physical_observations)
        no_difference = (delta == output == unit == 0 and not physical_change
                         and all(_d(value) in (None, 0) for value in factor_effects)
                         and all(_get(item, "amount_delta_exact", "amount_delta") == 0
                                 and all(_get(item, name) in (None, 0) for name in ("unit_effect", "volume_effect"))
                                 for item in details))
        if no_difference:
            core, quote = {}, None
            refs = [ident for ident in refs if ident in accounting_ids]
            details_text = [line for line in details_text if not any(f"[{item['id']}]" in line for item in market_provenance)]
            market_provenance = []
        observed_availability = {"labor_available": bool((row.get("labor_factors") or {}).get("available")),
                                 "physical_available": bool(physical_observations)}
        action = _immediate_action(key, details, settings, no_difference=no_difference,
                                   observed=observed_availability)
        gaps = _gaps(key, settings, details=details, observed=observed_availability) if not no_difference else []
        section = _section(key, labels.get(key, key), fact, reason, details_text, _mechanism_text(quote), action, gaps,
                           refs, index, accounting_ids, model=core, no_difference=no_difference,
                           prose_mode=payload.get('prose_mode'), prose_contract=prose_contract,
                           analysis_level=projection.get("analysis_level", row.get("analysis_level", "detailed")))
        section["market_references"] = market_provenance
        section["mechanism_evidence"] = quote
        section["applicable_document_ids"] = [source["id"] for source in index.values()
            if not no_difference and select_mechanism([source], key, details=details, config=settings, context=context)]
        section["physical_observations"] = physical_observations
        section["accounting_bridge"] = accounting_bridge
        section["focus_statements"] = focus_blocks if not no_difference else []
        section["comparison_label"] = comparison
        sections.append(section)
    result = _finish(overview, sections, admitted, context, settings,
                     industry_comparison or payload.get("industry_comparison"), observed_yields or payload.get("observed_yields"))
    alerts = payload.get("告警_环比超正负10%", facts.get("alerts", [])) or []
    notes = []
    for alert in alerts:
        value = _get(alert, "mom_pct_decimal", "环比%", "mom_pct")
        if value is not None and abs(value) > 10:
            notes.append(f"{alert.get('要素', alert.get('element', '成本要素'))}{comparison}{format_percent(value, signed=True)}%")
    if notes:
        result["alert_text"] = "重点告警：" + "；".join(notes) + "，严格超过±10%，优先按已有核算数据复核。"
        result["text"] += "\n\n" + result["alert_text"]
    else:
        result["alert_text"] = ""
    return result


def _cost_detail_summary(details, side, expected, actor, settings, *, source_index=None, coverage=None):
    """Top-four composition only closes against full, source-bound side details.

    A residual is the sum of actual omitted records, never total minus a partial
    export. Unknown, negative or unreconciled records are shown as provided data.
    """
    rows = []
    provided = [detail for detail in details if isinstance(detail.get(side), dict)]
    for detail in provided:
        value = _get(detail[side], "unit_cost_exact", "unit_cost")
        refs = _fact_ids(detail[side])
        if value is not None:
            rows.append({"name": str(detail.get("name", "费用项目")), "unit_cost": value, "evidence_ids": refs})
    if not rows:
        return "", {"available": False, "side": side}
    with localcontext() as arithmetic:
        arithmetic.prec = 512
        denominator = sum((row["unit_cost"] for row in rows), _ZERO)
    verified = (len(rows) == len(provided) and (coverage or {}).get("reconciled") is not False
                and all(row["evidence_ids"] and row["unit_cost"] >= 0
                        and all((source_index or {}).get(ref, {}).get("kind") in _ACCOUNTING_KINDS
                                for ref in row["evidence_ids"]) for row in rows)
                and expected is not None and denominator == expected and denominator > 0)
    rows.sort(key=lambda row: (-row["unit_cost"], row["name"]))
    selected, remaining = rows[:4] if verified else rows, rows[4:] if verified else []
    display = deepcopy(selected)
    with localcontext() as arithmetic:
        arithmetic.prec = 512
        remaining_cost = sum((row["unit_cost"] for row in remaining), _ZERO)
    if remaining:
        display.append({"name": "剩余已提供明细合计（非新增费用类别）",
                        "unit_cost": remaining_cost,
                        "evidence_ids": _ids([ref for row in remaining for ref in row["evidence_ids"]]),
                        "members": [row["name"] for row in remaining]})
    cu = settings["unit_cost_unit"]
    label = "前四项及剩余已提供明细" if remaining else "已提供全部明细" if len(rows) == len(provided) else "已提供可计算明细"
    text = f"{actor}{label}（按单位费用排序）："
    parts = []
    for row in display:
        part = f"{row['name']}{_n(row['unit_cost'])}{cu}"
        if verified:
            with localcontext() as arithmetic:
                arithmetic.prec = 60
                share = row['unit_cost']/denominator*100
            row["share_pct_exact"] = str(share)
            part += f"（占全量明细{format_percent(share)}%）"
        parts.append(part + _marks(row["evidence_ids"]))
    text += "；".join(parts) + "。"
    if verified:
        text += f"占比以全部源明细单位费用合计{_n(denominator)}{cu}为分母，与该主体要素汇总勾稽一致；列示项及剩余明细按未舍入值闭合，显示占比不强行配平。"
    else:
        text += "全量明细覆盖或非负余额尚未核实，仅列已提供项目，不编造其他费用类别，不以汇总减已列项目构造余额。"
        if len(rows) != len(provided):
            text += "未提供单位费用的项目保持空值，不补零、不纳入占比。"
    refs = _ids([ref for row in rows for ref in row["evidence_ids"]])
    text += _marks(refs)
    with localcontext() as arithmetic:
        arithmetic.prec = 512
        closure = denominator-sum((row['unit_cost'] for row in display), _ZERO)
    return text, {"available": True, "side": side, "actor": actor, "verified": verified,
                  "denominator_exact": str(denominator), "summary_unit_cost_exact": str(expected) if expected is not None else None,
                  "denominator_basis": "all_provided_source_details" if verified else "unverified_coverage",
                  "display_rows": [{**row, "unit_cost": str(row["unit_cost"])} for row in display],
                  "remaining_count": len(remaining), "closure_exact": str(closure) if verified else None,
                  "evidence_ids": refs}


def build_benchmark_narrative(facts, sources=None, explanations=None, *, amount_unit="元", reporting_unit="盒",
                              industry_comparison=None, observed_yields=None, config=None, prose_mode=None):
    """Render supplied benchmark facts with real actors, paired detail and references."""
    settings = _settings(config, amount_unit, reporting_unit)
    # Accept a payload wrapper as well as the canonical benchmark fact mapping.
    payload = facts
    facts = payload.get("facts", payload)
    if "reference_profile" not in settings and isinstance(payload.get("domain_config"), dict):
        settings["reference_profile"] = payload["domain_config"]
    period = str(payload.get("period", payload.get("month", facts.get("period", facts.get("month", "所选期间")))))
    previous_periods = list(payload.get("previous_months", []))
    previous_period = payload.get("previous_period", facts.get("previous_period", facts.get("previous_month")))
    if previous_period:
        previous_periods.append(previous_period)
    context = {"period": period, "months": list(payload.get("months", [])), "previous_months": previous_periods,
               "product": payload.get("product", facts.get("product")),
               "specification": payload.get("specification", facts.get("specification"))}
    admitted = _admitted_sources(facts, sources)
    index = _source_index(admitted, context)
    if not facts.get("available", True):
        return _finish(facts.get("reason", "缺少可比事实，未生成对标原因。"), [], admitted, context, settings, None, None)
    home = settings.get("home_label") or (facts.get("home") or {}).get("factory") or facts.get("home_factory") or "本方"
    peer = settings.get("peer_label") or (facts.get("peer") or {}).get("factory") or facts.get("peer_factory") or "对标方"
    a, cu = settings["amount_unit"], settings["unit_cost_unit"]
    total = _get(facts, "normalized_amount_exact", "normalized_amount", "normalized_gap_exact", "normalized_gap")
    rows = facts.get("elements", [])
    if isinstance(rows, dict):
        rows = [{"element": key, **row} for key, row in rows.items()]
    if total is None and rows and all(_get(row, "normalized_amount_exact", "normalized_amount") is not None for row in rows):
        total = sum((_get(row, "normalized_amount_exact", "normalized_amount") for row in rows), _ZERO)
    overview = (f"{period} {context['product'] or ''}按同产品、规格和所选期间比较{home}与{peer}，"
                f"以{home}产量标准化金额差{_n(total, signed=True)}{a}；标准化差额不是实际节约成果。")
    if total == 0:
        overview += "净差额为零，贡献度无定义；保留各要素的正反向差异。"
    sections = []
    for row in rows:
        key = row["element"]
        refs = _fact_ids(row)
        u0, u1 = _get(row, "home_unit_cost_exact", "home_unit_cost"), _get(row, "peer_unit_cost_exact", "peer_unit_cost")
        gap = _get(row, "unit_gap_exact", "unit_gap")
        amount = _get(row, "normalized_amount_exact", "normalized_amount")
        contribution = None if total == 0 else _get(row, "contribution_pct_exact", "contribution_pct")
        pct = _pct(u1, u0)
        fact = (f"{key}{home}{_n(u0)}{cu}，{peer}{_n(u1)}{cu}，单位差{_n(gap, signed=True)}{cu}，"
                + (f"差异率{format_percent(pct, signed=True)}%，" if pct is not None else "对标基数为零或未提供，差异率无定义，")
                + f"以{home}产量标准化金额差{_n(amount, signed=True)}{a}；"
                + (f"金额贡献度{format_percent(contribution)}%。" if contribution is not None else "净差额为零或贡献度未提供。") + _marks(refs))
        reason = ""
        no_difference = gap == 0
        if gap is not None and gap != 0:
            reason = (f"已观察的核算差异是{home}本项单位费用{'高于' if gap > 0 else '低于'}{peer}{_n(abs(gap))}{cu}，"
                      + (f"金额贡献度{format_percent(contribution)}%" if contribution is not None else "净额贡献度无定义或未提供")
                      + "；不能用两方总金额的规模差代替该单位差异。")
            if amount is not None and total is not None and amount*total < 0:
                reason += "本项反向抵消其他要素形成的净差额，保留负贡献。"
            reason += _marks(refs)
        branch = (facts.get("paired_drilldown") or {}).get(key) or {}
        details = branch.get("rows") or []
        details_text, action_details, physical_observations = [], [], []
        cost_summaries = []
        if _kind(key, settings) != "人工":
            for side, expected, actor in (("home", u0, home), ("peer", u1, peer)):
                summary_text, summary = _cost_detail_summary(details, side, expected, actor, settings,
                    source_index=index, coverage=(branch.get("coverage") or {}).get(side))
                cost_summaries.append(summary)
                if summary_text:
                    details_text.append(summary_text)
                    refs.extend(summary["evidence_ids"])
        for detail in details:
            sides = [detail.get(side) for side in ("home", "peer")]
            for position, side in enumerate(sides):
                if not side:
                    continue
                actor = side.get("factory") or (home if position == 0 else peer)
                ident = side.get("evidence_id")
                metrics = side.get("metrics") or {}
                if _kind(key, settings) == "人工" and _d(metrics.get("hours")) is not None:
                    text = (f"{actor}总工时{_n(metrics['hours'])}小时，归集人工费用{_n(side.get('amount'))}{a}，"
                            f"小时归集费用{_n(metrics.get('cost_per_hour'))}{a}/小时，"
                            f"每{settings['reporting_unit']}工时{_n(metrics.get('hours_per_box'), places=4)}小时/{settings['reporting_unit']}；归集比率不是个人工资或已证明的岗位效率。")
                else:
                    text = ""  # Full cost composition is rendered once above, not duplicated per side.
                if text:
                    details_text.append(text + _marks([ident]))
                refs.extend(_ids([ident]))
                if _kind(key, settings) == "材料":
                    physical_text, physical_refs, observations = _observed_material_notes(
                        {"name": detail.get("name", key), **side}, index, key, settings, actor=actor)
                    details_text.extend(physical_text)
                    refs.extend(physical_refs)
                    physical_observations.extend(observations)
            if all(sides) and _get(detail, "normalized_amount_exact", "normalized_amount") is not None:
                details_text.append(f"{detail.get('name', key)}同口径配对标准化金额差{_n(_get(detail, 'normalized_amount_exact', 'normalized_amount'), signed=True)}{a}。" + _marks([side.get("evidence_id") for side in sides]))
            action_details.append({"name": detail.get("name"), "amount_delta": _get(detail, "normalized_amount_exact", "normalized_amount"),
                                   "unit_effect": _get(detail, "normalized_amount_exact", "normalized_amount") if branch.get("complete") else
                                   _get(detail.get("home") or {}, "unit_cost_exact", "unit_cost")})
        paired = [detail for detail in details if detail.get("home") and detail.get("peer")
                  and _get(detail, "normalized_amount_exact", "normalized_amount") is not None]
        largest = max((abs(_get(detail, "normalized_amount_exact", "normalized_amount")) for detail in paired), default=_ZERO)
        priority = [detail for detail in paired if largest and abs(_get(detail, "normalized_amount_exact", "normalized_amount")) == largest]
        if priority and not no_difference:
            reason += "已完整配对的明细中，标准化影响绝对值" + ("并列最大" if len(priority) > 1 else "最大") + "的是" + "、".join(str(detail.get("name", key)) for detail in priority) + "。"
            for detail in priority:
                left, right = detail["home"], detail["peer"]
                object_refs = _ids([left.get("evidence_id"), right.get("evidence_id")])
                reason += (f"{detail.get('name', key)}的直接核算差异是{left.get('factory') or home}单位费用{_n(_get(left, 'unit_cost_exact', 'unit_cost'))}{cu}，"
                           f"而{right.get('factory') or peer}为{_n(_get(right, 'unit_cost_exact', 'unit_cost'))}{cu}，"
                           f"按本方产量形成{_n(_get(detail, 'normalized_amount_exact', 'normalized_amount'), signed=True)}{a}差额；尚不能仅凭该金额认定物理或经营原因。" + _marks(object_refs))
                refs.extend(object_refs)
        if not no_difference and not branch.get("complete"):
            details_text.append("已有单方明细仅用于定位核查对象；未配对记录不计算项目差额，也不认定为主体间根因。")
        core = _model_core(explanations, key, index, refs) if not no_difference else {}
        quote = select_mechanism(admitted, key, preferred_ids=core.get("evidence_ids", []), details=action_details, config=settings, context=context) if not no_difference else None
        if quote:
            refs.append(quote["id"])
        market, market_provenance = _market_notes(admitted, [], context=context, element=key, settings=settings) if _kind(key, settings) == "材料" and not no_difference else ([], [])
        details_text.extend(market)
        refs.extend(item["id"] for item in market_provenance)
        observed_availability = {"labor_available": any(_d(((detail.get(side) or {}).get("metrics") or {}).get("hours")) is not None
                                                       for detail in details for side in ("home", "peer")),
                                 "physical_available": bool(physical_observations)}
        action = _immediate_action(key, action_details, settings, benchmark=True, no_difference=no_difference,
                                   observed=observed_availability)
        gaps = _gaps(key, settings, benchmark=True, coverage=branch, details=action_details,
                     observed=observed_availability) if not no_difference else []
        section = _section(key, settings.get("element_labels", {}).get(key, key), fact, reason, details_text,
                           _mechanism_text(quote, benchmark=True), action, gaps, refs, index, refs,
                           model=core, no_difference=no_difference, prose_mode=prose_mode or payload.get('prose_mode'))
        section["market_references"] = market_provenance
        section["mechanism_evidence"] = quote
        section["applicable_document_ids"] = [source["id"] for source in index.values()
            if not no_difference and select_mechanism([source], key, details=action_details, config=settings, context=context)]
        section["physical_observations"] = physical_observations
        section["cost_detail_summary"] = cost_summaries
        section["prose_fact_role"] = "structure"
        if section.get("prose"):
            primary = re.sub(r"\[[A-Za-z][A-Za-z0-9_-]{0,63}\]", "", fact).strip()
            section["fact_in_prose"] = primary in section["prose"]
            if not section["fact_in_prose"]:
                section["text"] = fact + "\n" + section["text"]
        sections.append(section)
    return _finish(overview, sections, admitted, context, settings,
                   industry_comparison or payload.get("industry_comparison"), observed_yields or payload.get("observed_yields"))
