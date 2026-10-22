"""Independent expectations for the actual recovery algorithm, using toy adapters."""
from dataclasses import replace
import json
import pytest
from experiments.models import Evaluation, new_id
from experiments.protocol import Budget, Limits
from experiments.recorder import Recorder, TraceError
from experiments.recovery import FaultNotice
from experiments.selective import Node, SelectiveRecoveryCoordinator, SelectiveRecoveryPolicy, check_reuse, order_nodes
from experiments.selective_mock import make_workflow, run_selective_mock, toy_evaluate


def prepared(path, scenario="late", nodes=None):
    rec = Recorder(path, {"run_id": "trial", "synthetic": True})
    rec.segment_id = "initial"
    rec.log("attempt_started", "a0")
    w = make_workflow(rec, scenario, nodes=nodes)
    w.points.capture("a0", "C0", injection_phase="before_injection")
    w.run("a0")
    rec.save_evaluation(toy_evaluate(w, "a0"))
    return rec, w


def recover(w, **kwargs):
    return SelectiveRecoveryCoordinator(w, lambda r: make_workflow(r), toy_evaluate,
        **kwargs).recover(FaultNotice(tuple(w.faulty_ids)), "a0")


@pytest.mark.parametrize("scenario,count,retained,reused", [("late",2,1,0),("early",3,0,0),("dag",3,0,1),("shared",4,1,0)])
def test_recovery_matches_independent_expected_work(tmp_path, scenario, count, retained, reused):
    s = run_selective_mock(tmp_path / "selective", scenario)
    f = run_selective_mock(tmp_path / "full", scenario, "full_restart")
    assert s["final_task_success"] is True and f["final_task_success"] is True
    assert s["executed_strategy"] == "selective"
    assert s["recomputed_nodes"] == count
    assert s["retained_artifacts"] == retained
    assert s["reused_artifacts"] == reused
    assert s["recovery_added_cost"]["model_calls"] <= f["recovery_added_cost"]["model_calls"]
    assert len(s["attempts"]) == 1 and len(f["attempts"]) == 2
    assert s["total_cost"]["model_calls"] >= s["recovery_added_cost"]["model_calls"]
    if scenario != "shared":
        assert s["unauthorized_executions"] == 1  # historical toy violation not erased


def test_control_edges_invalidate_downstream(tmp_path):
    nodes = [Node("a", reads=("a",)), Node("plan", controls=("a",)),
             Node("execute", inputs=("plan",), writes=("out",))]
    rec,w = prepared(tmp_path, "early", nodes)
    plan = SelectiveRecoveryPolicy().plan(FaultNotice(tuple(w.faulty_ids)), attempt_id="a0", workflow=w)
    assert plan.rerun_node_ids == ("a", "plan", "execute")
    assert not plan.reuse_artifact_ids


@pytest.mark.parametrize("cause", ["value", "lineage", "config", "writes"])
def test_reuse_requires_state_and_effect_compatibility(tmp_path, cause):
    rec,w = prepared(tmp_path, "dag")
    record = next(e for e in rec.records("events") if e["event_type"] == "node_result" and e["node_id"] == "b")
    n = w.nodes[1]
    assert check_reuse(w,n,record)[0] == "reuse"
    if cause == "value":
        w.environment.state["b"] += 1
    elif cause == "lineage":
        w.state["resources"]["b"]["version"] = "different_generation_same_value"
    elif cause == "config":
        w.config_hash = "changed"
    else:
        n = replace(n, writes=("b",))
    assert check_reuse(w,n,record)[0] == "recompute"


def test_write_after_checkpoint_is_reexecuted_not_output_replayed(tmp_path):
    rec,w = prepared(tmp_path, "shared")
    result = recover(w)
    assert result["completed"]
    assert "mutate" in result["rerun_node_ids"]
    calls = [e for e in rec.records("events") if e["event_type"] == "tool_started" and e["node_id"] == "mutate"]
    assert len(calls) == 2
    assert calls[0]["execution_segment_id"] != calls[1]["execution_segment_id"]


