"""Phase 2 recovery on B's real stages and A's simulator/scorer, with a scripted model."""
import csv
import json

import pytest

from experiments.banking_selective import run_banking_phase2
from experiments.workflow_bridge import execute_workflow, workflow_batch
from faults.injection_harness import make_injector
from tests.test_workflow import FakeModel
from workflow.client import ModelRequestError
from workflow.contracts import InjectionSpec

LATE = {"user_task_0": "t0_pe_accidental_amount", "user_task_2": "t2_pe_accidental_new_rent"}
EARLY = {"user_task_0": "t0_rp_accidental_amount", "user_task_2": "t2_rp_accidental_rent_increase"}


def factory(model):
    client = FakeModel()
    client.config = model
    return client


def config(condition, task="user_task_0", scenario=None, **extra):
    c = dict(task_id=task, condition=condition, scenario_id="clean", comparison_group_id=task, repeat_index=0,
             synthetic=True, workflow=dict(model=dict(model="fixture", base_url="http://127.0.0.1:11434/v1"),
                                           runtime="scripted-test-client", session={}),
             limits=dict(model_calls=40, tool_calls=30, wall_seconds=300, tokens=None))
    if scenario:
        c.update(scenario_id=scenario, fault=dict(
            family="adversarial" if "adversarial" in scenario else "accidental",
            site="research_to_plan" if "_rp_" in scenario else "plan_to_execute",
            payload_ref="faults/payloads.yaml", fixture=False,
            injector_factory="faults.injection_harness:make_injector"))
    c.update(extra)
    return c


def events(path, kind=None):
    rows = [json.loads(x) for x in (path / "events.jsonl").read_text().splitlines()]
    return [e for e in rows if kind is None or e["event_type"] == kind]


def history(path):
    rows = [json.loads(x) for x in (path / "evaluations.jsonl").read_text().splitlines()]
    return [e for e in rows if e["scope"] == "history"][-1]


def direct(tmp_path, scenario, *, task="user_task_0", condition="selective", client=None, injector=None, **kwargs):
    site = "research" if "_rp_" in scenario else "plan"
    return run_banking_phase2(tmp_path, task_id=task, condition=condition, client=client or FakeModel(),
        injector=injector or make_injector(task_id=task, scenario_id=scenario),
        injection_spec=InjectionSpec(scenario, site), synthetic=True, **kwargs)


@pytest.mark.parametrize("task", ["user_task_0", "user_task_2"])
def test_faulty_plan_keeps_research_and_reruns_only_affected_stages(tmp_path, task):
    s = execute_workflow(config("selective", task, LATE[task]), tmp_path, client_factory=factory)
    assert s["initial_strict_success"] is False and s["final_strict_success"] is True
    assert s["executed_strategy"] == "selective" and s["fallback_reason"] is None
    assert s["recomputed_nodes"] == 2 and s["effective_retained_artifacts"] == 1
    assert [e["node_id"] for e in events(tmp_path, "node_started")] == [
        "research", "plan", "execute", "plan", "execute"]
    # The chosen point is the one after research; later points hold the faulty plan.
    created = {e["data"]["checkpoint_id"]: e["data"]["boundary"] for e in events(tmp_path, "checkpoint_created")}
    assert created[s["checkpoint_id"]] == "C1"
    # One-shot injection: the rerun planner output is delivered unmodified.
    assert sum(e["data"]["triggered"] for e in events(tmp_path, "fault_injected")) == 1
    assert events(tmp_path, "fault_disabled")
    assert s["trace_complete"] and not s["execution_errors"]
    # The dropped wrong write: money sent cannot be taken back, a rent update can be compensated.
    dropped = (s["unresolved_effects"], s["compensated_effects"])
    assert [len(d) for d in dropped] == ([1, 0] if task == "user_task_0" else [0, 1])


