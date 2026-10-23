"""A <-> B/D adapter contracts: scorer warnings become unknown; restart repeats are wired to A's helper."""

import pytest

from env.banking_env import BankingEnv
from experiments.banking import cross_attempt_adapter
from workflow.integration import BankingEnvironmentAdapter

BILL = {"recipient": "UK12345678901234567890", "amount": 98.70, "subject": "bill", "date": "2022-01-01"}


def paid_env(task="user_task_0"):
    env = BankingEnvironmentAdapter(task).create_initial_environment(task)
    env.call_tool("executor", "send_money", BILL)
    return env


def test_clean_verdicts_pass_through():
    env, adapter = paid_env(), BankingEnvironmentAdapter("user_task_0")
    r = adapter.evaluate_execution(env.initial_state, env.snapshot(), env.audit_log())
    assert (r["task_success"], r["agentdojo_utility"], r["strict_success"]) == (True, True, True)


def test_scorer_warnings_make_every_verdict_unknown():
    env, adapter = paid_env(), BankingEnvironmentAdapter("user_task_0")
    wrong_pre = env.snapshot()  # not the task's initial state -> scorer warning
    r = adapter.evaluate_execution(wrong_pre, env.snapshot(), env.audit_log())
    assert r["details"]["warnings"]
    assert (r["task_success"], r["agentdojo_utility"], r["strict_success"]) == (None, None, None)


def test_cross_attempt_adapter_reports_the_restart_repeat():
    a0, a1 = paid_env(), paid_env()
    a0.call_tool("executor", "send_money", {**BILL, "recipient": "US133000000121212121212", "amount": 100.0})
    rows = cross_attempt_adapter("user_task_0")([{"attempt_id": "attempt_0", "audit": a0.audit_log()},
                                                 {"attempt_id": "attempt_1", "audit": a1.audit_log()}])
    assert [(r["attempt_id"], r["tool"]) for r in rows] == [("attempt_1", "send_money")]


@pytest.mark.parametrize("task", ["user_task_0", "user_task_2"])
def test_fresh_environment_per_attempt(task):
    adapter = BankingEnvironmentAdapter(task)
    a, b = adapter.create_initial_environment(task), adapter.create_initial_environment(task)
    assert a is not b and isinstance(a, BankingEnv) and a.state_dict() == b.state_dict()
