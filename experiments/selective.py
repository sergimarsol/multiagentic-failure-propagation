"""Fixed-DAG, synchronous recovery over trusted application adapters.

Node functions receive only explicit inputs and a per-node context. Adapters must
truthfully declare reads/writes; this module is not a sandbox for malicious tools.
"""
from dataclasses import dataclass
from time import perf_counter

from .models import Artifact, Evaluation, detached, fingerprint, new_id
from .phase1 import Callbacks
from .recorder import TraceError
from .recovery import RecoveryPlan, RecoveryPointManager


class RetryableNodeError(RuntimeError):
    """Adapter-declared transient call failure, not a generic execution error.

    The adapter must wait for the call to terminate and report all effects before
    raising this. An in-flight or ambiguous external action must not be retried.
    """


@dataclass(frozen=True)
class Node:
    name: str
    inputs: tuple[str, ...] = ()
    controls: tuple[str, ...] = ()
    reads: tuple[str, ...] = ()
    writes: tuple[str, ...] = ()
    dependencies_complete: bool = True
    effects_restorable: bool = True


def order_nodes(nodes):
    table = {n.name: n for n in nodes}
    if len(table) != len(nodes):
        raise ValueError("duplicate node")
    ordered = []
    while len(ordered) < len(nodes):
        ready = [n for n in nodes if n.name not in ordered and
                 set(n.inputs + n.controls) <= set(ordered)]
        if not ready:
            raise ValueError("cycle or missing dependency")
        ordered.append(ready[0].name)
    return [table[n] for n in ordered]


