"""B-to-C evidence export. Semantic adoption and verifier correctness belong to C.

Keep four outcome categories separate from five observation stages and from
injection validity. No amount-string search is used as a propagation classifier.
"""

import json
from pathlib import Path

OUTCOMES = ("not_exposed", "exposed_not_adopted", "propagated", "state_consequence")


def rows(directory, name):
    return [json.loads(line) for line in (Path(directory) / name).read_text().splitlines()]


def clean_reference(directory, task_id):
    """Validate an explicit clean comparator before any model requests are made."""
    path = Path(directory).resolve()
    manifest = json.loads((path / "manifest.json").read_text())
    if manifest["condition"] != "clean" or manifest["task_id"] != task_id:
        raise ValueError("--matched-clean must reference a clean run of the same task")
    summary = json.loads((path / "summary.json").read_text())
    if not summary["trace_complete"]:
        raise ValueError("--matched-clean has an incomplete trace")
    events = rows(path, "events.jsonl")
    configs = [e["data"] for e in events if e["event_type"] == "workflow_config"]
    if len(configs) != 1:
        raise ValueError("--matched-clean must have one recorded B configuration")
    return {"directory": str(path), "run_id": manifest["run_id"], "config": configs[0]}


def export_cases(directory, *, matched_clean=None):
    directory = Path(directory)
    manifest = json.loads((directory / "manifest.json").read_text())
    events = rows(directory, "events.jsonl")
    artifacts = {a["artifact_id"]: a for a in rows(directory, "artifacts.jsonl")}
    evaluations = rows(directory, "evaluations.jsonl")
    baseline = clean_reference(matched_clean, manifest["task_id"]) if matched_clean else None
    configs = [e["data"] for e in events if e["event_type"] == "workflow_config"]
    if baseline:
        baseline["configuration_matches"] = bool(configs) and all(c == baseline["config"] for c in configs)
        # A comparator may itself fail. Preserve its scores and trace, never call it successful by fiat.
        baseline["summary"] = json.loads((Path(baseline["directory"]) / "summary.json").read_text())
        bundle = Path(baseline["directory"]) / "fault_cases.json"
        baseline["evidence_file"] = str(bundle) if bundle.exists() else None
        baseline["trace_files"] = {name: str(Path(baseline["directory"]) / name)
                                   for name in ("events.jsonl", "artifacts.jsonl", "evaluations.jsonl")}
    cases = []
    for start in (e for e in events if e["event_type"] == "attempt_started"):
        attempt = start["attempt_id"]
        evs = [e for e in events if e["attempt_id"] == attempt]
        observations = [e for e in evs if e["event_type"] == "injection_observation"]
        finals = [e["data"] for e in evs if e["event_type"] == "injection_attempt_finished"]
        applied = any(e["data"].get("applied") is True for e in observations)
        candidate_ids = {e["data"]["candidate_artifact_id"] for e in observations if e["data"].get("applied")}
        starts = [e for e in evs if e["event_type"] == "model_started"]
        ends = {e["step_id"]: e for e in evs if e["event_type"] == "model_finished"}
        business = [e for e in starts if e["data"]["role"] in {"planner", "executor"}]
        attempted_delivery = any(candidate_ids.intersection(e["data"]["input_artifact_ids"]) for e in business)
        acknowledged = any(e["event_type"] == "fault_exposure" and e["data"].get("delivered") for e in evs)
        exposed = (True if acknowledged else None if attempted_delivery else False) if applied else None
        def model_record(e):
            end = ends.get(e["step_id"])
            return {"started_event_id": e["event_id"], "step_id": e["step_id"],
                    "node_execution_id": e["node_execution_id"], "input": e["data"],
                    "finished_event_id": end["event_id"] if end else None,
                    "result": end["data"] if end else None}
        checks = []
        for e in evs:
            if e["event_type"] != "verification_decision":
                continue
            data = e["data"]
            checks.append({"event_id": e["event_id"], **data,
                "candidate": artifacts[data["candidate_artifact_id"]]["content"],
                "verdict": artifacts[data["verdict_artifact_id"]]["content"],
                "correctness": {"status": "pending_review", "candidate_valid": None,
                                "false_accept": None, "false_reject": None, "evidence_ids": []}})
        tool_starts = {e["step_id"]: e for e in evs if e["event_type"] == "tool_started"}
        audit = [a for e in evs if e["event_type"] == "environment_finalized" for a in e["data"]["audit"]]
        cases.append({"attempt_id": attempt,
            "injection": finals[-1] if finals else {"applied": None, "reason": "status_unavailable"},
            "boundaries": [{**e["data"], "event_id": e["event_id"],
                "original": artifacts[e["data"]["original_artifact_id"]]["content"],
                "candidate": artifacts[e["data"]["candidate_artifact_id"]]["content"]} for e in observations],
            "model_calls": [model_record(e) for e in starts],
            "exposure_events": [e for e in evs if e["event_type"] in {"fault_exposure", "verifier_exposure"}],
            "errors": [e for e in evs if e["event_type"] in {"workflow_failed", "attempt_failed", "injection_contract_error"}],
            "verification": checks,
            "tool_audit": [{"audit": a, "model_response_artifact_ids":
                tool_starts.get(a["step_id"], {}).get("data", {}).get("input_artifact_ids", [])} for a in audit],
            "evaluations": [e for e in evaluations if e["scope"] == "attempt" and e["attempt_id"] == attempt],
            "classification": {
                "status": "pending_review" if applied else "not_applicable" if finals else "status_unavailable",
                "outcome": None, "evidence_ids": [],
                "observation_stages": {"consumer_exposed": exposed, "adopted_in_output": None,
                    "tool_attempted": None, "tool_executed": None, "state_diff": None}}})
    result = {"schema": "b-c-evidence-v1", "run_id": manifest["run_id"], "task_id": manifest["task_id"],
              "condition": manifest["condition"], "outcome_categories": list(OUTCOMES),
              "matched_clean": baseline, "attempts": cases,
              "note": "Stage flags beyond receipt require semantic relevance to this fault. "
                      "Use the historical audit, not just final state. An unapplied fault has no propagation category. "
                      "Verifier correctness is judged independently, never inferred from its decision."}
    (directory / "fault_cases.json").write_text(json.dumps(result, indent=2) + "\n")
    return result
