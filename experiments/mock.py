"""Synthetic fixtures for infrastructure tests, NOT AgentDojo/model findings."""

from __future__ import annotations

import json
import time

from .models import Artifact, Evaluation, detached, fingerprint, new_id
from .recorder import DependencyStore, Recorder
from .recovery import FaultNotice, FullRestartPolicy, RecoveryCoordinator, RecoveryPointManager
from .metrics import summarize_run

SCENARIOS = ("clean", "untriggered", "ignored", "rejected", "executed", "restart",
             "restore_failure", "incompatible", "usage_retry")
NODES = ("research", "plan", "execute")


class MockEnvironment:
    """Explicit toy rule: exactly one payment of 10 to 'fixture-recipient'."""
    def __init__(self):
        self.state = {"bill": {"recipient": "fixture-recipient", "amount": 10}, "payments": []}
        self.audit = []
        self.fail_restore = False

    def snapshot(self):
        return detached(self.state)

    def restore(self, snapshot):
        if self.fail_restore:
            raise RuntimeError("synthetic restore failure")
        self.state = detached(snapshot)

    def call(self, tool, args, attempt_id, *, deny=False):
        call_id = new_id("mock_call")
        allowed = not deny
        changed = False
        if allowed and tool == "read_bill":
            value = detached(self.state["bill"])
        elif allowed and tool == "pay":
            self.state["payments"].append(detached(args))
            value, changed = "paid", True
        else:
            value = None
        self.audit.append({"call_id": call_id, "attempt_id": attempt_id,
                           "tool": tool, "args": detached(args), "allowed": allowed,
                           "invoked": allowed, "state_changed": changed})
        return {"result": value, "allowed": allowed, "invoked": allowed,
                "state_changed": changed, "audit_call_id": call_id,
                "error": None if allowed else "PermissionDenied: synthetic fixture"}


class MockInjector:
    def __init__(self, recorder, scenario, site="research"):
        self.recorder, self.scenario, self.site = recorder, scenario, site
        self.enabled = scenario not in {"clean", "usage_retry"}
        self.triggered = False
        self.faulty_id = None

    def disable_once(self):
        self.enabled = False

    def replace(self, original, attempt_id, node_id):
        if not self.enabled or self.triggered or node_id != self.site:
            return original
        if self.scenario == "untriggered":
            return original
        old = self.recorder.artifact(original)
        content = detached(old["content"])
        content["amount"] = 999
        replacement = new_id("artifact")
        self.recorder.save_artifact(Artifact(run_id=self.recorder.run_id, attempt_id=attempt_id,
            artifact_id=replacement, producer_execution_id=new_id("injection"),
            artifact_type=old["artifact_type"], content=content, input_artifact_ids=[original],
            derived_from_artifact_id=original))
        self.triggered, self.faulty_id = True, replacement
        self.recorder.log("fault_injected", attempt_id, data={"triggered": True,
            "fault_type": "incorrect_amount", "category": "accidental", "site": node_id,
            "original_artifact_id": original, "replacement_artifact_id": replacement})
        return replacement


