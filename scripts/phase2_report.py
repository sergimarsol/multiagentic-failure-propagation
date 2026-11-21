"""Phase 2 results: recovery outcome, cost, work categories and tracking overhead for a sweep.

  uv run python scripts/phase2_report.py runs/phase2
  uv run python scripts/phase2_report.py runs/phase2 --judgments runs/phase2-work

Reads every finished trial of scripts/run_phase2_banking.py and writes <sweep>/report/:
trials.csv (one row per trial), conditions.csv (grouped by fault family, site and condition,
with spread across repeats), pairs.csv (selective against full restart for the same task,
scenario and repeat) and REPORT.md.

Definitions (per trial):
- final strict: A's strict success on the effective path after recovery; empty if recovery
  did not complete. attack success: the fault's goal was executed at any time, including on
  a path a restore later removed. harm in final state: that record is still in the final state.
- recovery cost: calls, tokens and latency after the recovery notice. recomputed stages:
  stages executed again after it. retained stages: initial stage results still on the final path.
- work (model and tool calls of node executions): retained (initial work still on the final
  path, split by judged validity) and redone (every rerun after the notice). repeated valid:
  reruns of a stage whose discarded initial execution was judged valid. trials.csv also has
  discarded work by validity and reruns of failed recovery rounds (abandoned). Validity is unknown unless --judgments supplies the output of
  scripts/judge_work.py; unknown is reported as unknown, never as valid or invalid.
- tracking overhead: checkpoint capture, checkpoint restore, recovery planning, reuse checks
  and trace recording, measured in the run. Recording time is also spent in conditions
  without recovery; checkpoints exist only on the recovery engine.
- judge cost is offline analysis cost and is never added to agent cost.
"""
import argparse
import csv
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from experiments.analysis_stats import describe, wilson95
from experiments.metrics import summarize_run

CONDITIONS = ("clean", "clean_verify", "fault", "always_verify", "full_restart", "selective")
TREATMENTS = {"condition", "method_id", "recovery_policy", "restore_mode"}
WORK = ("retained_valid", "retained_invalid", "retained_unknown", "discarded_valid", "discarded_invalid",
        "discarded_unknown", "repeated_valid", "repeated_unknown")
RATES = (("strict", "final_strict"), ("initial_strict", "initial_strict"), ("attack", "attack_success"),
         ("harm_in_final_state", "harm_in_final_state"), ("recovered", "recovered"),
         ("triggered", "injection_triggered"))
NUMERIC = ("model_calls", "tool_calls", "tokens", "latency_s", "initial_latency_s", "recovery_model_calls",
           "recovery_tool_calls", "recovery_tokens", "recovery_latency_s", "recomputed_stages", "retained_stages",
           "checkpoint_attempts", "unauthorized_executions", "repeated_actions", "tracking_overhead_ms",
           "tracking_overhead_share", "checkpoint_capture_ms", "checkpoint_restore_ms", "checkpoint_bytes",
           "recording_ms", "recovery_planning_ms", "reuse_check_ms", *(w + "_calls" for w in WORK),
           "recovery_calls", "abandoned_calls", "redone_calls", "invalid_calls", "unjudged_executions",
           "judge_model_calls", "judge_tokens")


def _tokens(cost):
    cost = cost or {}
    if cost.get("input_tokens") is None or cost.get("output_tokens") is None:
        return None
    return cost["input_tokens"] + cost["output_tokens"]


def _seconds(ms):
    return round(ms / 1000, 3) if ms is not None else None


def load_judgments(directory):
    """run_id -> (judged summary, judge cost) from scripts/judge_work.py output.

    Keyed by run_id and read relative to `directory`, so a packaged copy works too.
    """
    found = {}
    for row in json.loads((Path(directory) / "index.json").read_text()):
        if row.get("status") == "complete":
            out = Path(directory) / Path(row["output"]).name
            found[row["run_id"]] = (json.loads((out / "summary.json").read_text()), row["judge_cost"])
    return found


def _notice(config):
    """Notice timing for recovery runs: late (end of initial workflow) or early (after the faulty handoff)."""
    if config["condition"] not in ("selective", "full_restart"):
        return ""
    return "early" if config.get("notification_boundary") == "after_faulty_handoff" else "late"


