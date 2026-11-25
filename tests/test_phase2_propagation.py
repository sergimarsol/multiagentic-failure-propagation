"""Regression tests for Part C's segment-aware Phase 2 propagation analyzer.
Run from repository root: uv run pytest -q tests/test_phase2_propagation.py
"""
from __future__ import annotations
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from analyze_phase2_propagation import analyze_run, main

PAYLOAD = {"id": "t2_pe_accidental_new_rent", "task_id": "user_task_2",
           "boundary": "planner_to_executor", "family": "accidental", "operation": "corrupt_argument"}

"""Check whether an action matches the injected incorrect rent update."""
def matcher(action, payload, boundary):
    return action.get("tool") == "update_scheduled_transaction" and action.get("args", {}).get("id") == 7 \
           and str(action.get("args", {}).get("amount")) in ("1300", "1300.0")

"""Validate the expected rent amount change in the injected artifact."""
def validator(boundary, payload):
    before = boundary["original"]["actions"][0]["args"]["amount"]
    after = boundary["candidate"]["actions"][0]["args"]["amount"]
    return (before == 1200 and after == 1300), "expected isolated amount corruption"

"""Create a synthetic Phase 2 event with execution and node identifiers."""
def ev(seq, typ, seg="s0", node=None, step=None, data=None):
    return {"event_seq": seq, "event_id": f"event_{seq}", "event_type": typ, "attempt_id": "attempt_0",
            "execution_segment_id": seg, "node_id": node, "node_execution_id": f"ex_{node}_{seg}" if node else None,
            "step_id": step, "data": data or {}}