@pytest.mark.parametrize("task", ["user_task_0", "user_task_2"])
def test_restore_does_not_erase_earlier_harm_or_cost(tmp_path, task):
    s = execute_workflow(config("selective", task, LATE[task]), tmp_path, client_factory=factory)
    h = history(tmp_path)
    initial, final = h["details"]["paths"]
    assert (initial["label"], final["label"]) == ("initial", "recovery_effective")
    wrong = initial["unauthorized_execution_ids"]
    assert len(wrong) == 1 and h["unauthorized_execution_ids"] == wrong == [
        c for c in initial["call_ids"] if c not in final["call_ids"] and c in wrong]
    assert final["unauthorized_execution_ids"] == [] and final["strict_success"] is True
    # Research calls stay on the effective path; the abandoned write does not.
    assert set(initial["call_ids"]) & set(final["call_ids"]) and wrong[0] not in final["call_ids"]
    assert h["evaluator"] == "A.evaluate_recovery" and h["strict_success"] is False
    assert wrong == (h["details"]["unresolved_effects"] or h["details"]["compensated_effects"])
    # A's attack judgment sees the whole audit, although the final state no longer shows it.
    assert s["attack_success"] is True and s["attack"]["status"] == "evaluated"
    assert s["attack"]["attempts"][0]["in_final_state"] is False
    assert s["unauthorized_executions"] == 1
    assert s["total_cost"]["model_calls"] == sum(1 for _ in events(tmp_path, "model_started"))
    assert 0 < s["recovery_added_cost"]["model_calls"] < s["total_cost"]["model_calls"]


def test_faulty_research_reruns_every_stage_from_the_initial_point(tmp_path):
    s = execute_workflow(config("selective", "user_task_0", EARLY["user_task_0"]), tmp_path, client_factory=factory)
    assert s["final_strict_success"] is True and s["executed_strategy"] == "selective"
    assert s["recomputed_nodes"] == 3 and s["effective_retained_artifacts"] == 0
    created = {e["data"]["checkpoint_id"]: e["data"]["boundary"] for e in events(tmp_path, "checkpoint_created")}
    assert created[s["checkpoint_id"]] == "C0"


def test_full_restart_baseline_uses_fresh_environment_and_costs_more(tmp_path):
    scenario = LATE["user_task_0"]
    selective = execute_workflow(config("selective", scenario=scenario), tmp_path / "s", client_factory=factory)
    restart = execute_workflow(config("full_restart", scenario=scenario, recovery_policy={}),
                               tmp_path / "f", client_factory=factory)
    assert restart["executed_strategy"] == "full_restart" and restart["final_strict_success"] is True
    assert restart["recomputed_nodes"] == 3 and len(restart["attempts"]) == 2
    assert restart["attack_success"] is True and len(restart["attack"]["attempts"]) == 2
    assert restart["attack"]["attempts"][0]["in_final_state"] is True  # the old environment keeps it
    assert selective["recovery_added_cost"]["model_calls"] < restart["recovery_added_cost"]["model_calls"]
    assert selective["initial_state_hash"] == restart["initial_state_hash"]


def test_repeated_valid_action_is_reported_with_restore_status(tmp_path):
    scenario = "t0_pe_adversarial_extra_payment"
    for condition, restored in (("selective", True), ("full_restart", False)):
        s = execute_workflow(config(condition, scenario=scenario, recovery_policy={}),
                             tmp_path / condition, client_factory=factory)
        repeats = s["cross_attempt_repeats"]
        assert repeats["status"] == "evaluated" and repeats["prior_effects_restored"] is restored
        assert [a["tool"] for a in repeats["actions"]] == ["send_money"]
        assert repeats["actions"][0]["path"] == "recovery_effective"


def test_damaged_checkpoint_is_rejected_and_older_point_reruns_research(tmp_path):
    scenario = LATE["user_task_0"]
    inner = make_injector(task_id="user_task_0", scenario_id=scenario)

    def injector(content, site):
        # Called at the plan boundary, after C1 was saved: damage that saved state.
        for path in (tmp_path / "checkpoints").glob("*/checkpoint.json"):
            if json.loads(path.read_text())["boundary"] == "C1":
                target = path.parent / "environment.json"
                target.write_text(target.read_text().replace("1810", "9999"))
        return inner(content, site)

    s = direct(tmp_path, scenario, injector=injector)
    created = {e["data"]["checkpoint_id"]: e["data"]["boundary"] for e in events(tmp_path, "checkpoint_created")}
    assert created[s["checkpoint_id"]] == "C0" and s["final_strict_success"] is True
    # Research is valid but made audited calls, so its saved result is recomputed, not replayed.
    checked = events(tmp_path, "reuse_checked")
    assert [(e["node_id"], e["data"]["decision"]) for e in checked] == [("research", "recompute")]
    assert s["recomputed_nodes"] == 3 and s["reused_artifacts"] == 0


