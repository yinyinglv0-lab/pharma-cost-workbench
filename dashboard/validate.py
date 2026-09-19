# -*- coding: utf-8 -*-
"""Five-layer read gate for the V1 cost-data contract.

Validate every supplied accounting year/factory before doing arithmetic. Monetary
reconciliation uses a fixed one-cent tolerance, never tolerance times production.
Missing comparison periods are warnings with undefined changes; malformed schema,
ambiguous identities and inconsistent supplied records are blocking errors.
No database is opened when callers inject their in-memory table mapping.
"""
from decimal import Decimal, InvalidOperation
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import pandas as pd

try:
    from .data_layer import (CONTRIB_ELEMENTS, ELEMENTS, ELEMENT_LABELS,
                             build_dashboard_data, load_merged_tables as load_cost_data)
except ImportError:
    from data_layer import (CONTRIB_ELEMENTS, ELEMENTS, ELEMENT_LABELS,
                            build_dashboard_data, load_merged_tables as load_cost_data)

IDENTITY = ["工厂", "产品名称", "产品规格", "月份"]
COST_KEYS = ("cost26", "cost25", "erchang26", "erchang25")
REQUIRED_COST_COLS = IDENTITY + ELEMENTS
BUDGET_NUMERIC = ["预算产量(盒)", "预算直接材料(元/盒)", "预算直接人工(元/盒)",
                  "预算制造费用(元/盒)", "预算单位成本(元/盒)", "预算总成本(元)"]
REQUIRED_BUDGET_COLS = IDENTITY + BUDGET_NUMERIC
REQUIRED_MATERIAL_COLS = IDENTITY + ["产量(盒)", "原材料名称", "单位消耗成本(元/盒)",
                                    "原材料总成本(元)", "占总材料成本比例"]
REQUIRED_LABOR_COLS = IDENTITY + ["产量(盒)", "直接人工总额(元)", "总工时(小时)",
                                 "生产人数(人)", "工作天数(天)"]
REQUIRED_MFG_COLS = IDENTITY + ["产量(盒)", "费用类别", "单位费用(元/盒)", "费用总额(元)"]
DETAIL_TABLES = [
    ("原材料消耗明细", "material", REQUIRED_MATERIAL_COLS,
     ["产量(盒)", "单位消耗成本(元/盒)", "原材料总成本(元)", "占总材料成本比例"]),
    ("人工工时明细", "labor", REQUIRED_LABOR_COLS,
     ["产量(盒)", "直接人工总额(元)", "总工时(小时)", "生产人数(人)", "工作天数(天)"]),
    ("制造费用明细", "mfg", REQUIRED_MFG_COLS,
     ["产量(盒)", "单位费用(元/盒)", "费用总额(元)"]),
]
EXTRA_KEYS = {"material": ["原材料名称"], "mfg": ["费用类别"]}
AMOUNT_TOLERANCE = Decimal("0.01")
UNIT_TOLERANCE = Decimal("0.005")
LAYERS = ("L1_完整性", "L2_准确性", "L3_勾稽", "L4_隔离", "L5_边界")


def _cost_keys(data):
    return list(dict.fromkeys([*COST_KEYS, *(key for key in data if re.fullmatch(r"(?:cost|erchang)\d+", str(key)))]))


def _schemas(data):
    result = {key: (REQUIRED_COST_COLS, ELEMENTS) for key in _cost_keys(data)}
    result["budget"] = (REQUIRED_BUDGET_COLS, BUDGET_NUMERIC)
    result.update({key: (required, numeric) for _, key, required, numeric in DETAIL_TABLES})
    return result


def _number(value):
    if isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, ValueError, TypeError):
        return None
    return number if number.is_finite() else None


def _ratio(value):
    return _number(str(value).strip().rstrip("%"))


def _identity(row):
    return tuple(str(row[key]) for key in IDENTITY)


def _label(key, row):
    return f"{key} {' / '.join(str(row.get(field, '缺失')) for field in IDENTITY)}"


