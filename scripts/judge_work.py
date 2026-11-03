"""Offline node validity and D waste metrics; source traces are never modified."""
import argparse
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from experiments.judge_cache import JudgeCache, seed_cache
from experiments.work_judgments import LLMJudge, REFERENCE, analyze_work
from workflow.client import ModelConfig, OpenAICompatibleClient


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("source", type=Path, help="a run directory or final sweep directory")
    p.add_argument("--output", required=True, type=Path, help="new directory, outside the source tree")
    p.add_argument("--reference", type=Path, default=REFERENCE, help="A task YAML matching the experiment version")
    p.add_argument("--llm", action="store_true", help="explicitly enable real judge API requests for unresolved nodes")
    p.add_argument("--full-evidence", action="store_true", help="use legacy uncompressed prompts (including for old cache replay)")
    p.add_argument("--cache-dir", type=Path, help="persistent local exact-request cache")
    p.add_argument("--cache-only", action="store_true", help="replay cached judgments; never call API or require a key")
    p.add_argument("--seed-cache-from", type=Path, help="import validated responses from a previous analysis")
    p.add_argument("--judge-model")
    p.add_argument("--judge-base-url")
    p.add_argument("--api-key-env", default="TRITONAI_API_KEY")
    p.add_argument("--max-judge-calls", type=int, help="shared cap for this entire invocation, required with --llm")
    p.add_argument("--max-packet-chars", type=int, default=80000)
    p.add_argument("--judge-max-tokens", type=int, default=1024)
    p.add_argument("--judge-timeout", type=float, default=120)
    a = p.parse_args()
    source, out = a.source.resolve(), a.output.resolve()
    if out.exists() or out == source or source in out.parents or out in source.parents:
        p.error("output must be new and outside the source tree")
    if a.max_packet_chars < 1 or a.judge_max_tokens < 1 or a.judge_timeout <= 0:
        p.error("judge limits must be positive")
    if (source / "manifest.json").is_file():
        runs = [source]
    elif (source / "trials").is_dir():
        runs = sorted(r for r in (source / "trials").glob("*/run_0000")
                      if (r.parent / "result_0000.json").exists())
    else:
        p.error("expected a run or a final sweep containing trials/")
    if not runs:
        p.error("no completed trial directories found")
    judge = None
    if (a.cache_only or a.seed_cache_from) and not (a.llm and a.cache_dir):
        p.error("cache-only and seeding require --llm and --cache-dir")
    if a.cache_dir:
        cachedir = a.cache_dir.resolve()
        if cachedir == source or source in cachedir.parents or cachedir in source.parents:
            p.error("cache must be outside the source tree")
        if cachedir == out or out in cachedir.parents or cachedir in out.parents:
            p.error("cache must be separate from analysis output")
    if a.llm:
        if not a.judge_model or not a.judge_base_url or (not a.cache_only and (not a.max_judge_calls or a.max_judge_calls < 1)):
            p.error("--llm requires explicit judge model, base URL, and positive max-judge-calls")
        key = os.environ.get(a.api_key_env)
        if not key and not a.cache_only:
            p.error("judge API key environment variable is not set")
        client = OpenAICompatibleClient(ModelConfig(model=a.judge_model, base_url=a.judge_base_url,
            plan_format="json_object", temperature=0, max_tokens=a.judge_max_tokens,
            timeout_seconds=a.judge_timeout), api_key=key or "unused-offline")
        cache = JudgeCache(a.cache_dir) if a.cache_dir else None
        if a.seed_cache_from:
            print("Cache import:", seed_cache(cache, a.seed_cache_from))
        judge = LLMJudge(client, max_calls=0 if a.cache_only else a.max_judge_calls,
                         max_packet_chars=a.max_packet_chars, cache=cache, cache_only=a.cache_only, compact_evidence=not a.full_evidence)
    out.mkdir(parents=True)
    index = []
    for i, run in enumerate(runs):
        destination = out / f"run_{i:04d}"
        try:
            report, summary = analyze_work(run, destination, judge=judge, reference_path=a.reference)
            index.append({"source": str(run), "output": str(destination), "run_id": report["run_id"],
                          "coverage": summary["work_validity_coverage"], "judge_cost": report["judge_cost"],
                          "llm_estimates_present": report["llm_estimates_present"], "status": "complete"})
        except Exception as exc:
            index.append({"source": str(run), "status": "error", "error_type": type(exc).__name__})
        (out / "index.json").write_text(json.dumps(index, indent=2) + "\n")
    if judge:
        (out / "all_judge_calls.json").write_text(json.dumps(judge.calls, indent=2, ensure_ascii=False) + "\n")
    print(f"{len(index)} source runs; {sum(r['status']=='error' for r in index)} errors; output: {out}")
    return int(any(r["status"] == "error" for r in index))


if __name__ == "__main__":
    raise SystemExit(main())
