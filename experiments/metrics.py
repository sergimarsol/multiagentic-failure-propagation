"""Derive summaries from traces, never infer semantic validity from dependency edges."""

from __future__ import annotations

import json
from pathlib import Path

from .models import Artifact, Evaluation, Event, SCHEMA_VERSION
from .recorder import TraceError, atomic_json, read_jsonl


def _cost(events):
    starts = [e for e in events if e["event_type"] in {"model_started", "tool_started"}]
    ends = {e["step_id"]: e for e in events if e["event_type"] in {"model_finished", "tool_finished"}}
    models = [e for e in starts if e["event_type"] == "model_started"]
    usage = {}
    for key in ("input_tokens", "output_tokens"):
        values = [ends.get(e["step_id"], {}).get("data", {}).get(key) for e in models]
        known = sum(v for v in values if v is not None)
        usage[key] = known if all(v is not None for v in values) else None
        usage[f"known_{key}"] = known
        usage[f"calls_missing_{key}"] = sum(v is None for v in values)
    missing = sum(any(ends.get(e["step_id"], {}).get("data", {}).get(k) is None
                      for k in ("input_tokens", "output_tokens")) for e in models)
    return {"model_calls": len(models), "tool_calls": len(starts) - len(models),
            "incomplete_calls": sum(e["step_id"] not in ends for e in starts),
            "failed_calls": sum(e["data"].get("status") == "error" for e in ends.values()),
            "calls_missing_usage": missing, "usage_complete": missing == 0,
            "known_invocation_duration_ms": sum(e["duration_ms"] for e in ends.values()
                                                if e["duration_ms"] is not None), **usage}


