from scripts.run_matrix import matrix_cases
from workflow.client import ModelConfig, OpenAICompatibleClient
import json


def test_matrix_covers_all_conditions_and_eight_payloads():
    cases = matrix_cases()
    assert len(cases) == len({c['key'] for c in cases}) == 28
    assert sum(c['condition'] == 'clean' for c in cases) == 2
    assert sum(c['condition'] == 'always_verify' and 'scenario' not in c for c in cases) == 2
    for scenario in {c['scenario'] for c in cases if 'scenario' in c}:
        assert {c['condition'] for c in cases if c.get('scenario') == scenario} == {'fault','full_restart','always_verify'}


def test_prompt_only_does_not_claim_provider_json_constraint(monkeypatch):
    from workflow import client as module
    payloads = []
    class Reply:
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def read(self):return b'{}'
    def urlopen(request, timeout):
        payloads.append(json.loads(request.data))
        return Reply()
    monkeypatch.setattr(module,'urlopen',urlopen)
    client=OpenAICompatibleClient(ModelConfig(plan_format='prompt_only'))
    client.complete([{'role':'user','content':'Plan'}],[],json_output=True)
    assert 'response_format' not in payloads[0]
    assert client.config.public_dict()['plan_format']=='prompt_only'


def test_report_retains_known_usage_when_total_usage_is_unknown(tmp_path):
    from scripts.report_matrix import build
    root=tmp_path/'ollama';root.mkdir()
    trial=root/'trial';trial.mkdir()
    (root/'matrix_manifest.json').write_text(json.dumps({'profiles':[{'id':'example'}],'cases':matrix_cases(),'repeat':1}))
    row={'profile':'example','key':'clean-user_task_0','condition':'clean','scenario':None,
         'completed':False,'trace_complete':True,'final_strict':False,'initial_strict':False,
         'applied':False,'injection_reason':'disabled','unauthorized_executions':0,'errors':[],
         'model_calls':2,'tool_calls':0,'verifier_calls':0,'input_tokens':None,'output_tokens':None,
         'wall_seconds':1.0,'run_directory':str(trial)}
    (root/'results.json').write_text(json.dumps([row]))
    (trial/'summary.json').write_text(json.dumps({'total_cost':{'known_input_tokens':23,
         'known_output_tokens':5,'calls_missing_usage':1}}))
    report=build(tmp_path).read_text()
    assert '| example | 2 | 0 | 0 | 23 | 5 | 1 | 1.0 |' in report
