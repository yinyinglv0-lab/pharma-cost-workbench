"""Pure one-month unit-cost baselines over an explicitly supplied table snapshot.

No loader, model, network, repository, or clock is called. Cost summaries use the
workspace's Chinese column names. All supplied summary rows are validated, but
fitting, backtesting, gaps, and training hashes use observations at/before cutoff
only. An absent cutoff observation is an error, never a request to fill a gap.

``naive`` uses the cutoff observation; ``ma3`` uses the last three calendar months;
``ses`` uses fixed-alpha simple exponential smoothing over the contiguous segment.
Candidate methods (and the requested method if absent from the candidates) share
origins after the largest required lookback, all within the cutoff's segment.
An origin is the LAST training month; its target is the following month, which
must already be observed at cutoff. Thus Jan--Jun provides Mar/Apr/May origins
and Apr/May/Jun targets when comparing naive and ma3. SES is deliberately fixed
(alpha=0.3), is not an MA3-equivalent shortcut, and is never fitted or selected
from the same backtest errors.

Numeric predictions are unrounded floats. Unit cost is computed by summing the
returned component floats, so they close exactly under Python's ``sum``. Exact
rational strings (``numerator/denominator``, or an integer) also preserve the
unrounded arithmetic, including repeating ma3 means. No future volume is
accepted or inferred and no total-cost forecast is produced. Invalid inputs,
unsupported horizons, and insufficient history for the requested method raise
ValueError. Candidate methods with no common folds have null metrics.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
from fractions import Fraction
import hashlib
import json
import math
from numbers import Integral
import re

import pandas as pd


SCHEMA_VERSION = "cost-forecast/1.0"
METHOD_VERSION = "cost-baseline/1.0"
ELEMENTS = {
    "材料": "直接材料(元/盒)",
    "人工": "直接人工(元/盒)",
    "制费": "制造费用(元/盒)",
}
IDENTITY = ("工厂", "产品名称", "产品规格", "月份")
UNIT_COLUMN = "单位成本(元/盒)"
LOOKBACK = {"naive": 1, "ma3": 3, "ses": 2}
SES_ALPHA = Fraction(3, 10)
METHOD_LABELS = {"naive": "上期持平（基准）", "ma3": "近三月均值", "ses": "简单指数平滑（固定 α=0.3）"}
SUMMARY_KEYS = {
    "cost", "cost25", "cost26", "erchang25", "erchang26", "cost25_2", "cost26_2",
    "yichang_2025", "yichang_2026", "erchang_2025", "erchang_2026",
}
HASH_ATTRS = ("cost_snapshot_hash", "source_hash", "source_sha256")
CLOSURE_TOLERANCE = Fraction(1, 10**12)


class ForecastInputError(ValueError):
    """Expected invalid forecast input; callers can avoid hiding unrelated bugs."""


def _json_copy(value, label):
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False))
    except (TypeError, ValueError, OverflowError):
        raise ForecastInputError(f"{label} must contain finite JSON-compatible values") from None


def _month(value, label="month"):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-(0[1-9]|1[0-2])", value):
        raise ForecastInputError(f"{label} must be YYYY-MM")
    year, month = map(int, value.split("-"))
    if year < 1:
        raise ForecastInputError(f"{label} year must be between 0001 and 9999")
    return year * 12 + month - 1


def _month_text(ordinal):
    year, month = divmod(ordinal, 12)
    if not 1 <= year <= 9999:
        raise ForecastInputError("target month is outside supported years 0001..9999")
    return f"{year:04d}-{month + 1:02d}"


def _identity(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ForecastInputError(f"{label} must be a nonempty string")
    if value != value.strip():
        raise ForecastInputError(f"{label} must not have surrounding whitespace")
    return value


def _finite_float(value):
    try:
        number = float(value)
    except (ValueError, OverflowError):
        raise ForecastInputError("cost arithmetic exceeds the finite numeric output range") from None
    if not math.isfinite(number):
        raise ForecastInputError("cost arithmetic exceeds the finite numeric output range")
    return number


def _number(value, label):
    if isinstance(value, bool):
        raise ForecastInputError(f"{label} must be a finite nonnegative number, not bool")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ForecastInputError(f"{label} must be a finite nonnegative number") from None
    if not number.is_finite() or number < 0:
        raise ForecastInputError(f"{label} must be a finite nonnegative number")
    _finite_float(number)
    return Fraction(number)


def _cost(elements):
    numeric = {key: _finite_float(elements[key]) for key in ELEMENTS}
    unit = _finite_float(sum(numeric.values()))
    return {
        "unit_cost": unit,
        "elements": numeric,
        "unit_cost_exact": str(sum(elements.values(), Fraction())),
        "elements_exact": {key: str(elements[key]) for key in ELEMENTS},
    }


def _table_rows(tables):
    if not isinstance(tables, Mapping):
        raise ForecastInputError("tables must be a mapping of summary names to pandas DataFrames")
    required = {*IDENTITY, *ELEMENTS.values(), UNIT_COLUMN}
    rows, seen, attrs = [], set(), {}
    for name, frame in tables.items():
        is_summary = name in SUMMARY_KEYS or (
            isinstance(frame, pd.DataFrame) and required.issubset(frame.columns)
        )
        if not is_summary:
            continue
        if not isinstance(name, str) or not isinstance(frame, pd.DataFrame):
            raise ForecastInputError("cost summary tables must be named pandas DataFrames")
        metadata = {key: frame.attrs[key] for key in (*HASH_ATTRS, "cost_revision") if key in frame.attrs}
        for key in HASH_ATTRS:
            if key in metadata and (not isinstance(metadata[key], str) or not metadata[key].strip()):
                raise ForecastInputError(f"{name}.attrs[{key}] must be a nonempty hash string")
        attrs[name] = _json_copy(metadata, f"{name}.attrs")
        if frame.empty:
            continue
        if frame.columns.duplicated().any():
            raise ForecastInputError(f"{name} has duplicate column names")
        if not required.issubset(frame.columns):
            raise ForecastInputError(f"{name} missing required columns: {sorted(required - set(frame.columns))}")
        for raw in frame.to_dict(orient="records"):
            identity = tuple(_identity(raw[key], key) for key in IDENTITY)
            ordinal = _month(identity[3])
            if identity in seen:
                raise ForecastInputError(f"duplicate factory/product/specification/month: {identity}")
            seen.add(identity)
            elements = {key: _number(raw[column], column) for key, column in ELEMENTS.items()}
            unit = _number(raw[UNIT_COLUMN], UNIT_COLUMN)
            computed = sum(elements.values(), Fraction())
            tolerance = CLOSURE_TOLERANCE * max(Fraction(1), unit, computed)
            if abs(unit - computed) > tolerance:
                raise ForecastInputError(f"{identity}: source components do not sum to unit cost")
            # Historical volume/amount are checked if supplied, never used to forecast.
            for column in ("产量(盒)", "总成本(元)"):
                if column in raw:
                    _number(raw[column], column)
            _cost(elements)  # Fail before returning any overflowing component sum.
            source_hash = raw.get("_source_hash")
            rows.append({
                "identity": identity, "ordinal": ordinal, "month": identity[3],
                "elements": elements, "reported_unit_cost": unit, "table": name,
                "source_hash": source_hash if isinstance(source_hash, str) and source_hash else None,
            })
    if not rows:
        raise ForecastInputError("no cost summary observations supplied")
    snapshot_hashes = {value["cost_snapshot_hash"] for value in attrs.values() if "cost_snapshot_hash" in value}
    if len(snapshot_hashes) > 1:
        raise ForecastInputError("conflicting cost_snapshot_hash attrs: tables are not one snapshot")
    return rows, attrs


def _training_hash(rows):
    normalized = [{
        "identity": list(row["identity"]),
        "elements": {key: str(row["elements"][key]) for key in ELEMENTS},
        "reported_unit_cost": str(row["reported_unit_cost"]),
    } for row in rows]
    encoded = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def forecast_method_availability(months, cutoff_month):
    """Describe valid methods for a scope; no values, interpolation, or I/O needed."""
    cutoff = _month(cutoff_month, "cutoff_month")
    observed = {_month(month) for month in months}
    count, cursor = 0, cutoff
    while cursor in observed:
        count += 1
        cursor -= 1
    return {
        "contiguous_months": count,
        "available_methods": [name for name, minimum in LOOKBACK.items() if count >= minimum],
        "unavailable_methods": {
            name: f"{METHOD_LABELS[name]}需要截至 {cutoff_month} 至少 {minimum} 个连续月份；"
                  f"当前只有 {count} 个。请选择更晚的连续截止月或其他可用方法。"
            for name, minimum in LOOKBACK.items() if count < minimum
        },
    }


def _method_rows(rows, method):
    return rows if method == "ses" else rows[-LOOKBACK[method]:]


def _predict(rows, method):
    used = _method_rows(rows, method)
    if method == "ses":
        level = dict(used[0]["elements"])
        for row in used[1:]:
            level = {key: SES_ALPHA * row["elements"][key] + (1 - SES_ALPHA) * level[key]
                     for key in ELEMENTS}
        return level
    return {key: sum((row["elements"][key] for row in used), Fraction()) / len(used) for key in ELEMENTS}


def _change(predicted, reference):
    """Exact signed change; zero denominators never become invented percentages."""
    delta = predicted - reference
    percent = delta / reference * 100 if reference else None
    numeric_percent = None
    if percent is not None:
        try:
            numeric_percent = _finite_float(percent)
        except ForecastInputError:
            pass  # Keep exact ratio but never return JSON Infinity.
    return {
        "delta": _finite_float(delta), "delta_exact": str(delta),
        "delta_percent": numeric_percent,
        "delta_percent_exact": str(percent) if percent is not None else None,
        "percent_status": "zero_reference" if reference == 0 else (
            "defined" if numeric_percent is not None else "outside_numeric_range"),
    }


def _budget_comparison(tables, scope, target_month, predicted, snapshot_hash):
    """Compare only one exact target identity in the supplied original budget table.

    Missing, ambiguous or invalid budgets do not invalidate a cost forecast; they
    explicitly withhold comparison. No annual/month-of-year fallback is permitted.
    A supplied current budget is not claimed to be a historical as-of vintage.
    """
    result = {
        "status": "unavailable", "target_month": target_month,
        "factory": scope[0], "product": scope[1], "specification": scope[2],
        "budget_unit_cost": None, "budget_unit_cost_exact": None,
        "delta": None, "delta_exact": None, "delta_percent": None,
        "delta_percent_exact": None, "direction": None,
        "source": {"table": "budget"},
        "policy": "exact factory/product/specification/YYYY-MM; no carry-forward or cross-year reuse",
        "historical_vintage_verified": False,
    }

    def withheld(code, note, status="unavailable"):
        return {**result, "status": status, "reason_code": code, "note": note}

    frame = tables.get("budget")
    if frame is None or (isinstance(frame, pd.DataFrame) and frame.empty):
        return withheld("missing_budget", "未提供预算表；请补充同工厂、产品、规格和目标年月的已确认预算。")
    required = {*IDENTITY, "预算单位成本(元/盒)"}
    if (not isinstance(frame, pd.DataFrame) or frame.columns.duplicated().any()
            or not required.issubset(frame.columns)):
        return withheld("invalid_budget_schema", "预算表缺少唯一身份列或预算单位成本列，请核对预算模板。", "invalid")
    budget_hash = frame.attrs.get("cost_snapshot_hash")
    if budget_hash is not None and (not isinstance(budget_hash, str) or not budget_hash.strip()):
        return withheld("invalid_budget_snapshot", "预算快照标识无效，请重新载入已确认预算。", "invalid")
    if budget_hash is not None and snapshot_hash and budget_hash != snapshot_hash:
        return withheld("snapshot_mismatch", "预算与成本数据版本不一致，请使用同一已确认快照重新计算。", "invalid")
    matching = frame
    for column, value in zip(IDENTITY, (*scope, target_month)):
        matching = matching.loc[matching[column].eq(value)]
    if matching.empty:
        return withheld("missing_target_budget", f"未找到 {target_month} 同工厂、产品、规格的预算；不沿用其他月份或年份。")
    if len(matching) != 1:
        return withheld("ambiguous_target_budget", "目标年月存在重复预算，请先确认唯一预算版本。", "invalid")
    row = matching.to_dict(orient="records")[0]
    try:
        budget = _number(row["预算单位成本(元/盒)"], "预算单位成本(元/盒)")
        columns = ["预算" + column for column in ELEMENTS.values()]
        present = [column in frame.columns for column in columns]
        if any(present) and not all(present):
            return withheld("incomplete_budget_components", "预算三要素列不完整，请核对后再比较。", "invalid")
        if all(present):
            components = sum((_number(row[column], column) for column in columns), Fraction())
            if abs(components - budget) > CLOSURE_TOLERANCE * max(Fraction(1), budget, components):
                return withheld("nonclosing_budget", "预算三要素与预算单位成本不闭合，请核对原预算。", "invalid")
    except ValueError:
        return withheld("invalid_budget_value", "目标预算含空值、负数或非有限金额，请核对后再比较。", "invalid")
    source = {"table": "budget", "cost_snapshot_hash": budget_hash}
    for key in ("_source_file", "_source_hash", "_source_row", "_source_sheet"):
        value = row.get(key)
        if isinstance(value, str) or (isinstance(value, (int, float)) and not isinstance(value, bool)
                                      and math.isfinite(value)):
            source[key.removeprefix("_")] = value
    encoded = json.dumps({"identity": [*scope, target_month], "budget_unit_cost_exact": str(budget)},
                         ensure_ascii=False, sort_keys=True)
    source["comparison_hash"] = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    return {**result, "status": "matched", "reason_code": "exact_target_match",
            "budget_unit_cost": _finite_float(budget), "budget_unit_cost_exact": str(budget),
            **_change(predicted, budget),
            "direction": "above" if predicted > budget else "below" if predicted < budget else "equal",
            "source": source,
            "note": "仅对照当前输入快照中的目标月单位成本预算；不是已实现节约、总成本预测或收益承诺。"}


def _vector(elements):
    return {"unit_cost": sum(elements.values(), Fraction()), **elements}


def _metric(errors, actuals):
    count = len(errors)
    nonzero = sum(value != 0 for value in actuals)
    if not count:
        return {"sample_count": 0, "mae": None, "rmse": None, "mape": None, "mape_defined_count": 0}
    scale = max(abs(value) for value in errors)
    # Scaling avoids squaring large floats and overflowing a finite RMSE.
    rmse = (_finite_float(scale) * math.sqrt(_finite_float(
        sum(((value / scale) ** 2 for value in errors), Fraction()) / count
    ))) if scale else 0.0
    return {
        "sample_count": count,
        "mae": _finite_float(sum((abs(value) for value in errors), Fraction()) / count),
        "rmse": rmse,
        # A zero actual invalidates aggregate MAPE; never silently drop that fold.
        "mape": _finite_float(sum((abs(error / actual) * 100 for error, actual in zip(errors, actuals)),
                                  Fraction()) / count) if nonzero == count else None,
        "mape_defined_count": nonzero,
    }


def _backtest(segment, method, first_target):
    folds, errors, actuals = [], [], []
    for index in range(first_target, len(segment)):
        training, target = segment[:index], segment[index]
        predicted = _predict(training, method)
        predicted_values, actual_values = _vector(predicted), _vector(target["elements"])
        error = {key: predicted_values[key] - actual_values[key] for key in predicted_values}
        folds.append({
            "origin_month": training[-1]["month"], "target_month": target["month"],
            "training_months": [row["month"] for row in training],
            "method_training_months": [row["month"] for row in _method_rows(training, method)],
            "training_hash": _training_hash(training),
            "prediction": _cost(predicted), "actual": _cost(target["elements"]),
            "errors": {key: _finite_float(value) for key, value in error.items()},
            "absolute_errors": {key: _finite_float(abs(value)) for key, value in error.items()},
            "absolute_percentage_errors": {
                key: _finite_float(abs(value / actual_values[key]) * 100) if actual_values[key] else None
                for key, value in error.items()
            },
        })
        errors.append(error)
        actuals.append(actual_values)
    metrics = {key: _metric([row[key] for row in errors], [row[key] for row in actuals])
               for key in ("unit_cost", *ELEMENTS)}
    return {"status": "evaluated" if folds else "insufficient_history",
            **metrics["unit_cost"], "metrics": metrics, "folds": folds}


def forecast_baseline(tables, *, factory, product, specification, cutoff_month,
                      horizon=1, method="naive", candidate_methods=("naive", "ma3"),
                      snapshot_meta=None) -> dict:
    """Return an auditable one-step forecast; see module docstring for the contract.

    ``training_months`` is the whole contiguous segment available at cutoff;
    ``method_training_months`` identifies observations actually used by the point
    forecast. Backtests are descriptive comparisons, not unbiased estimates of a
    method selected using those same errors. No method selection is performed.
    MAPE is a percentage and is null if any evaluated actual is zero.

    The interval has null bounds and coverage. Below five common residuals its
    status is ``insufficient_calibration``; otherwise it is ``not_calibrated``.
    This baseline does not assert interval coverage from dependent rolling errors.
    """
    scope = tuple(_identity(value, label) for label, value in (
        ("factory", factory), ("product", product), ("specification", specification)))
    cutoff = _month(cutoff_month, "cutoff_month")
    if isinstance(horizon, bool) or not isinstance(horizon, Integral) or horizon != 1:
        raise ForecastInputError("only one-step horizon=1 is supported; forecasts are not chained")
    target_month = _month_text(cutoff + 1)
    if not isinstance(method, str) or method not in LOOKBACK:
        raise ForecastInputError("method must be naive, ma3, or ses")
    if isinstance(candidate_methods, (str, bytes)) or not isinstance(candidate_methods, Sequence):
        raise ForecastInputError("candidate_methods must be a nonempty sequence of method names")
    candidates = list(candidate_methods)
    if not candidates or any(not isinstance(item, str) or item not in LOOKBACK for item in candidates):
        raise ForecastInputError("candidate_methods must contain naive, ma3, and/or ses")
    if len(set(candidates)) != len(candidates):
        raise ForecastInputError("candidate_methods must not contain duplicates")
    if snapshot_meta is not None and not isinstance(snapshot_meta, Mapping):
        raise ForecastInputError("snapshot_meta must be a JSON-compatible mapping or None")
    snapshot = _json_copy(dict(snapshot_meta), "snapshot_meta") if snapshot_meta is not None else None
    rows, table_attrs = _table_rows(tables)
    history = sorted((row for row in rows if row["identity"][:3] == scope and row["ordinal"] <= cutoff),
                     key=lambda row: row["ordinal"])
    if not history or history[-1]["ordinal"] != cutoff:
        raise ForecastInputError("cutoff_month must have an observation for the exact factory/product/specification")
    gaps = [{
        "after_month": left["month"], "before_month": right["month"],
        "missing_months": [_month_text(index) for index in range(left["ordinal"] + 1, right["ordinal"])],
    } for left, right in zip(history, history[1:]) if right["ordinal"] - left["ordinal"] > 1]
    start = len(history) - 1
    while start and history[start]["ordinal"] - history[start - 1]["ordinal"] == 1:
        start -= 1
    segment = history[start:]
    if len(segment) < LOOKBACK[method]:
        raise ForecastInputError(f"{method} requires {LOOKBACK[method]} contiguous months ending at cutoff_month")
    evaluated = candidates + ([] if method in candidates else [method])
    first_target = max(LOOKBACK[item] for item in evaluated)
    backtests = {item: _backtest(segment, item, first_target) for item in evaluated}
    residual_count = backtests[method]["sample_count"]
    attr_hashes = sorted({value[key] for value in table_attrs.values() for key in HASH_ATTRS if key in value})
    snapshot_hashes = sorted({value["cost_snapshot_hash"] for value in table_attrs.values() if "cost_snapshot_hash" in value})
    source_hash = snapshot_hashes[0] if snapshot_hashes else (attr_hashes[0] if len(attr_hashes) == 1 else None)
    predicted_elements = _predict(segment, method)
    predicted_unit = sum(predicted_elements.values(), Fraction())
    last_unit = sum(segment[-1]["elements"].values(), Fraction())
    direction = {
        "basis": "point_forecast_vs_cutoff_actual", "reference_month": cutoff_month,
        "target_month": target_month, "reference_unit_cost": _finite_float(last_unit),
        "reference_unit_cost_exact": str(last_unit),
        "direction": "up" if predicted_unit > last_unit else "down" if predicted_unit < last_unit else "flat",
        **_change(predicted_unit, last_unit),
        "trend_inference": "not_performed",
        "note": "方向仅指点预测相对本月实际值的算术比较，不是统计趋势、概率或因果结论。",
    }
    return {
        "schema_version": SCHEMA_VERSION, "status": "ok",
        "factory": factory, "product": product, "specification": specification,
        "cutoff_month": cutoff_month, "horizon": 1, "method": method,
        "training_months": [row["month"] for row in segment],
        "method_training_months": [row["month"] for row in _method_rows(segment, method)],
        "method_parameters": ({"alpha": float(SES_ALPHA), "alpha_exact": str(SES_ALPHA),
                               "initialization": "first_observation_of_contiguous_segment",
                               "parameter_selection": "fixed_in_advance_not_fitted"} if method == "ses" else {}),
        "target_months": [target_month],
        "history": [{"month": row["month"], **_cost(row["elements"])} for row in segment],
        "forecast": {"target_month": target_month, **_cost(predicted_elements)},
        "direction": direction,
        "budget_comparison": _budget_comparison(tables, scope, target_month, predicted_unit,
                                                 snapshot_hashes[0] if snapshot_hashes else None),
        "gaps": gaps,
        "backtests": backtests,
        "backtest_policy": {
            "type": "retrospective_time_split_current_revision",
            "historical_vintage_snapshot_available": False,
            "candidate_methods": candidates, "evaluated_methods": evaluated,
            "common_origins": [row["month"] for row in segment[first_target - 1:-1]],
            "common_target_months": [row["month"] for row in segment[first_target:]],
            "horizon": 1, "target_observed_by_cutoff": True,
            "error_convention": "prediction - actual",
            "mape_policy": "percent; null if any evaluated actual is zero",
        },
        "method_selection": {
            "status": "not_performed", "selected_method": method,
            "basis": "explicit_argument_or_naive_default", "unbiased_selection_estimate": False,
            "note": "Choosing a method using these same backtests would bias its reported evaluation.",
        },
        "interval": {
            "status": "insufficient_calibration" if residual_count < 5 else "not_calibrated",
            "lower": None, "upper": None, "coverage": None, "n_residuals": residual_count,
        },
        "provenance": {
            "snapshot_meta": snapshot, "source_hash": source_hash,
            "source_hashes": attr_hashes, "table_attrs": table_attrs,
            "training_source_hashes": sorted({row["source_hash"] for row in segment if row["source_hash"]}),
            "training_tables": sorted({row["table"] for row in segment}),
            "training_hash": _training_hash(segment), "hash_algorithm": "sha256",
            "method": method, "method_version": "cost-ses/1.0" if method == "ses" else METHOD_VERSION,
            "alpha_exact": str(SES_ALPHA) if method == "ses" else None, "cutoff_month": cutoff_month,
            "exact_encoding": "rational numerator/denominator, or integer; based on decimal input text",
            "input_component_closure_tolerance": "1e-12 * max(1, unit_cost, sum(elements))",
            "validation_scope": "all supplied summary rows; training and backtests use cutoff segment only",
        },
        "limitations": [
            "One-step baseline only; no interpolation or gap crossing; no fitted trend or seasonality term.",
            "Direction is point forecast minus cutoff actual, not evidence of a persistent statistical trend.",
            "Short histories cannot reliably identify trends or rank methods; nonsignificance does not prove no trend.",
            "SES uses fixed alpha=0.3 and exponentially decaying weights; it is not equal to MA3, including at alpha=0.5.",
            "Budget comparisons require the exact target YYYY-MM; missing targets are never filled from another month/year.",
            "No future volume supplied or inferred; total cost is not forecast.",
            "Candidate backtests are descriptive; the default method remains naive.",
            "No calibrated interval or coverage claim is made, even when five residuals are available.",
        ],
    }
