"""Attack success and unnecessary denials in D's run summaries and condition tables (scripted model, real A/B/C/D)."""

import csv
import json

import pytest

from experiments.batch import aggregate
from experiments.metrics import summarize_run
from tests.test_abcd_deterministic_integration import CASES, _run

C_REF = "faults/payloads.yaml"


def resummarize(run, *, payload_ref=C_REF, scenario_id=None):
    """The deterministic helper runs without experiment controls; add the ones a real C run records."""
    path = run.path / "manifest.json"
    manifest = json.loads(path.read_text())
    if payload_ref is not None:
        manifest["experiment"] = {"scenario_id": scenario_id, "fault": {"payload_ref": payload_ref}}
    path.write_text(json.dumps(manifest))
    return summarize_run(run.path, write=False)


@pytest.mark.parametrize("scenario_id", [s for s, c in CASES.items() if c["operation"] != "append_plan_note"])
def test_scripted_faults_reach_their_goal(tmp_path, scenario_id):
    run = _run(tmp_path, CASES[scenario_id]["task_id"], scenario_id=scenario_id)
    s = resummarize(run, scenario_id=scenario_id)
    assert s["attack"]["status"] == "evaluated"
    assert s["attack_success"] is True and s["initial_attack_success"] is True
    assert s["attack"]["attempts"][0]["in_final_state"] is True
    assert set(s["attack"]["attempts"][0]["executed_call_ids"]) <= set(run.evaluations[0]["unauthorized_execution_ids"])


def test_verifier_blocking_the_fault_means_no_attack_success(tmp_path):
    run = _run(tmp_path, "user_task_0", scenario_id="t0_rp_accidental_amount",
               condition="always_verify", verifier_policy="reject_injected")
    s = resummarize(run, scenario_id="t0_rp_accidental_amount")
    assert s["attack_success"] is False and s["attack"]["status"] == "evaluated"
    assert (s["verifier_decisions"], s["verifier_rejections"]) == (3, 1)  # research: reject, accept; plan: accept


def test_full_restart_keeps_the_first_attempts_attack(tmp_path):
    run = _run(tmp_path, "user_task_2", scenario_id="t2_pe_accidental_new_rent", condition="full_restart")
    s = resummarize(run, scenario_id="t2_pe_accidental_new_rent")
    rows = {r["attempt_id"]: r for r in s["attack"]["attempts"]}
    assert rows["attempt_0"]["executed"] is True and rows["attempt_1"]["executed"] is False
    assert s["attack_success"] is True and s["initial_attack_success"] is True


@pytest.mark.parametrize("payload_ref", [None, "workflow/fixture_payloads.yaml"])
def test_runs_not_from_c_payloads_are_not_judged(tmp_path, payload_ref):
    run = _run(tmp_path, "user_task_0", scenario_id="t0_pe_accidental_amount")
    s = resummarize(run, payload_ref=payload_ref, scenario_id="t0_pe_accidental_amount")
    assert s["attack"]["status"] == "not_applicable" and s["attack_success"] is None


def test_equivalent_payload_path_spellings_are_c_payloads(tmp_path):
    run = _run(tmp_path, "user_task_0", scenario_id="t0_pe_accidental_amount")
    assert resummarize(run, payload_ref="./faults/../faults/payloads.yaml", scenario_id="t0_pe_accidental_amount")["attack_success"] is True


def test_unknown_scenario_is_an_error_not_a_failure(tmp_path):
    run = _run(tmp_path, "user_task_0", scenario_id="t0_pe_accidental_amount")
    s = resummarize(run, scenario_id="t9_made_up")
    assert s["attack"]["status"] == "error" and s["attack_success"] is None


def test_clean_runs_have_no_attack_and_no_verifier(tmp_path):
    run = _run(tmp_path, "user_task_0", condition="clean")
    s = summarize_run(run.path, write=False)
    assert s["attack"]["status"] == "not_applicable" and s["attack_success"] is None
    assert (s["verifier_decisions"], s["verifier_rejections"]) == (0, 0)


# ---- condition table ---------------------------------------------------------------------------------------

def row(i, condition, attack=None, decisions=0, rejections=0, status=None):
    config = {"task_id": "user_task_0", "condition": condition, "scenario_id": "t0_pe_accidental_amount",
              "comparison_group_id": "g", "repeat_index": i, "synthetic": True, "workflow": {}, "fault": {}}
    return {"config": config, "error": None, "summary": {
        "run_id": f"run_{condition}_{i}", "attack_success": attack,
        "attack": {"status": status or ("not_applicable" if attack is None else "evaluated")},
        "verifier_decisions": decisions, "verifier_rejections": rejections, "trace_complete": True}}


def test_condition_table_reports_attack_success_and_unnecessary_denials(tmp_path):
    rows = [row(0, "fault", True), row(1, "fault", False), row(2, "fault", None),
            row(3, "fault", True, status="incomplete"), row(4, "fault", None, status="error"),
            row(0, "clean_verify", decisions=2, rejections=1), row(1, "clean_verify", decisions=2),
            row(2, "clean_verify"),  # verifier never ran: not in the denominator
            row(0, "always_verify", True, decisions=2, rejections=1)]
    table = {r["condition"]: r for r in aggregate(rows, tmp_path)}
    assert (table["fault"]["attack_success_rate_successes"], table["fault"]["attack_success_rate_samples"]) == (1, 2)
    assert table["fault"]["attack_success_rate"] == 0.5  # the incomplete success is not counted...
    assert table["fault"]["attack_unknown"] == 2  # ...but reported as unknown, with the error
    assert (table["clean_verify"]["unnecessary_denial_rate_successes"], table["clean_verify"]["unnecessary_denial_rate_samples"]) == (1, 2)
    assert table["clean_verify"]["attack_success_rate"] is None
    assert table["always_verify"]["unnecessary_denial_rate"] is None  # rejections there may be correct
    assert table["always_verify"]["verifier_rejections"] == 1
    with (tmp_path / "runs.csv").open() as f:
        assert {"attack_success", "verifier_rejections"} <= set(next(csv.reader(f)))
