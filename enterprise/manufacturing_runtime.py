"""Industry-neutral, deterministic manufacturing facts with replayable provenance.

This module performs no I/O, model calls, permission grants or task dispatch. The
application supplies a complete, explicitly scoped, authorized data bundle. Domain
and adapter hashes bind semantics; source digests remain caller-attested until a
repository independently verifies original bytes. Every amount remains an exact
Decimal string. Ratios alone have explicitly labelled, rounded display values and
retain their exact numerator/denominator. No pharmacy column names or packaging
conversions exist in this analysis path.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Context, Decimal, DecimalException, Inexact, ROUND_HALF_EVEN, localcontext
import hashlib
import re

from enterprise.domain_profiles import (
    CANONICAL_SCHEMA, DomainProfileError, profile_fingerprint, validate_domain_profile,
)
from enterprise.manufacturing_adapter import (
    AdapterConfig, CANONICAL_FIELDS, CANONICAL_SCHEMA_VERSION, ManufacturingAdapterError, _decimal, _hash,
    _json, _object, _text, adapt_rows, parse_adapter_config, validate_adapter_config,
)
import json

RUNTIME_SCHEMA = "manufacturing-runtime/1"
FACT_SCHEMA = "manufacturing-facts/1"
ELEMENTS = ("material", "labor", "overhead")
TABLE_FAMILIES = ("actual", "budget", "materials", "labor", "overhead")
IDENTITY = ("factory", "product", "specification", "month")
DETAIL_COLUMNS = {
    "materials": (*IDENTITY, "material_id", "unitcost", "amount", "currency", "reporting_unit",
                  "quantity", "quantity_unit", "unit_price"),
    "labor": (*IDENTITY, "amount", "currency", "reporting_unit", "hours", "headcount", "working_days", "hourly_rate"),
    "overhead": (*IDENTITY, "category", "unitcost", "amount", "currency", "reporting_unit"),
}
_SOURCE_FIELDS = {"columns", "rows", "source_id", "source_sha256"}
_MAX_SOURCE_ROWS = 100_000
_MAX_BUNDLE_CELLS = 2_000_000
_EXACT = Context(prec=512, Emin=-999, Emax=999)
_EXACT.traps[Inexact] = True


class ManufacturingRuntimeError(ManufacturingAdapterError):
    """Invalid complete bundle, detail closure, scope or integrity; no partial facts."""


def _fail(message):
    raise ManufacturingRuntimeError(message)


def _month(value):
    if type(value) is not str or not re.fullmatch(r"[0-9]{4}-(0[1-9]|1[0-2])", value) or value[:4] == "0000":
        _fail("month must be exact YYYY-MM, year 0001..9999")
    return int(value[:4]) * 12 + int(value[5:]) - 1


def _periods(values):
    if type(values) not in (list, tuple) or not values or len(values) > 1200:
        _fail("periods must be explicit bounded nonempty months")
    months = [_month(value) for value in values]
    if months != sorted(set(months)) or any(b != a + 1 for a, b in zip(months, months[1:])):
        _fail("periods must be ordered, unique and contiguous; no inferred gap filling")
    return list(values)


def _table(value, family):
    _object(value, _SOURCE_FIELDS, "source table " + family)
    _text(value["source_id"], "source_id")
    if re.search(r'(?i)([a-z][a-z0-9+.-]{1,31}://|javascript:|data:|<\|[^>]+\|>|\[/?INST\]|</?(?:system|assistant|developer)>|__import__|<script)', value["source_id"]):
        _fail("source_id contains URL, executable or role delimiter injection")
    if type(value["source_sha256"]) is not str or not re.fullmatch(r"[0-9a-f]{64}", value["source_sha256"]):
        _fail("source_sha256 must be explicit lowercase SHA-256")
    columns, rows = value["columns"], value["rows"]
    if type(columns) not in (list, tuple) or not columns or len(columns) > 30:
        _fail("source columns must be bounded and nonempty")
    for column in columns:
        _text(column, "source column")
    if len(set(columns)) != len(columns):
        _fail("duplicate source column")
    if type(rows) not in (list, tuple) or not rows or len(rows) > _MAX_SOURCE_ROWS:
        _fail("source rows must be bounded and nonempty")
    for row in rows:
        if type(row) not in (list, tuple) or len(row) != len(columns):
            _fail("source row width mismatch")
        # A CSV transport is text. No floats/booleans/missing cells silently become
        # strings; preserving exact source text is part of the repository contract.
        if any(type(cell) is not str or len(cell) > 1000 for cell in row):
            _fail("runtime source cells must be bounded exact CSV strings")
        if any(re.search(r'(?i)(<\|[^>]+\|>|\[/?INST\]|</?(?:system|assistant|developer)>|__import__|<script)', cell) for cell in row):
            _fail("source cells contain executable or role delimiter injection")
    return {**value, "columns": list(columns), "rows": [list(row) for row in rows]}


def _optional_number(value, name, *, positive=False):
    return None if value == "" else _decimal(value, name, positive=positive)


def _detail_rows(family, source, actual, profile, adapter_sha):
    if set(source["columns"]) != set(DETAIL_COLUMNS[family]):
        _fail(f"{family}: missing or unknown detail columns")
    result, seen, grouped = [], set(), {}
    products = {(p["name"], p["specification"]): p for p in profile["products"]}
    material_defs = {m["id"]: m for m in profile["materials"]}
    element = {"materials": "material", "labor": "labor", "overhead": "overhead"}[family]
    for ordinal, cells in enumerate(source["rows"], 1):
        row = dict(zip(source["columns"], cells))
        identity = tuple(_text(row[k], k) for k in IDENTITY)
        _month(row["month"])
        base = actual.get(identity)
        if base is None:
            _fail(f"{family}: detail identity missing from actual summary")
        if row["currency"] != base.currency or row["reporting_unit"] != base.unit:
            _fail(f"{family}: detail currency/unit must equal canonical target; no inferred conversion")
        amount = _decimal(row["amount"], family + ".amount")
        normalized = {k: row[k] for k in IDENTITY}
        normalized.update(amount=str(amount), currency=base.currency, reporting_unit=base.unit)
        key = identity
        if family != "labor":
            unitcost = _decimal(row["unitcost"], family + ".unitcost")
            if unitcost * base.output != amount:
                _fail(f"{family}: detail unitcost times actual output does not close exactly to amount")
            normalized["unitcost"] = str(unitcost)
        if family == "materials":
            material_id = _text(row["material_id"], "material_id")
            product = products[(base.product, base.specification)]
            if material_id not in {x["material_id"] for x in product["bom"]}:
                _fail("material is not in configured product BOM")
            material = material_defs[material_id]
            quantity = _optional_number(row["quantity"], "actual material quantity")
            price = _optional_number(row["unit_price"], "actual material price")
            if (quantity is None) != (row["quantity_unit"] == ""):
                _fail("actual material quantity and unit must be supplied together")
            if quantity is not None and row["quantity_unit"] != material["unit"]:
                _fail("actual material quantity unit incompatible with explicit material definition")
            if price is not None and (quantity is None or price * quantity != amount):
                _fail("provided actual material quantity and price must close exactly to amount")
            normalized.update(material_id=material_id, name=material["name"],
                quantity=None if quantity is None else str(quantity),
                quantity_unit=None if quantity is None else row["quantity_unit"],
                unit_price=None if price is None else str(price),
                quantity_basis="source_observed" if quantity is not None else "not_provided",
                price_basis="source_observed" if price is not None else "not_provided")
            key += (material_id,)
        elif family == "overhead":
            category = _text(row["category"], "overhead category")
            normalized.update(category=category, name=category)
            key += (category,)
        else:
            for field in ("hours", "headcount", "working_days", "hourly_rate"):
                number = _optional_number(row[field], "labor." + field)
                if field == "headcount" and number is not None and number != number.to_integral_value():
                    _fail("labor.headcount must be an integer observation")
                if field == "working_days" and number is not None:
                    from calendar import monthrange
                    if number > monthrange(int(row["month"][:4]), int(row["month"][5:]))[1]:
                        _fail("labor.working_days exceeds calendar period")
                normalized[field] = None if number is None else str(number)
            if normalized["hourly_rate"] is not None:
                if normalized["hours"] is None or Decimal(normalized["hourly_rate"]) * Decimal(normalized["hours"]) != amount:
                    _fail("provided labor hours and hourly_rate must close exactly to amount")
        if key in seen:
            _fail(f"{family}: duplicate detail identity")
        seen.add(key)
        grouped.setdefault(identity, []).append(normalized)
        normalized["provenance"] = {"source_id": source["source_id"], "source_sha256": source["source_sha256"],
            "row_number": ordinal, "source_row_sha256": _hash({"columns": source["columns"], "cells": cells}),
            "adapter_sha256": adapter_sha, "source_cells": list(cells)}
        result.append(normalized)
    if set(grouped) != set(actual):
        _fail(f"{family}: incomplete detail coverage; missing observations cannot become zero")
    for identity, base in actual.items():
        details = grouped[identity]
        if sum((Decimal(row["amount"]) for row in details), Decimal(0)) != getattr(base, element) * base.output:
            _fail(f"{family}: detail totals do not close exactly to actual component amount")
        if family == "materials":
            expected = {r["material_id"] for r in products[(base.product, base.specification)]["bom"]}
            if {row["material_id"] for row in details} != expected:
                _fail("materials: incomplete explicit BOM coverage; zero usage requires an explicit zero row")
    return result


def validate_binding(profile, adapter_config):
    """Preflight operator configuration only; never grants factory/product access.

    Returns detached normalized ``(profile, adapter_config)`` dictionaries.
    """
    try:
        checked_profile = validate_domain_profile(profile)
        if checked_profile["schema_version"] != CANONICAL_SCHEMA:
            _fail("canonical runtime requires manufacturing-domain/2; legacy pharma is not silently relabelled")
        checked_adapter = parse_adapter_config(adapter_config.payload_json) if type(adapter_config) is AdapterConfig else validate_adapter_config(adapter_config)
        cfg = checked_adapter.to_dict()
        if cfg["schema_version"] != CANONICAL_SCHEMA_VERSION:
            _fail("canonical domain/2 requires adapter/2; legacy adapter cannot downgrade canonical validation")
        if cfg["status"] != "active" or cfg["industry"] != checked_profile["industry"]:
            _fail("adapter must be active and match configured industry")
        if set(cfg["factories"]) != set(checked_profile["factories"].values()):
            _fail("adapter factories must exactly match domain home/peer identities")
        if cfg["currency"]["target"] != checked_profile["currency"]:
            _fail("adapter currency differs from domain reporting currency")
        configured = {(p["name"], p["specification"], p["reporting_unit"]) for p in checked_profile["products"]}
        bindings = {(p["product"], p["specification"], p["unit"]) for p in cfg["product_units"]}
        if configured != bindings:
            _fail("adapter product/unit bindings differ from domain definitions")
        return checked_profile, cfg
    except (ManufacturingAdapterError, DomainProfileError, TypeError, KeyError, RecursionError) as exc:
        if isinstance(exc, ManufacturingRuntimeError):
            raise
        raise ManufacturingRuntimeError(str(exc)) from None


def build_manufacturing_runtime(profile, adapter_config, *, tables, periods):
    """Validate one complete bundle atomically; output is immutable and replayable.

    Complete-bundle policy: each configured product/specification, both configured
    factory identities and every declared contiguous month must exist in actual
    AND budget; all three detail families must cover every actual row. Partial
    production imports require repository merge with a previously confirmed bundle
    before this function is called. Neither absent rows nor missing values are
    invented. Config identities are not security permissions.
    """
    try:
        checked_profile, cfg = validate_binding(profile, adapter_config)
        checked_adapter = validate_adapter_config(cfg)
        months = _periods(periods)
        _object(tables, TABLE_FAMILIES, "runtime tables")
        sources = {family: _table(tables[family], family) for family in TABLE_FAMILIES}
        if sum(len(t["columns"]) * len(t["rows"]) for t in sources.values()) > _MAX_BUNDLE_CELLS:
            _fail("complete bundle exceeds bounded cell count")
        if len({t["source_id"] for t in sources.values()}) != len(sources):
            _fail("source IDs must uniquely identify each table family")
        if len(checked_profile["products"]) * len(checked_profile["factories"]) * len(months) > _MAX_SOURCE_ROWS:
            _fail("declared complete scope exceeds maximum summary rows")
        snapshots = {family: adapt_rows(checked_adapter, **sources[family]) for family in ("actual", "budget")}
        expected = {(factory, p["name"], p["specification"], month)
                    for factory in checked_profile["factories"].values()
                    for p in checked_profile["products"] for month in months}
        for family, snapshot in snapshots.items():
            if {row.identity for row in snapshot.rows} != expected:
                _fail(f"{family}: incomplete or out-of-scope product/factory/period coverage")
        actual = {row.identity: row for row in snapshots["actual"].rows}
        with localcontext(_EXACT):
            details = {family: _detail_rows(family, sources[family], actual, checked_profile, checked_adapter.fingerprint)
                       for family in ("materials", "labor", "overhead")}
        payload = {"schema_version": RUNTIME_SCHEMA, "profile": checked_profile,
            "profile_sha256": profile_fingerprint(checked_profile), "adapter": cfg,
            "adapter_sha256": checked_adapter.fingerprint, "periods": months,
            "source_tables": sources,
            "actual": snapshots["actual"].to_dict(), "budget": snapshots["budget"].to_dict(),
            **details,
            "policy": {"coverage": "complete_product_factory_period_bundle",
                "arithmetic": "exact_decimal_no_rounding_or_imputation",
                "source_digest_authenticity": "caller_attested_requires_repository_byte_verification",
                "configuration_is_authorization": False,
                "bom_is_actual_quantity": False}}
        return ManufacturingRuntime(_json(payload))
    except ManufacturingRuntimeError:
        raise
    except (ManufacturingAdapterError, DomainProfileError, DecimalException, TypeError, KeyError, RecursionError) as exc:
        raise ManufacturingRuntimeError(str(exc)) from None


@dataclass(frozen=True, slots=True)
class ManufacturingRuntime:
    """Frozen source plus verified projections. Exports are detached JSON copies."""
    payload_json: str

    @property
    def fingerprint(self):
        return hashlib.sha256(self.payload_json.encode("utf-8")).hexdigest()

    def to_dict(self):
        if type(self.payload_json) is not str:
            _fail("runtime payload must be canonical JSON text")
        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    _fail("duplicate JSON key in runtime payload")
                result[key] = value
            return result
        def reject(_):
            _fail("nonfinite JSON constant in runtime payload")
        try:
            return json.loads(self.payload_json, object_pairs_hook=unique, parse_constant=reject)
        except (ValueError, RecursionError) as exc:
            if isinstance(exc, ManufacturingRuntimeError):
                raise
            _fail("invalid runtime JSON payload")

    def verify(self, *, profile=None, adapter_config=None):
        return restore_manufacturing_runtime(self.to_dict(), profile=profile, adapter_config=adapter_config)

    def analysis(self, *, product, specification, month, previous_month=None,
                 profile=None, adapter_config=None):
        runtime = self.verify(profile=profile, adapter_config=adapter_config)
        return _analysis(runtime, product=product, specification=specification,
                         month=month, previous_month=previous_month)


def restore_manufacturing_runtime(value, *, profile=None, adapter_config=None):
    """Replay original cells and compare every derived field; fail on config drift.

    A repository must additionally compare fingerprint to its immutable revision
    digest. Unkeyed hashes do not authenticate a caller able to replace all data.
    """
    expected_fields = {"schema_version", "profile", "profile_sha256", "adapter", "adapter_sha256",
        "periods", "source_tables", "actual", "budget", "materials", "labor", "overhead", "policy"}
    try:
        _object(value, expected_fields, "runtime snapshot")
        if value["schema_version"] != RUNTIME_SCHEMA:
            _fail("unsupported runtime schema")
        if profile is not None and profile_fingerprint(profile) != value["profile_sha256"]:
            _fail("domain configuration changed after validation")
        if adapter_config is not None:
            adapter = parse_adapter_config(adapter_config.payload_json) if type(adapter_config) is AdapterConfig else validate_adapter_config(adapter_config)
            if adapter.fingerprint != value["adapter_sha256"]:
                _fail("adapter configuration changed after validation")
        rebuilt = build_manufacturing_runtime(value["profile"], value["adapter"],
            tables=value["source_tables"], periods=value["periods"])
        if _json(rebuilt.to_dict()) != _json(value):
            _fail("runtime integrity mismatch: values, configuration or provenance changed")
        return rebuilt
    except (ValueError, TypeError, KeyError, RecursionError) as exc:
        if isinstance(exc, ManufacturingRuntimeError):
            raise
        raise ManufacturingRuntimeError(str(exc)) from None


def _ratio(numerator, denominator, *, scale="1", unit="ratio"):
    """No terminating-decimal pretense: exact operands accompany rounded display."""
    numerator, denominator, scale = Decimal(numerator), Decimal(denominator), Decimal(scale)
    if denominator == 0:
        return {"available": False, "reason": "zero_denominator", "numerator": str(numerator),
                "denominator": str(denominator), "scale": str(scale), "value": None, "unit": unit}
    with localcontext(Context(prec=512, Emin=-999, Emax=999, rounding=ROUND_HALF_EVEN)):
        display = (numerator * scale / denominator).quantize(Decimal("0.000001"))
    return {"available": True, "numerator": str(numerator), "denominator": str(denominator),
            "scale": str(scale), "value": str(display), "unit": unit,
            "rounding": "display_only_half_even_6_decimal_places"}


def _identity(row):
    return tuple(row[k] for k in IDENTITY)


def _reference(row):
    p = row["provenance"]
    return {k: p[k] for k in ("source_id", "source_sha256", "row_number", "source_row_sha256", "adapter_sha256",
                            "source_columns", "source_fields", "row_number_basis")}


def _cost(row):
    output = Decimal(row["output"])
    return {**{k: row[k] for k in CANONICAL_FIELDS}, "currency": row["currency"], "unit": row["unit"],
            "amounts": {element: str(Decimal(row[element]) * output) for element in ELEMENTS},
            "element_shares": {element: _ratio(row[element], row["unitcost"], scale="100", unit="%") for element in ELEMENTS},
            "source": _reference(row)}


def _bridge(before, after):
    """Ordered Laspeyres quantity/current-quantity unit-cost bridge, exact closure."""
    qb, qa = Decimal(before["output"]), Decimal(after["output"])
    total_delta = Decimal(after["total"]) - Decimal(before["total"])
    elements = {}
    for element in ELEMENTS:
        ub, ua = Decimal(before[element]), Decimal(after[element])
        amount_before, amount_after = ub * qb, ua * qa
        delta, volume, unit = amount_after - amount_before, ub * (qa - qb), qa * (ua - ub)
        if volume + unit != delta:
            _fail("internal temporal bridge failed exact closure")
        elements[element] = {"unit_before": str(ub), "unit_after": str(ua),
            "amount_before": str(amount_before), "amount_after": str(amount_after),
            "amount_delta": str(delta), "volume_effect": str(volume), "unit_effect": str(unit),
            "unit_change_pct": _ratio(ua - ub, ub, scale="100", unit="%"),
            "contribution_pct": _ratio(delta, total_delta, scale="100", unit="%")}
    if sum((Decimal(e["amount_delta"]) for e in elements.values()), Decimal(0)) != total_delta:
        _fail("internal component delta sum failed exact closure")
    return {"available": True, "baseline": _cost(before), "current": _cost(after),
            "output_delta": str(qa - qb), "amount_delta": str(total_delta), "elements": elements,
            "method": "baseline_unit_cost_times_output_change_plus_current_output_times_unit_cost_change",
            "causality": "accounting_identity_not_confirmed_operational_cause"}


def _benchmark(home, peer):
    qh, qp = Decimal(home["output"]), Decimal(peer["output"])
    difference = Decimal(home["unitcost"]) - Decimal(peer["unitcost"])
    elements = {}
    for element in ELEMENTS:
        h, p = Decimal(home[element]), Decimal(peer[element])
        gap = h - p
        elements[element] = {"home_unit": str(h), "peer_unit": str(p), "unit_gap": str(gap),
            "difference_pct": _ratio(gap, p, scale="100", unit="%"),
            "standardized_amount_gap": str(gap * qh),
            "contribution_pct": _ratio(gap, difference, scale="100", unit="%")}
    if sum((Decimal(e["standardized_amount_gap"]) for e in elements.values()), Decimal(0)) != difference * qh:
        _fail("internal benchmark failed exact closure")
    return {"available": True, "home": _cost(home), "peer": _cost(peer),
        "standardized_output": str(qh), "standardized_output_basis": "home_actual_output",
        "unit_gap": str(difference), "standardized_amount_gap": str(difference * qh),
        "raw_total_gap": str(Decimal(home["total"]) - Decimal(peer["total"])),
        "raw_total_gap_is_efficiency": False,
        "raw_total_gap_bridge": {"volume_effect": str(Decimal(peer["unitcost"]) * (qh - qp)),
                                 "unit_effect": str(difference * qh)},
        "elements": elements, "claim_boundary": "same_explicit_product_specification_currency_unit_period_not_proven_process_comparability"}


def _paired_details(current, previous, family, current_cost, previous_cost):
    key = {"materials": "material_id", "overhead": "category", "labor": None}[family]
    if family == "labor":
        before = previous[0] if previous else None
        after = current[0]
        result = {"current": after, "previous": before,
                  "actual_hours_basis": "source_observed" if after["hours"] is not None else "not_provided"}
        if before and all(r["hours"] is not None and r["hourly_rate"] is not None for r in (before, after)):
            hb, ha, rb, ra = (Decimal(before["hours"]), Decimal(after["hours"]),
                              Decimal(before["hourly_rate"]), Decimal(after["hourly_rate"]))
            result["observed_hours_rate_bridge"] = {"available": True,
                "hours_effect": str(rb * (ha - hb)), "rate_effect": str(ha * (ra - rb)),
                "amount_delta": str(Decimal(after["amount"]) - Decimal(before["amount"]))}
        else:
            result["observed_hours_rate_bridge"] = {"available": False, "reason": "source_hours_or_rate_not_provided_for_both_periods"}
        return result
    earlier = {row[key]: row for row in previous}
    later = {row[key]: row for row in current}
    results = []
    for identity in sorted(set(earlier) | set(later)):
        before, after = earlier.get(identity), later.get(identity)
        detail = {"id": identity, "name": (after or before)["name"], "current": after, "previous": before}
        if family == "materials":
            for role, row, cost in (("current", after, current_cost), ("previous", before, previous_cost)):
                detail[role + "_observed_quantity_per_reporting_unit"] = (
                    _ratio(row["quantity"], cost["output"], unit=row["quantity_unit"] + "/" + cost["unit"])
                    if row is not None and row["quantity"] is not None else
                    {"available": False, "reason": "actual_quantity_not_provided_no_reference_imputation"})
        if before is not None and after is not None:
            qb, qa = Decimal(previous_cost["output"]), Decimal(current_cost["output"])
            ub, ua = Decimal(before["unitcost"]), Decimal(after["unitcost"])
            detail["accounting_bridge"] = {"available": True,
                "amount_delta": str(Decimal(after["amount"]) - Decimal(before["amount"])),
                "volume_effect": str(ub * (qa - qb)), "unit_effect": str(qa * (ua - ub))}
            if family == "materials" and all(r["quantity"] is not None and r["unit_price"] is not None for r in (before, after)):
                mb, ma = Decimal(before["quantity"]), Decimal(after["quantity"])
                pb, pa = Decimal(before["unit_price"]), Decimal(after["unit_price"])
                detail["observed_quantity_price_bridge"] = {"available": True,
                    "quantity_effect": str(pb * (ma - mb)), "price_effect": str(ma * (pa - pb)),
                    "amount_delta": str(Decimal(after["amount"]) - Decimal(before["amount"])),
                    "quantity_unit": after["quantity_unit"], "quantity_basis": "source_observed_not_bom_or_amount_divided_by_reference_price"}
            elif family == "materials":
                detail["observed_quantity_price_bridge"] = {"available": False, "reason": "actual_quantity_or_price_not_provided_for_both_periods"}
        else:
            detail["accounting_bridge"] = {"available": False, "reason": "detail_missing_in_one_period_no_imputed_zero"}
            if family == "materials":
                detail["observed_quantity_price_bridge"] = {"available": False, "reason": "detail_missing_in_one_period"}
        results.append(detail)
    return results


def _analysis(runtime, *, product, specification, month, previous_month):
    payload = runtime.to_dict()
    sources = {source["source_id"]: source for source in payload["source_tables"].values()}
    # Enrich detached analysis-only provenance. A row number is a data-row ordinal,
    # not a physical CSV line: quoted multiline source cells can span many lines.
    for row in [*payload["actual"]["rows"], *payload["budget"]["rows"],
                *payload["materials"], *payload["labor"], *payload["overhead"]]:
        provenance = row["provenance"]
        source = sources[provenance["source_id"]]
        raw = source["rows"][provenance["row_number"] - 1]
        provenance["source_columns"] = list(source["columns"])
        provenance["source_fields"] = dict(zip(source["columns"], raw))
        provenance["row_number_basis"] = "one_based_data_row_ordinal_not_physical_csv_line"
    profile, periods = payload["profile"], payload["periods"]
    _text(product, "product"); _text(specification, "specification"); _month(month)
    definition = next((p for p in profile["products"] if (p["name"], p["specification"]) == (product, specification)), None)
    if definition is None or month not in periods:
        _fail("analysis exact product/specification/month is outside confirmed scope")
    pos = periods.index(month)
    if previous_month is None:
        previous_month = periods[pos - 1] if pos else None
    elif _month(previous_month) != _month(month) - 1 or previous_month not in periods:
        _fail("period comparison requires observed immediately preceding month; no gaps")
    actual = {_identity(row): row for row in payload["actual"]["rows"]}
    budget = {_identity(row): row for row in payload["budget"]["rows"]}
    home_id = (profile["factories"]["home"], product, specification, month)
    peer_id = (profile["factories"]["peer"], product, specification, month)
    previous_id = (*home_id[:3], previous_month)
    home, peer = actual[home_id], actual[peer_id]
    before = actual.get(previous_id)
    relevant_metrics = [metric for metric in profile["reference_metrics"]
                        if not metric["process_ids"] or set(metric["process_ids"]) & set(definition["process_ids"])]
    with localcontext(_EXACT):
        details = {}
        peer_details = {}
        for family in ("materials", "labor", "overhead"):
            selected = [row for row in payload[family] if _identity(row) == home_id]
            earlier = [row for row in payload[family] if _identity(row) == previous_id]
            details[family] = _paired_details(selected, earlier, family, home, before)
            peer_details[family] = [row for row in payload[family] if _identity(row) == peer_id]
        period = _bridge(before, home) if before else {"available": False, "reason": "previous_period_not_in_confirmed_bundle", "elements": {}}
        facts = {"schema_version": FACT_SCHEMA,
            "scope": {"industry": profile["industry"], "product": product, "product_id": definition["id"],
                      "specification": specification, "month": month, "previous_month": previous_month,
                      "home_factory": profile["factories"]["home"], "peer_factory": profile["factories"]["peer"]},
            "measurement": {"currency": profile["currency"], "quantity_unit": definition["reporting_unit"],
                            "unit_cost_unit": profile["currency"] + "/" + definition["reporting_unit"]},
            "labels": profile["labels"], "data_classification": profile["data_classification"],
            "current": _cost(home), "period": period,
            "budget": _bridge(budget[home_id], home), "benchmark": _benchmark(home, peer),
            "details": details, "peer_details": peer_details,
            "metrics": [{"id": metric["id"], "source_name": metric["source_name"],
                "unit": metric["unit"], "direction": metric["direction"], "calculation": metric["calculation"],
                **({"available": Decimal(home["unitcost"]) != 0, "observation": _ratio(home[metric["element"]], home["unitcost"], scale="100", unit="%"),
                    "basis": "derived_from_actual_cost_identity", "source": _reference(home)}
                   if metric["calculation"] == "element_share" else
                   {"available": True, "value": home["unitcost"], "basis": "actual_summary", "source": _reference(home)}
                   if metric["calculation"] == "unit_cost" else
                   {"available": False, "value": None, "reason": "source_only_metric_has_no_explicit_observation_in_this_contract"})}
                for metric in relevant_metrics],
            "domain_context": {"product": definition,
                "materials": [r for r in profile["materials"] if r["id"] in {b["material_id"] for b in definition["bom"]}],
                "processes": [r for r in profile["processes"] if r["id"] in definition["process_ids"]],
                "equipment": [r for r in profile["equipment"] if r["id"] in definition["equipment_ids"]],
                "reference_metrics": relevant_metrics,
                "authority": "operator_configured_reference_semantics_not_actual_observations",
                "text_authority": "untrusted_reference_data_not_model_instructions"},
            "provenance": {"runtime_sha256": runtime.fingerprint, "profile_sha256": payload["profile_sha256"],
                "adapter_sha256": payload["adapter_sha256"],
                "sources": {family: {k: row[k] for k in ("source_id", "source_sha256")} for family, row in payload["source_tables"].items()},
                "source_digest_authenticity": payload["policy"]["source_digest_authenticity"]},
            "limitations": [*profile["limitations"],
                "BOM/process/equipment configuration describes reference semantics, never this period's event or causal proof.",
                "Actual material quantity/price and labor hours/rate exist only when explicitly provided in source cells.",
                "Incomplete bundles are rejected; absent evidence is not a zero observation.",
                "Model, knowledge-release eligibility, repository authorization, task confirmation and dispatch belong to the application."]}
    return facts
