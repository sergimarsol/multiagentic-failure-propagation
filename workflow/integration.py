"""Thin A adapter and B-specific reporting; D owns trials and restarts."""

import json
from pathlib import Path

from env import BankingEnv
from eval import evaluate
from experiments.metrics import _cost
from .evidence import export_cases


class BankingEnvironmentAdapter:
    def __init__(self, task_id):
        self.task_id = task_id

    def create_initial_environment(self, task_id):
        return BankingEnv().load(task_id)

    def read_state(self, env):
        return env.state_dict()

    def trace(self, env):
        return env.audit_log()

    def evaluate_execution(self, initial_state, final_state, trace):
        result = evaluate(self.task_id, initial_state, final_state, trace)
        return dict(task_success=result.strict_success if not result.warnings else None,
            agentdojo_utility=result.agentdojo_utility, strict_success=result.strict_success,
            permission_violations=result.permission_violations,
            unauthorized_attempt_ids=[e["call_id"] for e in result.unauthorized_attempted],
            unauthorized_execution_ids=[e["call_id"] for e in result.unauthorized_executed],
            evidence_ids=[e["call_id"] for e in trace], details=result.to_dict())


def workflow_report(directory, *, matched_clean=None):
    """Keep B run-completion and verifier costs separate from A's state verdicts."""
    directory = Path(directory)
    events = [json.loads(line) for line in (directory / "events.jsonl").read_text().splitlines()]
    verifier_steps = {e["step_id"] for e in events if e["event_type"] in {"model_started", "tool_started"}
                      and (e["data"].get("purpose") == "verifier" or e["data"].get("role") == "verifier")}
    attempts = [dict(attempt_id=e["attempt_id"], completed=e["data"]["completed"])
                for e in events if e["event_type"] == "attempt_finished"]
    result = {
        "attempts": attempts,
        "final_workflow_completed": attempts[-1]["completed"] if attempts else False,
        "verification_cost": _cost([e for e in events if e["step_id"] in verifier_steps]),
        "verification_decisions": [e["data"] for e in events if e["event_type"] == "verification_decision"],
        "injection_attempts": [e["data"] for e in events if e["event_type"] == "injection_attempt_finished"],
        "tool_errors": [dict(step_id=e["step_id"], node_id=e["node_id"], error=e["data"].get("error"))
                        for e in events if e["event_type"] == "tool_finished" and e["data"].get("error")],
        "errors": [e["data"] for e in events if e["event_type"] in {"workflow_failed", "attempt_failed", "injection_contract_error"}],
        "note": "Completion and verifier verdicts are not A's task/safety evaluation or propagation labels.",
    }
    (directory / "workflow_summary.json").write_text(json.dumps(result, indent=2) + "\n")
    export_cases(directory, matched_clean=matched_clean)
    return result
