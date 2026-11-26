"""Separate clean-run failures, pre-injection mistakes and observed propagation.

Offline only: never supplies task answers to a model or rewrites source traces.
The supplied clean run is an independent comparator, not a shared-prefix causal
control. Missing evidence remains unknown; final success is not proof of defense.

  python scripts/analyze_error_sources.py --clean RUN --injected RUN --output DIR
"""
import argparse
import csv
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from env.tasks import TASKS_PATH, load_task_configs
from experiments.metrics import summarize_run
from scripts.analyze_injection_trace import analyze_run, numeric_eq


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def events(path):
    return [json.loads(line) for line in (path / 'events.jsonl').read_text(encoding='utf-8').splitlines()]


def task_outcome(summary):
    # A false task score after an API/runtime failure is not a model-only failure.
    healthy = (summary.get('trace_complete') is True
               and summary.get('final_workflow_completed') is True
               and not summary.get('execution_errors')
               and summary.get('total_cost', {}).get('failed_calls') == 0)
    strict = summary.get('final_strict_success')
    return {'strict_success': strict, 'execution_healthy': healthy,
            'task_error': not strict if healthy and type(strict) is bool else None,
            'execution_errors': summary.get('execution_errors', []),
            'failed_calls': summary.get('total_cost', {}).get('failed_calls')}


def fact_check(boundary, task):
    """Judge only present, known research fields, never all stage semantics."""
    facts = (boundary.get('original') or {}).get('facts')
    if boundary.get('site') != 'research' or not isinstance(facts, dict):
        return {'known_fact_error': None, 'checks': [], 'scope': 'research fields only'}
    checks = []
    expected = [f for f in task.critical_facts if f.source.get('tool') != 'derived']
    for fact in expected:
        if fact.name not in facts:
            continue
        actual = facts[fact.name]
        match = (numeric_eq(actual, fact.value) if isinstance(fact.value, (int, float))
                 else type(actual) is type(fact.value) and actual == fact.value)
        checks.append({'field': fact.name, 'actual': actual, 'expected': fact.value,
                       'matches': match, 'reference_source': fact.source,
                       'artifact_id': boundary['original_artifact_id'],
                       'boundary_event_id': boundary['event_id']})
    return {'known_fact_error': any(not c['matches'] for c in checks) if checks else None,
            'checks': checks, 'missing_fields': [f.name for f in expected if f.name not in facts],
            'scope': 'Present known research facts only; notes and omitted fields are not judged.'}