def test_no_fallback_policy_stops_when_no_point_is_usable(tmp_path):
    scenario = LATE["user_task_0"]
    inner = make_injector(task_id="user_task_0", scenario_id=scenario)

    def injector(content, site):
        for path in (tmp_path / "checkpoints").glob("*/environment.json"):
            path.write_text(path.read_text().replace("1810", "9999"))
        return inner(content, site)

    s = direct(tmp_path, scenario, injector=injector, recovery_policy={"fallback_policy": "error"})
    assert s["recovery_completed"] is False and s["final_strict_success"] is None
    assert s["unauthorized_executions"] == 1 and s["recovery_added_cost"]["model_calls"] == 0


class FlakyPlanner(FakeModel):
    """Fail the second planner request (the recovery rerun) once."""
    def __init__(self):
        super().__init__()
        self.planner_requests = 0

    def complete(self, messages, tools, *, json_output=False):
        if "You are the Planner." in messages[0]["content"]:
            self.planner_requests += 1
            if self.planner_requests == 2:
                raise ModelRequestError("fixture transport error", retryable=True)
        return super().complete(messages, tools, json_output=json_output)


def test_transient_provider_failure_retries_the_same_checkpoint(tmp_path):
    s = direct(tmp_path, LATE["user_task_0"], client=FlakyPlanner())
    assert s["final_strict_success"] is True and s["executed_strategy"] == "selective"
    assert s["checkpoint_attempts"] == 2 and s["checkpoint_retry_count"] == 1
    assert s["checkpoint_retry_cost"]["model_calls"] == 3
    # Every recovery call belongs to a segment the coordinator announced.
    announced = {e["execution_segment_id"] for e in events(tmp_path, "segment_started")}
    assert {e["execution_segment_id"] for e in events(tmp_path, "model_started")} <= announced


def test_recovery_shares_the_trial_budget(tmp_path):
    s = execute_workflow(config("selective", scenario=LATE["user_task_0"],
                                limits=dict(model_calls=6, tool_calls=30, wall_seconds=300, tokens=None)),
                         tmp_path, client_factory=factory)
    assert s["budget_exhausted"] and s["total_cost"]["model_calls"] == 6
    assert s["recovery_completed"] is False and s["final_strict_success"] is None
    assert s["unauthorized_executions"] == 1 and s["trace_complete"]


def test_untriggered_fault_starts_no_recovery(tmp_path):
    s = direct(tmp_path, LATE["user_task_0"],
               injector=lambda content, site: {"applied": False, "reason": "target_missing"})
    assert s["injection_triggered"] is False and not s["recovery_attempted"]
    assert s["final_strict_success"] is True and len(s["attempts"]) == 1


def test_batch_compares_phase1_and_phase2_methods_with_one_clean_control(tmp_path):
    scenario = LATE["user_task_0"]
    configs = [config("clean"), config("fault", scenario=scenario), config("full_restart", scenario=scenario),
               config("full_restart", scenario=scenario, recovery_policy={}, method_id="full_restart_dag"),
               config("selective", scenario=scenario, method_id="selective")]
    rows = workflow_batch(configs, tmp_path / "batch", client_factory=factory)
    assert all(r["error"] is None for r in rows)
    manifests = [json.loads((tmp_path / "batch" / f"run_{i:04d}" / "manifest.json").read_text()) for i in range(5)]
    assert ["runner" in m for m in manifests] == [False, False, False, True, True]  # Phase 1 path unchanged
    table = list(csv.DictReader((tmp_path / "batch" / "runs.csv").open()))
    assert all(r["clean_pair_status"] == "matched" for r in table)
    assert [r["executed_strategy"] for r in table[2:]] == ["full_restart", "full_restart", "selective"]
    assert [r["recovery_policy_match"] for r in table[3:]] == ["True", "True"]
    assert int(table[4]["recovery_added_cost_model_calls"]) < int(table[3]["recovery_added_cost_model_calls"])


def test_recovery_policy_is_rejected_for_other_conditions(tmp_path):
    with pytest.raises(ValueError, match="recovery_policy"):
        execute_workflow(config("fault", scenario=LATE["user_task_0"], recovery_policy={}),
                         tmp_path, client_factory=factory)


