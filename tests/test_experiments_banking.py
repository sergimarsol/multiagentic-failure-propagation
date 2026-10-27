"""Integration with A's real Banking adapter/scorer, without an LLM."""

from env.banking_env import BankingEnv
from experiments.banking import BankingAdapter, BankingEvaluator
from experiments.models import Artifact
from experiments.recorder import Recorder
from experiments.recovery import RecoveryPointManager
from tests.d_fixtures import MockInjector, MockWorkflow


def test_banking_snapshot_restore_preserves_audit(tmp_path):
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
    manager.restore(c0.checkpoint_id, "a1")
    clean_run("a1")
    assert evaluator.evaluate("a1", "attempt").task_success
    history = evaluator.evaluate("a1", "history")
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


def segmented_bank(tmp_path):
    from env.tasks import get_task_config
    rec = Recorder(tmp_path, {"run_id": "segmented", "synthetic": True})
    bank = BankingEnv().load("user_task_0")
    adapter = BankingAdapter(bank, rec, segmented_audit=True)
    args = dict(next(c for c in get_task_config("user_task_0").ground_truth(bank.env)
                     if c.function == "send_money").args)
    def pay(**overrides):
        return adapter.call_tool("executor", "send_money", {**args, **overrides},
            attempt_id="a0", node_id="execute", node_execution_id="execution")
    return rec, bank, adapter, BankingEvaluator(adapter), pay


def test_segment_restore_excludes_undone_calls_but_keeps_history(tmp_path):
    rec, bank, adapter, evaluator, pay = segmented_bank(tmp_path)
    initial = adapter.snapshot()
    wrong = pay(amount=1337.42)
    first_key = bank.audit_log()[0]["run_id"]
    adapter.restore(initial)
    correct = pay()
    assert bank.audit_log()[-1]["run_id"] != first_key
    effective = evaluator.evaluate("a0", "attempt")
    assert effective.strict_success is True
    assert effective.evidence_ids == [correct.call_id]
    history = evaluator.evaluate("a0", "history")
    assert wrong.call_id in history.unauthorized_execution_ids
    assert len(bank.audit_log()) == 2
    assert len([e for e in rec.records("events") if e["event_type"] == "tool_finished"]) == 2
    assert evaluator.evaluate("a0", "attempt", audit_view="history").strict_success is False


def test_segment_restore_retains_prefix_and_detects_duplicate(tmp_path):
    _, bank, adapter, evaluator, pay = segmented_bank(tmp_path)
    first = pay()
    checkpoint = adapter.snapshot()
    abandoned = pay(amount=1337.42)
    adapter.restore(checkpoint)
    duplicate = pay()
    effective = evaluator.evaluate("a0", "attempt")
    assert effective.evidence_ids == [first.call_id, duplicate.call_id]
    assert abandoned.call_id not in effective.evidence_ids
    assert duplicate.call_id in effective.unauthorized_execution_ids
    assert effective.strict_success is False
    assert len(bank.audit_log()) == 3
    assert len(set(effective.details["audit_source_keys"].values())) == 2


def test_segment_checkpoint_roundtrip_and_missing_path_rejected(tmp_path):
    import pytest
    from experiments.recorder import TraceError
    rec, bank, adapter, evaluator, pay = segmented_bank(tmp_path)
    workflow = MockWorkflow(rec, adapter, MockInjector(rec, "clean"), "clean")
    manager = RecoveryPointManager(rec, adapter, workflow)
    point = manager.capture("a0", "C0", injection_phase="before_injection")
    pay(amount=1337.42)
    manager.restore(point.checkpoint_id, "a0")
    pay()
    assert evaluator.evaluate("a0", "attempt").strict_success is True
    state = bank.state_dict()
    with pytest.raises(TraceError, match="effective audit path"):
        adapter.restore(state)
    assert bank.state_dict() == state