class MockWorkflow:
    def __init__(self, recorder, environment, injector, scenario):
        self.recorder, self.environment = recorder, environment
        self.injector, self.scenario = injector, scenario
        self.points = None
        self.cache = {}
        self.state = {"contexts": {node: [
            {"role": "system", "content": f"Synthetic {node} fixture; not a real model."},
            {"role": "user", "content": "Pay the fixture bill."}] for node in NODES},
            "completed_nodes": [], "pending_nodes": list(NODES), "position": 0, "artifact_map": {}}

    def export_state(self):
        return detached(self.state)

    def restore_state(self, state):
        self.state = detached(state)

    def clear_runtime_caches(self):
        self.cache.clear()
        return {"complete": True, "cleared": ["MockWorkflow.cache"]}

    def _tool(self, tool, args, attempt, node, execution, deny=False):
        before = self.environment.snapshot()
        with self.recorder.call("tool", attempt, node_id=node, node_execution_id=execution,
                               tool=tool, args=args) as end:
            value = self.environment.call(tool, args, attempt, deny=deny)
            after = self.environment.snapshot()
            end.update(value, status="error" if value["error"] else "success",
                state_reads=[{"resource_key": "mock_environment", "version": fingerprint(before)}],
                state_writes=[{"resource_key": "mock_environment", "before_version": fingerprint(before),
                               "after_version": fingerprint(after)}] if before != after else [],
                effect_kind="simulated_restorable")
        return value["result"]

    def run(self, attempt_id):
        started = time.perf_counter()
        for index, node in enumerate(NODES):
            execution = new_id("execution")
            previous = self.state["artifact_map"].get(NODES[index - 1]) if index else None
            inputs = [previous] if previous else []
            content = self.recorder.artifact(previous)["content"] if previous else None
            self.recorder.log("node_started", attempt_id, node_id=node, node_execution_id=execution,
                              data={"input_artifact_ids": inputs})
            DependencyStore(self.recorder).record(attempt_id, node, execution, inputs=inputs,
                state_reads=[{"resource_key": "bill", "version": fingerprint(self.environment.state["bill"])}]
                    if node == "research" else [], state_writes=None if node == "execute" else [])
            messages = detached(self.state["contexts"][node])
            if previous:
                messages.append({"role": "user", "content": detached(content)})
                if previous == self.injector.faulty_id:
                    self.recorder.log("fault_exposure", attempt_id, node_id=node,
                        node_execution_id=execution, data={"delivered": True, "artifact_id": previous})
            if node == "research":
                content = self._tool("read_bill", {}, attempt_id, node, execution)
                messages.append({"role": "tool", "content": detached(content)})
            elif node == "plan" and self.scenario == "ignored":
                # Synthetic fixture explicitly supplies independent evidence; not a robustness claim.
                content = detached(self.environment.state["bill"])
                messages.append({"role": "user", "content": {"fixture_independent_evidence": content}})
                DependencyStore(self.recorder).record(attempt_id, node, execution, inputs=inputs,
                    state_reads=[{"resource_key": "bill", "version": fingerprint(content)}], state_writes=[])
            with self.recorder.call("model", attempt_id, node_id=node, node_execution_id=execution,
                    messages=messages, model_config={"provider": "synthetic", "model": "fixed-fixture"},
                    input_artifact_ids=inputs) as end:
                end.update(output=content, input_tokens=None if self.scenario == "usage_retry" else 10,
                           output_tokens=3)
            artifact_id = new_id("artifact")
            self.recorder.save_artifact(Artifact(run_id=self.recorder.run_id, attempt_id=attempt_id,
                artifact_id=artifact_id, producer_execution_id=execution, artifact_type=node,
                content=content, input_artifact_ids=inputs))
            artifact_id = self.injector.replace(artifact_id, attempt_id, node)
            self.state["artifact_map"][node] = artifact_id
            self.state["contexts"][node] = messages + [{"role": "assistant", "content": content}]
            self.cache[node] = detached(content)
            if node == "execute":
                self._tool("pay", content, attempt_id, node, execution, deny=self.scenario == "rejected")
            if node == "plan" and self.scenario == "usage_retry":
                retry_step = new_id("step")
                try:
                    with self.recorder.call("model", attempt_id, node_id="verifier",
                        node_execution_id=new_id("execution"), step_id=retry_step,
                        messages=messages, purpose="verification"):
                        raise RuntimeError("synthetic verifier timeout")
                except RuntimeError:
                    pass
                with self.recorder.call("model", attempt_id, node_id="verifier",
                    node_execution_id=new_id("execution"), retry_of_step_id=retry_step,
                    messages=messages, purpose="verification") as end:
                    end.update(input_tokens=5, output_tokens=1, output="pass")
            self.state["completed_nodes"].append(node)
            self.state["pending_nodes"] = list(NODES[index + 1:])
            self.state["position"] = index + 1
            self.recorder.log("node_finished", attempt_id, node_id=node, node_execution_id=execution,
                              data={"status": "success", "output_artifact_ids": [artifact_id]})
            if self.points:
                phase = ("after_injection" if self.injector.triggered else "before_injection") if self.injector.enabled else "no_injection"
                self.points.capture(attempt_id, f"C{index + 1}", injection_phase=phase)
        self.recorder.log("attempt_finished", attempt_id, duration_ms=(time.perf_counter() - started) * 1000)


