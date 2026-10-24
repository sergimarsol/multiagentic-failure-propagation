"""C/B/D integration: real B Session + C injection + D persistent Recorder/Callbacks.

A's banking tools and the model responses are deterministic stubs. These are
software/trace tests, NOT measurements of actual LLM or AgentDojo behavior.

Run from the repository root:
    uv run python -m pytest -v tests/test_d_trace_integration.py

Requires the existing tests/test_workflow_session_integration.py file, which
provides the already-tested MockClient and MockEnvironment.
"""
from __future__ import annotations

import json
from copy import deepcopy
from types import SimpleNamespace

import pytest

from faults.injection_harness import FaultInjector, make_injector
from experiments.models import new_id
from experiments.phase1 import run_phase1
from workflow.contracts import InjectionSpec
from workflow.evidence import export_cases
import workflow.session as workflow_session

from test_workflow_session_integration import MockClient, MockEnvironment

CASES = FaultInjector.from_yaml().cases

"""Simulate Banking tool execution while preserving real D tracing.

Provides deterministic read/write results and records tool execution,
arguments, call IDs, and state-change flags through the real recorder.
"""
class TracedMockBankingAdapter:
    """Fake A tool results, but REAL D model/tool event persistence."""

    def __init__(self, env, recorder):
        self.env = env
        self.recorder = recorder

    def call_tool(self, role, name, args, *, attempt_id, node_id,
                  node_execution_id, inputs=()):
        """Execute a simulated Banking tool call with real trace recording.

        Returns deterministic read results or records simulated write actions.
        Uses D's recorder to persist tool events, invocation metadata, and
        state-change flags without calling the real Banking Environment.
        """
        step_id = new_id("step")
        call_id = new_id("mockcall")
        with self.recorder.call(
            "tool", attempt_id, step_id=step_id, node_id=node_id,
            node_execution_id=node_execution_id, role=role, tool=name,
            args=deepcopy(args), input_artifact_ids=list(inputs),
        ) as terminal:
            if role == "researcher" and name == "read_file":
                text = (
                    "invoice: amount=98.70, iban=UK12345678901234567890"
                    if self.env.task_id == "user_task_0"
                    else "rent notice: current_rent=1100, rent_increase=100, rent_tx_id=7"
                )
                changed = False
            elif role == "executor" and name in {"send_money", "update_scheduled_transaction"}:
                text = "simulated write success"
                self.env.executed.append({"tool": name, "args": deepcopy(args)})
                changed = True
            else:
                raise AssertionError(f"Unexpected mock tool: {role} {name}")
            terminal.update(
                status="success", error=None, result_text=text,
                allowed=True, invoked=True, state_changed=changed,
                audit_call_id=call_id,
            )
        self.env.audit.append({
            "call_id": call_id, "step_id": step_id, "role": role,
            "tool": name, "args": deepcopy(args), "allowed": True,
            "executed": True, "state_changed": changed,
        })
        return SimpleNamespace(error=None, result_text=text, call_id=call_id)


class SyntheticEnvironmentAdapter:
    """Provide a synthetic Banking environment interface for integration tests.

    Tracks simulated writes and audit records while leaving task success
    and authorization outcomes unknown instead of fabricating A scores.
    """

    def __init__(self, task_id):
        self.task_id = task_id

    def create_initial_environment(self, task_id):
        """Create a fresh mock Banking environment with an empty tool audit."""
        env = MockEnvironment(task_id)
        env.audit = []
        return env

    def read_state(self, env):
        """Return a snapshot of simulated Banking write operations."""
        return {"simulated_writes": deepcopy(env.executed)}

    def trace(self, env):
        """Return a copy of the simulated tool audit for trace persistence."""
        return deepcopy(env.audit)

    def evaluate_execution(self, initial_state, final_state, trace):
        return {
            "task_success": None,
            "agentdojo_utility": None,
            "strict_success": None,
            "permission_violations": None,
            "unauthorized_attempt_ids": None,
            "unauthorized_execution_ids": None,
            "evidence_ids": [entry["call_id"] for entry in trace],
            "details": {"scoring": "synthetic_unknown_not_A_scoring"},
        }


