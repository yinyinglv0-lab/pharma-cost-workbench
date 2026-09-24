"""Offline projection acceptance: canonical authority, native shared consumers.

Fixtures explicitly declare SIMULATION. No models, network, database, or CSV
attribution/benchmark kernels are used by the projection under test.
"""
from copy import deepcopy
import csv
from decimal import Decimal, localcontext
import hashlib
import io
import json
from pathlib import Path
import socket
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from enterprise.manufacturing_projection import (
    ELEMENTS, ManufacturingProjectionError, project_manufacturing_analysis,
)
from enterprise.manufacturing_runtime import build_manufacturing_runtime, TABLE_FAMILIES


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("projection must not access network or paid models")
    monkeypatch.setattr(socket.socket, "connect", denied)


def fixture(industry="machinery"):
    folder = ROOT / "config" / "manufacturing_examples" / industry
    profile = json.loads((folder / "domain.json").read_text(encoding="utf-8-sig"))
    adapter = json.loads((folder / "adapter.json").read_text(encoding="utf-8-sig"))
    tables = {}
    for family in TABLE_FAMILIES:
        data = (folder / (family + ".csv")).read_bytes()
        rows = list(csv.reader(io.StringIO(data.decode("utf-8-sig"), newline="")))
        tables[family] = {"columns": rows[0], "rows": rows[1:], "source_id": industry + "/" + family + ".csv",
            "source_sha256": hashlib.sha256(data).hexdigest()}
    return profile, adapter, tables


def analyze(profile=None, adapter=None, tables=None, *, month="2026-06", industry="machinery"):
    p, a, t = fixture(industry)
    profile, adapter, tables = profile or p, adapter or a, tables or t
    runtime = build_manufacturing_runtime(profile, adapter, tables=tables, periods=["2026-05", "2026-06"])
    product = profile["products"][0]
    canonical = runtime.analysis(product=product["name"], specification=product["specification"], month=month)
    return canonical, profile, project_manufacturing_analysis(canonical, profile)


def cell(table, column, value, row):
    table["rows"][row][table["columns"].index(column)] = value


def records(source):
    return source.get("records") or [source]


@pytest.mark.parametrize("industry", ["pharma", "machinery", "auto_parts", "chemicals", "electronics"])
def test_five_industries_use_same_contract_and_actual_display_identities(industry):
    canonical, profile, result = analyze(industry=industry)
    assert set(result) == {"attribution_payload", "benchmark_payload", "sources", "narrative_config", "charts"}
    a, b = result["attribution_payload"], result["benchmark_payload"]
    assert a["domain_config"] == b["domain_config"] == profile
    assert set(a["facts"]["elements"]) == set(ELEMENTS.values())
    assert [row["element"] for row in b["facts"]["elements"]] == list(ELEMENTS.values())
    assert result["narrative_config"]["reporting_unit"] == canonical["measurement"]["quantity_unit"]
    assert result["narrative_config"]["home_label"] == profile["factories"]["home"]
    assert result["narrative_config"]["peer_label"] == profile["factories"]["peer"]
    assert result["narrative_config"]["element_labels"]["材料"] == profile["labels"]["material"]
    dumped = json.dumps(result, ensure_ascii=False, allow_nan=False)
    assert "中药一厂" not in dumped and "中药二厂" not in dumped and "元/盒" not in dumped
    assert a["data_classification"] == b["data_classification"] == "simulation"


def test_exact_authoritative_amount_bridges_and_ratio_operands():
    canonical, _, result = analyze()
    a, b = result["attribution_payload"]["facts"], result["benchmark_payload"]["facts"]
    assert a["current"]["volume"] == 120 and a["previous"]["volume"] == 100
    assert a["current"]["total_cost"] == 2760 and a["previous"]["total_cost"] == 2000
    assert a["amount_delta"] == 760 and b["normalized_amount"] == 480
    with localcontext() as context:
        context.prec = 512
        for key, label in ELEMENTS.items():
            row, bridge = a["elements"][label], canonical["period"]["elements"][key]
            for field in ("unit_before", "unit_after", "amount_before", "amount_after", "amount_delta", "volume_effect", "unit_effect"):
                assert Decimal(row[field + "_exact"]) == Decimal(bridge[field])
                assert Decimal(str(row[field])) == Decimal(bridge[field])
            assert Decimal(row["volume_effect_exact"]) + Decimal(row["unit_effect_exact"]) == Decimal(row["amount_delta_exact"])
            assert row["contribution_ratio"]["numerator"] == bridge["contribution_pct"]["numerator"]
            assert row["contribution_ratio"]["denominator"] == bridge["contribution_pct"]["denominator"]
            assert row["detail"]
        assert sum(Decimal(row["normalized_amount_exact"]) for row in b["elements"]) == Decimal(b["normalized_amount_exact"])
        assert all(branch["complete"] for branch in b["paired_drilldown"].values())
        assert all(Decimal(branch["unallocated_normalized_amount_exact"]) == 0 for branch in b["paired_drilldown"].values())