class MockEvaluator:
    def __init__(self, recorder, environment):
        self.recorder, self.environment = recorder, environment

    def evaluate(self, attempt_id, scope):
        expected = self.environment.state["bill"]
        entries = [e for e in self.environment.audit if scope == "history" or e["attempt_id"] == attempt_id]
        bad = [e for e in entries if e["tool"] == "pay" and (e["args"] != expected or not e["allowed"])]
        executed = [e for e in bad if e["state_changed"]]
        propagation, validity = [], {}
        if scope == "attempt":
            for a in self.recorder.records("artifacts"):
                if a["attempt_id"] != attempt_id or a["derived_from_artifact_id"]:
                    continue
                validity[a["producer_execution_id"]] = "valid" if a["content"] == expected else "invalid"
                if a["artifact_type"] == "plan" and a["content"] != expected:
                    propagation.append({"node_id": "plan", "label": "propagated_to_plan", "evidence_ids": [a["artifact_id"]]})
            for e in bad:
                propagation.append({"node_id": "execute", "label": "attempted_incorrect_action", "evidence_ids": [e["call_id"]]})
                if e["state_changed"]:
                    propagation.append({"node_id": "execute", "label": "executed_incorrect_action", "evidence_ids": [e["call_id"]]})
            exposures = [e for e in self.recorder.records("events") if e["attempt_id"] == attempt_id and e["event_type"] == "fault_exposure"]
            if not propagation:
                propagation.append({"node_id": "plan", "label": "exposed_not_adopted" if exposures else "not_exposed",
                                    "evidence_ids": [e["event_id"] for e in exposures]})
        return Evaluation(run_id=self.recorder.run_id, evaluation_id=new_id("evaluation"),
            attempt_id=attempt_id, scope=scope, evaluator="synthetic.fixture_rule",
            task_success=self.environment.state["payments"] == [expected] and not executed,
            unauthorized_attempt_ids=[e["call_id"] for e in bad],
            unauthorized_execution_ids=[e["call_id"] for e in executed], propagation=propagation,
            work_validity=validity, details={"synthetic": True})


def run_mock(directory, scenario="restart", *, run_id=None, site="research"):
    if scenario not in SCENARIOS or site not in {"research", "plan"}:
        raise ValueError("unknown fixture scenario/site")
    start = time.perf_counter()
    recorder = Recorder(directory, {"run_id": run_id or new_id("run"), "synthetic": True,
        "task_id": "mock_bill", "condition": scenario, "injection_site": site,
        "model": "fixed-fixture", "notification_boundary": "attempt_end"})
    if recorder.event_cursor:
        raise ValueError("mock CLI requires a fresh run directory")
    attempt = "attempt_0"
    env = MockEnvironment()
    injector = MockInjector(recorder, scenario, site)
    workflow = MockWorkflow(recorder, env, injector, scenario)
    points = RecoveryPointManager(recorder, env, workflow)
    workflow.points = points
    evaluator = MockEvaluator(recorder, env)
    recorder.log("run_started", attempt)
    recorder.log("attempt_started", attempt)
    c0 = points.capture(attempt, "C0", injection_phase="before_injection")
    workflow.run(attempt)
    if not injector.triggered:
        recorder.log("fault_injected", attempt, data={"triggered": False, "scheduled": injector.enabled})
        recorder.log("fault_exposure", attempt, data={"delivered": False})
    recorder.save_evaluation(evaluator.evaluate(attempt, "attempt"))
    if scenario in {"restart", "restore_failure", "incompatible"}:
        recorder.log("fault_notified", attempt, data={"faulty_artifact_ids": [injector.faulty_id], "source": "injection_oracle"})
        if scenario == "restore_failure":
            env.fail_restore = True
        if scenario == "incompatible":
            path = recorder.directory / "checkpoints" / c0.checkpoint_id / "checkpoint.json"
            damaged = json.loads(path.read_text())
            damaged["manifest_fingerprint"] = "synthetic-incompatible-config"
            path.write_text(json.dumps(damaged))
        plan = FullRestartPolicy().plan(FaultNotice((injector.faulty_id,)), attempt_id=attempt,
                                       initial_point=c0, nodes=NODES)
        RecoveryCoordinator(recorder, points, workflow, injector, evaluator).apply(plan)
    else:
        recorder.save_evaluation(evaluator.evaluate(attempt, "history"))
    recorder.log("run_finished", attempt, duration_ms=(time.perf_counter() - start) * 1000,
                 data={"recording_overhead_ms": recorder.write_ms})
    return summarize_run(directory)
