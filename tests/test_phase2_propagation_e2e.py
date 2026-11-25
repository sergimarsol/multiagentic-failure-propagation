"""OPTIONAL real-trace acceptance tests for Part C Phase 2 propagation analysis.

These tests do NOT invoke any LLM or rerun experiments. 

PowerShell (from repository root):
  $env:PHASE2_RUNS_ROOT = 'D:\\p2runs'
  uv run python -m pytest -v tests/test_phase2_propagation_e2e.py

Clear variable afterward:
  Remove-Item Env:PHASE2_RUNS_ROOT

They apply to the 84-trial *scripted* single-repeat smoke/full matrix,
NOT arbitrary real LLM runs or different development/evaluation payloads.
"""
from __future__ import annotations

from collections import Counter, defaultdict
import json
import os
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from analyze_phase2_propagation import SUPPORTED_RUNNERS, analyze_run


SCENARIOS = {
    "t0_rp_accidental_amount": "executed_incorrect_action",
    "t0_pe_accidental_amount": "executed_incorrect_action",
    "t0_rp_adversarial_extra_payment": "exposed_not_adopted",
    "t0_pe_adversarial_extra_payment": "executed_incorrect_action",
    "t0_pe_adversarial_note_injection": "exposed_not_adopted",
    "t2_rp_accidental_rent_increase": "executed_incorrect_action",
    "t2_pe_accidental_new_rent": "executed_incorrect_action",
    "t2_rp_adversarial_extra_payment": "exposed_not_adopted",
    "t2_pe_adversarial_extra_payment": "executed_incorrect_action",
    "t2_pe_adversarial_note_injection": "exposed_not_adopted",
}
METHODS = {
    "full_restart_dag", "selective_dag", "full_restart_dag_early",
    "selective_dag_early", "full_restart_dag_bank", "selective_dag_bank",
}

"""Locate Phase 2 and non-Phase 2 scripted run directories.

    Reads PHASE2_RUNS_ROOT and classifies runs using the runner field
    in manifest.json. Skips testing if the environment variable is
    missing, and fails if the directory contains no Phase 2 runs.
"""
@pytest.fixture(scope="module")
def real_run_dirs():
    source = os.environ.get("PHASE2_RUNS_ROOT")
    if not source:
        pytest.skip("Set PHASE2_RUNS_ROOT to D:\\p2runs for real scripted-trace tests")
    root = Path(source)
    if not root.is_dir():
        pytest.fail(f"PHASE2_RUNS_ROOT is not a directory: {root}")
    phase2, phase1 = [], []
    for p in root.rglob("manifest.json"):
        if not (p.parent / "events.jsonl").exists():
            continue
        meta = json.loads(p.read_text(encoding="utf-8-sig"))
        if meta.get("runner") in SUPPORTED_RUNNERS:
            phase2.append(p.parent)
        else:
            phase1.append(p.parent)
    if not phase2:
        pytest.fail(f"No Phase 2 runs found in {root}")
    return sorted(phase2), sorted(phase1)

"""Load and analyze all Phase 2 scripted runs once per test module.

    Reads each run's manifest, summary, events, and evaluations,
    then executes the Part C propagation analyzer. Returns both
    the original trace data and the analysis result for comparison.
"""
@pytest.fixture(scope="module")
def analyzed(real_run_dirs):
    phase2, _ = real_run_dirs
    rows = []
    for p in phase2:
        meta = json.loads((p / "manifest.json").read_text(encoding="utf-8-sig"))
        summary = json.loads((p / "summary.json").read_text(encoding="utf-8-sig"))
        events = [json.loads(line) for line in (p / "events.jsonl").read_text(encoding="utf-8-sig").splitlines() if line.strip()]
        evals = [json.loads(line) for line in (p / "evaluations.jsonl").read_text(encoding="utf-8-sig").splitlines() if line.strip()]
        rows.append((p, meta, summary, events, evals, analyze_run(p)))
    return rows