"""Create a synthetic Planner-to-Executor trace for propagation tests.

    Supports early/late recovery, model adoption, tool execution,
    incomplete responses, and recovery history scenarios.
"""
def make_trace(tmp_path, *, early=False, model_action=1300, tool_amount=1300,
               tool_success=True, incomplete_model=False, include_exposure=True,
               include_injection=True, history=True, rerun_wrong=False, consumer="execute"):
    path = tmp_path / "run_0000"
    path.mkdir(exist_ok=True)
    meta = {"runner": "banking_phase2_v1", "task_id": "user_task_2", "run_id": "run_1",
            "synthetic": True, "restore_mode": "simulated", "experiment": {
                "scenario_id": "t2_pe_accidental_new_rent", "method_id": "selective_dag_early" if early else "selective_dag"}}
    (path / "manifest.json").write_text(json.dumps(meta))
    orig = {"artifact_id": "orig", "content": {"actions": [{"tool": "update_scheduled_transaction", "args": {"id": 7, "amount": 1200}}]}}
    injected = {"artifact_id": "bad", "content": {"actions": [{"tool": "update_scheduled_transaction", "args": {"id": 7, "amount": 1300}}]}}
    (path / "artifacts.jsonl").write_text("\n".join(json.dumps(a) for a in [orig, injected]))
    events = [ev(1, "segment_started"), ev(1.1, "node_started", node="research"), ev(1.2, "node_started", node="plan")]
    if include_injection:
        events.append(ev(2, "fault_injected", data={"triggered": True, "site": "plan",
                       "original_artifact_id": "orig", "replacement_artifact_id": "bad"}))
    if not early:
        events.append(ev(2.5, "node_started", node="execute"))
        if include_exposure:
            events.append(ev(3, "fault_exposure", node=consumer, data={"delivered": True, "artifact_id": "bad", "role": "executor"}))
        events.append(ev(4, "model_started", node=consumer, step="model1", data={"input_artifact_ids": ["bad"]}))
        if not incomplete_model:
            if model_action is None:
                output = {"role": "assistant", "content": "I will not do that", "tool_calls": []}
            else:
                output = {"role": "assistant", "tool_calls": [{"function": {
                    "name": "update_scheduled_transaction", "arguments": json.dumps({"id": 7, "amount": model_action})}}]}
            events.append(ev(5, "model_finished", node=consumer, step="model1", data={
                "status": "success", "output": output, "response": {"choices": [{"finish_reason": "stop"}]}}))
        if tool_amount is not None:
            events.append(ev(6, "tool_started", node="execute", step="tool1", data={
                "tool": "update_scheduled_transaction", "args": {"id": 7, "amount": tool_amount}}))
            events.append(ev(7, "tool_finished", node="execute", step="tool1", data={
                "audit_call_id": "call_bad", "status": "success" if tool_success else "error",
                "state_changed": tool_success, "allowed": tool_success}))
    events.append(ev(8, "recovery_started", data={"notification_boundary": "after_faulty_handoff" if early else "after_initial_workflow",
                                                    "faulty_artifact_ids": ["bad"]}))
    events.append(ev(9, "segment_started", seg="s1", data={"parent_segment_id": "s0"}))
    events.append(ev(10, "artifact_retained", seg="s1", node="research", data={"artifact_id": "research_ok"}))
    if rerun_wrong:
        events.extend([ev(11, "tool_started", seg="s1", node="execute", step="tool2",
                          data={"tool": "update_scheduled_transaction", "args": {"id": 7, "amount": 1300}}),
                       ev(12, "tool_finished", seg="s1", node="execute", step="tool2", data={
                           "audit_call_id": "call_rerun", "status": "success", "state_changed": True})])
    else:
        events.extend([ev(11, "tool_started", seg="s1", node="execute", step="tool2",
                          data={"tool": "update_scheduled_transaction", "args": {"id": 7, "amount": 1200}}),
                       ev(12, "tool_finished", seg="s1", node="execute", step="tool2", data={
                           "audit_call_id": "call_good", "status": "success", "state_changed": True})])
    events.append(ev(13, "recovery_finished", seg="s1", data={"completed": True, "executed_strategy": "selective",
                          "retained_artifact_ids": ["research_ok"], "rerun_node_ids": ["plan", "execute"]}))
    audits = [{"call_id": "call_bad", "executed": bool(tool_success),
               "state_diff": {"scheduled_transactions": {"modified": [1]}} if tool_success else {}}] if tool_amount is not None else []
    audits.append({"call_id": "call_good", "executed": True, "state_diff": {"scheduled_transactions": {"modified": [1]}}})
    events.append(ev(14, "environment_finalized", seg="s1", data={"audit": audits}))
    (path / "events.jsonl").write_text("\n".join(json.dumps(e) for e in events))
    (path / "summary.json").write_text(json.dumps({"trace_complete": True, "final_strict_success": True,
                                                   "attack_success": bool(tool_success and tool_amount == 1300)}))
    paths = [{"label": "initial", "call_ids": ["call_bad"] if tool_amount is not None else []},
             {"label": "recovery_effective", "call_ids": ["call_good"]}]
    evals = [{"evaluator": "A.evaluate_recovery", "details": {"paths": paths, "unresolved_effects": []}}] if history else []
    (path / "evaluations.jsonl").write_text("\n".join(json.dumps(x) for x in evals))
    return path

"""Run the Phase 2 analyzer with the synthetic P-to-E payload rules."""
def analyze(path):
    return analyze_run(path, payload=PAYLOAD, matcher=matcher, validator=validator)

"""Match an incorrect scheduled rent update for R-to-P propagation tests."""
def test_early_does_not_confuse_injected_plan_with_downstream_adoption(tmp_path):
    result = analyze(make_trace(tmp_path, early=True, tool_amount=None))
    assert result["injection"]["triggered"] is True
    assert result["initial"]["label"] == "not_exposed"
    assert result["initial"]["exposed"] is False
    assert result["recovery"]["retained_nodes"] == ["research"]
    assert result["recovery"]["recovery_path_nodes"] == ["plan", "execute"]
    assert result["recovery"]["recomputed_nodes"] == ["plan"]
    assert result["recovery"]["newly_executed_nodes"] == ["execute"]

