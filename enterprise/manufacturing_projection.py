"""Pure projection of replayed manufacturing facts into the shared M2/M3 contracts.

``project_manufacturing_analysis(runtime.analysis(...), profile)`` never loads a
CSV, configuration, database, document, or model. The caller must first replay and
authorize the runtime. This module does not authenticate caller-attested hashes.
Chinese element keys are engine enums, not factory identities or reporting units.
The return value has separate ``sources['attribution']`` / ``sources['benchmark']``
lists, because the existing validators deliberately use different evidence kinds.

Exact decimal operands are retained beside JSON-safe primary values. A primary
number is numeric only when its decimal JSON round trip is lossless (and integral
values are JavaScript-safe); otherwise it is a decimal string. Recurring ratios
retain exact rational operands and explicitly label their decimal approximation.
"""
from __future__ import annotations

from copy import deepcopy
from decimal import Context, Decimal, InvalidOperation, localcontext
from fractions import Fraction
import hashlib
import json
import math
import re

from enterprise.domain_profiles import CANONICAL_SCHEMA, profile_fingerprint, validate_domain_profile
from enterprise.numeric import format_number, format_percent

SCHEMA_VERSION = "manufacturing-projection/1"
ELEMENTS = {"material": "材料", "labor": "人工", "overhead": "制费"}
FAMILIES = {"material": "materials", "labor": "labor", "overhead": "overhead"}
IDENTITY = ("factory", "product", "specification", "month")
_NUMERIC = ("material", "labor", "overhead", "unitcost", "output", "total", "amount",
            "quantity", "unit_price", "hours", "hourly_rate", "headcount", "working_days")
_CONTEXT = Context(prec=512, Emin=-999, Emax=999)
_SAFE_INTEGER = 2 ** 53 - 1


class ManufacturingProjectionError(ValueError):
    """Malformed, conflicting, or out-of-scope canonical facts; no partial output."""


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash(value):
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _decimal(value):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise ManufacturingProjectionError("canonical observations require exact decimal strings, not floats")
    try:
        number = Decimal(value)
    except (InvalidOperation, ValueError):
        raise ManufacturingProjectionError("invalid canonical decimal") from None
    if not number.is_finite() or abs(number.adjusted()) > 600 or len(number.as_tuple().digits) > 512:
        raise ManufacturingProjectionError("nonfinite or unbounded canonical decimal")
    return number


def _number(value):
    number = _decimal(value)
    if number is None:
        return None
    if number == number.to_integral_value():
        return int(number) if abs(number) <= _SAFE_INTEGER else str(number)
    candidate = float(number)
    if math.isfinite(candidate) and Decimal(str(candidate)) == number:
        return candidate
    return str(number)


def _put(target, key, value, *, percent=False):
    number = _decimal(value)
    target[key] = _number(number)
    target[key + "_exact"] = None if number is None else str(number)
    target[key + "_display"] = None if number is None else (
        format_percent(number) if percent else format_number(number))


def _ratio(target, key, numerator=None, denominator=None, *, canonical=None, scale="1", percent=False):
    if canonical is not None:
        numerator, denominator, scale = (canonical.get(k) for k in ("numerator", "denominator", "scale"))
    n, d, s = map(_decimal, (numerator, denominator, scale))
    if n is None or d is None or d == 0:
        _put(target, key, None, percent=percent)
        target[key + "_ratio"] = {"available": False, "numerator": None if n is None else str(n),
            "denominator": None if d is None else str(d), "scale": str(s or 1), "reason": "missing_or_zero_denominator"}
        return
    rational = Fraction(n) * Fraction(s) / Fraction(d)
    remaining = rational.denominator
    for divisor in (2, 5):
        while remaining % divisor == 0:
            remaining //= divisor
    precision = 512 if remaining == 1 else 60
    with localcontext(_CONTEXT) as context:
        context.prec = precision
        value = Decimal(rational.numerator) / Decimal(rational.denominator)
    _put(target, key, value, percent=percent)
    target[key + "_ratio"] = {"available": True, "numerator": str(n), "denominator": str(d),
        "scale": str(s), "decimal_is_exact": remaining == 1,
        "decimal_precision": precision, "rounding": "none" if remaining == 1 else "half_even_decimal_approximation"}
    if canonical is not None:
        target[key + "_ratio"]["canonical_display"] = canonical.get("value")