def execute_synthetic_trial(monkeypatch, tmp_path, scenario_id, *,
                            condition="fault", verdict_policy="accept"):
    """Run one synthetic B/C/D integration trial with persistent tracing.

    Replaces real Banking tools and LLM responses with deterministic mocks,
    while exercising B's actual workflow, C's injector, and D's recorder.
    Returns the run directory, summary, recorded events, artifacts, and clients.
    """
    case = CASES[scenario_id]
    site = "research" if case["boundary"] == "researcher_to_planner" else "plan"
    monkeypatch.setattr(workflow_session, "BankingAdapter", TracedMockBankingAdapter)
    clients = []

    def create_session():
        """Create a fresh mock-model session for a workflow attempt."""
        client = MockClient(case["task_id"], verdict_policy=verdict_policy,
                            executor_writes=True)
        clients.append(client)
        return workflow_session.BankingWorkflowSession(
            client, injection_spec=InjectionSpec(scenario_id, site)
        )

    output = tmp_path / f"{scenario_id}-{condition}"
    summary = run_phase1(
        output,
        task_id=case["task_id"],
        condition=condition,
        environment_adapter=SyntheticEnvironmentAdapter(case["task_id"]),
        create_session=create_session,
        injector=make_injector(task_id=case["task_id"], scenario_id=scenario_id),
        synthetic=True,
    )

    def load_jsonl(name):
        return [json.loads(line) for line in (output / name).read_text(encoding="utf-8").splitlines()]

    return output, summary, load_jsonl("events.jsonl"), load_jsonl("artifacts.jsonl"), clients

"""Verify injection lineage and downstream exposure for all eight scenarios.

Checks that the selected handoff is replaced, D links the original and
injected artifacts, the correct consumer receives the faulty artifact,
and B's exporter preserves the evidence in fault_cases.json.
"""
@pytest.mark.parametrize("scenario_id", list(CASES))
def test_all_8_faults_have_real_d_artifact_lineage_and_exposure(
    monkeypatch, tmp_path, scenario_id,
):
    """Only selected boundary is injected and actually delivered to model input."""
    output, summary, events, artifacts, clients = execute_synthetic_trial(
        monkeypatch, tmp_path, scenario_id
    )
    assert summary["trace_complete"] is True
    assert summary["synthetic"] is True
    assert summary["injection_triggered"] is True

    selected_site = (
        "research" if CASES[scenario_id]["boundary"] == "researcher_to_planner" else "plan"
    )
    triggered = [e for e in events if e["event_type"] == "fault_injected"
                 and e["data"]["triggered"]]
    assert len(triggered) == 1
    injection = triggered[0]["data"]
    assert injection["site"] == selected_site
    assert injection["original_artifact_id"] != injection["replacement_artifact_id"]

    by_id = {a["artifact_id"]: a for a in artifacts}
    original_id = injection["original_artifact_id"]
    faulty_id = injection["replacement_artifact_id"]
    assert by_id[faulty_id]["derived_from_artifact_id"] == original_id
    assert by_id[original_id]["content"] != by_id[faulty_id]["content"]

    consumer = "planner" if selected_site == "research" else "executor"
    deliveries = [e for e in events if e["event_type"] == "model_started"
                  and e["data"].get("role") == consumer
                  and faulty_id in e["data"].get("input_artifact_ids", [])]
    assert deliveries, f"{scenario_id}: consumer did not reference injected artifact"
    assert any(e["event_type"] == "fault_exposure"
               and e["data"].get("delivered") is True
               and e["data"].get("artifact_id") == faulty_id
               and e["data"].get("role") == consumer for e in events)

    decisions = [e for e in events if e["event_type"] == "injection_attempt_finished"]
    assert len(decisions) == 1 and decisions[0]["data"]["applied"] is True

    # B's exporter consumes D's ACTUAL JSONL records, not hand-written event rows.
    bundle = export_cases(output)
    assert (output / "fault_cases.json").exists()
    assert bundle["attempts"][0]["injection"]["applied"] is True
    assert bundle["attempts"][0]["classification"]["observation_stages"]["consumer_exposed"] is True

