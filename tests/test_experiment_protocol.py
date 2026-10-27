from dataclasses import asdict
import json
import pytest
from experiments.protocol import Limits, Budget, BudgetExceeded, phase1_matrix, validate_config
from experiments.batch import run_batch, aggregate
from experiments.phase1 import run_phase1
from experiments.recorder import Recorder
from experiments.models import Artifact, Evaluation, new_id
from experiments.metrics import summarize_run
from scripts.run_phase1_example import AAdapter, ScriptedSession, OneShotFault


def configs():
    return phase1_matrix(1, {"model": "fixture"}, asdict(Limits()), synthetic=True)


def test_matrix_and_reject_placeholders():
    rows=configs()
    assert len(rows)==28
    assert len([r for r in rows if r["condition"]=="clean_verify"])==2
    for row in rows:
        validate_config(row)
    with pytest.raises((ValueError,TypeError)):
        validate_config({**rows[0],"limits":{"model_calls":"TBD"}})
    with pytest.raises(ValueError):
        validate_config({**rows[0],"condition":"magic"})
    with pytest.raises(ValueError):
        validate_config({**rows[0],"synthetic":False})


def test_clean_verifier_budget_and_metadata(tmp_path):
    config=configs()[1]
    result=run_phase1(tmp_path,task_id=config['task_id'],condition='clean_verify',
        environment_adapter=AAdapter(config['task_id']),create_session=ScriptedSession,
        injector=OneShotFault(),synthetic=True,experiment_config=config)
    assert result['injection_triggered'] is False
    assert result['verification_cost']['model_calls']==1
    manifest=json.loads((tmp_path/'manifest.json').read_text())
    assert manifest['experiment']['comparison_group_id']==config['comparison_group_id']
    assert manifest['protocol_version']=='1.1'


def test_budget_retains_call_failure_and_prevents_free_restart(tmp_path):
    s=run_phase1(tmp_path,task_id='user_task_0',condition='full_restart',
        environment_adapter=AAdapter('user_task_0'),create_session=ScriptedSession,
        injector=OneShotFault(),synthetic=True,limits=Limits(model_calls=2))
    assert not s['recovery_completed']
    assert s['budget_exhausted']
    assert s['total_cost']['model_calls']==2


def test_unknown_usage_stops_enforced_token_budget():
    b=Budget(Limits(tokens=100))
    b.admit('model');b.finish('model',{'input_tokens':None,'output_tokens':None})
    with pytest.raises(BudgetExceeded,match='unknown'):
        b.admit('model')


def test_batch_preserves_errors_and_unknown_denominators(tmp_path):
    cs=configs()[:2]
    def run(c,p):
        if c['condition']=='clean_verify':
            raise RuntimeError('provider not connected')
        return {'run_id':'ok','final_agentdojo_utility':True,'final_strict_success':True,'trace_complete':True}
    rows=run_batch(cs,tmp_path/'batch',run)
    assert len(rows)==2 and rows[1]['error']
    summary=aggregate(rows,tmp_path/'again')
    failed=next(s for s in summary if s['condition']=='clean_verify')
    assert failed['total']==1 and failed['completion_denominator']==0
    assert failed['completion_rate'] is None and failed['execution_failures']==1
    assert (tmp_path/'batch'/'runs.csv').exists()


def test_synthetic_never_pooled_with_real_and_subgroup_not_cherrypicked(tmp_path):
    c=configs()[0]
    rows=[{'config':c,'summary':{'final_agentdojo_utility':True},'error':None},
          {'config':{**c,'synthetic':False},'summary':{'final_agentdojo_utility':False},'error':None}]
    summary=aggregate(rows,tmp_path)
    assert len(summary)==2
    assert {s['completion_rate'] for s in summary}=={0,1}


def test_old_records_without_segments_readable(tmp_path):
    rec=Recorder(tmp_path,{'run_id':'legacy'})
    rec.log('attempt_started','a0')
    rec.save_evaluation(Evaluation(run_id='legacy',attempt_id='a0',evaluation_id='ev',scope='attempt',evaluator='old',task_success=True))
    rec.log('run_finished','a0',duration_ms=1)
    for name in ('events','evaluations'):
        p=tmp_path/(name+'.jsonl')
        rows=[json.loads(s) for s in p.read_text().splitlines()]
        for row in rows:
            row.pop('execution_segment_id',None)
        p.write_text(''.join(json.dumps(row)+'\n' for row in rows))
    Recorder(tmp_path,{'run_id':'legacy'})
    assert summarize_run(tmp_path)['final_task_success'] is True


def test_different_models_and_budgets_are_not_pooled(tmp_path):
    c=configs()[0]
    alternate={**c,'workflow':{'model':'different'}}
    budget={**c,'limits':{**c['limits'],'model_calls':2}}
    rows=[{'config':config,'summary':{'final_agentdojo_utility':True},'error':None}
          for config in [c,alternate,budget]]
    assert len(aggregate(rows,tmp_path))==3