class DagWorkflow:
    """Optional advanced B adapter; Phase 1 sessions need not subclass this.

    execute(node, inputs, environment, node_context, callbacks, execution_id)
    returns JSON. Mutable model histories must live ONLY in node_context. Each
    node executes at most once per segment, using a fresh node context. Hidden
    shared mutable caches are outside this adapter contract.
    """
    def __init__(self, recorder, environment, nodes, execute, *, config, injector=None, fault_node=None):
        self.recorder, self.environment = recorder, environment
        self.nodes = order_nodes(nodes)
        self.execute = execute
        self.config_hash = fingerprint({"configuration": config, "nodes": [
            {"name": n.name, "inputs": list(n.inputs), "controls": list(n.controls),
             "reads": list(n.reads), "writes": list(n.writes),
             "dependencies_complete": n.dependencies_complete, "effects_restorable": n.effects_restorable}
            for n in self.nodes]})
        self.injector, self.fault_node = injector, fault_node
        self.injection_enabled = True
        self.faulty_ids = []
        initial = environment.snapshot()
        self.state = dict(contexts={}, completed_nodes=[], pending_nodes=[n.name for n in self.nodes],
            position=0, artifact_map={}, config_hash=self.config_hash,
            resources={k: {"version": fingerprint(v), "source": None} for k, v in initial.items()})
        self.initial_fingerprint = fingerprint(initial)
        self.points = RecoveryPointManager(recorder, environment, self)

    def export_state(self):
        return detached(self.state)

    def restore_state(self, state):
        if state.get("config_hash") != self.config_hash:
            raise TraceError("workflow configuration mismatch")
        self.state = detached(state)

    def clear_runtime_caches(self):
        self.state["contexts"] = {}
        return {"complete": True, "cleared": ["per_node_contexts"], "contract": "no hidden caches"}

    def _done(self, node, artifact, context):
        self.state["artifact_map"][node.name] = artifact
        self.state["contexts"][node.name] = detached(context)
        self.state["completed_nodes"].append(node.name)
        self.state["pending_nodes"].remove(node.name)
        self.state["position"] = len(self.state["completed_nodes"])

    def run_node(self, node, attempt):
        cb = Callbacks(self.recorder, attempt)
        refs = {name: self.state["artifact_map"][name] for name in node.inputs + node.controls}
        if set(refs.values()) & set(self.faulty_ids):
            self.recorder.log("fault_exposure", attempt, node_id=node.name,
                data={"delivered": True, "input_artifact_ids": list(refs.values())})
        before = detached(self.environment.snapshot())
        # Undeclared resource changes between boundaries make provenance unknown.
        for key, value in before.items():
            current = self.state["resources"].get(key)
            if current is None or current.get("value_hash", current["version"]) != fingerprint(value):
                self.state["resources"][key] = {"version": new_id("external"),
                    "value_hash": fingerprint(value), "source": None, "external": True}
        reads = {key: detached(self.state["resources"][key]) for key in node.reads}
        sources = [v["source"] for v in reads.values() if v.get("source")]
        all_inputs = list(dict.fromkeys([*refs.values(), *sources]))
        with cb.node(node.name, all_inputs) as execution:
            context = {}
            try:
                content = self.execute(node, {k: cb.content(v) for k, v in refs.items()},
                                       self.environment, context, cb, execution)
                after = detached(self.environment.snapshot())
                if set(after) != set(before):
                    raise TraceError("resource inventory changes are unsupported")
                changed = [k for k in before if before[k] != after[k]]
                if not set(changed) <= set(node.writes):
                    raise TraceError("undeclared state write")
                original = cb.artifact(execution, content, all_inputs)
                ref = original
                if node.name == self.fault_node:
                    ref = cb.inject(original, execution, self.injector, site=node.name,
                                    enabled=self.injection_enabled)
                    self.faulty_ids.extend(cb.faulty_artifact_ids)
                writes = {}
                for key in changed:
                    old = self.state["resources"][key]
                    new = {"version": new_id("version"), "value_hash": fingerprint(after[key]), "source": ref}
                    writes[key] = {"before": detached(old), "after": detached(new)}
                    self.state["resources"][key] = new
                # Context for an injected output must reflect what is actually delivered.
                context["delivered_output"] = cb.content(ref)
                self._done(node, ref, context)
                self.recorder.log("node_result", attempt, node_id=node.name, node_execution_id=execution,
                    data={"artifact_id": ref, "input_map": refs, "input_artifact_ids": all_inputs,
                          "control_artifact_ids": [refs[k] for k in node.controls],
                          "state_reads": reads, "state_writes": writes,
                          "dependencies_complete": node.dependencies_complete,
                          "effects_restorable": node.effects_restorable, "config_hash": self.config_hash,
                          "context": detached(context)})
            except Exception as exc:
                try:
                    rec_after = detached(self.environment.snapshot())
                    observation_error = None
                except Exception as snapshot_exc:
                    rec_after = None
                    observation_error = str(snapshot_exc)
                self.recorder.log("node_failed_effects", attempt, node_id=node.name,
                    node_execution_id=execution, data={"error": str(exc), "before": before,
                        "after": rec_after,
                        "state_writes": before != rec_after if observation_error is None else None,
                        "state_observation_complete": observation_error is None,
                        "observation_error": observation_error,
                        "effects_restorable": node.effects_restorable})
                raise
        self.points.capture(attempt, f"C{self.state['position']}",
                            injection_phase="after_injection" if self.faulty_ids else "no_injection")

    def run(self, attempt):
        for node in self.nodes:
            if node.name in self.state["pending_nodes"]:
                self.run_node(node, attempt)


class InvalidationGraph:
    """Restrict traversal to one attempt; reused artifacts retain immutable ancestry."""
    def __init__(self, recorder, attempt):
        self.recorder, self.attempt = recorder, attempt
        self.artifacts = {a["artifact_id"]: a for a in recorder.records("artifacts") if a["attempt_id"] == attempt}
        self.results = [e for e in recorder.records("events")
                        if e["attempt_id"] == attempt and e["event_type"] == "node_result"]

    def invalidate(self, roots):
        if not roots or not set(roots) <= self.artifacts.keys():
            raise TraceError("unknown or foreign faulty artifact")
        invalid = set(roots)
        changed = True
        while changed:
            changed = False
            for ref, a in self.artifacts.items():
                deps = set(a["input_artifact_ids"])
                # A derivative of a bad result is suspect; the original of a bad replacement is not.
                if a.get("derived_from_artifact_id"):
                    deps.add(a["derived_from_artifact_id"])
                if ref not in invalid and deps & invalid:
                    invalid.add(ref)
                    changed = True
            for event in self.results:
                d = event["data"]
                deps = set(d["input_artifact_ids"] + d["control_artifact_ids"])
                deps.update(v["source"] for v in d["state_reads"].values() if v.get("source"))
                if d["artifact_id"] not in invalid and deps & invalid:
                    invalid.add(d["artifact_id"])
                    changed = True
        return invalid


