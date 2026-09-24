"""Portable offline tests of lossless provider transport; no private artifacts."""
from copy import deepcopy
import hashlib
import json
import socket

import pytest

from enterprise.prose_contract import (
    PROSE_MODE, PROVIDER_SCHEMA_VERSION, _fewshot, numeric_prose_diagnostics,
    prose_prompt, provider_prose_context, reconstruct_provider_context,
)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    monkeypatch.setattr(socket.socket, "connect", lambda *a, **k: (_ for _ in ()).throw(AssertionError("offline transport test")))


def transport_fixture(mode="attribution"):
    context, output = _fewshot(mode)
    common = {"id": "Rshared", "kind": "industry_reference", "category": "合成测试类",
              "metric": "类别定位", "scope": {"periods": ["2026-05"]},
              "source": {"file": "synthetic.csv", "record_number": 2, "sha256": "a"*64},
              "boundary": "仅作类别参照，不证明实际经营原因"}
    for element, task in context["tasks_by_element"].items():
        task["numeric_prose_instruction"] = "逐字复制所有必需完整句；来源ID仅进入引用数组。"
        task["reasoning_priority"] = "保留核算观察与并列对象；机制不能替代观察。"
        task["references"] = [deepcopy(common), {"id": {"材料": "Rmaterial", "人工": "Rlabor", "制费": "Roverhead"}[element],
            "kind": "market_reference", "scope": {"element": element}, "unit": "元/kg",
            "limitations": ["不等于工厂结算价"], "source": {"file": "synthetic.csv", "offset": 123}}]
        task["document_basis"] = [{"id": "Ksynthetic", "untrusted_excerpt": "这是完整知识原文，不能删改或充当本期事实。",
                                   "elements": [element], "scope": {"month": "2026-05"},
                                   "claim_boundary": "仅作核查方向", "limitations": ["需要源记录"]}]
        task["observed_costs"] = {"before": "1.0000000000001", "after": "1.05"}
        task["focus"] = {"tied_objects": ["甲项", "乙项"], "tied_evidence_ids": ["Fleft", "Fright"], "is_tied": True}
        task["eligible_evidence_ids"] = ["Fleft", "Fright", "Ksynthetic"]
    context["numeric_contract"] = {"read_only": True, "unit": "元/盒"}
    return context, output


def test_projection_roundtrips_exactly_and_preserves_every_source_and_statement_field():
    full, _ = transport_fixture()
    original = deepcopy(full)
    projected = provider_prose_context(full)
    assert projected["provider_projection"]["schema_version"] == PROVIDER_SCHEMA_VERSION
    assert projected["prose_contract"] == full["prose_contract"]
    assert projected["task_defaults"] == {key: full["tasks_by_element"]["材料"][key]
                                          for key in ("numeric_prose_instruction", "reasoning_priority")}
    assert len(projected["reference_registry"]) == 4
    for element, task in projected["tasks_by_element"].items():
        assert "bound_numeric_statements" not in task
        assert task["bound_numeric_statements_ref"] == f"prose_contract.elements.{element}.statements"
        assert [projected["reference_registry"][ident] for ident in task["reference_ids"]] == full["tasks_by_element"][element]["references"]
        for key in ("document_basis", "observed_costs", "focus", "eligible_evidence_ids", "action_availability"):
            assert task[key] == full["tasks_by_element"][element][key]
    assert reconstruct_provider_context(projected) == original
    assert full == original
    assert provider_prose_context(projected) == projected
    assert len(json.dumps(projected, ensure_ascii=False)) < len(json.dumps(full, ensure_ascii=False))
    projected["prose_contract"]["elements"]["材料"]["statements"][0]["text"] = "修改副本"
    assert full == original