@pytest.mark.parametrize("mode", ["missing_dependencies", "all_checkpoints_corrupt", "restore_failure", "contaminated_context"])
def test_fallback_or_older_point(tmp_path, mode):
    rec,w = prepared(tmp_path)
    if mode == "missing_dependencies":
        for row in rec._rows["events"]:
            if row["event_type"] == "node_result":
                row["data"]["dependencies_complete"] = False
    elif mode == "all_checkpoints_corrupt":
        for p in (tmp_path / "checkpoints").glob("*/environment.json"):
            p.write_text('{}')
    elif mode == "restore_failure":
        w.environment.restore = lambda state: (_ for _ in ()).throw(RuntimeError("failed restore"))
    else:
        # A validly checksummed but polluted context must be rejected by policy.
        from experiments.models import fingerprint
        for p in (tmp_path / "checkpoints").glob("*/execution.json"):
            state = json.loads(p.read_text())
            if "a" in state["contexts"]:
                state["contexts"]["a"]["hidden_bad_message"] = "fault"
                p.write_text(json.dumps(state))
                manifest = p.with_name("checkpoint.json")
                data = json.loads(manifest.read_text())
                data["component_hashes"]["execution.json"] = fingerprint(state)
                manifest.write_text(json.dumps(data))
    result = recover(w)
    assert result["completed"]
    if mode == "contaminated_context":
        assert result["executed_strategy"] == "selective"
        assert result["rerun_node_ids"] == ["plan","execute"]
        assert len(result["reused_artifact_ids"]) == 1
        assert "hidden_bad_message" not in w.state["contexts"]["a"]
    else:
        assert result["executed_strategy"] == "full_restart"
        assert result["fallback_reason"]
        assert result["attempt_id"] != "a0"


def test_no_fallback_when_disabled(tmp_path):
    rec,w = prepared(tmp_path)
    for p in (tmp_path / "checkpoints").glob("*/environment.json"):
        p.write_text('{}')
    result = recover(w, fallback_policy="error")
    assert not result["completed"] and result["executed_strategy"] is None


def test_budget_shared_with_recovery(tmp_path):
    rec,w = prepared(tmp_path)
    rec.budget = Budget(Limits(model_calls=1))
    rec.budget.calls["model"] = 1
    result = recover(w)
    assert not result["completed"]
    assert "BudgetExceeded" in result["error"]
    assert not any(e["event_type"] == "recovery_fallback" for e in rec.records("events"))


def test_nonrestorable_effect_reported_not_reset(tmp_path):
    nodes=[Node("a",reads=("a",)), Node("plan",inputs=("a",)),
           Node("execute",inputs=("plan",),writes=("out",),effects_restorable=False)]
    rec,w = prepared(tmp_path,nodes=nodes)
    before=w.environment.snapshot()
    result=recover(w)
    assert not result["completed"] and result["unresolved_effects"]
    assert w.environment.snapshot() == before


def test_history_and_original_artifacts_immutable(tmp_path):
    rec,w = prepared(tmp_path)
    old_artifacts=rec.records("artifacts")
    old_events=rec.records("events")
    result=recover(w)
    assert result["completed"]
    assert rec.records("artifacts")[:len(old_artifacts)] == old_artifacts
    assert rec.records("events")[:len(old_events)] == old_events
    assert w.state["artifact_map"]["plan"] not in w.faulty_ids


def test_unsupported_capability_and_cycle():
    with pytest.raises(TypeError,match="optional fixed-DAG"):
        SelectiveRecoveryCoordinator(object(),None,None)
    with pytest.raises(ValueError,match="cycle"):
        order_nodes([Node("a",inputs=("b",)),Node("b",inputs=("a",))])


def test_recovery_limit_and_action_grouping(tmp_path):
    from experiments.evidence import group_actions
    rec,w=prepared(tmp_path)
    coordinator=SelectiveRecoveryCoordinator(w,lambda r:make_workflow(r),toy_evaluate)
    notice=FaultNotice(tuple(w.faulty_ids))
    assert coordinator.recover(notice,'a0')['completed']
    groups=group_actions(rec.records('events'))
    assert len(groups)==2
    assert {g['attempt_id'] for g in groups}=={'a0'}
    assert len({g['execution_segment_id'] for g in groups})==2
    with pytest.raises(TraceError,match='limit'):
        coordinator.recover(notice,'a0')


def test_partial_failed_node_effects_fall_back_without_erasing_evidence(tmp_path):
    rec=Recorder(tmp_path,{'run_id':'partial','synthetic':True})
    rec.segment_id='initial'
    rec.log('attempt_started','a0')
    w=make_workflow(rec)
    original_execute=w.execute
    def fail_after_write(node,inputs,env,context,cb,execution):
        result=original_execute(node,inputs,env,context,cb,execution)
        if node.name=='execute':
            raise RuntimeError('after write')
        return result
    w.execute=fail_after_write
    w.points.capture('a0','C0',injection_phase='before_injection')
    with pytest.raises(RuntimeError):
        w.run('a0')
    result=recover(w)
    assert result['completed'] and result['executed_strategy']=='full_restart'
    failed=[e for e in rec.records('events') if e['event_type']=='node_failed_effects']
    assert failed[0]['data']['state_writes'] is True


