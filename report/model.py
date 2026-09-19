"""Frozen, JSON-safe report data. No model client, dispatch or repository writes.

This is the shared authority for DOCX and PDF. A formal report requires every
selected month of cost and operating detail; optional comparators remain visibly
unavailable. Approval and external data-flow authorization belong to the APP.
"""
from __future__ import annotations

import base64
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, localcontext
import hashlib
import json
from pathlib import Path
import re
import uuid

import pandas as pd

from .datafill import (COST_COLS, BUDGET_COLS, MFG_NAMES, THEMES, ZERO, CENT,
                       load_data, build_mapping, resolve_period, product_specification,
                       scoped_rows, _period_values, _labor_values, _dec, _mom_pct)
from .registry import TEMPLATE_PATH, parse_template

SCHEMA_VERSION = "report-payload/1.0"
CALCULATION_VERSION = "period-cost/1.0"
TEMPLATE_VERSION = "competition-six-sections/1.0"
RENDERER_VERSION = "shared-docx-reportlab/1.0"
PROMPT_VERSION = "report-hypotheses/1.1"
LABELS = {"材料": "直接材料", "人工": "直接人工", "制费": "制造费用"}
ACTIONS = {
    "材料": ("采购部、生产部", "核对主要原材料采购合同、结算单、领退料、投料和批次产出记录，区分采购价格与实际耗用。"),
    "人工": ("生产部、财务部", "核对工时、考勤和加班记录，复核工资归集、跨期计提及产出，区分用工投入与费用归集。"),
    "制费": ("财务部、设备部", "核对制造费用凭证、分配基数、能源计量和维修记录，区分费用支出、分摊和产量变化。"),
}
LIMITATIONS = [
    "产量减少导致支出下降不等于成本管控节约；单位成本变化也需核实业务原因。",
    "原材料明细为单位消耗成本（元/盒），不是采购单价；市场行情及参考折算用量不能证明实际采购价格或实物耗用。",
    "贡献度以要素金额变动除以总金额变动计算；总变动为零时无定义，方向相反的要素可出现负贡献或超过百分之百。",
    "自动结构与引用检查不代替专业审核；任务仅为待批准草稿，未发送、未送达。",
]


class ReportError(ValueError):
    pass


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def fnum(value):
    return None if value is None else float(value)


def display(value, suffix="", signed=False):
    if value is None or value == "—":
        return "—"
    return format(Decimal(str(value)), "+,.2f" if signed else ",.2f") + suffix


def _records(data):
    return {name: json.loads(frame.to_json(orient="records", force_ascii=False, double_precision=15))
            for name, frame in data.items() if isinstance(frame, pd.DataFrame)}


def source_ref(table, row):
    """Reuse the explicit provenance contract, never guess physical line numbers."""
    from attribution_facts import _source
    extra = {"material": "原材料名称", "mfg": "费用类别", "market": "药材名称"}.get(table)
    result = _source(table, row, extra)
    if result.get("record_number") is None:
        value = row.get("source_record_number", row.get("record_number"))
        try:
            number = Decimal(str(value))
            if number.is_finite() and number > 0 and number == int(number):
                result["record_number"] = int(number)
        except (ValueError, ArithmeticError, TypeError):
            pass
    return result


def _frame(data, table, product, spec, months, complete=False):
    return scoped_rows(data.get(table), product, spec, months, complete=complete,
                       extra_key={"material": "原材料名称", "mfg": "费用类别"}.get(table))


def _scope_data(data, product, spec):
    out = {}
    for name, frame in data.items():
        if not isinstance(frame, pd.DataFrame):
            continue
        subset = frame.copy(deep=True)
        for column, value in (("产品名称", product), ("产品规格", spec)):
            if column in subset:
                subset = subset[subset[column].eq(value)].copy()
        if "工厂" in subset:
            factory = "中药二厂" if name.startswith("erchang") or name == "cost26_2" else "中药一厂"
            subset = subset[subset["工厂"].eq(factory)].copy()
        subset.attrs = dict(frame.attrs)
        out[name] = subset
    return out


def _period_pack(values):
    if values is None:
        return None
    return {"volume": fnum(values["产量"]), "total_cost": fnum(values["总成本"]),
            "unit_cost": fnum(values["单位成本"]),
            "elements": {key: {"unit_cost": fnum(values[key]), "amount": fnum(values["amounts"][key])}
                         for key in LABELS}}


def _check_details(data, product, spec, months, formal):
    """Missing/partial detail cannot silently become a full-period statement."""
    status, warnings = {}, []
    costs = _frame(data, "cost26", product, spec, months, complete=True)
    index = {str(row["月份"]): row for _, row in costs.iterrows()}
    for table, amount_col, unit_col, element in (
        ("material", "原材料总成本(元)", "单位消耗成本(元/盒)", "材料"),
        ("labor", "直接人工总额(元)", None, "人工"),
        ("mfg", "费用总额(元)", "单位费用(元/盒)", "制费"),
    ):
        rows = _frame(data, table, product, spec, months)
        missing = sorted(set(months) - (set(rows["月份"].astype(str)) if rows is not None else set()))
        status[table] = {"complete": not missing, "missing_months": missing}
        if missing:
            note = f"{table}本期明细缺月：" + "、".join(missing)
            if formal:
                raise ReportError("正式报告完整期校验失败：" + note + "；可选择非正式核查稿")
            warnings.append(note + "；不将部分期间明细冒充完整期间")
            continue
        for month, group in rows.groupby("月份"):
            base = index[str(month)]
            volume = _dec(base["产量(盒)"])
            target = _dec(base[COST_COLS[element]]) * volume
            actual = sum((_dec(value, amount_col) for value in group[amount_col]), ZERO)
            if abs(target - actual) > CENT:
                raise ReportError(f"{month}/{table}明细与成本汇总金额不闭合")
            for _, row in group.iterrows():
                if _dec(row.get("产量(盒)"), "明细产量") != volume:
                    raise ReportError(f"{month}/{table}明细产量与汇总不一致")
                if unit_col and abs(_dec(row.get(unit_col), unit_col) * volume - _dec(row[amount_col])) > CENT:
                    raise ReportError(f"{month}/{table}单位明细与金额不闭合")
    return status, warnings


