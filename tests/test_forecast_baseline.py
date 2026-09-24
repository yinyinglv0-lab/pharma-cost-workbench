"""Auditable forecast contract; in-memory fixtures and read-only CSV loader only."""
from copy import deepcopy
from decimal import Decimal
from fractions import Fraction
import json
import math
from pathlib import Path
import socket
import sys

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from enterprise.forecast import ELEMENTS, forecast_baseline


FACTORY = "中药一厂"
PRODUCT = "测试产品"
SPEC = "S"


def cost_row(month, material=1, labor=2, overhead=3, *, factory=FACTORY, product=PRODUCT,
             specification=SPEC, volume=100):
    values = [Decimal(str(value)) for value in (material, labor, overhead)]
    unit = sum(values)
    return {"工厂": factory, "产品名称": product, "产品规格": specification, "月份": month,
            **dict(zip(ELEMENTS.values(), map(str, values))),
            "单位成本(元/盒)": str(unit), "产量(盒)": volume,
            "总成本(元)": str(unit * Decimal(str(volume)))}


def monthly_rows(count=6, year=2026):
    return [cost_row(f"{year}-{index:02d}", index) for index in range(1, count + 1)]


def tables(*rows):
    return {"cost26": pd.DataFrame(rows), "material": pd.DataFrame(), "budget": pd.DataFrame()}


def forecast(data=None, **kwargs):
    options = {"factory": FACTORY, "product": PRODUCT, "specification": SPEC, "cutoff_month": "2026-06"}
    options.update(kwargs)
    return forecast_baseline(tables(*monthly_rows()) if data is None else data, **options)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("forecast tests must not connect to a model, RPA, or network")
    monkeypatch.setattr(socket.socket, "connect", forbidden)


def assert_closes(value):
    assert value["unit_cost"] == sum(value["elements"].values())
    assert Fraction(value["unit_cost_exact"]) == sum(map(Fraction, value["elements_exact"].values()))


def test_default_schema_forecast_closure_and_no_total_or_future_volume():
    result = forecast()
    assert result["schema_version"] == "cost-forecast/1.0"
    assert result["method"] == "naive"
    assert result["forecast"]["unit_cost"] == 11
    assert result["forecast"]["elements"] == {"材料": 6, "人工": 2, "制费": 3}
    assert result["target_months"] == ["2026-07"]
    assert result["method_training_months"] == ["2026-06"]
    assert result["forecast"]["target_month"] == "2026-07"
    assert_closes(result["forecast"])
    assert "total_cost" not in result["forecast"] and "volume" not in result["forecast"]
    assert result["interval"] == {"status": "insufficient_calibration", "lower": None,
                                  "upper": None, "coverage": None, "n_residuals": 3}
    assert result["method_selection"]["status"] == "not_performed"
    assert result["method_selection"]["unbiased_selection_estimate"] is False
    json.dumps(result, ensure_ascii=False, allow_nan=False)


@pytest.mark.parametrize("method", ["naive", "ma3"])
def test_fractional_components_close_for_forecast_and_every_fold(method):
    data = tables(*(cost_row(f"2026-{index:02d}", ".1", str(Decimal(index) / 10), ".3")
                    for index in range(1, 7)))
    result = forecast(data, method=method)
    assert_closes(result["forecast"])
    for candidate in result["backtests"].values():
        for fold in candidate["folds"]:
            assert_closes(fold["prediction"])
            assert_closes(fold["actual"])


def test_ma3_unrounded_repeating_mean_and_used_months():
    data = tables(cost_row("2026-01", ".1", ".2", ".3"), cost_row("2026-02", ".2", ".2", ".3"),
                  cost_row("2026-03", ".2", ".2", ".3"))
    result = forecast(data, cutoff_month="2026-03", method="ma3")
    assert result["forecast"]["elements"]["材料"] == pytest.approx(1 / 6)
    assert result["forecast"]["elements_exact"]["材料"] == "1/6"
    assert result["forecast"]["unit_cost_exact"] == "2/3"
    assert result["method_training_months"] == ["2026-01", "2026-02", "2026-03"]
    assert result["interval"]["n_residuals"] == 0
    assert_closes(result["forecast"])


