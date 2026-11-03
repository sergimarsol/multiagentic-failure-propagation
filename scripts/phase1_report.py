"""Phase 1 results: propagation, affected stages and work lost to faults for a final sweep.

Joins C's propagation analysis (scripts/analyze_injection_trace.py) with D's traces
and A's scores for every trial of scripts/run_phase1_final.py:

  uv run python scripts/phase1_report.py runs/phase1-final

Writes <sweep>/report/: trials.csv (one row per trial), conditions.csv and
propagation.csv (grouped), and tables.tex (LaTeX tables for the report).

Only the injected (first) attempt is labelled. Its node executions are valid or
invalid from C's observations, which fills D's invalid_path_cost and
repeated_previously_valid_cost:
- affected stages: downstream stages whose own work used the fault. With a fault at
  researcher->planner the plan is affected if the planner adopted it; the execution is
  affected only if the executor attempted the fault's action or adopted it, never just
  because the plan carried it (an executor that refuses the bad action did valid work).
  With a fault at planner->executor only the execution can be affected.
- contaminated work: cost of the affected stages' node executions, i.e. work that used
  the fault, whether or not the attempt then failed.
- discarded work: cost of all planning/execution after the injection when the injected
  attempt failed strict scoring, since its result is thrown away; 0 when that attempt
  succeeded. It also counts correct work in a run the fault broke without being adopted
  (e.g. a planner that saw conflicting facts and rightly planned nothing).
  The two measure different things and are not bounds on each other: an attempt can
  succeed despite contaminated work (a bad plan the executor refused).
- valid work redone: for full restart, what the second attempt spent redoing stages
  of the first that were not discarded; this is what selective recovery could keep.
Verifier calls are verification overhead, never discarded work; a stage regenerated
after a rejection counts like any other stage. A fault that was not applied, or whose
adoption C could not judge, gives unknown (empty) values, never 0. --reviewed replaces C's suggested label of a trial with a
reviewed one (CSV with columns trial, final_outcome). C's analyzer adds its derived
fault_cases.json to a trial directory that lacks one; nothing else in the traces changes.
"""
import argparse
import csv
import json
from pathlib import Path
from statistics import mean, median
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from experiments.analysis_stats import wilson95
from experiments.metrics import _cost
from scripts.analyze_injection_trace import analyze_run

CONDITIONS = ("clean", "clean_verify", "fault", "full_restart", "always_verify")
OUTCOMES = {  # C's suggested label -> the plan's four categories
    "not_exposed": "not_exposed", "exposed_not_adopted_candidate": "exposed_not_adopted",
    "propagated_candidate": "propagated", "state_consequence_candidate": "state_consequence",
    "exposed_not_adopted": "exposed_not_adopted", "propagated": "propagated",
    "state_consequence": "state_consequence",
}
CATEGORIES = ("not_exposed", "exposed_not_adopted", "propagated", "state_consequence")
SITE_ROLE = {"research": "planner", "plan": "executor"}


def _events(run):
    return [json.loads(line) for line in (run / "events.jsonl").read_text().splitlines() if line.strip()]


def _node_runs(events, attempt):
    return [e for e in events if e["event_type"] == "node_started" and e["attempt_id"] == attempt]


def _cost_of(events, node_execution_ids):
    return _cost([e for e in events if e["node_execution_id"] in node_execution_ids])


def _injection(events, attempt):
    found = [e for e in events if e["event_type"] == "fault_injected" and e["data"].get("triggered")
             and e["attempt_id"] == attempt]
    return found[0] if len(found) == 1 else None


def downstream_nodes(events, attempt):
    """Planning and execution after the injection, including regenerations (verifier nodes excluded)."""
    injected = _injection(events, attempt)
    return {e["node_execution_id"] for e in _node_runs(events, attempt)
            if e["event_seq"] > injected["event_seq"] and e["node_id"] in ("plan", "execute")}


