"""Integration with A's real Banking adapter/scorer, without an LLM."""

from env.banking_env import BankingEnv
from experiments.banking import BankingAdapter, BankingEvaluator
from experiments.models import Artifact
from experiments.recorder import Recorder
from experiments.recovery import FaultNotice, FullRestartPolicy, RecoveryCoordinator, RecoveryPointManager
from experiments.mock import MockInjector, MockWorkflow


def test_banking_snapshot_audit_mapping_and_restart(tmp_path):
    rec = Recorder(tmp_path, {"run_id": "bank_trial", "task_id": "user_task_0", "synthetic": True})
    bank = BankingEnv().load("user_task_0")
    adapter = BankingAdapter(bank, rec)
    injector = MockInjector(rec, "clean")
    workflow = MockWorkflow(rec, adapter, injector, "clean")
    manager = RecoveryPointManager(rec, adapter, workflow)
    c0 = manager.capture("a0", "C0", injection_phase="before_injection")
    # Reference args belong to A's task config, not D's business logic.
    from env.tasks import get_task_config
    from env.state import state_dict
    calls = get_task_config("user_task_0").ground_truth(bank.env)
    payment = next(c for c in calls if c.function == "send_money")
    args = dict(payment.args)
    wrong = {**args, "amount": 1337.42}
    rec.save_artifact(Artifact(run_id=rec.run_id, attempt_id="a0", artifact_id="bad",
        producer_execution_id="producer", artifact_type="plan", content=wrong))
    adapter.call_tool("executor", "send_money", wrong, attempt_id="a0", node_id="execute",
                      node_execution_id="exec_bad", inputs=["bad"])
    evaluator = BankingEvaluator(adapter)
    assert not evaluator.evaluate("a0", "attempt").task_success
    audit_before = bank.audit_log()
    def clean_run(attempt):
        adapter.call_tool("executor", "send_money", args, attempt_id=attempt,
                         node_id="execute", node_execution_id="exec_good")
    workflow.run = clean_run
    plan = FullRestartPolicy().plan(FaultNotice(("bad",)), attempt_id="a0", initial_point=c0,
                                   nodes=("research", "plan", "execute"))
    result = RecoveryCoordinator(rec, manager, workflow, injector, evaluator).apply(plan)
    assert result["completed"] and result["task_success"]
    history = evaluator.evaluate(result["attempt_id"], "history")
    assert history.task_success is False and len(history.unauthorized_execution_ids) == 1
    assert bank.audit_log()[0] == audit_before[0]
    assert bank.audit_log()[0]["run_id"] == "bank_trial:a0"
    events = rec.records("events")
    writes = [e for e in events if e["event_type"] == "tool_finished"]
    assert all(e["data"]["state_changed"] for e in writes)
    assert bank.state_dict()["balance"] == state_dict(bank.initial_state)["balance"]


def test_banking_denied_tool_and_read_dependencies(tmp_path):
    rec = Recorder(tmp_path, {"run_id": "bank_trial", "synthetic": True})
    adapter = BankingAdapter(BankingEnv().load("user_task_0"), rec)
    adapter.call_tool("planner", "send_money", {}, attempt_id="a0", node_id="plan", node_execution_id="e1")
    adapter.call_tool("researcher", "read_file", {"file_path": "bill-december-2023.txt"},
                      attempt_id="a0", node_id="research", node_execution_id="e2")
    ends = [e["data"] for e in rec.records("events") if e["event_type"] == "tool_finished"]
    assert ends[0]["allowed"] is False and ends[0]["invoked"] is False
    assert ends[0]["state_changed"] is False
    assert ends[1]["state_reads"] and ends[1]["state_writes"] == []