def test_gap_boundary_is_reported_and_only_cutoff_segment_is_used():
    data = {"cost25": pd.DataFrame(monthly_rows(year=2025)), "cost26": pd.DataFrame(monthly_rows())}
    result = forecast(data)
    assert result["training_months"] == [f"2026-{index:02d}" for index in range(1, 7)]
    assert result["gaps"] == [{"after_month": "2025-06", "before_month": "2026-01",
                               "missing_months": [f"2025-{index:02d}" for index in range(7, 13)]}]
    assert all(month.startswith("2026") for method in result["backtests"].values()
               for fold in method["folds"] for month in fold["training_months"])
    january = forecast(data, cutoff_month="2026-01")
    assert january["training_months"] == ["2026-01"]
    assert january["forecast"]["unit_cost"] == 6
    assert all(candidate["sample_count"] == 0 for candidate in january["backtests"].values())
    with pytest.raises(ValueError, match="3 contiguous"):
        forecast(data, cutoff_month="2026-02", method="ma3")


def test_calendar_adjacency_includes_december_to_january():
    data = tables(cost_row("2025-11", 100), cost_row("2025-12", 2), cost_row("2026-01", 4))
    result = forecast(data, cutoff_month="2026-01", method="ma3")
    assert result["training_months"] == ["2025-11", "2025-12", "2026-01"]
    assert result["forecast"]["elements"]["材料"] == pytest.approx(106 / 3)
    assert result["gaps"] == []
    december = forecast(data, cutoff_month="2025-12")
    assert december["target_months"] == ["2026-01"]
    assert december["forecast"]["elements"]["材料"] == 2


def test_internal_gap_does_not_supply_ma3_history_or_false_fold():
    data = tables(cost_row("2026-01", 99), cost_row("2026-03", 3), cost_row("2026-04", 4),
                  cost_row("2026-05", 5), cost_row("2026-06", 6))
    result = forecast(data, method="ma3")
    assert result["training_months"] == ["2026-03", "2026-04", "2026-05", "2026-06"]
    assert result["forecast"]["elements"]["材料"] == 5
    assert result["backtest_policy"]["common_origins"] == ["2026-05"]
    assert result["backtest_policy"]["common_target_months"] == ["2026-06"]
    assert result["gaps"][0]["missing_months"] == ["2026-02"]
    with pytest.raises(ValueError, match="cutoff_month must have"):
        forecast(data, cutoff_month="2026-02")


def test_common_origins_and_hand_computed_metrics():
    result = forecast()
    assert result["backtest_policy"]["common_origins"] == ["2026-03", "2026-04", "2026-05"]
    assert result["backtest_policy"]["common_target_months"] == ["2026-04", "2026-05", "2026-06"]
    for method, expected_error in [("naive", -1), ("ma3", -2)]:
        candidate = result["backtests"][method]
        assert candidate["sample_count"] == len(candidate["folds"]) == 3
        assert candidate["mae"] == candidate["rmse"] == abs(expected_error)
        assert candidate["mape"] == pytest.approx(sum(abs(expected_error) / actual * 100
                                                      for actual in (9, 10, 11)) / 3)
        assert candidate["metrics"]["材料"]["mae"] == abs(expected_error)
        assert candidate["metrics"]["人工"]["rmse"] == 0
        for index, fold in enumerate(candidate["folds"], start=4):
            assert fold["origin_month"] == f"2026-{index - 1:02d}"
            assert fold["target_month"] == f"2026-{index:02d}"
            assert max(fold["training_months"]) == fold["origin_month"] < fold["target_month"]
            assert fold["errors"]["unit_cost"] == expected_error
            assert fold["absolute_errors"]["unit_cost"] == abs(expected_error)
            assert fold["absolute_percentage_errors"]["unit_cost"] == pytest.approx(abs(expected_error) / (index + 5) * 100)
            replay = forecast(cutoff_month=fold["origin_month"], method=method)
            assert fold["prediction"] == {key: value for key, value in replay["forecast"].items()
                                           if key != "target_month"}
            assert fold["training_hash"] == replay["provenance"]["training_hash"]