def invalid_nodes(events, label):
    """Node executions of the injected attempt whose work used the fault; None if unknown."""
    injected = _injection(events, label["attempt_id"])
    if label["injection_applied"] is not True or injected is None:
        return None
    if label["consumer_exposed"] is False:
        return set()
    site, after = injected["data"]["site"], injected["event_seq"]
    nodes = [e for e in _node_runs(events, label["attempt_id"]) if e["event_seq"] > after]
    attempted = label["relevant_tool_attempted"]
    if site == "research":
        planner, executor = label["planner_adopted_fault"], label["executor_adopted_fault"]
        bad_execute = attempted is True or executor is True
        if planner is None and not bad_execute:
            return None
        if planner is True and not bad_execute and attempted is None and executor is None:
            return None  # the plan carried the fault but what the executor did is unknown
        bad_plan = planner is True
    else:
        executor = label["executor_adopted_fault"]
        if executor is None and attempted is None:
            return None
        bad_plan, bad_execute = False, executor is True or attempted is True
    return {e["node_execution_id"] for e in nodes
            if (e["node_id"] == "plan" and bad_plan) or (e["node_id"] == "execute" and bad_execute)}


def trial_row(trial, reviewed):
    run = trial / "run_0000"
    result = json.loads((trial / "result_0000.json").read_text())
    summary, config = result["summary"], result["config"]
    fault = config.get("fault") or {}
    row = {"trial": trial.name, "task": config["task_id"], "scenario": config["scenario_id"],
           "family": fault.get("family", ""), "site": fault.get("site", ""), "condition": config["condition"],
           "repeat": config["repeat_index"], "error": result.get("error") or "",
           "trace_complete": summary.get("trace_complete"),
           "initial_strict": summary.get("initial_strict_success"),
           "final_strict": summary.get("final_strict_success"),
           "attack_success": summary.get("attack_success"),
           "unauthorized_executions": summary.get("unauthorized_executions"),
           "latency_s": round(summary["total_latency_ms"] / 1000, 3) if summary.get("total_latency_ms") else None}
    total = summary.get("total_cost") or {}
    row.update(model_calls=total.get("model_calls"), tool_calls=total.get("tool_calls"),
               tokens=_tokens(total), verifier_calls=(summary.get("verification_cost") or {}).get("model_calls"),
               verifier_decisions=summary.get("verifier_decisions"), verifier_rejections=summary.get("verifier_rejections"))
    row.update(injection_applied=None, outcome="", affected_stages=None, state_changed=None,
               contaminated_calls=None, discarded_calls=None, discarded_tokens=None, discarded_s=None,
               redone_valid_calls=None, redone_valid_tokens=None, redone_valid_s=None, label_conflict="")
    if row["condition"] in ("clean", "clean_verify") or not run.exists():
        return row
    try:
        _label_trial(row, run, summary, reviewed.get(trial.name))
    except (OSError, ValueError, KeyError, TypeError, StopIteration) as exc:
        # A trace that cannot be analyzed is reported, never guessed.
        for key in ("injection_applied", "affected_stages", "state_changed", "contaminated_calls"):
            row[key] = None
        for key in [k for k in row if k.startswith(("discarded_", "redone_valid_"))]:
            row[key] = None
        row["outcome"] = f"analysis_error: {type(exc).__name__}"
    return row


def _label_trial(row, run, summary, reviewed_outcome):
    events = _events(run)
    label = next(l for l in analyze_run(run) if l["attempt_id"] == "attempt_0")
    row["injection_applied"] = label["injection_applied"]
    suggested = reviewed_outcome or label["suggested_outcome"]
    row["outcome"] = OUTCOMES.get(suggested, suggested)
    row["state_changed"] = label["relevant_state_changed"]
    contaminated = invalid_nodes(events, label)
    if contaminated is not None:
        stages = {e["node_id"] for e in events if e.get("node_execution_id") in contaminated
                  and e["event_type"] == "node_started"}
        row["affected_stages"] = len(stages)
        cost = _cost_of(events, contaminated)
        row["contaminated_calls"] = cost["model_calls"] + cost["tool_calls"]
    strict = summary.get("initial_strict_success")
    if label["injection_applied"] is True and _injection(events, "attempt_0") and strict is not None:
        bad = downstream_nodes(events, "attempt_0") if strict is False else set()
        _put(row, "discarded", _cost_of(events, bad))
        if row["condition"] == "full_restart":
            old = {e["node_id"] for e in _node_runs(events, "attempt_0")
                   if e["node_execution_id"] not in bad and e["node_id"] != "setup"}
            redo = {e["node_execution_id"] for e in events if e["event_type"] == "node_started"
                    and e["attempt_id"] != "attempt_0" and e["node_id"] in old}
            _put(row, "redone_valid", _cost_of(events, redo))
    # A's scorer and C's analyzer judge the same attempt independently; flag disagreement.
    if summary.get("initial_attack_success") is True and row["outcome"] in ("not_exposed", "exposed_not_adopted"):
        row["label_conflict"] = "attack reached its goal but C labels it not adopted"