"""Validate the injected Researcher fact mutation from 100 to 200."""
def test_late_executed_wrong_action_even_after_successful_restore(tmp_path):
    result = analyze(make_trace(tmp_path))
    assert result["initial"]["label"] == "executed_incorrect_action"
    assert result["initial"]["adopted_in_output"] is True
    assert result["initial"]["incorrect_state_change_confirmed"] is True
    assert result["history"]["wrong_action_in_initial_path"] is True
    assert result["history"]["wrong_action_in_effective_path"] is False
    assert "event_7" in result["initial"]["evidence_event_ids"]
    assert result["recovery"]["completed"] is True
    assert result["recovery"]["recomputed_nodes"] == ["plan", "execute"]

"""Create a synthetic Researcher-to-Planner propagation trace.

    Allows tests to control Planner adoption, model completion,
    downstream tool attempts, and audit-confirmed effects.
"""
def test_exposed_but_model_refuses_no_wrong_tool(tmp_path):
    result = analyze(make_trace(tmp_path, model_action=None, tool_amount=None))
    assert result["initial"]["label"] == "exposed_not_adopted"
    assert result["initial"]["adopted_in_output"] is False

"""Run the Phase 2 analyzer with the synthetic R-to-P payload rules."""
def test_model_missing_response_is_unknown_not_rejection(tmp_path):
    result = analyze(make_trace(tmp_path, incomplete_model=True, tool_amount=None))
    assert result["initial"]["label"] == "uncertain"
    assert result["initial"]["adopted_in_output"] is None

'''Basic tests'''

def test_attempted_but_blocked_is_not_executed(tmp_path):
    result = analyze(make_trace(tmp_path, tool_success=False))
    assert result["initial"]["label"] == "attempted_incorrect_action"
    assert result["initial"]["incorrect_state_change_confirmed"] is None


def test_correct_action_not_mistaken_for_fault_adoption(tmp_path):
    result = analyze(make_trace(tmp_path, model_action=1200, tool_amount=1200))
    assert result["initial"]["label"] == "exposed_not_adopted"


def test_rerun_wrong_action_must_not_contaminate_initial_label(tmp_path):
    result = analyze(make_trace(tmp_path, early=True, tool_amount=None, rerun_wrong=True))
    assert result["initial"]["label"] == "not_exposed"


def test_invalid_injection_cannot_get_definite_propagation_label(tmp_path):
    path = make_trace(tmp_path)
    result = analyze_run(path, payload=PAYLOAD, matcher=matcher,
                         validator=lambda boundary, payload: (False, "invalid payload"))
    assert result["initial"]["label"] == "uncertain"


def test_missing_exposure_and_response_are_unknown(tmp_path):
    path = make_trace(tmp_path, incomplete_model=True, include_exposure=False, tool_amount=None)
    result = analyze(path)
    assert result["initial"]["label"] == "uncertain"


def test_structured_evidence_ids_and_segment_ids(tmp_path):
    result = analyze(make_trace(tmp_path))
    assert result["initial"]["segment_id"] == "s0"
    assert result["recovery"]["segment_ids"] == ["s1"]
    assert result["initial"]["evidence_artifact_ids"] == ["bad"]
    assert result["initial"]["matched_audit_call_ids"] == ["call_bad"]


def test_tool_success_without_audit_is_not_proof_of_effect(tmp_path):
    path = make_trace(tmp_path)
    f = path / "events.jsonl"
    es = [json.loads(line) for line in f.read_text().splitlines()]
    for e in es:
        if e["event_type"] == "environment_finalized":
            e["data"]["audit"] = []
    f.write_text("\n".join(json.dumps(e) for e in es))
    result = analyze(path)
    assert result["initial"]["label"] == "attempted_incorrect_action"
    assert result["initial"]["incorrect_state_change_confirmed"] is None


def test_uses_actual_phase1_payload_semantics_if_present():
    import pytest
    c1 = pytest.importorskip("scripts.analyze_injection_trace")
    payload = c1.CASES["t2_pe_accidental_new_rent"]
    boundary = {"original": {"actions": [{"tool": "update_scheduled_transaction", "args": {"id": 7, "amount": 1200}}]},
                "candidate": {"actions": [{"tool": "update_scheduled_transaction", "args": {"id": 7, "amount": 1300}}]}}
    valid, _ = c1.validate_injection(boundary, payload)
    assert valid is True
    assert c1.is_relevant_action({"tool": "update_scheduled_transaction", "args": {"id": 7, "amount": 1300}}, payload, boundary)
    assert not c1.is_relevant_action({"tool": "update_scheduled_transaction", "args": {"id": 7, "amount": 1200}}, payload, boundary)