def test_rmse_uses_squared_errors_not_mae():
    data = tables(*(cost_row(f"2026-{index:02d}", value, 0, 0)
                    for index, value in enumerate((1, 1, 1, 2, 6, 4), start=1)))
    candidate = forecast(data)["backtests"]["naive"]
    assert [fold["errors"]["unit_cost"] for fold in candidate["folds"]] == [-1, -4, 2]
    assert candidate["mae"] == pytest.approx(7 / 3)
    assert candidate["rmse"] == pytest.approx(math.sqrt(7))


def test_default_stays_naive_even_when_ma3_backtest_is_better():
    data = tables(*(cost_row(f"2026-{index:02d}", value, 0, 0)
                    for index, value in enumerate((1, 4, 1, 4, 1, 4), start=1)))
    result = forecast(data)
    assert result["backtests"]["ma3"]["mae"] < result["backtests"]["naive"]["mae"]
    assert result["method"] == result["method_selection"]["selected_method"] == "naive"
    assert result["forecast"]["unit_cost"] == 4


def test_single_candidate_uses_its_own_common_origins_without_coverage_claim():
    result = forecast(candidate_methods=("naive",))
    assert result["backtest_policy"]["common_origins"] == [f"2026-{index:02d}" for index in range(1, 6)]
    assert result["backtests"]["naive"]["sample_count"] == 5
    assert result["interval"] == {"status": "not_calibrated", "lower": None,
                                  "upper": None, "coverage": None, "n_residuals": 5}
    ma3 = forecast(method="ma3", candidate_methods=("naive",))
    assert ma3["backtest_policy"]["candidate_methods"] == ["naive"]
    assert ma3["backtest_policy"]["evaluated_methods"] == ["naive", "ma3"]
    assert ma3["interval"]["n_residuals"] == 3
    assert {item["sample_count"] for item in ma3["backtests"].values()} == {3}


def test_future_rows_never_change_point_training_hash_gaps_or_backtests():
    base = tables(*monthly_rows())
    original = forecast(base)
    future = pd.DataFrame([cost_row("2026-07", 90000), cost_row("2027-03", 0)])
    extended = {**base, "cost26": pd.concat([base["cost26"], future], ignore_index=True)}
    assert forecast(extended) == original


def test_future_target_mutation_does_not_change_earlier_fold_predictions():
    data = tables(*monthly_rows())
    original = forecast(data)
    changed_rows = monthly_rows()
    changed_rows[-1] = cost_row("2026-06", 999)
    changed = forecast(tables(*changed_rows))
    for method in ("naive", "ma3"):
        before, after = original["backtests"][method]["folds"], changed["backtests"][method]["folds"]
        assert before[:2] == after[:2]
        assert before[-1]["prediction"] == after[-1]["prediction"]
        assert before[-1]["training_hash"] == after[-1]["training_hash"]
        assert before[-1]["actual"] != after[-1]["actual"]
        assert before[-1]["errors"] != after[-1]["errors"]
    assert original["provenance"]["training_hash"] != changed["provenance"]["training_hash"]


def test_history_before_gap_cannot_affect_training_hash_prediction_or_backtest():
    base = tables(*monthly_rows())
    original = forecast(base)
    base["cost25"] = pd.DataFrame([cost_row("2025-06", 90000)])
    with_old_segment = forecast(base)
    for key in ("forecast", "backtests", "training_months"):
        assert with_old_segment[key] == original[key]
    assert with_old_segment["provenance"]["training_hash"] == original["provenance"]["training_hash"]
    assert len(with_old_segment["gaps"]) == 1


