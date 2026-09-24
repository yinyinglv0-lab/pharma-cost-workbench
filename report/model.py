"""Frozen, JSON-safe report data. No model client, dispatch or repository writes.

This is the shared authority for DOCX and PDF. A formal report requires every
selected month of cost and operating detail; optional comparators remain visibly
unavailable. Approval and external data-flow authorization belong to the APP.
"""
from __future__ import annotations

import base64
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, localcontext, ROUND_HALF_UP
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

SCHEMA_VERSION = "report-payload/2.1"
SUPPORTED_SCHEMAS = {"report-payload/1.0", "report-payload/2.0", SCHEMA_VERSION}
CALCULATION_VERSION = "period-cost/2.0"
TEMPLATE_VERSION = "competition-full-outline/2.0"
RENDERER_VERSION = "shared-docx-reportlab/3.0"
SUPPORTED_RENDERERS = {'shared-docx-reportlab/1.0', 'shared-docx-reportlab/2.0', RENDERER_VERSION}
from .claims import PROMPT_VERSION
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
    from enterprise.numeric import format_number, format_percent
    return (format_percent(value, signed=signed) if suffix == '%' else format_number(value, signed=signed)) + suffix


def display_pct(value):
    """Keep small nonzero ratios within 1% display-relative error."""
    if value is None:
        return '—'
    from enterprise.numeric import format_percent
    return format_percent(value) + '%'


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
        item['volume_effect'] = item['unit_effect'] = None
        if c is not None and p is not None and previous and previous['产量']:
            prior_unit = Decimal(str(p)) / previous['产量']
            volume_effect = (current['产量'] - previous['产量']) * prior_unit
            # Exact amount residual avoids rounding recurring quarterly unit rates.
            item['volume_effect'] = fnum(volume_effect)
            item['unit_effect'] = fnum(Decimal(str(c)) - Decimal(str(p)) - volume_effect)
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
        prior_field = f"{int(end[5:7]) - 1}月价格"
        prior = _dec(row[prior_field]) if prior_field in frame else None
        source = source_ref("market", row)
        source["key"].update({"价格字段": target, "年份": str(year), "单位": str(row.get("单位", "未标单位"))})
        source["note"] = "市场参考行情，不是本厂采购实价；不采用晚于分析期的报价或趋势结论"
        result.append({"material": str(row["药材名称"]), "unit": str(row.get("单位", "未标单位")),
                       "first": fnum(first), "current": fnum(last), "change_pct": fnum(_mom_pct(last, first)),
                       "previous": fnum(prior), "mom_pct": fnum(_mom_pct(last, prior)),
                       "previous_month": str(pd.Period(end, freq='M') - 1) if prior is not None else None,
                       "month": end, "source": source})
    return result


def _table(headers, rows, note=None):
    strings = [[str(value) for value in row] for row in rows]
    numeric = [index for index in range(1, len(headers))
               if any(re.fullmatch(r'[+−-]?\d[\d,.]*%?', row[index]) for row in strings)
               and all(row[index] == '—' or re.fullmatch(r'[+−-]?\d[\d,.]*%?', row[index]) for row in strings)]
    return {"headers": headers, "rows": strings, "note": note, 'numeric_columns': numeric}


def _table_text(table):
    return "\n".join("\t".join(row) for row in table["rows"]) or table.get("note") or "无可用记录"


def _ref_text(refs):
    return " ".join(f"[{ident}]" for ident in refs)


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
    structure = []
    if complete:
        for key, label in LABELS.items():
            value = sum((Decimal(next(part for part in row['elements'] if part['element'] == key)['normalized_amount_exact'])
                         for row in periods), ZERO)
            structure.append({'element': key, 'label': label, 'amount': fnum(value), 'amount_exact': str(value),
                              'contribution_pct': fnum(value / amount * 100) if amount else None,
                              'direction': '无差额' if not value else '一厂高于二厂' if value > 0 else '一厂低于二厂',
                              'effect': '无差额' if not value else '相互抵消' if not amount else '抵消净差额' if value * amount < 0 else '同向形成净差额'})
    return {"available": complete, "periods": periods, "sources": sources,
            "normalized_amount": fnum(amount), "structure": structure,
            "reason": None if complete else "；".join(row["reason"] for row in periods if not row["available"]),
            "formula": "逐月（同品同规格一厂单位成本−二厂单位成本）×当月一厂产量，再对完整期间求和；保留每月的产品与生产结构。",
            "limitation": "标准化差额按一厂产量比较两厂成本水平，不是实际节约；实际原因需两厂同口径明细核对。"}


def _topic_analysis(facts, focus, sections, *, product, specification):
    """A substantive focus section from observed facts, not invented topic data."""
    from .narrative import unit_amount
    item = facts['elements'][focus]
    section = next(row for row in sections if row['element'] == focus)
    refs = _ref_text(item['evidence_ids'])
    parts = [f"专题焦点：{LABELS[focus]}。专题范围：{product}／{specification}，月份{'、'.join(facts['months'])}。",
             '专题背景：根据所选焦点单独梳理核算变化、重点对象与核对次序；选择焦点不等于已确认异常或经营根因。',
             f"本期{LABELS[focus]}金额{display(item['amount'])}元，单位成本{display(item['unit_cost'])}元/盒；"
             + (f"较完整前期金额变动{display(item['amount_delta'], signed=True)}元，产量影响{display(item['volume_effect'], signed=True)}元，"
                f"单位成本影响{display(item['unit_effect'], signed=True)}元。" if item['amount_delta'] is not None else
                '缺少完整前期，不计算金额桥接或以缺失值补零。') + ' ' + refs]
    details = item.get('details', [])[:3]
    if details:
        parts.append('重点明细（按金额变动绝对值排序；缺少前期时仅作本期对象清单）：')
        for row in details:
            parts.append(f"{row['name']}：本期金额{display(row['current_amount'])}元，前期{display(row['previous_amount'])}元，"
                         f"金额变动{display(row['amount_delta'], signed=True)}元；单位影响折算{display(row['unit_effect'], signed=True)}元。 "
                         + _ref_text(row['evidence_ids']))
    elif focus == '人工':
        labor = facts.get('labor', {})
        current = labor.get('current')
        if current:
            parts.append(f"专题可用资料：汇总工时{display(current['hours'])}小时、归集工资{display(current['wages'])}元；"
                         f"每工时产出{display(current['output_per_hour'])}盒。汇总归集费率不是个人工资。 " + refs)
        bridge = labor.get('decomposition', {})
        if bridge.get('available'):
            parts.append(f"人工专项拆分：单位工时投入影响{unit_amount(bridge['hours_unit_effect'], signed=True)}元/盒，"
                         f"按本期产量折算{display(bridge['hours_amount_effect'], signed=True)}元；每工时归集费率影响"
                         f"{unit_amount(bridge['rate_unit_effect'], signed=True)}元/盒，折算{display(bridge['rate_amount_effect'], signed=True)}元。 " + refs)
    else:
        parts.append('重点明细未提供完整同口径记录，不能推定分项差异；先核对资料完整性。')
    if section.get('no_difference'):
        parts.append('本专题未发生可比要素差异，不新增原因核查任务；保留同口径记录，不编造异常。')
    else:
        parts.append('专项核查链（建议，尚未实施）：')
        if section.get('immediate_action'):
            parts.append('当前可执行：' + section['immediate_action'])
        from .actions import action_core_text
        parts.append('后续对象核实方向：' + action_core_text(section['actions']))
        parts.append('交付与验收：统一采用6.3共同完成口径；资料缺口不阻断本轮已提供数据的复算和勾稽，逐对象要求保留于冻结审计记录。')
    gaps = {'材料': '采购结算/领料计价、批次实物领退料与收率原始记录',
            '人工': '岗位班次工时、个人工资与返工分配记录',
            '制费': '能源分表计量、设备运行与费用分配原始记录'}
    parts.append('专题限制：现有汇总及明细不替代' + gaps[focus] + '；未提供的专项实物资料保持缺口，不伪造实耗、效率根因或节约收益。')
    return '\n'.join(parts)


