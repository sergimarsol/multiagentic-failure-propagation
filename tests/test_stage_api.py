"""Public B stage contract against the real bank/recorder and deterministic models."""

from dataclasses import replace
import json

import pytest

from env import BankingEnv
from experiments.banking import BankingAdapter
from experiments.phase1 import Callbacks
from experiments.protocol import Budget, Limits
from experiments.recorder import Recorder, TraceError
from tests.test_workflow import FakeModel, run as run_phase1_fixture
from workflow import BankingWorkflowSession, StageState, WorkflowConfig
from workflow.contracts import InjectionGate, WorkflowError
from workflow.client import ModelRequestError
from workflow.prompts import PROMPTS


class StageHarness:
    def __init__(self, path, task="user_task_0", model=None, config=None):
        self.model = model or FakeModel()
        self.config = config or WorkflowConfig()
        self.env = BankingEnv().load(task)
        self.rec = Recorder(path, {"run_id": "stage_test", "task_id": task})
        self.rec.budget = Budget(Limits())
        self.cb = Callbacks(self.rec, "attempt_0")
        self.bank = BankingAdapter(self.env, self.rec)
        with self.cb.node("setup") as execution:
            self.request = self.cb.artifact(execution, {"user_request": self.env.prompt})
        self.state = None

    def run(self, stage, inputs=None, **kwargs):
        session = BankingWorkflowSession(self.model, self.config)
        result = session.run_stage(stage, environment=self.bank, callbacks=self.cb,
            request_id=self.request, inputs=inputs, state=self.state, **kwargs)
        # Every call resumes from persisted JSON in a completely fresh B session.
        self.state = json.loads(json.dumps(result.state.to_dict()))
        return result


def normalized_requests(requests):
    def normalize(value):
        if isinstance(value, dict):
            return {k: "audit_id" if k == "audit_call_id" else normalize(v) for k, v in value.items()}
        if isinstance(value, list):
            return [normalize(v) for v in value]
        return value
    rows = json.loads(json.dumps(requests))
    for request in rows:
        body = json.loads(request["messages"][1]["content"])
        request["messages"][1]["content"] = json.dumps(normalize(body), sort_keys=True)
    return rows


@pytest.mark.parametrize("task", ["user_task_0", "user_task_2"])
@pytest.mark.parametrize("verify", [False, True])
def test_public_stages_match_phase1_requests_tools_and_costs(tmp_path, task, verify):
    old_model = FakeModel()
    summary, _, _, _ = run_phase1_fixture(tmp_path / "old", task=task, model=old_model,
                                         condition="clean_verify" if verify else "clean")
    h = StageHarness(tmp_path / "stages", task)
    research = h.run("research")
    if verify:
        h.run("verify_research", {"candidate": research.artifact_id})
    plan = h.run("plan", {"research": research.artifact_id})
    if verify:
        h.run("verify_plan", {"candidate": plan.artifact_id})
    result = h.run("execute", {"plan": plan.artifact_id})
    assert normalized_requests(h.model.requests) == normalized_requests(old_model.requests)
    assert result.state.model_calls == summary["total_cost"]["model_calls"]
    assert result.state.tool_calls == summary["total_cost"]["tool_calls"]
    assert result.state.to_dict() == StageState.from_dict(h.state).to_dict()
    assert result.state.evidence_ids == research.state.evidence_ids
    assert len(result.messages) == 1  # executor has one fresh dialogue
    from eval import evaluate
    scored = evaluate(task, h.env.initial_state, h.env.snapshot(), h.env.audit_log())
    assert scored.strict_success and summary["final_strict_success"]


@pytest.mark.parametrize("site", ["research", "plan"])
def test_verified_regeneration_matches_phase1_and_keeps_evidence(tmp_path, site):
    def inject(body, boundary):
        if boundary != site:
            return {"triggered": False}
        if site == "research":
            body["facts"]["bill_amount"] = 1337.42
        else:
            body["actions"][0]["args"]["amount"] = 1337.42
        return {"triggered": True, "replacement": body, "fault_type": "fixture"}

    old_model = FakeModel()
    summary, _, _, _ = run_phase1_fixture(tmp_path / "old", model=old_model,
        condition="always_verify", injector=inject)
    h = StageHarness(tmp_path / "stages")
    gate = InjectionGate(inject)
    research_id = None
    for stage in ("research", "plan"):
        inputs = {} if stage == "research" else {"research": research_id}
        produced = h.run(stage, inputs)
        delivered = h.cb.inject(produced.artifact_id, produced.execution_id, gate,
                                 site=stage, enabled=True)
        verified = h.run("verify_" + stage, {"candidate": delivered})
        if stage == site:
            assert not h.cb.content(verified.artifact_id)["accepted"]
            regenerated = h.run(stage, inputs, feedback=verified.artifact_id, generation=1)
            assert regenerated.control_artifact_ids == (verified.artifact_id,)
            assert len(regenerated.messages) == 1  # rejected dialogue never restored
            delivered = regenerated.artifact_id
            verified = h.run("verify_" + stage, {"candidate": delivered}, generation=1)
        assert h.cb.content(verified.artifact_id)["accepted"]
        if stage == "research":
            research_id = delivered
    h.run("execute", {"plan": delivered})
    assert normalized_requests(h.model.requests) == normalized_requests(old_model.requests)
    assert h.state["model_calls"] == summary["total_cost"]["model_calls"]
    assert h.state["tool_calls"] == summary["total_cost"]["tool_calls"]
    events = h.rec.records("events")
    assert sum(e["data"]["triggered"] for e in events if e["event_type"] == "fault_injected") == 1
    controls = [e["data"]["control_artifact_ids"] for e in events
                if e["event_type"] == "dependencies_recorded" and e["data"].get("control_artifact_ids")]
    assert len(controls) == 1