@pytest.mark.parametrize("changed", [None, "limits", "initial_state_hash", "repeat_index", "workflow", "seed"])
def test_clean_pair_requires_matching_controls(tmp_path, changed):
    import csv
    from copy import deepcopy
    clean = {**configs()[0], "initial_state_hash": "same-state", "seed": 1}
    fault = deepcopy(clean)
    fault.update(condition="fault", scenario_id="injected", method_id="fault-only",
                 fault={"family": "accidental", "site": "research_to_plan"})
    if changed == "limits":
        fault["limits"]["model_calls"] = 1
    elif changed == "workflow":
        fault["workflow"]["model"] = "other-model"
    elif changed is not None:
        fault[changed] = "different"
    rows = [{"config": c, "summary": {"final_agentdojo_utility": True,
             "downstream_exposed": True, "propagation": [{"label": "propagated_to_plan"}]},
             "error": None} for c in (clean, fault)]
    summaries = aggregate(rows, tmp_path)
    with (tmp_path / "runs.csv").open() as stream:
        results = list(csv.DictReader(stream))
    assert results[1]["clean_pair_success"] == ("True" if changed is None else "")
    subgroup = next(s for s in summaries if s["condition"] == "fault")
    assert subgroup["clean_success_exposed_denominator"] == (1 if changed is None else 0)


@pytest.mark.parametrize("policy", [{"checkpoint_retries": -1}, {"max_checkpoint_attempts": 0},
                                   {"max_recoveries": True}, {"unknown_option": 1},
                                   {"fallback_policy": "magic"}])
def test_phase2_policy_config_validation(policy):
    with pytest.raises(ValueError):
        validate_config({**configs()[0], "recovery_policy": policy})


def test_phase2_batch_export_and_policy_provenance(tmp_path):
    import csv
    from pathlib import Path
    from scripts.run_phase2_batch import fixture
    config_path = Path(__file__).resolve().parents[1] / "configs/d_phase2_smoke.json"
    rows = run_batch(json.loads(config_path.read_text()), tmp_path / "batch", fixture)
    assert len(rows) == 3 and all(r["error"] is None for r in rows)
    assert all(r["summary"]["recovery_policy"] == r["config"]["recovery_policy"] for r in rows)
    manifest = json.loads((tmp_path / "batch/run_0000/manifest.json").read_text())
    assert manifest["recovery_policy"]["checkpoint_retries"] == 0
    assert manifest["experiment"]["recovery_policy"] == manifest["recovery_policy"]
    with (tmp_path / "batch/runs.csv").open() as stream:
        flat = list(csv.DictReader(stream))
    assert len({r["settings_hash"] for r in flat}) == 3
    assert flat[1]["recomputed_node_executions"] == "3"
    assert flat[1]["effective_reused_artifacts"] == "1"
    assert flat[1]["recovery_policy_match"] == "True"
    assert flat[1]["unresolved_effects_count"] == "0"
    assert json.loads(flat[1]["recovery_path"])[0]["event_type"] == "checkpoint_attempt_started"
    assert flat[2]["executed_strategy"] == "full_restart"
    assert all(r["completion"] == "" for r in flat)  # toy correctness is not AgentDojo utility


def test_recovery_reporting_distinguishes_fallback_and_missing_costs(tmp_path):
    import csv
    from experiments.protocol import normalize_recovery_policy
    c = {**configs()[2], "condition": "selective", "recovery_policy": normalize_recovery_policy({})}
    common = dict(recovery_attempted=True, recovery_completed=True, recovery_task_success=True,
                  requested_strategy="selective", recovery_policy=c["recovery_policy"])
    samples = [
        {**common, "executed_strategy": "selective", "unresolved_effects": [],
         "recovery_added_cost": {"model_calls": 2, "input_tokens": 0}},
        {**common, "executed_strategy": "full_restart", "fallback_reason": "points exhausted",
         "unresolved_effects": ["effect1"], "recovery_added_cost": {"model_calls": 4, "input_tokens": None}},
        {"recovery_attempted": True, "recovery_policy": c["recovery_policy"]},
    ]
    rows = [{"config": {**c, "repeat_index": i}, "summary": s, "error": None}
            for i, s in enumerate(samples)]
    report = aggregate(rows, tmp_path)[0]
    assert report["total"] == 3
    assert report["recovery_outcome_denominator"] == 2
    assert report["selective_successes"] == 1 and report["fallback_successes"] == 1
    assert report["full_restart_baseline_successes"] == 0
    assert report["mean_recovery_added_cost_model_calls"] == 3
    assert report["recovery_added_cost_input_tokens_samples"] == 1
    assert report["mean_recovery_added_cost_input_tokens"] == 0
    assert report["unresolved_effects_denominator"] == 2
    assert report["runs_with_unresolved_effects"] == 1
    with (tmp_path / "runs.csv").open() as stream:
        flat = list(csv.DictReader(stream))
    assert flat[2]["unresolved_effects_count"] == ""
    assert flat[2]["checkpoint_attempts"] == ""


def test_actual_policy_mismatch_is_visible_and_not_pooled(tmp_path):
    from experiments.protocol import normalize_recovery_policy
    c = {**configs()[0], "recovery_policy": normalize_recovery_policy({})}
    summaries = [{"recovery_policy": c["recovery_policy"]},
                 {"recovery_policy": normalize_recovery_policy({"checkpoint_retries": 0})}, {}]
    reports = aggregate([{"config": c, "summary": s, "error": None} for s in summaries], tmp_path)
    assert len(reports) == 3
    assert sum(r["policy_mismatches"] for r in reports) == 1


def test_recovery_treatment_does_not_break_clean_control_pairing(tmp_path):
    import csv
    c = configs()[0]
    treatment = {**c, "condition": "selective", "recovery_policy": {"checkpoint_retries": 1}}
    aggregate([{"config": cfg, "summary": {"final_agentdojo_utility": True}, "error": None}
               for cfg in (c, treatment)], tmp_path)
    with (tmp_path / "runs.csv").open() as stream:
        assert list(csv.DictReader(stream))[1]["clean_pair_success"] == "True"