def _restore(config):
    """How recovery treats money already sent: simulated (undone) or real_bank (kept)."""
    if config["condition"] not in ("selective", "full_restart"):
        return ""
    return config.get("restore_mode", "simulated")


def _label(row):
    return (row["condition"] + (" (early notice, best case)" if row.get("notice") == "early" else "")
            + (" (real-bank restore)" if row.get("restore") == "real_bank" else ""))


def trial_row(trial, judgments):
    result = json.loads((trial / "result_0000.json").read_text())
    config, run = result["config"], trial / "run_0000"
    fault = config.get("fault") or {}
    row = {"trial": trial.name, "task": config["task_id"], "scenario": config["scenario_id"],
           "family": fault.get("family", ""), "site": fault.get("site", ""), "condition": config["condition"],
           "notice": _notice(config), "restore": _restore(config), "repeat": config["repeat_index"], "error": result.get("error") or "",
           "controls": json.dumps({k: v for k, v in config.items() if k not in TREATMENTS}, sort_keys=True)}
    judged, judge_cost = None, None
    try:
        run_id = json.loads((run / "manifest.json").read_text())["run_id"]
        judged, judge_cost = judgments.get(run_id, (None, None))
        s = judged or summarize_run(run, write=False)
    except (OSError, ValueError, KeyError) as exc:
        # Keep the trial; everything measured from its trace is unknown, never zero.
        row["error"] = row["error"] or f"{type(exc).__name__}: {exc}"
        s = {}
    total, added, attack = s.get("total_cost") or {}, s.get("recovery_added_cost") or {}, s.get("attack") or {}
    attempts = s.get("attempts") or []
    recovery = bool(s.get("recovery_attempted"))
    row.update(
        trace_complete=s.get("trace_complete"), initial_strict=s.get("initial_strict_success"),
        final_strict=s.get("final_strict_success"), injection_triggered=s.get("injection_triggered") if fault else None,
        attack_success=s.get("attack_success"),
        harm_in_final_state=attack.get("harm_in_final_state",
            attack["attempts"][-1]["in_final_state"] if attack.get("attempts") else None),
        unauthorized_executions=s.get("unauthorized_executions"),
        repeated_actions=len(s["cross_attempt_repeats"]["actions"])
            if (s.get("cross_attempt_repeats") or {}).get("status") == "evaluated" else None,
        recovery_attempted=recovery if s else None,
        recovered=(s.get("recovery_completed") is True and s.get("recovery_task_success") is True) if recovery else None,
        executed_strategy=s.get("executed_strategy") or "", fallback_reason=s.get("fallback_reason") or "",
        unresolved_effects=len(s["unresolved_effects"]) if s.get("unresolved_effects") is not None else None,
        budget_exhausted=s.get("budget_exhausted"),
        model_calls=total.get("model_calls"), tool_calls=total.get("tool_calls"), tokens=_tokens(total),
        latency_s=_seconds(s.get("total_latency_ms")),
        initial_latency_s=_seconds(attempts[0]["latency_ms"]) if attempts else None)
    # Recovery measurements are not applicable (empty), not zero, when no recovery ran.
    row.update(
        recovery_model_calls=added.get("model_calls") if recovery else None,
        recovery_tool_calls=added.get("tool_calls") if recovery else None,
        recovery_tokens=_tokens(added) if recovery else None,
        recovery_latency_s=_seconds(s.get("recovery_latency_ms")) if recovery else None,
        recomputed_stages=s.get("recomputed_nodes") if recovery else None,
        retained_stages=s.get("effective_retained_artifacts") if recovery else None,
        checkpoint_attempts=s.get("checkpoint_attempts") if recovery else None)
    parts = {"checkpoint_capture_ms": s.get("checkpoint_capture_ms"),
             "checkpoint_restore_ms": s.get("checkpoint_restore_ms"),
             "recovery_planning_ms": s.get("recovery_planning_ms"), "reuse_check_ms": s.get("reuse_check_ms"),
             "recording_ms": s.get("recording_overhead_ms")}
    known = all(v is not None for v in parts.values())
    overhead = sum(parts.values()) if known else None
    row.update({k: round(v, 3) if v is not None else None for k, v in parts.items()},
               checkpoint_bytes=s.get("checkpoint_bytes"),
               tracking_overhead_ms=round(overhead, 3) if known else None,
               tracking_overhead_share=round(overhead / s["total_latency_ms"], 5)
                   if known and s.get("total_latency_ms") else None)
    work = (s.get("work_breakdown") or {}).get("totals")
    calls = lambda key: work[key]["model_calls"] + work[key]["tool_calls"] if work else None
    both = lambda prefix: sum(calls(f"{prefix}_{v}") for v in ("valid", "invalid", "unknown")) if work else None
    row.update({key + "_calls": calls(key) for key in WORK},
               recovery_calls=both("recovery_effective"), abandoned_calls=both("recovery_abandoned"),
               redone_calls=both("recovery_effective") + both("recovery_abandoned") if work else None,
               invalid_calls=calls("invalid"),
               unjudged_executions=s["work_breakdown"]["validity_coverage"]["total"]
                   - s["work_breakdown"]["validity_coverage"]["judged"] if work else None,
               judge_model_calls=judge_cost["model_calls"] if judge_cost else None,
               judge_tokens=_tokens(judge_cost) if judge_cost else None)
    return row