def _details(data, table, product, spec, months, previous_months, current, previous):
    name_col, amount_col, unit_col = {
        "material": ("原材料名称", "原材料总成本(元)", "单位消耗成本(元/盒)"),
        "mfg": ("费用类别", "费用总额(元)", "单位费用(元/盒)"),
    }[table]
    rows = _frame(data, table, product, spec, [*previous_months, *months])
    if rows is None:
        return []
    result = []
    for name, group in rows.groupby(name_col):
        item = {"name": str(name), "sources": [], "evidence_ids": []}
        for prefix, span, values in (("current", months, current), ("previous", previous_months, previous)):
            selected = group[group["月份"].isin(span)]
            full = values is not None and set(selected["月份"].astype(str)) == set(span)
            amount = sum((_dec(value, amount_col) for value in selected[amount_col]), ZERO) if full else None
            unit = (amount / values["产量"] if values["产量"] else
                    _dec(selected.iloc[0][unit_col]) if len(span) == 1 else None) if full else None
            item[prefix + "_amount"] = fnum(amount)
            item[prefix + "_unit"] = fnum(unit)
            item[prefix + "_complete"] = full
            item["sources"].extend(source_ref(table, row) for _, row in selected.iterrows())
        c, p = item["current_amount"], item["previous_amount"]
        item["amount_delta"] = fnum(Decimal(str(c)) - Decimal(str(p))) if c is not None and p is not None else None
        item["unit_change_pct"] = fnum(_mom_pct(item["current_unit"], item["previous_unit"]))
        result.append(item)
    return sorted(result, key=lambda row: (row["amount_delta"] is None, -abs(row["amount_delta"] or 0), row["name"]))


def _trend(data, product, spec, end):
    period = pd.Period(end, freq="M")
    window = [str(period - 5 + n) for n in range(6)]
    rows = scoped_rows(data["history"], product, spec, [str(period - 6), *window])
    index = {str(row["月份"]): row for _, row in rows.iterrows()} if rows is not None else {}
    result = []
    for month in window:
        row = index.get(month)
        previous = index.get(str(pd.Period(month, freq="M") - 1))
        if row is None:
            result.append({"month": month, "available": False, "reason": "缺少该月数据"})
            continue
        values = {key: _dec(row[column], column) for key, column in COST_COLS.items()}
        previous_unit = _dec(previous["单位成本(元/盒)"]) if previous is not None else None
        result.append({"month": month, "available": True, "volume": fnum(values["产量"]),
                       "unit_cost": fnum(values["单位成本"]), "total_cost": fnum(values["总成本"]),
                       "elements": {key: fnum(values[key]) for key in LABELS},
                       "mom_pct": fnum(_mom_pct(values["单位成本"], previous_unit)),
                       "source": source_ref("cost_history", row)})
    return result


def _market(data, end, material_names):
    frame = data.get("market", pd.DataFrame())
    year = frame.attrs.get("year", 2026)
    if frame.empty or str(year) != end[:4] or "药材名称" not in frame:
        return []
    target = f"{int(end[5:7])}月价格"
    if target not in frame or "1月价格" not in frame:
        return []
    result = []
    for _, row in frame[frame["药材名称"].isin(material_names)].iterrows():
        if len(frame[frame["药材名称"].eq(row["药材名称"])]) != 1:
            continue
        first, last = _dec(row["1月价格"]), _dec(row[target])
        source = source_ref("market", row)
        source["key"].update({"价格字段": target, "年份": str(year), "单位": str(row.get("单位", "未标单位"))})
        source["note"] = "市场参考行情，不是本厂采购实价；不采用晚于分析期的报价或趋势结论"
        result.append({"material": str(row["药材名称"]), "unit": str(row.get("单位", "未标单位")),
                       "first": fnum(first), "current": fnum(last), "change_pct": fnum(_mom_pct(last, first)),
                       "month": end, "source": source})
    return result


def _table(headers, rows, note=None):
    return {"headers": headers, "rows": [[str(value) for value in row] for row in rows], "note": note}


def _table_text(table):
    return "\n".join("\t".join(row) for row in table["rows"]) or table.get("note") or "无可用记录"


def _ref_text(refs):
    return " ".join(f"[{ident}]" for ident in refs)


