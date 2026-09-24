"""All observations/factories/products/rates below are SYNTHETIC TEST FIXTURES.

No benchmark CSV, production data, database, delivery artifact, network or model
is consulted. Starter JSON files contain schema only and must remain inactive.
"""
from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
from decimal import Decimal, localcontext
import hashlib
import json
from pathlib import Path
import socket
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from enterprise.manufacturing_adapter import (
    AdapterConfig, CANONICAL_FIELDS, ManufacturingAdapterError,
    adapt_rows, forecast_manufacturing, parse_adapter_config, validate_adapter_config,
)

FACTORY = "SYNTHETIC_FIXTURE_FACTORY_A"
PEER = "SYNTHETIC_FIXTURE_FACTORY_B"
PRODUCT = "SYNTHETIC_FIXTURE_PRODUCT_NOT_REAL"
SPEC = "SYNTHETIC_FIXTURE_REV_1"
SOURCE = "SYNTHETIC_IN_MEMORY_FIXTURE_NOT_ACTUAL_INDUSTRY_DATA"
SOURCE_HASH = hashlib.sha256(b"SYNTHETIC_FIXTURE_ATTESTATION_NOT_A_REAL_SOURCE_FILE").hexdigest()


def synthetic_config(unit="piece"):
    conversions = [{"source": unit, "target": unit, "factor": "1"}]
    if unit == "kg":
        conversions.extend([{"source": "g", "target": "kg", "factor": "0.001"},
                            {"source": "t", "target": "kg", "factor": "1000"}])
    return {
        "schema_version": "manufacturing-adapter/1", "id": "synthetic-fixture-only",
        "version": "1", "industry": "machinery", "label": "SYNTHETIC TEST CONFIG NOT REAL",
        "status": "active", "columns": {key: "fixture_" + key for key in CANONICAL_FIELDS},
        "currency": {"source_column": "fixture_currency", "target": "CNY",
                     "rates": [{"source": "CNY", "factor": "1"}]},
        "units": {"quantity_column": "fixture_output_unit", "cost_denominator_column": "fixture_cost_unit",
                  "conversions": conversions},
        "factories": [FACTORY, PEER],
        "product_units": [{"product": PRODUCT, "specification": SPEC, "unit": unit}],
        "limitations": ["SYNTHETIC FIXTURE: not a measurement, benchmark or exchange-rate observation."],
    }


def synthetic_row(month="2026-01", unit="piece", **overrides):
    values = {"factory": FACTORY, "product": PRODUCT, "specification": SPEC, "month": month,
              "material": "1.1", "labor": "2.2", "overhead": "3.3", "unitcost": "6.6",
              "output": "10", "total": "66", "currency": "CNY", "output_unit": unit, "cost_unit": unit}
    values.update(overrides)
    return {"fixture_" + key: value for key, value in values.items()}


def adapt_fixture(*raw_rows, config=None, columns=None, source_id=SOURCE, source_sha256=SOURCE_HASH):
    config = synthetic_config() if config is None else config
    raw_rows = raw_rows or (synthetic_row(),)
    columns = list(raw_rows[0]) if columns is None else columns
    return adapt_rows(config, columns=columns, rows=[[row.get(c) for c in columns] for row in raw_rows],
                      source_id=source_id, source_sha256=source_sha256)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("Manufacturing fixture tests may not use network/RPA/LLM")
    monkeypatch.setattr(socket.socket, "connect", denied)


@pytest.mark.parametrize("name", ["machinery", "auto_parts", "chemicals", "electronics"])
def test_starter_is_schema_only_not_an_actual_dataset(name):
    config = parse_adapter_config((ROOT / "config" / "manufacturing_adapters" / (name + ".json")).read_text(encoding="utf-8"))
    value = config.to_dict()
    assert value["industry"] == name
    assert value["status"] == "template"
    assert value["factories"] == [] and value["product_units"] == []
    assert set(value["columns"]) == set(CANONICAL_FIELDS)
    assert not ({"observations", "benchmark", "measurements", "reference_metrics", "products"} & value.keys())
    with pytest.raises(ManufacturingAdapterError, match="template is not active"):
        adapt_rows(config, columns=[], rows=[], source_id=SOURCE, source_sha256=SOURCE_HASH)