def _delta(before, after):
    return None if before is None or after is None else _decimal(after) - _decimal(before)


def _checked_scope(row, scope, factories, months):
    if row is None:
        return
    if not isinstance(row, dict) or any(row.get(k) != scope[k] for k in ("product", "specification")):
        raise ManufacturingProjectionError("row product/specification outside selected scope")
    if row.get("factory") not in factories or row.get("month") not in months:
        raise ManufacturingProjectionError("row factory/period outside selected scope")


def _record(row, family, fields=None):
    """An original row ordinal is never mislabelled as a physical CSV line."""
    provenance = row.get("provenance") or row.get("source") or {}
    for key in ("source_sha256", "source_row_sha256", "adapter_sha256"):
        if not re.fullmatch(r"[0-9a-f]{64}", str(provenance.get(key, ""))):
            raise ManufacturingProjectionError("canonical row missing explicit " + key)
    if not isinstance(provenance.get("source_id"), str) or not provenance["source_id"]:
        raise ManufacturingProjectionError("canonical row missing source ID")
    ordinal = provenance.get("row_number")
    if type(ordinal) is not int or ordinal < 1:
        raise ManufacturingProjectionError("canonical row missing positive source record ordinal")
    selected = fields or [k for k in _NUMERIC if k in row]
    canonical_fields = {k: row[k] for k in selected if k in row}
    raw_fields = provenance.get("source_fields", provenance.get("fields"))
    columns = provenance.get("source_columns", provenance.get("columns"))
    column_mapping = provenance.get("column_mapping", provenance.get("columns_mapping", {}))
    if isinstance(raw_fields, dict):
        raw_fields = deepcopy(raw_fields)
        if isinstance(column_mapping, dict) and column_mapping:
            wanted = {column_mapping.get(k, k) for k in (*IDENTITY, *selected)}
            raw_fields = {k: v for k, v in raw_fields.items() if k in wanted}
        columns = [key for key in columns if key in raw_fields] if isinstance(columns, (list, tuple)) else sorted(raw_fields)
        if set(columns) != set(raw_fields):
            raise ManufacturingProjectionError("source fields disagree with original column metadata")
    elif isinstance(columns, (list, tuple)) and isinstance(provenance.get("source_cells"), (list, tuple)):
        cells = provenance["source_cells"]
        if len(columns) != len(cells):
            raise ManufacturingProjectionError("source columns/cells width mismatch")
        raw_fields = {k: (v.get("text") if isinstance(v, dict) else v) for k, v in zip(columns, cells)}
    else:
        raw_fields = None
        columns = list(canonical_fields)
    return {"table": family, "file": provenance["source_id"], "source_id": provenance["source_id"],
        "row": ordinal, "row_number": ordinal, "record_number": ordinal, "line": None,
        "sha256": provenance["source_sha256"], "source_sha256": provenance["source_sha256"],
        "source_row_sha256": provenance["source_row_sha256"], "adapter_sha256": provenance["adapter_sha256"],
        "key": {k: row[k] for k in (*IDENTITY, "material_id", "category") if k in row},
        "columns": list(columns), "fields": raw_fields if raw_fields is not None else canonical_fields,
        "canonical_fields": canonical_fields,
        "columns_basis": "original_source_columns" if raw_fields is not None else "canonical_columns_only_original_mapping_not_supplied",
        "note": "源数据记录序号（不含表头），不是推断的文件物理行；摘要真实性由上游仓库验证"}