def summarize_run(directory, *, write=True):
    directory = Path(directory)
    manifest = json.loads((directory / "manifest.json").read_text())
    streams, problems, ids = {}, [], set()
    for stream, key, cls in (("events", "event_id", Event), ("artifacts", "artifact_id", Artifact),
                             ("evaluations", "evaluation_id", Evaluation)):
        rows, issues = read_jsonl(directory / f"{stream}.jsonl", tolerate_tail=True)
        problems.extend(issues)
        for row in rows:
            try:
                cls(**row)
                if row["run_id"] != manifest["run_id"] or row[key] in ids:
                    raise ValueError("foreign run/duplicate ID")
                ids.add(row[key])
            except (ValueError, TypeError, KeyError) as exc:
                raise TraceError(f"invalid {stream} record") from exc
        streams[stream] = rows
    events, evaluations = streams["events"], streams["evaluations"]
    active, seen_steps = {}, set()
    for i, e in enumerate(events, 1):
        if e["event_seq"] != i:
            raise TraceError("invalid event sequence")
        typ, step = e["event_type"], e["step_id"]
        if typ in {"model_started", "tool_started"}:
            if not step or step in seen_steps:
                raise TraceError("duplicate/missing step")
            seen_steps.add(step)
            active[step] = (typ.split("_")[0], e["attempt_id"])
        if typ in {"model_finished", "tool_finished"}:
            if active.pop(step, None) != (typ.split("_")[0], e["attempt_id"]):
                raise TraceError("unmatched call end")
            for key in ("input_tokens", "output_tokens"):
                n = e["data"].get(key)
                if n is not None and (type(n) is not int or n < 0):
                    raise TraceError("invalid token measurement")
    attempts = list(dict.fromkeys(e["attempt_id"] for e in events if e["event_type"] == "attempt_started"))
    attempt_evals = {e["attempt_id"]: e for e in evaluations if e["scope"] == "attempt"}
    histories = [e for e in evaluations if e["scope"] == "history"]
    history = histories[-1] if histories else None
    recoveries = [e for e in events if e["event_type"] == "recovery_started"]
    recovery_ends = [e for e in events if e["event_type"] == "recovery_finished"]
    restarted = {e["data"]["new_attempt_id"] for e in recoveries}
    recovery_events = [e for e in events if e["attempt_id"] in restarted]
    nodes = [e for e in events if e["event_type"] == "node_started"]
    initial_nodes = {e["node_id"] for e in nodes if e["attempt_id"] not in restarted}
    repeated = [e for e in nodes if e["attempt_id"] in restarted and e["node_id"] in initial_nodes]
    validity = {}
    for ev in evaluations:
        validity.update(ev["work_validity"])
    invalid_ids = {k for k, v in validity.items() if v == "invalid"}
    valid_old_nodes = {e["node_id"] for e in nodes if e["attempt_id"] not in restarted
                       and validity.get(e["node_execution_id"]) == "valid"}
    judged_nodes = sum(validity.get(e["node_execution_id"]) in {"valid", "invalid"} for e in nodes)
    validity_complete = bool(nodes) and judged_nodes == len(nodes)
    old_nodes = [e for e in nodes if e["attempt_id"] not in restarted]
    old_validity_complete = bool(old_nodes) and all(
        validity.get(e["node_execution_id"]) in {"valid", "invalid"} for e in old_nodes)
    propagation = [{**p, "attempt_id": ev["attempt_id"], "evaluation_id": ev["evaluation_id"]}
                   for ev in evaluations for p in ev["propagation"]]
    affected = {p["node_id"] for p in propagation if p.get("node_id") and p["label"] in
                {"propagated_to_plan", "attempted_incorrect_action", "executed_incorrect_action"}}
    injection = [e for e in events if e["event_type"] == "fault_injected"]
    exposure = [e for e in events if e["event_type"] == "fault_exposure"]
    finished = [e for e in events if e["event_type"] == "run_finished"]
    def safety(key):
        return len(set(history[key])) if history and history[key] is not None else None
    summary = {
        "schema_version": SCHEMA_VERSION, "run_id": manifest["run_id"],
        "synthetic": manifest.get("synthetic", False), "condition": manifest.get("condition"),
        "initial_agentdojo_utility": attempt_evals.get(attempts[0], {}).get("agentdojo_utility") if attempts else None,
        "initial_strict_success": attempt_evals.get(attempts[0], {}).get("strict_success") if attempts else None,
        "final_agentdojo_utility": attempt_evals.get(attempts[-1], {}).get("agentdojo_utility") if attempts else None,
        "final_strict_success": attempt_evals.get(attempts[-1], {}).get("strict_success") if attempts else None,
        "permission_violations": history.get("permission_violations") if history else None,
        "cross_attempt_repeats": history.get("details", {}).get("cross_attempt_repeats", {"status": "not_evaluated", "actions": None}) if history else {"status": "not_evaluated", "actions": None},
        "initial_task_success": attempt_evals.get(attempts[0], {}).get("task_success") if attempts else None,
        "final_task_success": attempt_evals.get(attempts[-1], {}).get("task_success") if attempts else None,
        "injection_triggered": any(e["data"].get("triggered") for e in injection) if injection else None,
        "downstream_exposed": any(e["data"].get("delivered") for e in exposure) if exposure else None,
        "propagation": propagation,
        "observed_affected_nodes": len(affected) if propagation else None,
        "unauthorized_attempts": safety("unauthorized_attempt_ids"),
        "unauthorized_executions": safety("unauthorized_execution_ids"),
        "recovery_attempted": bool(recoveries),
        "recovery_completed": recovery_ends[-1]["data"]["completed"] if recovery_ends else None,
        "recovery_task_success": recovery_ends[-1]["data"]["task_success"] if recovery_ends else None,
        "recovery_latency_ms": sum(e["duration_ms"] for e in recovery_ends) if recovery_ends else None,
        "recomputed_nodes": len({e["node_id"] for e in repeated}),
        "recomputed_node_executions": len(repeated),
        "total_latency_ms": finished[-1]["duration_ms"] if finished else None,
        "total_cost": _cost(events), "recovery_added_cost": _cost(recovery_events),
        "invalid_path_cost": _cost([e for e in events if e["node_execution_id"] in invalid_ids]) if validity_complete else None,
        "known_invalid_path_cost": _cost([e for e in events if e["node_execution_id"] in invalid_ids]) if judged_nodes else None,
        "repeated_previously_valid_nodes": len({e["node_id"] for e in repeated} & valid_old_nodes) if old_validity_complete else None,
        "repeated_previously_valid_cost": _cost([e for e in recovery_events
            if e["node_id"] in valid_old_nodes]) if old_validity_complete else None,
        "work_validity_coverage": {"judged": judged_nodes, "total": len(nodes), "complete": validity_complete},
        "recording_overhead_ms": finished[-1]["data"].get("recording_overhead_ms") if finished else None,
        "checkpoint_capture_ms": sum(e["duration_ms"] for e in events if e["event_type"] == "checkpoint_created"),
        "checkpoint_restore_ms": sum(e["duration_ms"] for e in events if e["event_type"] in {"checkpoint_restored", "checkpoint_restore_failed"}),
        "checkpoint_bytes": sum(e["data"]["bytes"] for e in events if e["event_type"] == "checkpoint_created"),
        "attempts": [{"attempt_id": a,
                      "agentdojo_utility": attempt_evals.get(a, {}).get("agentdojo_utility"),
                      "strict_success": attempt_evals.get(a, {}).get("strict_success"),
                      "permission_violations": attempt_evals.get(a, {}).get("permission_violations"), "task_success": attempt_evals.get(a, {}).get("task_success"),
                      "unauthorized_attempts": len(set(attempt_evals[a]["unauthorized_attempt_ids"]))
                          if a in attempt_evals and attempt_evals[a]["unauthorized_attempt_ids"] is not None else None,
                      "unauthorized_executions": len(set(attempt_evals[a]["unauthorized_execution_ids"]))
                          if a in attempt_evals and attempt_evals[a]["unauthorized_execution_ids"] is not None else None,
                      "latency_ms": next((e["duration_ms"] for e in reversed(events)
                          if e["attempt_id"] == a and e["event_type"] == "attempt_finished"), None),
                      "cost": _cost([e for e in events if e["attempt_id"] == a])} for a in attempts],
        "trace_issues": problems,
        "trace_complete": bool(finished) and not active and not problems,
    }
    if recoveries and (not recovery_ends or not recovery_ends[-1]["data"]["completed"]):
        summary["final_task_success"] = None
        summary["final_agentdojo_utility"] = None
        summary["final_strict_success"] = None
    if write:
        atomic_json(directory / "summary.json", summary)
    return summary