def test_exact_piece_identity_and_detached_immutable_provenance():
    cfg = synthetic_config()
    raw = synthetic_row()
    original_cfg, original_raw = deepcopy(cfg), deepcopy(raw)
    snapshot = adapt_fixture(raw, config=cfg)
    row = snapshot.rows[0]
    assert row.identity == (FACTORY, PRODUCT, SPEC, "2026-01")
    assert row.material + row.labor + row.overhead == row.unitcost == Decimal("6.6")
    assert row.output * row.unitcost == row.total == Decimal("66")
    assert row.unit == "piece" and row.currency == "CNY"
    assert all(type(getattr(row, k)) is Decimal for k in CANONICAL_FIELDS[4:])
    assert row.provenance.source_id == SOURCE
    assert row.provenance.source_sha256 == SOURCE_HASH
    assert row.provenance.row_number == 1
    assert row.provenance.adapter_sha256 == snapshot.config.fingerprint
    before = snapshot.fingerprint
    cfg["factories"][0] = "MUTATED_CALLER_CONFIG"
    raw["fixture_material"] = "999"
    exported = snapshot.to_dict()
    exported["rows"][0]["provenance"]["source_cells"][0]["text"] = "MUTATED_EXPORT"
    detached = snapshot.config.to_dict()
    detached["columns"]["factory"] = "MUTATED_EXPORT_COLUMN"
    assert snapshot.fingerprint == before
    assert snapshot == adapt_fixture(original_raw, config=original_cfg)
    with pytest.raises(FrozenInstanceError):
        row.total = Decimal("99")
    with pytest.raises(FrozenInstanceError):
        row.provenance.source_sha256 = "0" * 64
    with pytest.raises(FrozenInstanceError):
        row.provenance.source_cells[0].text = "changed"
    with pytest.raises(TypeError):
        snapshot.rows[0] = row
    json.dumps(snapshot.to_dict(), allow_nan=False)


def test_exact_currency_and_different_mass_denominators_preserve_total():
    cfg = synthetic_config("kg")
    # Deliberately artificial rate; absolutely not an actual FX observation.
    cfg["currency"]["rates"].append({"source": "USD", "factor": "7.125"})
    row = synthetic_row(unit="kg", currency="USD", output_unit="t", cost_unit="g",
                        material="0.001", labor="0.002", overhead="0.003", unitcost="0.006",
                        output="2", total="12000")
    converted = adapt_fixture(row, config=cfg).rows[0]
    assert converted.unit == "kg" and converted.currency == "CNY"
    assert converted.output == Decimal("2000")
    assert converted.material == Decimal("7.125")
    assert converted.labor == Decimal("14.25")
    assert converted.overhead == Decimal("21.375")
    assert converted.unitcost == Decimal("42.75")
    assert converted.total == Decimal("85500") == converted.unitcost * converted.output
    assert converted.provenance.source_cells[list(row).index("fixture_total")].text == "12000"


def test_adapter_arithmetic_does_not_depend_on_ambient_decimal_precision():
    with localcontext() as ctx:
        ctx.prec = 2
        result = adapt_fixture(synthetic_row(material="1.234567890123456789", labor="2", overhead="3",
                                            unitcost="6.234567890123456789", output="100",
                                            total="623.4567890123456789"))
    assert result.rows[0].unitcost == Decimal("6.234567890123456789")
    assert result.rows[0].total == Decimal("623.4567890123456789")


def test_zero_cost_positive_output_is_valid_and_no_balancing_item_added():
    row = adapt_fixture(synthetic_row(material=0, labor=0, overhead=0, unitcost=0, total=0)).rows[0]
    assert row.unitcost == row.total == 0
    assert set(row.to_dict()) == {*CANONICAL_FIELDS, "currency", "unit", "provenance"}


@pytest.mark.parametrize("bad", [True, False, None, 1.1, float("nan"), float("inf"), "NaN", "Infinity",
                                "-Infinity", Decimal("NaN"), Decimal("sNaN"), Decimal("Infinity"),
                                "-1", "", " 1", "1 ", "1,000", "1_000", "1/3", "=1+2",
                                "1e1000", "1e-1000", "9" * 61, [], {}])
@pytest.mark.parametrize("field", ["material", "labor", "overhead", "unitcost", "output", "total"])
def test_reject_bad_numeric_cells(field, bad):
    with pytest.raises(ManufacturingAdapterError):
        adapt_fixture(synthetic_row(**{field: bad}))


@pytest.mark.parametrize("output", [0, "0", Decimal("0"), "-0"])
def test_zero_output_needs_separate_policy_and_is_rejected(output):
    with pytest.raises(ManufacturingAdapterError, match="positive"):
        adapt_fixture(synthetic_row(output=output, total="0"))