def test_zero_actuals_produce_null_mape_but_finite_mae_rmse():
    data = tables(*(cost_row(f"2026-{index:02d}", 0, 0, 0, volume=0) for index in range(1, 7)))
    result = forecast(data)
    assert result["forecast"]["unit_cost"] == 0
    for candidate in result["backtests"].values():
        assert candidate["sample_count"] == 3
        assert candidate["mae"] == candidate["rmse"] == 0
        assert candidate["mape"] is None
        assert candidate["mape_defined_count"] == 0
        assert all(set(fold["absolute_percentage_errors"].values()) == {None} for fold in candidate["folds"])
    json.dumps(result, allow_nan=False)


def test_any_zero_actual_nulls_aggregate_mape_without_dropping_samples():
    rows = [cost_row(f"2026-{index:02d}", value, 0, 0)
            for index, value in enumerate((1, 1, 1, 0, 2, 3), start=1)]
    for candidate in forecast(tables(*rows))["backtests"].values():
        assert candidate["sample_count"] == 3
        assert candidate["mape"] is None and candidate["mape_defined_count"] == 2
        assert candidate["folds"][0]["absolute_percentage_errors"]["unit_cost"] is None
        assert candidate["folds"][1]["absolute_percentage_errors"]["unit_cost"] is not None


@pytest.mark.parametrize("column", [*ELEMENTS.values(), "单位成本(元/盒)", "产量(盒)", "总成本(元)"])
@pytest.mark.parametrize("value", [float("nan"), float("inf"), -1, "not-a-number", None, True])
def test_nonfinite_negative_and_malformed_values_are_rejected(column, value):
    data = tables(*monthly_rows())
    data["cost26"][column] = data["cost26"][column].astype(object)
    data["cost26"].loc[0, column] = value
    with pytest.raises(ValueError, match="finite nonnegative"):
        forecast(data)


@pytest.mark.parametrize("month", ["2026-1", "2026-00", "2026-13", "2026-01-01", "0000-01", "2026/01", None, 202601])
def test_malformed_source_month_is_rejected(month):
    data = tables(*monthly_rows())
    data["cost26"].loc[0, "月份"] = month
    with pytest.raises(ValueError):
        forecast(data)


@pytest.mark.parametrize("column", ["工厂", "产品名称", "产品规格"])
def test_missing_identity_is_rejected(column):
    data = tables(*monthly_rows())
    data["cost26"].loc[0, column] = None
    with pytest.raises(ValueError, match="nonempty string"):
        forecast(data)


@pytest.mark.parametrize("split_tables", [False, True])
def test_duplicate_identity_is_rejected_including_across_table_names(split_tables):
    rows = monthly_rows()
    data = tables(*rows)
    if split_tables:
        data["cost25"] = pd.DataFrame([rows[0]])
    else:
        data["cost26"] = pd.concat([data["cost26"], data["cost26"].iloc[[0]]])
    with pytest.raises(ValueError, match="duplicate factory/product/specification/month"):
        forecast(data)


def test_factory_product_and_specification_are_all_isolated():
    base = forecast()
    other = [cost_row("2026-06", 999, factory="中药二厂"),
             cost_row("2026-06", 999, specification="S2"), cost_row("2026-06", 999, product="其他产品")]
    result = forecast(tables(*monthly_rows(), *other))
    assert result == base
    for options in ({"factory": "不存在"}, {"product": "不存在"}, {"specification": "不存在"}):
        with pytest.raises(ValueError, match="exact factory/product/specification"):
            forecast(**options)


