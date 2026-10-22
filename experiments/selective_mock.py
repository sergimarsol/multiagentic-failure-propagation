"""Toy arithmetic/state fixtures, NOT Banking or A/C business rules."""
from time import perf_counter
from dataclasses import asdict

from .models import Evaluation, detached, new_id
from .protocol import Budget, Limits, provenance, normalize_recovery_policy, validate_config
from .recorder import Recorder
from .recovery import FaultNotice
from .selective import DagWorkflow, Node, SelectiveRecoveryCoordinator
from .metrics import summarize_run


class ToyEnvironment:
    def __init__(self):
        self.state = {"a": 10, "b": 20, "out": []}
        self.audit = []  # kept outside reversible state

    def snapshot(self):
        return detached(self.state)

    def restore(self, state):
        self.state = detached(state)


def make_workflow(recorder, scenario="late", *, nodes=None):
    env = ToyEnvironment()
    if nodes is None:
        if scenario == "dag":
            nodes = [Node("a", reads=("a",)), Node("b", reads=("b",)),
                     Node("plan", inputs=("a", "b")), Node("execute", inputs=("plan",), writes=("out",))]
        elif scenario == "shared":
            nodes = [Node("a", reads=("a",)), Node("mutate", inputs=("a",), reads=("b",), writes=("b",)),
                     Node("b", reads=("b",)), Node("plan", inputs=("a", "b")),
                     Node("execute", inputs=("plan",), writes=("out",))]
        else:
            nodes = [Node("a", reads=("a",)), Node("plan", inputs=("a",)),
                     Node("execute", inputs=("plan",), writes=("out",))]
    def execute(node, inputs, environment, context, cb, execution):
        if node.name in {"a", "b"}:
            with cb.call("model", node.name, execution, provider="synthetic_arithmetic_fixture",
                         messages=[{"role": "user", "content": str(environment.state[node.name])}]) as end:
                value = environment.state[node.name]
                end.update(output=value, input_tokens=2, output_tokens=1)
        elif node.name == "plan":
            with cb.call("model", node.name, execution, provider="synthetic_arithmetic_fixture", messages=inputs) as end:
                value = sum(inputs.values())
                end.update(output=value, input_tokens=2, output_tokens=1)
        else:
            with cb.call("tool", node.name, execution, tool="toy_" + node.name, args=inputs) as end:
                if node.name == "mutate":
                    environment.state["b"] = inputs["a"] + 1
                    value = environment.state["b"]
                else:
                    value = inputs["plan"]
                    environment.state["out"].append(value)
                action_id = new_id("action")
                environment.audit.append({"action_id": action_id, "node": node.name, "value": value,
                    "attempt_id": cb.attempt_id, "segment_id": cb.recorder.segment_id})
                end.update(result=value, invoked=True, state_changed=True, audit_call_id=action_id)
        context["output"] = value
        return value
    used = False
    def injector(content, site):
        nonlocal used
        if used:
            return {"triggered": False}
        used = True
        return {"triggered": True, "replacement": 999, "fault_type": "toy_wrong_number"}
    fault = "plan" if scenario == "late" else "mutate" if scenario == "shared" else "a"
    return DagWorkflow(recorder, env, nodes, execute, config={"toy_scenario": scenario},
                       injector=injector, fault_node=fault)


def toy_evaluate(workflow, attempt):
    expected = 30 if len(workflow.nodes) == 4 else 21 if len(workflow.nodes) == 5 else 10
    correct = workflow.environment.state["out"] == [expected]
    # Explicit toy oracle, isolated from A's Banking task/safety scoring.
    wrong = [e["action_id"] for e in workflow.environment.audit if e["node"] == "execute" and e["value"] != expected]
    return Evaluation(run_id=workflow.recorder.run_id, attempt_id=attempt,
        evaluation_id=new_id("evaluation"), scope="attempt", evaluator="synthetic_arithmetic_oracle",
        task_success=correct, unauthorized_attempt_ids=wrong, unauthorized_execution_ids=wrong,
        details={"synthetic": True, "expected_output": expected})


def run_selective_mock(directory, scenario="late", strategy="selective", *, limits=None, checkpoint_retries=1, max_checkpoint_attempts=3,
                       experiment_config=None):
    policy = normalize_recovery_policy(dict(checkpoint_retries=checkpoint_retries,
                                             max_checkpoint_attempts=max_checkpoint_attempts))
    if experiment_config is not None:
        experiment_config = validate_config(experiment_config)
        if (experiment_config["task_id"], experiment_config["condition"], experiment_config["synthetic"]) != ("toy_" + scenario, strategy, True):
            raise ValueError("toy runner/configuration mismatch")
        policy = normalize_recovery_policy(experiment_config.get("recovery_policy", policy))
        limits = Limits(**experiment_config["limits"])
    limits = limits or Limits()
    rec = Recorder(directory, {"run_id": new_id("run"), "synthetic": True, "condition": strategy,
        "task_id": "toy_" + scenario, "recovery_policy": policy, "limits": asdict(limits),
        **provenance(experiment_config)})
    rec.budget = Budget(limits)
    start = perf_counter()
    rec.segment_id = "initial"
    rec.log("attempt_started", "attempt_0")
    workflow = make_workflow(rec, scenario)
    workflow.points.capture("attempt_0", "C0", injection_phase="before_injection")
    workflow.run("attempt_0")
    rec.save_evaluation(toy_evaluate(workflow, "attempt_0"))
    coordinator = SelectiveRecoveryCoordinator(workflow, lambda r: make_workflow(r, scenario), toy_evaluate,
        **policy)
    result = coordinator.recover(FaultNotice(tuple(workflow.faulty_ids)), "attempt_0", requested_strategy=strategy)
    evaluations = rec.records("evaluations")
    wrong = sorted({a for ev in evaluations for a in ev["unauthorized_execution_ids"] or []})
    rec.save_evaluation(Evaluation(run_id=rec.run_id, attempt_id=result["attempt_id"],
        evaluation_id=new_id("evaluation"), scope="history", evaluator="synthetic_history_union",
        unauthorized_attempt_ids=wrong, unauthorized_execution_ids=wrong,
        details={"business_scoring": "toy_only"}))
    rec.log("run_finished", result["attempt_id"], duration_ms=(perf_counter()-start)*1000,
            data={"recording_overhead_ms": rec.write_ms, "budget": rec.budget.report()})
    summary = summarize_run(directory)
    return summary