def _period_narrative(facts, explanations):
    current, previous = facts["current"], facts["previous"]
    period = facts["period_label"]
    opening = (f"{period}产量{display(current['volume'])}盒，总成本{display(current['total_cost'])}元，"
               f"单位成本{display(current['unit_cost'])}元/盒。")
    if previous:
        opening += (f"与完整前期相比，总成本变动{display(facts['amount_change']['总变动额'], signed=True)}元；"
                    f"产量影响{display(facts['volume_effect'], signed=True)}元，"
                    f"单位成本影响{display(facts['unit_effect'], signed=True)}元。")
    else:
        opening += "缺少完整可比前期，不提供期间变化贡献度与产量/单位成本桥接。"
    if len(facts["months"]) > 1:
        opening += "季度产量和金额为全部月份之和，单位成本按产量加权，季度结论覆盖完整季度。"
    opening += LIMITATIONS[0]
    sections = []
    for key, label in LABELS.items():
        item = facts["elements"][key]
        text = f"{label}本期金额{display(item['amount'])}元，单位成本{display(item['unit_cost'])}元/盒。"
        if item["amount_delta"] is not None:
            text += (f"前期单位成本{display(item['previous_unit'])}元/盒，变动率{display(item['change_pct'], '%')}；"
                     f"金额变动{display(item['amount_delta'], signed=True)}元，"
                     + (f"金额贡献度{display(item['contribution_pct'], '%')}。" if item["contribution_pct"] is not None else "总净变动为零，贡献度无定义。")
                     + f"产量影响{display(item['volume_effect'], signed=True)}元，单位成本影响{display(item['unit_effect'], signed=True)}元。")
        complete = [row for row in item.get("details", []) if row.get("amount_delta") is not None]
        if complete:
            text += "可比明细中主要变动为" + "、".join(f"{row['name']}{display(row['amount_delta'], signed=True)}元" for row in complete[:3]) + "。"
        elif key != "人工":
            text += "缺少完整前后期明细，不将未提供项目当作零。"
        text += " " + _ref_text(item["evidence_ids"])
        row = explanations.get("elements", {}).get(key, {}) if explanations else {}
        hypothesis = row.get("hypothesis") or {
            "材料": "现有单位消耗成本不能确认采购价或实际单耗变化，需补齐合同、领退料与批次产出证据。",
            "人工": "人工费用变化可能涉及用工投入、工资归集或生产安排，具体原因待核查。",
            "制费": "制造费用变化可能涉及支出、分配基数或生产负荷，具体原因待凭证核查。",
        }[key]
        recommendation = row.get("recommendation") or ACTIONS[key][0] + "应" + ACTIONS[key][1]
        text += "\n待核查解释：" + hypothesis + (" " + _ref_text(row["evidence_ids"]) if row else "")
        text += "\n建议：" + recommendation
        sections.append({"element": key, "title": label, "text": text, "hypothesis": hypothesis,
                         "recommendation": recommendation,
                         "claim_type": "hypothesis", "evidence_ids": list(dict.fromkeys(item["evidence_ids"] + row.get("evidence_ids", []))),
                         "missing_evidence": row.get("missing_evidence", [ACTIONS[key][1]])})
    return opening, sections


def _benchmark(data, product, spec, months, include):
    if not include:
        return {"available": False, "reason": "本报告未选择对标分析", "periods": [], "sources": []}
    from enterprise.benchmark import build_benchmark
    periods = [build_benchmark(product, spec, month, data) for month in months]
    complete = all(row["available"] for row in periods)
    amount = sum((Decimal(row["normalized_amount_exact"]) for row in periods), ZERO) if complete else None
    sources = []
    for row in periods:
        for source in row["sources"]:
            sources.append({**source, "id": row["month"].replace("-", "") + "-" + source["id"]})
    return {"available": complete, "periods": periods, "sources": sources,
            "normalized_amount": fnum(amount),
            "reason": None if complete else "；".join(row["reason"] for row in periods if not row["available"]),
            "formula": "逐月（同品同规格一厂单位成本−二厂单位成本）×当月一厂产量，再对完整期间求和；保留每月的产品与生产结构。",
            "limitation": "标准化差额为会计比较情景，不是实际节约；缺少二厂原料、工时及费用分配明细，经营原因待核查。"}