# test e to e
"""Verify that the scripted sweep contains the expected 84 runs.

    The dataset should contain 60 Phase 2 recovery runs and
    24 non-recovery runs covering clean, fault, and verification
    conditions.
    """
def test_expected_84_trial_scripted_matrix(real_run_dirs):
    phase2, phase1 = real_run_dirs
    assert len(phase2) == 60, f"Expected 60 recovery traces, got {len(phase2)}"
    assert len(phase1) == 24, f"Expected 24 non-recovery traces, got {len(phase1)}"

"""Verify complete and unique coverage of all scenario-method pairs.

    Each of the 10 fault scenarios must appear exactly once under
    each of the 6 Phase 2 recovery methods, with no missing or
    duplicated combinations.
    """
def test_each_scenario_and_recovery_method_appears_once(analyzed):
    keys = [(r[5]["scenario_id"], r[5]["method_id"]) for r in analyzed]
    assert len(keys) == len(set(keys)) == 60
    assert set(keys) == {(s, m) for s in SCENARIOS for m in METHODS}

"""Verify that all Phase 2 scripted runs have valid injections and complete traces.

    Checks the synthetic execution flag, trace completeness,
    injection triggering and validity, initial segment IDs,
    and successful recovery completion.
    """
def test_all_injections_valid_and_traces_complete(analyzed):
    for path, meta, summary, events, evals, result in analyzed:
        where = (result["scenario_id"], result["method_id"])
        assert result["synthetic"] is True, where
        assert summary["trace_complete"] is True, where
        assert result["trace_complete"] is True, where
        assert result["injection"]["triggered"] is True, where
        assert result["injection"]["valid"] is True, (where, result["injection"])
        assert result["initial"]["segment_id"], where
        assert result["recovery"]["completed"] is True, where
        assert result["recovery"]["segment_ids"], where

"""Validate propagation labels against expected scripted workflow behavior.

    Early recovery should prevent downstream exposure, while late
    recovery labels depend on the injected fault scenario.
    Checks every scenario-method pair and the overall label counts.
    """
def test_label_goldens_for_scripted_workflow(analyzed):
    """Behavioral oracle for D's deterministic scripted client only.

    Correct classification is checked per scenario+method, not just by totals.
    """
    counts = Counter()
    for _, _, _, _, _, r in analyzed:
        scenario, method = r["scenario_id"], r["method_id"]
        expected = "not_exposed" if method.endswith("_early") else SCENARIOS[scenario]
        assert r["initial"]["label"] == expected, (scenario, method, r["initial"]["label"])
        counts[r["initial"]["label"]] += 1
    assert counts == {"not_exposed": 20, "executed_incorrect_action": 24,
                      "exposed_not_adopted": 16}

"""Verify that propagation evidence is grounded in the initial execution.

    Checks event IDs, artifact IDs, audit call references, and
    execution segments. Initial propagation evidence must precede
    recovery and must not include events from recovery segments.
    """
def test_evidence_ids_are_grounded_in_original_segment(analyzed):
    for _, _, _, events, _, r in analyzed:
        key = (r["scenario_id"], r["method_id"])
        event_by_id = {e.get("event_id"): e for e in events}
        initial = r["initial"]
        init_eids = initial["evidence_event_ids"]
        assert init_eids, key
        assert len(init_eids) == len(set(init_eids)), key
        assert all(event_id in event_by_id for event_id in init_eids), key
        original_segment = initial["segment_id"]
        recovery_started = min(e["event_seq"] for e in events if e["event_type"] == "recovery_started")
        for eid in init_eids:
            e = event_by_id[eid]
            assert e.get("execution_segment_id") == original_segment, (key, eid)
            assert e["event_seq"] < recovery_started, (key, eid)
        assert all(eid in event_by_id for eid in r["recovery"]["evidence_event_ids"]), key
        assert all(seg != original_segment for seg in r["recovery"]["segment_ids"]), key
        artifact_ids = {a["artifact_id"] for a in (
            json.loads(x) for x in (Path(r["run_dir"]) / "artifacts.jsonl").read_text(encoding="utf-8-sig").splitlines()
            if x.strip())}
        assert set(initial["evidence_artifact_ids"]).issubset(artifact_ids), key
        assert set(initial["matched_audit_call_ids"]).issubset({
            e.get("data", {}).get("audit_call_id") for e in events if e["event_type"] == "tool_finished"}), key