class _Evidence:
    def __init__(self, kind, scope):
        self.kind, self.scope, self.rows = kind, scope, {}

    def add(self, purpose, elements, records, text):
        records = [deepcopy(record) for record in records if record is not None]
        if not records:
            raise ManufacturingProjectionError("accounting evidence requires actual source rows")
        records.sort(key=_json)
        membership = [key for key in ELEMENTS.values() if key in elements]
        ident = "F" + _hash({"purpose": purpose, "elements": membership, "records": records})[:40]
        source = records[0] if len(records) == 1 else {"records": records}
        row = {"id": ident, "kind": self.kind, "evidence_role": self.kind, "support_status": "eligible",
            "elements": membership, "text": text, "source": source,
            "scope": {"product": self.scope["product"], "specification": self.scope["specification"],
                "periods": sorted({record["key"]["month"] for record in records}),
                "factories": sorted({record["key"]["factory"] for record in records})},
            "claim_boundary": "源记录支持核算观察或可复算恒等分解，不证明经营根因或个人工资率"}
        if ident in self.rows and self.rows[ident] != row:
            raise ManufacturingProjectionError("conflicting evidence identity")
        self.rows[ident] = row
        return ident

    def export(self):
        return [deepcopy(self.rows[key]) for key in sorted(self.rows)]


def _summary(row, evidence, measurement, family="actual"):
    if row is None:
        return None
    result = {k: row[k] for k in IDENTITY}
    for key, field in (("volume", "output"), ("unit_cost", "unitcost"), ("total_cost", "total")):
        _put(result, key, row[field])
    result["elements"] = {label: _number(row[key]) for key, label in ELEMENTS.items()}
    result["source"] = _record(row, family)
    result["evidence_id"] = evidence.add("summary", list(ELEMENTS.values()), [result["source"]],
        f"{row['factory']} {row['month']} {row['product']}产量{row['output']}{measurement['quantity_unit']}，"
        f"单位成本{row['unitcost']}{measurement['unit_cost_unit']}，总成本{row['total']}{measurement['currency']}。")
    return result


def _temporal_detail(pair, key, evidence, measurement, cost_records):
    before, after = pair.get("previous"), pair.get("current")
    result = {"id": pair["id"], "name": pair["name"], "status": "complete" if before and after else
        "missing_before" if before is None else "missing_after",
        "unit_semantics": "实际归集金额/产出单位，不是采购单价" if key == "material" else "实际归集费用/产出单位"}
    for side, row in (("before", before), ("after", after)):
        _put(result, "unit_" + side, row.get("unitcost") if row else None)
        _put(result, "amount_" + side, row.get("amount") if row else None)
        result["source_" + side] = _record(row, FAMILIES[key]) if row else None
    _put(result, "unit_delta", _delta(before.get("unitcost") if before else None, after.get("unitcost") if after else None))
    bridge = pair.get("accounting_bridge") or {}
    for field in ("amount_delta", "volume_effect", "unit_effect"):
        _put(result, field, bridge.get(field) if bridge.get("available") else None)
    result["evidence_id"] = evidence.add("temporal_detail:" + pair["id"], [ELEMENTS[key]],
        [result["source_before"], result["source_after"], *cost_records],
        f"{pair['name']}所选期间归集明细及同厂产出；单位费用是{measurement['unit_cost_unit']}，金额是{measurement['currency']}，缺期不补零。")
    if key == "material":
        physical = pair.get("observed_quantity_price_bridge") or {"available": False, "reason": "canonical_bridge_not_provided"}
        observed = all(row and row.get("quantity_basis") == "source_observed" and row.get("price_basis") == "source_observed"
            and row.get("quantity") is not None and row.get("unit_price") is not None for row in (before, after))
        if physical.get("available") and not observed:
            raise ManufacturingProjectionError("physical material bridge without source-observed quantities/prices")
        result["observed_quantity_price_bridge"] = deepcopy(physical)
        result["physical_observations"] = {side: {field: row.get(field) for field in
            ("quantity", "quantity_unit", "unit_price", "quantity_basis", "price_basis")} if row else None
            for side, row in (("previous", before), ("current", after))}
        for side in ("current", "previous"):
            field = side + "_observed_quantity_per_reporting_unit"
            if field in pair:
                result[field] = deepcopy(pair[field])
    return result


