"""Same-product, specification and period factory benchmarking.

All comparisons use unit cost or the home factory's volume. They are accounting
comparisons, not verified efficiency savings or proof of operational causes.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
import re

import pandas as pd

from dashboard.data_layer import load_merged_tables

FACTORIES = {"cost26": "中药一厂", "erchang26": "中药二厂"}
ELEMENTS = {
    "材料": "直接材料(元/盒)",
    "人工": "直接人工(元/盒)",
    "制费": "制造费用(元/盒)",
}
IDENTITY = ["工厂", "产品名称", "产品规格", "月份"]
CENT = Decimal("0.01")
ZERO = Decimal("0")


class BenchmarkError(ValueError):
    pass


def _number(value, field):
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise BenchmarkError(f"{field}不是有效数字") from None
    if not number.is_finite() or number < ZERO:
        raise BenchmarkError(f"{field}缺失、非有限或为负")
    return number


def _display(value, precision=CENT):
    return None if value is None else float(value.quantize(precision, rounding=ROUND_HALF_UP))


def _rate(change, base):
    return change / base * 100 if base else None


def _source(table, row):
    """Keep explicit physical lines separate from importer record ordinals.

    A DataFrame index is neither a source line nor a record number. In particular,
    CSV records may span physical lines and Excel records belong to a sheet.
    """
    def text(*names):
        for name in names:
            value = row.get(name)
            if value is not None and pd.notna(value) and str(value).strip():
                return str(value)
        return None

    def position(*names):
        for name in names:
            try:
                number = Decimal(str(row.get(name)))
                if number.is_finite() and number > 0 and number == int(number):
                    return int(number)
            except (InvalidOperation, TypeError, ValueError, OverflowError):
                pass
        return None

    file = text("_source_file", "source_file")
    return {
        "table": table, "file": file,
        "line": position("_source_line", "source_line") if file else None,
        "record_number": position("_source_row", "source_record_number") if file else None,
        "sheet": text("_source_sheet", "source_sheet") if file else None,
        "sha256": text("_source_hash", "source_hash"),
        "key": {key: str(row[key]) for key in [*IDENTITY, "原材料名称"] if key in row and pd.notna(row[key])},
        "fields": {key: str(row[key]) for key in [*ELEMENTS.values(), "单位成本(元/盒)", "产量(盒)", "总成本(元)", "单位消耗成本(元/盒)", "原材料总成本(元)"] if key in row},
        "note": "源文件与业务主键；记录序号不等于物理行号" if file else "合并数据表与业务主键；未提供原始文件路径",
    }


def benchmark_options(tables=None):
    """Union of scope keys, so missing peers remain visible instead of silently hidden."""
    tables = load_merged_tables() if tables is None else tables
    options = set()
    for table, factory in FACTORIES.items():
        frame = tables.get(table)
        if frame is None or frame.empty or not set(IDENTITY).issubset(frame.columns):
            continue
        for _, row in frame.iterrows():
            if row["工厂"] != factory or any(pd.isna(row[k]) or not str(row[k]).strip() for k in IDENTITY):
                continue
            product, specification, month = (str(row[k]) for k in ("产品名称", "产品规格", "月份"))
            if re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
                options.add((product, specification, month))
    return [{"product": product, "specification": specification, "month": month}
            for product, specification, month in sorted(options)]


def _record(tables, table, product, specification, month):
    factory = FACTORIES[table]
    frame = tables.get(table)
    if frame is None or frame.empty:
        raise BenchmarkError(f"缺少{factory}当期成本数据，禁止跨厂比较")
    if not set(IDENTITY).issubset(frame.columns):
        raise BenchmarkError(f"{factory}缺少工厂、产品、规格或月份字段")
    rows = frame[(frame["工厂"] == factory) & (frame["产品名称"] == product)
                 & (frame["产品规格"] == specification) & (frame["月份"] == month)]
    if rows.empty:
        raise BenchmarkError(f"缺少{factory} {month} {product} / {specification}同口径记录，禁止替代比较")
    if len(rows) != 1:
        raise BenchmarkError(f"{factory}同产品同规格同月存在重复记录，需先消除版本歧义")
    row = rows.iloc[0]
    values = {name: _number(row.get(column), f"{factory}/{column}") for name, column in ELEMENTS.items()}
    unit = _number(row.get("单位成本(元/盒)"), f"{factory}/单位成本")
    volume = _number(row.get("产量(盒)"), f"{factory}/产量")
    total = _number(row.get("总成本(元)"), f"{factory}/总成本")
    if sum(values.values(), ZERO) != unit:
        raise BenchmarkError(f"{factory}三要素之和与单位成本不闭合")
    if abs(total - unit * volume) > CENT:
        raise BenchmarkError(f"{factory}单位成本×产量与总成本不闭合")
    return {"factory": factory, "unit_cost": unit, "volume": volume, "total_cost": total,
            "elements": values, "source": _source(table, row)}


def _home_materials(tables, product, specification, month):
    """A one-sided detail list is a checking lead, never a fabricated peer comparison."""
    frame = tables.get("material")
    if frame is None or frame.empty:
        return [], "一厂原料明细缺失；二厂原料明细未提供，不能进行原料级跨厂归因。"
    required = {*IDENTITY, "原材料名称", "单位消耗成本(元/盒)", "原材料总成本(元)"}
    if not required.issubset(frame.columns):
        return [], "一厂明细缺少关键字段；不能进行原料级跨厂归因。"
    rows = frame[(frame["工厂"] == "中药一厂") & (frame["产品名称"] == product)
                 & (frame["产品规格"] == specification) & (frame["月份"] == month)]
    if rows.empty:
        return [], "一厂同品同规格同月明细缺失；二厂原料明细未提供。"
    if rows["原材料名称"].isna().any() or rows["原材料名称"].duplicated().any():
        return [], "一厂明细存在原料身份缺失或重复，暂停明细核查展示。"
    details = []
    try:
        for _, row in rows.iterrows():
            unit = _number(row["单位消耗成本(元/盒)"], "原料单位消耗成本")
            amount = _number(row["原材料总成本(元)"], "原料金额")
            details.append({"material": str(row["原材料名称"]), "unit_consumption_cost": float(unit),
                            "home_amount": _display(amount), "source": _source("material", row),
                            "peer_unit_consumption_cost": None})
    except BenchmarkError as exc:
        return [], str(exc) + "；未采用无效明细。"
    details.sort(key=lambda item: (-item["home_amount"], item["material"]))
    return details, "仅提供一厂原料明细供核查；二厂无原料明细，不能确认采购价、单耗或收率的跨厂差异。"


def _suggestions(product, specification, month, elements, material_details):
    names = "、".join(row["material"] for row in material_details[:2]) or "主要原材料"
    checks = {
        "材料": ("采购负责人", f"两厂补齐{names}同等级材料的领料计价、领退料量与批次产出，按相同计价和耗用口径区分价格及实物单耗差异。"),
        "人工": ("生产负责人", "两厂对齐工时范围与工资归集，比较工时/盒及人工费用/工时，并核对外包与加班是否计入同一要素。"),
        "制费": ("财务负责人", "两厂对齐折旧、动力、间接人工及维修归集，核对固定/变动费用划分、分配基数和产能利用情况。"),
    }
    rows = []
    for element in sorted(elements, key=lambda item: abs(item["normalized_amount"]), reverse=True):
        if element["unit_gap"] == 0:
            continue
        role, check = checks[element["element"]]
        rows.append({
            "title": f"{product}跨厂{element['element']}差异核查", "owner_role": role,
            "source": f"模块三 / {product} / {specification} / {month} / {element['element']}",
            "priority": "高" if element["unit_gap"] > 0 else "中",
            "due_date": None, "action": check, "status": "草稿",
            "evidence_ids": ["B001", "B002"], "dispatch_status": "未发送",
        })
    return rows


def build_benchmark(product, specification, month, tables=None):
    """Return a JSON-safe report, blocking any non-comparable or invalid scope."""
    result = {"available": False, "product": product, "specification": specification, "month": month,
              "home_factory": "中药一厂", "peer_factory": "中药二厂", "reason": None,
              "analysis_method": "确定性会计对标", "used_llm": False, "elements": [], "sources": [], "suggestions": []}
    if not isinstance(month, str) or not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
        return {**result, "reason": "月份必须为YYYY-MM"}
    if not isinstance(product, str) or not product.strip() or not isinstance(specification, str) or not specification.strip():
        return {**result, "reason": "必须明确选择产品与规格，不允许跨规格合并"}
    tables = load_merged_tables() if tables is None else tables
    try:
        with localcontext() as ctx:
            ctx.prec = 40
            home = _record(tables, "cost26", product, specification, month)
            peer = _record(tables, "erchang26", product, specification, month)
            gap = home["unit_cost"] - peer["unit_cost"]
            normalized = gap * home["volume"]
            elements = []
            for element in ELEMENTS:
                difference = home["elements"][element] - peer["elements"][element]
                impact = difference * home["volume"]
                elements.append({
                    "element": element, "home_unit_cost": float(home["elements"][element]),
                    "peer_unit_cost": float(peer["elements"][element]), "unit_gap": float(difference),
                    "gap_pct": _display(_rate(difference, peer["elements"][element])),
                    "normalized_amount": _display(impact), "normalized_amount_exact": str(impact),
                    "contribution_pct": _display(_rate(impact, normalized)),
                    "home_share_pct": _display(_rate(home["elements"][element], home["unit_cost"])),
                    "peer_share_pct": _display(_rate(peer["elements"][element], peer["unit_cost"])),
                })
            check = normalized - sum((Decimal(row["normalized_amount_exact"]) for row in elements), ZERO)
            if check != 0:
                raise BenchmarkError("三要素标准化金额差未闭合，禁止输出对标结论")
            materials, detail_note = _home_materials(tables, product, specification, month)
            direction = "高于" if gap > 0 else "低于" if gap < 0 else "等于"
            largest = max(elements, key=lambda row: abs(row["unit_gap"]))
            overview = (f"{month}同产品同规格下，中药一厂单位成本{direction}中药二厂"
                        + (f"{abs(gap):.2f}元/盒" if gap else "，单位成本无差异")
                        + f"；以一厂产量{home['volume']:,.0f}盒标准化，金额差为{normalized:+,.2f}元。")
            if home["volume"] == 0:
                overview += "本厂产量为零，标准化金额差为零，不代表实际节约或支出。"
            if any(row["unit_gap"] for row in elements):
                overview += f"{largest['element']}是单位差异绝对值最大的要素；该结果不是已核实的效率差或降本成果。"
            else:
                overview += "三要素单位成本均无差异，不据此认定工艺与管理效率完全相同。"
            result.update({
                "available": True, "home": {key: (float(value) if isinstance(value, Decimal) else value) for key, value in home.items() if key not in ("elements", "source")},
                "peer": {key: (float(value) if isinstance(value, Decimal) else value) for key, value in peer.items() if key not in ("elements", "source")},
                "unit_gap": float(gap), "gap_pct": _display(_rate(gap, peer["unit_cost"])),
                "normalized_amount": _display(normalized), "normalized_amount_exact": str(normalized),
                "reconciliation_difference": float(check),
                "display_rounding_difference": float(Decimal(str(_display(normalized))) - sum((Decimal(str(row["normalized_amount"])) for row in elements), ZERO)),
                "elements": elements, "overview": overview,
                "sources": [{"id": "B001", **home["source"]}, {"id": "B002", **peer["source"]}],
                "home_materials": materials, "detail_note": detail_note,
                "cause_status": "仅定位成本结构差异；缺少二厂明细与生产条件证据，实际原因待核查",
                "suggestions": _suggestions(product, specification, month, elements, materials),
                "formula": "单位差=一厂单位成本-二厂单位成本；差异率=单位差/二厂单位成本；标准化金额差=单位差×一厂产量。",
                "limitations": ["不同工厂总成本受各自产量影响，不能直接作为效率差。", "同产品同规格同期是必要条件，不保证质量等级、产能利用与核算分配口径完全一致。", "标准化金额差是同产量会计情景，不是实际节约或已批准目标。"],
            })
    except (BenchmarkError, KeyError, InvalidOperation, OverflowError) as exc:
        result["reason"] = str(exc)
    return result


def build_multi_product_summary(month, tables=None):
    """One comparable row per product/specification, never aggregate unlike units."""
    tables = load_merged_tables() if tables is None else tables
    rows = []
    for scope in benchmark_options(tables):
        if scope["month"] != month:
            continue
        report = build_benchmark(scope["product"], scope["specification"], month, tables)
        rows.append({"产品": scope["product"], "规格": scope["specification"], "月份": month,
                     "可比较": report["available"], "一厂单位成本": report.get("home", {}).get("unit_cost"),
                     "二厂单位成本": report.get("peer", {}).get("unit_cost"), "单位差(元/盒)": report.get("unit_gap"),
                     "差异率(%)": report.get("gap_pct"), "一厂产量标准化差额(元)": report.get("normalized_amount"),
                     "数据状态": "同品同规格同期" if report["available"] else report["reason"]})
    return rows