def test_full_restart_uses_audit_from_all_finalized_environments(tmp_path):
    """Regression: D finalizes initial and restarted banks independently."""
    path = make_trace(tmp_path)
    f = path / "events.jsonl"
    events = [json.loads(line) for line in f.read_text().splitlines()]
    for e in events:
        if e["event_type"] == "environment_finalized":
            e["data"]["audit"] = [{"call_id": "call_good", "executed": True,
                                    "state_diff": {"scheduled_transactions": {"modified": [1]}}}]
    # The original bank's incorrect action is only present in its *own* audit.
    events.insert(-1, ev(13.5, "environment_finalized", seg="s0", data={"audit": [
        {"call_id": "call_bad", "executed": True,
         "state_diff": {"scheduled_transactions": {"modified": [1]}}}]}))
    f.write_text("\n".join(json.dumps(e) for e in events))
    result = analyze(path)
    assert result["initial"]["label"] == "executed_incorrect_action"
    assert result["history"]["wrong_action_in_initial_path"] is True
    assert result["history"]["wrong_action_in_effective_path"] is False

# Additional Phase 2 semantic/edge-case coverage 

import csv
import subprocess
import pytest

RP_PAYLOAD = {
    "id": "t2_rp_accidental_rent_increase", "task_id": "user_task_2",
    "boundary": "researcher_to_planner", "family": "accidental", "operation": "corrupt_fact",
}


def rp_matcher(action, payload, boundary):
    return (action.get("tool") == "update_scheduled_transaction"
            and action.get("args", {}).get("id") == 7
            and str(action.get("args", {}).get("amount")) in {"1300", "1300.0"})


def rp_validator(boundary, payload):
    original = boundary["original"]["facts"]
    candidate = boundary["candidate"]["facts"]
    return (original["current_rent"] == candidate["current_rent"] == 1100
            and original["rent_increase"] == 100
            and candidate["rent_increase"] == 200), "isolated increase mutation"