def _labor(pair, before_cost, current_cost, evidence):
    """Reuse the shared aggregate H x (L/H) bridge, not an employee pay model."""
    from attribution_facts import labor_factor_bridge
    before, after = pair.get("previous"), pair.get("current")
    result = labor_factor_bridge(before.get("amount") if before else None,
        after.get("amount") if after else None, before.get("hours") if before else None,
        after.get("hours") if after else None, before_cost.get("output") if before_cost else None, current_cost["output"])
    for key, value in result.get("exact", {}).items():
        _put(result, key, value)
    result["quantity_unit"] = current_cost["unit"]
    result["basis"] = "已提供总工时与归集人工费用的核算比率，不是个人时薪或岗位效率"
    result["unit_formula"] = "hours_per_reporting_unit=(H1/Q1-H0/Q0)*(L0/H0); rate=(H1/Q1)*(L1/H1-L0/H0)"
    # The historical key is an engine alias; never a claim that output is boxes.
    for stem in ("before", "after", "change_pct"):
        for suffix in ("", "_exact", "_display"):
            old = "hours_per_box_" + stem + suffix
            if old in result:
                result["hours_per_unit_" + stem + suffix] = result[old]
    result["observed_hours_rate_bridge"] = deepcopy(pair.get("observed_hours_rate_bridge", {"available": False}))
    records = [_record(row, "labor") for row in (before, after) if row]
    if result.get("unit_available"):
        records.extend(_record(row, "actual", ["output", "labor"]) for row in (before_cost, current_cost) if row)
    if records:
        result["evidence_id"] = evidence.add("labor_factors", ["人工"], records,
            "人工费用、实际提供工时及其归集比率；缺少正工时则工时分解不可用，不以人数或工作天数推算工时。")
    if result.get("available"):
        for side, row in (("before", before), ("after", after)):
            _ratio(result, "cost_per_hour_" + side, row["amount"], row["hours"])
            _ratio(result, "hours_per_unit_" + side, row["hours"], (before_cost if side == "before" else current_cost)["output"])
        result["ratio_precision"] = "shared_labor_factor_bridge_decimal_60; exact operands remain in source rows"
    return result


def _attribution(facts, profile, evidence, common):
    period, current = facts["period"], facts["current"]
    before = period.get("baseline") if period.get("available") else None
    result = {"available": bool(period.get("available")), "reason": period.get("reason"),
        "product": common["product"], "specification": common["specification"], "month": common["month"],
        "previous_month": facts["scope"].get("previous_month"), "current": _summary(current, evidence, facts["measurement"]),
        "previous": _summary(before, evidence, facts["measurement"]), "elements": {}, "alerts": [], "warnings": []}
    _put(result, "amount_delta", period.get("amount_delta"))
    _put(result, "unit_cost_change", _delta(before["unitcost"] if before else None, current["unitcost"]))
    projections = {}
    for key, label in ELEMENTS.items():
        canonical = period.get("elements", {}).get(key, {})
        row = {"element": label, "label": profile["labels"][key], "detail": []}
        for field in ("unit_before", "unit_after", "amount_before", "amount_after", "amount_delta", "volume_effect", "unit_effect"):
            value = canonical.get(field)
            if field == "unit_after":
                value = current[key]
            elif field == "amount_after":
                value = current["amounts"][key]
            _put(row, field, value)
        _put(row, "unit_delta", _delta(canonical.get("unit_before"), current[key]))
        _ratio(row, "mom_pct", canonical=canonical.get("unit_change_pct"), percent=True)
        _ratio(row, "contribution", canonical=canonical.get("contribution_pct"), percent=True)
        records = [_record(item, "actual", [key, "output", "total", "unitcost"]) for item in (before, current) if item]
        row["evidence_ids"] = [evidence.add("temporal_element:" + key, [label], records,
            f"{profile['labels'][key]}所选期间单位费用及产量/单位成本会计桥接；不是已证实的经营原因。")]
        if key == "labor":
            pair = facts["details"].get("labor") or {}
            row["labor_factors"] = _labor(pair, before, current, evidence)
            detail = {"id": "labor", "name": profile["labels"]["labor"],
                "status": "complete" if pair.get("previous") and pair.get("current") else "missing_before",
                "unit_semantics": "归集人工费用/实际产出，不是个人时薪", "metrics": {}}
            for field in ("unit_before", "unit_after", "unit_delta", "amount_before", "amount_after", "amount_delta", "volume_effect", "unit_effect"):
                _put(detail, field, row[field + "_exact"])
            for side, original in (("before", pair.get("previous")), ("after", pair.get("current"))):
                detail["source_" + side] = _record(original, "labor") if original else None
                for field in ("hours", "headcount", "working_days"):
                    _put(detail["metrics"], field + "_" + side, original.get(field) if original else None)
            detail["evidence_id"] = evidence.add("temporal_detail:labor", [label],
                [detail["source_before"], detail["source_after"], *records],
                "人工归集金额与实际产出桥接；只在源记录明确提供时显示工时、人数、工作天数。")
            row["detail"] = [detail]
        else:
            row["detail"] = [_temporal_detail(pair, key, evidence, facts["measurement"], records) for pair in facts["details"].get(FAMILIES[key], [])]
            row["detail"].sort(key=lambda item: (-(abs(_decimal(item["unit_effect_exact"])) if item["unit_effect_exact"] is not None else Decimal(-1)), item["id"]))
        volume, unit = (_decimal(row[field + "_exact"]) for field in ("volume_effect", "unit_effect"))
        dominant = "none" if volume is None or unit is None or volume == unit == 0 else "balanced" if abs(volume) == abs(unit) else "output" if abs(volume) > abs(unit) else "unit_cost"
        projections[label] = {"analysis_level": "detailed", "dominant_driver": dominant, "top_materials": [], "evidence": []}
        result["elements"][label] = row
    return {**deepcopy(common), "schema_version": "attribution-payload/1.0", "facts": result, "elements": projections}