"""Verify that full restart records both attempts without reinjection.

Checks that the initial attempt receives the configured fault,
the restarted attempt uses a fresh session with injection disabled,
and D preserves both attempts in the execution trace.
"""
@pytest.mark.parametrize("scenario_id", [
    "t0_rp_accidental_amount", "t2_pe_adversarial_extra_payment",
])
def test_full_restart_persists_both_attempts_without_reinjection(
    monkeypatch, tmp_path, scenario_id,
):
    """Actual D full_restart factory generates a fresh attempt; C is off on rerun."""
    _, summary, events, _, clients = execute_synthetic_trial(
        monkeypatch, tmp_path, scenario_id, condition="full_restart"
    )
    assert summary["trace_complete"] is True
    assert summary["recovery_attempted"] is True
    assert len(clients) == 2
    assert {e["attempt_id"] for e in events if e["event_type"] == "attempt_started"} == {
        "attempt_0", "attempt_1"
    }
    triggers = [e for e in events if e["event_type"] == "fault_injected"
                and e["data"]["triggered"]]
    assert len(triggers) == 1 and triggers[0]["attempt_id"] == "attempt_0"
    assert all(not e["data"]["enabled"] for e in events
               if e["event_type"] == "fault_injected" and e["attempt_id"] == "attempt_1")
    finished = [e for e in events if e["event_type"] == "injection_attempt_finished"]
    assert [e["data"]["applied"] for e in finished] == [True, False]
    assert finished[1]["data"]["reason"] == "disabled"

"""Verify that rejection prevents injected data from reaching Planner.

Checks that the mock verifier rejects the faulty candidate, accepts
the clean retry, and B delivers only the original facts to Planner.
"""
def test_verifier_receives_fault_then_retries_without_leaking_to_planner(
    monkeypatch, tmp_path,
):
    """Verifier sees candidate; a rejection prevents exposure to downstream Planner."""
    _, summary, events, artifacts, clients = execute_synthetic_trial(
        monkeypatch, tmp_path, "t2_rp_accidental_rent_increase",
        condition="always_verify", verdict_policy="reject_injected",
    )
    assert summary["trace_complete"] is True
    decisions = [e["data"]["accepted"] for e in events
                 if e["event_type"] == "verification_decision"
                 and e["data"]["site"] == "research"]
    assert decisions == [False, True]
    assert sum(e["event_type"] == "fault_injected" and e["data"]["triggered"]
               for e in events) == 1
    assert any(e["event_type"] == "verifier_exposure" and e["data"]["delivered"] for e in events)
    assert not any(e["event_type"] == "fault_exposure" and e["data"]["role"] == "planner"
                   for e in events)
    planner = [c for c in clients[0].calls if c["role"] == "planner"]
    assert planner and planner[0]["body"]["research"]["facts"]["rent_increase"] == 100

"""Verify D's event sequence and simulated tool-call trace integrity.

Checks sequential event IDs, matching model/tool start and finish
events, preservation of injected tool arguments, and explicitly
unknown strict-scoring results for the synthetic environment.
"""
def test_real_d_recorder_tracks_tool_call_and_event_sequence(monkeypatch, tmp_path):
    """D recorder has exact start/end joins and traceable simulated tool action."""
    output, summary, events, artifacts, clients = execute_synthetic_trial(
        monkeypatch, tmp_path, "t2_rp_accidental_rent_increase"
    )
    assert [e["event_seq"] for e in events] == list(range(1, len(events) + 1))
    starts = {e["step_id"]: e for e in events if e["event_type"] in {"model_started", "tool_started"}}
    ends = {e["step_id"]: e for e in events if e["event_type"] in {"model_finished", "tool_finished"}}
    assert starts.keys() == ends.keys()
    writes = [e for e in events if e["event_type"] == "tool_started"
              and e["data"].get("tool") == "update_scheduled_transaction"]
    assert len(writes) == 1
    assert writes[0]["data"]["args"]["amount"] == 1300
    finished = [e for e in events if e["event_type"] == "environment_finalized"]
    assert len(finished) == 1
    assert any(a["tool"] == "update_scheduled_transaction" and a["args"]["amount"] == 1300
               for a in finished[0]["data"]["audit"])
    assert (output / "summary.json").exists()
    assert (output / "evaluations.jsonl").exists()
    assert summary["final_strict_success"] is None  # Synthetic A stub does not score.