@pytest.mark.parametrize("mode", ["attribution", "benchmark"])
@pytest.mark.parametrize("industry", ["pharma", "machinery", "auto_parts", "chemicals", "electronics"])
def test_all_public_synthetic_industries_preserve_full_validator_context(mode, industry):
    from tests.test_round2_manufacturing_projection import analyze
    from attribution_gen import _model_context
    from enterprise.benchmark_ai import grouped_model_context
    _, _, projection = analyze(industry=industry)
    payload = deepcopy(projection[mode+"_payload"])
    payload["prose_mode"] = PROSE_MODE
    sources = projection["sources"][mode]
    builder = _model_context if mode == "attribution" else grouped_model_context
    full = builder(payload, sources, include_numeric=True)
    original = deepcopy(full)
    projected = provider_prose_context(full)
    assert reconstruct_provider_context(projected) == full == original
    assert projected["prose_contract"] == full["prose_contract"]
    assert set(projected["tasks_by_element"]) == {"材料", "人工", "制费"}
    for element in full["tasks_by_element"]:
        task = full["tasks_by_element"][element]
        current = projected["tasks_by_element"][element]
        for field in ("document_basis", "eligible_evidence_ids", "action_availability", "focus",
                      "required_observations", "observed_labor", "observed_comparison", "paired_coverage",
                      "observed_details", "observed_costs", "data_fact", "available_document_ids"):
            assert current.get(field) == task.get(field)
        assert projected["prose_contract"]["elements"][element]["statements"] == task["bound_numeric_statements"]


def test_descriptor_collision_stays_inline_without_dropping_or_choosing_a_version():
    full, _ = transport_fixture()
    full["tasks_by_element"]["人工"]["references"][0]["source"]["sha256"] = "b"*64
    original = deepcopy(full)
    projected = provider_prose_context(full)
    for element, task in projected["tasks_by_element"].items():
        assert task["references"] == full["tasks_by_element"][element]["references"]
        assert "reference_ids" not in task
    assert "reference_registry" not in projected
    assert reconstruct_provider_context(projected) == original
    assert full == original


def test_duplicate_reference_order_and_membership_are_exact_even_across_elements():
    full, _ = transport_fixture()
    rows = full["tasks_by_element"]["材料"]["references"]
    rows.insert(1, deepcopy(rows[0]))
    projected = provider_prose_context(full)
    assert projected["tasks_by_element"]["材料"]["reference_ids"] == ["Rshared", "Rshared", "Rmaterial"]
    assert reconstruct_provider_context(projected) == full


def test_mismatched_duplicate_statements_remain_inline_and_do_not_change_numeric_authority():
    full, _ = transport_fixture()
    # Detach the synthetic helper's equivalent lists before deliberate mismatch.
    full["tasks_by_element"]["材料"]["bound_numeric_statements"] = deepcopy(full["tasks_by_element"]["材料"]["bound_numeric_statements"])
    full["tasks_by_element"]["材料"]["bound_numeric_statements"][0]["text"] = "这不是合同中的授权完整句。"
    projected = provider_prose_context(full)
    assert projected["tasks_by_element"]["材料"]["bound_numeric_statements"] == full["tasks_by_element"]["材料"]["bound_numeric_statements"]
    assert "bound_numeric_statements_ref" not in projected["tasks_by_element"]["材料"]
    assert projected["prose_contract"] == full["prose_contract"]
    assert reconstruct_provider_context(projected) == full


def test_differing_or_missing_common_instruction_never_becomes_global_default():
    full, _ = transport_fixture()
    full["tasks_by_element"]["材料"]["numeric_prose_instruction"] = "本要素专属指令。"
    full["tasks_by_element"]["人工"].pop("reasoning_priority")
    projected = provider_prose_context(full)
    assert "task_defaults" not in projected
    assert reconstruct_provider_context(projected) == full


@pytest.mark.parametrize("reserved", ["provider_projection", "task_defaults", "reference_registry"])
def test_foreign_reserved_fields_are_not_overwritten(reserved):
    full, _ = transport_fixture()
    full[reserved] = {"foreign": "untouched"}
    projected = provider_prose_context(full)
    assert projected == full and projected is not full


def test_nonprose_and_unavailable_contracts_remain_detached_unchanged():
    full, _ = transport_fixture()
    full.pop("prose_mode")
    assert provider_prose_context(full) == full
    assert provider_prose_context(full) is not full
    malformed = {"prose_mode": PROSE_MODE, "tasks_by_element": []}
    assert provider_prose_context(malformed) == malformed
    assert reconstruct_provider_context(full) == full
    with pytest.raises(TypeError):
        provider_prose_context(None)