def build_report_payload(params, tables=None, *, evidence=None, model_fn=None,
                         model_version="not_configured", versions=None):
    """Build once, export many. ``model_fn(payload, evidence)`` is opt-in injection.

    params: product, specification(optional only if unique), month, theme,
    formal(default True), include_benchmark(default True), focus(optional
    材料/人工/制费), use_llm(default True), compiled_date(optional ISO date).
    No call to a remote model or implicit RAG reader is made here.
    """
    if not isinstance(params, dict):
        raise ReportError("报告参数必须为对象")
    params = deepcopy(params)
    if model_fn is not None and model_version == "not_configured":
        model_version = "injected_version_unspecified"
    params.setdefault("theme", THEMES[0])
    params.setdefault("formal", True)
    params.setdefault("include_benchmark", True)
    for field in ("formal", "include_benchmark", "use_llm"):
        if field in params and not isinstance(params[field], bool):
            raise ReportError(field + "必须为布尔值")
    original = load_data() if tables is None else tables
    if not isinstance(original, dict):
        raise ReportError("tables必须为DataFrame映射")
    product, month, theme = params.get("product"), params.get("month"), params["theme"]
    months, previous_months, yoy_months, label = resolve_period(theme, month)
    spec = product_specification(original, product, params.get("specification"))
    params["specification"] = spec
    compiled = date.fromisoformat(params.get("compiled_date") or date.today().isoformat())
    data = _scope_data(original, product, spec)
    mapping = build_mapping(product, month, theme, data, specification=spec)
    data["history"] = pd.concat([data.get("cost26", pd.DataFrame()), data.get("cost25", pd.DataFrame())], ignore_index=True)
    current_rows = _frame(data, "cost26", product, spec, months, complete=True)
    previous_rows = _frame(data, "history", product, spec, previous_months, complete=True)
    yoy_rows = _frame(data, "history", product, spec, yoy_months, complete=True)
    budget_rows = _frame(data, "budget", product, spec, months, complete=True)
    completeness, warnings = _check_details(data, product, spec, months, params["formal"])
    warnings += mapping["_warnings"]
    for name, rows in (("上年同期", yoy_rows), ("预算", budget_rows)):
        if rows is None:
            warnings.append(name + "缺少完整可比期间，相关指标不计算")
    sources = []

    def add_evidence(text, refs, elements, kind="data_fact"):
        ident = f"R{len(sources) + 1:03d}"
        sources.append({"id": ident, "text": text, "kind": kind, "elements": elements,
                        "source": {"table": "report_period", "key": {"工厂": "中药一厂", "产品名称": product,
                                   "产品规格": spec, "月份": months}, "records": refs},
                        "scope": {"product": product, "specification": spec, "months": months}})
        return ident

    with localcontext() as context:
        context.prec = 40
        current, previous, yoy, budget = (_period_values(rows, columns) for rows, columns in (
            (current_rows, COST_COLS), (previous_rows, COST_COLS), (yoy_rows, COST_COLS), (budget_rows, BUDGET_COLS)))
        summary_sources = [source_ref(table, row) for table, rows in (("cost26", current_rows), ("cost_history", previous_rows))
                           if rows is not None for _, row in rows.iterrows()]
        summary_id = add_evidence(f"{label}金额与产量来源；采用完整期间及明确产品规格。", summary_sources, list(LABELS))
        for table, rows in (("yoy", yoy_rows), ("budget", budget_rows)):
            if rows is not None:
                add_evidence("同比/预算独立期间来源", [source_ref(table, row) for _, row in rows.iterrows()], list(LABELS))
        elements = {}
        for key in LABELS:
            c, p = current[key], previous[key] if previous else None
            amount = current["amounts"][key]
            delta = amount - previous["amounts"][key] if previous else None
            output = (current["产量"] - previous["产量"]) * p if previous and p is not None else None
            unit = (c - p) * current["产量"] if c is not None and p is not None else None
            change_pct = _mom_pct(c, p)
            details = _details(data, "material" if key == "材料" else "mfg", product, spec, months, previous_months, current, previous) if key != "人工" else []
            ident = add_evidence(f"{LABELS[key]}金额{display(amount)}元；变动{display(delta)}元；产量影响{display(output)}元；单位成本影响{display(unit)}元。",
                                 summary_sources, [key])
            detail_ids = []
            for item in details:
                eid = add_evidence(f"{item['name']}本期金额{display(item['current_amount'])}元，前期{display(item['previous_amount'])}元，差额{display(item['amount_delta'])}元；缺失不视为零。",
                                   item.pop("sources"), [key])
                item["evidence_ids"] = [eid]
                detail_ids.append(eid)
            elements[key] = {"amount": fnum(amount), "unit_cost": fnum(c), "previous_unit": fnum(p),
                             "amount_delta": fnum(delta), "change_pct": fnum(change_pct),
                             "contribution_pct": mapping["_amount_change"]["贡献度"].get(key),
                             "volume_effect": fnum(output), "unit_effect": fnum(unit),
                             "effect_reconciliation_difference": fnum(delta-output-unit) if output is not None and unit is not None else None,
                             "alert": bool(change_pct is not None and abs(change_pct) > 10),
                             "details": details, "evidence_ids": [ident, *detail_ids]}
        labor_rows = _frame(data, "labor", product, spec, [*previous_months, *months])
        if labor_rows is not None:
            labor_id = add_evidence("人工工时、人数、工作天数与工资总额来自同品同规格记录；期间比率采用先求和再相除。",
                                    [source_ref("labor", row) for _, row in labor_rows.iterrows()], ["人工"])
            elements["人工"]["evidence_ids"].append(labor_id)
        def sum_effect(name):
            values = [row[name] for row in elements.values()]
            return sum(values) if all(value is not None for value in values) else None
        facts = {"period_label": label, "months": months, "previous_months": previous_months, "yoy_months": yoy_months,
                 "current": _period_pack(current), "previous": _period_pack(previous), "yoy": _period_pack(yoy),
                 "budget": _period_pack(budget), "amount_change": mapping["_amount_change"], "elements": elements,
                 "volume_effect": sum_effect("volume_effect"), "unit_effect": sum_effect("unit_effect"),
                 "evidence_ids": [summary_id], "completeness": {"cost26": {"complete": True, "missing_months": []}, **completeness}}
    trend = _trend(data, product, spec, months[-1])
    for row in trend:
        if row["available"]:
            row["evidence_id"] = add_evidence(f"{row['month']}单位成本{row['unit_cost']}元/盒，产量{row['volume']}盒。", [row["source"]], list(LABELS))
    market = _market(data, months[-1], [row["name"] for row in elements["材料"]["details"]])
    for row in market:
        row["evidence_id"] = add_evidence(f"{row['material']}市场参考报价{row['current']}{row['unit']}；非本厂采购实价。", [row["source"]], ["材料"], "reference_scenario")
    facts.update(trend=trend, market=market)
    # Preserve the existing audited monthly facts/decomposition without fetching any
    # hidden market data. The quarter conclusion itself uses the period facts above.
    if len(months) == 1:
        from attribution_facts import build_facts
        from attribution_decomposition import build_decomposition
        monthly_data = {**data, "cost26": data["history"]}
        monthly = build_facts({"product": product, "amount_change": [mapping["_amount_change"]]}, product, month, monthly_data)
        facts["monthly_attribution"] = monthly
        facts["reference_decomposition"] = build_decomposition(monthly, data.get("market", pd.DataFrame()))
    else:
        facts["monthly_totals"] = [row for row in trend if row["month"] in months]
    from enterprise.benchmark_ai import EXPLANATION_PROMPT, BenchmarkValidationError, filter_evidence, parse_explanations
    allowed, evidence_notes = filter_evidence(evidence or [], {"product": product, "specification": spec, "months": months})
    warnings.extend(evidence_notes)
    known = {row["id"] for row in sources}
    for row in allowed:
        if row["id"] in known:
            warnings.append("外部证据ID冲突，未采用：" + row["id"])
            continue
        sources.append(deepcopy(row))
        known.add(row["id"])
    explanations, diagnostics = None, []
    status = "deterministic_requested" if not params.get("use_llm", True) else "model_not_configured"
    fallback = "请求仅使用确定性分析" if status == "deterministic_requested" else "未注入已授权模型调用函数"
    if model_fn is not None and params.get("use_llm", True):
        try:
            candidate = model_fn(deepcopy({"analysis_type": theme, "product": product, "specification": spec,
                                           "months": months, "facts": facts, "limitations": LIMITATIONS,
                                           "instruction": EXPLANATION_PROMPT + "\n本次为中药一厂单厂期间报告：只解释选中产品和规格的整个分析期间，不能用某一个月替代季度结论；本回调不生成二厂比较结论。",
                                           "prompt_version": PROMPT_VERSION}), deepcopy(sources))
            explanations = parse_explanations(candidate, sources)
            status, fallback = "model_validated", None
        except Exception as exc:
            status = "model_rejected" if isinstance(exc, ValueError) else "model_unavailable"
            # Exception text from a remote client can contain secrets or request bodies.
            fallback = "模型输出未通过结构或证据校验" if isinstance(exc, ValueError) else "模型调用失败：" + type(exc).__name__
            diagnostics.extend(str(exc).split("；") if isinstance(exc, BenchmarkValidationError) else [fallback])
    overview, sections = _period_narrative(facts, explanations)
    focus = params.get("focus")
    if focus is None:
        focus = max(elements, key=lambda key: abs(elements[key]["amount_delta"] or elements[key]["amount"]))
    if focus not in LABELS:
        raise ReportError("专题focus必须为材料、人工或制费")
    params["focus"] = focus
    special = (f"专题焦点：{LABELS[focus]}。本专题将该要素的金额、明细、证据缺口与核查任务作为重点，"
               "其余要素用于核对总成本闭合。" if theme == "专题分析" else
               "季度专项：逐月呈现季度内变化，并以完整季度加权结果评价期间结构。" if len(months) > 1 else
               "月度专项：结合连续月份趋势定位异常项目，并回查同产品同规格原始记录。")
    special += "\n" + next(row["text"] for row in sections if row["element"] == focus)
    benchmark = _benchmark(data, product, spec, months, params["include_benchmark"])
    for source in benchmark["sources"]:
        sources.append({"id": source["id"], "text": "同期同规格跨厂成本源记录", "kind": "data_fact",
                        "elements": list(LABELS), "source": {k: v for k, v in source.items() if k != "id"}})
    due = (compiled + timedelta(days=14)).isoformat()
    suggestions, tasks = [], []
    for index, row in enumerate(sorted(sections, key=lambda row: row["element"] != focus), 1):
        key = row["element"]
        recommendation = {"id": f"S{index:03d}", "title": f"{product}{LABELS[key]}核查", "action": row["recommendation"],
                          "department": ACTIONS[key][0], "owner_role": ACTIONS[key][0] + "负责人（待指定）",
                          "priority": "高" if elements[key]["alert"] or (theme == "专题分析" and key == focus) else "中",
                          "expected_effect": "核实原因并形成可复核的改进方案；收益待测算", "due_date": due,
                          "evidence_ids": row["evidence_ids"], "source": f"{theme}/{label}/{product}/{spec}/{LABELS[key]}"}
        suggestions.append(recommendation)
        tasks.append({**recommendation, "id": "DRAFT-" + digest({"params": params, "element": key, "compiled": compiled.isoformat()})[:12],
                      "title": recommendation["title"], "status": "草稿", "dispatch_status": "未发送", "delivery_status": "未送达",
                      "approval_status": "待审批", "schedule_status": "未调度", "deadline_basis": "编制日起建议两周内，批准时需确认"})
    all_tables = _build_tables(facts, mapping, sections, benchmark, suggestions, tasks)
    all_tables["suggestions"]["column_weights"] = [40, 16, 8, 23, 13]
    all_tables["tasks"]["column_weights"] = [17, 25, 7, 23, 13, 15]
    highlights = (f"本期数据覆盖{'、'.join(months)}，成本总额与三要素勾稽完成；"
                  "已区分产量与单位成本影响，未将减产支出下降记作节约。")
    alerts = [f"{LABELS[key]}期间单位成本变动{display(row['change_pct'], '%')}，严格超过±10%" for key, row in elements.items() if row["alert"]]
    concerns = "；".join([*alerts, *warnings]) or "未发现可计算要素严格超过±10%的期间告警；仍应执行凭证、计量和生产记录复核。"
    benchmark_structure = ("完整期间标准化金额差" + display(benchmark["normalized_amount"], signed=True) + "元。" + benchmark["formula"]
                           if benchmark["available"] else benchmark["reason"])
    benchmark_causes = benchmark.get("limitation", benchmark.get("reason")) or "待核查"
    mapping.update({"报告标题": f"{label}{product}{theme}报告", "报告类型": theme, "编制日期": compiled.isoformat(),
                    "报告编号": "CB-" + uuid.uuid4().hex[:12].upper(),
                    "材料成本归因分析文本": sections[0]["text"], "成本异常排查分析": special + "\n" + concerns,
                    "差异结构拆解分析": benchmark_structure, "差异归因分析文本": benchmark_causes,
                    "本月亮点": highlights, "需关注问题": concerns})
    for name, table_key in (("原材料成本明细表格", "material"), ("近6个月成本趋势表格", "trend"),
                            ("原材料价格跟踪表格", "market"), ("对标差异表格", "benchmark"),
                            ("改进建议表格", "suggestions"), ("整改任务表格", "tasks")):
        mapping[name] = _table_text(all_tables[table_key])
    for placeholder, terms in (("配方文档引用", ("配方",)), ("工艺文档引用", ("工艺", "设备")),
                               ("GMP文档引用", ("GMP", "法规")), ("行业基准引用", ("行业", "基准"))):
        refs = [row for row in sources if row.get("kind") == "document_basis" and any(term in canonical(row) for term in terms)]
        mapping[placeholder] = "；".join(f"[{row['id']}] {row['source'].get('file', '受控知识文档')}" for row in refs) or "未提供适用且已授权的知识证据，不据此作事实结论"
    registry = parse_template()
    missing = [name for name in registry if name not in mapping]
    if missing:
        raise ReportError("模板映射缺少字段：" + "、".join(missing))
    template_bytes = TEMPLATE_PATH.read_bytes()
    from .export import font_descriptor, make_charts, renderer_versions
    # Check the actual evidence glyphs before freezing a renderer/font choice.
    # A font that covers a short Chinese probe may still omit PDF radical glyphs.
    font = font_descriptor(canonical({'sources': sources, 'params': params, 'sections': sections,
                                      'mapping': mapping, 'tables': all_tables, 'benchmark': benchmark,
                                      'limitations': LIMITATIONS, 'warnings': warnings}))
    version_map = {"schema": SCHEMA_VERSION, "calculation": CALCULATION_VERSION, "model": model_version,
                   "prompt": PROMPT_VERSION, "template": {"version": TEMPLATE_VERSION, "file": TEMPLATE_PATH.name,
                   "sha256": hashlib.sha256(template_bytes).hexdigest()},
                   "renderer": renderer_versions(font), "data_sha256": digest(_records(original)),
                   "knowledge": [{"id": row["id"], "version_id": row.get("version_id"),
                                  "sha256": row.get("document_sha256") or row.get("source", {}).get("sha256")}
                                 for row in allowed], "upstream": deepcopy(versions or {})}
    payload = {"schema_version": SCHEMA_VERSION, "report_id": mapping["报告编号"],
               "analysis_run_id": uuid.uuid4().hex, "created_at": datetime.now(timezone.utc).isoformat(),
               "params": params, "period": {"label": label, "months": months, "previous_months": previous_months, "yoy_months": yoy_months},
               "formal": params["formal"], "review_status": "needs_review", "facts": facts, "mapping": mapping,
               "overview": overview, "sections": sections, "special_analysis": special, "highlights": highlights,
               "concerns": concerns, "tables": all_tables, "sources": sources, "benchmark": benchmark,
               "suggestions": suggestions, "task_drafts": tasks, "used_llm": explanations is not None,
               "generation_status": status, "fallback_reason": fallback,
               "assumptions": [{"element": row["element"], "claim_type": "hypothesis", "text": row["hypothesis"],
                                "evidence_ids": row["evidence_ids"], "missing_evidence": row["missing_evidence"]} for row in sections],
               "validation": {"numeric_facts": "program_rendered", "model_explanations": "passed" if explanations else "not_used",
                              "diagnostics": diagnostics, "template_placeholders": "fully_mapped", "period_completeness": facts["completeness"]},
               "warnings": warnings, "limitations": LIMITATIONS, "versions": version_map,
               "template_base64": base64.b64encode(template_bytes).decode("ascii")}
    payload["charts"] = make_charts(facts, font)
    payload["blocks"] = _blocks(payload)
    payload["blocks"], display_normalization = _normalize_display_blocks(payload["blocks"])
    payload["versions"]["display_text"] = display_normalization
    if display_normalization["replacements"]:
        payload["blocks"].append({"kind": "paragraph", "text":
            "呈现规范化：PDF提取的康熙部首及西字部首异体按cjk-display/1.0转为标准汉字；"
            "仅作用于双格式共用的展示块，原件、完整证据正文与来源哈希保持不变。"})
    payload = json.loads(canonical(payload))  # Detached data, no DataFrame/date/NaN leaks.
    payload["frozen_hash"] = digest(payload)
    return payload


