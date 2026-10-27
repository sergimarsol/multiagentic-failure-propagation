"""D/B integration using B's real session and A's simulator, never inference."""
import copy
import csv
import json
from pathlib import Path
import pytest
from experiments.workflow_bridge import execute_workflow, workflow_batch, prepare_config, summarize_workflow_runs
from experiments.batch import aggregate
from tests.test_workflow import FakeModel, response


def config(condition='clean', task='user_task_0'):
    c = dict(task_id=task, condition=condition, scenario_id='clean', comparison_group_id=task,
             repeat_index=0, synthetic=True,
             workflow=dict(model=dict(model='fixture', base_url='http://127.0.0.1:11434/v1'),
                           runtime='scripted-test-client', session={}),
             limits=dict(model_calls=40, tool_calls=30, wall_seconds=300, tokens=None))
    if condition not in {'clean', 'clean_verify'}:
        c.update(scenario_id='t0_rp_accidental_bill_amount' if task == 'user_task_0' else 't2_rp_accidental_rent_increase',
                 fault=dict(family='accidental', site='research_to_plan',
                            payload_ref='workflow/fixture_payloads.yaml', fixture=True,
                            injector_factory='workflow.threat_fixtures:make_injector'))
    return c


def factory(model):
    client = FakeModel()
    client.config = model
    return client


def read_csv(path):
    return list(csv.DictReader(path.open()))


def events(path):
    return [json.loads(x) for x in (path/'events.jsonl').read_text().splitlines()]


@pytest.mark.parametrize('task', ['user_task_0', 'user_task_2'])
def test_five_conditions_use_real_b_session_and_shared_reports(tmp_path, task):
    conditions = ['clean', 'fault', 'full_restart', 'clean_verify', 'always_verify']
    rows = workflow_batch([config(c, task) for c in conditions], tmp_path/'batch', client_factory=factory)
    assert all(r['error'] is None for r in rows)
    clean, faulty, restarted, verified, defended = [r['summary'] for r in rows]
    assert clean['final_strict_success'] and verified['final_strict_success']
    assert faulty['initial_strict_success'] is False
    assert restarted['initial_strict_success'] is False and restarted['final_strict_success']
    assert restarted['unauthorized_executions'] == 1
    assert restarted['cross_attempt_repeats']['status'] == 'evaluated'
    assert defended['verification_cost']['model_calls'] > 0
    for i, row in enumerate(rows):
        summary = row['summary']
        assert summary['trace_complete'] and summary['final_workflow_completed']
        assert summary['propagation'] == []  # C has not annotated anything.
        calls = [e for e in events(tmp_path/'batch'/f'run_{i:04d}') if e['event_type']=='model_started']
        assert len(calls) == summary['total_cost']['model_calls']
        assert summary['budget']['calls']['model'] == len(calls)
        assert all(e['data']['model'] == row['config']['workflow']['model'] for e in calls)
    table = read_csv(tmp_path/'batch'/'runs.csv')
    assert {r['condition'] for r in table} == set(conditions)
    assert all(r['clean_pair_status']=='matched' for r in table)
    assert all(r['propagated']=='' for r in table)


def test_restart_cannot_refill_trial_budget(tmp_path):
    c = config('full_restart'); c['limits']['model_calls'] = 6
    s = execute_workflow(c, tmp_path/'run', client_factory=factory)
    assert s['budget_exhausted'] and s['budget']['calls']['model'] == 6
    assert s['total_cost']['model_calls'] == 6
    assert len(s['attempts']) == 2 and s['attempts'][1]['cost']['model_calls'] == 1
    assert s['unauthorized_executions'] == 1 and not s['final_workflow_completed']
    assert s['trace_complete'] and s['execution_errors']


def test_verifier_reads_counted_once_and_budget_applies(tmp_path):
    class ReadingVerifier(FakeModel):
        def complete(self, messages, tools, *, json_output=False):
            if 'You are the Verifier.' in messages[0]['content'] and messages[-1]['role'] != 'tool':
                return response(calls=[('read_file', {'file_path':'bill-december-2023.txt'})])
            return super().complete(messages, tools, json_output=json_output)
    def build(m):
        v=ReadingVerifier(); v.config=m; return v
    c=config('clean_verify')
    s=execute_workflow(c,tmp_path/'ok',client_factory=build)
    assert s['verification_cost']['tool_calls']==2
    assert s['verification_cost']['model_calls']==4
    assert s['total_cost']['tool_calls']==4
    c['limits']['model_calls']=3
    s=execute_workflow(c,tmp_path/'limited',client_factory=build)
    assert s['budget_exhausted'] and s['total_cost']['model_calls']==3
    assert s['verification_cost']['model_calls']==1


def test_retry_and_unknown_usage_are_recorded(tmp_path):
    def build(m):
        v=FakeModel(fail_once=True); v.config=m; return v
    c=config(); c['workflow']['session']['request_retries']=1
    s=execute_workflow(c,tmp_path/'retry',client_factory=build)
    assert s['final_strict_success'] and s['retry_cost']['model_calls']==1
    assert s['total_cost']['model_calls']==6
    assert s['total_cost']['input_tokens'] is None
    c['limits']['tokens']=1000
    s=execute_workflow(c,tmp_path/'unknown',client_factory=build)
    assert s['budget_exhausted'] and s['total_cost']['model_calls']==1
    assert s['budget']['calls_missing_usage']==1