def _side(row, key, summary, evidence, measurement):
    if row is None:
        return None
    result = {"name": row.get("name", ELEMENTS[key]), "factory": row["factory"], "metrics": {},
        "source": _record(row, FAMILIES[key]), "volume_source_id": summary["evidence_id"]}
    unit = row.get("unitcost") if key != "labor" else summary["elements"]["人工"]
    _put(result, "unit_cost", str(unit))
    _put(result, "amount", row["amount"])
    _put(result, "volume", summary["volume_exact"])
    result["evidence_id"] = evidence.add("detail:" + key, [ELEMENTS[key]], [result["source"], summary["source"]],
        f"{row['factory']}{result['name']}所选期间归集金额{row['amount']}{measurement['currency']}及同厂产出；只支持源记录实际提供的字段及明确的核算比率。")
    if key == "labor":
        for field in ("hours", "headcount", "working_days"):
            _put(result["metrics"], field, row.get(field))
        _ratio(result["metrics"], "cost_per_hour", row["amount"], row.get("hours"))
        _ratio(result["metrics"], "hours_per_box", row.get("hours"), summary["volume_exact"])
        _ratio(result["metrics"], "output_per_hour", summary["volume_exact"], row.get("hours"))
        result["metrics"]["hours_per_unit"] = result["metrics"]["hours_per_box"]
        result["metrics"]["boundary"] = "工时为源记录观察；费用/工时为归集比率，不是个人时薪"
    elif key == "material":
        result["physical_observations"] = {field: row.get(field) for field in
            ("quantity", "quantity_unit", "unit_price", "quantity_basis", "price_basis")}
    return result