@pytest.mark.parametrize("updates", [
    {"unitcost": "6.600000000000000000000000000001"},
    {"material": "1.100000000000000000000000000001"},
    {"total": "66.000000000000000000000000000001"},
    {"output": "10.000000000000000000000000000001"},
])
def test_exact_closure_no_tolerance_no_silent_rounding(updates):
    with pytest.raises(ManufacturingAdapterError, match="close exactly"):
        adapt_fixture(synthetic_row(**updates))


@pytest.mark.parametrize("updates", [
    {"factory": "UNCONFIGURED_FACTORY"}, {"factory": FACTORY + " "},
    {"factory": ""}, {"product": "UNKNOWN_PRODUCT"}, {"specification": "UNKNOWN_REVISION"},
    {"month": "2026-1"}, {"month": "2026-00"}, {"month": "2026-13"},
    {"month": "0000-01"}, {"month": "2026年1月"}, {"month": "２０２６-01"},
    {"month": "2026-01-01"}, {"month": "2026-01\n"}, {"month": None},
    {"currency": "USD"}, {"currency": "cny"}, {"currency": "CNY "},
    {"output_unit": "kg"}, {"cost_unit": "kg"}, {"output_unit": "pieces"},
    {"cost_unit": "盒"}, {"product": PRODUCT + "\u200b"},
])
def test_reject_unknown_or_nonexact_identity_currency_units(updates):
    with pytest.raises(ManufacturingAdapterError):
        adapt_fixture(synthetic_row(**updates))


def test_duplicate_identity_rejected_but_two_confirmed_factories_allowed():
    row = synthetic_row()
    with pytest.raises(ManufacturingAdapterError, match="duplicate factory/product/specification/month"):
        adapt_fixture(row, deepcopy(row))
    result = adapt_fixture(row, synthetic_row(factory=PEER))
    assert {r.factory for r in result.rows} == {FACTORY, PEER}
    assert [r.provenance.row_number for r in result.rows] == [1, 2]


def test_mass_requires_explicit_pair_no_auto_conversions_or_dimension_crossing():
    cfg = synthetic_config("kg")
    cfg["units"]["conversions"] = [{"source": "kg", "target": "kg", "factor": "1"}]
    with pytest.raises(ManufacturingAdapterError, match="unit conversion"):
        adapt_fixture(synthetic_row(unit="kg", output_unit="g"), config=cfg)
    for source, target, factor in [("piece", "kg", "1"), ("L", "kg", "1"),
                                   ("kg", "piece", "1"), ("g", "kg", "0.002"),
                                   ("piece", "piece", "2"), ("box", "piece", "10")]:
        invalid = synthetic_config()
        invalid["units"]["conversions"] = [{"source": source, "target": target, "factor": factor}]
        with pytest.raises(ManufacturingAdapterError):
            validate_adapter_config(invalid)


@pytest.mark.parametrize("field", ["function", "expression", "path", "files", "url", "roles", "network",
                                 "loader", "dispatch", "user", "model", "source_dataset"])
def test_unknown_executable_io_or_authorization_config_fields_rejected(field):
    cfg = synthetic_config()
    cfg[field] = "never interpreted"
    with pytest.raises(ManufacturingAdapterError, match="unknown fields"):
        validate_adapter_config(cfg)


@pytest.mark.parametrize("section", ["columns", "currency", "units"])
def test_nested_unknown_schema_field_rejected(section):
    cfg = synthetic_config()
    cfg[section]["function"] = "__import__('os')"
    with pytest.raises(ManufacturingAdapterError, match="unknown fields"):
        validate_adapter_config(cfg)


def test_missing_required_config_and_mapping_fields_rejected():
    baseline = synthetic_config()
    for key in baseline:
        cfg = deepcopy(baseline)
        del cfg[key]
        with pytest.raises(ManufacturingAdapterError, match="missing"):
            validate_adapter_config(cfg)
    for key in CANONICAL_FIELDS:
        cfg = deepcopy(baseline)
        del cfg["columns"][key]
        with pytest.raises(ManufacturingAdapterError, match="missing"):
            validate_adapter_config(cfg)