def _report_references(data, product, spec, months, evidence, include_industry):
    """Freeze monthly reference views, never synthesize a quarterly percentile."""
    from enterprise.industry_benchmark import build_industry_comparison
    references = [row for row in evidence if row.get('kind') in {'industry_reference', 'market_reference'}]
    periods, market_rows, reading, observed_sources = [], [], {}, []
    for month in months:
        selected = [row for row in references if month in row.get('report_reference_months', [])]
        if include_industry:
            comparison = build_industry_comparison(product, spec, month, data, selected)
            for index, row in enumerate(comparison['rows'], 1):
                records = [row[side]['source'] for side in ('home', 'peer')
                           if row[side]['value'] is not None and row[side].get('source')]
                row['observation_evidence_ids'] = []
                if records:
                    ident = 'R7' + month.replace('-', '') + f'{index:02d}'
                    row['observation_evidence_ids'] = [ident]
                    observed_sources.append({'id': ident, 'kind': 'reference_comparison', 'elements': list(LABELS),
                        'text': month + '同品同规格汇总计算' + row['metric'] + '；只反映成本水平或结构，不证明效率或经营原因。',
                        'source': {'table': 'industry_observed_comparison', 'records': deepcopy(records)},
                        'scope': {'product': product, 'specification': spec, 'months': [month]}})
                reading[row['evidence_id']] = (
                    f"参考值摘列（非逐字引文）：{row['reference_year']}年行业文件，{row['category']}／{row['metric']}（{row['unit']}）："
                    f"P25 {display(row['p25']['value'])}，P50 {display(row['p50']['value'])}，P75 {display(row['p75']['value'])}；"
                    f"文件列示本厂值{display(row['source_reported_home']['value'])}，不是所选月份实测。统计窗口未提供，不据原文件评价认定原因。")
            periods.append(comparison)
        for source in selected:
            if source['kind'] != 'market_reference':
                continue
            # Recompute the bounded projection from the checked original row,
            # not mutable cached observations or its full-half-year trend prose.
            columns = source['table_row']['columns']
            number = int(month[-2:])
            with localcontext() as ctx:
                ctx.prec = 40
                current = _dec(columns[f'{number}月价格'])
                previous = _dec(columns[f'{number - 1}月价格']) if number > 1 else None
                change = _mom_pct(current, previous)
            market_rows.append({'month': month, 'material': columns['药材名称'],
                'grade': columns['规格等级'], 'unit': columns['单位'], 'source_market': columns['价格来源'],
                'current_price': fnum(current), 'current_price_exact': str(current),
                'previous_price': fnum(previous), 'previous_price_exact': str(previous) if previous is not None else None,
                'month_change_pct': fnum(change), 'month_change_pct_exact': str(change) if change is not None else None,
                'evidence_id': source['id'], 'future_prices_and_full_period_trend_excluded': True})
    for source in references:
        selected = [row for row in market_rows if row['evidence_id'] == source['id']]
        if selected:
            reading[source['id']] = '\n'.join(
                f"按月参考摘列（非逐字引文）：{row['month']} {row['material']}／{row['grade']}，参考价{display(row['current_price'])}{row['unit']}，"
                f"上月{display(row['previous_price'])}，环比{display_pct(row['month_change_pct'])}；来源市场：{row['source_market']}。"
                '只展示对应月份及上月，不采用未来报价或整期趋势；不是任一工厂采购实价。' for row in selected)
    industry_available = any(p['available'] for p in periods)
    industry = {'available': industry_available, 'periods': periods,
        'aggregation': 'month_specific_comparisons_only', 'months': list(months),
        'reason': None if industry_available else ('未选择行业参考' if not include_industry else
                   '当前未取得通过授权、范围及期间校验的行业参考；不自行补充基准。'),
        'boundary': '年度/产品类别P25、P50、P75仅作参照，统计窗口及样本未明确；不是同品同规格月度行业观测，不汇总或均值化为季度行业行。'
                    '文件列示本厂值与两厂所选月实测分开；占比高低不是效率优劣，差额不是可实现节约。'}
    market = {'available': bool(market_rows), 'rows': market_rows,
        'reason': None if market_rows else '未取得适用于所选月份及本产品材料的已授权市场参考。',
        'boundary': '市场报价不等于任一工厂结算价；等级、产地和计价单位应分别核对，不以报价反推实际耗用或因果。'}
    return industry, market, observed_sources, reading