def compare(clean, injected, *, task_spec=TASKS_PATH):
    clean, injected = Path(clean).resolve(), Path(injected).resolve()
    cm, fm = read(clean / 'manifest.json'), read(injected / 'manifest.json')
    if cm['condition'] not in {'clean', 'clean_verify'} or cm['task_id'] != fm['task_id']:
        raise ValueError('Comparator must be clean/clean_verify with the same task')
    if fm['condition'] in {'clean', 'clean_verify'} or cm['run_id'] == fm['run_id']:
        raise ValueError('Supply distinct clean and fault-condition runs')
    ce, fe = events(clean), events(injected)
    cs, fs = summarize_run(clean, write=False), summarize_run(injected, write=False)
    if cs.get('injection_triggered') is True:
        raise ValueError('Clean comparator contains an applied injection')
    configs = lambda es: [e['data'] for e in es if e['event_type'] == 'workflow_config']
    cc, fc = configs(ce), configs(fe)
    config_match = bool(cc and fc) and all(c == cc[0] for c in cc + fc)
    cx, fx = cm.get('experiment') or {}, fm.get('experiment') or {}
    cw, fw = cx.get('workflow', {}), fx.get('workflow', {})
    source_match = cw['source_hash'] == fw['source_hash'] if cw.get('source_hash') and fw.get('source_hash') else None
    controls_match = (all(cx.get(k) == fx.get(k) for k in
                         ('limits', 'repeat_index', 'comparison_group_id'))
                      and cw.get('runtime') == fw.get('runtime')) if cx and fx else None
    state_match = (cs['initial_state_hash'] == fs['initial_state_hash']
                   if cs.get('initial_state_hash') and fs.get('initial_state_hash') else None)
    policy_match = (cm['condition'] == 'clean_verify') == (fm['condition'] == 'always_verify')
    controls = [config_match, source_match, controls_match, state_match, policy_match,
                cm.get('synthetic') == fm.get('synthetic')]
    comparable = False if False in controls else True if all(x is True for x in controls) else None
    pair = {'configuration_matches': config_match, 'source_matches': source_match,
            'experiment_controls_match': controls_match,
            'initial_state_matches': state_match, 'verification_policy_matches': policy_match,
            'comparable': comparable, 'kind': 'independent_runs',
            'causal_attribution': 'not_established'}
    spec = Path(task_spec)
    task = load_task_configs(spec)[cm['task_id']]
    bundle = read(injected / 'fault_cases.json')
    # C's existing analyzer validates bundle identity and retains provisional labels.
    analysis = analyze_run(injected, create_evidence=False)
    by_attempt = {a['attempt_id']: a for a in bundle['attempts']}
    cases = []
    for observation in analysis:
        attempt = by_attempt[observation['attempt_id']]
        selected = [b for b in attempt['boundaries'] if b.get('applied') is True]
        boundary = selected[0] if len(selected) == 1 else None
        pre = fact_check(boundary, task) if boundary else {
            'known_fact_error': None, 'checks': [], 'scope': 'No unique applied boundary'}
        strict = observation['a_strict_success']
        cancel = (fs.get('trace_complete') is True
                  and pre['known_fact_error'] is True and strict is True
                  and observation['injection_valid'] is True
                  and observation['adopted_in_output'] is True
                  and observation['relevant_state_changed'] is True)
        cases.append({'attempt_id': observation['attempt_id'],
            'scenario_id': observation['scenario_id'],
            'pre_injection': pre,
            'injection_applied': observation['injection_applied'],
            'injection_valid': observation['injection_valid'],
            'consumer_exposed': observation['consumer_exposed'],
            'injection_adopted': observation['adopted_in_output'],
            'attempt_strict_success': strict,
            'relevant_state_changed': observation['relevant_state_changed'],
            'relevant_unauthorized_execution_ids': sorted(set(observation['relevant_audit_call_ids']) &
                set(observation['a_unauthorized_execution_ids'] or []))
                if observation['a_unauthorized_execution_ids'] is not None else None,
            'error_cancellation_candidate': True if cancel else None,
            'review_required': True,
            'propagation_assessment': observation['suggested_outcome'],
            'evidence_ids': observation['evidence_ids']})
    return {'schema': 'b-error-observations-v1', 'task_id': cm['task_id'],
        'clean': {'run_id': cm['run_id'], 'directory': str(clean), **task_outcome(cs)},
        'injected': {'run_id': fm['run_id'], 'directory': str(injected), **task_outcome(fs)},
        'comparison': pair, 'attempts': cases,
        'offline_reference': {'path': str(spec.resolve()), 'sha256': hashlib.sha256(spec.read_bytes()).hexdigest()},
        'notes': ['Clean-run failure and pre-injection error in the injected run are separate observations.',
                  'task_error=None means unknown, not success. A clean failure is not proof of the same error in another run.',
                  'No observed post-injection failure is automatically classified as caused by injection.',
                  'Fact checks use the supplied offline A specification; they never enter model inputs.',
                  'Successful final scoring with adopted corruption may reflect error cancellation; inspect cited evidence.']}


def write_report(report, output):
    output = Path(output)
    # Preserve source trace directories; reports must use a fresh output directory.
    for source in (Path(report['clean']['directory']), Path(report['injected']['directory'])):
        if output.resolve() == source or source in output.resolve().parents:
            raise ValueError('Output must be separate from source traces')
    output.mkdir(parents=True, exist_ok=False)
    (output / 'error_observations.json').write_text(json.dumps(report, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    rows = [{'task_id': report['task_id'], 'clean_run_id': report['clean']['run_id'],
        'injected_run_id': report['injected']['run_id'],
        'clean_task_error': report['clean']['task_error'],
        'injected_task_error': report['injected']['task_error'],
        'comparator_matches': report['comparison']['comparable'],
        'causal_attribution': report['comparison']['causal_attribution'],
        **{k: case[k] for k in ('attempt_id', 'scenario_id', 'injection_applied', 'injection_adopted',
                               'attempt_strict_success', 'error_cancellation_candidate')},
        'pre_injection_known_fact_error': case['pre_injection']['known_fact_error']}
        for case in report['attempts']]
    if rows:
        with (output / 'error_observations.csv').open('w', newline='', encoding='utf-8') as stream:
            writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--clean', type=Path, required=True)
    p.add_argument('--injected', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--task-spec', type=Path, default=TASKS_PATH)
    a = p.parse_args()
    write_report(compare(a.clean, a.injected, task_spec=a.task_spec), a.output)
    print(f'Offline error observations saved to {a.output}')


if __name__ == '__main__':
    main()