def test_baseline_requires_known_fault_and_matching_factory(tmp_path):
    rec,w=prepared(tmp_path)
    c=SelectiveRecoveryCoordinator(w,lambda r:make_workflow(r),toy_evaluate)
    out=c.recover(FaultNotice(('missing_artifact',)),'a0',requested_strategy='full_restart')
    assert not out['completed'] and out['executed_strategy'] is None


def test_fallback_rejects_changed_workflow_configuration(tmp_path):
    rec,w=prepared(tmp_path)
    def factory(r):
        fresh=make_workflow(r)
        fresh.config_hash='different_prompt'
        return fresh
    c=SelectiveRecoveryCoordinator(w,factory,toy_evaluate)
    out=c.recover(FaultNotice(tuple(w.faulty_ids)),'a0',requested_strategy='full_restart')
    assert not out['completed']
    assert 'matching configuration' in out['error']


@pytest.mark.parametrize("strategy", ["selective", "full_restart"])
def test_new_nonrestorable_effects_survive_recovery_failure(tmp_path, strategy):
    rec = Recorder(tmp_path, {"run_id": "new-effects", "synthetic": True})
    nodes = [Node("a", reads=("a",)), Node("plan", inputs=("a",)),
             Node("execute", inputs=("plan",), writes=("out",), effects_restorable=False)]
    factories = []
    def factory(recorder):
        w = make_workflow(recorder, nodes=nodes)
        original = w.execute
        def execute(node, inputs, env, context, cb, execution):
            if node.name == "execute" and inputs["plan"] == 999:
                return 999  # initial faulty branch has no state effect
            value = original(node, inputs, env, context, cb, execution)
            if node.name == "execute":
                raise RuntimeError("write completed before failure")
            return value
        w.execute = execute
        factories.append(w)
        return w
    w = factory(rec)
    w.points.capture("a0", "C0", injection_phase="before_injection")
    w.run("a0")
    c = SelectiveRecoveryCoordinator(w, factory, toy_evaluate)
    result = c.recover(FaultNotice(tuple(w.faulty_ids)), "a0", requested_strategy=strategy)
    assert not result["completed"]
    assert len(factories) == (1 if strategy == "selective" else 2)
    failed = [e for e in rec.records("events") if e["event_type"] == "node_failed_effects"]
    assert result["unresolved_effects"] == [failed[0]["event_id"]]
    assert c.workflow.environment.state["out"] == [10]
    if strategy == "selective":
        assert not any(e["event_type"] == "recovery_fallback" for e in rec.records("events"))


@pytest.mark.parametrize("failure", ["undeclared_write", "inventory", "artifact", "snapshot"])
def test_post_execution_failure_preserves_effect_evidence(tmp_path, failure):
    rec = Recorder(tmp_path, {"run_id": "post-processing", "synthetic": True})
    nodes = [Node("a", reads=("a",)), Node("plan", inputs=("a",)),
             Node("execute", inputs=("plan",),
                  writes=() if failure == "undeclared_write" else ("out",),
                  effects_restorable=False)]
    w = make_workflow(rec, nodes=nodes)
    original = w.execute
    def execute(node, inputs, env, context, cb, execution):
        value = original(node, inputs, env, context, cb, execution)
        if node.name == "execute":
            if failure == "inventory":
                env.state["unexpected"] = True
            elif failure == "artifact":
                return object()  # non-JSON result after a successful tool write
            elif failure == "snapshot":
                def unavailable():
                    raise RuntimeError("snapshot unavailable")
                env.snapshot = unavailable
        return value
    w.execute = execute
    w.points.capture("a0", "C0", injection_phase="before_injection")
    with pytest.raises((TraceError, TypeError, ValueError, RuntimeError)):
        w.run("a0")
    assert w.environment.state["out"] == [999]
    failed = [e for e in rec.records("events") if e["event_type"] == "node_failed_effects"]
    assert len(failed) == 1
    assert failed[0]["data"]["state_writes"] is (None if failure == "snapshot" else True)
    result = recover(w)
    assert not result["completed"]
    assert result["unresolved_effects"] == [failed[0]["event_id"]]