def _paired(facts, key, home, peer, evidence):
    family = FAMILIES[key]
    if key == "labor":
        current = (facts["details"].get(family) or {}).get("current")
        left = {"labor": current} if current else {}
        right = {"labor": row for row in facts.get("peer_details", {}).get(family, [])}
    else:
        identity = "material_id" if key == "material" else "category"
        left = {pair["current"][identity]: pair["current"] for pair in facts["details"].get(family, []) if pair.get("current")}
        right = {row[identity]: row for row in facts.get("peer_details", {}).get(family, [])}
    rows = []
    for identity in sorted(set(left) | set(right)):
        a, b = left.get(identity), right.get(identity)
        sides = [_side(row, key, summary, evidence, facts["measurement"]) for row, summary in ((a, home), (b, peer))]
        row = {"id": identity, "name": (a or b).get("name", facts["labels"][key]), "element": ELEMENTS[key],
            "home": sides[0], "peer": sides[1], "status": "paired" if all(sides) else "missing_peer" if a else "missing_home",
            "normalization_volume": home["volume"], "normalization_source_id": home["evidence_id"],
            "evidence_ids": [side["evidence_id"] for side in sides if side],
            "gap_reason": None if all(sides) else "另一方同键明细未提供；差值为空，不推算零值"}
        gap = _decimal(sides[0]["unit_cost_exact"]) - _decimal(sides[1]["unit_cost_exact"]) if all(sides) else None
        _put(row, "unit_gap", gap)
        _put(row, "normalized_amount", gap * _decimal(home["volume_exact"]) if gap is not None else None)
        rows.append(row)
    rows.sort(key=lambda row: (-(abs(_decimal(row["normalized_amount_exact"])) if row["normalized_amount_exact"] is not None else Decimal(-1)), row["id"]))
    coverage = {}
    for side, records, summary in (("home", left, home), ("peer", right, peer)):
        covered = sum((_decimal(row[side]["unit_cost_exact"]) for row in rows if row[side]), Decimal(0))
        expected = _decimal(str(summary["elements"][ELEMENTS[key]]))
        coverage[side] = {"provided_count": len(records), "reconciled": bool(records) and covered == expected}
        _put(coverage[side], "covered_unit_cost", covered)
        _put(coverage[side], "uncovered_unit_cost", expected - covered)
    paired = sum((_decimal(row["normalized_amount_exact"]) for row in rows if row["normalized_amount_exact"] is not None), Decimal(0))
    total = _decimal(facts["benchmark"]["elements"][key]["standardized_amount_gap"])
    result = {"rows": rows, "coverage": coverage, "diagnostics": [], "paired_count": sum(row["status"] == "paired" for row in rows),
        "complete": all(side["reconciled"] for side in coverage.values()) and all(row["status"] == "paired" for row in rows),
        "scope": "同产品、规格、月份与源明细键；以本厂实际产量标准化，不证明经营根因"}
    _put(result, "paired_normalized_amount", paired)
    _put(result, "unallocated_normalized_amount", total - paired)
    return result


def _benchmark(facts, profile, evidence, common):
    benchmark = facts["benchmark"]
    home, peer = (_summary(benchmark[side], evidence, facts["measurement"]) for side in ("home", "peer"))
    result = {"available": True, "reason": None, "product": common["product"], "specification": common["specification"],
        "month": common["month"], "home_factory": home["factory"], "peer_factory": peer["factory"],
        "home": home, "peer": peer, "elements": [], "paired_drilldown": {}, "suggestions": [],
        "raw_total_gap_is_efficiency": False, "claim_boundary": benchmark.get("claim_boundary"),
        "normalization_volume": home["volume"], "normalization_source_id": home["evidence_id"]}
    for name, field in (("unit_gap", "unit_gap"), ("normalized_amount", "standardized_amount_gap"), ("raw_total_gap", "raw_total_gap")):
        _put(result, name, benchmark[field])
    result["raw_total_gap_bridge"] = deepcopy(benchmark.get("raw_total_gap_bridge", {}))
    result["standardized_output_basis"] = benchmark.get("standardized_output_basis")
    _ratio(result, "gap_pct", benchmark["unit_gap"], benchmark["peer"]["unitcost"], scale="100", percent=True)
    for key, label in ELEMENTS.items():
        canonical = benchmark["elements"][key]
        row = {"element": label, "label": profile["labels"][key]}
        for name, field in (("home_unit_cost", "home_unit"), ("peer_unit_cost", "peer_unit"), ("unit_gap", "unit_gap"), ("normalized_amount", "standardized_amount_gap")):
            _put(row, name, canonical[field])
        for name, ratio in (("gap_pct", canonical["difference_pct"]), ("contribution_pct", canonical["contribution_pct"]),
                            ("home_share_pct", benchmark["home"]["element_shares"][key]), ("peer_share_pct", benchmark["peer"]["element_shares"][key])):
            _ratio(row, name, canonical=ratio, percent=True)
        records = [_record(benchmark[side], "actual", [key, "output", "unitcost"]) for side in ("home", "peer")]
        row["evidence_id"] = evidence.add("benchmark_element:" + key, [label], records,
            f"{profile['labels'][key]}本厂单位费用减对标厂单位费用，以本厂产量标准化；差额不是实际节约成果。")
        result["elements"].append(row)
        result["paired_drilldown"][label] = _paired(facts, key, home, peer, evidence)
    _put(result, "reconciliation_difference", _decimal(result["normalized_amount_exact"]) - sum((_decimal(row["normalized_amount_exact"]) for row in result["elements"]), Decimal(0)))
    if result["reconciliation_difference"] != 0:
        raise ManufacturingProjectionError("benchmark canonical elements do not reconcile")
    result["source_ids"] = [summary["evidence_id"] for summary in (home, peer)]
    return {**deepcopy(common), "schema_version": "benchmark-explanations/1.0", "facts": result}