def _report_forecast(data, product, spec, cutoff, include):
    """Use only supplied home summary history, without aliases or live reloads."""
    from enterprise.forecast import ForecastInputError, forecast_baseline
    target = str(pd.Period(cutoff, freq='M') + 1)
    result = {'available': False, 'method': 'naive', 'method_label': '上期持平（固定基线）',
              'cutoff_month': cutoff, 'target_month': target, 'evidence_ids': [],
              'boundary': '固定单步基线，不择优拟合；只使用截止月及以前成本，不使用未来实际值。'
                          '预测不是预算、统计趋势、置信区间或节约承诺；未预测产量或总成本。'}
    if not include:
        return {**result, 'reason': '未选择预测基线'}, []
    history = {}
    for name in ('cost25', 'cost26'):
        frame = data.get(name)
        if isinstance(frame, pd.DataFrame):
            history[name] = frame.loc[frame['月份'].le(cutoff)].copy() if '月份' in frame else frame.copy()
    # Original budget is comparison-only; the engine never uses it in fitting.
    if 'budget' in data:
        history['budget'] = data['budget']
    try:
        value = forecast_baseline(history, factory='中药一厂', product=product, specification=spec,
                                  cutoff_month=cutoff, horizon=1, method='naive', candidate_methods=('naive', 'ma3'))
    except ForecastInputError as exc:
        return {**result, 'reason': '预测输入不足或无效，未计算基线：' + str(exc)}, []
    refs = [source_ref(name, row) for name, frame in history.items() if name != 'budget' and not frame.empty
            for _, row in frame.loc[frame['月份'].isin(value['training_months'])].iterrows()]
    ident = 'R8' + cutoff.replace('-', '')
    sources = [{'id': ident, 'kind': 'forecast_baseline', 'elements': list(LABELS),
                'text': f"上期持平固定基线：截止{cutoff}，目标{target}，单位成本{display(value['forecast']['unit_cost'])}元/盒。"
                        '仅作预测基线，不是预算或已实现节约。',
                'source': {'table': 'forecast_training', 'records': refs},
                'scope': {'product': product, 'specification': spec, 'months': value['training_months']}}]
    budget = value['budget_comparison']
    budget_id = None
    if budget['status'] == 'matched':
        budget_id = 'R9' + cutoff.replace('-', '')
        frame = history['budget']
        rows = frame.loc[frame['月份'].eq(target)]
        sources.append({'id': budget_id, 'kind': 'forecast_budget_reference', 'elements': list(LABELS),
            'text': target + '同工厂同品同规格预算，仅比较当前输入快照，历史时点版本未核实。',
            'source': {'table': 'budget', 'records': [source_ref('budget', row) for _, row in rows.iterrows()]},
            'scope': {'product': product, 'specification': spec, 'months': [target]}})
    sufficient = value['backtests']['naive']['status'] == 'evaluated'
    return {**result, 'available': True, 'result': value, 'evidence_ids': [ident],
            'budget_evidence_ids': [budget_id] if budget_id else [],
            'backtest_reason': None if sufficient else
                '连续历史不足四个月，naive与ma3没有共同已观测回测目标；不填充缺月，误差指标不计算。',
            'interval_reason': '未建立校准区间，置信界限与覆盖率均不提供。'}, sources


def _reference_tables(industry, market, forecast):
    from .narrative import unit_amount

    def reference_number(value, unit):
        if value is None:
            return '—'
        return display_pct(value).removesuffix('%') if unit == '%' else unit_amount(value, minimum_places=4)

    comparison, observations = [], []
    for period in industry['periods']:
        for row in period['rows']:
            label = period['month'] + '\n' + row['category'] + '／' + row['metric']
            comparison.append([label, str(row['reference_year']), row['unit'],
                *[reference_number(row[key]['value'], row['unit']) for key in ('p25', 'p50', 'p75')],
                reference_number(row['source_reported_home']['value'], row['unit']), _ref_text([row['evidence_id']])])
            for side in ('home', 'peer'):
                value = row[side]
                observations.append([label, value['factory'], reference_number(value['value'], row['unit']), row['unit'],
                    value['position_label'], reference_number(value['gap_from_p50']['value'], row['unit']),
                    (value.get('reason') or row['interpretation']) + ' ' +
                    _ref_text([row['evidence_id'], *row['observation_evidence_ids']])])
    market_rows = [[row['month'], row['material'] + '／' + row['grade'], row['unit'],
                   display(row['previous_price']), display(row['current_price']), display_pct(row['month_change_pct']),
                   _ref_text([row['evidence_id']])] for row in market['rows']]
    forecast_rows, backtest_rows, budget_rows = [], [], []
    if forecast['available']:
        value = forecast['result']
        current, predicted = value['history'][-1], value['forecast']
        for key, label in [('unit_cost', '单位成本'), *LABELS.items()]:
            c = current['unit_cost'] if key == 'unit_cost' else current['elements'][key]
            p = predicted['unit_cost'] if key == 'unit_cost' else predicted['elements'][key]
            forecast_rows.append([label, forecast['cutoff_month'], display(c), forecast['target_month'], display(p),
                                  _ref_text(forecast['evidence_ids'])])
        for method, label in (('naive', '上期持平（固定基线）'), ('ma3', '近三月均值（仅回测参照）')):
            evaluation = value['backtests'][method]
            backtest_rows.append([label, str(evaluation['sample_count']), display(evaluation['mae']),
                                 display(evaluation['rmse']), display_pct(evaluation['mape'])])
        budget = value['budget_comparison']
        if budget['status'] == 'matched':
            budget_rows.append([forecast['target_month'], display(budget['budget_unit_cost']),
                display(budget['delta'], signed=True), display_pct(budget['delta_percent']),
                _ref_text(forecast['budget_evidence_ids'])])
    tables = {
        'industry_reference': _table(['观察月／类别／指标', '参考年份', '单位', 'P25', 'P50', 'P75', '文件列示本厂值', '引用'], comparison, industry['boundary']),
        'industry_observed': _table(['观察月／类别／指标', '工厂', '月度计算值', '单位', '分位区间位置', '较P50差额', '限制与引用'], observations,
            '百分比指标差额单位为百分点；其他差额沿用该行单位。缺少授权汇总或统计口径不可比时保留为空，不拿文件本厂值替代。'),
        'market_reference': _table(['月份', '药材／规格等级', '单位', '上月参考价', '当月参考价', '环比', '引用'], market_rows, market['boundary']),
        'forecast_baseline': _table(['成本指标（元/盒）', '截止月', '截止月实际', '目标月', '固定基线值', '引用'], forecast_rows, forecast['boundary']),
        'forecast_backtest': _table(['方法', '共同回测数', 'MAE(元/盒)', 'RMSE(元/盒)', 'MAPE'], backtest_rows,
            forecast.get('backtest_reason') or '按截止月以前已观测目标滚动回测；当前修订快照，不代表历史时点版本；不据同组误差选择方法。'),
        'forecast_budget': _table(['目标月份', '预算元/盒', '基线减预算(元/盒)', '预算偏差', '引用'], budget_rows,
            '仅对照输入快照中的同工厂同品同规格目标年月预算；历史时点预算版本未核实，预算差额不是节约。'),
    }
    weights = {'industry_reference': [30, 9, 9, 9, 9, 9, 17, 8], 'industry_observed': [28, 12, 12, 9, 15, 12, 30],
               'market_reference': [12, 28, 10, 13, 13, 12, 8], 'forecast_baseline': [23, 16, 16, 16, 17, 8],
               'forecast_backtest': [32, 15, 18, 18, 15], 'forecast_budget': [20, 20, 23, 22, 10]}
    for name, table in tables.items():
        table['column_weights'] = weights[name]
    return tables


