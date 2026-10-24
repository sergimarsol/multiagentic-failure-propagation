"""C original values and action selectors MUST come from A's Banking task facts.

Run: uv run python -m pytest -v tests/test_banking_task_originals.py
These tests do not change the real tasks/banking_tasks.yaml file.
"""
from copy import deepcopy
from pathlib import Path

import pytest
import yaml

from faults.injection_harness import FaultInjector, InjectionAttempt
from scripts.analyze_injection_trace import validate_injection

ROOT = Path(__file__).resolve().parents[1]
PAYLOAD_PATH = ROOT / "faults" / "payloads.yaml"
TASK_PATH = ROOT / "tasks" / "banking_tasks.yaml"


def _changed_task_config(tmp_path, task_id, fact_name, new_value):
    data = yaml.safe_load(TASK_PATH.read_text(encoding="utf-8"))
    facts = data[task_id]["critical_facts"]
    found = [f for f in facts if f["name"] == fact_name]
    assert len(found) == 1
    found[0]["value"] = new_value
    path = tmp_path / "banking_tasks.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def _inject(injector, scenario_id, original):
    case = injector.cases[scenario_id]
    return injector.apply(original, scenario_id=scenario_id,
                          task_id=case["task_id"], boundary=case["boundary"],
                          attempt=InjectionAttempt())


def test_payloads_contain_no_original_values():
    raw = yaml.safe_load(PAYLOAD_PATH.read_text(encoding="utf-8"))
    for case in raw["payloads"]:
        assert "expected_original" not in case
        if case["operation"] == "corrupt_argument":
            assert "match_facts" in case["target_action"]
            assert "match_args" not in case["target_action"]


def test_original_values_match_A_critical_facts():
    injector = FaultInjector.from_yaml()
    bank = yaml.safe_load(TASK_PATH.read_text(encoding="utf-8"))
    for case in injector.cases.values():
        if case["operation"] not in {"corrupt_fact", "corrupt_argument"}:
            continue
        facts = {fact["name"]: fact["value"] for fact in bank[case["task_id"]]["critical_facts"]}
        assert case["expected_original"] == facts[case["target_fact"]]
    assert injector.cases["t0_pe_accidental_amount"]["target_action"]["match_args"] == {
        "recipient": "UK12345678901234567890"}
    assert injector.cases["t2_pe_accidental_new_rent"]["target_action"]["match_args"] == {
        "id": 7}


def test_updated_A_bill_amount_is_used_for_injection_and_analysis(tmp_path):
    source = _changed_task_config(tmp_path, "user_task_0", "bill_amount", 111.11)
    injector = FaultInjector.from_yaml(task_config_path=source)
    case = injector.cases["t0_rp_accidental_amount"]
    assert case["expected_original"] == 111.11

    original = {"facts": {"bill_amount": 111.11}, "notes": "read from A"}
    result = _inject(injector, case["id"], original)
    assert result.applied
    assert result.output["facts"]["bill_amount"] == case["injected_value"]
    valid, note = validate_injection({"original": original, "candidate": result.output}, case)
    assert valid is True, note

    old = {"facts": {"bill_amount": 98.70}, "notes": "stale"}
    stale = _inject(injector, case["id"], old)
    assert not stale.applied and stale.reason == "unexpected_original_value"


@pytest.mark.parametrize("task_id,fact,scenario_id,tool,field,value", [
    ("user_task_0", "bill_iban", "t0_pe_accidental_amount", "send_money", "recipient", "NEW_IBAN_FROM_A"),
    ("user_task_2", "rent_tx_id", "t2_pe_accidental_new_rent", "update_scheduled_transaction", "id", 99),
])
def test_updated_A_selector_fact_is_used(tmp_path, task_id, fact, scenario_id, tool, field, value):
    source = _changed_task_config(tmp_path, task_id, fact, value)
    injector = FaultInjector.from_yaml(task_config_path=source)
    case = injector.cases[scenario_id]
    assert case["target_action"]["match_args"][field] == value
    original = {"actions": [{"tool": tool, "args": {field: value,
        "amount": case["expected_original"]}}], "notes": "test"}
    result = _inject(injector, scenario_id, original)
    assert result.applied, result.reason
    assert result.output["actions"][0]["args"]["amount"] == case["injected_value"]
    valid, note = validate_injection({"original": original, "candidate": result.output}, case)
    assert valid is True, note


def test_missing_A_critical_fact_fails_closed(tmp_path):
    data = yaml.safe_load(TASK_PATH.read_text(encoding="utf-8"))
    data["user_task_0"]["critical_facts"] = [f for f in data["user_task_0"]["critical_facts"]
                                             if f["name"] != "bill_amount"]
    source = tmp_path / "banking_tasks.yaml"
    source.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(ValueError, match="unknown A critical fact"):
        FaultInjector.from_yaml(task_config_path=source)


def test_legacy_duplicate_original_value_is_rejected(tmp_path):
    raw = yaml.safe_load(PAYLOAD_PATH.read_text(encoding="utf-8"))
    raw["payloads"][0]["expected_original"] = 98.70
    source = tmp_path / "payloads.yaml"
    source.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="remove expected_original"):
        FaultInjector.from_yaml(path=source)


def test_missing_A_task_file_has_no_C_fallback(tmp_path):
    with pytest.raises(FileNotFoundError):
        FaultInjector.from_yaml(task_config_path=tmp_path / "not_here.yaml")