def _charts(attribution, benchmark, config, fact_hash):
    labels = [config["element_labels"][label] for label in ELEMENTS.values()]
    def chart(title, unit, series, exact):
        values = [float(value) for row in series for value in row["data"] if value is not None]
        axis = {"type": "value", "name": unit, "scale": False, "axisLine": {"onZero": True}}
        if values and min(values) < 0:
            axis["max"] = max(0, max(values))
        else:
            axis["min"] = 0
        return {"source_fact_sha256": fact_hash, "data_exact": exact, "option": {
            "title": {"text": title}, "tooltip": {"trigger": "axis"}, "legend": {},
            "grid": {"containLabel": True, "left": 24, "right": 24, "bottom": 24},
            "xAxis": {"type": "category", "data": labels}, "yAxis": axis, "series": series},
            "boundary": "图形数值只作显示；data_exact与源事实哈希保留核对口径，坐标包含零基线"}
    m2 = attribution["facts"]["elements"]
    m3 = benchmark["facts"]["elements"]
    temporal_series, temporal_exact = [], {}
    for field, name in (("volume_effect", "产量影响"), ("unit_effect", "单位成本影响")):
        temporal_exact[field] = [m2[label][field + "_exact"] for label in ELEMENTS.values()]
        temporal_series.append({"name": name, "type": "bar", "data": [None if value is None else float(value) for value in temporal_exact[field]]})
    comparison_series, comparison_exact = [], {}
    for field, name in (("home_unit_cost", config["home_label"]), ("peer_unit_cost", config["peer_label"])):
        comparison_exact[field] = [row[field + "_exact"] for row in m3]
        comparison_series.append({"name": name, "type": "bar", "data": [float(value) for value in comparison_exact[field]]})
    return {"attribution": chart("成本金额变动分解", config["amount_unit"], temporal_series, temporal_exact),
            "benchmark": chart("同口径单位成本对比", config["unit_cost_unit"], comparison_series, comparison_exact)}


