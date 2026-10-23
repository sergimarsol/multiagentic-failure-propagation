"""Synchronous application checkpoints and bounded full restart."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import time
from typing import Protocol

from .models import Evaluation, RecoveryPoint, detached, fingerprint, identifier, new_id
from .recorder import Recorder, TraceError, atomic_json


class Environment(Protocol):
    def snapshot(self) -> dict: ...
    def restore(self, snapshot: dict) -> None: ...


class Workflow(Protocol):
    def export_state(self) -> dict:
        """contexts, completed_nodes, pending_nodes, position, artifact_map."""
        ...
    def restore_state(self, state: dict) -> None: ...
    def clear_runtime_caches(self) -> dict:
        """Return {complete: bool, cleared: [...]} for caches owned by B."""
        ...
    def run(self, attempt_id: str) -> None: ...


class FaultController(Protocol):
    def disable_once(self) -> None: ...


class Evaluator(Protocol):
    def evaluate(self, attempt_id: str, scope: str) -> Evaluation: ...


class RecoveryPointManager:
    def __init__(self, recorder: Recorder, environment: Environment, workflow: Workflow):
        self.recorder, self.environment, self.workflow = recorder, environment, workflow

    def _quiescent(self):
        if self.recorder.active_calls:
            raise TraceError("checkpoint operation requires no in-flight calls")

    def capture(self, attempt_id, boundary, *, injection_phase):
        self._quiescent()
        if boundary not in {"C0", "C1", "C2", "C3"}:
            raise ValueError("only stage boundaries C0-C3 are supported")
        if injection_phase not in {"before_injection", "after_injection", "no_injection"}:
            raise ValueError("explicit injection phase required")
        if boundary == "C0":
            forbidden = {"model_started", "tool_started", "fault_injected", "node_started"}
            if any(e["event_type"] in forbidden for e in self.recorder.records("events")):
                raise TraceError("C0 must precede task calls and injection")
            if any(p.boundary == "C0" for p in self.available()):
                raise TraceError("run already has an initial checkpoint")
        start = time.perf_counter()
        checkpoint_id = new_id("cp")
        folder = self.recorder.directory / "checkpoints" / checkpoint_id
        folder.mkdir(parents=True)
        try:
            environment = detached(self.environment.snapshot())
            execution = detached(self.workflow.export_state())
            self._validate_execution(execution)
            stage = int(boundary[1:])
            if execution["position"] != stage or len(execution["completed_nodes"]) != stage:
                raise TraceError("checkpoint boundary does not match completed workflow stages")
            if boundary == "C0" and (execution["completed_nodes"] or execution["artifact_map"]
                                      or execution["position"] != 0 or injection_phase == "after_injection"):
                raise TraceError("C0 must contain initial execution state")
            components = {"environment.json": environment, "execution.json": execution}
            for name, value in components.items():
                atomic_json(folder / name, value)
            point = RecoveryPoint(run_id=self.recorder.run_id, checkpoint_id=checkpoint_id,
                attempt_id=attempt_id, boundary=boundary, injection_phase=injection_phase,
                environment_ref="environment.json", execution_ref="execution.json",
                manifest_fingerprint=fingerprint(self.recorder.manifest),
                component_hashes={name: fingerprint(value) for name, value in components.items()},
                event_cursor=self.recorder.event_cursor)
            # Publish last. Orphan component files never become available checkpoints.
            atomic_json(folder / "checkpoint.json", point.to_dict())
        except Exception as exc:
            self.recorder.log("checkpoint_failed", attempt_id, data={"error": str(exc), "boundary": boundary},
                              duration_ms=(time.perf_counter() - start) * 1000)
            raise
        self.recorder.log("checkpoint_created", attempt_id,
            data={"checkpoint_id": checkpoint_id, "boundary": boundary,
                  "injection_phase": injection_phase,
                  "bytes": sum(p.stat().st_size for p in folder.iterdir())},
            duration_ms=(time.perf_counter() - start) * 1000)
        return point

    def _validate_execution(self, state):
        required = {"contexts", "completed_nodes", "pending_nodes", "position", "artifact_map"}
        if not isinstance(state, dict) or not required <= state.keys():
            raise TraceError("workflow snapshot missing execution/context fields")
        if (not isinstance(state["contexts"], dict) or not isinstance(state["artifact_map"], dict)
                or not isinstance(state["completed_nodes"], list) or not isinstance(state["pending_nodes"], list)):
            raise TraceError("invalid workflow state types")
        for ref in state["artifact_map"].values():
            self.recorder.artifact(ref)

    def load(self, checkpoint_id):
        identifier(checkpoint_id)
        folder = self.recorder.directory / "checkpoints" / checkpoint_id
        try:
            point = RecoveryPoint(**json.loads((folder / "checkpoint.json").read_text()))
            if (point.run_id != self.recorder.run_id or point.checkpoint_id != checkpoint_id
                    or point.status != "ready"
                    or point.manifest_fingerprint != fingerprint(self.recorder.manifest)):
                raise TraceError("checkpoint run/configuration mismatch")
            if (point.environment_ref, point.execution_ref) != ("environment.json", "execution.json"):
                raise TraceError("invalid component references")
            components = {}
            for name in (point.environment_ref, point.execution_ref):
                value = json.loads((folder / name).read_text())
                if fingerprint(value) != point.component_hashes.get(name):
                    raise TraceError(f"checkpoint checksum mismatch: {name}")
                components[name] = value
            self._validate_execution(components[point.execution_ref])
            return point, components[point.environment_ref], components[point.execution_ref]
        except (OSError, TypeError, ValueError, KeyError) as exc:
            raise TraceError(f"checkpoint unavailable: {checkpoint_id}: {exc}") from exc

    def available(self):
        # Corrupt published checkpoints fail explicitly. Unpublished ones are ignored.
        return [self.load(p.parent.name)[0] for p in sorted(
            (self.recorder.directory / "checkpoints").glob("*/checkpoint.json"))]

    def restore(self, checkpoint_id, attempt_id):
        self._quiescent()
        point, environment, execution = self.load(checkpoint_id)
        started = time.perf_counter()
        try:
            self.environment.restore(environment)
            report = self.workflow.clear_runtime_caches()
            if not isinstance(report, dict) or report.get("complete") is not True:
                raise TraceError("workflow did not confirm cache cleanup")
            self.workflow.restore_state(execution)
            if (fingerprint(self.environment.snapshot()) != fingerprint(environment)
                    or fingerprint(self.workflow.export_state()) != fingerprint(execution)):
                raise TraceError("restored environment/context differs from checkpoint")
        except Exception as exc:
            self.recorder.log("checkpoint_restore_failed", attempt_id,
                data={"checkpoint_id": checkpoint_id, "error": str(exc)},
                duration_ms=(time.perf_counter() - started) * 1000)
            raise
        self.recorder.log("checkpoint_restored", attempt_id,
            data={"checkpoint_id": checkpoint_id, "cache_cleanup": report},
            duration_ms=(time.perf_counter() - started) * 1000)
        return point


@dataclass(frozen=True)
class FaultNotice:
    faulty_artifact_ids: tuple[str, ...]
    source: str = "injection_oracle"


@dataclass(frozen=True)
class RecoveryPlan:
    recovery_id: str
    from_attempt_id: str
    checkpoint_id: str
    faulty_artifact_ids: tuple[str, ...]
    rerun_node_ids: tuple[str, ...]
    strategy: str = "full_restart"


class FullRestartPolicy:
    def plan(self, notice: FaultNotice, *, attempt_id, initial_point, nodes):
        if initial_point.boundary != "C0" or not notice.faulty_artifact_ids:
            raise ValueError("full restart requires C0 and a supplied fault identity")
        return RecoveryPlan(new_id("recovery"), attempt_id, initial_point.checkpoint_id,
                            notice.faulty_artifact_ids, tuple(nodes))


class RecoveryCoordinator:
    def __init__(self, recorder, points, workflow, fault_controller, evaluator, *, max_restarts=1):
        if max_restarts < 1:
            raise ValueError("max_restarts must be positive")
        self.recorder, self.points, self.workflow = recorder, points, workflow
        self.fault_controller, self.evaluator = fault_controller, evaluator
        self.max_restarts = max_restarts

    def apply(self, plan: RecoveryPlan):
        events = self.recorder.records("events")
        if sum(e["event_type"] == "recovery_started" for e in events) >= self.max_restarts:
            raise TraceError("restart limit reached")
        if plan.strategy != "full_restart":
            raise ValueError("only full restart is implemented")
        for ref in plan.faulty_artifact_ids:
            if self.recorder.artifact(ref)["attempt_id"] != plan.from_attempt_id:
                raise TraceError("fault belongs to a different attempt")
        self.points._quiescent()
        if not any(e["scope"] == "attempt" and e["attempt_id"] == plan.from_attempt_id
                   for e in self.recorder.records("evaluations")):
            self.recorder.save_evaluation(self.evaluator.evaluate(plan.from_attempt_id, "attempt"))
        new_attempt = new_id("attempt")
        started = time.perf_counter()
        self.recorder.log("recovery_started", plan.from_attempt_id, recovery_id=plan.recovery_id,
            data={"strategy": plan.strategy, "faulty_artifact_ids": list(plan.faulty_artifact_ids),
                  "checkpoint_id": plan.checkpoint_id, "rerun_node_ids": list(plan.rerun_node_ids),
                  "new_attempt_id": new_attempt})
        completed, correct, error = False, None, None
        try:
            point, _, execution = self.points.load(plan.checkpoint_id)
            if point.boundary != "C0" or tuple(execution["pending_nodes"]) != plan.rerun_node_ids:
                raise TraceError("plan must rerun the complete initial workflow")
            self.points.restore(plan.checkpoint_id, new_attempt)
            self.fault_controller.disable_once()
            self.recorder.log("fault_disabled", new_attempt, recovery_id=plan.recovery_id)
            self.recorder.log("attempt_started", new_attempt,
                              data={"restored_from_checkpoint_id": plan.checkpoint_id})
            self.workflow.run(new_attempt)
            evaluation = self.evaluator.evaluate(new_attempt, "attempt")
            self.recorder.save_evaluation(evaluation)
            correct = evaluation.task_success
            completed = True
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
        self.recorder.log("recovery_finished", new_attempt, recovery_id=plan.recovery_id,
            duration_ms=(time.perf_counter() - started) * 1000,
            data={"completed": completed, "task_success": correct, "error": error})
        # A failed restore may have left partial state. Never silently call it successful.
        try:
            self.recorder.save_evaluation(self.evaluator.evaluate(new_attempt, "history"))
        except Exception as exc:
            self.recorder.log("evaluation_failed", new_attempt, data={"scope": "history", "error": str(exc)})
        return {"attempt_id": new_attempt, "completed": completed,
                "task_success": correct, "error": error}