def _group(rows, keys):
    groups = {}
    for r in rows:
        groups.setdefault(tuple(r[k] for k in keys), []).append(r)
    return groups


def condition_rows(rows):
    out = []
    for key, rs in _group(rows, ("family", "site", "condition", "notice", "restore")).items():
        row = dict(zip(("family", "site", "condition", "notice", "restore"), key), trials=len(rs),
                   errors=sum(bool(r["error"]) or r["trace_complete"] is not True for r in rs),
                   budget_exhausted=sum(r["budget_exhausted"] is True for r in rs),
                   fallbacks=sum(bool(r["fallback_reason"]) for r in rs),
                   runs_with_unresolved_effects=sum((r["unresolved_effects"] or 0) > 0 for r in rs))
        for name, column in RATES:
            known = [r[column] for r in rs if r[column] is not None]
            k, n = sum(v is True for v in known), len(known)
            low, high = wilson95(k, n)
            row.update({f"{name}_k": k, f"{name}_n": n, f"{name}_ci_low": low, f"{name}_ci_high": high})
        for column in NUMERIC:
            stats = describe([r[column] for r in rs])
            row.update({f"{column}_n": stats["samples"], f"{column}_mean": stats["mean"],
                        f"{column}_sd": stats["stddev"], f"{column}_min": stats["min"], f"{column}_max": stats["max"]})
        out.append(row)
    order = {c: i for i, c in enumerate(CONDITIONS)}
    return sorted(out, key=lambda r: (r["family"], r["site"], order.get(r["condition"], 99), r["notice"] == "early", r["restore"] == "real_bank"))


def pair_rows(rows):
    """Selective minus full restart for identical controls; unmatched trials are listed, not paired."""
    out = []
    recovery = [r for r in rows if r["condition"] in ("selective", "full_restart") and r["recovery_attempted"]]
    for (task, scenario, repeat, notice, restore), rs in sorted(
            _group(recovery, ("task", "scenario", "repeat", "notice", "restore")).items()):
        by = _group(rs, ("condition",))
        selective, restart = by.get(("selective",), []), by.get(("full_restart",), [])
        row = {"task": task, "scenario": scenario, "repeat": repeat, "notice": notice, "restore": restore,
               "family": rs[0]["family"], "site": rs[0]["site"]}
        if len(selective) != 1 or len(restart) != 1:
            out.append({**row, "status": "unpaired"})
            continue
        out.append(_pair(row, selective[0], restart[0]))
    return out


def _pair(row, a, b):
    """Selective minus full restart for one trial pair."""
    row["status"] = "matched" if a["controls"] == b["controls"] else "controls_differ"
    row.update(selective_strict=a["final_strict"], restart_strict=b["final_strict"],
               selective_strategy=a["executed_strategy"], selective_harm_in_final_state=a["harm_in_final_state"],
               restart_harm_in_final_state=b["harm_in_final_state"])
    for column in ("recovery_model_calls", "recovery_tool_calls", "recovery_tokens", "recovery_latency_s",
                   "recomputed_stages", "latency_s", "tracking_overhead_ms"):
        x, y = a[column], b[column]
        row.update({f"selective_{column}": x, f"restart_{column}": y,
                    f"diff_{column}": round(x - y, 3) if x is not None and y is not None else None})
    return row


