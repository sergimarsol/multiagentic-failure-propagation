"""Phase 2 work categories, report and packaging on scripted Banking recovery traces."""
import csv
import json
import subprocess
import sys

import pytest

from experiments.work_judgments import LLMJudge, analyze_work
from experiments.workflow_bridge import execute_workflow, workflow_batch
from scripts.package_phase2_results import main as package
from scripts.phase2_report import build
from tests.test_banking_selective import LATE, EARLY, config, factory
from tests.test_work_judgments import ScriptedJudge


def breakdown(summary):
    rows = summary["work_breakdown"]["executions"]
    return [(r["node_id"], r["phase"], r["disposition"], r["validity"], r["repeats"]) for r in rows]


def test_selective_work_is_split_by_disposition_and_judged_validity(tmp_path):
    run = tmp_path / "run"
    source = execute_workflow(config("selective", scenario=LATE["user_task_0"]), run, client_factory=factory)
    # Without judgments nothing is called valid or invalid.
    assert {r[3] for r in breakdown(source)} == {"unknown"}
    assert [r[:3] for r in breakdown(source)] == [
        ("research", "initial", "retained"), ("plan", "initial", "discarded"),
        ("execute", "initial", "discarded"), ("plan", "recovery", "recovery_effective"),
        ("execute", "recovery", "recovery_effective")]
    report, judged = analyze_work(run, tmp_path / "judged", judge=LLMJudge(ScriptedJudge(), max_calls=20))
    assert breakdown(judged) == [
        ("research", "initial", "retained", "valid", None),
        # The planner's own output was valid; only the delivered replacement was faulty.
        ("plan", "initial", "discarded", "valid", None),
        ("execute", "initial", "discarded", "invalid", None),
        ("plan", "recovery", "recovery_effective", "valid", "valid"),
        ("execute", "recovery", "recovery_effective", "valid", "invalid")]
    totals = judged["work_breakdown"]["totals"]
    assert totals["retained_valid"]["model_calls"] == 2 and totals["discarded_valid"]["model_calls"] == 1
    assert totals["repeated_valid"]["model_calls"] == 1 and totals["invalid"]["model_calls"] == 2
    assert totals["recovery_added"]["model_calls"] == judged["recovery_added_cost"]["model_calls"] == 3
    assert sum(r["model_calls"] for r in judged["work_breakdown"]["executions"]) == judged["total_cost"]["model_calls"]
    # Judge calls are analysis cost, never agent cost.
    assert report["judge_cost"]["model_calls"] == 4 and judged["total_cost"] == source["total_cost"]


def test_full_restart_discards_and_repeats_valid_research(tmp_path):
    run = tmp_path / "run"
    execute_workflow(config("full_restart", scenario=LATE["user_task_0"], recovery_policy={}), run,
                     client_factory=factory)
    _, judged = analyze_work(run, tmp_path / "judged", judge=LLMJudge(ScriptedJudge(), max_calls=20))
    totals = judged["work_breakdown"]["totals"]
    assert totals["retained_valid"]["executions"] == 0 and totals["discarded_valid"]["executions"] == 2
    assert totals["repeated_valid"]["model_calls"] == 3  # research and plan redone


def test_incomplete_recovery_and_abandoned_rounds_stay_explicit(tmp_path):
    limited = config("selective", scenario=LATE["user_task_0"],
                     limits=dict(model_calls=6, tool_calls=30, wall_seconds=300, tokens=None))
    s = execute_workflow(limited, tmp_path / "limited", client_factory=factory)
    assert not s["work_breakdown"]["final_path_known"]
    assert {r[2] for r in breakdown(s)} == {"unresolved"}
    from tests.test_banking_selective import FlakyPlanner, direct
    retried = direct(tmp_path / "retried", LATE["user_task_0"], client=FlakyPlanner())
    assert [r[:3] for r in breakdown(retried)][3:] == [
        ("plan", "recovery", "recovery_abandoned"), ("plan", "recovery", "recovery_effective"),
        ("execute", "recovery", "recovery_effective")]
    # Phase 1 runs keep working: no recovery means all work is simply kept.
    clean = execute_workflow(config("clean"), tmp_path / "clean", client_factory=factory)
    assert {r[2] for r in breakdown(clean)} == {"kept"}


