"""Shared synchronous application checkpoints and recovery data contracts."""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
import time
from typing import Protocol

from .models import RecoveryPoint, detached, fingerprint, identifier, new_id
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


class RecoveryPointManager:
    def __init__(self, recorder: Recorder, environment: Environment, workflow: Workflow):
        self.recorder, self.environment, self.workflow = recorder, environment, workflow

    def _quiescent(self):
        if self.recorder.active_calls:
            raise TraceError("checkpoint operation requires no in-flight calls")

    def capture(self, attempt_id, boundary, *, injection_phase):
        self._quiescent()
        if not re.fullmatch(r"C[0-9]+", boundary):
            raise ValueError("only completed-node boundaries C0-Cn are supported")
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
    checkpoint_id: str | None
    faulty_artifact_ids: tuple[str, ...]
    rerun_node_ids: tuple[str, ...]
    strategy: str = "full_restart"
    reuse_artifact_ids: tuple[str, ...] = ()
    state_action: str = "restore_checkpoint"
    fallback_policy: str = "full_restart"
