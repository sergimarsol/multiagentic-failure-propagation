"""Grouping evidence for A/C; deliberately contains no Banking action matching."""
from collections import defaultdict


def group_actions(events):
    """Keep actions separated by attempt and recovery segment for external scoring.

    Invocation/state_changed are observations, not authorization judgments. Legacy
    traces with no segment retain a null segment; do not infer a recovery boundary.
    """
    starts = {e["step_id"]: e for e in events if e["event_type"] == "tool_started"}
    groups = defaultdict(list)
    for event in events:
        if event["event_type"] != "tool_finished":
            continue
        start = starts.get(event["step_id"], {})
        groups[(event["attempt_id"], event.get("execution_segment_id"))].append({
            "step_id": event["step_id"], "event_id": event["event_id"],
            "tool": start.get("data", {}).get("tool"), "args": start.get("data", {}).get("args"),
            "result": event["data"], "node_execution_id": event.get("node_execution_id")})
    return [{"attempt_id": a, "execution_segment_id": segment, "actions": actions}
            for (a, segment), actions in groups.items()]
