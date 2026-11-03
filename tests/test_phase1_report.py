"""Phase 1 report: C's labels joined with D's costs give affected stages and work lost to faults."""
import csv
from pathlib import Path
import subprocess
import sys

import pytest

from scripts.phase1_report import CATEGORIES, invalid_nodes

ROOT = Path(__file__).resolve().parents[1]


def _node(seq, node, attempt="attempt_0"):
    return {"event_type": "node_started", "event_seq": seq, "attempt_id": attempt, "node_id": node,
            "node_execution_id": f"{node}-{seq}", "data": {}}


def _events(site):
    return [_node(1, "research"),
            {"event_type": "fault_injected", "event_seq": 2 if site == "research" else 4, "attempt_id": "attempt_0",
             "node_id": None, "node_execution_id": None, "data": {"site": site, "triggered": True}},
            _node(3, "plan"), _node(5, "execute")]


def _label(**kw):
    base = {"attempt_id": "attempt_0", "injection_applied": True, "consumer_exposed": True,
            "planner_adopted_fault": None, "executor_adopted_fault": None, "relevant_tool_attempted": False}
    return {**base, **kw}


@pytest.mark.parametrize("site,label,expected", [
    ("research", _label(planner_adopted_fault=True), {"plan-3", "execute-5"}),
    ("research", _label(planner_adopted_fault=False), set()),
    ("research", _label(planner_adopted_fault=False, relevant_tool_attempted=True), {"execute-5"}),
    ("research", _label(planner_adopted_fault=None, relevant_tool_attempted=None), None),
    ("plan", _label(executor_adopted_fault=True), {"execute-5"}),
    ("plan", _label(executor_adopted_fault=False), set()),
    ("plan", _label(consumer_exposed=False), set()),
    ("plan", _label(injection_applied=False), None),
])
def test_contaminated_nodes(site, label, expected):
    assert invalid_nodes(_events(site), label) == expected


def test_work_before_the_injection_is_never_contaminated():
    # A plan made before a planner->executor injection stays valid even if the executor adopts it.
    assert "plan-3" not in invalid_nodes(_events("plan"), _label(executor_adopted_fault=True))


@pytest.fixture(scope="module")
def sweep(tmp_path_factory):
    out = tmp_path_factory.mktemp("sweep")
    for script in ("run_phase1_final.py", "phase1_report.py"):
        args = ["--scripted", "--repeats", "1", "--output", str(out)] if script.startswith("run") else [str(out)]
        subprocess.run([sys.executable, str(ROOT / "scripts" / script), *args], cwd=ROOT, check=True,
                       capture_output=True)
    read = lambda name: list(csv.DictReader((out / "report" / name).open()))
    return read("trials.csv"), read("conditions.csv"), read("propagation.csv"), out / "report" / "tables.tex"


def test_every_trial_is_reported(sweep):
    trials, conditions, propagation, _ = sweep
    assert len(trials) == 34 and sum(int(r["trials"]) for r in conditions) == 34
    assert sum(int(r["trials"]) for r in propagation) == 30
    controls = [r for r in trials if r["condition"] in ("clean", "clean_verify")]
    assert len(controls) == 4 and all(r["outcome"] == "" and r["discarded_calls"] == "" for r in controls)


def test_labels_and_costs_are_consistent(sweep):
    trials = [r for r in sweep[0] if r["condition"] in ("fault", "full_restart", "always_verify")]
    for r in trials:
        assert r["injection_applied"] == "True" and r["outcome"] in CATEGORIES
        assert r["label_conflict"] == ""
        # Discarded work exists exactly when the injected attempt failed, and includes contaminated work.
        assert (int(r["discarded_calls"]) > 0) == (r["initial_strict"] == "False")
        if r["initial_strict"] == "False":
            assert int(r["contaminated_calls"]) <= int(r["discarded_calls"])
        limit = 2 if r["site"] == "research_to_plan" else 1
        assert 0 <= int(r["affected_stages"]) <= limit
        assert (int(r["affected_stages"]) > 0) == (r["outcome"] in ("propagated", "state_consequence"))
        assert (r["redone_valid_calls"] != "") == (r["condition"] == "full_restart")
    restarts = [r for r in trials if r["condition"] == "full_restart"]
    assert all(int(r["redone_valid_calls"]) > 0 for r in restarts)


def test_families_are_reported_separately(sweep):
    _, conditions, _, tables = sweep
    assert {(r["family"], r["condition"]) for r in conditions} == {
        ("", "clean"), ("", "clean_verify"),
        *((f, c) for f in ("accidental", "adversarial") for c in ("fault", "full_restart", "always_verify"))}
    text = tables.read_text()
    assert r"\label{tab:conditions}" in text and r"\label{tab:propagation}" in text
    assert text.count(r"\\") >= 8 + 10  # condition rows + one row per scenario


def _package(sweep_dir, dest, **env):
    import os
    return subprocess.run([sys.executable, str(ROOT / "scripts/package_phase1_results.py"), str(sweep_dir),
                           "--dest", str(dest)], cwd=ROOT, capture_output=True, text=True,
                          env={**os.environ, **env})


def test_package_keeps_every_trial_with_checksums(sweep, tmp_path):
    import hashlib
    sweep_dir = sweep[3].parents[1]
    dest = tmp_path / "results"
    assert _package(sweep_dir, dest).returncode == 0
    assert len(list((dest / "trials").iterdir())) == 34
    readme = (dest / "README.md").read_text()
    assert "34 of 34 planned finished" in readme and "`fixture`" in readme
    for line in (dest / "SHA256SUMS").read_text().splitlines():
        digest, name = line.split("  ", 1)
        assert hashlib.sha256((dest / name).read_bytes()).hexdigest() == digest
    assert _package(sweep_dir, dest).returncode != 0  # never overwrites


def test_package_refuses_credentials(sweep, tmp_path):
    import shutil
    copy = tmp_path / "sweep"
    shutil.copytree(sweep[3].parents[1], copy)
    (next(copy.glob("trials/*/run_0000")) / "notes.txt").write_text("key abcd1234secretvalue")
    result = _package(copy, tmp_path / "results", TRITONAI_API_KEY="abcd1234secretvalue")
    assert result.returncode != 0 and "credentials" in result.stderr
    assert not (tmp_path / "results").exists() and not (tmp_path / "results.partial").exists()


def test_package_refuses_unfinished_sweep(sweep, tmp_path):
    import json
    import shutil
    copy = tmp_path / "sweep"
    shutil.copytree(sweep[3].parents[1], copy)
    info = json.loads((copy / "run_info.json").read_text())
    info.pop("finished")
    (copy / "run_info.json").write_text(json.dumps(info))
    assert _package(copy, tmp_path / "results").returncode != 0


def test_unreadable_trace_is_reported_not_guessed(sweep, tmp_path):
    import shutil
    from scripts.phase1_report import build
    copy = tmp_path / "sweep"
    shutil.copytree(sweep[3].parents[1], copy)
    run = next(copy.glob("trials/*-fault/run_0000"))
    (run / "events.jsonl").write_text((run / "events.jsonl").read_text() + '{"not": "an event"}\n')
    rows = build(copy, tmp_path / "report")[0]
    broken = next(r for r in rows if r["trial"] == run.parent.name)
    assert broken["outcome"].startswith("analysis_error")
    assert broken["discarded_calls"] is None and broken["affected_stages"] is None