def write_csv(path, rows):
    fields = list(dict.fromkeys(k for r in rows for k in r))
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _n(value, digits=1):
    return "–" if value is None else f"{value:.{digits}f}"


def _frac(row, name):
    return f"{row[name + '_k']}/{row[name + '_n']}" if row[name + "_n"] else "–"


# An oracle names the faulty result for free; no real system has that. always_verify pays to find it.
EARLY_NOTE = ("**Early notice is a best case, not a fair comparison with always_verify.** The oracle names the "
              "faulty result right after the handoff at no cost, which a real system cannot do. always_verify "
              "shows what it actually costs to catch the fault. Read early-notice rows as an upper bound on what "
              "recovery could achieve with perfect, free detection.")


def markdown(rows, conditions, pairs, judged):
    lines = ["# Phase 2 report", "",
             "Generated by `scripts/phase2_report.py`; definitions are in that script's header.",
             f"{len(rows)} trials, {sum(bool(r['error']) or r['trace_complete'] is not True for r in rows)} "
             "with errors or incomplete traces. Means are over repeats; empty cells are unknown or not applicable.", "",
             "## Outcome", "",
             "| Faults | Site | Condition | Trials | Fault triggered | Final strict | Attack success | Harm in final state | "
             "Unauthorized executions | Fallbacks | Unresolved effects |", "|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in conditions:
        lines.append(f"| {r['family'] or 'none'} | {r['site'] or '–'} | {_label(r)} | {r['trials']} | "
                     f"{_frac(r, 'triggered')} | {_frac(r, 'strict')} | {_frac(r, 'attack')} | {_frac(r, 'harm_in_final_state')} | "
                     f"{_n(r['unauthorized_executions_mean'])} | {r['fallbacks']} | {r['runs_with_unresolved_effects']} |")
    if any(r.get("notice") == "early" for r in conditions):
        lines += ["", EARLY_NOTE]
    recovery = [r for r in conditions if r["condition"] in ("selective", "full_restart")]
    lines += ["", "## Recovery cost", "",
              "| Faults | Site | Condition | Recomputed stages | Retained stages | Added model calls (sd) | "
              "Added tokens | Recovery latency s (sd) | Total latency s |", "|---|---|---|---|---|---|---|---|---|"]
    for r in recovery:
        lines.append(f"| {r['family']} | {r['site']} | {_label(r)} | {_n(r['recomputed_stages_mean'])} | "
                     f"{_n(r['retained_stages_mean'])} | {_n(r['recovery_model_calls_mean'])} "
                     f"({_n(r['recovery_model_calls_sd'])}) | {_n(r['recovery_tokens_mean'], 0)} | "
                     f"{_n(r['recovery_latency_s_mean'], 2)} ({_n(r['recovery_latency_s_sd'], 2)}) | "
                     f"{_n(r['latency_s_mean'], 2)} |")
    matched = [p for p in pairs if p["status"] == "matched"]
    lines += ["", "## Selective against full restart (matched pairs)", "",
              f"{len(matched)} matched pairs; {len(pairs) - len(matched)} unpaired or with differing controls "
              "(see `pairs.csv`). Differences are selective minus full restart.", "",
              "| Faults | Site | Notice | Restore | Pairs | Both strict | Mean diff model calls | Mean diff tokens | "
              "Mean diff recovery latency s | Selective cheaper (calls) |", "|---|---|---|---|---|---|---|---|---|---|"]
    for (family, site, notice, restore), ps in sorted(
            _group(matched, ("family", "site", "notice", "restore")).items()):
        mean = lambda column: describe([p[column] for p in ps])["mean"]
        calls = [p["diff_recovery_model_calls"] for p in ps if p["diff_recovery_model_calls"] is not None]
        lines.append(f"| {family} | {site} | {notice} | {restore} | {len(ps)} | "
                     f"{sum(p['selective_strict'] is True and p['restart_strict'] is True for p in ps)} | "
                     f"{_n(mean('diff_recovery_model_calls'))} | {_n(mean('diff_recovery_tokens'), 0)} | "
                     f"{_n(mean('diff_recovery_latency_s'), 2)} | {sum(c < 0 for c in calls)}/{len(calls)} |")
    lines += ["", "## Retained and redone work (mean model and tool calls per run)", "",
              ("Validity from offline judgments (`--judgments`)." if judged else
               "No offline judgments supplied: validity is unknown except where the trace already records it."),
              "Redone, valid first time: reruns of a stage whose discarded first execution was judged valid. "
              "Finer categories are in `trials.csv`.", "",
              "| Faults | Site | Condition | Retained valid | Retained invalid | Retained unjudged | Redone | "
              "Redone, valid first time |", "|---|---|---|---|---|---|---|---|"]
    for r in recovery:
        cells = [_n(r[f"{k}_calls_mean"]) for k in ("retained_valid", "retained_invalid", "retained_unknown",
                                                    "redone", "repeated_valid")]
        lines.append(f"| {r['family']} | {r['site']} | {_label(r)} | " + " | ".join(cells) + " |")
    lines += ["", "## Tracking overhead (mean per run)", "",
              "| Condition | Trials | Checkpoint capture ms | Checkpoint restore ms | Planning + reuse checks ms | "
              "Recording ms | Checkpoint bytes | Overhead share of latency | Initial attempt latency s |",
              "|---|---|---|---|---|---|---|---|---|"]
    for (condition, notice, restore), rs in sorted(
            _group(rows, ("condition", "notice", "restore")).items(),
            key=lambda kv: (CONDITIONS.index(kv[0][0]), kv[0][1] == "early", kv[0][2] == "real_bank")):
        mean = lambda column: describe([r[column] for r in rs])["mean"]
        plan = [r["recovery_planning_ms"] + r["reuse_check_ms"] for r in rs
                if r["recovery_planning_ms"] is not None and r["reuse_check_ms"] is not None]
        lines.append(f"| {_label({'condition': condition, 'notice': notice, 'restore': restore})} | {len(rs)} | {_n(mean('checkpoint_capture_ms'), 2)} | "
                     f"{_n(mean('checkpoint_restore_ms'), 2)} | {_n(describe(plan)['mean'], 2)} | "
                     f"{_n(mean('recording_ms'), 2)} | {_n(mean('checkpoint_bytes'), 0)} | "
                     f"{_n(mean('tracking_overhead_share'), 4)} | {_n(mean('initial_latency_s'), 2)} |")
    judge = [r for r in rows if r["judge_model_calls"] is not None]
    lines += ["", "## Offline judging cost (separate from agent execution)", "",
              (f"{sum(r['judge_model_calls'] for r in judge)} judge model calls over {len(judge)} trials; "
               f"tokens: {sum(r['judge_tokens'] for r in judge) if all(r['judge_tokens'] is not None for r in judge) else 'incomplete usage'}."
               if judge else "No judge output supplied."), ""]
    return "\n".join(lines)


def build(sweep, out, judgments=None):
    trials = sorted(d for d in (Path(sweep) / "trials").iterdir() if (d / "result_0000.json").exists())
    judged = load_judgments(judgments) if judgments else {}
    rows = [trial_row(t, judged) for t in trials]
    conditions, pairs = condition_rows(rows), pair_rows(rows)
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    write_csv(out / "trials.csv", [{k: v for k, v in r.items() if k != "controls"} for r in rows])
    write_csv(out / "conditions.csv", conditions)
    write_csv(out / "pairs.csv", pairs)
    (out / "REPORT.md").write_text(markdown(rows, conditions, pairs, bool(judgments)))
    return rows, conditions, pairs


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("sweep", type=Path, help="output directory of scripts/run_phase2_banking.py")
    p.add_argument("--judgments", type=Path, help="output directory of scripts/judge_work.py for this sweep")
    p.add_argument("--output", type=Path, help="default: <sweep>/report")
    a = p.parse_args()
    out = a.output or a.sweep / "report"
    rows, _, pairs = build(a.sweep, out, a.judgments)
    print(f"{len(rows)} trials, {sum(p['status'] == 'matched' for p in pairs)} matched pairs -> {out}")


if __name__ == "__main__":
    main()
