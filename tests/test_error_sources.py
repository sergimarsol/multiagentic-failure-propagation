"""Offline attribution must not confuse native mistakes, faults, and recovery."""
import json

import pytest

from env.tasks import get_task_config
from faults.injection_harness import make_injector
from scripts.analyze_error_sources import compare, fact_check, task_outcome, write_report
from tests.test_workflow import FakeModel, run
from workflow.contracts import InjectionSpec


class WrongResearch(FakeModel):
    def complete(self, messages, tools, *, json_output=False):
        response = super().complete(messages, tools, json_output=json_output)
        message = response['choices'][0]['message']
        if 'You are the Researcher.' in messages[0]['content'] and not message.get('tool_calls'):
            content = json.loads(message['content'])
            content['facts']['current_rent'] = 1000
            message['content'] = json.dumps(content)
        return response


def pair(tmp_path, model=FakeModel, condition='fault'):
    task, scenario = 'user_task_2', 't2_rp_accidental_rent_increase'
    clean, fault = tmp_path / 'clean', tmp_path / 'fault'
    run(clean, task=task, model=model())
    run(fault, task=task, model=model(), condition=condition,
        injector=make_injector(task_id=task, scenario_id=scenario),
        spec=InjectionSpec(scenario, 'research'))
    return clean, fault


def test_clean_success_and_injected_failure_are_not_automatic_causal_proof(tmp_path):
    clean, fault = pair(tmp_path)
    report = compare(clean, fault)
    assert report['clean']['task_error'] is False
    assert report['injected']['task_error'] is True
    case = report['attempts'][0]
    assert case['pre_injection']['known_fact_error'] is False
    assert case['injection_adopted'] is True
    assert case['relevant_unauthorized_execution_ids']
    assert report['comparison']['causal_attribution'] == 'not_established'
    assert report['comparison']['source_matches'] is None  # old traces lack this control


def test_wrong_native_fact_plus_injected_fact_can_cancel(tmp_path):
    clean, fault = pair(tmp_path, WrongResearch)
    report = compare(clean, fault)
    assert report['clean']['task_error'] is True
    assert report['injected']['task_error'] is False
    case = report['attempts'][0]
    assert case['pre_injection']['known_fact_error'] is True
    assert case['injection_adopted'] is True
    assert case['error_cancellation_candidate'] is True
    assert case['review_required'] is True
    error = next(c for c in case['pre_injection']['checks'] if not c['matches'])
    assert (error['field'], error['actual'], error['expected']) == ('current_rent', 1000, 1100)
    assert error['artifact_id'] and error['boundary_event_id']


def test_verification_mismatch_is_not_a_matched_control(tmp_path):
    clean, fault = pair(tmp_path, condition='always_verify')
    report = compare(clean, fault)
    assert report['comparison']['verification_policy_matches'] is False
    assert report['comparison']['comparable'] is False


def test_unapplied_fault_is_not_recorded_as_adoption_or_cancellation(tmp_path):
    clean, fault = tmp_path / 'clean', tmp_path / 'fault'
    run(clean)
    run(fault, condition='fault', injector=lambda content, site: {'triggered': False})
    report = compare(clean, fault)
    case = report['attempts'][0]
    assert case['injection_applied'] is False
    assert case['injection_adopted'] is None
    assert case['pre_injection']['known_fact_error'] is None
    assert case['error_cancellation_candidate'] is None


def test_prompt_mismatch_and_foreign_task_are_detected(tmp_path):
    clean, fault = pair(tmp_path)
    path = clean / 'events.jsonl'
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    next(e for e in rows if e['event_type'] == 'workflow_config')['data']['prompt_hashes']['planner'] = 'other'
    path.write_text(''.join(json.dumps(e) + '\n' for e in rows))
    assert compare(clean, fault)['comparison']['comparable'] is False
    manifest = json.loads((clean / 'manifest.json').read_text())
    manifest['task_id'] = 'user_task_0'
    (clean / 'manifest.json').write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match='same task'):
        compare(clean, fault)


@pytest.mark.parametrize('change', [
    {'trace_complete': False}, {'final_workflow_completed': False},
    {'execution_errors': [{'error': 'transport failed'}]},
    {'total_cost': {'failed_calls': 1}}, {'final_strict_success': None},
])
def test_runtime_failures_or_missing_scores_are_not_model_errors(change):
    summary = {'trace_complete': True, 'final_workflow_completed': True,
               'execution_errors': [], 'total_cost': {'failed_calls': 0}, 'final_strict_success': False}
    assert task_outcome({**summary, **change})['task_error'] is None


def test_missing_and_unchecked_fields_are_not_declared_correct():
    task = get_task_config('user_task_2')
    boundary = {'site': 'research', 'original_artifact_id': 'a', 'event_id': 'e',
                'original': {'facts': {}}}
    assert fact_check(boundary, task)['known_fact_error'] is None
    boundary['original']['facts'] = {'rent_increase': 100}
    check = fact_check(boundary, task)
    assert check['known_fact_error'] is False  # only this present field was checked
    assert 'current_rent' in check['missing_fields']
    boundary['site'] = 'plan'
    assert fact_check(boundary, task)['known_fact_error'] is None


def test_report_preserves_source_traces_and_emits_machine_readable_rows(tmp_path):
    clean, fault = pair(tmp_path, WrongResearch)
    paths = list(clean.glob('*.json*')) + list(fault.glob('*.json*'))
    before = {p: p.read_bytes() for p in paths}
    report = compare(clean, fault)
    output = tmp_path / 'analysis'
    write_report(report, output)
    assert json.loads((output / 'error_observations.json').read_text()) == report
    assert 'pre_injection_known_fact_error' in (output / 'error_observations.csv').read_text()
    assert all(p.read_bytes() == data for p, data in before.items())
    with pytest.raises(ValueError, match='separate'):
        write_report(report, fault / 'analysis')
    with pytest.raises(FileExistsError):
        write_report(report, output)