@pytest.mark.parametrize("mutation", ["version", "missing", "extra", "negative", "boolean", "duplicates"])
def test_stage_state_rejects_invalid_serialization(mutation):
    state = StageState("run", "attempt", "hash", evidence_ids=("evidence",)).to_dict()
    if mutation == "version":
        state["version"] = "future"
    elif mutation == "missing":
        del state["version"]
    elif mutation == "extra":
        state["hidden_history"] = []
    elif mutation == "negative":
        state["model_calls"] = -1
    elif mutation == "boolean":
        state["tool_calls"] = True
    else:
        state["evidence_ids"] *= 2
    with pytest.raises(WorkflowError):
        StageState.from_dict(state)


@pytest.mark.parametrize("mutation", ["run_id", "attempt_id", "config_hash", "model", "session", "prompt", "evidence"])
def test_incompatible_continuation_is_rejected_before_model_calls(tmp_path, mutation, monkeypatch):
    h = StageHarness(tmp_path)
    research = h.run("research")
    if mutation in {"run_id", "attempt_id", "config_hash"}:
        h.state[mutation] = "different"
    elif mutation == "model":
        h.model.config = replace(h.model.config, temperature=0.5)
    elif mutation == "session":
        h.config = replace(h.config, max_tool_calls=100)
    elif mutation == "prompt":
        monkeypatch.setitem(PROMPTS, "planner", "changed prompt")
    else:
        h.state["evidence_ids"] = [research.artifact_id]  # summary is not tool evidence
    calls = len(h.model.requests)
    with pytest.raises(WorkflowError):
        h.run("plan", {"research": research.artifact_id})
    assert len(h.model.requests) == calls


def test_context_exports_are_detached_and_new_sessions_cannot_be_reused(tmp_path):
    h = StageHarness(tmp_path)
    session = BankingWorkflowSession(h.model)
    args = dict(environment=h.bank, callbacks=h.cb, request_id=h.request)
    result = session.run_stage("research", **args)
    exported = result.to_context()
    exported["messages"][0][0]["content"] = "polluted"
    exported["resume_state"]["evidence_ids"].clear()
    assert result.state.evidence_ids and result.messages[0][0]["content"] != "polluted"
    with pytest.raises(WorkflowError, match="single-use"):
        session.run_stage("research", **args)
    with pytest.raises(WorkflowError, match="single-use"):
        session.run(h.env, h.cb, None, injection_enabled=False, always_verify=False)


def test_stage_limits_continue_across_fresh_sessions(tmp_path):
    h = StageHarness(tmp_path, config=WorkflowConfig(max_model_calls=2))
    research = h.run("research")
    assert research.state.model_calls == 2
    with pytest.raises(WorkflowError, match="budget exhausted"):
        h.run("plan", {"research": research.artifact_id})
    assert len(h.model.requests) == 2 and h.rec.budget.calls["model"] == 2


def test_failed_regeneration_records_feedback_without_publishing_state(tmp_path):
    from workflow.fixtures import wrong_amount

    h = StageHarness(tmp_path)
    research = h.run("research")
    delivered = h.cb.inject(research.artifact_id, research.execution_id,
        InjectionGate(wrong_amount(task_id="user_task_0")), site="research", enabled=True)
    verdict = h.run("verify_research", {"candidate": delivered})
    assert not h.cb.content(verdict.artifact_id)["accepted"]
    previous = json.loads(json.dumps(h.state))
    h.model.fail_once = True
    with pytest.raises(ModelRequestError):
        h.run("research", feedback=verdict.artifact_id, generation=1)
    assert h.state == previous  # only completed-stage states can be resumed
    events = h.rec.records("events")
    controls = next(e for e in events if e["event_type"] == "dependencies_recorded"
                    and e["data"].get("control_artifact_ids") == [verdict.artifact_id])
    failed_call = [e for e in events if e["event_type"] == "model_started"][-1]
    assert controls["event_seq"] < failed_call["event_seq"]
    assert verdict.artifact_id in failed_call["data"]["input_artifact_ids"]
    assert not h.rec.active_calls


@pytest.mark.parametrize("bad_input", ["wrong_request", "missing_upstream", "foreign_artifact", "unknown_artifact",
                                      "unknown_stage", "unexplained_generation", "accepted_feedback", "missing_state"])
def test_stage_input_contract_fails_before_execution(tmp_path, bad_input):
    h = StageHarness(tmp_path)
    research = h.run("research")
    kwargs = dict(environment=h.bank, callbacks=h.cb, request_id=h.request,
                  inputs={"research": research.artifact_id}, state=h.state)
    stage = "plan"
    if bad_input == "wrong_request":
        kwargs["request_id"] = research.artifact_id
    elif bad_input == "missing_upstream":
        kwargs["inputs"] = {}
    elif bad_input == "foreign_artifact":
        other = Callbacks(h.rec, "attempt_other")
        with other.node("plan") as execution:
            kwargs["inputs"] = {"research": other.artifact(execution, h.cb.content(research.artifact_id))}
    elif bad_input == "unknown_artifact":
        kwargs["inputs"] = {"research": "missing"}
    elif bad_input == "unknown_stage":
        stage = "other"
    elif bad_input == "unexplained_generation":
        kwargs["generation"] = 1
    elif bad_input == "missing_state":
        kwargs["state"] = None
    else:
        verdict = h.run("verify_research", {"candidate": research.artifact_id})
        kwargs.update(state=h.state, feedback=verdict.artifact_id, generation=1)
    calls = len(h.model.requests)
    with pytest.raises((WorkflowError, TraceError)):
        BankingWorkflowSession(h.model).run_stage(stage, **kwargs)
    assert len(h.model.requests) == calls