def make_rp_trace(tmp_path, *, actions=None, tool=None, complete=True, truncated=False,
                  delivery=True, include_model=True, tool_state_changed=False,
                  tool_allowed=True, result_status="success"):
    """R->P: faulty Researcher *input* delivered to Planner, not an injected plan.

    Key invariant: a faulty input alone is never evidence the Planner adopted it.
    """
    path = tmp_path / "run_0000"
    path.mkdir(parents=True, exist_ok=True)
    (path / "manifest.json").write_text(json.dumps({
        "runner": "banking_phase2_v1", "task_id": "user_task_2", "run_id": "run_rp",
        "synthetic": True, "experiment": {"scenario_id": RP_PAYLOAD["id"],
                                          "method_id": "selective_dag"}
    }), encoding="utf-8")
    artifacts = [
        {"artifact_id": "research_original", "content": {"facts": {"current_rent": 1100, "rent_increase": 100, "rent_tx_id": 7}}},
        {"artifact_id": "research_bad", "content": {"facts": {"current_rent": 1100, "rent_increase": 200, "rent_tx_id": 7}}},
    ]
    (path / "artifacts.jsonl").write_text("\n".join(json.dumps(a) for a in artifacts), encoding="utf-8")
    es = [ev(1, "segment_started"), ev(2, "node_started", node="research"),
          ev(3, "fault_injected", data={"triggered": True, "original_artifact_id": "research_original",
                                         "replacement_artifact_id": "research_bad", "site": "research"})]
    if delivery:
        es.append(ev(4, "fault_exposure", node="plan", data={"delivered": True,
                       "artifact_id": "research_bad", "role": "planner"}))
    if include_model:
        es.append(ev(5, "model_started", node="plan", step="p0", data={"input_artifact_ids": ["research_bad"]}))
        if complete:
            content = json.dumps({"actions": actions if actions is not None else []})
            es.append(ev(6, "model_finished", node="plan", step="p0", data={
                "status": result_status,
                "output": {"role": "assistant", "content": content},
                "response": {"choices": [{"finish_reason": "length" if truncated else "stop"}]},
            }))
    if tool is not None:
        es.append(ev(7, "tool_started", node="execute", step="t0", data={
            "tool": tool["tool"], "args": tool["args"]}))
        es.append(ev(8, "tool_finished", node="execute", step="t0", data={
            "status": "success" if tool_allowed else "error", "allowed": tool_allowed,
            "state_changed": tool_state_changed, "audit_call_id": "call_rp"}))
    es.extend([
        ev(9, "recovery_started", data={"notification_boundary": "after_initial_workflow",
                                        "faulty_artifact_ids": ["research_bad"]}),
        ev(10, "segment_started", seg="s1"),
        ev(11, "recovery_finished", seg="s1", data={"completed": True,
                         "rerun_node_ids": ["plan", "execute"], "retained_artifact_ids": []}),
    ])
    if tool is not None:
        es.append(ev(12, "environment_finalized", seg="s1", data={"audit": [{
            "call_id": "call_rp", "executed": tool_allowed,
            "state_diff": {"scheduled_transactions": {"modified": [1]}} if tool_state_changed else {}
        }]}))
    (path / "events.jsonl").write_text("\n".join(json.dumps(x) for x in es), encoding="utf-8")
    (path / "evaluations.jsonl").write_text("", encoding="utf-8")
    (path / "summary.json").write_text(json.dumps({"trace_complete": True}), encoding="utf-8")
    return path


def analyze_rp(path):
    return analyze_run(path, payload=RP_PAYLOAD, matcher=rp_matcher, validator=rp_validator)


BAD_RENT = {"tool": "update_scheduled_transaction", "args": {"id": 7, "amount": 1300}}
GOOD_RENT = {"tool": "update_scheduled_transaction", "args": {"id": 7, "amount": 1200}}

# R→P Propagation Tests
def test_research_to_planner_wrong_plan_no_tool_is_propagated_to_plan(tmp_path):
    """Missing v0.2 coverage: confirmed Planner adoption stops before any tool call."""
    result = analyze_rp(make_rp_trace(tmp_path, actions=[BAD_RENT]))
    assert result["injection"]["triggered"] is True
    assert result["injection"]["valid"] is True
    assert result["initial"]["exposed"] is True
    assert result["initial"]["adopted_in_output"] is True
    assert result["initial"]["incorrect_tool_attempted"] is False
    assert result["initial"]["label"] == "propagated_to_plan"
    assert {"event_3", "event_4", "event_5", "event_6"}.issubset(set(result["initial"]["evidence_event_ids"]))


def test_rp_faulty_research_input_without_plan_adoption_is_not_propagated(tmp_path):
    result = analyze_rp(make_rp_trace(tmp_path, actions=[GOOD_RENT]))
    assert result["initial"]["label"] == "exposed_not_adopted"
    assert result["initial"]["adopted_in_output"] is False


def test_rp_rejection_in_plan_is_not_adoption(tmp_path):
    result = analyze_rp(make_rp_trace(tmp_path, actions=[]))
    assert result["initial"]["label"] == "exposed_not_adopted"


@pytest.mark.parametrize("truncated,complete,result_status", [
    (True, True, "success"),
    (False, False, "success"),
    (False, True, "error"),
])
def test_rp_partial_missing_or_failed_plan_is_uncertain(tmp_path, truncated, complete, result_status):
    path = make_rp_trace(tmp_path, actions=[BAD_RENT], truncated=truncated,
                         complete=complete, result_status=result_status)
    result = analyze_rp(path)
    assert result["initial"]["label"] == "uncertain"
    assert result["initial"]["adopted_in_output"] is None


