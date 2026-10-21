import json
import pytest
from experiments.phase1 import run_phase1
from scripts.run_phase1_example import AAdapter, ScriptedSession, OneShotFault


@pytest.mark.parametrize("task", ["user_task_0", "user_task_2"])
@pytest.mark.parametrize("condition", ["clean", "fault", "full_restart", "always_verify"])
def test_minimal_banking(tmp_path, task, condition):
    sessions = []
    def factory():
        session = ScriptedSession()
        sessions.append(session)
        return session
    result = run_phase1(tmp_path, task_id=task, condition=condition,
        environment_adapter=AAdapter(task), create_session=factory,
        injector=OneShotFault(), synthetic=True)
    assert result["trace_complete"]
    assert result["initial_task_success"] == (condition == "clean")
    assert result["final_task_success"] == (condition in {"clean", "full_restart"})
    assert result["unauthorized_executions"] == (0 if condition == "clean" else 1)
    assert result["checkpoint_bytes"] == 0
    assert not (tmp_path / "checkpoints").exists()
    assert len(sessions) == (2 if condition == "full_restart" else 1)
    assert result["total_cost"]["model_calls"] == (4 if condition == "full_restart" else 3 if condition == "always_verify" else 2)
    assert result["total_cost"]["input_tokens"] is None
    if condition == "full_restart":
        assert result["recovery_completed"] and result["recomputed_nodes"] == 3
        events = [json.loads(line) for line in (tmp_path / "events.jsonl").read_text().splitlines()]
        assert not any(e["data"].get("enabled") for e in events if e["attempt_id"] == "attempt_1" and e["event_type"] == "fault_injected")


def test_reused_session_rejected(tmp_path):
    session = ScriptedSession()
    result = run_phase1(tmp_path, task_id="user_task_0", condition="full_restart",
        environment_adapter=AAdapter("user_task_0"), create_session=lambda: session,
        injector=OneShotFault(), synthetic=True)
    assert result["recovery_completed"] is False
    assert result["final_task_success"] is None
    assert result["unauthorized_executions"] is None  # incomplete evaluation is not zero


def test_untriggered_does_not_restart(tmp_path):
    result = run_phase1(tmp_path, task_id="user_task_0", condition="full_restart",
        environment_adapter=AAdapter("user_task_0"), create_session=ScriptedSession,
        injector=lambda content, site: {"triggered": False}, synthetic=True)
    assert not result["recovery_attempted"]
    assert not result["injection_triggered"]


@pytest.mark.parametrize("mode", ["shared_environment", "different_initial_state", "factory_failure"])
def test_restart_environment_failure(tmp_path, mode):
    class BrokenAdapter(AAdapter):
        def create_initial_environment(self, task_id):
            if hasattr(self, "previous"):
                if mode == "shared_environment":
                    return self.previous
                if mode == "factory_failure":
                    raise RuntimeError("initialization unavailable")
            env = super().create_initial_environment(task_id)
            self.previous = env
            return env

        def read_state(self, env):
            state = super().read_state(env)
            if mode == "different_initial_state" and hasattr(self, "reads") and self.reads >= 2:
                state["balance"] += 1
            self.reads = getattr(self, "reads", 0) + 1
            return state

    result = run_phase1(tmp_path, task_id="user_task_0", condition="full_restart",
        environment_adapter=BrokenAdapter("user_task_0"), create_session=ScriptedSession,
        injector=OneShotFault(), synthetic=True)
    assert result["recovery_completed"] is False
    assert result["final_task_success"] is None
    assert result["recovery_added_cost"]["tool_calls"] == 0