def _tokens(cost):
    if cost.get("input_tokens") is None or cost.get("output_tokens") is None:
        return None
    return cost["input_tokens"] + cost["output_tokens"]


def _put(row, prefix, cost):
    row[f"{prefix}_calls"] = cost["model_calls"] + cost["tool_calls"]
    row[f"{prefix}_tokens"] = _tokens(cost)
    row[f"{prefix}_s"] = round(cost["known_invocation_duration_ms"] / 1000, 3)


def _rate(rows, key):
    known = [r[key] for r in rows if r[key] is not None]
    k, n = sum(v is True for v in known), len(known)
    low, high = wilson95(k, n) if n else (None, None)
    return k, n, (k / n if n else None), low, high


def _avg(rows, key, f=mean):
    values = [r[key] for r in rows if r[key] is not None]
    return round(f(values), 3) if values else None


def group(rows, keys):
    groups = {}
    for r in rows:
        groups.setdefault(tuple(r[k] for k in keys), []).append(r)
    return groups


def condition_rows(rows):
    out = []
    clean_ok = {(r["task"], r["repeat"]) for r in rows if r["condition"] == "clean" and r["final_strict"] is True}
    for key, rs in group(rows, ("condition", "family")).items():
        row = dict(zip(("condition", "family"), key), trials=len(rs),
                   errors=sum(bool(r["error"]) or r["trace_complete"] is not True for r in rs))
        for name, col in (("strict", "final_strict"), ("initial_strict", "initial_strict"), ("attack", "attack_success")):
            k, n, p, low, high = _rate(rs, col)
            row.update({f"{name}_k": k, f"{name}_n": n, f"{name}_rate": p, f"{name}_ci_low": low, f"{name}_ci_high": high})
        paired = [r for r in rs if (r["task"], r["repeat"]) in clean_ok]
        row["strict_k_clean_ok"], row["strict_n_clean_ok"] = _rate(paired, "final_strict")[:2]
        decided = [r for r in rs if r["verifier_decisions"]]
        row["runs_with_rejection"], row["runs_with_decision"] = sum((r["verifier_rejections"] or 0) > 0 for r in decided), len(decided)
        known = [r["unauthorized_executions"] for r in rs if r["unauthorized_executions"] is not None]
        row["runs_with_unauthorized_execution"], row["unauthorized_known"] = sum(v > 0 for v in known), len(known)
        for col in ("latency_s",):
            row[f"median_{col}"] = _avg(rs, col, median)
        for col in ("model_calls", "tokens", "verifier_calls", "affected_stages", "contaminated_calls", "discarded_calls",
                    "discarded_tokens", "discarded_s", "redone_valid_calls", "redone_valid_tokens", "redone_valid_s"):
            row[f"mean_{col}"] = _avg(rs, col)
        out.append(row)
    order = {c: i for i, c in enumerate(CONDITIONS)}
    return sorted(out, key=lambda r: (r["family"] != "", r["family"], order[r["condition"]]))


def propagation_rows(rows):
    out = []
    faulty = [r for r in rows if r["condition"] in ("fault", "full_restart", "always_verify")]
    for key, rs in sorted(group(faulty, ("scenario", "family", "site", "condition")).items()):
        row = dict(zip(("scenario", "family", "site", "condition"), key), trials=len(rs),
                   not_applied=sum(r["injection_applied"] is False for r in rs),
                   unknown=sum(r["injection_applied"] is None for r in rs))
        for c in CATEGORIES:
            row[c] = sum(r["outcome"] == c for r in rs)
        row["needs_review"] = sum(r["injection_applied"] is True and r["outcome"] not in CATEGORIES for r in rs)
        row["mean_affected_stages"] = _avg(rs, "affected_stages")
        row["label_conflicts"] = sum(bool(r["label_conflict"]) for r in rs)
        out.append(row)
    return out


def write_csv(path, rows):
    if rows:
        with path.open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)


