"""Launch live C injection experiments through the REAL A/B/C/D Banking workflow.

Only this launcher is supplied; it never injects directly or alters A/B/D code.
C's canonical payloads are loaded through FaultInjector, which resolves original
values from tasks/banking_tasks.yaml (not from faults/payloads.yaml).

Useful PowerShell commands (run from the repository root):
    uv run python scripts/demo_injection.py --list
    uv run python scripts/demo_injection.py --scenario t2_rp_accidental_rent_increase --paired --dry-run
    uv run python scripts/demo_injection.py --task user_task_2 --condition clean --model MODEL --base-url URL
    uv run python scripts/demo_injection.py --scenario t2_rp_accidental_rent_increase --paired --model MODEL --base-url URL
    uv run python scripts/demo_injection.py --matrix --dry-run
    uv run python scripts/demo_injection.py --matrix --analyze --model MODEL --base-url URL

A `run_workflow.py` exit code of 1 may simply mean the task failed the strict
scorer; if it produced a complete trace it remains a VALID experiment trial.
This launcher does not infer error propagation or causal conclusions.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
from urllib.parse import urlsplit
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from faults.injection_harness import FaultInjector

CONDITIONS = ("clean", "fault", "always_verify", "full_restart")
FAULT_CONDITIONS = CONDITIONS[1:]
TASKS = ("user_task_0", "user_task_2")
INJECTOR_FACTORY = "faults.injection_harness:make_injector"

# Parses command-line arguments to configure experiment selection,
# model settings, output paths, and optional trace analysis.
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    selection = p.add_mutually_exclusive_group(required=True)
    selection.add_argument("--list", action="store_true", help="List all 10 payloads; no model calls")
    selection.add_argument("--scenario", metavar="ID", help="Run a single fixed fault scenario")
    selection.add_argument("--all", action="store_true", help="Run 8 core scenarios (or both clean tasks)")
    selection.add_argument("--task", choices=TASKS, help="Run a clean task baseline")
    selection.add_argument("--matrix", action="store_true", help="2 clean + 8 x 3 fault conditions = 26 runs")
    p.add_argument("--condition", choices=CONDITIONS,
                   help="Run condition; default fault (or clean when --task is given)")
    p.add_argument("--paired", action="store_true",
                   help="Automatically run clean baseline(s) first and pair fault runs")
    p.add_argument("--matched-clean", type=Path,
                   help="Existing clean trace directory; --scenario only, instead of --paired")
    p.add_argument("--model", default=os.getenv("MODEL_ID"))
    p.add_argument("--base-url", default=os.getenv("MODEL_BASE_URL"))
    p.add_argument("--api-key-env", default="TRITONAI_API_KEY",
                   help="Name of environment variable containing API key (not the key itself)")
    p.add_argument("--plan-format", choices=("json_schema", "json_object", "prompt_only"),
                   default="json_object")
    p.add_argument("--temperature", type=float, default=0.0)
    p.add_argument("--max-tokens", type=int)
    p.add_argument("--timeout", type=float)
    p.add_argument("--max-turns", type=int)
    p.add_argument("--max-model-calls", type=int)
    p.add_argument("--max-tool-calls", type=int)
    p.add_argument("--request-retries", type=int, choices=(0, 1))
    p.add_argument("--output-root", type=Path, default=Path("runs/c-injection-demo"))
    p.add_argument("--dry-run", action="store_true", help="Preview commands without writing files or model calls")
    p.add_argument("--stop-on-error", action="store_true",
                   help="Stop on infrastructure or incomplete-trace error, not strict task failure")
    p.add_argument("--analyze", action="store_true",
                   help="Run existing scripts/analyze_injection_trace.py on this session's traces")
    return p.parse_args(argv)

# Maps each fault scenario's handoff boundary to the injection site
# expected by the Banking workflow.
def _site(case: dict) -> str:
    mapping = {"researcher_to_planner": "research", "planner_to_executor": "plan"}
    try:
        return mapping[case["boundary"]]
    except KeyError as exc:
        raise ValueError(f"Unknown injection boundary: {case.get('boundary')!r}") from exc

# Builds an ordered execution plan for the selected scenarios and
# conditions, including clean baselines when pairing is enabled.
def _make_plan(args: argparse.Namespace, cases: dict[str, dict]) -> list[dict]:
    """An ordered list; matched baselines are resolved within this session."""
    core_cases = [c for c in cases.values() if c["operation"] != "append_plan_note"]
    if args.matrix:
        if args.condition or args.paired or args.matched_clean:
            raise ValueError("--matrix cannot be combined with --condition, --paired or --matched-clean")
        return ([dict(task_id=t, condition="clean") for t in TASKS] +
                [dict(task_id=c["task_id"], condition=condition, scenario_id=c["id"])
                 for c in core_cases for condition in FAULT_CONDITIONS])

    condition = args.condition or ("clean" if args.task else "fault")
    if args.task:
        if condition != "clean" or args.paired or args.matched_clean:
            raise ValueError("--task is only for a clean baseline, without --paired or --matched-clean")
        return [dict(task_id=args.task, condition="clean")]
    if args.scenario and args.scenario not in cases:
        raise ValueError(f"Unknown scenario: {args.scenario}. Use --list to see valid IDs")
    if args.scenario and condition == "clean":
        raise ValueError("Use --task user_task_0/user_task_2 to run a clean baseline")
    if args.matched_clean and (not args.scenario or args.paired):
        raise ValueError("--matched-clean requires one --scenario and cannot be combined with --paired")
    if args.paired and condition == "clean":
        raise ValueError("--paired is for fault conditions, not clean-only runs")

    if args.all and condition == "clean":
        return [dict(task_id=t, condition="clean") for t in TASKS]
    targets = core_cases if args.all else [cases[args.scenario]]
    faults = [dict(task_id=c["task_id"], scenario_id=c["id"], condition=condition)
              for c in targets]
    if args.paired:
        needed = {row["task_id"] for row in faults}
        return [dict(task_id=t, condition="clean") for t in TASKS if t in needed] + faults
    return faults


def _is_local_endpoint(url: str) -> bool:
    host = urlsplit(url).hostname
    return host in {"127.0.0.1", "localhost", "::1"}


def _json_if_exists(path: Path) -> dict | None:
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, ValueError, UnicodeError):
        return None

# Checks whether a completed clean run has the basic trace artifacts
# needed for use as a baseline comparator.
def _can_match_clean(run_dir: Path) -> bool:
    """Keep B's clean_reference() validation as the final authority."""
    manifest = _json_if_exists(run_dir / "manifest.json") or {}
    summary = _json_if_exists(run_dir / "summary.json") or {}
    return (manifest.get("condition") == "clean" and summary.get("trace_complete") is True
            and (run_dir / "events.jsonl").is_file()
            and (run_dir / "workflow_summary.json").is_file())