def build_report_payload(params, tables=None, *, evidence=None, model_fn=None,
                         model_version="not_configured", versions=None, evidence_fn=None):
    """Build once, export many. ``model_fn(payload, evidence)`` is opt-in injection.

    params: product, specification(optional only if unique), month, theme,
    formal(default True), include_benchmark(default True), include_industry_reference
    and include_forecast(default True), focus(optional 材料/人工/制费), use_llm
    (default True), compiled_date(optional ISO date). Forecast uses a fixed naive
    baseline at the report period end; it is not a configurable model or budget.
    No implicit model or retrieval clients: authorized APP callbacks are optional.
    evidence_fn(facts), when supplied, runs after deterministic calculations so
    retrieval can target actual material/expense changes in this frozen scope.
    """
    if not isinstance(params, dict):
        raise ReportError("报告参数必须为对象")
    params = deepcopy(params)
    if model_fn is not None and model_version == "not_configured":
        model_version = "injected_version_unspecified"
    params.setdefault("theme", THEMES[0])
    params.setdefault("formal", True)
    params.setdefault("include_benchmark", True)
    params.setdefault('include_industry_reference', True)
    params.setdefault('include_forecast', True)
    for field in ("formal", "include_benchmark", "use_llm", 'include_industry_reference', 'include_forecast'):
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
        comparator_ids = []
        for table, rows in (("yoy", yoy_rows), ("budget", budget_rows)):
            if rows is not None:
                comparator_ids.append(add_evidence("同比/预算独立期间来源", [source_ref(table, row) for _, row in rows.iterrows()], list(LABELS)))
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
                 "evidence_ids": [summary_id], "comparator_evidence_ids": comparator_ids, "completeness": {"cost26": {"complete": True, "missing_months": []}, **completeness}}
    trend = _trend(data, product, spec, months[-1])
    for row in trend:
        if row["available"]:
            row["evidence_id"] = add_evidence(f"{row['month']}单位成本{row['unit_cost']}元/盒，产量{row['volume']}盒。", [row["source"]], list(LABELS))
    market = _market(data, months[-1], [row["name"] for row in elements["材料"]["details"]])
    for row in market:
        row["evidence_id"] = add_evidence(f"{row['material']}市场参考报价{row['current']}{row['unit']}；非本厂采购实价。", [row["source"]], ["材料"], "reference_scenario")
    facts.update(trend=trend, market=market)
    from .narrative import labor_snapshot, labor_decomposition
    facts['labor'] = {name: labor_snapshot(_frame(data, 'labor', product, spec, span, complete=True), span)
                      for name, span in (('current', months), ('previous', previous_months))}
    facts['labor']['decomposition'] = labor_decomposition(facts['labor'])
    facts['available_facts'] = ['完整当期成本与产量', '三要素核算金额']
    for key, item in elements.items():
        if item['details']:
            facts['available_facts'].append('一厂' + LABELS[key] + '明细及来源')
    if facts['labor']['current']:
        facts['available_facts'].append('一厂汇总总工时、产量与工资；可计算平均工时和产出每工时')
    facts['missing_facts'] = ['实际采购与领料结转计价凭证', '批次领退料实物量与收率记录', '个人岗位班次工时与工资分配明细']
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
    from .claims import filter_report_evidence, generate_claims
    if evidence_fn is not None:
        if evidence is not None:
            raise ReportError('不能同时传入静态证据和动态检索回调')
        evidence = evidence_fn(deepcopy(facts))
    retrieval_diagnostics = deepcopy(getattr(evidence, 'diagnostics', {}))
    allowed, evidence_notes = filter_report_evidence(evidence or [], {"product": product, "specification": spec, "months": months})
    warnings.extend(evidence_notes)
    known = {row["id"] for row in sources}
    for row in allowed:
        if row["id"] in known:
            warnings.append("外部证据ID冲突，未采用：" + row["id"])
            continue
        sources.append(deepcopy(row))
        known.add(row["id"])
    # Authorized reference projections precede the new bound-prose worker so its
    # complete period statements see the same typed references as the renderer.
    # They never become unqualified business-cause evidence.
    industry, authorized_market, reference_sources, reference_reading = _report_references(
        data, product, spec, months, [row for row in sources if row.get('kind') in {'industry_reference', 'market_reference'}],
        params['include_industry_reference'])
    claim_result = generate_claims(facts, [*sources, *reference_sources], product=product, specification=spec, months=months,
                                   model_fn=model_fn, use_llm=params.get('use_llm', True), model_version=model_version,
                                   market_reference=authorized_market, industry_comparison=industry)
    status, fallback = claim_result['generation_status'], claim_result['fallback_reason']
    diagnostics = list(evidence_notes) + list(claim_result['diagnostics'])
    explanations = claim_result if claim_result['used_llm'] else None
    forecast, forecast_sources = _report_forecast(data, product, spec, months[-1], params['include_forecast'])
    sources.extend(reference_sources + forecast_sources)
    from .narrative import period_narrative, knowledge_summary
    overview, sections, claim_ledger, shared_narrative = period_narrative(
        facts, claim_result, sources, product=product, months=months, specification=spec,
        market_reference=authorized_market, industry_comparison=industry, return_shared=True)
    focus = params.get("focus")
    if focus is None:
        focus = max(elements, key=lambda key: abs(elements[key]["amount_delta"] or elements[key]["amount"]))
    if focus not in LABELS:
        raise ReportError("专题focus必须为材料、人工或制费")
    params["focus"] = focus
    from .narrative import special_observations, operating_observations, market_observations, unit_amount
    special = (_topic_analysis(facts, focus, sections, product=product, specification=spec) if theme == '专题分析'
               else special_observations(facts, focus, theme))
    benchmark = _benchmark(data, product, spec, months, params["include_benchmark"])
    for source in benchmark["sources"]:
        sources.append({"id": source["id"], "text": "同期同规格跨厂成本源记录", "kind": "data_fact",
                        "elements": list(LABELS), "source": {k: v for k, v in source.items() if k != "id"}})
    from .peer_analysis import explain_peer
    peer_analysis = explain_peer(benchmark, facts, allowed, product=product, specification=spec, months=months,
                                  model_fn=model_fn, use_llm=params.get('use_llm', True), model_version=model_version)
    existing_ids = {source['id'] for source in sources}
    for source in peer_analysis['evidence']:
        if source['id'] not in existing_ids:
            sources.append(deepcopy(source))
            existing_ids.add(source['id'])
    benchmark['analysis'] = peer_analysis
    due = (compiled + timedelta(days=14)).isoformat()
    from .actions import complete_reading_actions
    from .claims import render_actions
    for section in sections:
        if not section.get('no_difference'):
            section['actions'], section['action_supplements'] = complete_reading_actions(
                facts, section['element'], section['actions'])
            section['recommendation'] = render_actions(section['actions'], product, months,
                immediate_action=section.get('immediate_action'),
                accepted_model_recommendation=section.get('accepted_model_recommendation'))
    suggestions, tasks = [], []
    for index, row in enumerate(sorted((s for s in sections if not s.get('no_difference')), key=lambda row: row["element"] != focus), 1):
        key = row["element"]
        departments = '、'.join(dict.fromkeys(part for action in row['actions'] for part in action['department'].split('、')))
        recommendation = {"id": f"S{index:03d}", "title": f"{product}{LABELS[key]}核查", "action": row["recommendation"],
                          "department": departments, "owner_role": departments + "负责人（待指定）",
                          "priority": "高" if elements[key]["alert"] or (theme == "专题分析" and key == focus) else "中",
                          "expected_effect": "核实原因并形成可复核的改进方案；收益待测算", "due_date": due,
                          "evidence_ids": row["evidence_ids"], "source": f"{theme}/{label}/{product}/{spec}/{LABELS[key]}",
                           'immediate_action': row.get('immediate_action', ''),
                           'evidence_gaps': deepcopy(row.get('evidence_gaps', [])),
                           "period": {"months": list(months), "label": label, "coverage": "full_period"},
                           "actions": deepcopy(row['actions']),
                           "deliverables": [action['deliverable'] for action in row['actions']],
                           "acceptance_criteria": [action['acceptance'] for action in row['actions']]}
        suggestions.append(recommendation)
        tasks.append({**recommendation, "id": "DRAFT-" + digest({"params": params, "element": key, "compiled": compiled.isoformat()})[:12],
                      "title": recommendation["title"], "status": "草稿", "dispatch_status": "未发送", "delivery_status": "未送达",
                      "approval_status": "待审批", "schedule_status": "未调度", "deadline_basis": "编制日起建议两周内，批准时需确认"})
    all_tables = _build_tables(facts, mapping, sections, benchmark, suggestions, tasks)
    all_tables.update(_reference_tables(industry, authorized_market, forecast))
    all_tables["suggestions"]["column_weights"] = [10, 51, 22, 17]
    all_tables["tasks"]["column_weights"] = [12, 38, 22, 28]
    highlights = operating_observations(facts)
    alerts = [f"{LABELS[key]}期间单位成本变动{display(row['change_pct'], '%')}，严格超过±10%" for key, row in elements.items() if row["alert"]]
    business_warnings = [note for note in warnings if any(word in note for word in ('缺月', '缺少完整', '明细缺', '期间不完整'))]
    budget_gap = (facts['current']['unit_cost'] - facts['budget']['unit_cost']
                  if facts.get('budget') and facts['current']['unit_cost'] is not None and facts['budget']['unit_cost'] is not None else None)
    if budget_gap is not None and budget_gap > 0:
        alerts.append(f"单位成本超预算{unit_amount(budget_gap)}元/盒，应按6.3逐项复核可控差额")
    labor_bridge = facts['labor']['decomposition']
    if labor_bridge.get('available') and labor_bridge['hours_change_pct'] > 0:
        alerts.append(f"单位工时投入增加{display(labor_bridge['hours_change_pct'], '%')}，需核对排产及岗位班次记录")
    concerns = '；'.join([*alerts, *business_warnings]) or '本期未发现超过设定阈值的单位要素波动；后续关注实际计价、生产投入与归集口径的持续可比性。'
    benchmark_structure = ("完整期间标准化金额差" + display(benchmark["normalized_amount"], signed=True) + "元。" + benchmark["formula"]
                           if benchmark["available"] else benchmark["reason"])
    from .actions import peer_action_text, peer_action_boundary
    for section in peer_analysis['sections']:
        if not section.get('no_difference'):
            # Cross-factory plans retain their validated paired scope. Home
            # month-on-month drivers must not become invented peer differences.
            section['text'] += '\n' + peer_action_text(section['actions'])
    if peer_analysis['available']:
        lead = '本节比较期间为' + '、'.join(months) + '，逐月差额和要素结构分别见5.1、5.2。'
        peer_analysis['text'] = lead + '\n\n' + '\n\n'.join(row['text'] for row in peer_analysis['sections'])
        if any(not row.get('no_difference') for row in peer_analysis['sections']):
            peer_analysis['action_boundary'] = peer_action_boundary()
            peer_analysis['text'] += '\n\n' + peer_analysis['action_boundary']
    benchmark_causes = peer_analysis['text']
    followup_criteria = shared_narrative.get('followup_criteria', '')
    if not followup_criteria and any(not row.get('no_difference') for row in peer_analysis['sections']):
        from enterprise.analysis_narrative import common_action_criteria
        followup_criteria = common_action_criteria()
    mapping.update({"报告标题": f"{label}{product}{theme}报告", "报告类型": theme, "编制日期": compiled.isoformat(),
                    "报告编号": "CB-" + uuid.uuid4().hex[:12].upper(),
                    "材料成本归因分析文本": sections[0]["text"], "成本异常排查分析": special,
                    "差异结构拆解分析": benchmark_structure, "差异归因分析文本": benchmark_causes,
                    "本月亮点": highlights, "需关注问题": concerns})
    for name, table_key in (("原材料成本明细表格", "material"), ("近6个月成本趋势表格", "trend"),
                            ("原材料价格跟踪表格", "market"), ("对标差异表格", "benchmark"),
                            ("改进建议表格", "suggestions"), ("整改任务表格", "tasks")):
        mapping[name] = _table_text(all_tables[table_key])
    adopted_knowledge = {ref for claim in [*claim_ledger, *peer_analysis.get('claim_ledger', [])] for ref in claim['knowledge_ids']}
    for placeholder, terms in (("配方文档引用", ("配方",)), ("工艺文档引用", ("工艺", "设备")),
                               ("GMP文档引用", ("GMP", "法规")), ("行业基准引用", ("行业", "基准"))):
        refs = [row for row in sources if row['id'] in adopted_knowledge and row.get("kind") == "document_basis" and any(term in canonical(row) for term in terms)]
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
                                      'industry_comparison': industry, 'market_reference': authorized_market,
                                      'forecast_baseline': forecast, 'reference_reading': reference_reading,
                                      'limitations': LIMITATIONS, 'warnings': warnings}))
    version_map = {"schema": SCHEMA_VERSION, "calculation": CALCULATION_VERSION, "model": model_version,
                   'unit_effect_display': 'nonzero-four-significant/1.0',
                   'reading_actions': 'observed-drivers-compact-core-common-criteria/2.1',
                    'attribution_narrative': shared_narrative['schema_version'],
                    'narrative_adapter': shared_narrative['input']['adapter_version'],
                    'report_references': 'monthly-typed-reference-sections/1.0',
                    'forecast_baseline': 'report-fixed-naive/1.0',
                   "prompt": claim_result['prompt_version'],
                    **({'reading_style': claim_result['reading_style']} if claim_result.get('reading_style') else {}),
                    "template": {"version": TEMPLATE_VERSION, "file": TEMPLATE_PATH.name,
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
               'shared_narrative': shared_narrative,
               'followup_criteria': followup_criteria,
               "concerns": concerns, "market_observations": market_observations(facts),
               "tables": all_tables, "sources": sources, "benchmark": benchmark,
                'industry_comparison': industry, 'market_reference': authorized_market,
                'forecast_baseline': forecast,
                'reference_usage': {'eligible_ids': [row['id'] for row in allowed if row.get('kind') in {'industry_reference', 'market_reference'}],
                                    'displayed_ids': sorted(reference_reading), 'causal_use': False},
               "suggestions": suggestions, "task_drafts": tasks, "used_llm": explanations is not None,
               "generation_status": status, "fallback_reason": fallback,
               "generation": {'single_factory': claim_result,
                              'cross_factory': {key: peer_analysis.get(key) for key in ('used_llm', 'generation_status', 'fallback_reason', 'generation')}},
               "knowledge_usage": knowledge_summary(sources, claim_ledger, retrieval_diagnostics),
               "assumptions": [{"element": row["element"], "claim_type": "hypothesis", "text": row["hypothesis"],
                                "evidence_ids": row["evidence_ids"], "missing_evidence": row["missing_evidence"]} for row in sections],
               "validation": {"numeric_facts": "program_rendered", "model_explanations": "passed" if explanations else "not_used",
                              "diagnostics": diagnostics, "template_placeholders": "fully_mapped", "period_completeness": facts["completeness"]},
               "warnings": warnings, "limitations": LIMITATIONS, "versions": version_map,
               "template_base64": base64.b64encode(template_bytes).decode("ascii")}
    payload["charts"] = make_charts(facts, font)
    payload["blocks"] = _blocks(payload)
    from .presentation import reading_appendix
    payload['blocks'], payload['reading_citations'] = reading_appendix(payload, payload['blocks'])
    # The immutable reference CSV row may contain future monthly columns or an
    # unverified source evaluation. Only bounded projections enter reading exports;
    # exact raw text, offsets, row/file hashes remain in sources and audit metadata.
    citation_table = next(block for block in payload['blocks'] if block.get('name') == 'citations')
    for entry, row in zip(payload['reading_citations']['entries'], citation_table['rows']):
        if entry['evidence_id'] in reference_reading:
            row[1] = reference_reading[entry['evidence_id']]
            entry['reference_projection'] = {'text': row[1], 'months': list(months),
                                             'raw_source_preserved': True, 'causal_use': False}
    payload["blocks"], display_normalization = _normalize_display_blocks(payload["blocks"])
    payload["versions"]["display_text"] = display_normalization
    from .structure import validate_blocks
    structure = validate_blocks(payload['blocks'], template_bytes=template_bytes, product=product,
                                period=payload['period'], charts=payload['charts'])
    payload['validation']['template_structure'] = structure
    if not structure['passed']:
        raise ReportError('报告完整模板内容校验失败：' + canonical(structure.get('errors') or structure.get('metrics')))
    payload = json.loads(canonical(payload))  # Detached data, no DataFrame/date/NaN leaks.
    payload["frozen_hash"] = digest(payload)
    return payload


def _build_tables(facts, mapping, sections, benchmark, suggestions, tasks):
    from .narrative import unit_amount
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
        ("工时(h/万盒)", "本月工时", "上月工时", "工时环比"), ("归集人工费用(元/h)", "本月时薪", "上月时薪", "时薪环比"),
        ("效率(盒/人·日)", "本月效率", "上月效率", "效率环比"))]
    mfg = [[row["name"], display(row["current_unit"]), display(row["previous_unit"]), display(row["current_amount"]),
            display(row["amount_delta"], signed=True), _ref_text(row["evidence_ids"])] for row in elements["制费"]["details"]]
    trend = [[row["month"], display(row.get("volume")), *[display(row.get("elements", {}).get(key)) for key in LABELS],
              display(row.get("unit_cost")), display(row.get("mom_pct"), "%")] for row in facts["trend"]]
    market = [[row["material"], row["unit"], display(row['previous']), display(row["current"]), display_pct(row['mom_pct']),
               _ref_text([row["evidence_id"]])] for row in facts["market"]]
    comparison = []
    for period in benchmark["periods"]:
        if not period["available"]:
            comparison.append([period["month"], "不可比", "—", "—", "—", "—", period["reason"]])
            continue
        for row in period["elements"]:
            comparison.append([period["month"], LABELS[row["element"]], display(row["home_unit_cost"]), display(row["peer_unit_cost"]),
                               unit_amount(row["unit_gap"], signed=True), display_pct(row.get('gap_pct_exact') if row.get('gap_pct_exact') is not None else
                                            Decimal(str(row['unit_gap'])) / Decimal(str(row['peer_unit_cost'])) * 100 if row['peer_unit_cost'] else None), display(row["normalized_amount"], signed=True)])
    bridge = facts.get('labor', {}).get('decomposition', {})
    labor_bridge = ([[label, unit_amount(bridge[unit], signed=True, minimum_places=6), display(bridge[value], signed=True)]
                     for label, unit, value in [('单位工时投入', 'hours_unit_effect', 'hours_amount_effect'),
                                                ('每工时归集费率', 'rate_unit_effect', 'rate_amount_effect'),
                                                ('合计', 'unit_delta', 'amount_delta')]] if bridge.get('available') else [])
    peer_structure = [[row['label'], display(row['amount'], signed=True), display_pct(row['contribution_pct']), row['direction'], row['effect']]
                      for row in benchmark.get('structure', [])]
    if peer_structure:
        peer_structure.append(['合计', display(benchmark['normalized_amount'], signed=True),
                               '100.00%' if benchmark['normalized_amount'] else '—', '按一厂产量比较', '净差额'])
    advice = []
    for suggestion in suggestions:
        # The same compact reading projection feeds UI, task drafts and exports.
        # Per-object voucher/deliverable/acceptance fields remain in the snapshot,
        # not repeated as a prose template on every object.
        text = suggestion['action']
        if suggestion.get('evidence_gaps'):
            text += '\n证据缺口：' + '；'.join(suggestion['evidence_gaps']) + '。'
        advice.append([suggestion['id'], text, suggestion['department'], suggestion['priority'] + '／' + suggestion['due_date']])
    return {
        "metrics": _table(["指标", "本期", "前期", "变动率", "上年同期", "同比", "预算", "预算偏差"], metrics,
                          '来源：' + _ref_text(facts['evidence_ids'] + facts.get('comparator_evidence_ids', []))),
        "structure": _table(["要素", "元/盒", "金额(元)", "占比", "金额变动(元)", "金额贡献度"], structure),
        "material": _table(["原材料", "本期元/盒", "前期元/盒", "本期金额(元)", "变动额(元)", "引用"], material,
                           "单位消耗成本不是采购单价；缺失/不完整期间显示—，不按零补齐。"),
        "labor": _table(["指标", "本期", "前期", "变动率"], labor, "归集费率为人工总额除以总工时，不等于个人时薪；工时、产出按完整期间汇总，零分母显示—。"),
        'labor_bridge': _table(['人工分解指标', '单位成本影响(元/盒)', '按本期产量折算(元)'], labor_bridge,
                              bridge.get('method') if bridge.get('available') else bridge.get('reason')),
        "mfg": _table(["费用类别", "本期元/盒", "前期元/盒", "本期金额(元)", "变动额(元)", "引用"], mfg),
        "trend": _table(["月份", "产量(盒)", "材料元/盒", "人工元/盒", "制费元/盒", "单位成本", "连续环比"], trend,
                        "窗口截至分析期末；缺月保留空值，连续环比不跳过缺失月份。来源：" + _ref_text([r['evidence_id'] for r in facts['trend'] if r.get('evidence_id')])),
        "market": _table(["药材", "单位", "上月参考价", "期末参考价", "环比", "引用"], market,
                         "期末月为" + facts['months'][-1] + "；报价按各自计价单位比较，不是实际采购价。"),
        "benchmark": _table(["月份", "要素", "一厂元/盒", "二厂元/盒", "单位差", "差异率", "标准化差额(元)"], comparison,
                            benchmark.get("limitation") or benchmark["reason"]),
        'benchmark_structure': _table(['要素', '标准化差额(元)', '净差额贡献', '成本水平', '形成或抵消'], peer_structure,
                                      '贡献为要素差额÷净差额；反向项目可为负贡献，净差额为零时贡献无定义。' if peer_structure else benchmark.get('reason')),
        "suggestions": _table(["建议编号", "当前核对、后续方向与证据缺口", "责任部门", "优先级／建议完成"], advice,
                              '本节统一适用上列共同完成口径；后续凭证需求与当前可执行核对分开，未经批准不实施或派发。'
                              if advice else '本期可比要素均无变化，无新增差异核查建议。'),
        "tasks": _table(["建议编号", "责任岗位", "建议截止", "任务状态"],
                        [[suggestions[i]['id'], row["owner_role"], row["due_date"], "待审批／未发送／未送达"] for i, row in enumerate(tasks)],
                        "任务对应6.3同号建议，责任人待指定；尚未创建外部任务。" if tasks else '本期无新增任务；未发送、未送达。'),
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
        value = payload['tables'][name]
        if value['rows']:
            blocks.append({"kind": "table", "name": name, **value})
        elif name in ('suggestions', 'tasks'):
            text(value.get('note') or '本期无新增差异核查任务。')
        else:
            reasons = {'material': '缺少完整期间原材料明细，不将缺失材料或月份补零。',
                       'mfg': '缺少完整期间制造费用明细，不将缺失费用项目补零。',
                       'market': '缺少与本产品材料、计价单位及分析期间匹配的市场参考行情，相关价格趋势不计算。',
                       'benchmark': '未选择跨厂对标或缺少同产品同规格完整期间二厂成本，跨厂差异不计算。'}
            text(reasons.get(name, '缺少该节所需的完整期间资料，相关指标不计算。') + ' ' + (value.get('note') or ''))
    def chart(name):
        if name in payload["charts"]:
            blocks.append({"kind": "chart", "name": name, "caption": payload["charts"][name]["caption"]})
    text(payload["mapping"]["报告标题"], "title")
    text(f"报告编号：{payload['report_id']}　编制日期：{payload['mapping']['编制日期']}")
    blocks.append({'kind': 'paragraph', 'role': 'approval_status', 'text':
                   '完整期间报告／待专业审核' if payload['formal'] else '非正式核查稿／资料缺口详见正文'})
    text("一、封面与基本信息", "heading")
    metadata = _table(["项目", "内容"], [["报告类型", payload["params"]["theme"]], ["期间", "、".join(payload["period"]["months"])],
                                           ["产品/规格", payload["params"]["product"] + "／" + payload["params"]["specification"]],
                                           ["工厂", "中药一厂"],
                                           ["数据来源", "成本汇总与同期间明细；引用见附录，完整追溯信息见独立审计附件"]])
    blocks.append({"kind": "table", **metadata})
    text("二、总成本概览", "heading")
    text(payload["overview"])
    industry = payload.get('industry_comparison')
    if industry and payload['params'].get('include_industry_reference', True):
        from .narrative import unit_amount
        anchors = []
        for period in industry['periods']:
            for row in period['rows']:
                if row['calculation'] != 'unit_conversion' or row['home']['value'] is None:
                    continue
                anchors.append(f"{period['month']}{row['home']['factory']}按所选规格折算单位成本"
                    f"{unit_amount(row['home']['value'], minimum_places=4)}{row['unit']}；"
                    f"{row['reference_year']}年{row['category']}参考P25/P50/P75为"
                    + '/'.join(unit_amount(row[key]['value'], minimum_places=4) for key in ('p25', 'p50', 'p75'))
                    + row['unit'] + '，数值位置' + row['home']['position_label'] + '。 '
                    + _ref_text([row['evidence_id'], *row['observation_evidence_ids']]))
        if anchors:
            text('行业单位成本参考（逐月列示）：' + '\n'.join(anchors)
                 + '\n年度类别基准的统计窗口、样本与产品组合未明确，不是同品同规格月度行业实测；'
                   '不平均为季度基准，不生成效率评分或节约额。明细及源文件本厂值分列见第五节。')
    text("2.1 核心指标一览", "heading", 2); table("metrics")
    text("2.2 成本结构与金额桥接", "heading", 2); table("structure")
    text(payload["mapping"]["波动告警描述"]); chart("waterfall"); chart("structure")
    text("三、成本要素明细分析", "heading")
    for index, section in enumerate(payload["sections"], 1):
        text(f"3.{index} {section['title']}分析", "heading", 2)
        if section['element'] == '材料':
            text('3.1.1 原材料成本明细', 'heading', 3)
        table({"材料": "material", "人工": "labor", "制费": "mfg"}[section["element"]])
        if section['element'] == '材料':
            text('3.1.2 材料成本变动归因', 'heading', 3)
        text(section["text"])
        if section['element'] == '人工' and payload['tables']['labor_bridge']['rows']:
            table('labor_bridge')
    text("四、重点产品专项分析", "heading")
    text("4.1 近六个月趋势与期间范围", "heading", 2); table("trend"); chart("trend")
    text("4.2 专项问题与异常排查", "heading", 2); text(payload["special_analysis"])
    forecast = payload.get('forecast_baseline')
    if forecast and payload['params'].get('include_forecast', True):
        text('预测基线（固定上期持平，不是预算）')
        text(f"截止月：{forecast['cutoff_month']}；目标月：{forecast['target_month']}。" + forecast['boundary'])
        if forecast['available']:
            table('forecast_baseline')
            text('连续训练月份：' + '、'.join(forecast['result']['training_months']) + '；点基线实际使用：' +
                 '、'.join(forecast['result']['method_training_months']) + '。 ' + _ref_text(forecast['evidence_ids']))
            table('forecast_backtest')
            text(forecast['interval_reason'])
            if payload['tables']['forecast_budget']['rows']:
                table('forecast_budget')
            else:
                text(forecast['result']['budget_comparison']['note'])
        else:
            text(forecast['reason'])
    text("4.3 原材料市场参考行情", "heading", 2)
    authorized_market = payload.get('market_reference')
    if authorized_market and authorized_market['available']:
        # Governed references replace the legacy view rather than contradicting
        # them with an empty ungoverned-table explanation. Frozen old blocks are
        # never reconstructed during replay.
        text('已授权市场参考（按月与规格等级列示）'); table('market_reference')
    else:
        table('market'); text(payload['market_observations'])
    text("五、对标分析（与中药二厂）", "heading")
    text("5.1 同期同规格差异", "heading", 2); table("benchmark")
    text("5.2 差异结构拆解", "heading", 2); text(payload["mapping"]["差异结构拆解分析"]); table('benchmark_structure')
    text("5.3 差异原因核查", "heading", 2); text(payload["mapping"]["差异归因分析文本"])
    industry = payload.get('industry_comparison')
    if industry and payload['params'].get('include_industry_reference', True):
        text('行业参考比较（年度类别基准，逐月列示）')
        text(industry['boundary'])
        if industry['available']:
            table('industry_reference'); table('industry_observed')
        for period in industry['periods']:
            if not period['available']:
                text(period['month'] + '：' + period['reason'])
        # Source-level alerts are displayed once even in quarterly reports. They
        # remain review candidates; they never create an extra task or savings.
        seen_alerts = set()
        for period in industry['periods']:
            for alert in period['alerts']:
                if alert['alert_id'] not in seen_alerts:
                    seen_alerts.add(alert['alert_id'])
                    text(alert['finding'] + alert['boundary'] + ' ' + _ref_text(alert['evidence_ids']))
    text("六、总结与建议", "heading")
    text("6.1 本期经营观察", "heading", 2); text(payload["highlights"])
    text("6.2 需关注问题", "heading", 2); text(payload["concerns"])
    text("6.3 改进建议", "heading", 2)
    if payload.get('followup_criteria'):
        text(payload['followup_criteria'])
    table("suggestions")
    text("6.4 整改任务草稿", "heading", 2); table("tasks")
    return blocks


def verify_payload(payload):
    if not isinstance(payload, dict) or payload.get("schema_version") not in SUPPORTED_SCHEMAS:
        raise ReportError("报告快照结构版本不受支持")
    body = {key: value for key, value in payload.items() if key != "frozen_hash"}
    if payload.get("frozen_hash") != digest(body):
        raise ReportError("冻结报告内容校验失败，禁止渲染被修改的快照")
    raw = base64.b64decode(payload["template_base64"], validate=True)
    if hashlib.sha256(raw).hexdigest() != payload["versions"]["template"]["sha256"]:
        raise ReportError("冻结模板哈希不一致")
    if payload["versions"]["renderer"]["version"] not in SUPPORTED_RENDERERS:
        raise ReportError("渲染器版本不一致，须用记录的版本重放")
    return True
