"""D contracts and failure semantics; all fixtures are explicitly synthetic."""

from dataclasses import replace
import json

import pytest

from experiments.metrics import summarize_run
from tests.d_fixtures import (MockEnvironment, MockEvaluator, MockInjector, MockWorkflow,
                              NODES, run_mock)
from experiments.models import Artifact, Evaluation, Event, new_id
from experiments.recorder import DependencyStore, Recorder, TraceError, read_jsonl
from experiments.recovery import RecoveryPointManager


def setup(tmp_path, scenario="restart"):
    rec = Recorder(tmp_path, {"run_id": "test_run", "synthetic": True})
    env = MockEnvironment()
    injector = MockInjector(rec, scenario)
    workflow = MockWorkflow(rec, env, injector, scenario)
    points = RecoveryPointManager(rec, env, workflow)
    return rec, env, injector, workflow, points


@pytest.mark.parametrize("scenario,success,triggered,exposed,attempts,executions", [
    ("clean", True, False, False, 0, 0),
    ("untriggered", True, False, False, 0, 0),
    ("ignored", True, True, True, 0, 0),
    ("rejected", False, True, True, 1, 0),
    ("executed", False, True, True, 1, 1),
])
def test_scenarios(tmp_path, scenario, success, triggered, exposed, attempts, executions):
    result = run_mock(tmp_path, scenario)
    assert result["synthetic"] is True
    assert result["initial_task_success"] is success
    assert result["injection_triggered"] is triggered
    assert result["downstream_exposed"] is exposed
    assert result["unauthorized_attempts"] == attempts
    assert result["unauthorized_executions"] == executions
    assert not result["recovery_attempted"]
    assert result["recovery_task_success"] is None


@pytest.mark.parametrize("site", ["research", "plan"])
def test_restart_preserves_history_and_cost(tmp_path, site):
    summary = run_mock(tmp_path, "restart", site=site)
    assert summary["initial_task_success"] is False
    assert summary["final_task_success"] is True
    assert summary["recovery_completed"] is True
    assert summary["unauthorized_executions"] == 1
    assert summary["recomputed_nodes"] == 3
    assert summary["total_cost"]["model_calls"] == 6
    assert summary["total_cost"]["tool_calls"] == 4
    assert summary["recovery_added_cost"]["input_tokens"] == 30
    events, _ = read_jsonl(tmp_path / "events.jsonl")
    assert sum(e["event_type"] == "fault_injected" and e["data"]["triggered"] for e in events) == 1
    assert len({e["node_execution_id"] for e in events if e["event_type"] == "node_started"}) == 6
    assert not any(e["event_type"] == "checkpoint_created" for e in events)
    initial_end = next(i for i, e in enumerate(events) if e["event_type"] == "recovery_started")
    for e in events[initial_end:]:
        if e["event_type"] == "model_started":
            assert "999" not in json.dumps(e["data"]["messages"])


def test_usage_failed_verifier_and_retry_are_counted_once(tmp_path):
    s = run_mock(tmp_path, "usage_retry")
    c = s["total_cost"]
    assert c["model_calls"] == 5 and c["tool_calls"] == 2
    assert c["failed_calls"] == 1
    assert c["input_tokens"] is None and c["known_input_tokens"] == 5
    assert c["output_tokens"] is None and c["known_output_tokens"] == 10
    assert c["calls_missing_usage"] == 4
    assert c["usage_complete"] is False


def test_checkpoint_detaches_restores_context_and_cache(tmp_path):
    r, env, inj, w, points = setup(tmp_path)
    original = w.export_state()
    c0 = points.capture("a0", "C0", injection_phase="before_injection")
    w.run("a0")
    env.state["bill"]["amount"] = 88
    assert w.cache
    points.restore(c0.checkpoint_id, "a1")
    assert env.state == {"bill": {"recipient": "fixture-recipient", "amount": 10}, "payments": []}
    assert w.export_state() == original and not w.cache
    assert all(len(messages) == 2 for messages in w.state["contexts"].values())
    assert len(env.audit) == 2  # audit is not part of the restored state
    assert points.load(c0.checkpoint_id)[1]["bill"]["amount"] == 10


def test_checkpoint_inflight_partial_corrupt_and_config(tmp_path):
    r, env, inj, w, p = setup(tmp_path)
    with r.call("model", "a0", messages=[]):
        with pytest.raises(TraceError, match="in-flight"):
            p.capture("a0", "C1", injection_phase="no_injection")
    w.state.update(position=1, completed_nodes=["research"], pending_nodes=["plan", "execute"])
    cp = p.capture("a0", "C1", injection_phase="no_injection")
    (tmp_path / "checkpoints" / "orphan").mkdir()
    (tmp_path / "checkpoints" / "orphan" / "environment.json").write_text("{}")
    assert len(p.available()) == 1
    with pytest.raises(TraceError, match="unavailable"):
        p.restore("orphan", "a1")
    path = tmp_path / "checkpoints" / cp.checkpoint_id / "environment.json"
    path.write_text("{}")
    with pytest.raises(TraceError, match="checksum"):
        p.restore(cp.checkpoint_id, "a1")
    with pytest.raises(TraceError, match="manifest"):
        Recorder(tmp_path, {"run_id": "test_run", "synthetic": False})


def test_capture_failure_never_publishes_checkpoint(tmp_path, monkeypatch):
    import experiments.recovery as module
    r, env, inj, w, p = setup(tmp_path)
    original = module.atomic_json
    def fail_second(path, data):
        if path.name == "execution.json":
            raise OSError("disk full")
        original(path, data)
    monkeypatch.setattr(module, "atomic_json", fail_second)
    with pytest.raises(OSError):
        p.capture("a0", "C0", injection_phase="before_injection")
    assert p.available() == []
    assert r.records("events")[-1]["event_type"] == "checkpoint_failed"