# Reads workflow outputs and distinguishes complete experiment traces
# from partial traces or runner errors, regardless of task success.
def _read_result(run_dir: Path, returncode: int) -> dict:
    summary = _json_if_exists(run_dir / "summary.json") or {}
    workflow = _json_if_exists(run_dir / "workflow_summary.json") or {}
    stream_names = ("manifest.json", "events.jsonl", "artifacts.jsonl", "evaluations.jsonl")
    has_trace = all((run_dir / name).is_file() for name in stream_names)
    trace_complete = summary.get("trace_complete") is True
    recorded = bool(has_trace and trace_complete and workflow)
    injection_attempts = workflow.get("injection_attempts") or []
    return {
        "status": "recorded" if recorded else "partial_trace" if has_trace else "runner_error",
        "runner_exit_code": returncode,
        "trace_complete": trace_complete if summary else None,
        "workflow_completed": workflow.get("final_workflow_completed"),
        "initial_strict_success": summary.get("initial_strict_success"),
        "final_strict_success": summary.get("final_strict_success"),
        "injection_applied": (any(x.get("applied") is True for x in injection_attempts)
                              if injection_attempts else None),
        "injection_reasons": [x.get("reason") for x in injection_attempts],
        "model_calls": (summary.get("total_cost") or {}).get("model_calls"),
        "fault_cases_exists": (run_dir / "fault_cases.json").is_file(),
    }