@pytest.mark.parametrize("task", ["user_task_0", "user_task_2"])
def test_early_notice_reruns_planner_keeps_research_and_prevents_the_wrong_action(tmp_path, task):
    early = dict(notification_boundary="after_faulty_handoff")
    s = execute_workflow(config("selective", task, LATE[task], **early), tmp_path, client_factory=factory)
    # The pass halts at the faulty plan handoff: the executor never sees it.
    assert [e["node_id"] for e in events(tmp_path, "node_started")] == ["research", "plan", "plan", "execute"]
    assert not events(tmp_path, "fault_exposure")
    assert events(tmp_path, "attempt_finished")[0]["data"]["stopped_for_notice"] is True
    assert events(tmp_path, "recovery_started")[0]["data"]["notification_boundary"] == "after_faulty_handoff"
    assert s["executed_strategy"] == "selective" and s["final_strict_success"] is True
    # Only the planner is redone; the executor runs for the first time during recovery.
    assert s["effective_retained_artifacts"] == 1 and s["recomputed_nodes"] == 1
    h = history(tmp_path)
    assert h["unauthorized_execution_ids"] == [] and s["unauthorized_executions"] == 0
    assert s["trace_complete"] and not s["execution_errors"]


def test_early_notice_full_restart_also_prevents_harm_but_reruns_research(tmp_path):
    early = dict(notification_boundary="after_faulty_handoff", recovery_policy={})
    scenario = LATE["user_task_2"]
    selective = execute_workflow(config("selective", "user_task_2", scenario, **early),
                                 tmp_path / "s", client_factory=factory)
    restart = execute_workflow(config("full_restart", "user_task_2", scenario, **early),
                               tmp_path / "f", client_factory=factory)
    assert [e["node_id"] for e in events(tmp_path / "f", "node_started")] == [
        "research", "plan", "research", "plan", "execute"]
    for s in (selective, restart):
        assert s["final_strict_success"] is True and s["unauthorized_executions"] == 0
    assert selective["recovery_added_cost"]["model_calls"] < restart["recovery_added_cost"]["model_calls"]


def test_early_notice_requires_the_recovery_engine(tmp_path):
    for c in (config("fault", scenario=LATE["user_task_0"], notification_boundary="after_faulty_handoff"),
              config("full_restart", scenario=LATE["user_task_0"], notification_boundary="after_faulty_handoff"),
              config("selective", scenario=LATE["user_task_0"], notification_boundary="sometime")):
        with pytest.raises(ValueError):
            execute_workflow(c, tmp_path, client_factory=factory)


def test_phase2_plan_has_late_and_early_recovery_methods():
    from scripts.run_phase2_banking import plan
    configs = plan({"model": "m", "base_url": "http://127.0.0.1:1/v1"}, "rt", 1, True)
    methods = {c.get("method_id") for c in configs if c["condition"] in ("selective", "full_restart")}
    assert methods == {"selective_dag", "full_restart_dag", "selective_dag_early", "full_restart_dag_early",
                       "selective_dag_bank", "full_restart_dag_bank"}
    assert all(c.get("notification_boundary") == "after_faulty_handoff"
               for c in configs if str(c.get("method_id", "")).endswith("_early"))
    bank = [c for c in configs if str(c.get("method_id", "")).endswith("_bank")]
    assert bank and all(c["restore_mode"] == "real_bank" and "notification_boundary" not in c for c in bank)


def _transactions(path, attempt):
    finals = [e for e in events(path, "environment_finalized") if e["attempt_id"] == attempt]
    return finals[-1]["data"]["final_state"]["transactions"]


@pytest.mark.parametrize("condition", ["selective", "full_restart"])
def test_real_bank_restore_keeps_money_already_sent(tmp_path, condition):
    scenario = LATE["user_task_0"]
    runs = {mode: execute_workflow(config(condition, scenario=scenario, recovery_policy={}, restore_mode=mode),
                                   tmp_path / mode, client_factory=factory)
            for mode in ("simulated", "real_bank")}
    sim, bank = runs["simulated"], runs["real_bank"]
    assert sim["final_strict_success"] is True and sim["attack"]["harm_in_final_state"] is False
    # The wrong payment cannot be taken back: it stays next to the corrected one.
    assert bank["executed_strategy"] == condition and bank["recovery_completed"] is True
    assert bank["final_strict_success"] is False and bank["attack"]["harm_in_final_state"] is True
    final_attempt = bank["attempts"][-1]["attempt_id"]
    assert len(_transactions(tmp_path / "real_bank", final_attempt)) == \
        len(_transactions(tmp_path / "simulated", sim["attempts"][-1]["attempt_id"])) + 1
    assert len(bank["unresolved_effects"]) == 1 and not bank["execution_errors"]
    assert json.loads((tmp_path / "real_bank" / "manifest.json").read_text())["restore_mode"] == "real_bank"


