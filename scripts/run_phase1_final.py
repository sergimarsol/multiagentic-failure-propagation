"""Phase 1 final runs: tasks 0 and 2, C's 10 scenarios, five conditions, repeated.

Each trial is its own D batch under <output>/trials/, so the sweep can be stopped and
resumed: finished trials are skipped, an interrupted one is moved to
<output>/interrupted/ (kept, never deleted) and run again. At the end every finished
trial is summarized together into <output>/summary/ (runs.csv, condition_summary.csv).

  uv run python scripts/run_phase1_final.py --profile configs/phase1_final.json \
      --output runs/phase1-final

The profile holds the team's locked model settings; any flag given explicitly overrides it.

Use --smoke first (one scenario through all five conditions, repeat 0 only).
--scripted runs the same plan with B's test client, to check the pipeline only.
"""
import argparse
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from faults.injection_harness import FaultInjector
from experiments.workflow_bridge import workflow_batch, summarize_workflow_runs

TASKS = ("user_task_0", "user_task_2")
CONTROLS = ("clean", "clean_verify")
FAULT_CONDITIONS = ("fault", "full_restart", "always_verify")
SITES = {"researcher_to_planner": "research_to_plan", "planner_to_executor": "plan_to_execute"}
PAYLOADS = "faults/payloads.yaml"
PROFILE_KEYS = {"model", "base_url", "runtime", "plan_format", "temperature", "max_tokens", "request_timeout",
                "repeats", "api_key_env"}
LIMITS = {"model_calls": 60, "tool_calls": 40, "wall_seconds": 900, "tokens": None}


def plan(model, runtime, repeats, synthetic):
    """One config per trial, ordered repeat by repeat so a partial sweep stays balanced."""
    cases = FaultInjector.from_yaml().cases
    configs = []
    for repeat in range(repeats):
        for task in TASKS:
            base = {"task_id": task, "comparison_group_id": f"{task}_r{repeat}", "repeat_index": repeat,
                    "synthetic": synthetic, "workflow": {"model": model, "runtime": runtime, "session": {}},
                    "limits": dict(LIMITS)}
            for condition in CONTROLS:
                configs.append({**base, "condition": condition, "scenario_id": "clean"})
            for scenario, case in cases.items():
                if case["task_id"] != task:
                    continue
                fault = {"family": case["family"], "site": SITES[case["boundary"]], "payload_ref": PAYLOADS,
                         "fixture": False, "injector_factory": "faults.injection_harness:make_injector"}
                for condition in FAULT_CONDITIONS:
                    configs.append({**base, "condition": condition, "scenario_id": scenario, "fault": fault})
    return configs


def key(config):
    return f"r{config['repeat_index']}-{config['task_id']}-{config['scenario_id']}-{config['condition']}"


def git(*args):
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True).stdout.strip()


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--model", help="model identifier served at --base-url")
    p.add_argument("--base-url", default="http://127.0.0.1:11434/v1")
    p.add_argument("--runtime", help="backend, version and quantization (recorded as given)")
    p.add_argument("--plan-format", default="json_schema", choices=["json_schema", "json_object", "prompt_only"])
    p.add_argument("--temperature", type=float, default=0.0)
    p.add_argument("--max-tokens", type=int, default=1536)
    p.add_argument("--request-timeout", type=float, default=120.0)
    p.add_argument("--repeats", type=int, default=5)
    p.add_argument("--smoke", action="store_true", help="repeat 0, task 0: controls + first scenario only")
    p.add_argument("--scripted", action="store_true", help="B's test client instead of a model (pipeline check)")
    p.add_argument("--api-key-env", default="OPENAI_COMPATIBLE_API_KEY")
    p.add_argument("--profile", type=Path, help="JSON file with the locked settings (keys are option names)")
    pre, _ = p.parse_known_args()
    profile = {}
    if pre.profile:
        profile = json.loads(pre.profile.read_text())
        unknown = set(profile) - PROFILE_KEYS
        if unknown:
            p.error(f"unknown profile settings: {sorted(unknown)}")
        p.set_defaults(**profile)
    a = p.parse_args()
    if a.repeats < 1:
        p.error("--repeats must be positive")
    if not a.scripted and not (a.model and a.runtime):
        p.error("--model and --runtime are required for real runs")
    dirty = git("status", "--porcelain", "--untracked-files=no")
    if dirty and not a.scripted:
        p.error("commit or stash changes first; final runs must come from one recorded commit\n" + dirty)

    model = {"model": a.model or "fixture", "base_url": a.base_url, "plan_format": a.plan_format,
             "temperature": a.temperature, "max_tokens": a.max_tokens, "timeout_seconds": a.request_timeout}
    configs = plan(model, a.runtime or "scripted-test-client", 1 if a.smoke else a.repeats, a.scripted)
    if a.smoke:
        first = next(c["scenario_id"] for c in configs if c["task_id"] == "user_task_0" and "fault" in c)
        configs = [c for c in configs if c["task_id"] == "user_task_0" and c["scenario_id"] in ("clean", first)]

    info = {"commit": git("rev-parse", "HEAD"), "dirty": bool(dirty), "python": platform.python_version(),
            "machine": f"{platform.system()} {platform.release()} {platform.machine()}", "host": platform.node(),
            "argv": sys.argv[1:], "profile": profile, "trials": len(configs), "keys": [key(c) for c in configs]}
    out = a.output.resolve()
    trials, interrupted = out / "trials", out / "interrupted"
    info_path = out / "run_info.json"
    if info_path.exists():
        old = json.loads(info_path.read_text())
        same = {k: old.get(k) for k in ("commit", "argv", "profile", "keys")} == \
            {k: info[k] for k in ("commit", "argv", "profile", "keys")}
        if not same:
            p.error(f"{out} holds a sweep with a different commit or settings; use a new --output")
        info["started"] = old["started"]
    else:
        out.mkdir(parents=True, exist_ok=True)
        info["started"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    info_path.write_text(json.dumps(info, indent=2) + "\n")

    factory = None
    if a.scripted:
        from tests.test_workflow import FakeModel
        def factory(model_config):
            client = FakeModel()
            client.config = model_config
            return client
    api_key = os.environ.get(a.api_key_env, "ollama")

    for i, config in enumerate(configs, 1):
        directory = trials / key(config)
        if (directory / "result_0000.json").exists():
            continue
        if directory.exists():
            interrupted.mkdir(parents=True, exist_ok=True)
            shutil.move(str(directory), str(interrupted / f"{key(config)}-{time.strftime('%Y%m%d%H%M%S')}"))
        tick = time.monotonic()
        row = workflow_batch([config], directory, client_factory=factory, api_key=api_key)[0]
        s = row["summary"]
        print(f"{i}/{len(configs)} {key(config)} strict={s.get('final_strict_success')} "
              f"attack={s.get('attack_success')} error={bool(row['error'])} {time.monotonic() - tick:.0f}s", flush=True)

    summary = out / "summary"
    if summary.exists():
        shutil.rmtree(summary)
    runs = sorted(d for d in trials.glob("*/run_0000") if (d.parent / "result_0000.json").exists())
    rows = summarize_workflow_runs(runs, summary)
    info["finished"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    info["interrupted_kept"] = sorted(d.name for d in interrupted.glob("*")) if interrupted.exists() else []
    info_path.write_text(json.dumps(info, indent=2) + "\n")
    bad = [r for r in rows if r["error"] or not r["summary"].get("trace_complete")]
    print(f"done: {len(rows)} trials summarized in {summary}; {len(bad)} with errors or incomplete traces")


if __name__ == "__main__":
    main()