def _build_tables(facts, mapping, sections, benchmark, suggestions, tasks):
    elements = facts["elements"]
    metrics = []
    for label, value_key in (("产量(盒)", "volume"), ("单位成本(元/盒)", "unit_cost"), ("总成本(元)", "total_cost")):
        values = [facts[period][value_key] if facts[period] else None for period in ("current", "previous", "yoy", "budget")]
        metrics.append([label, display(values[0]), display(values[1]), display(_mom_pct(values[0], values[1]), "%"),
                        display(values[2]), display(_mom_pct(values[0], values[2]), "%"), display(values[3]), display(_mom_pct(values[0], values[3]), "%")])
    for key, label in LABELS.items():
        values = [facts[period]["elements"][key]["unit_cost"] if facts[period] else None for period in ("current", "previous", "yoy", "budget")]
        metrics.append([label + "(元/盒)", display(values[0]), display(values[1]), display(_mom_pct(values[0], values[1]), "%"),
                        display(values[2]), display(_mom_pct(values[0], values[2]), "%"), display(values[3]), display(_mom_pct(values[0], values[3]), "%")])
    total = facts["current"]["total_cost"]
    structure = [[LABELS[key], display(row["unit_cost"]), display(row["amount"]),
                  display(row["amount"] / total * 100 if total else None, "%"), display(row["amount_delta"], signed=True), display(row["contribution_pct"], "%")]
                 for key, row in elements.items()]
    material = [[row["name"], display(row["current_unit"]), display(row["previous_unit"]), display(row["current_amount"]),
                 display(row["amount_delta"], signed=True), _ref_text(row["evidence_ids"])] for row in elements["材料"]["details"]]
    labor = [[name, str(mapping[c]), str(mapping[p]), str(mapping[rate])] for name, c, p, rate in (
        ("单位人工(元/盒)", "人工单位成本", "上月人工单位成本", "人工环比"),
        ("工时(h/万盒)", "本月工时", "上月工时", "工时环比"), ("平均小时工资(元/h)", "本月时薪", "上月时薪", "时薪环比"),
        ("效率(盒/人·日)", "本月效率", "上月效率", "效率环比"))]
    mfg = [[row["name"], display(row["current_unit"]), display(row["previous_unit"]), display(row["current_amount"]),
            display(row["amount_delta"], signed=True), _ref_text(row["evidence_ids"])] for row in elements["制费"]["details"]]
    trend = [[row["month"], display(row.get("volume")), *[display(row.get("elements", {}).get(key)) for key in LABELS],
              display(row.get("unit_cost")), display(row.get("mom_pct"), "%")] for row in facts["trend"]]
    market = [[row["material"], row["unit"], display(row["first"]), display(row["current"]), display(row["change_pct"], "%"),
               "市场参考 " + _ref_text([row["evidence_id"]])] for row in facts["market"]]
    comparison = []
    for period in benchmark["periods"]:
        if not period["available"]:
            comparison.append([period["month"], "不可比", "—", "—", "—", "—", period["reason"]])
            continue
        for row in period["elements"]:
            comparison.append([period["month"], LABELS[row["element"]], display(row["home_unit_cost"]), display(row["peer_unit_cost"]),
                               display(row["unit_gap"], signed=True), display(row["gap_pct"], "%"), display(row["normalized_amount"], signed=True)])
    return {
        "metrics": _table(["指标", "本期", "前期", "变动率", "上年同期", "同比", "预算", "预算偏差"], metrics),
        "structure": _table(["要素", "元/盒", "金额(元)", "占比", "金额变动(元)", "金额贡献度"], structure),
        "material": _table(["原材料", "本期元/盒", "前期元/盒", "本期金额(元)", "变动额(元)", "引用"], material,
                           "单位消耗成本不是采购单价；缺失/不完整期间显示—，不按零补齐。"),
        "labor": _table(["指标", "本期", "前期", "变动率"], labor, "工时、时薪、效率按完整期间的投入与产出汇总后计算；零分母显示—。"),
        "mfg": _table(["费用类别", "本期元/盒", "前期元/盒", "本期金额(元)", "变动额(元)", "引用"], mfg),
        "trend": _table(["月份", "产量(盒)", "材料元/盒", "人工元/盒", "制费元/盒", "单位成本", "连续环比"], trend,
                        "窗口截至分析期末；缺月保留空值，连续环比不跳过缺失月份。"),
        "market": _table(["药材", "单位", "年初参考价", "期末参考价", "变动率", "属性/引用"], market,
                         "仅展示已匹配材料与分析期末可用市场行情；不是实际采购价。"),
        "benchmark": _table(["月份", "要素", "一厂元/盒", "二厂元/盒", "单位差", "差异率", "标准化差额(元)"], comparison,
                            benchmark.get("limitation") or benchmark["reason"]),
        "suggestions": _table(["建议", "责任部门", "优先级", "预期效果", "建议完成"],
                              [[row["action"], row["department"], row["priority"], row["expected_effect"], row["due_date"]] for row in suggestions]),
        "tasks": _table(["草稿编号", "任务/责任岗位", "优先级", "来源", "建议截止", "状态"],
                        [[row["id"], row["title"] + "；" + row["owner_role"], row["priority"], row["source"], row["due_date"],
                          "待审批／未发送／未送达"] for row in tasks], "任务为建议草稿；部门岗位尚需指派到责任人，未创建外部任务。"),
    }