def test_real_bank_restore_still_undoes_compensable_updates(tmp_path):
    scenario = LATE["user_task_2"]
    s = execute_workflow(config("selective", "user_task_2", scenario, restore_mode="real_bank"),
                         tmp_path, client_factory=factory)
    assert s["final_strict_success"] is True and len(s["compensated_effects"]) == 1
    assert not s["unresolved_effects"]


def test_restore_mode_requires_the_recovery_engine(tmp_path):
    for c in (config("fault", scenario=LATE["user_task_0"], restore_mode="real_bank"),
              config("selective", scenario=LATE["user_task_0"], restore_mode="somewhere")):
        with pytest.raises(ValueError):
            execute_workflow(c, tmp_path, client_factory=factory)


def test_checkpoint_preserves_stage_evidence_and_actual_output_lineage(tmp_path):
    from experiments.recorder import Recorder
    from experiments.selective import InvalidationGraph
    from workflow import StageState

    s = direct(tmp_path, LATE["user_task_0"])
    rows = events(tmp_path, "node_result")
    research, plan, execute, rerun_plan, rerun_execute = [e["data"] for e in rows]
    evidence = research["context"]["resume_state"]["evidence_ids"]
    assert evidence
    for row in (plan, execute, rerun_plan, rerun_execute):
        assert StageState.from_dict(row["context"]["resume_state"]).evidence_ids == tuple(evidence)
    assert rerun_plan["context"]["resume_state"]["model_calls"] == 3
    assert rerun_execute["context"]["resume_state"]["model_calls"] == 5
    assert s["total_cost"]["model_calls"] == 8
    assert s["budget"]["calls"]["model"] == 8  # restore never refills the trial budget
    assert plan["context"]["produced_artifact_id"] != plan["context"]["delivered_artifact_id"]
    assert rerun_plan["context"]["produced_artifact_id"] == rerun_plan["context"]["delivered_artifact_id"]
    assert rerun_plan["context"]["input_artifact_ids"][1] == research["artifact_id"]
    checkpoint = json.loads((tmp_path / "checkpoints" / s["checkpoint_id"] / "execution.json").read_text())
    assert checkpoint["contexts"]["research"]["resume_state"] == research["context"]["resume_state"]

    rec = Recorder(tmp_path, json.loads((tmp_path / "manifest.json").read_text()))
    source = research["context"]["stage_artifact_id"]
    assert source in rec.artifact(research["artifact_id"])["input_artifact_ids"]
    # Invalidating the inner B result reaches D's wrapper and downstream nodes.
    invalid = InvalidationGraph(rec, rows[0]["attempt_id"]).invalidate([source])
    assert all(row["artifact_id"] in invalid for row in (research, plan, execute))
    assert rec.artifact(source)["producer_execution_id"] == rows[0]["node_execution_id"]
    assert len(research["context"]["messages"]) == len(rerun_plan["context"]["messages"]) == 1


def test_provider_failure_after_recovery_write_restores_bank_but_keeps_cost(tmp_path):
    class FailAfterRecoveryWrite(FakeModel):
        failed = False

        def complete(self, messages, tools, *, json_output=False):
            body = json.loads(messages[1]["content"])
            if ("You are the Executor." in messages[0]["content"]
                    and messages[-1]["role"] == "tool"
                    and body["plan"]["actions"][0]["args"]["amount"] == 98.7 and not self.failed):
                self.failed = True
                raise ModelRequestError("temporary provider error after successful tool", retryable=True)
            return super().complete(messages, tools, json_output=json_output)

    s = direct(tmp_path, LATE["user_task_0"], client=FailAfterRecoveryWrite())
    assert s["recovery_completed"] and s["final_strict_success"]
    assert s["checkpoint_retry_count"] == 1 and s["checkpoint_attempts"] == 2
    assert s["total_cost"]["model_calls"] == s["budget"]["calls"]["model"] == 11
    assert len(events(tmp_path, "model_started")) == 11
    assert len(events(tmp_path, "tool_started")) == 4  # one read and all three writes
    final = events(tmp_path, "node_result")[-1]["data"]["context"]
    assert final["resume_state"]["model_calls"] == 5  # only the restored effective path
    assert len(final["messages"]) == 1
    assert len(events(tmp_path, "node_failed_effects")) == 1
    h = history(tmp_path)
    wrong = h["details"]["paths"][0]["unauthorized_execution_ids"]
    assert wrong and set(wrong) <= set(h["unauthorized_execution_ids"])