def test_raw_primary_precision_is_not_two_decimal_display_rounding():
    profile, adapter, tables = fixture()
    # CNY -> USD remains explicit exact configured conversion; detail rows already
    # use the canonical currency, so no legacy CSV cost kernel is involved.
    rate = Decimal("1.000000000000000000000000000001")
    profile["currency"] = "USD"
    adapter["currency"]["target"] = "USD"
    adapter["currency"]["rates"][0]["factor"] = str(rate)
    for metric in profile["reference_metrics"]:
        if metric["calculation"] == "unit_cost":
            metric["unit"] = "USD/" + profile["reporting_unit"]
    for family in ("materials", "labor", "overhead"):
        table = tables[family]
        for row in table["rows"]:
            row[table["columns"].index("currency")] = "USD"
            for name in ("amount", "unitcost", "unit_price", "hourly_rate"):
                if name in table["columns"]:
                    pos = table["columns"].index(name)
                    if row[pos]:
                        with localcontext() as context:
                            context.prec = 100
                            row[pos] = str(Decimal(row[pos]) * rate)
    canonical, _, result = analyze(profile, adapter, tables)
    a, b = result["attribution_payload"]["facts"], result["benchmark_payload"]["facts"]
    assert isinstance(a["current"]["total_cost"], str)
    assert a["current"]["total_cost"] == canonical["current"]["total"]
    assert b["normalized_amount"] == b["normalized_amount_exact"]
    assert Decimal(b["normalized_amount_exact"]) != Decimal(b["normalized_amount_display"].replace(",", ""))


def test_provenance_has_true_element_membership_source_row_columns_and_stable_ids():
    canonical, profile, first = analyze()
    second = project_manufacturing_analysis(deepcopy(canonical), deepcopy(profile))
    assert first == second
    for name, kind in (("attribution", "accounting_fact"), ("benchmark", "data_fact")):
        sources = first["sources"][name]
        assert len({source["id"] for source in sources}) == len(sources)
        for source in sources:
            assert source["kind"] == kind
            assert source["elements"] and set(source["elements"]) <= set(ELEMENTS.values())
            assert source["id"].startswith("F") and len(source["id"]) == 41
            for record in records(source["source"]):
                assert record["file"] and record["row"] > 0 and record["line"] is None
                assert len(record["source_sha256"]) == len(record["source_row_sha256"]) == 64
                assert record["columns_basis"] == "original_source_columns"
                assert set(record["columns"]) == set(record["fields"])
                assert record["key"]["product"] == canonical["scope"]["product"]
                if record["table"] == "materials":
                    assert source["elements"] == ["材料"]
                if record["table"] == "labor":
                    assert source["elements"] == ["人工"]
                if record["table"] == "overhead":
                    assert source["elements"] == ["制费"]
    first["sources"]["attribution"][0]["source"]["mutated"] = True
    assert project_manufacturing_analysis(canonical, profile) == second


def test_source_aliases_are_preserved_not_renamed_into_pharmacy_columns():
    profile, adapter, tables = fixture()
    # Change original summary CSV headings and corresponding declarative adapter.
    mapping = adapter["columns"]
    original = mapping["material"]
    alias = "alloy_input_cost_CNY_each"
    mapping["material"] = alias
    for family in ("actual", "budget"):
        columns = tables[family]["columns"]
        columns[columns.index(original)] = alias
    canonical, _, result = analyze(profile, adapter, tables)
    actual = [record for source in result["sources"]["attribution"] for record in records(source["source"]) if record["table"] == "actual"]
    assert actual and all(alias in record["fields"] for record in actual)
    assert all("直接材料(元/盒)" not in record["fields"] for record in actual)
    assert result["attribution_payload"]["facts"]["elements"]["材料"]["unit_after"] == 12