def _source_catalog(sources):
    """Deduplicate source files/locations for a readable appendix; JSON keeps full refs."""
    from pathlib import PureWindowsPath
    documents, index_rows = {}, []
    def leaves(source):
        if source.get("records"):
            return [leaf for record in source["records"] if isinstance(record, dict) for leaf in leaves(record)]
        return [source]
    for source in sources:
        doc_ids = []
        for ref in leaves(source["source"]):
            path = str(ref.get("file") or ref.get("table") or "受控知识来源")
            signature = (path, ref.get("sha256"), ref.get("sheet"))
            if signature not in documents:
                documents[signature] = {"id": f"D{len(documents) + 1:03d}", "file": PureWindowsPath(path).name,
                                        "path": path, "sha256": ref.get("sha256"), "sheet": ref.get("sheet"), "scopes": [], "locations": []}
            document = documents[signature]
            doc_ids.append(document["id"])
            key = ref.get("key", {})
            identity = "；".join(f"{name}={key[name]}" for name in ("工厂", "产品名称", "产品规格") if name in key)
            if identity and identity not in document["scopes"]:
                document["scopes"].append(identity)
            scope = "；".join(f"{name}={value}" for name, value in key.items() if name not in ("工厂", "产品名称", "产品规格"))
            loc = str(ref.get("table") or "文档") + "：" + scope
            if ref.get("record_number") is not None:
                loc += f"；record_number={ref['record_number']}"
            if ref.get("line") is not None:
                loc += f"；line={ref['line']}"
            if loc not in document["locations"]:
                document["locations"].append(loc)
        excerpt = source["text"] if len(source["text"]) <= 480 else source["text"][:480] + "…（节选；完整证据见冻结快照）"
        index_rows.append([source["id"], excerpt, "、".join(dict.fromkeys(doc_ids))])
    return list(documents.values()), index_rows