# Constructs the command used to launch the real Banking workflow
# with the selected task, condition, injector, and model settings.
def _command(args: argparse.Namespace, job: dict, run_dir: Path,
             matched_clean: Path | None) -> list[str]:
    cmd = [sys.executable, str(ROOT / "scripts" / "run_workflow.py"),
           "--task", job["task_id"], "--condition", job["condition"],
           "--output", str(run_dir), "--model", args.model or "MODEL_ID",
           "--base-url", args.base_url or "MODEL_BASE_URL",
           "--api-key-env", args.api_key_env, "--plan-format", args.plan_format,
           "--temperature", str(args.temperature)]
    if job["condition"] != "clean":
        case = FaultInjector.from_yaml().cases[job["scenario_id"]]
        cmd += ["--injector", INJECTOR_FACTORY, "--scenario-id", case["id"],
                "--injection-site", _site(case)]
    if matched_clean is not None and job["condition"] != "clean":
        cmd += ["--matched-clean", str(matched_clean)]
    for flag, value in (("--max-tokens", args.max_tokens), ("--timeout", args.timeout),
                        ("--max-turns", args.max_turns),
                        ("--max-model-calls", args.max_model_calls),
                        ("--max-tool-calls", args.max_tool_calls),
                        ("--request-retries", args.request_retries)):
        if value is not None:
            cmd += [flag, str(value)]
    return cmd


def _label(job: dict) -> str:
    if job["condition"] == "clean":
        return f"clean-{job['task_id']}"
    return f"{job['scenario_id']}-{job['condition']}"