def test_never_infer_material_quantity_from_bom_and_keep_physical_bridge_separate():
    canonical, _, result = analyze()
    details = result["attribution_payload"]["facts"]["elements"]["材料"]["detail"]
    observed = [row for row in details if row["observed_quantity_price_bridge"]["available"]]
    absent = [row for row in details if not row["observed_quantity_price_bridge"]["available"]]
    assert len(observed) == len(absent) == 1
    physical = observed[0]["observed_quantity_price_bridge"]
    assert Decimal(physical["quantity_effect"]) + Decimal(physical["price_effect"]) == Decimal(physical["amount_delta"])
    assert absent[0]["physical_observations"]["current"]["quantity"] is None
    assert absent[0]["physical_observations"]["current"]["unit_price"] is None
    assert absent[0]["current_observed_quantity_per_reporting_unit"]["available"] is False
    assert canonical["domain_context"]["product"]["bom"]


def test_observed_hours_bridge_is_not_an_invented_employee_rate():
    canonical, _, result = analyze()
    labor = result["attribution_payload"]["facts"]["elements"]["人工"]["labor_factors"]
    assert labor["available"] and labor["unit_available"]
    assert labor["hours_effect"] == 80 and labor["rate_effect"] == 120
    assert labor["amount_delta"] == 200
    assert labor["observed_hours_rate_bridge"] == canonical["details"]["labor"]["observed_hours_rate_bridge"]
    assert "不是个人时薪" in labor["basis"]
    assert labor["hours_per_unit_before"] == 1
    assert labor["quantity_unit"] == "piece"


def test_paired_labor_without_hours_remains_accounting_only():
    profile, adapter, tables = fixture()
    for index in range(len(tables["labor"]["rows"])):
        cell(tables["labor"], "hours", "", index)
        cell(tables["labor"], "hourly_rate", "", index)
    _, _, result = analyze(profile, adapter, tables)
    labor = result["attribution_payload"]["facts"]["elements"]["人工"]["labor_factors"]
    assert not labor["available"]
    branch = result["benchmark_payload"]["facts"]["paired_drilldown"]["人工"]
    assert branch["complete"] and branch["rows"][0]["status"] == "paired"
    assert branch["rows"][0]["home"]["metrics"]["cost_per_hour"] is None
    assert branch["rows"][0]["home"]["metrics"]["hours"] is None
    assert branch["rows"][0]["normalized_amount"] == 120


def test_first_period_preserves_observed_current_values_without_fake_history():
    canonical, _, result = analyze(month="2026-05")
    facts = result["attribution_payload"]["facts"]
    assert not facts["available"] and facts["previous"] is None
    assert facts["current"]["volume"] == 100 and facts["current"]["total_cost"] == 2000
    assert facts["amount_delta"] is None
    for key, label in ELEMENTS.items():
        row = facts["elements"][label]
        assert row["unit_before"] is None and row["amount_delta"] is None and row["contribution"] is None
        assert Decimal(str(row["unit_after"])) == Decimal(canonical["current"][key])
        assert all(item["status"] == "missing_before" for item in row["detail"])
    assert result["benchmark_payload"]["facts"]["available"]


def test_zero_totals_have_none_contributions_and_no_nan():
    profile, adapter, tables = fixture()
    for family, table in tables.items():
        fields = {"material", "labor", "overhead", "unitcost", "total"} if family in ("actual", "budget") else {"amount", "unitcost", "unit_price", "hourly_rate"}
        for row in table["rows"]:
            for field in fields & set(table["columns"]):
                pos = table["columns"].index(field)
                if row[pos]:
                    row[pos] = "0"
    _, _, result = analyze(profile, adapter, tables)
    assert all(row["contribution"] is None for row in result["attribution_payload"]["facts"]["elements"].values())
    assert all(row["contribution_pct"] is None for row in result["benchmark_payload"]["facts"]["elements"])
    assert "NaN" not in json.dumps(result, allow_nan=False)
    from enterprise.benchmark_ai import _zero_explanation, validate_explanations, grouped_model_context
    facts = result["benchmark_payload"]["facts"]
    candidate = {"elements": {row["element"]: _zero_explanation(row) for row in facts["elements"]}}
    assert validate_explanations(candidate, result["sources"]["benchmark"], facts) == []
    context = grouped_model_context(result["benchmark_payload"], result["sources"]["benchmark"])
    assert all(row["mode"] == "no_difference" for row in context["tasks_by_element"].values())