def test_no_unverified_cache_cleanup(tmp_path):
    r, env, inj, w, p = setup(tmp_path)
    cp = p.capture("a0", "C0", injection_phase="before_injection")
    w.clear_runtime_caches = lambda: {"complete": False}
    with pytest.raises(TraceError, match="cache"):
        p.restore(cp.checkpoint_id, "a1")


def test_artifacts_immutable_actual_version_dependencies(tmp_path):
    run_mock(tmp_path, "executed", run_id="run_test")
    r = Recorder(tmp_path, json.loads((tmp_path / "manifest.json").read_text()))
    artifacts = r.records("artifacts")
    corrupted = next(a for a in artifacts if a["derived_from_artifact_id"])
    original = r.artifact(corrupted["derived_from_artifact_id"])
    assert original["content"]["amount"] == 10
    assert corrupted["content"]["amount"] == 999
    planner = next(a for a in artifacts if a["artifact_type"] == "plan")
    assert planner["input_artifact_ids"] == [corrupted["artifact_id"]]
    corrupted["content"]["amount"] = 0
    assert r.artifact(corrupted["artifact_id"])["content"]["amount"] == 999
    with pytest.raises(TraceError, match="duplicate"):
        r.save_artifact(Artifact(**original))


def test_dependency_unknown_control_and_missing_refs(tmp_path):
    r, *_ = setup(tmp_path)
    a = Artifact(run_id=r.run_id, artifact_id="a", attempt_id="a0",
                 producer_execution_id="x", artifact_type="decision", content="pass")
    r.save_artifact(a)
    DependencyStore(r).record("a0", "pay", "x2", inputs=["a"], controls=["a"])
    row = r.records("events")[-1]["data"]
    assert row["state_reads"] is None and row["control_artifact_ids"] == ["a"]
    with pytest.raises(TraceError, match="unknown artifact"):
        DependencyStore(r).record("a0", "pay", "x3", inputs=["missing"])
    s = summarize_run(tmp_path)
    assert s["invalid_path_cost"] is None
    assert s["unauthorized_attempts"] is None
    assert s["observed_affected_nodes"] is None


def test_reopen_append_duplicate_and_malformed(tmp_path):
    r, *_ = setup(tmp_path)
    row = r.log("example", "a0")
    r = Recorder(tmp_path, r.manifest)
    with pytest.raises(TraceError, match="duplicate"):
        r.emit(Event(**row))
    r.log("example", "a0")
    assert r.event_cursor == 2
    path = tmp_path / "events.jsonl"
    with path.open("a") as f:
        f.write('{"schema_version":')
    s = summarize_run(tmp_path)
    assert s["trace_issues"] and not s["trace_complete"]
    with pytest.raises(TraceError):
        Recorder(tmp_path, r.manifest)
    path.write_text("broken\n" + path.read_text())
    with pytest.raises(TraceError, match="malformed"):
        summarize_run(tmp_path)


def test_incomplete_call_counts_usage_and_blocks_snapshot(tmp_path):
    r, env, inj, w, p = setup(tmp_path)
    r.log("model_started", "a0", step_id="s", data={"messages": []})
    r2 = Recorder(tmp_path, r.manifest)
    assert r2.active_calls
    with pytest.raises(TraceError):
        RecoveryPointManager(r2, env, w).capture("a0", "C1", injection_phase="no_injection")
    s = summarize_run(tmp_path)
    assert s["total_cost"]["model_calls"] == 1
    assert s["total_cost"]["incomplete_calls"] == 1
    assert s["total_cost"]["input_tokens"] is None


def test_summary_deterministic_and_no_cost_double_count(tmp_path):
    expected = run_mock(tmp_path, "restart")
    before = (tmp_path / "events.jsonl").read_bytes()
    assert summarize_run(tmp_path) == expected == summarize_run(tmp_path)
    assert (tmp_path / "events.jsonl").read_bytes() == before
    assert expected["total_cost"]["input_tokens"] == 60
    assert expected["repeated_previously_valid_nodes"] == 1


def test_reject_invalid_schema_json_and_call_lifecycle(tmp_path):
    r, *_ = setup(tmp_path)
    with pytest.raises(ValueError):
        Event(run_id="x", attempt_id="a", event_id="e", event_type="x", schema_version="future")
    with pytest.raises(ValueError):
        r.log("x", "a", data={"nan": float("nan")})
    with pytest.raises(TraceError):
        r.log("model_finished", "a", step_id="not-started")
    r.log("model_started", "a", step_id="s")
    with pytest.raises(TraceError):
        r.log("model_finished", "a", step_id="s", data={"input_tokens": -1})
    with pytest.raises(ValueError):
        Evaluation(run_id="x", attempt_id="a", evaluation_id="ev", scope="attempt",
                   evaluator="test", propagation=[{"label": "invented", "evidence_ids": []}])


def test_unknown_work_judgments_do_not_become_zero_waste(tmp_path):
    r, *_ = setup(tmp_path)
    r.log("node_started", "a0", node_id="plan", node_execution_id="e1")
    r.save_evaluation(Evaluation(run_id=r.run_id, evaluation_id="ev", attempt_id="a0",
        scope="stage", evaluator="external", work_validity={"e1": "unknown"}))
    s = summarize_run(tmp_path)
    assert s["invalid_path_cost"] is None
    assert s["known_invalid_path_cost"] is None
    assert s["repeated_previously_valid_nodes"] is None
    assert s["work_validity_coverage"]["complete"] is False