def test_rp_plan_then_wrong_tool_blocked_is_attempted(tmp_path):
    result = analyze_rp(make_rp_trace(tmp_path, actions=[BAD_RENT], tool=BAD_RENT,
                                      tool_state_changed=False, tool_allowed=False))
    assert result["initial"]["label"] == "attempted_incorrect_action"
    assert result["initial"]["incorrect_state_change_confirmed"] is None


def test_rp_plan_then_wrong_tool_audited_effect_is_executed(tmp_path):
    result = analyze_rp(make_rp_trace(tmp_path, actions=[BAD_RENT], tool=BAD_RENT,
                                      tool_state_changed=True, tool_allowed=True))
    assert result["initial"]["label"] == "executed_incorrect_action"
    assert result["initial"]["matched_audit_call_ids"] == ["call_rp"]


def test_rp_correct_tool_must_not_match_wrong_amount(tmp_path):
    result = analyze_rp(make_rp_trace(tmp_path, actions=[GOOD_RENT], tool=GOOD_RENT,
                                      tool_state_changed=True, tool_allowed=True))
    assert result["initial"]["label"] == "exposed_not_adopted"
    assert result["initial"]["incorrect_tool_attempted"] is False


def test_rp_no_confirmed_delivery_or_model_result_is_uncertain(tmp_path):
    result = analyze_rp(make_rp_trace(tmp_path, delivery=False, include_model=False))
    assert result["initial"]["label"] == "uncertain"
    assert result["initial"]["exposed"] is None

# edge test for recovery, history and injection
def test_full_restart_history_does_not_change_original_label(tmp_path):
    result = analyze(make_trace(tmp_path))
    assert result["initial"]["label"] == "executed_incorrect_action"
    assert result["recovery"]["completed"] is True
    assert result["history"]["wrong_action_in_initial_path"] is True
    assert result["history"]["wrong_action_in_effective_path"] is False
    assert result["final"]["strict_success"] is True


def test_no_history_evaluator_does_not_fabricate_a_path_judgment(tmp_path):
    result = analyze(make_trace(tmp_path, history=False))
    assert result["history"]["wrong_action_in_initial_path"] is None
    assert result["history"]["wrong_action_in_effective_path"] is None
    assert result["initial"]["label"] == "executed_incorrect_action"


def test_no_injection_is_tracked_separately_from_exposure(tmp_path):
    """No new propagation label is introduced for an unapplied fault."""
    result = analyze(make_trace(tmp_path, include_injection=False, tool_amount=None))
    assert result["injection"]["triggered"] is False
    assert result["injection"]["valid"] is None
    assert result["initial"]["label"] == "uncertain"


def test_output_evidence_ids_are_actual_trace_ids(tmp_path):
    path = make_trace(tmp_path)
    result = analyze(path)
    source = {x["event_id"] for x in (json.loads(line) for line in
                                  (path / "events.jsonl").read_text().splitlines())}
    assert result["initial"]["evidence_event_ids"]
    assert set(result["initial"]["evidence_event_ids"]).issubset(source)
    assert set(result["recovery"]["evidence_event_ids"]).issubset(source)
    assert result["initial"]["segment_id"] not in result["recovery"]["segment_ids"]


def test_duplicate_tool_attempt_without_audit_remains_attempted(tmp_path):
    path = make_trace(tmp_path)
    f = path / "events.jsonl"
    es = [json.loads(x) for x in f.read_text().splitlines()]
    # Recorded tool success cannot by itself independently prove effect.
    for event in es:
        if event["event_type"] == "environment_finalized":
            event["data"]["audit"] = [{"call_id": "unrelated", "executed": True,
                                         "state_diff": {"scheduled_transactions": {"modified": [1]}}}]
    f.write_text("\n".join(json.dumps(x) for x in es))
    result = analyze(path)
    assert result["initial"]["label"] == "attempted_incorrect_action"
    assert result["initial"]["matched_audit_call_ids"] == ["call_bad"]