def test_negative_offsets_are_not_absolute_value_contributions():
    profile, adapter, tables = fixture()
    # Make home overhead cheaper than peer while material/labor gaps stay positive.
    for index, row in enumerate(tables["actual"]["rows"]):
        values = dict(zip(tables["actual"]["columns"], row))
        if values["factory"] == profile["factories"]["home"] and values["month"] == "2026-06":
            cell(tables["actual"], "overhead", "4", index)
            cell(tables["actual"], "unitcost", "21", index)
            cell(tables["actual"], "total", "2520", index)
    for index, row in enumerate(tables["overhead"]["rows"]):
        values = dict(zip(tables["overhead"]["columns"], row))
        if values["factory"] == profile["factories"]["home"] and values["month"] == "2026-06":
            # Two equal overhead detail rows in the supplied SIMULATION fixture.
            cell(tables["overhead"], "unitcost", "2", index)
            cell(tables["overhead"], "amount", "240", index)
    _, _, result = analyze(profile, adapter, tables)
    row = next(row for row in result["benchmark_payload"]["facts"]["elements"] if row["element"] == "制费")
    assert row["unit_gap"] == -1 and row["normalized_amount"] == -120 and row["contribution_pct"] == -50
    assert Decimal(str(result["attribution_payload"]["facts"]["elements"]["制费"]["contribution"])) < 0
    from attribution_gen import _model_context
    context = _model_context(result["attribution_payload"], result["sources"]["attribution"])
    assert context["tasks_by_element"]["制费"]["accounting_directions"]["unit_cost"] == "decrease"


def test_explicit_unpaired_detail_keeps_none_not_zero():
    canonical, profile, _ = analyze()
    canonical["peer_details"]["materials"] = []
    result = project_manufacturing_analysis(canonical, profile)
    branch = result["benchmark_payload"]["facts"]["paired_drilldown"]["材料"]
    assert not branch["complete"] and branch["paired_count"] == 0
    assert branch["coverage"]["peer"]["provided_count"] == 0
    assert all(row["peer"] is None and row["unit_gap"] is None and row["normalized_amount"] is None for row in branch["rows"])


def test_selected_scope_only_and_no_future_or_other_product_evidence():
    canonical, profile, result = analyze(month="2026-05")
    for mode, sources in result["sources"].items():
        for source in sources:
            assert source["scope"]["periods"] == ["2026-05"]
            for record in records(source["source"]):
                assert record["key"]["month"] == "2026-05"
                assert record["key"]["product"] == canonical["scope"]["product"]
    second = profile["products"][1]["name"]
    assert all(second not in json.dumps(source) for sources in result["sources"].values() for source in sources)
    assert all(source["kind"] not in ("document_basis", "observed_fact") for sources in result["sources"].values() for source in sources)