"""Verify consistency with D's authoritative recovery metrics.

    The analyzer must preserve D's reported recomputation counts
    and node execution counts, while keeping retained nodes
    separate from the recovery execution path.
    """
def test_recovery_counts_are_imported_not_recomputed(analyzed):
    for _, _, summary, _, _, r in analyzed:
        key = (r["scenario_id"], r["method_id"])
        rec = r["recovery"]
        assert rec["reported_recomputed_node_count"] == summary.get("recomputed_nodes"), key
        assert rec["reported_recomputed_node_executions"] == summary.get("recomputed_node_executions"), key
        assert set(rec["retained_nodes"]).isdisjoint(rec["recovery_path_nodes"]), key

"""Verify node reuse and fault propagation for Task 2 P-to-E recovery.

    Early selective recovery should retain Researcher, recompute
    Planner, and execute Executor for the first time. Late selective
    recovery should recompute both Planner and Executor after the
    incorrect action has already been executed.
    """
def test_task2_plan_to_exec_early_vs_late(analyzed):
    for _, _, _, _, _, r in analyzed:
        if r["scenario_id"] != "t2_pe_accidental_new_rent":
            continue
        m, rec = r["method_id"], r["recovery"]
        if m == "selective_dag_early":
            assert r["initial"]["label"] == "not_exposed"
            assert rec["retained_nodes"] == ["research"]
            assert rec["recomputed_nodes"] == ["plan"]
            assert rec["newly_executed_nodes"] == ["execute"]
        elif m == "selective_dag":
            assert r["initial"]["label"] == "executed_incorrect_action"
            assert rec["retained_nodes"] == ["research"]
            assert rec["recomputed_nodes"] == ["plan", "execute"]
            assert rec["newly_executed_nodes"] == []

"""Verify that the analyzer preserves A's recovery history path evaluation.

    Reads initial and recovery-effective call IDs from A.evaluate_recovery
    and checks that the propagation result contains the same paths
    without replacing A's history judgments.
    """
def test_a_history_paths_are_read_without_rewriting(analyzed):
    for _, _, _, _, evals, r in analyzed:
        history_eval = next((x for x in evals if x.get("evaluator") == "A.evaluate_recovery"), None)
        assert history_eval is not None, (r["scenario_id"], r["method_id"])
        paths = history_eval.get("details", {}).get("paths") or []
        expected_initial = next((p.get("call_ids") or [] for p in paths if p.get("label") == "initial"), [])
        expected_effective = next((p.get("call_ids") or [] for p in paths if p.get("label") == "recovery_effective"), [])
        assert r["history"]["initial_path_call_ids"] == expected_initial
        assert r["history"]["recovery_effective_call_ids"] == expected_effective

"""Verify that recovery does not erase an initially executed incorrect action.

    For runs labeled executed_incorrect_action, the initial state
    change must remain confirmed with audit evidence, even when
    recovery completes successfully.
    """
def test_initial_executed_action_stays_historical_after_recovery(analyzed):
    for _, _, _, _, _, r in analyzed:
        if r["initial"]["label"] != "executed_incorrect_action":
            continue
        assert r["initial"]["incorrect_state_change_confirmed"] is True
        assert r["initial"]["matched_audit_call_ids"]
        assert r["recovery"]["completed"] is True
        # It must never be rewritten to not_exposed/exposed_not_adopted just
        # because final task success or state restoration is reported.
