"""Opt-in adapters to A; preserve A's authorization and historical scoring policy."""

from pathlib import Path

from .models import Evaluation, fingerprint, new_id
from .recorder import TraceError


class BankingAdapter:
    def __init__(self, env, recorder, *, segmented_audit=False):
        self.env, self.recorder = env, recorder
        self.segmented_audit = segmented_audit
        self._effective_call_ids = []
        if segmented_audit and env.audit_log():
            raise TraceError("segmented adapter requires a fresh environment audit")
        if segmented_audit and recorder.segment_id is None:
            recorder.segment_id = new_id("segment")

    def snapshot(self):
        state = self.env.state_dict()
        if self.segmented_audit:
            state["_d_audit_path"] = list(self._effective_call_ids)
        return state

    def restore(self, snapshot):
        from env.state import env_from_state
        state = dict(snapshot)
        path = state.pop("_d_audit_path", None)
        if self.segmented_audit:
            known = {e["call_id"] for e in self.env.audit_log()}
            if (not isinstance(path, list) or any(not isinstance(x, str) for x in path)
                    or len(path) != len(set(path)) or not set(path) <= known):
                raise TraceError("checkpoint lacks a valid effective audit path")
        elif path is not None:
            raise TraceError("segmented checkpoint requires segmented_audit=True")
        self.env.restore(env_from_state(state))
        if self.segmented_audit:
            self._effective_call_ids = list(path)
            # Manual restores also start a new segment; the coordinator may assign its own ID.
            self.recorder.segment_id = new_id("segment")

    def audit_key(self, attempt_id, *, segment_id=None):
        # A's run_id is an execution filter. D's top-level run_id remains unchanged.
        base = f"{self.recorder.run_id}:{attempt_id}"
        if self.segmented_audit:
            segment = segment_id or self.recorder.segment_id
            if segment is None:
                raise TraceError("segmented audit requires an execution segment")
            return f"{base}:{segment}"
        return base

    def call_tool(self, role, name, args, *, attempt_id, node_id, node_execution_id, inputs=()):
        for ref in inputs:
            self.recorder.artifact(ref)
        step = new_id("step")
        before = self.env.state_dict()
        with self.recorder.call("tool", attempt_id, step_id=step, node_id=node_id,
            node_execution_id=node_execution_id, role=role, tool=name, args=args,
            input_artifact_ids=list(inputs)) as end:
            result = self.env.call_tool(role, name, args, run_id=self.audit_key(attempt_id), step_id=step)
            if self.segmented_audit:
                self._effective_call_ids.append(result.call_id)
            after = self.env.state_dict()
            # Conservatively depend on every state resource for any invoked function.
            # Hashes are content references, not a claim of fine-grained read tracking.
            reads = [{"resource_key": k, "version": fingerprint(v)} for k, v in before.items()] if result.executed else []
            writes = [{"resource_key": k, "before_version": fingerprint(before[k]),
                       "after_version": fingerprint(after[k])} for k in before if before[k] != after[k]]
            end.update(status="error" if result.error else "success", error=result.error,
                allowed=result.allowed, invoked=result.executed, state_changed=bool(writes),
                result=result.result, result_text=result.result_text, audit_call_id=result.call_id,
                audit_run_id=self.audit_key(attempt_id),
                state_reads=reads, state_writes=writes, env_epoch=self.env.env_version,
                effect_ids=[result.call_id] if writes else [], effect_kind="simulated_restorable")
        self.recorder.log("dependencies_recorded", attempt_id, node_id=node_id,
            node_execution_id=node_execution_id, step_id=step,
            data={"input_artifact_ids": list(inputs), "control_artifact_ids": [],
                  "state_reads": reads, "state_writes": writes, "granularity": "whole_environment"})
        return result


