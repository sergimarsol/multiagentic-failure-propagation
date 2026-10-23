"""Run every pinned Banking query once, clean, with native AgentDojo utility."""
import argparse
import csv
import hashlib
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from env.banking_env import get_banking_suite, get_user_task
from env.state import as_env
from experiments.phase1 import run_phase1
from workflow import BankingWorkflowSession, ModelConfig, OpenAICompatibleClient, WorkflowConfig
from workflow.integration import BankingEnvironmentAdapter, workflow_report


def final_answer(session):
    generations = session.histories.get('executor', [])
    if not generations:
        return ''
    message = generations[-1][-1]
    if message.get('role') != 'assistant' or message.get('tool_calls'):
        return ''
    return message.get('content') or ''


class NativeUtilityAdapter(BankingEnvironmentAdapter):
    """Do not fabricate strict authorization rules for unconfigured tasks."""
    def __init__(self, task_id, session):
        super().__init__(task_id)
        self.session = session

    def evaluate_execution(self, initial_state, final_state, trace):
        answer = final_answer(self.session)
        utility = bool(get_user_task(self.task_id).utility(
            answer, as_env(initial_state), as_env(final_state), strict=True))
        return dict(task_success=utility, agentdojo_utility=utility, strict_success=None,
                    permission_violations=[e for e in trace if not e['allowed'] and e.get('kind') != 'write'],
                    unauthorized_attempt_ids=None, unauthorized_execution_ids=None,
                    evidence_ids=[e['call_id'] for e in trace],
                    details={'scoring': 'AgentDojo.native_utility', 'final_answer': answer,
                             'strict_safety': 'not_evaluated',
                             'utility_is_trivial': self.task_id == 'user_task_8'})


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--model', required=True)
    p.add_argument('--base-url', default='http://127.0.0.1:11434/v1')
    p.add_argument('--plan-format', choices=['json_schema','prompt_only'], default='json_schema')
    p.add_argument('--output', type=Path, required=True)
    args = p.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    tasks = sorted(get_banking_suite().user_tasks, key=lambda k: int(k.rsplit('_',1)[1]))
    model = ModelConfig(model=args.model, base_url=args.base_url, plan_format=args.plan_format)
    config = WorkflowConfig()
    files = sorted([*ROOT.glob('workflow/*.py'), *ROOT.glob('env/*.py'),
                    *ROOT.glob('experiments/*.py'), Path(__file__)])
    manifest = dict(model=model.public_dict(), tasks=[{'id':t,'prompt':get_user_task(t).PROMPT} for t in tasks],
                    benchmark='banking@v1.2.2', condition='clean', repeat=1,
                    scoring='workflow_completed AND AgentDojo native utility(strict=True)',
                    source_hashes={str(f.relative_to(ROOT)):hashlib.sha256(f.read_bytes()).hexdigest() for f in files})
    (args.output/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
    rows=[]
    for task in tasks:
        tick=time.monotonic()
        session=BankingWorkflowSession(OpenAICompatibleClient(model),config)
        path=args.output/task
        score=run_phase1(path,task_id=task,condition='clean',
            environment_adapter=NativeUtilityAdapter(task,session),create_session=lambda:session,synthetic=False)
        report=workflow_report(path)
        answer=final_answer(session)
        (path/'final_answer.txt').write_text(answer+'\n')
        row=dict(task=task,prompt=get_user_task(task).PROMPT,
                 success=report['final_workflow_completed'] and score['final_agentdojo_utility'] is True,
                 completed=report['final_workflow_completed'],utility=score['final_agentdojo_utility'],
                 trivial_utility=task=='user_task_8',wall_seconds=round(time.monotonic()-tick,3),
                 errors=report['errors'],final_answer=answer)
        rows.append(row)
        (args.output/'results.json').write_text(json.dumps(rows,indent=2)+'\n')
        with (args.output/'results.csv').open('w') as f:
            writer=csv.DictWriter(f,fieldnames=list(row));writer.writeheader();writer.writerows(rows)
        print(f'{len(rows)}/{len(tasks)} {task} success={row["success"]} completed={row["completed"]} {row["wall_seconds"]}s',flush=True)
    (args.output/'complete.json').write_text(json.dumps({'success':sum(r['success'] for r in rows),'total':len(rows)},indent=2)+'\n')


if __name__=='__main__':
    main()
