"""Where each node execution's work ended up after recovery, by judged validity.

Disposition comes from the trace alone; validity comes only from recorded or offline
judgments and stays "unknown" otherwise. Discarded work is not assumed incorrect.
"""
from .metrics import _cost

DISPOSITIONS = ("kept", "retained", "discarded", "recovery_effective", "recovery_abandoned", "unresolved")
FIELDS = ("model_calls", "tool_calls", "known_input_tokens", "known_output_tokens",
          "calls_missing_usage", "known_invocation_duration_ms")


def _final_executions(events, artifacts, recovery_ends):
    """Node executions whose output is on the final effective path; None if recovery did not complete."""
    if not recovery_ends or not recovery_ends[-1]["data"].get("completed"):
        return None
    active = [e for e in events if e["event_type"] == "active_artifacts"]
    if active:  # Phase 2: the coordinator records the final artifact per stage.
        producers = {a["artifact_id"]: a["producer_execution_id"] for a in artifacts}
        return {producers[ref] for ref in active[-1]["data"]["artifact_map"].values() if ref in producers}
    final_attempt = recovery_ends[-1]["attempt_id"]  # Phase 1: the restarted attempt.
    return {e["node_execution_id"] for e in events
            if e["event_type"] == "node_started" and e["attempt_id"] == final_attempt}


def work_breakdown(events, artifacts, validity):
    """Per-execution rows and cost totals for every disposition/validity combination.

    kept: no recovery ran. retained: initial work still on the final path. discarded:
    initial work replaced by recovery. recovery_effective / recovery_abandoned: reruns on,
    or off, the final path. unresolved: recovery started but did not complete.
    A rerun "repeats" the discarded initial execution of the same stage; whether that
    repeated valid work depends on the initial execution's judgment.
    """
    nodes = [e for e in events if e["event_type"] == "node_started"]
    recoveries = [e for e in events if e["event_type"] == "recovery_started"]
    recovery_ends = [e for e in events if e["event_type"] == "recovery_finished"]
    boundary = recoveries[0]["event_seq"] if recoveries else float("inf")
    final = _final_executions(events, artifacts, recovery_ends)
    by_execution = {}
    for e in events:
        if e.get("node_execution_id"):
            by_execution.setdefault(e["node_execution_id"], []).append(e)
    rows = []
    for node in nodes:
        nid = node["node_execution_id"]
        recovery = node["event_seq"] > boundary
        if not recoveries:
            disposition = "kept"
        elif final is None:
            disposition = "unresolved"
        elif recovery:
            disposition = "recovery_effective" if nid in final else "recovery_abandoned"
        else:
            disposition = "retained" if nid in final else "discarded"
        cost = _cost(by_execution.get(nid, []))
        rows.append({"node_execution_id": nid, "node_id": node["node_id"], "attempt_id": node["attempt_id"],
                     "execution_segment_id": node.get("execution_segment_id"),
                     "phase": "recovery" if recovery else "initial", "disposition": disposition,
                     "validity": validity.get(nid, "unknown"), "repeats": None,
                     **{k: cost[k] for k in FIELDS}})
    discarded = {}
    for row in rows:
        if row["disposition"] == "discarded":
            discarded.setdefault(row["node_id"], []).append(row["validity"])
    for row in rows:
        if row["phase"] == "recovery" and row["node_id"] in discarded:
            judged = set(discarded[row["node_id"]])
            # Several discarded executions of one stage (regenerations): only unanimous judgments count.
            row["repeats"] = judged.pop() if len(judged) == 1 else "unknown"

    def total(selected):
        return {"executions": len(selected), **{k: sum(r[k] for r in selected) for k in FIELDS}}

    totals = {}
    for disposition in DISPOSITIONS:
        for label in ("valid", "invalid", "unknown"):
            totals[f"{disposition}_{label}"] = total(
                [r for r in rows if r["disposition"] == disposition and r["validity"] == label])
    for label in ("valid", "invalid", "unknown"):
        totals[f"repeated_{label}"] = total([r for r in rows if r["repeats"] == label])
    totals["invalid"] = total([r for r in rows if r["validity"] == "invalid"])
    totals["recovery_added"] = total([r for r in rows if r["phase"] == "recovery"])
    return {"executions": rows, "totals": totals,
            "validity_coverage": {"judged": sum(r["validity"] != "unknown" for r in rows), "total": len(rows)},
            "final_path_known": not recoveries or final is not None}