def project_manufacturing_analysis(canonical_facts, profile):
    """Project one authorized analysis selection; return detached JSON-only objects.

    All supplied selected-period/detail observations are retained. Original file
    tables and unrelated future rows are neither requested nor copied. Profile
    vocabulary remains explicit reference semantics, never document/actual proof.
    ``sources`` is a mapping keyed by ``attribution`` and ``benchmark``.
    """
    profile = validate_domain_profile(profile)
    if profile["schema_version"] != CANONICAL_SCHEMA:
        raise ManufacturingProjectionError("projection requires manufacturing-domain/2")
    facts = deepcopy(canonical_facts)
    if not isinstance(facts, dict) or facts.get("schema_version") != "manufacturing-facts/1":
        raise ManufacturingProjectionError("projection requires replayed manufacturing-facts/1")
    if facts.get("provenance", {}).get("profile_sha256") != profile_fingerprint(profile):
        raise ManufacturingProjectionError("canonical facts/profile fingerprint mismatch")
    scope, measurement = facts["scope"], facts["measurement"]
    product = next((item for item in profile["products"] if (item["name"], item["specification"]) ==
        (scope["product"], scope["specification"])), None)
    if product is None or scope["home_factory"] != profile["factories"]["home"] or scope["peer_factory"] != profile["factories"]["peer"]:
        raise ManufacturingProjectionError("canonical scope/profile identities mismatch")
    expected_measurement = {"currency": profile["currency"], "quantity_unit": product["reporting_unit"],
        "unit_cost_unit": profile["currency"] + "/" + product["reporting_unit"]}
    if measurement != expected_measurement or facts.get("labels") != profile["labels"]:
        raise ManufacturingProjectionError("canonical measurement/labels mismatch")
    months = {scope["month"], scope.get("previous_month")} - {None}
    factories = {scope["home_factory"], scope["peer_factory"]}
    # Validate every row we will project, rather than trusting caller-added details.
    candidates = [facts["current"], facts["period"].get("baseline"), facts["benchmark"]["home"], facts["benchmark"]["peer"]]
    role_rows = [(facts["current"], scope["home_factory"], scope["month"]),
        (facts["period"].get("baseline"), scope["home_factory"], scope.get("previous_month")),
        (facts["benchmark"]["home"], scope["home_factory"], scope["month"]),
        (facts["benchmark"]["peer"], scope["peer_factory"], scope["month"])]
    for family in FAMILIES.values():
        pairs = [facts["details"][family]] if family == "labor" else facts["details"][family]
        candidates.extend(row for pair in pairs for row in (pair.get("current"), pair.get("previous")))
        peers = facts.get("peer_details", {}).get(family, [])
        candidates.extend(peers)
        for pair in pairs:
            role_rows.extend(((pair.get("current"), scope["home_factory"], scope["month"]),
                (pair.get("previous"), scope["home_factory"], scope.get("previous_month"))))
        role_rows.extend((row, scope["peer_factory"], scope["month"]) for row in peers)
    for row in candidates:
        _checked_scope(row, scope, factories, months)
    for row, factory, month in role_rows:
        if row is not None and (row["factory"] != factory or row["month"] != month):
            raise ManufacturingProjectionError("row factory/period disagrees with canonical comparison role")
    if facts["current"] != facts["benchmark"]["home"]:
        raise ManufacturingProjectionError("benchmark/home disagrees with selected current observation")
    fact_hash = _hash(facts)
    config = {"amount_unit": measurement["currency"], "reporting_unit": measurement["quantity_unit"],
        "unit_cost_unit": measurement["unit_cost_unit"], "home_label": scope["home_factory"], "peer_label": scope["peer_factory"],
        "element_labels": {label: profile["labels"][key] for key, label in ELEMENTS.items()},
        "element_kinds": {label: label for label in ELEMENTS.values()}, "industry_category": product["category"]}
    common = {"product": scope["product"], "specification": scope["specification"], "month": scope["month"],
        "domain_config": deepcopy(profile), "measurement": deepcopy(measurement), "narrative_config": deepcopy(config),
        "source_fact_sha256": fact_hash, "data_classification": facts.get("data_classification"),
        "available_record_names": [], "limitations": deepcopy(facts.get("limitations", [])),
        "projection_schema": SCHEMA_VERSION}
    m2_sources, m3_sources = _Evidence("accounting_fact", scope), _Evidence("data_fact", scope)
    with localcontext(_CONTEXT):
        attribution = _attribution(facts, profile, m2_sources, common)
        benchmark = _benchmark(facts, profile, m3_sources, common)
    result = {"attribution_payload": attribution, "benchmark_payload": benchmark,
        "sources": {"attribution": m2_sources.export(), "benchmark": m3_sources.export()},
        "narrative_config": config, "charts": _charts(attribution, benchmark, config, fact_hash)}
    # Reject accidental Decimal/nonfinite output and detach all references.
    return json.loads(_json(result))