# Coordinates experiment execution, saves run metadata, and optionally
# launches propagation analysis on the resulting traces.
def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    cases = FaultInjector.from_yaml().cases
    if args.list:
        for c in cases.values():
            print(f"{c['id']} | {c['task_id']} | {_site(c)} | {c['operation']}")
        return 0
    try:
        jobs = _make_plan(args, cases)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    if not args.dry_run:
        if not args.model or not args.base_url:
            print("ERROR: supply --model and --base-url, or MODEL_ID and MODEL_BASE_URL", file=sys.stderr)
            return 2
        if not _is_local_endpoint(args.base_url) and not os.environ.get(args.api_key_env):
            print(f"ERROR: set environment variable {args.api_key_env} before using a remote provider",
                  file=sys.stderr)
            return 2
        if not (ROOT / "scripts" / "run_workflow.py").is_file():
            print("ERROR: scripts/run_workflow.py is missing", file=sys.stderr)
            return 2
        if args.analyze and not (ROOT / "scripts" / "analyze_injection_trace.py").is_file():
            print("ERROR: scripts/analyze_injection_trace.py is missing", file=sys.stderr)
            return 2

    tag = ("DRY_RUN" if args.dry_run else
           datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:6])
    root = args.output_root if args.output_root.is_absolute() else ROOT / args.output_root
    session_dir = (root / tag).resolve()
    paired_paths: dict[str, Path] = {}
    if args.matched_clean:
        paired_paths[jobs[0]["task_id"]] = (
            args.matched_clean if args.matched_clean.is_absolute() else ROOT / args.matched_clean).resolve()
    report: dict = {"created_utc": datetime.now(timezone.utc).isoformat(),
                    "session": tag, "model": args.model, "base_url": args.base_url,
                    "plan_format": args.plan_format, "dry_run": args.dry_run,
                    "note": "Recorded trials can have strict_success=False. This is not a runner crash.",
                    "runs": []}
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"  # B's existing text readers need UTF-8 on Windows.

    print(f"Planned trials: {len(jobs)} | session: {session_dir}")
    infrastructure_failed = False
    for idx, job in enumerate(jobs, 1):
        run_dir = session_dir / _label(job)
        matched_clean = paired_paths.get(job["task_id"]) if job["condition"] != "clean" else None
        cmd = _command(args, job, run_dir, matched_clean)
        print(f"\n[{idx}/{len(jobs)}] {job['task_id']} / {job['condition']} / "
              f"{job.get('scenario_id', '-')}", flush=True)
        print(subprocess.list2cmdline(cmd), flush=True)
        entry = {"task_id": job["task_id"], "scenario_id": job.get("scenario_id"),
                 "condition": job["condition"], "run_directory": str(run_dir),
                 "matched_clean": str(matched_clean) if matched_clean else None}
        # Never silently call a trial "paired" if its clean comparator failed.
        # During dry-run, the intended clean path is enough to preview the plan.
        needs_baseline = (args.matrix or args.paired) and job["condition"] != "clean"
        if needs_baseline and matched_clean is None:
            entry.update(status="skipped_missing_clean_baseline",
                         error="clean baseline has no complete trace")
            report["runs"].append(entry)
            infrastructure_failed = True
            print("WARNING: paired trial skipped because clean baseline is unavailable", file=sys.stderr)
            if args.stop_on_error:
                break
            continue
        if args.dry_run:
            entry["status"] = "dry_run"
            if job["condition"] == "clean":
                paired_paths[job["task_id"]] = run_dir
            report["runs"].append(entry)
            continue
        if run_dir.exists():
            print(f"ERROR: refusing to overwrite {run_dir}", file=sys.stderr)
            entry["status"] = "runner_error"
            entry["error"] = "output directory already exists"
            infrastructure_failed = True
            report["runs"].append(entry)
            if args.stop_on_error:
                break
            continue
        try:
            completed = subprocess.run(cmd, cwd=ROOT, env=env, check=False)
            entry.update(_read_result(run_dir, completed.returncode))
        except OSError as exc:
            entry.update(status="runner_error", error=str(exc))
        report["runs"].append(entry)
        if job["condition"] == "clean" and _can_match_clean(run_dir):
            paired_paths[job["task_id"]] = run_dir
        if entry["status"] != "recorded":
            infrastructure_failed = True
            print(f"WARNING: trial has {entry['status']} (see {run_dir})", file=sys.stderr)
            if args.stop_on_error:
                break
        else:
            print(f"Trace recorded. Workflow complete={entry['workflow_completed']}; "
                  f"strict success={entry['final_strict_success']}; "
                  f"injection applied={entry['injection_applied']}", flush=True)

    if args.dry_run:
        print("\nDRY RUN only: no files were written and no model was called.")
        return 0

    session_dir.mkdir(parents=True, exist_ok=True)
    index = session_dir / "demo_index.json"
    index.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"\nSaved index: {index}")
    if args.analyze and any(r["status"] == "recorded" for r in report["runs"]):
        analyzer = [sys.executable, str(ROOT / "scripts" / "analyze_injection_trace.py"),
                    "--runs-root", str(session_dir), "--output", str(session_dir / "analysis")]
        print("\nAnalysis: " + subprocess.list2cmdline(analyzer), flush=True)
        analysis_exit_code = subprocess.run(analyzer, cwd=ROOT, env=env, check=False).returncode
        report["analysis_exit_code"] = analysis_exit_code
        index.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        if analysis_exit_code:
            print("WARNING: analyzer returned nonzero; inspect its output", file=sys.stderr)
            infrastructure_failed = True
    print("\nExperiment outputs are OBSERVATIONS, not final causal claims. Compare paired clean traces.")
    return 1 if infrastructure_failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