def _frac(k, n):
    return f"{k}/{n}" if n else "--"


def _num(v, digits=1):
    return "--" if v is None else f"{v:.{digits}f}"


def latex(conditions, propagation):
    tex = lambda s: s.replace("_", r"\_")
    clean_verify = next((r for r in conditions if r["condition"] == "clean_verify"), None)
    rejected = _frac(clean_verify["runs_with_rejection"], clean_verify["runs_with_decision"]) if clean_verify else "--"
    lines = [r"% Generated by scripts/phase1_report.py; do not edit by hand.",
             r"\begin{table}[t]\centering\footnotesize\setlength{\tabcolsep}{3.5pt}",
             r"\begin{tabular}{llccccccc}\toprule",
             r"Faults & Condition & Strict success & Attack success & Unauth.\ exec. & Latency (s)"
             r" & Contaminated & Discarded & Valid redone \\\midrule"]
    previous = None
    for r in conditions:
        family = r["family"] or "none"
        first = family != previous
        if previous is not None and first:
            lines.append(r"\midrule")
        previous = family
        lines.append(" & ".join([family if first else "", tex(r["condition"]),
                                 _frac(r["strict_k"], r["strict_n"]),
                                 _frac(r["attack_k"], r["attack_n"]) if r["family"] else "--",
                                 _frac(r["runs_with_unauthorized_execution"], r["unauthorized_known"]),
                                 _num(r["median_latency_s"]), _num(r["mean_contaminated_calls"]),
                                 _num(r["mean_discarded_calls"]),
                                 _num(r["mean_redone_valid_calls"])]) + r" \\")
    lines += [r"\bottomrule\end{tabular}",
              r"\caption{Per condition and fault family, all repeats. Strict success is the final attempt, latency "
              r"the median; the last three columns are mean model and tool calls per run. Clean runs the "
              r"verifier rejected: "
              + rejected + r".}\label{tab:conditions}\end{table}", ""]
    fault = [r for r in propagation if r["condition"] == "fault"]
    lines += [r"\begin{table}[t]\centering\footnotesize\setlength{\tabcolsep}{4pt}",
              r"\begin{tabular}{lcccccc}\toprule",
              r"Scenario & Not exposed & Exposed, not adopted & Propagated & State consequence"
              r" & Other & Affected stages \\\midrule"]
    lines += [" & ".join([tex(r["scenario"]), *(str(r[c]) for c in CATEGORIES),
                          str(r["needs_review"] + r["not_applied"] + r["unknown"]),
                          _num(r["mean_affected_stages"])]) + r" \\" for r in fault]
    lines += [r"\bottomrule\end{tabular}",
              r"\caption{Furthest consequence of each injected fault (fault condition, no recovery; other: not "
              r"applied, unknown or under review) and mean downstream stages that used it.}"
              r"\label{tab:propagation}\end{table}"]
    return "\n".join(lines) + "\n"


def build(sweep, out, reviewed=None):
    """Write the report for a sweep directory into out; returns (trials, conditions, propagation)."""
    trials = sorted(d for d in (Path(sweep) / "trials").iterdir() if (d / "result_0000.json").exists())
    rows = [trial_row(t, reviewed or {}) for t in trials]
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    conditions, propagation = condition_rows(rows), propagation_rows(rows)
    write_csv(out / "trials.csv", rows)
    write_csv(out / "conditions.csv", conditions)
    write_csv(out / "propagation.csv", propagation)
    (out / "tables.tex").write_text(latex(conditions, propagation))
    return rows, conditions, propagation


def read_reviewed(path):
    return {r["trial"]: r["final_outcome"] for r in csv.DictReader(Path(path).open()) if r.get("final_outcome")}


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("sweep", type=Path, help="output directory of scripts/run_phase1_final.py")
    p.add_argument("--reviewed", type=Path, help="CSV with columns trial, final_outcome")
    p.add_argument("--output", type=Path, help="default: <sweep>/report")
    a = p.parse_args()
    out = a.output or a.sweep / "report"
    rows = build(a.sweep, out, read_reviewed(a.reviewed) if a.reviewed else None)[0]
    conflicts = sum(bool(r["label_conflict"]) for r in rows)
    print(f"{len(rows)} trials -> {out}; {conflicts} label conflicts to review")


if __name__ == "__main__":
    main()