@pytest.mark.parametrize("column", ["产品规格", "工厂", "月份", "直接人工(元/盒)", "单位成本(元/盒)"])
def test_missing_columns_fail_closed(column):
    data = tables(*monthly_rows())
    data["cost26"] = data["cost26"].drop(columns=column)
    with pytest.raises(ValueError, match="missing required columns"):
        forecast(data)


def test_duplicate_columns_and_nonclosing_source_fail_closed():
    frame = tables(*monthly_rows())["cost26"]
    duplicated = pd.concat([frame, frame[["直接人工(元/盒)"]]], axis=1)
    with pytest.raises(ValueError, match="duplicate column"):
        forecast({"cost26": duplicated})
    frame.loc[0, "单位成本(元/盒)"] = "99"
    with pytest.raises(ValueError, match="components do not sum"):
        forecast({"cost26": frame})


def test_float_input_roundoff_is_tolerated_without_balancing_material_errors():
    row = cost_row("2026-06", ".1", ".2", ".3")
    row["单位成本(元/盒)"] = 0.1 + 0.2 + 0.3
    assert_closes(forecast(tables(row))["forecast"])
    row["单位成本(元/盒)"] = ".600001"
    with pytest.raises(ValueError, match="components do not sum"):
        forecast(tables(row))


@pytest.mark.parametrize("options", [
    {"horizon": 0}, {"horizon": 2}, {"horizon": -1}, {"horizon": True}, {"horizon": 1.0},
    {"method": "auto"}, {"method": None}, {"candidate_methods": ()},
    {"candidate_methods": "naive"}, {"candidate_methods": ("naive", "naive")},
    {"candidate_methods": ("arima",)}, {"cutoff_month": "2026-6"},
    {"cutoff_month": "9999-12"}, {"factory": ""}, {"product": None}, {"specification": " S "},
    {"snapshot_meta": []}, {"snapshot_meta": {"bad": float("nan")}},
])
def test_invalid_parameters_raise_value_error(options):
    with pytest.raises(ValueError):
        forecast(**options)


@pytest.mark.parametrize("data", [{}, {"cost26": pd.DataFrame()}, {"cost26": []}, None, []])
def test_empty_or_malformed_table_mapping_is_rejected(data):
    with pytest.raises(ValueError):
        forecast_baseline(data, factory=FACTORY, product=PRODUCT, specification=SPEC, cutoff_month="2026-06")


def test_historical_volume_not_required_or_used_for_unit_prediction():
    data = tables(*monthly_rows())
    expected = forecast(data)
    data["cost26"] = data["cost26"].drop(columns=["产量(盒)", "总成本(元)"])
    assert forecast(data) == expected


def test_snapshot_attrs_hashes_and_inputs_are_copied_without_mutation():
    data = tables(*monthly_rows())
    data["cost26"]["_source_hash"] = "c" * 64
    data["cost26"].attrs.update(cost_snapshot_hash="a" * 64, cost_revision=4, source_hash="b" * 64)
    metadata = {"revision": 4, "snapshot": {"id": "supplied"}}
    before = deepcopy(data)
    result = forecast(data, snapshot_meta=metadata)
    provenance = result["provenance"]
    assert provenance["snapshot_meta"] == metadata
    assert provenance["source_hash"] == "a" * 64
    assert provenance["source_hashes"] == ["a" * 64, "b" * 64]
    assert provenance["training_source_hashes"] == ["c" * 64]
    assert provenance["table_attrs"]["cost26"]["cost_revision"] == 4
    assert provenance["method"] == "naive" and provenance["method_version"] == "cost-baseline/1.0"
    assert provenance["cutoff_month"] == "2026-06" and len(provenance["training_hash"]) == 64
    for key in data:
        pd.testing.assert_frame_equal(data[key], before[key])
        assert data[key].attrs == before[key].attrs
    metadata["snapshot"]["id"] = "later-change"
    data["cost26"].attrs["cost_revision"] = 99
    assert provenance["snapshot_meta"]["snapshot"]["id"] == "supplied"
    assert provenance["table_attrs"]["cost26"]["cost_revision"] == 4


