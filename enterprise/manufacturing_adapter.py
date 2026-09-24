"""Pure, fail-closed manufacturing table adapter; no file/network/role/dispatch API.

Only the server operator may supply configuration. Validation is not authentication.
Inputs are explicit headers plus positional rows, so duplicate headers cannot be
lost in a dict. Monetary cells are per-unit material/labor/overhead/unitcost and
one total amount, all in the row's declared currency. Output and cost-denominator
units are separately declared. Identities are never guessed, trimmed or aliased.

All arithmetic uses finite Decimal with trapped inexact results. Source and target
identities close exactly, with no tolerance, balancing item or implicit rounding.
Returned records/configuration/provenance are frozen; JSON exports are detached
copies. Hashes establish deterministic integrity, NOT authenticity of source data.
The optional forecast bridge reuses enterprise.forecast without migrating its UI.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Context, Decimal, DecimalException, Inexact, localcontext
from fractions import Fraction
import hashlib
import json
import re
import unicodedata

SCHEMA_VERSION = "manufacturing-adapter/1"
CANONICAL_SCHEMA_VERSION = "manufacturing-adapter/2"
CANONICAL_FIELDS = (
    "factory", "product", "specification", "month", "material", "labor",
    "overhead", "unitcost", "output", "total",
)
NUMERIC_FIELDS = CANONICAL_FIELDS[4:]
INDUSTRIES = frozenset({"machinery", "auto_parts", "chemicals", "electronics"})
_MAX_ROWS = 100_000
# Closed physical vocabulary, not automatic conversion: each used pair must also
# be explicitly configured. Packaging/BOM-dependent and mass<->count conversions
# are deliberately unsupported. Scales are exact SI definitions, not observations.
_UNIT_SCALES = {
    "piece": ("count", "1"), "kg": ("mass", "1"), "g": ("mass", "0.001"),
    "t": ("mass", "1000"), "L": ("volume", "1"), "mL": ("volume", "0.001"),
    "m": ("length", "1"), "cm": ("length", "0.01"), "mm": ("length", "0.001"),
    "m2": ("area", "1"), "cm2": ("area", "0.0001"),
}
# Version 2 permits explicitly named reporting packages, but ONLY identity
# conversion for each distinct package. A box never becomes pieces implicitly.
_CANONICAL_UNIT_SCALES = {**_UNIT_SCALES, **{name: ("package:" + name, "1")
    for name in ("box", "tablet", "capsule", "vial", "bag", "bottle", "set")}}
_CONFIG_FIELDS = {
    "schema_version", "id", "version", "industry", "label", "status", "columns",
    "currency", "units", "factories", "product_units", "limitations",
}


class ManufacturingAdapterError(ValueError):
    """Invalid schema, observations, scope, arithmetic or integrity; no partial output."""


def _text(value, label, limit=160):
    if (type(value) is not str or not value or value != value.strip() or len(value) > limit
            or any(unicodedata.category(c).startswith("C") for c in value)):
        raise ManufacturingAdapterError(f"{label}: expected bounded, exact nonempty text")
    return value


def _object(value, fields, label):
    if type(value) is not dict or set(value) != set(fields):
        raise ManufacturingAdapterError(f"{label}: missing or unknown fields")
    return value


def _list(value, label, *, maximum=10_000, empty=False):
    if type(value) is not list or len(value) > maximum or (not empty and not value):
        raise ManufacturingAdapterError(f"{label}: expected bounded {'possibly empty ' if empty else ''}list")
    return value


def _decimal(value, label, *, positive=False):
    if type(value) not in (str, int, Decimal):
        raise ManufacturingAdapterError(f"{label}: use exact decimal text/integer/Decimal, not float/bool")
    if type(value) is str and (len(value) > 160 or not re.fullmatch(
            r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?", value)):
        raise ManufacturingAdapterError(f"{label}: invalid finite decimal text")
    try:
        number = Decimal(value)
    except (DecimalException, ValueError, OverflowError):
        raise ManufacturingAdapterError(f"{label}: invalid decimal") from None
    if (not number.is_finite() or len(number.as_tuple().digits) > 60
            or not -60 <= number.as_tuple().exponent <= 60
            or not -60 <= number.adjusted() <= 60
            or number < 0 or (positive and number <= 0)):
        raise ManufacturingAdapterError(f"{label}: expected bounded finite {'positive' if positive else 'nonnegative'} decimal")
    return number


def _unit(value, label, *, canonical=False):
    if type(value) is not str or value not in (_CANONICAL_UNIT_SCALES if canonical else _UNIT_SCALES):
        raise ManufacturingAdapterError(f"{label}: unsupported unit; no inferred aliases or packaging")
    return value


def _currency(value, label):
    if type(value) is not str or not re.fullmatch(r"[A-Z]{3}", value):
        raise ManufacturingAdapterError(f"{label}: expected explicit three-letter currency code")
    return value


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash(value):
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class AdapterConfig:
    """Validated canonical JSON; to_dict returns a copy, never the stored payload."""
    payload_json: str

    @property
    def fingerprint(self):
        return hashlib.sha256(self.payload_json.encode("utf-8")).hexdigest()

    def to_dict(self):
        return json.loads(self.payload_json)


def validate_adapter_config(value) -> AdapterConfig:
    """Accept only declarative plain data; active bindings must be operator-supplied.

    A valid template has NO factories or products and cannot adapt data. Unknown
    keys (including expressions, loaders, paths, roles or URLs) are rejected.
    Unit factors are source quantity * factor = target quantity. Currency rates
    are source amount * rate = target amount; no exchange-rate lookup occurs.
    """
    _object(value, _CONFIG_FIELDS, "config")
    if type(value["schema_version"]) is not str or value["schema_version"] not in {SCHEMA_VERSION, CANONICAL_SCHEMA_VERSION}:
        raise ManufacturingAdapterError("unsupported schema_version")
    for name in ("id", "version", "industry", "label", "status"):
        _text(value[name], name)
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", value["id"]):
        raise ManufacturingAdapterError("id: expected stable identifier")
    canonical = value["schema_version"] == CANONICAL_SCHEMA_VERSION
    if canonical:
        def check_declarative(node):
            if type(node) is str and re.search(r'(?i)([a-z][a-z0-9+.-]{1,31}://|javascript:|data:|<\|[^>]+\|>|\[/?INST\]|__import__|<script|</?(?:system|assistant|developer)>)', node):
                raise ManufacturingAdapterError("canonical configuration cannot contain URL, executable or role markup")
            if type(node) is dict:
                for item in node.values():
                    check_declarative(item)
            elif type(node) is list:
                for item in node:
                    check_declarative(item)
        check_declarative(value)
    industries = INDUSTRIES | {"pharma"} if canonical else INDUSTRIES
    if value["industry"] not in industries or value["status"] not in {"template", "active"}:
        raise ManufacturingAdapterError("unknown industry or status")
    columns = _object(value["columns"], CANONICAL_FIELDS, "columns")
    for key, column in columns.items():
        _text(column, f"columns.{key}")
    currency = _object(value["currency"], {"source_column", "target", "rates"}, "currency")
    _text(currency["source_column"], "currency.source_column")
    target = _currency(currency["target"], "currency.target")
    rates, seen = [], set()
    for rate in _list(currency["rates"], "currency.rates", maximum=100):
        _object(rate, {"source", "factor"}, "currency.rate")
        source = _currency(rate["source"], "currency.source")
        factor = _decimal(rate["factor"], "currency.factor", positive=True)
        if source in seen or (source == target and factor != 1):
            raise ManufacturingAdapterError("duplicate currency source or non-identity same-currency rate")
        seen.add(source)
        rates.append({"source": source, "factor": str(factor)})
    units = _object(value["units"], {"quantity_column", "cost_denominator_column", "conversions"}, "units")
    for key in ("quantity_column", "cost_denominator_column"):
        _text(units[key], f"units.{key}")
    conversions, pairs = [], set()
    for rule in _list(units["conversions"], "units.conversions", maximum=200):
        _object(rule, {"source", "target", "factor"}, "unit conversion")
        source = _unit(rule["source"], "unit.source", canonical=canonical)
        dest = _unit(rule["target"], "unit.target", canonical=canonical)
        factor = _decimal(rule["factor"], "unit.factor", positive=True)
        scales = _CANONICAL_UNIT_SCALES if canonical else _UNIT_SCALES
        left, right = scales[source], scales[dest]
        if left[0] != right[0]:
            raise ManufacturingAdapterError("unit dimension mismatch; count/mass or mass/volume need separate evidence")
        if Fraction(factor) != Fraction(left[1]) / Fraction(right[1]):
            raise ManufacturingAdapterError("unit factor conflicts with exact physical definition")
        if (source, dest) in pairs:
            raise ManufacturingAdapterError("duplicate unit conversion")
        pairs.add((source, dest))
        conversions.append({"source": source, "target": dest, "factor": str(factor)})
    headers = [*columns.values(), currency["source_column"], units["quantity_column"], units["cost_denominator_column"]]
    if len(set(headers)) != len(headers):
        raise ManufacturingAdapterError("source columns must be unique, including measurement metadata")
    factories = _list(value["factories"], "factories", maximum=500, empty=True)
    for factory in factories:
        _text(factory, "factory")
    if len(set(factories)) != len(factories):
        raise ManufacturingAdapterError("duplicate configured factory")
    bindings, identities = [], set()
    for row in _list(value["product_units"], "product_units", empty=True):
        _object(row, {"product", "specification", "unit"}, "product_units entry")
        product, spec = _text(row["product"], "product"), _text(row["specification"], "specification")
        unit = _unit(row["unit"], "product unit", canonical=canonical)
        if (product, spec) in identities:
            raise ManufacturingAdapterError("duplicate product/specification unit binding")
        if not any(dest == unit for _, dest in pairs):
            raise ManufacturingAdapterError("product unit has no explicit conversion target")
        identities.add((product, spec))
        bindings.append({"product": product, "specification": spec, "unit": unit})
    if value["status"] == "template" and (factories or bindings):
        raise ManufacturingAdapterError("templates cannot contain assumed actual factories/products")
    if value["status"] == "active" and (not factories or not bindings):
        raise ManufacturingAdapterError("active config requires confirmed factories and product/unit bindings")
    for note in _list(value["limitations"], "limitations", maximum=30):
        _text(note, "limitation", 1000)
    normalized = {
        **value, "columns": dict(columns), "factories": list(factories), "product_units": bindings,
        "currency": {**currency, "rates": rates}, "units": {**units, "conversions": conversions},
    }
    return AdapterConfig(_json(normalized))


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ManufacturingAdapterError("duplicate JSON field")
        result[key] = value
    return result


def parse_adapter_config(text: str) -> AdapterConfig:
    """Parse already-read server JSON; deliberately accepts no path or environment."""
    if type(text) is not str or len(text) > 1_000_000:
        raise ManufacturingAdapterError("config JSON must be bounded text")
    def reject_constant(_):
        raise ManufacturingAdapterError("nonfinite JSON constant")
    try:
        value = json.loads(text, object_pairs_hook=_unique_object, parse_constant=reject_constant)
    except (ValueError, RecursionError) as exc:
        if isinstance(exc, ManufacturingAdapterError):
            raise
        raise ManufacturingAdapterError("invalid config JSON") from None
    return validate_adapter_config(value)


@dataclass(frozen=True, slots=True)
class SourceCell:
    kind: str
    text: str

    def original(self):
        if self.kind == "str":
            return self.text
        if self.kind == "int":
            return int(self.text)
        if self.kind == "decimal":
            return Decimal(self.text)
        raise ManufacturingAdapterError("unknown source cell encoding")


@dataclass(frozen=True, slots=True)
class RowProvenance:
    source_id: str
    source_sha256: str  # Caller-attested digest; adapter does not open the source.
    row_number: int  # 1-based DATA-row ordinal, not an inferred CSV/Excel line number.
    source_cells: tuple[SourceCell, ...]
    source_row_sha256: str
    adapter_sha256: str


@dataclass(frozen=True, slots=True)
class CanonicalRow:
    factory: str
    product: str
    specification: str
    month: str
    material: Decimal
    labor: Decimal
    overhead: Decimal
    unitcost: Decimal
    output: Decimal
    total: Decimal
    currency: str
    unit: str
    provenance: RowProvenance

    @property
    def identity(self):
        return self.factory, self.product, self.specification, self.month

    def to_dict(self):
        result = {key: str(getattr(self, key)) if key in NUMERIC_FIELDS else getattr(self, key)
                  for key in CANONICAL_FIELDS}
        p = self.provenance
        result.update(currency=self.currency, unit=self.unit, provenance={
            "source_id": p.source_id, "source_sha256": p.source_sha256,
            "row_number": p.row_number, "source_row_sha256": p.source_row_sha256,
            "adapter_sha256": p.adapter_sha256,
            "source_cells": [{"kind": cell.kind, "text": cell.text} for cell in p.source_cells],
        })
        return result


@dataclass(frozen=True, slots=True)
class ManufacturingSnapshot:
    config: AdapterConfig
    columns: tuple[str, ...]
    source_id: str
    source_sha256: str
    rows: tuple[CanonicalRow, ...]

    def to_dict(self):
        return {"schema_version": self.config.to_dict()["schema_version"], "adapter_sha256": self.config.fingerprint,
                "source_id": self.source_id, "source_sha256": self.source_sha256,
                "columns": list(self.columns), "rows": [row.to_dict() for row in self.rows]}

    @property
    def fingerprint(self):
        return _hash(self.to_dict())


def adapt_rows(config, *, columns, rows, source_id, source_sha256) -> ManufacturingSnapshot:
    """Validate the WHOLE table, then return immutable canonical records atomically.

    Only list/tuple headers and list/tuple positional rows are accepted; adapters
    perform no data loading. Extra/missing headers and row-width mismatches fail.
    No filtering of invalid/future/out-of-scope rows or implicit aggregation occurs.
    A positive historical output is required; zero-output cost allocation needs a
    different accounting policy and is not invented here.
    """
    checked = parse_adapter_config(config.payload_json) if type(config) is AdapterConfig else validate_adapter_config(config)
    cfg = checked.to_dict()
    if cfg["status"] != "active":
        raise ManufacturingAdapterError("starter template is not active; supply confirmed server bindings first")
    _text(source_id, "source_id")
    if type(source_sha256) is not str or not re.fullmatch(r"[0-9a-f]{64}", source_sha256):
        raise ManufacturingAdapterError("source_sha256 must be an explicit lowercase SHA-256 digest")
    if type(columns) not in (list, tuple) or len(columns) != len(CANONICAL_FIELDS) + 3:
        raise ManufacturingAdapterError("missing or extra source columns")
    for column in columns:
        _text(column, "source column")
    if len(set(columns)) != len(columns):
        raise ManufacturingAdapterError("duplicate source columns")
    mapping, currency, units = cfg["columns"], cfg["currency"], cfg["units"]
    expected = {*mapping.values(), currency["source_column"], units["quantity_column"], units["cost_denominator_column"]}
    if set(columns) != expected:
        raise ManufacturingAdapterError("missing or extra mapped source columns")
    if type(rows) not in (list, tuple) or not rows or len(rows) > _MAX_ROWS:
        raise ManufacturingAdapterError("rows must be a bounded nonempty list/tuple")
    product_units = {(p["product"], p["specification"]): p["unit"] for p in cfg["product_units"]}
    rates = {rate["source"]: Decimal(rate["factor"]) for rate in currency["rates"]}
    factors = {(r["source"], r["target"]): Decimal(r["factor"]) for r in units["conversions"]}
    seen, result = set(), []
    arithmetic = Context(prec=512, Emin=-999, Emax=999)
    arithmetic.traps[Inexact] = True
    for index, row in enumerate(rows, 1):
        if type(row) not in (list, tuple) or len(row) != len(columns):
            raise ManufacturingAdapterError(f"row {index}: source row width mismatch")
        raw = dict(zip(columns, row))
        values = {key: raw[source] for key, source in mapping.items()}
        identity = tuple(_text(values[key], key) for key in CANONICAL_FIELDS[:4])
        factory, product, specification, month = identity
        if not re.fullmatch(r"[0-9]{4}-(0[1-9]|1[0-2])", month) or month[:4] == "0000":
            raise ManufacturingAdapterError("month must be exact YYYY-MM, year 0001..9999")
        if factory not in cfg["factories"]:
            raise ManufacturingAdapterError("unknown factory; configured names are exact and not authorization grants")
        if (product, specification) not in product_units:
            raise ManufacturingAdapterError("unknown product/specification; no inferred unit")
        if identity in seen:
            raise ManufacturingAdapterError("duplicate factory/product/specification/month")
        seen.add(identity)
        target_unit = product_units[(product, specification)]
        source_currency = _currency(raw[currency["source_column"]], "row currency")
        canonical = cfg["schema_version"] == CANONICAL_SCHEMA_VERSION
        quantity_unit = _unit(raw[units["quantity_column"]], "row quantity unit", canonical=canonical)
        denominator_unit = _unit(raw[units["cost_denominator_column"]], "row cost denominator unit", canonical=canonical)
        if source_currency not in rates:
            raise ManufacturingAdapterError("unconfigured source currency")
        if (quantity_unit, target_unit) not in factors or (denominator_unit, target_unit) not in factors:
            raise ManufacturingAdapterError("unconfigured or mismatched unit conversion")
        numbers = {key: _decimal(values[key], key, positive=key == "output") for key in NUMERIC_FIELDS}
        rate = rates[source_currency]
        qfactor, cfactor = factors[(quantity_unit, target_unit)], factors[(denominator_unit, target_unit)]
        try:
            with localcontext(arithmetic):
                if numbers["material"] + numbers["labor"] + numbers["overhead"] != numbers["unitcost"]:
                    raise ManufacturingAdapterError("source components do not close exactly to unitcost")
                # This cross-multiplied identity needs no rounding or division.
                if numbers["total"] * cfactor != numbers["unitcost"] * numbers["output"] * qfactor:
                    raise ManufacturingAdapterError("source total does not close exactly to unitcost times output")
                converted = {key: numbers[key] * rate / cfactor for key in NUMERIC_FIELDS[:4]}
                converted.update(output=numbers["output"] * qfactor, total=numbers["total"] * rate)
                if converted["material"] + converted["labor"] + converted["overhead"] != converted["unitcost"]:
                    raise ManufacturingAdapterError("converted components do not close exactly")
                if converted["unitcost"] * converted["output"] != converted["total"]:
                    raise ManufacturingAdapterError("converted total does not close exactly")
        except DecimalException:
            raise ManufacturingAdapterError("conversion is inexact/nonfinite/outside bounded arithmetic; no rounding permitted") from None
        cells = tuple(SourceCell({str: "str", int: "int", Decimal: "decimal"}[type(value)], str(value)) for value in row)
        row_hash = _hash({"columns": list(columns), "cells": [{"kind": c.kind, "text": c.text} for c in cells]})
        provenance = RowProvenance(source_id, source_sha256, index, cells, row_hash, checked.fingerprint)
        result.append(CanonicalRow(*identity, **converted, currency=currency["target"], unit=target_unit, provenance=provenance))
    return ManufacturingSnapshot(checked, tuple(columns), source_id, source_sha256, tuple(result))


@dataclass(frozen=True, slots=True)
class ManufacturingForecast:
    """Frozen forecast/proof payload; callers receive only detached JSON copies."""
    payload_json: str

    def to_dict(self):
        return json.loads(self.payload_json)


def forecast_manufacturing(snapshot, *, factory, product, specification, cutoff_month,
                           horizon=1, method="naive", candidate_methods=("naive", "ma3")) -> ManufacturingForecast:
    """Bridge to the existing pure forecast kernel, not dashboard/router integration.

    Replays validation against frozen source cells before forecasting; all rows,
    including future/other-factory rows, must still validate. Legacy 元/盒 headers
    exist only in the internal bridge. Values are NOT converted to boxes/CNY; the
    explicit output measurement metadata is authoritative. No budget adapter,
    future volume/total, model invocation, permissions or task dispatch is added.
    """
    if type(snapshot) is not ManufacturingSnapshot:
        raise ManufacturingAdapterError("forecast requires a validated ManufacturingSnapshot")
    if type(snapshot.config) is AdapterConfig and snapshot.config.to_dict().get("schema_version") == CANONICAL_SCHEMA_VERSION:
        raise ManufacturingAdapterError("adapter/2 does not use the legacy pharmacy forecast bridge; use canonical runtime facts")
    # Decimal(1) == 1 == 1.0: dataclass equality alone cannot enforce the schema.
    if (type(snapshot.config) is not AdapterConfig or type(snapshot.rows) is not tuple
            or type(snapshot.columns) is not tuple
            or any(type(row) is not CanonicalRow
                   or any(type(getattr(row, field)) is not Decimal for field in NUMERIC_FIELDS)
                   or type(row.provenance) is not RowProvenance
                   or type(row.provenance.row_number) is not int
                   or type(row.provenance.source_cells) is not tuple
                   or any(type(cell) is not SourceCell for cell in row.provenance.source_cells)
                   for row in snapshot.rows)):
        raise ManufacturingAdapterError("snapshot contains noncanonical mutable or numeric types")
    try:
        rebuilt = adapt_rows(snapshot.config, columns=snapshot.columns,
                             rows=[tuple(c.original() for c in row.provenance.source_cells) for row in snapshot.rows],
                             source_id=snapshot.source_id, source_sha256=snapshot.source_sha256)
    except (AttributeError, TypeError, ValueError, DecimalException):
        raise ManufacturingAdapterError("snapshot source/provenance validation failed") from None
    if rebuilt.to_dict() != snapshot.to_dict():
        raise ManufacturingAdapterError("snapshot integrity mismatch; canonical values/provenance were changed")
    for name, value in (("factory", factory), ("product", product), ("specification", specification)):
        _text(value, name)
    selected = [r for r in snapshot.rows if r.identity[:3] == (factory, product, specification)]
    if not selected:
        raise ManufacturingAdapterError("no observations for exact configured factory/product/specification")
    # Imports are lazy; importing/using the schema adapter itself has no pandas dependency.
    import pandas as pd
    from enterprise.forecast import ELEMENTS, IDENTITY, UNIT_COLUMN, forecast_baseline
    records = []
    for row in selected:
        record = dict(zip(IDENTITY, row.identity))
        record.update(zip(ELEMENTS.values(), (row.material, row.labor, row.overhead)))
        record.update({UNIT_COLUMN: row.unitcost, "_source_hash": row.provenance.source_row_sha256})
        records.append(record)
    frame = pd.DataFrame(records)
    frame.attrs["cost_snapshot_hash"] = snapshot.fingerprint
    frame.attrs["source_sha256"] = snapshot.source_sha256
    cfg = snapshot.config.to_dict()
    measurement = {"currency": selected[0].currency, "quantity_unit": selected[0].unit,
                   "unit_cost_unit": selected[0].currency + "/" + selected[0].unit}
    proof = {"schema_version": SCHEMA_VERSION, "adapter_id": cfg["id"], "adapter_version": cfg["version"],
             "adapter_sha256": snapshot.config.fingerprint, "snapshot_sha256": snapshot.fingerprint,
             "source_id": snapshot.source_id, "source_sha256": snapshot.source_sha256,
             "source_digest_authenticity": "caller_attested_not_independently_verified",
             "closure_policy": "exact Decimal; material + labor + overhead = unitcost; unitcost * output = total",
             "source_row_hashes": [r.provenance.source_row_sha256 for r in selected],
             "measurement": measurement}
    result = forecast_baseline({"manufacturing_adapter": frame}, factory=factory, product=product,
                               specification=specification, cutoff_month=cutoff_month, horizon=horizon,
                               method=method, candidate_methods=candidate_methods, snapshot_meta=proof)
    result["measurement"] = measurement
    result["provenance"]["manufacturing_adapter"] = proof
    result["limitations"].extend([
        "Manufacturing bridge only: legacy dashboard, import, attribution, benchmark and router are not migrated.",
        "Per-unit amounts use measurement.currency/quantity_unit; private legacy kernel headers do not mean boxes.",
        "Adapter validates exact identities before the kernel's weaker tolerance; no budget mapping is provided.",
        "Configuration and source hashes are integrity identifiers, not signatures, authenticity or permission grants.",
    ])
    return ManufacturingForecast(_json(result))