# Optional Phase 2 recovery escalation; Phase 1 factory interfaces stay untouched.
def transient_executor(w, *, failures=1, error_type=None):
    from experiments.selective import RetryableNodeError
    original = w.execute
    calls = []
    def execute(node, inputs, env, context, cb, execution):
        value = original(node, inputs, env, context, cb, execution)
        if node.name == "execute":
            calls.append(cb.recorder.segment_id)
            if len(calls) <= failures:
                context["failed_branch_only"] = True
                raise (error_type or RetryableNodeError)("temporary failure after reversible write")
        return value
    w.execute = execute
    return calls


def test_phase2_transient_retry_restores_same_point_and_isolates_context(tmp_path):
    from experiments.metrics import summarize_run
    rec, w = prepared(tmp_path)
    initial_events = rec.records("events")
    calls = transient_executor(w)
    result = recover(w, checkpoint_retries=1, max_checkpoint_attempts=2)
    assert result["completed"] and result["executed_strategy"] == "selective"
    assert result["checkpoint_attempts"] == 2 and result["checkpoint_retry_count"] == 1
    starts = [e for e in rec.records("events") if e["event_type"] == "checkpoint_attempt_started"]
    assert len({e["data"]["checkpoint_id"] for e in starts}) == 1
    assert len(set(calls)) == 2
    assert w.environment.state["out"] == [10]  # failed branch's write was rolled back
    assert "failed_branch_only" not in w.state["contexts"]["execute"]
    assert rec.records("events")[:len(initial_events)] == initial_events
    assert result["attempt_id"] == "a0"
    summary = summarize_run(tmp_path)
    assert summary["recomputed_node_executions"] == 4
    assert summary["recovery_added_cost"]["model_calls"] == 2
    assert summary["recovery_added_cost"]["tool_calls"] == 2
    assert summary["checkpoint_retry_cost"]["model_calls"] == 1
    assert summary["checkpoint_retry_cost"]["tool_calls"] == 1
    assert summary["checkpoint_attempts"] == 2


@pytest.mark.parametrize("mode", ["corrupt_after_selection", "partial_restore"])
def test_phase2_restore_failure_uses_older_prevalidated_point(tmp_path, mode):
    rec, w = prepared(tmp_path)
    latest = SelectiveRecoveryPolicy().plan(FaultNotice(tuple(w.faulty_ids)),
                                          attempt_id="a0", workflow=w).checkpoint_id
    original = w.points.restore
    seen = []
    def restore(checkpoint, attempt):
        seen.append(checkpoint)
        if checkpoint == latest:
            if mode == "corrupt_after_selection":
                (tmp_path / "checkpoints" / checkpoint / "environment.json").write_text("{}")
            else:
                w.environment.state["out"] = [123]
                raise TraceError("application restore interrupted")
        return original(checkpoint, attempt)
    w.points.restore = restore
    result = recover(w, max_checkpoint_attempts=2)
    assert result["completed"] and result["executed_strategy"] == "selective"
    assert len(seen) == 2 and seen[0] != seen[1]
    assert w.points.load(result["checkpoint_id"])[0].boundary == "C0"
    assert w.environment.state["out"] == [10]
    assert any(e["event_type"] == "checkpoint_older_selected" for e in rec.records("events"))


@pytest.mark.parametrize("fallback", ["full_restart", "error"])
def test_phase2_exhausted_retries_are_bounded_before_fallback(tmp_path, fallback):
    rec, w = prepared(tmp_path)
    calls = transient_executor(w, failures=100)
    result = recover(w, checkpoint_retries=1, max_checkpoint_attempts=2, fallback_policy=fallback)
    assert len(calls) == 4
    assert result["checkpoint_attempts"] == 4
    assert result["checkpoint_retry_count"] == 2
    assert result["completed"] is (fallback == "full_restart")
    if fallback == "full_restart":
        assert result["executed_strategy"] == "full_restart"
        assert result["attempt_id"] != "a0"
    assert len([e for e in rec.records("events") if e["event_type"] == "node_failed_effects"]) == 4


def test_phase2_structural_error_is_not_retried_as_transient(tmp_path):
    rec, w = prepared(tmp_path)
    calls = transient_executor(w, error_type=ValueError)
    result = recover(w, checkpoint_retries=3, max_checkpoint_attempts=3)
    assert result["completed"] and result["executed_strategy"] == "full_restart"
    assert len(calls) == 1 and result["checkpoint_retry_count"] == 0


