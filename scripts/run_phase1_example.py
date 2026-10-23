"""Real A Banking tools/scoring; scripted B/C, no model performance claims."""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from env.banking_env import BankingEnv
from eval.scoring import evaluate
from experiments.banking import BankingAdapter, cross_attempt_adapter
from experiments.phase1 import run_phase1


class AAdapter:
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
            agentdojo_utility=None if result.warnings else result.agentdojo_utility,
            strict_success=None if result.warnings else result.strict_success,
            permission_violations=result.permission_violations,
            unauthorized_attempt_ids=[e["call_id"] for e in result.unauthorized_attempted],
            unauthorized_execution_ids=[e["call_id"] for e in result.unauthorized_executed],
            evidence_ids=[e["call_id"] for e in trace], details=result.to_dict())


class ScriptedSession:
    def __init__(self):
        self.messages = []

    def run(self, env, cb, injector, *, injection_enabled, always_verify):
        assert not self.messages
        bank = BankingAdapter(env, cb.recorder)
        # Ground truth is ONLY a software fixture, never an agent input in experiments.
        calls = env.user_task.ground_truth(env.snapshot())
        writes = [{"tool": c.function, "args": dict(c.args)} for c in calls
                  if env.roles.kind(c.function) == "write"]
        def model(node, execution, messages, output, purpose="agent"):
            self.messages.extend(messages)
            with cb.call("model", node, execution, provider="scripted_fixture",
                         messages=messages, purpose=purpose) as end:
                end.update(output=output, input_tokens=None, output_tokens=None)
        with cb.node("research") as ex:
            for call in calls:
                if env.roles.kind(call.function) == "read":
                    bank.call_tool("researcher", call.function, dict(call.args),
                        attempt_id=cb.attempt_id, node_id="research", node_execution_id=ex)
            model("research", ex, [{"role": "user", "content": env.prompt}], writes)
            original = cb.artifact(ex, writes)
            research = cb.inject(original, ex, injector, site="research", enabled=injection_enabled)
        with cb.node("plan", [research]) as ex:
            content = cb.content(research)
            cb.recorder.log("fault_exposure", cb.attempt_id,
                data={"delivered": research != original, "artifact_id": research})
            model("plan", ex, [{"role": "user", "content": json.dumps(content)}], content)
            plan = cb.artifact(ex, content, [research])
        if always_verify:
            with cb.node("verify", [plan]) as ex:
                # Instrumentation fixture only: pass-through, not a working defense.
                model("verify", ex, [{"role": "user", "content": json.dumps(cb.content(plan))}],
                      "fixture pass-through", purpose="verifier")
        with cb.node("execute", [plan]) as ex:
            for action in cb.content(plan):
                bank.call_tool("executor", action["tool"], action["args"],
                    attempt_id=cb.attempt_id, node_id="execute", node_execution_id=ex, inputs=[plan])


class OneShotFault:
    def __init__(self):
        self.used = False

    def __call__(self, content, site):
        if self.used:
            return {"triggered": False}
        self.used = True
        content[0]["args"]["amount"] = 1337.42
        return {"triggered": True, "replacement": content, "fault_type": "accidental_wrong_amount"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--task", choices=["user_task_0", "user_task_2"], default="user_task_0")
    parser.add_argument("--condition", choices=["clean", "fault", "full_restart", "always_verify"], default="full_restart")
    args = parser.parse_args()
    print(json.dumps(run_phase1(args.output, task_id=args.task, condition=args.condition,
        environment_adapter=AAdapter(args.task), create_session=ScriptedSession,
        injector=OneShotFault(), synthetic=True, cross_attempt_evaluator=cross_attempt_adapter(args.task)), indent=2))


if __name__ == "__main__":
    main()
