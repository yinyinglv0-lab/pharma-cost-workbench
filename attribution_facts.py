"""Auditable deterministic attribution facts; no model calls or database writes.

JSON numbers are floats for consumers; ``mom_pct_decimal`` preserves the Decimal
calculation before display rounding. Alerts compare unrounded Decimal values.
Amount changes and contributions always come from dashboard ``amount_change``.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation, localcontext
import re

import pandas as pd

ELEMENT_COLUMNS = {
    "材料": "直接材料(元/盒)",
    "人工": "直接人工(元/盒)",
    "制费": "制造费用(元/盒)",
}
THRESHOLD = Decimal("10")
IDENTITY = ("工厂", "产品规格")


class FactError(ValueError):
    pass


def _dec(value):
    if value is None:
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return number if number.is_finite() else None


def _num(value):
    number = _dec(value)
    return float(number) if number is not None else None


def _required(row, col):
    value = _dec(row.get(col))
    if value is None or value < 0:
        raise FactError(f"成本汇总字段缺失、非有限或为负：{col}")
    return value


def _previous_month(month):
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", str(month)):
        raise FactError("月份必须为 YYYY-MM")
    year, mon = map(int, month.split("-"))
    return f"{year if mon > 1 else year - 1:04d}-{mon - 1 if mon > 1 else 12:02d}"


def _source(table, row, extra_key=None):
    """Never infer file line numbers from a filtered DataFrame index."""
    key = {k: str(row[k]) for k in ("工厂", "产品名称", "产品规格", "月份")
           if k in row and pd.notna(row[k])}
    if extra_key and extra_key in row:
        key[extra_key] = str(row[extra_key])
    filename = row.get("_source_file", row.get("source_file"))
    filename = str(filename) if filename is not None and pd.notna(filename) else None
    line = _dec(row.get("_source_line", row.get("source_line")))
    line = int(line) if filename and line is not None and line > 0 and line == int(line) else None
    record = _dec(row.get('_source_row'))
    record = int(record) if record is not None and record > 0 and record == int(record) else None
    return {'table':table,'key':key,'file':filename,'line':line,'record_number':record,
            'sheet':str(row['_source_sheet']) if pd.notna(row.get('_source_sheet')) else None,
            'sha256':str(row['_source_hash']) if pd.notna(row.get('_source_hash')) else None,
            'note':('导入记录序号（含表头），不是推断的物理行号' if record else '显式来源元数据' if filename else '合并后数据表/主键定位；未提供原始文件行号')}


def _rows(frame, product, months, table):
    if frame is None or frame.empty:
        return []
    if not {"产品名称", "月份"}.issubset(frame.columns):
        raise FactError(f"{table}缺少产品名称或月份键")
    return frame[(frame["产品名称"] == product) & frame["月份"].isin(months)].to_dict("records")


def _scope(rows, table, expected=None):
    for col in IDENTITY:
        values = {str(r[col]) for r in rows if col in r and pd.notna(r[col]) and str(r[col]).strip()}
        if len(values) > 1:
            raise FactError(f"{table}存在{col}歧义，禁止混合归因")
        if expected and values and expected.get(col) and values != {expected[col]}:
            raise FactError(f"{table}与成本汇总的{col}不一致")
    return {col: next((str(r[col]) for r in rows if col in r and pd.notna(r[col])
                       and str(r[col]).strip()), None) for col in IDENTITY}


def _change(before, after):
    if before is None or after is None:
        return None
    return float(after - before)


def _detail(table, rows, before_month, after_month):
    settings = {
        "material": ("原材料名称", "单位消耗成本(元/盒)", "原材料总成本(元)",
                     "单位消耗成本（元/盒），不是采购单价；不能据此区分采购价格和实物耗用量"),
        "mfg": ("费用类别", "单位费用(元/盒)", "费用总额(元)", "单位制造费用（元/盒）"),
        "labor": (None, None, "直接人工总额(元)", "人工总额/产量（元/盒），不是员工时薪"),
    }
    name_col, unit_col, amount_col, semantics = settings[table]
    indexed = {}
    for row in rows:
        name = str(row.get(name_col, "")) if name_col else "直接人工"
        if not name or name == "nan":
            continue
        key = (row["月份"], name)
        if key in indexed:
            raise FactError(f"{table}存在重复明细主键：{key}")
        indexed[key] = row
    result = []
    for name in sorted({key[1] for key in indexed}):
        before = indexed.get((before_month, name))
        after = indexed.get((after_month, name))
        def values(row):
            if row is None:
                return None, None
            amount = _dec(row.get(amount_col))
            volume = _dec(row.get("产量(盒)"))
            unit = _dec(row.get(unit_col)) if unit_col else (
                amount / volume if amount is not None and volume else None)
            # No invented zero for a missing month/amount. Source totals remain authoritative.
            return unit, amount
        u0, a0 = values(before)
        u1, a1 = values(after)
        item = {"name": name, "unit_before": _num(u0), "unit_after": _num(u1),
                "unit_delta": _change(u0, u1), "amount_before": _num(a0),
                "amount_after": _num(a1), "amount_delta": _change(a0, a1),
                "unit_semantics": semantics,
                "status": "complete" if before is not None and after is not None else
                          "missing_previous" if before is None else "missing_current",
                "source_before": _source(table, before, name_col) if before else None,
                "source_after": _source(table, after, name_col) if after else None}
        if table == "labor":
            item["metrics"] = {
                col: {"before": _num(before.get(col)) if before else None,
                      "after": _num(after.get(col)) if after else None}
                for col in ("产量(盒)", "总工时(小时)", "生产人数(人)", "工作天数(天)")}
        result.append(item)
    result.sort(key=lambda x: (x["amount_delta"] is None,
                              -abs(x["amount_delta"]) if x["amount_delta"] is not None else 0,
                              x["name"]))
    return result


def build_facts(data, product, month, d):
    """Return JSON-safe facts, or ``available=False`` with a concrete reason.

    ``d`` is the source DataFrame mapping used to build ``data``; provided tables
    must not contain ambiguous factory/specification or duplicate monthly keys.
    Missing detail tables are allowed. Unrounded alert decisions use source units,
    never dashboard's rounded mom values. Source references use explicit metadata
    when supplied, otherwise honest table/key references (not fabricated CSV lines).
    """
    result = {"available": False, "reason": None, "product": product, "month": month,
              "previous_month": None, "current": None, "previous": None,
              "elements": {}, "alerts": [], "evidence": [], "warnings": []}
    try:
        previous_month = _previous_month(month)
        result["previous_month"] = previous_month
        if data.get("product", product) != product:
            raise FactError("看板产品与请求产品不一致")
        # Follow the exact cost26 source used by build_dashboard_data, not an unrelated history table.
        rows = _rows(d.get("cost26"), product, [previous_month, month], "cost26")
        scope = _scope(rows, "cost26")
        pairs = [[r for r in rows if r["月份"] == m] for m in (previous_month, month)]
        if any(len(rs) > 1 for rs in pairs):
            raise FactError("成本汇总存在重复产品月份或工厂/规格歧义")
        if not pairs[1]:
            raise FactError("缺少本月成本汇总数据")
        if not pairs[0]:
            raise FactError(f"缺少连续上月 {previous_month} 成本汇总数据")
        before, after = pairs[0][0], pairs[1][0]
        amounts = [a for a in data.get("amount_change", []) if a.get("month") == month]
        if len(amounts) != 1 or amounts[0].get("总变动额") is None:
            raise FactError("缺少唯一有效的 amount_change 金额口径结果")
        amount = amounts[0]
        q0, q1 = _required(before, "产量(盒)"), _required(after, "产量(盒)")
        for name, row in (("previous", before), ("current", after)):
            result[name] = {"month": row["月份"], "volume": float(_required(row, "产量(盒)")),
                            "unit_cost": float(_required(row, "单位成本(元/盒)")),
                            "total_cost": float(_required(row, "总成本(元)")),
                            "source": _source("cost26", row)}
        if any(_dec(amount.get(key)) != _required(row, "总成本(元)") for key, row in
               (("上月总成本", before), ("本月总成本", after))):
            raise FactError("amount_change 与传入源表版本/范围不一致")

        def evidence(text, source):
            ident = f"F{len(result['evidence']) + 1:03d}"
            result["evidence"].append({"id": ident, "text": text, "source": source})
            return ident

        for name in ("previous", "current"):
            row = result[name]
            row["evidence_id"] = evidence(
                f"{product} {row['month']} 产量{row['volume']}盒，单位成本{row['unit_cost']}元/盒，总成本{row['total_cost']}元。",
                row["source"])
        source_pair = {"table": "cost26", "key": {"产品名称": product,
                        "月份": [previous_month, month], **scope},
                       "file": None, "line": None, "note": "两期源记录计算；金额和贡献度引用amount_change",
                       "records": [result["previous"]["source"], result["current"]["source"]]}
        result["amount_delta"] = _num(amount["总变动额"])
        result["reconciliation_difference"] = _num(amount.get("勾稽差额"))
        if result['reconciliation_difference'] is not None and abs(result['reconciliation_difference']) > 0.01:
            raise FactError('源表金额不闭合，需先复核后生成归因')
        with localcontext() as ctx:
            ctx.prec = 40
            for label, col in {**ELEMENT_COLUMNS, "单位成本": "单位成本(元/盒)"}.items():
                c0, c1 = _required(before, col), _required(after, col)
                delta = c1 - c0
                pct = delta / c0 * 100 if c0 else None
                # Exact inequality avoids display rounding and recurring-decimal boundary issues.
                alert = bool(c0 and abs(delta) * 100 > c0 * THRESHOLD)
                if alert:
                    result["alerts"].append({"要素": label, "环比%": float(pct),
                        "element": label, "mom_pct": float(pct), "mom_pct_decimal": str(pct),
                        "threshold": 10, "comparison": "strict_abs_gt"})
                unit = {"unit_before": float(c0), "unit_after": float(c1), "unit_delta": float(delta),
                        "mom_pct": _num(pct), "mom_pct_decimal": str(pct) if pct is not None else None,
                        "alert": alert}
                if label == "单位成本":
                    result["unit_cost_change"] = unit
                    continue
                canonical = _dec(amount.get(label + "变动额"))
                if canonical is None or label not in amount.get("贡献度", {}):
                    raise FactError(f"amount_change 缺少{label}金额或贡献度字段")
                unit.update({"amount_delta": float(canonical),
                             "contribution": _num(amount["贡献度"][label]),
                             "volume_effect": float((q1 - q0) * c0),
                             "unit_effect": float(delta * q1)})
                unit["effect_reconciliation_difference"] = float(
                    canonical - (q1 - q0) * c0 - delta * q1)
                if abs(Decimal(str(unit["effect_reconciliation_difference"]))) > Decimal("0.01"):
                    raise FactError(f"{label}金额结果与源表不一致，不能拼接不同版本事实")
                table = {"材料": "material", "人工": "labor", "制费": "mfg"}[label]
                detail_rows = _rows(d.get(table), product, [previous_month, month], table)
                _scope(detail_rows, table, scope)
                unit["detail"] = _detail(table, detail_rows, previous_month, month)
                if not unit["detail"]:
                    result["warnings"].append(f"{label}明细缺失，不能推断具体业务原因")
                unit["evidence_ids"] = [evidence(
                    f"{label}单位成本{c0}→{c1}元/盒，金额变动{unit['amount_delta']}元，"
                    f"贡献度{unit['contribution']}%；产量影响{unit['volume_effect']}元，"
                    f"单位成本影响{unit['unit_effect']}元。" if unit["contribution"] is not None else
                    f"{label}金额变动{unit['amount_delta']}元；总金额变动为零，贡献度无定义；"
                    f"产量影响{unit['volume_effect']}元，单位成本影响{unit['unit_effect']}元。",
                    source_pair)]
                for detail in unit["detail"]:
                    ident = evidence(
                        f"{label}/{detail['name']}：单位成本{detail['unit_before']}→{detail['unit_after']}元/盒；"
                        f"金额{detail['amount_before']}→{detail['amount_after']}元，"
                        f"金额差{detail['amount_delta']}元；{detail['unit_semantics']}。"
                        f"状态{detail['status']}，缺失不能视为零。",
                        {"table": table, "key": {"产品名称": product, "明细": detail["name"],
                         "月份": [previous_month, month]}, "file": None, "line": None,
                         "note": "明细前后两条记录", "records": [detail["source_before"], detail["source_after"]]})
                    detail["evidence_id"] = ident
                    unit["evidence_ids"].append(ident)
                result["elements"][label] = unit
        result["available"] = True
        return result
    except (FactError, InvalidOperation, KeyError, TypeError, ValueError) as exc:
        # Never expose partial numeric facts as a usable analysis on ambiguity/failure.
        return {**result, "available": False, "reason": str(exc), "current": None,
                "previous": None, "elements": {}, "alerts": [], "evidence": []}