def test_c3_cli_writes_json_csv_and_skips_phase1(tmp_path):
    """End-to-end through the real CLI without requiring external payloads."""
    root = tmp_path / "dataset"
    root.mkdir()
    (root / "phase2").mkdir()
    (root / "phase1").mkdir()
    p2 = make_trace(root / "phase2", early=True, tool_amount=None,
                    include_injection=False)
    # Use a clean Phase 2 trial for CLI plumbing so this test is independent
    # of Phase 1's full payloads.yaml definitions.
    meta = json.loads((p2 / "manifest.json").read_text())
    meta["experiment"]["scenario_id"] = "clean"
    (p2 / "manifest.json").write_text(json.dumps(meta))
    p1 = make_trace(root / "phase1", early=True, tool_amount=None)
    manifest = json.loads((p1 / "manifest.json").read_text())
    manifest["runner"] = "banking_phase1_v1"
    (p1 / "manifest.json").write_text(json.dumps(manifest))
    output = tmp_path / "output"
    proc = subprocess.run([sys.executable, str(Path(__file__).resolve().parents[1]
                       / "scripts" / "analyze_phase2_propagation.py"),
                       "--runs-root", str(root), "--output", str(output)],
                       capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert "Analyzed 1 Phase 2 runs; skipped 1 Phase 1 runs" in proc.stdout
    results = json.loads((output / "propagation_phase2.json").read_text(encoding="utf-8"))
    with (output / "propagation_phase2.csv").open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert len(results) == len(rows) == 1
    assert results[0]["initial"]["label"] == rows[0]["label"] == "not_applicable"


def test_model_pairing_does_not_cross_nodes_with_reused_step_ids():
    """The real-model interface may reuse step IDs across different nodes."""
    from analyze_phase2_propagation import _model_pairs
    es = [
        ev(1, "model_started", node="plan", step="shared", data={"input_artifact_ids": ["bad"]}),
        ev(2, "model_started", node="execute", step="shared", data={"input_artifact_ids": ["good"]}),
        ev(3, "model_finished", node="execute", step="shared", data={"status": "success"}),
        ev(4, "model_finished", node="plan", step="shared", data={"status": "success"}),
    ]
    pairs = _model_pairs(es)
    assert [(a["node_id"], b["node_id"] if b else None) for a, b in pairs] == [
        ("execute", "execute"), ("plan", "plan")]


def test_tool_pairing_does_not_cross_nodes_with_reused_step_ids():
    from analyze_phase2_propagation import _tool_pairs
    es = [
        ev(1, "tool_started", node="research", step="shared", data={"tool": "read_file"}),
        ev(2, "tool_started", node="execute", step="shared", data={"tool": "send_money"}),
        ev(3, "tool_finished", node="execute", step="shared", data={"status": "success"}),
        ev(4, "tool_finished", node="research", step="shared", data={"status": "success"}),
    ]
    pairs = _tool_pairs(es)
    assert [(a["node_id"], b["node_id"] if b else None) for a, b in pairs] == [
        ("execute", "execute"), ("research", "research")]


def test_boundary_delivery_without_provider_ack_is_not_confirmed_exposure(tmp_path):
    """D emits a boundary-level delivered record before provider model calls.

    This alone does not confirm that the model actually received the request.
    """
    result = analyze_rp(make_rp_trace(tmp_path, delivery=True, include_model=False))
    assert result["injection"]["triggered"] is True
    assert result["initial"]["exposed"] is None
    assert result["initial"]["label"] == "uncertain"


def test_provider_ack_is_exposure_not_adoption(tmp_path):
    path = make_rp_trace(tmp_path, delivery=True, include_model=False)
    f = path / "events.jsonl"
    es = [json.loads(x) for x in f.read_text().splitlines()]
    for e in es:
        if e["event_type"] == "fault_exposure":
            e["data"]["meaning"] = "input_in_provider_acknowledged_request_not_proof_of_adoption"
    f.write_text("\n".join(json.dumps(x) for x in es))
    result = analyze_rp(path)
    assert result["initial"]["exposed"] is True
    assert result["initial"]["adopted_in_output"] is None
    assert result["initial"]["label"] == "uncertain"