def test_shared_consumers_accept_context_provenance_narrative_and_candidates():
    _, profile, result = analyze()
    from attribution_gen import _model_context, _model_errors
    from enterprise.benchmark_ai import grouped_model_context, validate_explanations, _valid_source
    from enterprise.analysis_narrative import build_attribution_narrative, build_benchmark_narrative
    a, b = result["attribution_payload"], result["benchmark_payload"]
    sa, sb = result["sources"]["attribution"], result["sources"]["benchmark"]
    for source in sb:
        assert _valid_source(source["source"])
    ca = _model_context(a, sa, include_numeric=True)
    cb = grouped_model_context(b, sb, include_numeric=True)
    assert ca["observed_totals"]["current"]["volume"] == 120
    assert cb["comparison_totals"]["home_factory"] == profile["factories"]["home"]
    assert ca["domain_descriptors"]["factories"] == cb["domain_descriptors"]["factories"] == profile["factories"]
    hypotheses = {label: {"hypothesis": "该项已观察核算差额需要结合业务凭证核查，尚不能确认经营根因。",
        "recommendation": "请财务部核对现有核算记录，形成差异核对表。",
        "evidence_ids": [a["facts"]["elements"][label]["evidence_ids"][0]]} for label in ELEMENTS.values()}
    assert _model_errors({"elements": hypotheses}, sa, a["facts"]) == []
    benchmark_rows = {row["element"]: row for row in b["facts"]["elements"]}
    candidate = {"elements": {label: {**row, "claim_type": "hypothesis", "missing_evidence": ["原始业务凭证"],
        "evidence_ids": [benchmark_rows[label]["evidence_id"]]} for label, row in hypotheses.items()}}
    assert validate_explanations(candidate, sb, b["facts"]) == []
    na = build_attribution_narrative(a, sources=sa, config=result["narrative_config"])
    nb = build_benchmark_narrative(b, sources=sb, config=result["narrative_config"])
    assert len(na["sections"]) == len(nb["sections"]) == 3
    assert profile["labels"]["material"] in na["text"]
    assert profile["factories"]["home"] in nb["text"] and "CNY/piece" in nb["text"]
    assert "元/盒" not in na["text"] + nb["text"]


def test_charts_are_native_json_zero_based_unit_labelled_and_hash_bound():
    canonical, _, result = analyze()
    expected = hashlib.sha256(json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
    for chart in result["charts"].values():
        assert chart["source_fact_sha256"] == expected
        assert chart["option"]["yAxis"]["scale"] is False
        assert chart["option"]["yAxis"]["min"] == 0
        assert chart["option"]["yAxis"]["name"] in {"CNY", "CNY/piece"}
        assert all(series["type"] == "bar" for series in chart["option"]["series"])
        assert "function(" not in json.dumps(chart)
        assert chart["data_exact"]


def test_profile_mismatch_and_out_of_scope_rows_fail_closed():
    canonical, profile, _ = analyze()
    wrong = deepcopy(profile)
    wrong["version"] += ".changed"
    with pytest.raises(ManufacturingProjectionError, match="fingerprint"):
        project_manufacturing_analysis(canonical, wrong)
    canonical["details"]["materials"][0]["current"]["month"] = "2026-07"
    with pytest.raises(ManufacturingProjectionError, match="outside selected scope"):
        project_manufacturing_analysis(canonical, profile)


def test_stability_survives_json_key_order_and_detail_order_changes():
    canonical, profile, first = analyze()
    reordered = json.loads(json.dumps(canonical, sort_keys=True))
    reordered["details"]["materials"].reverse()
    reordered["details"]["overhead"].reverse()
    reordered["peer_details"]["materials"].reverse()
    reordered["peer_details"]["overhead"].reverse()
    second = project_manufacturing_analysis(reordered, profile)
    assert first["sources"] == second["sources"]
    assert first["attribution_payload"]["facts"] == second["attribution_payload"]["facts"]
    assert first["benchmark_payload"]["facts"] == second["benchmark_payload"]["facts"]


def test_valid_factory_cannot_be_substituted_for_comparison_role():
    canonical, profile, _ = analyze()
    canonical["details"]["labor"]["current"]["factory"] = profile["factories"]["peer"]
    with pytest.raises(ManufacturingProjectionError, match="comparison role"):
        project_manufacturing_analysis(canonical, profile)


def test_projection_never_calls_old_csv_kernels_or_loaders(monkeypatch):
    canonical, profile, expected = analyze()
    import attribution_facts
    import enterprise.benchmark as benchmark
    import enterprise.domain_profiles as profiles
    def denied(*args, **kwargs):
        raise AssertionError("projection may not reopen or recompute through old CSV kernel")
    monkeypatch.setattr(attribution_facts, "build_facts", denied)
    monkeypatch.setattr(benchmark, "build_benchmark", denied)
    monkeypatch.setattr(benchmark, "load_merged_tables", denied)
    monkeypatch.setattr(profiles, "load_domain_profile", denied)
    assert project_manufacturing_analysis(canonical, profile) == expected