@pytest.mark.parametrize("bad", ["0", "-1", "NaN", "Infinity", "1/3", 1.1, True, None, "1e1000", lambda x: x])
@pytest.mark.parametrize("kind", ["currency", "unit"])
def test_nonpositive_nonfinite_or_executable_conversion_factor_rejected(kind, bad):
    cfg = synthetic_config()
    if kind == "currency":
        cfg["currency"]["rates"][0]["factor"] = bad
    else:
        cfg["units"]["conversions"][0]["factor"] = bad
    with pytest.raises(ManufacturingAdapterError):
        validate_adapter_config(cfg)


def test_duplicates_in_config_all_fail_closed():
    variations = []
    cfg = synthetic_config(); cfg["factories"].append(FACTORY); variations.append(cfg)
    cfg = synthetic_config(); cfg["product_units"].append(deepcopy(cfg["product_units"][0])); variations.append(cfg)
    cfg = synthetic_config(); cfg["units"]["conversions"] *= 2; variations.append(cfg)
    cfg = synthetic_config(); cfg["currency"]["rates"] *= 2; variations.append(cfg)
    cfg = synthetic_config(); cfg["columns"]["labor"] = cfg["columns"]["material"]; variations.append(cfg)
    cfg = synthetic_config(); cfg["units"]["quantity_column"] = cfg["columns"]["output"]; variations.append(cfg)
    for cfg in variations:
        with pytest.raises(ManufacturingAdapterError, match="duplicate|unique"):
            validate_adapter_config(cfg)


@pytest.mark.parametrize("change", ["schema", "industry", "status", "factories", "products", "same_currency", "unit_target", "empty_rates"])
def test_invalid_config_contracts(change):
    cfg = synthetic_config()
    if change == "schema": cfg["schema_version"] = "manufacturing-adapter/999"
    if change == "industry": cfg["industry"] = "not-configured"
    if change == "status": cfg["status"] = "template"
    if change == "factories": cfg["factories"] = []
    if change == "products": cfg["product_units"] = []
    if change == "same_currency": cfg["currency"]["rates"][0]["factor"] = "2"
    if change == "unit_target": cfg["product_units"][0]["unit"] = "kg"
    if change == "empty_rates": cfg["currency"]["rates"] = []
    with pytest.raises(ManufacturingAdapterError):
        validate_adapter_config(cfg)


def test_json_parser_rejects_duplicate_nested_keys_nonfinite_and_paths():
    text = json.dumps(synthetic_config())
    assert parse_adapter_config(text) == validate_adapter_config(synthetic_config())
    variants = [text.replace('"version": "1"', '"version": "1", "version": "2"'),
                text.replace('"target": "CNY"', '"target": "CNY", "target": "USD"'),
                text.replace('"factor": "1"', '"factor": NaN', 1),
                text.replace('"factor": "1"', '"factor": Infinity', 1),
                "{}", "[1]", "not-json", "config/manufacturing_adapters/machinery.json", "x" * 1_000_001]
    for raw in variants:
        with pytest.raises(ManufacturingAdapterError):
            parse_adapter_config(raw)


def test_schema_is_data_only_no_callable_or_dict_expression_mapping():
    for value in (lambda: "factory", {"expression": "row[0]"}, ["fixture_factory"], None):
        cfg = synthetic_config()
        cfg["columns"]["factory"] = value
        with pytest.raises(ManufacturingAdapterError):
            validate_adapter_config(cfg)


def test_headers_rows_and_provenance_require_explicit_unambiguous_shape():
    row = synthetic_row()
    columns = list(row)
    base = dict(config=synthetic_config(), columns=columns, rows=[list(row.values())],
                source_id=SOURCE, source_sha256=SOURCE_HASH)
    bad_calls = [
        {"columns": columns[:-1]}, {"columns": [*columns, "extra"]},
        {"columns": [*columns[:-1], columns[0]]}, {"columns": [*columns[:-1], "unknown"]},
        {"columns": iter(columns)}, {"rows": []}, {"rows": [row]}, {"rows": iter([list(row.values())])},
        {"rows": [list(row.values())[:-1]]}, {"rows": [list(row.values()) + ["extra"]]},
        {"source_id": ""}, {"source_id": "SOURCE\n"}, {"source_sha256": "unknown"},
        {"source_sha256": "A" * 64}, {"source_sha256": None},
    ]
    for overrides in bad_calls:
        with pytest.raises(ManufacturingAdapterError):
            adapt_rows(**{**base, **overrides})


