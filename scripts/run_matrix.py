"""Sequential, resumable local model coverage matrix; never discard failed trials."""
import argparse
import csv
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from workflow.threat_fixtures import definitions


def matrix_cases():
    cases = []
    for task in ('user_task_0', 'user_task_2'):
        cases.append(dict(key=f'clean-{task}', task=task, condition='clean'))
        cases.append(dict(key=f'verify-clean-{task}', task=task, condition='always_verify'))
    for scenario, spec in definitions()['scenarios'].items():
        for condition in ('fault', 'full_restart', 'always_verify'):
            cases.append(dict(key=f'{scenario}-{condition}', task=spec['task_id'], condition=condition,
                              scenario=scenario, site=spec['site']))
    return cases


def read(path, default):
    return json.loads(path.read_text()) if path.exists() else default


def summarize(path, job, exit_code, elapsed):
    score = read(path / 'summary.json', {})
    report = read(path / 'workflow_summary.json', {})
    attempts = report.get('injection_attempts', [])
    cost = score.get('total_cost', {})
    return dict(**job, exit_code=exit_code, wall_seconds=round(elapsed, 3),
        trace_complete=score.get('trace_complete', False), completed=report.get('final_workflow_completed', False),
        initial_strict=score.get('initial_strict_success'), final_strict=score.get('final_strict_success'),
        utility=score.get('final_agentdojo_utility'), applied=any(a.get('applied') for a in attempts),
        injection_reason=attempts[0].get('reason') if attempts else None,
        exposed=score.get('downstream_exposed'), unauthorized_attempts=score.get('unauthorized_attempts'),
        unauthorized_executions=score.get('unauthorized_executions'),
        model_calls=cost.get('model_calls'), tool_calls=cost.get('tool_calls'),
        input_tokens=cost.get('input_tokens'), output_tokens=cost.get('output_tokens'),
        verifier_calls=report.get('verification_cost', {}).get('model_calls'),
        verifier_decisions=report.get('verification_decisions', []),
        errors=report.get('errors', []), run_directory=str(path.resolve()))


def outputs(root, rows):
    (root / 'results.json').write_text(json.dumps(rows, indent=2) + '\n')
    if rows:
        with (root / 'results.csv').open('w') as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows({k: json.dumps(v) if isinstance(v, (list, dict)) else v for k,v in r.items()} for r in rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profiles', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--trial-timeout', type=float, default=300)
    parser.add_argument('--repeat', type=int, default=1)
    args = parser.parse_args()
    if args.repeat < 1 or args.trial_timeout <= 0: parser.error('Positive repeat and timeout required')
    profiles = json.loads(args.profiles.read_text())
    args.output.mkdir(parents=True, exist_ok=True)
    source_files = sorted([*ROOT.glob('workflow/*.py'), *ROOT.glob('workflow/*.yaml'), ROOT/'scripts/run_workflow.py'])
    hashes = {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in source_files}
    manifest = dict(profiles=profiles, repeat=args.repeat, trial_timeout=args.trial_timeout, source_hashes=hashes,
        cases=matrix_cases(), temperature=0, max_tokens=1536, request_timeout=120,
        note='One complete coverage sweep per repeat. Not a robustness estimate; no semantic C labels inferred.')
    manifest_path = args.output / 'matrix_manifest.json'
    if manifest_path.exists() and read(manifest_path, {}) != manifest:
        raise ValueError('Cannot resume with changed code/configuration; choose a new output root')
    manifest_path.write_text(json.dumps(manifest, indent=2)+'\n')
    rows = read(args.output / 'results.json', [])
    done = {(r['profile'], r['repeat'], r['key']) for r in rows}
    total = len(profiles)*len(matrix_cases())*args.repeat
    for profile in profiles:
        for repeat in range(args.repeat):
            base = args.output / profile['id'] / f'repeat-{repeat}'
            for case in matrix_cases():
                if (profile['id'], repeat, case['key']) in done: continue
                path = base / case['key']
                if path.exists():
                    raise ValueError(f'Unfinished trial retained at {path}; inspect it before resuming')
                base.mkdir(parents=True, exist_ok=True)
                command = [sys.executable, str(ROOT/'scripts/run_workflow.py'), '--task', case['task'],
                    '--condition', case['condition'], '--model', profile['model'], '--base-url', profile['base_url'],
                    '--plan-format', profile.get('plan_format','json_schema'), '--output', str(path)]
                clean = base / f"clean-{case['task']}"
                if case['condition'] != 'clean' and read(clean/'summary.json', {}).get('trace_complete'):
                    command += ['--matched-clean', str(clean)]
                if 'scenario' in case:
                    command += ['--injector','workflow.threat_fixtures:make_injector', '--scenario-id',case['scenario'],
                                '--injection-site',case['site']]
                job = dict(profile=profile['id'], model=profile['model'], repeat=repeat, **case)
                # A fixed superset keeps CSV columns stable for clean/fault cases.
                job.setdefault('scenario', None); job.setdefault('site', None)
                (args.output/'current.json').write_text(json.dumps(dict(**job, command=command)))
                tick = time.monotonic()
                with (base/(case['key']+'.console.log')).open('w') as log:
                    try:
                        result = subprocess.run(command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, timeout=args.trial_timeout)
                        code = result.returncode
                    except subprocess.TimeoutExpired:
                        code = 124
                row = summarize(path, job, code, time.monotonic()-tick)
                rows.append(row); outputs(args.output, rows)
                print(f"{len(rows)}/{total} {profile['id']} {case['key']} completed={row['completed']} strict={row['final_strict']} applied={row['applied']} {row['wall_seconds']}s", flush=True)
    (args.output/'complete.json').write_text(json.dumps({'completed_trials':len(rows),'planned_trials':total}))


if __name__ == '__main__': main()