def check_structure(d=None):
    """Schema failure blocks all later layers; optional absent tables stay explicit."""
    data = load_cost_data() if d is None else d
    errors, warnings = [], []
    if not isinstance(data, dict):
        return ["L0 结构 数据必须为表名到DataFrame的映射"], []
    for key, (required, _) in _schemas(data).items():
        frame = data.get(key)
        if frame is None or (isinstance(frame, pd.DataFrame) and frame.empty):
            if key == "cost26":
                errors.append("L0 结构 成本汇总cost26: 数据为空或未加载")
            elif key in ("cost25", "budget", "material", "labor", "mfg"):
                warnings.append(f"L0 结构 {key}: 数据为空，相应比较/明细不可计算")
            continue
        if not isinstance(frame, pd.DataFrame):
            errors.append(f"L0 结构 {key}: 必须为DataFrame")
            continue
        if frame.columns.duplicated().any():
            errors.append(f"L0 结构 {key}: 重复列名")
        missing = [column for column in required if column not in frame.columns]
        if missing:
            errors.append(f"L0 结构 {key}: 缺少必需列 {missing}")
    return errors, warnings


def check_integrity(d=None):
    """All supplied years, budget and details: finite numbers, keys, dates and scope."""
    data = load_cost_data() if d is None else d
    errors, warnings = check_structure(data)
    if errors:
        return errors, warnings
    seen_costs = set()
    for key, (_, numeric) in _schemas(data).items():
        frame = data.get(key)
        if frame is None or frame.empty:
            continue
        keys = IDENTITY + EXTRA_KEYS.get(key, [])
        for _, row in frame.iterrows():
            label = _label(key, row)
            for field in keys:
                value = row[field]
                if not isinstance(value, str) or not value.strip():
                    errors.append(f"L1 {label}: {field}为空或不是有效文本身份")
            month = row["月份"]
            if (not isinstance(month, str) or not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month)
                    or month.startswith("0000-")):
                errors.append(f"L1 {label}: 月份须为有效YYYY-MM")
            expected_factory = "中药二厂" if key.startswith("erchang") else "中药一厂" if key in _cost_keys(data) else None
            if expected_factory and row["工厂"] != expected_factory:
                errors.append(f"L1 {label}: {key}工厂须为{expected_factory}")
            for column in numeric:
                value = _ratio(row[column]) if column == "占总材料成本比例" else _number(row[column])
                if value is None:
                    errors.append(f"L1 {label}: {column}为空、非数值或非有限数")
                elif value < 0:
                    errors.append(f"L1 {label}: {column}负值{value}")
                elif column == "占总材料成本比例" and value > 100:
                    errors.append(f"L1 {label}: 材料比例超过100%")
                elif value == 0 and column in ("产量(盒)", "单位成本(元/盒)", "总工时(小时)", "生产人数(人)", "工作天数(天)"):
                    warnings.append(f"L1 {label}: {column}为零，涉及该分母的指标不可计算")
            if key in _cost_keys(data):
                identity = _identity(row)
                if identity in seen_costs:
                    errors.append(f"L1 {label}: 汇总主键重复（含跨年份容器重复）")
                seen_costs.add(identity)
        if frame.duplicated(keys).any():
            errors.append(f"L1 {key}: 工厂/产品/规格/月份及明细主键重复")
        # Missing calendar periods are valid incomplete comparisons, never adjacent-row MoM.
        for scope, group in frame.groupby(IDENTITY[:-1], dropna=False):
            months = sorted({str(value) for value in group["月份"] if isinstance(value, str) and re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", value)})
            for previous, current in zip(months, months[1:]):
                py, pm = map(int, previous.split("-"))
                cy, cm = map(int, current.split("-"))
                if cy * 12 + cm - py * 12 - pm != 1:
                    warnings.append(f"L1 {key} {scope}: 月份不连续 {previous} → {current}，缺口处不算环比")
    if not errors:
        current_keys = {_identity(row) for row in data["cost26"].to_dict("records")}
        for key in ("budget", "material", "labor", "mfg"):
            frame = data.get(key)
            if frame is None or frame.empty:
                continue  # Entirely absent tables already have a clear warning.
            available = {_identity(row) for row in frame.to_dict("records")}
            for identity in sorted(current_keys - available):
                warnings.append(f"L1 {key} {' / '.join(identity)}: 缺少同口径记录，相应预算/明细指标不可计算")
    return list(dict.fromkeys(errors)), list(dict.fromkeys(warnings))


def _numeric_copy(data):
    result = dict(data)
    for key, (_, numeric) in _schemas(data).items():
        frame = data.get(key)
        if frame is None or frame.empty:
            result[key] = pd.DataFrame() if frame is None else frame.copy()
            continue
        frame = frame.copy()
        for column in numeric:
            if column != "占总材料成本比例":
                frame[column] = pd.to_numeric(frame[column])
        result[key] = frame
    return result


def _scopes(data):
    for (factory, product, specification), _ in data["cost26"].groupby(IDENTITY[:-1], sort=True):
        scoped = {}
        for key, frame in data.items():
            if isinstance(frame, pd.DataFrame) and set(IDENTITY[:-1]).issubset(frame.columns):
                scoped[key] = frame[(frame["工厂"] == factory) & (frame["产品名称"] == product) & (frame["产品规格"] == specification)].copy()
            else:
                scoped[key] = frame
        yield product, specification, scoped


def _recompute_independent(product, d):
    """Independent pandas arithmetic, using the exact consecutive calendar month."""
    sub = d["cost26"].loc[d["cost26"]["产品名称"].eq(product)].sort_values("月份").reset_index(drop=True)
    history = pd.concat([d.get("cost25", pd.DataFrame()), d["cost26"]], ignore_index=True)
    budget = d.get("budget", pd.DataFrame())
    bcols = dict(zip(ELEMENTS, ["预算" + col for col in ELEMENTS]))
    out = {"mom": [], "yoy": [], "budget": [], "contrib": []}
    def rate(current, base):
        return None if base is None or base == 0 else (current-base)/base*100
    for index, row in sub.iterrows():
        previous_month = str(pd.Period(row["月份"], freq="M") - 1)
        prior_rows = sub.loc[sub["月份"].eq(previous_month)]
        prev = prior_rows.iloc[0] if not prior_rows.empty else None
        if index:
            out["mom"].append({col: rate(row[col], prev[col] if prev is not None else None) for col in ELEMENTS})
            delta = row["总成本(元)"] - prev["总成本(元)"] if prev is not None else None
            out["contrib"].append({col: (row[col]*row["产量(盒)"] - prev[col]*prev["产量(盒)"])/delta*100 if delta else None for col in CONTRIB_ELEMENTS})
        year_ago = str(pd.Period(row["月份"], freq="M") - 12)
        prior = history.loc[history["月份"].eq(year_ago)]
        out["yoy"].append({col: rate(row[col], prior.iloc[0][col] if not prior.empty else None) for col in ELEMENTS})
        target = budget.loc[budget["月份"].eq(row["月份"])] if not budget.empty else budget
        out["budget"].append({col: rate(row[col], target.iloc[0][bcols[col]] if not target.empty else None) for col in ELEMENTS})
    return out


def check_accuracy(d=None, tol_pct=0.01):
    data = load_cost_data() if d is None else d
    errors, _ = check_integrity(data)
    if errors:
        return errors
    data = _numeric_copy(data)
    for product, specification, scoped in _scopes(data):
        actual = build_dashboard_data(product, scoped)
        expected = _recompute_independent(product, scoped)
        for field, reference, columns, skip in [("mom", "mom", ELEMENTS, 1), ("yoy", "yoy", ELEMENTS, 0),
                                                ("budget_var", "budget", ELEMENTS, 0), ("contribution_raw", "contrib", CONTRIB_ELEMENTS, 1)]:
            for position, (left, right) in enumerate(zip(actual[field][skip:], expected[reference]), start=skip):
                for column in columns:
                    a, b = left.get(ELEMENT_LABELS[column]), right[column]
                    if a is None and b is None:
                        continue
                    if a is None or b is None or abs(a-b) > max(abs(b)*tol_pct/100, 1e-6):
                        errors.append(f"L2 {product}/{specification}/{left['month']} {field}/{column}: {a} vs 独立复算{b}")
    return errors


def check_consistency(d=None, tol=0.01):
    """Every year/factory and supplied detail must reconcile within fixed 0.01 yuan.

    ``tol`` is retained for callers but cannot widen the public accounting gate.
    Unit fields have a separate .005 yuan/box tolerance, matching staged imports.
    """
    data = load_cost_data() if d is None else d
    errors, _ = check_integrity(data)
    if errors:
        return errors
    tolerance = min(abs(Decimal(str(tol))), AMOUNT_TOLERANCE)
    costs = {}
    for key in _cost_keys(data):
        frame = data.get(key)
        if frame is None or frame.empty:
            continue
        for row in frame.to_dict("records"):
            identity = _identity(row)
            costs[identity] = row
            label = _label(key, row)
            unit_sum = sum(_number(row[col]) for col in CONTRIB_ELEMENTS)
            if abs(unit_sum - _number(row["单位成本(元/盒)"])) > UNIT_TOLERANCE:
                errors.append(f"L3 {label}: 三要素之和与单位成本不闭合")
            delta = _number(row["单位成本(元/盒)"])*_number(row["产量(盒)"]) - _number(row["总成本(元)"])
            if abs(delta) > tolerance:
                errors.append(f"L3 {label}: 单位成本×产量与总成本不闭合，差额{delta}元（容差{tolerance}元）")
    budget = data.get("budget")
    if budget is not None and not budget.empty:
        for row in budget.to_dict("records"):
            label = _label("budget", row)
            unit = _number(row["预算单位成本(元/盒)"])
            components = sum(_number(row["预算"+col]) for col in CONTRIB_ELEMENTS)
            if abs(components-unit) > UNIT_TOLERANCE:
                errors.append(f"L3 {label}: 预算三要素之和与预算单位成本不闭合")
            delta = unit*_number(row["预算产量(盒)"]) - _number(row["预算总成本(元)"])
            if abs(delta) > tolerance:
                errors.append(f"L3 {label}: 预算单位成本×预算产量与预算总额不闭合，差额{delta}元")
    for key, unit_col, amount_col, target_col in [
            ("material", "单位消耗成本(元/盒)", "原材料总成本(元)", "直接材料(元/盒)"),
            ("mfg", "单位费用(元/盒)", "费用总额(元)", "制造费用(元/盒)"),
            ("labor", None, "直接人工总额(元)", "直接人工(元/盒)")]:
        frame = data.get(key)
        if frame is None or frame.empty:
            continue
        for identity, group in frame.groupby(IDENTITY, dropna=False, sort=False):
            rows = group.to_dict("records")
            label = _label(key, rows[0])
            base = costs.get(tuple(str(value) for value in identity))
            if base is None:
                errors.append(f"L3 {label}: 缺少同工厂/产品/规格/月份的成本汇总")
                continue
            quantity = _number(base["产量(盒)"])
            actual = sum(_number(row[amount_col]) for row in rows)
            target = _number(base[target_col])*quantity
            if abs(actual-target) > tolerance:
                errors.append(f"L3 {label}: 明细总额{actual}与汇总金额{target}不闭合")
            if unit_col and abs(sum(_number(row[unit_col]) for row in rows)-_number(base[target_col])) > UNIT_TOLERANCE:
                errors.append(f"L3 {label}: 明细单位成本合计与汇总不闭合")
            for row in rows:
                q = _number(row["产量(盒)"])
                if q != quantity:
                    errors.append(f"L3 {label}: 明细产量与汇总产量不一致")
                if unit_col and abs(_number(row[unit_col])*q-_number(row[amount_col])) > tolerance:
                    errors.append(f"L3 {label}: 明细行单位成本×产量与金额不闭合，容差{tolerance}元")
            if key == "material" and abs(sum(_ratio(row["占总材料成本比例"]) for row in rows)-100) > 1:
                errors.append(f"L3 {label}: 材料占比合计不在100%±1个百分点内")
    return list(dict.fromkeys(errors))


def check_isolation(d=None):
    data = load_cost_data() if d is None else d
    errors, _ = check_integrity(data)
    if errors:
        return errors
    data = _numeric_copy(data)
    for product, specification, scoped in _scopes(data):
        output = build_dashboard_data(product, scoped)
        expected = scoped["cost26"].sort_values("月份")
        months = [row["month"] for row in output["series"]]
        if months != sorted(set(months)):
            errors.append(f"L4 {product}/{specification}: 月份重复或乱序")
        for actual, (_, row) in zip(output["series"], expected.iterrows()):
            if actual["month"] != row["月份"] or any(actual[ELEMENT_LABELS[col]] != row[col] for col in ELEMENTS):
                errors.append(f"L4 {product}/{specification}/{row['月份']}: 输出与源行不一致")
    return errors


def check_boundaries():
    """Small in-memory V1 boundary probes; no data loader, database or filesystem."""
    errors = []
    rows = [{"工厂": "中药一厂", "产品名称": "边界测试品", "产品规格": "测试", "月份": f"2026-{month:02d}",
             "产量(盒)": 100, "直接材料(元/盒)": 1.0, "直接人工(元/盒)": 1.0,
             "制造费用(元/盒)": 1.0, "单位成本(元/盒)": 3.0, "总成本(元)": 300.0} for month in range(1, 7)]
    def inputs(values):
        return {"cost26": pd.DataFrame(values), "cost25": pd.DataFrame(), "budget": pd.DataFrame()}
    zero = [dict(row) for row in rows]
    zero[1]["单位成本(元/盒)"] = 0
    result = build_dashboard_data("边界测试品", inputs(zero))
    if result["mom"][2]["单位成本"] is not None or result["mom"][2]["材料"] is None:
        errors.append("L5 零基期: 应仅对应指标不可计算")
    extreme = [dict(row) for row in rows]
    extreme[1]["直接材料(元/盒)"] = 10
    if not any("超±500%" in warning for warning in build_dashboard_data("边界测试品", inputs(extreme))["warnings"]):
        errors.append("L5 极端波动: 未产生预警")
    missing = inputs([row for row in rows if row["月份"] != "2026-04"])
    _, warnings = check_integrity(missing)
    output = build_dashboard_data("边界测试品", missing)
    may = next(row for row in output["amount_change"] if row["month"] == "2026-05")
    if not any("不连续" in warning for warning in warnings) or may["总变动额"] is not None:
        errors.append("L5 缺月: 必须告警并禁止跨缺口计算")
    negative = [dict(row) for row in rows]
    negative[0]["总成本(元)"] = -1
    issues, _ = check_integrity(inputs(negative))
    if not any("负值" in issue for issue in issues):
        errors.append("L5 负数: 未阻断")
    return errors


def run_all_validation(d=None, verbose=True):
    """Public read gate: stop on invalid structure/values before dependent arithmetic."""
    data = load_cost_data() if d is None else d
    integrity = check_integrity(data)
    results = {layer: ([], []) for layer in LAYERS}
    results["L1_完整性"] = integrity
    if integrity[0]:
        for layer in LAYERS[1:]:
            results[layer] = ([], ["前置结构/数值校验失败，跳过依赖计算"])
    else:
        consistency = check_consistency(data)
        results["L3_勾稽"] = (consistency, [])
        if consistency:
            for layer in ("L2_准确性", "L4_隔离"):
                results[layer] = ([], ["金额勾稽失败，跳过依赖计算"])
        else:
            results["L2_准确性"] = (check_accuracy(data), [])
            results["L4_隔离"] = (check_isolation(data), [])
        results["L5_边界"] = (check_boundaries(), [])
    if verbose:
        print("成本数据统一五层验证（结构错误短路；全年、两厂、预算、明细固定金额容差）")
        for layer, (errors, warnings) in results.items():
            print(f"{layer}: {len(errors)} 个错误 / {len(warnings)} 条提示")
            for message in [*errors[:5], *warnings[:5]]:
                print("  " + message)
    return results


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    result = run_all_validation()
    return 1 if any(errors for errors, _ in result.values()) else 0


if __name__ == "__main__":
    raise SystemExit(main())