class BankingEvaluator:
    def __init__(self, adapter):
        self.adapter = adapter

    def evaluate(self, attempt_id, scope, *, audit_view=None):
        from eval.scoring import evaluate
        adapter, env = self.adapter, self.adapter.env
        if scope not in {"attempt", "history"}:
            raise ValueError("Banking adapter evaluates attempts/history, not propagation")
        prefix = adapter.recorder.run_id + ":"
        entries = [e for e in env.audit_log() if (e.get("run_id") or "").startswith(prefix)]
        view = audit_view or ("effective" if adapter.segmented_audit and scope == "attempt" else "history")
        if view not in {"effective", "history"} or (scope == "history" and view != "history"):
            raise ValueError("invalid audit view for evaluation scope")
        if view == "effective" and not adapter.segmented_audit:
            raise ValueError("effective view requires segmented_audit=True")
        base = f"{adapter.recorder.run_id}:{attempt_id}"
        if scope == "attempt":
            entries = [e for e in entries if e["run_id"] == base or
                       (adapter.segmented_audit and e["run_id"].startswith(base + ":"))]
        if view == "effective":
            active = set(adapter._effective_call_ids)
            entries = [e for e in entries if e["call_id"] in active]
        # A treats run_id as the attempt boundary. Segment provenance stays in raw
        # audit and details; normalize copies so same-attempt duplicates stay duplicates.
        source_keys = {e["call_id"]: e["run_id"] for e in entries}
        scored = [{**e, "run_id": e["run_id"].rsplit(":", 1)[0]}
                  for e in entries] if adapter.segmented_audit else entries
        result = evaluate(env.task_id, env.initial_state, env.snapshot(), scored)
        return Evaluation(run_id=adapter.recorder.run_id, attempt_id=attempt_id,
            evaluation_id=new_id("evaluation"), scope=scope, evaluator="A.strict_scoring",
            task_success=result.strict_success if not result.warnings else None,
            agentdojo_utility=None if result.warnings else result.agentdojo_utility,
            strict_success=None if result.warnings else result.strict_success,
            permission_violations=result.permission_violations,
            unauthorized_attempt_ids=[e["call_id"] for e in result.unauthorized_attempted],
            unauthorized_execution_ids=[e["call_id"] for e in result.unauthorized_executed],
            evidence_ids=[e["call_id"] for e in entries], details={**result.to_dict(), "audit_view": view,
                "audit_source_keys": source_keys})


def cross_attempt_adapter(task_id):
    """Adapt A's optional helper; do not reimplement action matching in D.

    Calling this factory raises ImportError on versions without A's helper.
    Selecting an approved helper revision is the integration owner's decision.
    """
    from eval.scoring import cross_attempt_repeats

    def evaluate_repeats(attempts):
        results = cross_attempt_repeats(task_id, [a["audit"] for a in attempts])
        return [{**row, "attempt_id": attempts[row["attempt_index"]]["attempt_id"]} for row in results]
    return evaluate_repeats


C_PAYLOADS = (Path(__file__).resolve().parents[1] / "faults" / "payloads.yaml").resolve()


def _is_c_payload(ref):
    return isinstance(ref, str) and bool(ref) and (C_PAYLOADS.parent.parent / ref).resolve() == C_PAYLOADS


def attack_report(manifest, events):
    """A's attack success per attempt, from the recorded final state and audit of each attempt.

    Only runs injected from C's payload file are judged: their scenario_id is a payload id with a
    goal in tasks/banking_tasks.yaml. Other runs (clean, B fixtures) are not_applicable, never False.
    """
    config = manifest.get("experiment") or {}
    fault = config.get("fault") or {}
    if manifest.get("condition") in {"clean", "clean_verify"} or not _is_c_payload(fault.get("payload_ref")):
        return {"status": "not_applicable", "goal": None, "success": None, "attempts": []}
    from env.tasks import get_task_config
    from eval.scoring import attack_outcome
    task_id, scenario = manifest["task_id"], config.get("scenario_id")
    try:
        goal = get_task_config(task_id).attack_goal(scenario).name
    except KeyError:
        return {"status": "error", "error": f"no attack goal for {task_id}/{scenario}", "goal": None,
                "success": None, "attempts": []}
    finals = [e for e in events if e["event_type"] == "environment_finalized"]
    started = [e["attempt_id"] for e in events if e["event_type"] == "attempt_started"]
    rows = []
    for e in finals:
        try:
            o = attack_outcome(task_id, scenario, e["data"]["audit"], e["data"]["final_state"])
        except Exception as exc:  # a malformed trace is unknown, never "attack failed"
            return {"status": "error", "error": repr(exc), "goal": goal, "success": None, "attempts": rows}
        rows.append({"attempt_id": e["attempt_id"], "attempted": o["attempted"], "executed": o["executed"],
                     "in_final_state": o["in_final_state"],
                     "executed_call_ids": [c["call_id"] for c in o["executed_calls"]], "warnings": o["warnings"]})
    complete = bool(rows) and len(rows) == len(started) and not any(r["warnings"] for r in rows)
    executed = any(r["executed"] for r in rows)
    # An executed goal is a fact even if a later attempt is missing; "not reached" needs every attempt judged.
    return {"status": "evaluated" if complete else "incomplete", "goal": goal,
            "success": True if executed else (False if complete else None), "attempts": rows}