@pytest.fixture(scope="module")
def sweep(tmp_path_factory):
    root = tmp_path_factory.mktemp("sweep")
    late, early = LATE["user_task_0"], EARLY["user_task_0"]
    configs = [config("clean"), config("fault", scenario=late),
               config("full_restart", scenario=late, recovery_policy={}), config("selective", scenario=late),
               config("full_restart", scenario=early, recovery_policy={}), config("selective", scenario=early),
               config("selective", scenario=late, repeat_index=1)]  # no matching restart
    for c in configs:
        key = f"r{c['repeat_index']}-{c['task_id']}-{c['scenario_id']}-{c['condition']}"
        workflow_batch([c], root / "trials" / key, client_factory=factory)
    (root / "run_info.json").write_text(json.dumps({"commit": "test", "keys": [], "argv": [], "finished": "now"}))
    return root


def table(path):
    return list(csv.DictReader(path.open()))


def test_report_pairs_selective_with_restart_and_keeps_unknowns_empty(sweep, tmp_path):
    rows, conditions, pairs = build(sweep, tmp_path / "report")
    assert len(rows) == 7 and not any(r["error"] for r in rows)
    by = {(p["scenario"], p["repeat"]): p for p in pairs}
    late, early = by[(LATE["user_task_0"], 0)], by[(EARLY["user_task_0"], 0)]
    assert late["status"] == early["status"] == "matched"
    assert late["diff_recovery_model_calls"] == -2 and late["diff_recomputed_stages"] == -1
    assert early["diff_recovery_model_calls"] == 0  # a faulty research result leaves nothing to keep
    assert by[(LATE["user_task_0"], 1)]["status"] == "unpaired"
    trials = {r["trial"]: r for r in table(tmp_path / "report" / "trials.csv")}
    clean = trials["r0-user_task_0-clean-clean"]
    assert clean["recovery_model_calls"] == "" and clean["checkpoint_bytes"] == "0"
    selective = trials[f"r0-user_task_0-{LATE['user_task_0']}-selective"]
    # Harm is historical: attack succeeded although the final state no longer shows it.
    assert (selective["attack_success"], selective["harm_in_final_state"]) == ("True", "False")
    assert float(selective["checkpoint_capture_ms"]) > 0 and float(selective["checkpoint_restore_ms"]) > 0
    assert selective["retained_unknown_calls"] == "3" and selective["retained_valid_calls"] == "0"
    assert selective["judge_model_calls"] == ""
    text = (tmp_path / "report" / "REPORT.md").read_text()
    assert "No offline judgments supplied" in text and "2 matched pairs; 1 unpaired" in text
    group = next(c for c in conditions if c["condition"] == "selective" and c["site"] == "plan_to_execute")
    assert group["trials"] == 2 and group["recovery_model_calls_sd"] == 0 and group["strict_k"] == 2


def test_judgments_fill_validity_and_package_is_self_describing(sweep, tmp_path):
    judged = tmp_path / "judged"
    done = subprocess.run([sys.executable, "scripts/judge_work.py", str(sweep), "--output", str(judged)],
                          capture_output=True, text=True)
    assert done.returncode == 0, done.stdout + done.stderr
    package([str(sweep), "--dest", str(tmp_path / "results"), "--judgments", str(judged)])
    out = tmp_path / "results"
    trials = {r["trial"]: r for r in table(out / "report" / "trials.csv")}
    selective = trials[f"r0-user_task_0-{LATE['user_task_0']}-selective"]
    # Rules alone establish the unauthorized execution; the rest stays unjudged.
    assert selective["discarded_invalid_calls"] == "3" and selective["unjudged_executions"] == "4"
    assert selective["judge_model_calls"] == "0"
    readme = (out / "README.md").read_text()
    assert "Validity from offline judgments" in readme and "work_judgments/" in readme
    assert (out / "SHA256SUMS").exists() and (out / "work_judgments" / "index.json").exists()
    assert not list((out / "trials").glob("*/run_0000/checkpoints")) and list((out / "trials").glob("*/run_0000/events.jsonl"))
    # The package rebuilds from itself, without the original sweep or judgment directories.
    rebuilt = build(out, tmp_path / "rebuilt", out / "work_judgments")[0]
    assert {r["trial"]: r["discarded_invalid_calls"] for r in rebuilt}[selective["trial"]] == 3
    with pytest.raises(SystemExit):
        package([str(sweep), "--dest", str(out)])  # never overwrites packaged results