def test_training_hash_is_stable_under_order_dtype_and_method():
    data = tables(*monthly_rows())
    original = forecast(data)
    reordered = data["cost26"].iloc[::-1].copy()
    reordered.index = range(100, 106)
    for column in [*ELEMENTS.values(), "单位成本(元/盒)"]:
        reordered[column] = reordered[column].astype(float)
    result = forecast({"arbitrary_summary": reordered}, method="ma3")
    assert result["provenance"]["training_hash"] == original["provenance"]["training_hash"]
    assert result["training_months"] == original["training_months"]


def test_source_hash_is_none_without_attrs_and_conflicting_snapshot_is_rejected():
    assert forecast()["provenance"]["source_hash"] is None
    data = tables(*monthly_rows())
    data["cost26"].attrs["cost_snapshot_hash"] = "a" * 64
    data["cost25"] = pd.DataFrame([cost_row("2025-06")])
    data["cost25"].attrs["cost_snapshot_hash"] = "b" * 64
    with pytest.raises(ValueError, match="conflicting cost_snapshot_hash"):
        forecast(data)


def test_large_finite_costs_have_finite_metrics_without_squared_overflow():
    rows = [cost_row(f"2026-{index:02d}", value, 0, 0, volume=1)
            for index, value in enumerate(("1e200", "2e200", "3e200", "4e200", "5e200", "6e200"), start=1)]
    result = forecast(tables(*rows))
    assert result["backtests"]["naive"]["rmse"] == pytest.approx(1e200)
    assert result["backtests"]["ma3"]["mae"] == pytest.approx(2e200)
    json.dumps(result, allow_nan=False)


def test_real_legacy_loader_cost_summaries_are_read_only_and_gap_aware(monkeypatch):
    from dashboard import data_layer
    import enterprise.cost_imports as imports

    def forbidden(*args, **kwargs):
        raise AssertionError("read-only real-loader forecast must not open a managed database")
    monkeypatch.setattr(imports.CostRepository, "_connect", forbidden)
    monkeypatch.setattr(imports, "active_tables", forbidden)
    # Pin the real loader to the supplied root CSVs, excluding uploaded revisions.
    monkeypatch.setattr(data_layer, "DATA_DIR", ROOT)
    monkeypatch.setattr(data_layer, "_TABLE_PATTERNS", {
        key: [data_layer._FALLBACK_NAMES[key]] for key in ("cost25", "cost26", "erchang25", "erchang26")
    })
    required = [ROOT / name for name in data_layer._FALLBACK_NAMES.values()
                if "成本汇总" in name]
    if not all(path.is_file() for path in required):
        pytest.skip("workspace source CSVs are not present")
    data = data_layer.load_legacy_tables(fallback=False)
    before = deepcopy(data)
    for name in ("cost26", "erchang26"):
        for _, row in data[name][data[name]["月份"] == "2026-06"].iterrows():
            result = forecast_baseline(data, factory=row["工厂"], product=row["产品名称"],
                                       specification=row["产品规格"], cutoff_month="2026-06")
            assert result["training_months"] == [f"2026-{index:02d}" for index in range(1, 7)]
            assert result["forecast"]["unit_cost"] == pytest.approx(float(row["单位成本(元/盒)"]))
            assert result["target_months"] == ["2026-07"]
            assert result["backtest_policy"]["common_target_months"] == ["2026-04", "2026-05", "2026-06"]
            assert result["gaps"][0]["missing_months"] == [f"2025-{index:02d}" for index in range(7, 13)]
            assert result["interval"]["n_residuals"] == 3
            assert result["provenance"]["training_source_hashes"] == [row["_source_hash"]]
            assert_closes(result["forecast"])
            json.dumps(result, ensure_ascii=False, allow_nan=False)
    for name in data:
        pd.testing.assert_frame_equal(data[name], before[name])