class SelectiveRecoveryPolicy:
    def plan(self, notice, *, attempt_id, workflow, fallback_policy="full_restart"):
        return self.candidate_plans(notice, attempt_id=attempt_id, workflow=workflow,
                                    fallback_policy=fallback_policy)[0]

    def candidate_plans(self, notice, *, attempt_id, workflow, fallback_policy="full_restart"):
        """Freeze eligible points and reuse candidates before starting recovery."""
        graph = InvalidationGraph(workflow.recorder, attempt_id)
        invalid = graph.invalidate(notice.faulty_artifact_ids)
        if any(e["event_type"] == "node_failed_effects" and e["attempt_id"] == attempt_id
               for e in workflow.recorder.records("events")):
            raise TraceError("incomplete node effects require full restart")
        current = workflow.state["artifact_map"]
        results = {e["data"]["artifact_id"]: e for e in graph.results}
        if any(ref not in results or not results[ref]["data"]["dependencies_complete"] for ref in current.values()):
            raise TraceError("incomplete dependency capture")
        if any(not e["data"]["effects_restorable"] and e["data"]["state_writes"] for e in graph.results):
            raise TraceError("non-restorable effects require explicit resolution")
        candidates = []
        for path in (workflow.recorder.directory / "checkpoints").glob("*/checkpoint.json"):
            try:
                point, env, state = workflow.points.load(path.parent.name)
                if point.attempt_id != attempt_id or state.get("config_hash") != workflow.config_hash:
                    continue
                refs = set(state["artifact_map"].values())
                # DagWorkflow contexts are isolated per node, with dependencies captured in its artifact.
                if refs & invalid or any(ref not in results for ref in refs):
                    continue
                if set(state["contexts"]) != set(state["artifact_map"]):
                    continue
                if any(state["contexts"][n] != results[ref]["data"]["context"] for n, ref in state["artifact_map"].items()):
                    continue
                candidates.append((point, env, state))
            except (TraceError, ValueError):
                continue
        if not candidates:
            raise TraceError("no consistent uncontaminated recovery point")
        plans = []
        for point, _, state in sorted(candidates, key=lambda item: item[0].event_cursor, reverse=True):
            pending = [n.name for n in workflow.nodes if n.name not in state["completed_nodes"]]
            reuse = [current[n] for n in pending if n in current and current[n] not in invalid]
            plan = RecoveryPlan(new_id("recovery"), attempt_id, point.checkpoint_id,
                tuple(notice.faulty_artifact_ids), tuple(pending), strategy="selective",
                reuse_artifact_ids=tuple(reuse), fallback_policy=fallback_policy)
            workflow.recorder.log("invalidation_decided", attempt_id, recovery_id=plan.recovery_id,
                data={"invalid_artifact_ids": sorted(invalid), "checkpoint_id": point.checkpoint_id,
                      "potential_only": True, "candidate_reuse_ids": reuse})
            plans.append(plan)
        return plans


def check_reuse(workflow, node, record):
    d = record["data"]
    if not d["dependencies_complete"]:
        return "unknown", "incomplete_dependencies"
    if d["config_hash"] != workflow.config_hash:
        return "recompute", "configuration_changed"
    if node.writes or d["state_writes"]:
        return "recompute", "write_effects_must_be_executed_not_replayed"
    actual = {k: workflow.state["artifact_map"].get(k) for k in node.inputs + node.controls}
    if actual != d["input_map"]:
        return "recompute", "data_or_control_input_changed"
    state = workflow.environment.snapshot()
    for key, expected in d["state_reads"].items():
        current = workflow.state["resources"].get(key)
        if current != expected or key not in state or fingerprint(state[key]) != expected.get("value_hash", expected["version"]):
            return "recompute", "state_version_or_lineage_changed"
    return "reuse", "inputs_state_context_configuration_compatible"