def _normalize_display_blocks(blocks):
    """Normalize only known CJK glyph equivalents, never numbers or source text.

    PDF extractors sometimes emit Unicode Kangxi radicals as whole Han glyphs.
    Keep the immutable evidence verbatim. A detached, versioned display copy is
    frozen before both DOCX and PDF rendering, with every glyph mapping recorded.
    """
    import unicodedata
    replacements = {}
    def convert(value):
        if isinstance(value, str):
            chars = []
            for char in value:
                rendered = (unicodedata.normalize('NFKC', char) if 0x2F00 <= ord(char) <= 0x2FD5
                            else '西' if char == '\u2ec4' else char)
                if char != rendered:
                    replacements[char] = rendered
                chars.append(rendered)
            return ''.join(chars)
        if isinstance(value, list):
            return [convert(item) for item in value]
        if isinstance(value, dict):
            return {key: convert(item) for key, item in value.items()}
        return value
    display_blocks = convert(blocks)
    return display_blocks, {'version': 'cjk-display/1.0', 'scope': 'shared_render_blocks_only',
                            'replacements': [{'codepoint': f'U+{ord(char):04X}', 'to': rendered}
                                             for char, rendered in sorted(replacements.items())]}


def _blocks(payload):
    """Semantic block list is shared by both renderers and itself frozen."""
    blocks = []
    def text(value, kind="paragraph", level=1):
        blocks.append({"kind": kind, "text": value, "level": level})
    def table(name):
        blocks.append({"kind": "table", "name": name, **payload["tables"][name]})
    def chart(name):
        if name in payload["charts"]:
            blocks.append({"kind": "chart", "name": name, "caption": payload["charts"][name]["caption"]})
    text(payload["mapping"]["报告标题"], "title")
    text(f"报告编号：{payload['report_id']}　版本：{TEMPLATE_VERSION}　编制日期：{payload['mapping']['编制日期']}")
    text("完整期间报告／待专业审核" if payload["formal"] else "非正式核查稿／资料缺口详见附录")
    text("生成状态：" + ("已使用通过结构与引用校验的模型假设" if payload["used_llm"] else "确定性分析；" + payload["fallback_reason"]))
    text("一、封面与基本信息", "heading")
    metadata = _table(["项目", "内容"], [["报告类型", payload["params"]["theme"]], ["期间", "、".join(payload["period"]["months"])],
                                           ["产品/规格", payload["params"]["product"] + "／" + payload["params"]["specification"]],
                                           ["工厂", "中药一厂"], ["审核状态", "待专业审核；未经批准不代表正式发布"],
                                           ["数据来源", "统一合并成本数据；来源记录与内容哈希见附录"]])
    blocks.append({"kind": "table", **metadata})
    text("二、总成本概览", "heading")
    text(payload["overview"])
    text("2.1 核心指标一览", "heading", 2); table("metrics")
    text("2.2 成本结构与金额桥接", "heading", 2); table("structure")
    text(payload["mapping"]["波动告警描述"]); chart("waterfall"); chart("structure")
    text("三、成本要素明细分析", "heading")
    for index, section in enumerate(payload["sections"], 1):
        text(f"3.{index} {section['title']}分析", "heading", 2)
        table({"材料": "material", "人工": "labor", "制费": "mfg"}[section["element"]])
        text(section["text"])
    text("四、重点产品专项分析", "heading")
    text("4.1 近六个月趋势与期间范围", "heading", 2); table("trend"); chart("trend")
    text("4.2 专项问题与异常排查", "heading", 2); text(payload["special_analysis"]); text(payload["concerns"])
    text("4.3 原材料市场参考行情", "heading", 2); table("market")
    text("五、对标分析（与中药二厂）", "heading")
    text("5.1 同期同规格差异", "heading", 2); table("benchmark")
    text("5.2 差异结构拆解", "heading", 2); text(payload["mapping"]["差异结构拆解分析"])
    text("5.3 差异原因核查", "heading", 2); text(payload["mapping"]["差异归因分析文本"])
    text("六、总结与建议", "heading")
    text("6.1 本期管理亮点", "heading", 2); text(payload["highlights"])
    text("6.2 需关注问题", "heading", 2); text(payload["concerns"])
    text("6.3 改进建议", "heading", 2); table("suggestions")
    text("6.4 整改任务草稿", "heading", 2); table("tasks")
    text("附录一：来源、知识引用与证据边界", "heading")
    for limit in payload["limitations"]:
        text(limit)
    for name in ("配方文档引用", "工艺文档引用", "GMP文档引用", "行业基准引用"):
        text(name + "：" + payload["mapping"][name])
    documents, index_rows = _source_catalog(payload["sources"])
    index_table = _table(["证据ID", "事实或依据摘要", "来源档案"], index_rows)
    index_table["column_weights"] = [13, 70, 17]
    blocks.append({"kind": "table", **index_table})
    for document in documents:
        text(f"{document['id']}  {document['file']}", "heading", 2)
        text("来源路径：" + document["path"])
        text("来源SHA256：" + (document["sha256"] or "未提供源文件哈希；本次输入摘要见版本附录"))
        if document["sheet"]:
            text("工作表：" + document["sheet"])
        for scope in document["scopes"]:
            text("适用记录：" + scope)
        text("来源定位（record_number为含表头记录序号；line仅为显式物理行）：")
        for location in document["locations"]:
            text(location)
    text("附录二：数据与生成版本", "heading")
    for name, value in payload["versions"].items():
        text(name + "：" + (canonical(value) if isinstance(value, (dict, list)) else str(value)))
    text("analysis_run_id：" + payload["analysis_run_id"])
    text("历史回看读取冻结结果，不调用模型；数值复算与模型重跑须建立新的分析运行。")
    return blocks


def verify_payload(payload):
    if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA_VERSION:
        raise ReportError("报告快照结构版本不受支持")
    body = {key: value for key, value in payload.items() if key != "frozen_hash"}
    if payload.get("frozen_hash") != digest(body):
        raise ReportError("冻结报告内容校验失败，禁止渲染被修改的快照")
    raw = base64.b64decode(payload["template_base64"], validate=True)
    if hashlib.sha256(raw).hexdigest() != payload["versions"]["template"]["sha256"]:
        raise ReportError("冻结模板哈希不一致")
    if payload["versions"]["renderer"]["version"] != RENDERER_VERSION:
        raise ReportError("渲染器版本不一致，须用记录的版本重放")
    return True
