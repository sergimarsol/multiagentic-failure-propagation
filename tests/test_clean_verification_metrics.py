"""D reporting regressions; no semantic validity is inferred from clean execution."""

import json
from pathlib import Path

import pytest

from experiments.batch import aggregate


def observation(index, condition="clean_verify", decisions=2, rejections=0, success=None):
    return {
        "config": {"task_id": "user_task_0", "condition": condition, "scenario_id": "clean",
                   "comparison_group_id": "g", "repeat_index": index, "synthetic": True,
                   "workflow": {}, "fault": {}},
        "error": None,
        "summary": {"run_id": f"run_{index}", "verifier_decisions": decisions,
                    "verifier_rejections": rejections, "final_agentdojo_utility": success,
                    "trace_complete": True},
    }


@pytest.mark.parametrize("success", [True, False, None])
def test_rejection_is_not_adjudicated_by_final_utility(tmp_path, success):
    report = aggregate([
        observation(0, rejections=2, success=success),
        observation(1),
        observation(2, decisions=0),
    ], tmp_path)[0]
    assert report["clean_run_rejection_rate"] == 0.5
    assert report["clean_run_rejection_rate_successes"] == 1  # runs, not decisions
    assert report["clean_run_rejection_rate_samples"] == 2
    assert report["clean_verify_runs_without_decision"] == 1
    assert report["false_rejection_status"] == "not_evaluated"
    assert not any(key.startswith("unnecessary_denial_rate") for key in report)
    lower = report["clean_run_rejection_rate_ci95_lower"]
    upper = report["clean_run_rejection_rate_ci95_upper"]
    assert lower == pytest.approx(0.09453120573423074)
    assert upper == pytest.approx(0.9054687942657693)
    import csv
    with (tmp_path / "condition_summary.csv").open() as stream:
        columns = next(csv.reader(stream))
    assert "clean_run_rejection_rate_ci95_lower" in columns
    assert "clean_run_rejection_rate_ci95_upper" in columns
    assert not any(key.startswith("unnecessary_denial_rate") for key in columns)



@pytest.mark.parametrize("condition", ["clean", "fault", "always_verify", "full_restart"])
def test_other_conditions_do_not_supply_clean_rejection_rate(tmp_path, condition):
    report = aggregate([observation(0, condition=condition, rejections=1)], tmp_path)[0]
    assert report["clean_run_rejection_rate"] is None
    assert report["clean_run_rejection_rate_samples"] == 0
    assert report["verifier_rejections"] == 1


def test_no_verifier_decision_is_not_zero_rejection_rate(tmp_path):
    report = aggregate([observation(0, decisions=0)], tmp_path)[0]
    assert report["clean_run_rejection_rate"] is None
    assert report["clean_verify_runs_without_decision"] == 1


@pytest.mark.parametrize("filename", ["d_phase1_matrix.template.json", "d_b_workflow_smoke.json"])
def test_existing_matrices_keep_unique_clean_controls(filename):
    root = Path(__file__).resolve().parents[1]
    rows = json.loads((root / "configs" / filename).read_text())
    for task in ("user_task_0", "user_task_2"):
        selected = [r for r in rows if r["task_id"] == task]
        assert {r["condition"] for r in selected} == {
            "clean", "clean_verify", "fault", "always_verify", "full_restart"}
        for condition in ("clean", "clean_verify"):
            controls = [r for r in selected if r["condition"] == condition]
            assert len(controls) == 1
            assert not controls[0].get("fault")