class SelectiveRecoveryCoordinator:
    """Execute a policy using the same recorder, checkpoint manager and evaluator.

    factory(recorder) -> new DagWorkflow is used ONLY for explicit full fallback.
    evaluate(workflow, attempt) -> Evaluation; no business scoring lives here.
    """
    def __init__(self, workflow, factory, evaluate, *, fallback_policy="full_restart", max_recoveries=1,
                 checkpoint_retries=1, max_checkpoint_attempts=3):
        if fallback_policy not in {"full_restart", "error"}:
            raise ValueError("unknown fallback policy")
        if not isinstance(workflow, DagWorkflow):
            raise TypeError("selective recovery requires the optional fixed-DAG adapter; Phase 1 sessions remain supported by run_phase1")
        self.workflow, self.factory, self.evaluate = workflow, factory, evaluate
        if type(max_recoveries) is not int or max_recoveries < 1:
            raise ValueError("max_recoveries must be positive")
        if type(checkpoint_retries) is not int or checkpoint_retries < 0:
            raise ValueError("checkpoint_retries must be a nonnegative integer")
        if type(max_checkpoint_attempts) is not int or max_checkpoint_attempts < 1:
            raise ValueError("max_checkpoint_attempts must be positive")
        self.checkpoint_retries = checkpoint_retries
        self.max_checkpoint_attempts = max_checkpoint_attempts
        self.max_recoveries = max_recoveries
        self.fallback_policy = fallback_policy

    def _execute_plan(self, plan, attempt, result, rid):
        w, rec = self.workflow, self.workflow.recorder
        result["retained_artifact_ids"] = []
        result["reused_artifact_ids"] = []
        result["rerun_node_ids"] = []
        w.injection_enabled = False
        result["retained_artifact_ids"] = list(w.state["artifact_map"].values())
        for node_name, ref in w.state["artifact_map"].items():
            rec.log("artifact_retained", attempt, node_id=node_name, recovery_id=rid,
                data={"artifact_id": ref, "checkpoint_id": plan.checkpoint_id,
                      "reason": "validated_checkpoint_prefix"})
        rec.log("fault_disabled", attempt, recovery_id=rid)
        candidates = {e["data"]["artifact_id"]: e for e in rec.records("events") if e["event_type"] == "node_result"}
        result["executed_strategy"] = "selective"
        for node in w.nodes:
            if node.name not in w.state["pending_nodes"]:
                continue
            candidate = next((candidates[ref] for ref in plan.reuse_artifact_ids
                              if candidates[ref]["node_id"] == node.name), None)
            decision, reason = "recompute", "no_valid_candidate"
            if candidate:
                tick = perf_counter()
                decision, reason = check_reuse(w, node, candidate)
                rec.log("reuse_checked", attempt, node_id=node.name, recovery_id=rid,
                    duration_ms=(perf_counter()-tick)*1000,
                    data={"artifact_id": candidate["data"]["artifact_id"], "decision": decision,
                          "reason": reason, "validator_version": "fixed_dag_v1",
                          "evidence_ids": [candidate["event_id"]]})
            if decision == "reuse":
                ref = candidate["data"]["artifact_id"]
                w._done(node, ref, candidate["data"]["context"])
                result["reused_artifact_ids"].append(ref)
                rec.log("artifact_reused", attempt, node_id=node.name, recovery_id=rid,
                        data={"artifact_id": ref, "source_execution_id": candidate["node_execution_id"]})
            else:
                result["rerun_node_ids"].append(node.name)
                w.run_node(node, attempt)

    def _recover_checkpoints(self, notice, attempt, result, rid, require_safe_reset):
        from .protocol import BudgetExceeded
        w, rec = self.workflow, self.workflow.recorder
        tick = perf_counter()
        plans = SelectiveRecoveryPolicy().candidate_plans(
            notice, attempt_id=attempt, workflow=w, fallback_policy=self.fallback_policy)
        plans = plans[:self.max_checkpoint_attempts]
        rec.log("recovery_planned", attempt, recovery_id=rid,
                duration_ms=(perf_counter()-tick)*1000,
                data={"checkpoint_ids": [p.checkpoint_id for p in plans],
                      "checkpoint_id": plans[0].checkpoint_id,
                      "checkpoint_retries": self.checkpoint_retries,
                      "max_checkpoint_attempts": self.max_checkpoint_attempts})
        last_error = None
        for index, plan in enumerate(plans):
            for retry in range(self.checkpoint_retries + 1):
                if rec.budget:
                    rec.budget.check()
                require_safe_reset()
                parent = rec.segment_id
                rec.segment_id = new_id("segment")
                result["checkpoint_attempts"] += 1
                rec.log("segment_started", attempt, recovery_id=rid,
                        data={"checkpoint_id": plan.checkpoint_id, "parent_segment_id": parent,
                              "checkpoint_index": index, "retry_index": retry})
                rec.log("checkpoint_attempt_started", attempt, recovery_id=rid,
                        data={"checkpoint_id": plan.checkpoint_id, "retry_index": retry})
                phase = "restore"
                try:
                    w.points.restore(plan.checkpoint_id, attempt)
                    phase = "execute"
                    self._execute_plan(plan, attempt, result, rid)
                except BudgetExceeded:
                    raise
                except Exception as exc:
                    last_error = exc
                    rec.log("checkpoint_attempt_failed", attempt, recovery_id=rid,
                            data={"checkpoint_id": plan.checkpoint_id, "retry_index": retry,
                                  "phase": phase, "error": f"{type(exc).__name__}: {exc}"})
                    require_safe_reset()
                    if phase == "execute" and not isinstance(exc, RetryableNodeError):
                        raise  # Structural/programming errors are not transient calls.
                    if phase == "execute" and retry < self.checkpoint_retries:
                        result["checkpoint_retry_count"] += 1
                        rec.log("checkpoint_retry_scheduled", attempt, recovery_id=rid,
                                data={"checkpoint_id": plan.checkpoint_id, "retry_index": retry + 1})
                        continue
                    if index + 1 < len(plans):
                        rec.log("checkpoint_older_selected", attempt, recovery_id=rid,
                                data={"from_checkpoint_id": plan.checkpoint_id,
                                      "checkpoint_id": plans[index + 1].checkpoint_id,
                                      "reason": "restore_failed" if phase == "restore" else "retries_exhausted"})
                    break
                else:
                    result["checkpoint_id"] = plan.checkpoint_id
                    rec.log("checkpoint_attempt_succeeded", attempt, recovery_id=rid,
                            data={"checkpoint_id": plan.checkpoint_id, "retry_index": retry})
                    return
        raise TraceError(f"eligible checkpoint attempts exhausted: {last_error}") from last_error

    def recover(self, notice, attempt, *, requested_strategy="selective"):
        if requested_strategy not in {"selective", "full_restart"}:
            raise ValueError("unsupported recovery strategy")
        w, rec = self.workflow, self.workflow.recorder
        if sum(e["event_type"] == "recovery_started" for e in rec.records("events")) >= self.max_recoveries:
            raise TraceError("recovery limit reached")
        policy_settings = dict(checkpoint_retries=self.checkpoint_retries,
            max_checkpoint_attempts=self.max_checkpoint_attempts,
            max_recoveries=self.max_recoveries, fallback_policy=self.fallback_policy)
        rid, started = new_id("recovery"), perf_counter()
        segment = new_id("segment")
        rec.log("recovery_started", attempt, recovery_id=rid,
            data={"requested_strategy": requested_strategy, "faulty_artifact_ids": list(notice.faulty_artifact_ids),
                  "source": notice.source, "notification_boundary": "after_initial_workflow",
                  "recovery_segment_id": segment, "recovery_policy": policy_settings})
        result = {"recovery_policy": policy_settings, "requested_strategy": requested_strategy, "executed_strategy": None,
                  "fallback_reason": None, "completed": False, "task_success": None,
                  "checkpoint_attempts": 0, "checkpoint_retry_count": 0, "checkpoint_id": None,
                  "unresolved_effects": [], "retained_artifact_ids": [], "reused_artifact_ids": [], "rerun_node_ids": []}
        original_initial = w.initial_fingerprint
        # Audit evidence survives every rollback and every recovery branch.
        def refresh_unresolved_effects():
            effects = [e["event_id"] for e in rec.records("events")
                       if e["event_type"] in {"node_result", "node_failed_effects"}
                       and (e["data"].get("state_observation_complete") is False
                            or ((bool(e["data"].get("state_writes"))
                                 or e["data"].get("state_writes") is None)
                                and not e["data"]["effects_restorable"]))]
            result["unresolved_effects"] = list(dict.fromkeys(
                result["unresolved_effects"] + effects))
            return result["unresolved_effects"]

        def require_safe_reset():
            if refresh_unresolved_effects():
                raise TraceError("non-restorable or unknown effects: automatic reset disabled")

        parent_segment = rec.segment_id
        rec.segment_id = segment
        try:
            if rec.budget:
                rec.budget.check()
            if not notice.faulty_artifact_ids:
                raise TraceError("recovery requires a supplied fault identity")
            for ref in notice.faulty_artifact_ids:
                if rec.artifact(ref)["attempt_id"] != attempt:
                    raise TraceError("fault belongs to another attempt")
            require_safe_reset()
            try:
                if requested_strategy == "full_restart":
                    raise TraceError("full restart requested")
                self._recover_checkpoints(notice, attempt, result, rid, require_safe_reset)
            except Exception as exc:
                from .protocol import BudgetExceeded
                if isinstance(exc, BudgetExceeded):
                    raise
                if requested_strategy == "selective" and self.fallback_policy != "full_restart":
                    raise
                require_safe_reset()
                if rec.budget:
                    rec.budget.check()
                result["fallback_reason"] = str(exc) if requested_strategy == "selective" else None
                rec.log("recovery_fallback", attempt, recovery_id=rid, data={"reason": result["fallback_reason"]})
                fresh = self.factory(rec)
                if fresh is w or fresh.environment is w.environment or fingerprint(fresh.environment.snapshot()) != original_initial or fresh.config_hash != w.config_hash:
                    raise TraceError("fallback must create fresh workflow with matching configuration and initial environment")
                w = self.workflow = fresh
                w.injection_enabled = False
                attempt = new_id("attempt")
                previous_segment = rec.segment_id
                rec.segment_id = new_id("segment")
                rec.log("segment_started", attempt, recovery_id=rid, data={"parent_segment_id": previous_segment, "fresh_environment": True})
                rec.log("attempt_started", attempt, recovery_id=rid)
                rec.log("fault_disabled", attempt, recovery_id=rid)
                result["executed_strategy"] = "full_restart"
                result["reused_artifact_ids"] = []
                result["retained_artifact_ids"] = []
                result["rerun_node_ids"] = [n.name for n in w.nodes]
                w.run(attempt)
            if rec.budget:
                rec.budget.check()
            ev = self.evaluate(w, attempt)
            rec.save_evaluation(ev)
            result.update(completed=True, task_success=ev.task_success)
        except Exception as exc:
            result["error"] = f"{type(exc).__name__}: {exc}"
            rec.log("recovery_error", attempt, recovery_id=rid, data={"error": result["error"]})
        refresh_unresolved_effects()
        if result["completed"]:
            rec.log("active_artifacts", attempt, recovery_id=rid,
                    data={"artifact_map": detached(w.state["artifact_map"])})
        result["attempt_id"] = attempt
        rec.log("recovery_finished", attempt, recovery_id=rid,
                duration_ms=(perf_counter()-started)*1000, data=result)
        return result