@pytest.mark.parametrize("mutation", ["pointer", "registry", "defaults"])
def test_inverse_fails_closed_for_missing_or_conflicting_transport_targets(mutation):
    full, _ = transport_fixture()
    projected = provider_prose_context(full)
    if mutation == "pointer":
        projected["tasks_by_element"]["材料"]["bound_numeric_statements_ref"] = "prose_contract.elements.人工.statements"
    elif mutation == "registry":
        projected["reference_registry"].pop("Rshared")
    else:
        projected["task_defaults"].pop("numeric_prose_instruction")
    with pytest.raises(ValueError):
        reconstruct_provider_context(projected)


@pytest.mark.parametrize("mode", ["attribution", "benchmark"])
def test_prompt_keeps_complete_lossless_fewshot_with_short_optional_guidance(mode):
    original_input, original_output = _fewshot(mode)
    prompt = prose_prompt(mode)
    input_text, output_text = prompt.split("完整合成few-shot输入：\n", 1)[1].split("\n完整合成few-shot输出：\n")
    supplied, output = json.loads(input_text), json.loads(output_text)
    assert reconstruct_provider_context(supplied) == original_input
    assert output == original_output
    assert "task_defaults" in prompt and "reference_registry" in prompt
    assert "不逐条复述全部备选" in prompt
    assert "并列对象、反向抵消及适用知识和全部对应引用不得省略" in prompt
    assert "两项、三个等数量词" in prompt
    assert "凭证类别逐字使用action_availability声明，不自行近义改名" in prompt
    assert "去重并集" in prompt
    for element, row in output["elements"].items():
        unused = [item for item in supplied["prose_contract"]["elements"][element]["statements"]
                  if item["text"] not in row["hypothesis"]]
        if element == "材料":
            optional_kinds = {"market_reference", "primary_fact"} if mode == "benchmark" else {"market_reference"}
            assert unused and all(item["kind"] in optional_kinds for item in unused)
        assert all(not item["required"] for item in unused)
        used_refs = {ref for item in supplied["prose_contract"]["elements"][element]["statements"]
                     if item not in unused for ref in item["evidence_ids"]}
        unused_refs = {ref for item in unused for ref in item["evidence_ids"]}
        assert not ((unused_refs-used_refs) & set(row["evidence_ids"]))
        assert supplied["tasks_by_element"][element]["document_basis"] == original_input["tasks_by_element"][element]["document_basis"]
        assert all(ref not in row["recommendation"] for ref in row["evidence_ids"])
        assert len(row["evidence_ids"]) == len(set(row["evidence_ids"]))
        for field in ("hypothesis", "recommendation"):
            assert numeric_prose_diagnostics(row[field], element, supplied["prose_contract"], row["evidence_ids"],
                                             field=f"elements.{element}.{field}") == []
    compact = lambda obj: json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    assert len(input_text) < len(compact(original_input))


def test_fake_provider_can_receive_compact_data_while_validator_keeps_full_context():
    full, valid = transport_fixture()
    original = deepcopy(full)
    captured = []

    def provider(data):
        captured.append(deepcopy(data))
        assert "bound_numeric_statements" not in data["tasks_by_element"]["材料"]
        return deepcopy(valid)

    returned = provider(provider_prose_context(full))
    for element, row in returned["elements"].items():
        assert numeric_prose_diagnostics(row["hypothesis"], element, full["prose_contract"], row["evidence_ids"],
                                         field=f"elements.{element}.hypothesis") == []
    correction = {**provider_prose_context(full), "previous_candidate": deepcopy(returned),
                  "validation_diagnostics": [{"rule_id": "EXAMPLE", "offending": "不修改原候选"}]}
    provider(correction)
    assert reconstruct_provider_context(captured[0]) == original
    assert captured[1]["previous_candidate"] == valid
    assert full == original and returned == valid
    encoded = json.dumps(captured[0], ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    assert hashlib.sha256(encoded.encode("utf-8")).hexdigest() != hashlib.sha256(
        json.dumps(full, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def test_projection_is_pure_without_loading_config_files_or_database(monkeypatch):
    import builtins
    import sqlite3
    from pathlib import Path
    full, _ = transport_fixture()

    def forbidden(*a, **k):
        raise AssertionError("provider projection must be pure")

    monkeypatch.setattr(builtins, "open", forbidden)
    monkeypatch.setattr(Path, "read_text", forbidden)
    monkeypatch.setattr(sqlite3, "connect", forbidden)
    assert reconstruct_provider_context(provider_prose_context(full)) == full
    prose_prompt("attribution")