def test_reordered_columns_are_explicit_not_positional_guessing():
    row = synthetic_row()
    reordered = adapt_fixture(row, columns=list(reversed(row)))
    original = adapt_fixture(row)
    assert reordered.rows[0].identity == original.rows[0].identity
    assert reordered.rows[0].total == original.rows[0].total
    assert reordered.rows[0].provenance.source_row_sha256 != original.rows[0].provenance.source_row_sha256


def test_hash_records_raw_format_config_and_exact_source_types():
    original = adapt_fixture(synthetic_row())
    lexical = adapt_fixture(synthetic_row(material="1.10"))
    typed = adapt_fixture(synthetic_row(output=10))
    renamed_source = adapt_fixture(source_id="SYNTHETIC_OTHER_SOURCE")
    assert original.rows[0].material == lexical.rows[0].material
    assert original.rows[0].provenance.source_row_sha256 != lexical.rows[0].provenance.source_row_sha256
    assert original.rows[0].provenance.source_row_sha256 != typed.rows[0].provenance.source_row_sha256
    assert original.fingerprint != renamed_source.fingerprint
    cfg = synthetic_config(); cfg["version"] = "2"
    revised = adapt_fixture(config=cfg)
    assert original.config.fingerprint != revised.config.fingerprint
    assert original.fingerprint != revised.fingerprint


def synthetic_history(*, unit="piece", factory=FACTORY):
    return [synthetic_row(month=f"2026-{i:02d}", unit=unit, factory=factory,
                          material=str(i), labor="2", overhead="3", unitcost=str(i + 5),
                          output="10", total=str((i + 5) * 10)) for i in range(1, 7)]


@pytest.mark.parametrize("method,expected", [("naive", "11"), ("ma3", "10"), ("ses", "905883/100000")])
@pytest.mark.parametrize("unit", ["piece", "kg"])
def test_existing_forecast_kernel_reused_with_configured_names_units_and_exact_outputs(method, expected, unit):
    snapshot = adapt_fixture(*synthetic_history(unit=unit, factory=PEER), config=synthetic_config(unit))
    before = snapshot.fingerprint
    frozen = forecast_manufacturing(snapshot, factory=PEER, product=PRODUCT, specification=SPEC,
                                    cutoff_month="2026-06", method=method)
    result = frozen.to_dict()
    assert result["factory"] == PEER and result["product"] == PRODUCT
    assert result["measurement"] == {"currency": "CNY", "quantity_unit": unit, "unit_cost_unit": "CNY/" + unit}
    assert result["forecast"]["unit_cost_exact"] == expected
    assert result["forecast"]["unit_cost"] == sum(result["forecast"]["elements"].values())
    assert result["target_months"] == ["2026-07"]
    assert "output" not in result["forecast"] and "total" not in result["forecast"]
    assert result["budget_comparison"]["status"] == "unavailable"
    assert result["interval"]["coverage"] is None
    assert result["provenance"]["manufacturing_adapter"]["adapter_sha256"] == snapshot.config.fingerprint
    assert result["provenance"]["manufacturing_adapter"]["snapshot_sha256"] == snapshot.fingerprint
    assert result["provenance"]["manufacturing_adapter"]["source_digest_authenticity"].startswith("caller_attested")
    assert snapshot.fingerprint == before
    assert "中药一厂" not in frozen.payload_json and "中药二厂" not in frozen.payload_json
    result["provenance"]["manufacturing_adapter"]["source_sha256"] = "tampered detached export"
    assert frozen.to_dict()["provenance"]["manufacturing_adapter"]["source_sha256"] == SOURCE_HASH
    with pytest.raises(FrozenInstanceError):
        frozen.payload_json = "{}"
    json.dumps(frozen.to_dict(), allow_nan=False)