def test_phase2_retry_cannot_refresh_budget(tmp_path):
    rec, w = prepared(tmp_path)
    rec.budget = Budget(Limits(model_calls=1))
    calls = transient_executor(w)
    result = recover(w, checkpoint_retries=3, max_checkpoint_attempts=3)
    assert not result["completed"] and "BudgetExceeded" in result["error"]
    assert len(calls) == 1 and rec.budget.calls["model"] == 1
    assert not any(e["event_type"] == "recovery_fallback" for e in rec.records("events"))


@pytest.mark.parametrize("unknown", [False, True])
def test_phase2_unsafe_effect_prevents_checkpoint_retry(tmp_path, unknown):
    from experiments.selective import RetryableNodeError
    rec, w = prepared(tmp_path)
    original = w.execute
    def execute(node, inputs, env, context, cb, execution):
        value = original(node, inputs, env, context, cb, execution)
        if node.name == "execute":
            if unknown:
                def unavailable():
                    raise RuntimeError("cannot determine state")
                env.snapshot = unavailable
            else:
                # Simulate adapter evidence for an external effect, independent of A scoring.
                rec.log("node_failed_effects", "a0", node_id=node.name,
                        data={"state_writes": True, "effects_restorable": False})
            raise RetryableNodeError("terminated call with effects")
        return value
    w.execute = execute
    result = recover(w, checkpoint_retries=2, max_checkpoint_attempts=3)
    assert not result["completed"] and result["unresolved_effects"]
    assert result["checkpoint_attempts"] == 1
    assert result["checkpoint_retry_count"] == 0
    assert not any(e["event_type"] == "recovery_fallback" for e in rec.records("events"))


def test_phase2_retries_can_be_disabled(tmp_path):
    rec, w = prepared(tmp_path)
    calls = transient_executor(w)
    result = recover(w, checkpoint_retries=0, max_checkpoint_attempts=1)
    assert result["completed"] and result["executed_strategy"] == "full_restart"
    assert len(calls) == 1 and result["checkpoint_attempts"] == 1


@pytest.mark.parametrize("options", [{"checkpoint_retries": -1}, {"checkpoint_retries": True},
                                    {"max_checkpoint_attempts": 0}, {"max_checkpoint_attempts": 1.5}])
def test_phase2_retry_limits_validated(tmp_path, options):
    rec, w = prepared(tmp_path)
    with pytest.raises(ValueError):
        recover(w, **options)


def test_phase2_defaults_enable_one_retry_and_three_points(tmp_path):
    rec, w = prepared(tmp_path)
    calls = transient_executor(w)
    coordinator = SelectiveRecoveryCoordinator(w, lambda r: make_workflow(r), toy_evaluate)
    assert coordinator.checkpoint_retries == 1
    assert coordinator.max_checkpoint_attempts == 3
    result = coordinator.recover(FaultNotice(tuple(w.faulty_ids)), "a0")
    assert result["completed"] and result["executed_strategy"] == "selective"
    assert len(calls) == 2 and result["checkpoint_retry_count"] == 1


@pytest.mark.parametrize("failure", ["restore_failure", "incompatible"])
def test_failed_checkpoint_recovery_has_no_new_calls_or_false_success(tmp_path, failure):
    from experiments.metrics import summarize_run
    rec, w = prepared(tmp_path)
    if failure == "restore_failure":
        def broken_restore(state):
            raise RuntimeError("restore unavailable")
        w.environment.restore = broken_restore
    else:
        for path in (tmp_path / "checkpoints").glob("*/checkpoint.json"):
            manifest = json.loads(path.read_text())
            manifest["manifest_fingerprint"] = "incompatible"
            path.write_text(json.dumps(manifest))
    result = recover(w, fallback_policy="error")
    summary = summarize_run(tmp_path)
    assert not result["completed"] and result["task_success"] is None
    assert summary["final_task_success"] is None
    assert summary["recovery_added_cost"]["model_calls"] == 0
    assert summary["recovery_added_cost"]["tool_calls"] == 0


def test_full_restart_completion_does_not_imply_correctness(tmp_path):
    rec, w = prepared(tmp_path)
    def factory(recorder):
        fresh = make_workflow(recorder)
        original_run = fresh.run
        def wrong_run(attempt):
            original_run(attempt)
            fresh.environment.state["out"] = []
        fresh.run = wrong_run
        return fresh
    coordinator = SelectiveRecoveryCoordinator(w, factory, toy_evaluate)
    notice = FaultNotice(tuple(w.faulty_ids))
    result = coordinator.recover(notice, "a0", requested_strategy="full_restart")
    assert result["completed"] is True and result["task_success"] is False
    with pytest.raises(TraceError, match="limit"):
        coordinator.recover(notice, "a0")