@pytest.mark.parametrize('mutation', ['model','prompt','initial','session','seed','payload','family'])
def test_configuration_mismatch_rejected_before_calls(tmp_path, mutation):
    c=config('fault')
    if mutation=='model': c['workflow']['model']['unknown_option']=1
    if mutation=='prompt': c['workflow']['prompt_hash']='wrong'
    if mutation=='initial': c['initial_state_hash']='wrong'
    if mutation=='session': c['workflow']['session']['unknown_option']=1
    if mutation=='seed': c['workflow']['model']['seed']=1
    if mutation=='payload': c['fault']['payload_hash']='wrong'
    if mutation=='family': c['fault']['family']='adversarial'
    with pytest.raises((ValueError, TypeError)):
        execute_workflow(c,tmp_path/'run',client_factory=lambda m: pytest.fail('must not build client'))


def test_actual_client_mismatch_rejected(tmp_path):
    c=config(); c['workflow']['model']['model']='different'
    with pytest.raises(ValueError,match='actual client'):
        execute_workflow(c,tmp_path/'run',client_factory=lambda m: FakeModel())


def test_pair_mismatch_and_duplicate_run_import(tmp_path):
    clean=config(); fault=config('fault'); fault['limits']['model_calls']=39
    rows=workflow_batch([clean,fault],tmp_path/'batch',client_factory=factory)
    table=read_csv(tmp_path/'batch'/'runs.csv')
    assert table[1]['clean_pair_status']=='missing_or_mismatched_controls'
    assert table[1]['clean_pair_success']==''
    with pytest.raises(ValueError,match='duplicate source'):
        aggregate([rows[0],rows[0]],tmp_path/'dup')
    a=tmp_path/'batch'/'run_0000'
    before=(a/'summary.json').read_bytes()
    imported=summarize_workflow_runs([a],tmp_path/'import')
    assert imported[0]['summary']['run_id']==rows[0]['summary']['run_id']
    assert (a/'summary.json').read_bytes()==before
    with pytest.raises(ValueError,match='duplicate source'):
        summarize_workflow_runs([a,a],tmp_path/'invalid')


def test_incomplete_trace_retained_by_readonly_import(tmp_path):
    run=tmp_path/'run'; execute_workflow(config(),run,client_factory=factory)
    # Simulate process loss: retain the prefix through a model start, no terminal event.
    es=events(run); cut=next(i for i,e in enumerate(es) if e['event_type']=='model_started')
    (run/'events.jsonl').write_text(''.join(json.dumps(e)+'\n' for e in es[:cut+1]))
    (run/'artifacts.jsonl').write_text(''); (run/'evaluations.jsonl').write_text('')
    s=summarize_workflow_runs([run],tmp_path/'out')[0]['summary']
    assert not s['trace_complete'] and s['final_workflow_completed'] is None
    assert s['total_cost']['input_tokens'] is None


def test_untriggered_fault_is_not_recovery_or_propagation(tmp_path, monkeypatch):
    from workflow.contracts import InjectionSpec
    monkeypatch.setattr('experiments.workflow_bridge._injector', lambda c:
        (lambda content,site: {'applied':False,'reason':'target_missing'}, InjectionSpec(c['scenario_id'],'research')))
    s=execute_workflow(config('full_restart'),tmp_path/'run',client_factory=factory)
    assert s['injection_triggered'] is False
    assert not s['recovery_attempted'] and len(s['attempts'])==1
    assert s['propagation']==[]


def test_session_limits_still_apply_and_model_settings_are_actual(tmp_path):
    c=config(); c['workflow']['session']['max_model_calls']=1
    c['workflow']['model'].update(temperature=0.2,max_tokens=123,plan_format='prompt_only')
    s=execute_workflow(c,tmp_path/'run',client_factory=factory)
    assert not s['final_workflow_completed'] and s['total_cost']['model_calls']==1
    assert s['budget']['limits']['model_calls']==40  # session ceiling was hit first
    calls=[e for e in events(tmp_path/'run') if e['event_type']=='model_started']
    assert calls[0]['data']['model']['max_tokens']==123
    assert calls[0]['data']['model']['temperature']==0.2


def test_mismatched_model_and_state_excluded_from_pairs(tmp_path):
    rows=workflow_batch([config(),config('fault')],tmp_path/'batch',client_factory=factory)
    mismatch=copy.deepcopy(rows)
    mismatch[1]['config']['workflow']['model']['temperature']=0.5
    aggregate(mismatch,tmp_path/'model')
    assert read_csv(tmp_path/'model'/'runs.csv')[1]['clean_pair_success']==''
    mismatch=copy.deepcopy(rows); mismatch[1]['summary']['initial_state_hash']='other'
    aggregate(mismatch,tmp_path/'state')
    assert read_csv(tmp_path/'state'/'runs.csv')[1]['clean_pair_status']=='state_mismatch'


def test_postprocessing_failure_retains_trace_cost(tmp_path, monkeypatch):
    def broken(directory):
        raise ValueError('report failed')
    monkeypatch.setattr('experiments.workflow_bridge.workflow_report',broken)
    rows=workflow_batch([config()],tmp_path/'batch',client_factory=factory)
    assert 'report failed' in rows[0]['error']
    assert rows[0]['summary']['total_cost']['model_calls']==5
    assert rows[0]['summary']['trace_complete']


def test_clean_config_does_not_reveal_fault_oracle_to_model(tmp_path):
    clients=[]
    def build(m):
        v=factory(m); clients.append(v); return v
    execute_workflow(config('fault'),tmp_path/'run',client_factory=build)
    payload=json.dumps(clients[0].requests)
    for forbidden in ('comparison_group_id','payload_hash','injection_oracle','scenario_id'):
        assert forbidden not in payload