def test_forecast_cutoff_scope_gap_and_unsupported_parameters():
    cfg = synthetic_config()
    all_rows = synthetic_history() + synthetic_history(factory=PEER)
    snapshot = adapt_fixture(*all_rows, config=cfg)
    normal = forecast_manufacturing(snapshot, factory=FACTORY, product=PRODUCT, specification=SPEC,
                                    cutoff_month="2026-04").to_dict()
    changed = deepcopy(all_rows)
    changed[5] = synthetic_row(month="2026-06", material="900", unitcost="905.5", total="9055")
    future_changed = forecast_manufacturing(adapt_fixture(*changed), factory=FACTORY, product=PRODUCT,
                                           specification=SPEC, cutoff_month="2026-04").to_dict()
    assert normal["forecast"] == future_changed["forecast"]
    assert normal["backtests"] == future_changed["backtests"]
    assert normal["provenance"]["training_hash"] == future_changed["provenance"]["training_hash"]
    assert normal["provenance"]["source_hash"] != future_changed["provenance"]["source_hash"]
    kwargs = {"factory": FACTORY, "product": PRODUCT, "specification": SPEC, "cutoff_month": "2026-06"}
    for override in ({"factory": "home"}, {"product": "UNKNOWN"}, {"specification": "unknown"},
                     {"cutoff_month": "2026-07"}, {"horizon": 2}, {"method": "llm"},
                     {"candidate_methods": ["naive", "naive"]}):
        with pytest.raises(ValueError):
            forecast_manufacturing(snapshot, **{**kwargs, **override})
    gapped = adapt_fixture(*[r for i, r in enumerate(synthetic_history()) if i != 4])
    with pytest.raises(ValueError, match="contiguous"):
        forecast_manufacturing(gapped, **kwargs, method="ma3")


def test_forged_canonical_value_provenance_or_config_rejected_by_forecast_replay():
    snapshot = adapt_fixture(*synthetic_history())
    first = snapshot.rows[0]
    changed_row = replace(first, total=Decimal("999"))
    bad_proof = replace(first, provenance=replace(first.provenance, row_number=999))
    bad_hash = replace(first, provenance=replace(first.provenance, source_row_sha256="0" * 64))
    bad_cells = replace(first, provenance=replace(first.provenance, source_cells=()))
    variants = [replace(snapshot, rows=(row, *snapshot.rows[1:])) for row in (changed_row, bad_proof, bad_hash, bad_cells)]
    variants.extend([replace(snapshot, config=AdapterConfig("{}")), replace(snapshot, source_sha256="0" * 64),
                     replace(snapshot, columns=tuple(reversed(snapshot.columns)))])
    for forged in variants:
        with pytest.raises(ManufacturingAdapterError):
            forecast_manufacturing(forged, factory=FACTORY, product=PRODUCT, specification=SPEC, cutoff_month="2026-06")


@pytest.mark.parametrize("field,value", [("unitcost", 6), ("unitcost", 6.0),
                                         ("unitcost", Decimal("6.0")), ("output", 10.0)])
def test_forecast_replay_rejects_equal_but_type_or_scale_changed_values(field, value):
    snapshot = adapt_fixture(*synthetic_history())
    first = snapshot.rows[0]
    assert getattr(first, field) == value  # equality alone must not bypass provenance
    changed = replace(snapshot, rows=(replace(first, **{field: value}), *snapshot.rows[1:]))
    with pytest.raises(ManufacturingAdapterError, match="noncanonical|integrity"):
        forecast_manufacturing(changed, factory=FACTORY, product=PRODUCT, specification=SPEC,
                               cutoff_month="2026-06")


def test_forecast_reports_non_cny_currency_without_implicit_box_or_cny_conversion():
    cfg = synthetic_config("kg")
    cfg["currency"] = {"source_column": "fixture_currency", "target": "USD",
                       "rates": [{"source": "USD", "factor": "1"}]}
    rows = synthetic_history(unit="kg")
    for row in rows:
        row["fixture_currency"] = "USD"
    snapshot = adapt_fixture(*rows, config=cfg)
    result = forecast_manufacturing(snapshot, factory=FACTORY, product=PRODUCT, specification=SPEC,
                                    cutoff_month="2026-06").to_dict()
    assert result["measurement"] == {"currency": "USD", "quantity_unit": "kg", "unit_cost_unit": "USD/kg"}
    assert result["forecast"]["unit_cost_exact"] == "11"


def test_bad_future_or_unselected_factory_row_is_not_silently_filtered():
    for row in (synthetic_row(month="2027-01", total="1"), synthetic_row(factory=PEER, total="1")):
        with pytest.raises(ManufacturingAdapterError, match="close exactly"):
            adapt_fixture(*synthetic_history(), row)


def test_no_file_or_network_calls_during_plain_adapter(monkeypatch):
    import builtins
    def denied(*args, **kwargs):
        raise AssertionError("Pure adapter may not open files")
    monkeypatch.setattr(builtins, "open", denied)
    monkeypatch.setattr(Path, "open", denied)
    cfg = parse_adapter_config(json.dumps(synthetic_config()))
    assert adapt_fixture(config=cfg).rows[0].factory == FACTORY
