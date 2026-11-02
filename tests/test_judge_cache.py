"""Exact-request caching tests; never connect to a model service."""
import json
from dataclasses import replace

import pytest

from experiments.judge_cache import JudgeCache, request_identity, seed_cache
from experiments.models import fingerprint
from experiments.work_judgments import LLMJudge, RUBRIC, analyze_work
from tests.test_work_judgments import ScriptedJudge
from tests.test_workflow_bridge import config, factory
from experiments.workflow_bridge import workflow_batch


def packet():
    return {'node_execution_id': 'exec1', 'original_output_ids': ['a1'],
            'artifacts': [{'artifact_id': 'a1', 'content': 'sample'}], 'events': [], 'a_scoring': []}


class NoNetwork(ScriptedJudge):
    def complete(self, *args, **kwargs):
        raise AssertionError('Cache test must never request inference')


def test_replay_is_identical_and_costs_no_tokens(tmp_path):
    cache = JudgeCache(tmp_path/'cache')
    first = LLMJudge(ScriptedJudge(), max_calls=1, cache=cache)
    original, error = first.judge(packet())
    assert not error and first.calls[0]['input_tokens']==10
    replay = LLMJudge(NoNetwork(), max_calls=0, cache=cache, cache_only=True)
    result, error = replay.judge(packet())
    assert result==original and not error
    assert replay.calls[0]['cache_hit'] is True
    assert replay.calls[0]['input_tokens']==replay.calls[0]['output_tokens']==0
    assert replay.calls[0]['response']['usage']['prompt_tokens']==10


@pytest.mark.parametrize('field,value', [('model','different'),('base_url','https://other.example/v1'),
    ('temperature',0.4),('max_tokens',2048),('timeout_seconds',30)])
def test_model_settings_must_match(tmp_path,field,value):
    cache=JudgeCache(tmp_path/'cache')
    LLMJudge(ScriptedJudge(),max_calls=1,cache=cache).judge(packet())
    client=NoNetwork(); client.config=replace(client.config,**{field:value})
    replay=LLMJudge(client,max_calls=0,cache=cache,cache_only=True)
    assert replay.judge(packet())==(None,'cache_miss_offline')
    assert replay.calls==[]


def test_evidence_and_rubric_changes_miss(tmp_path,monkeypatch):
    cache=JudgeCache(tmp_path/'cache')
    LLMJudge(ScriptedJudge(),max_calls=1,cache=cache).judge(packet())
    replay=LLMJudge(NoNetwork(),max_calls=0,cache=cache,cache_only=True)
    p=packet();p['artifacts'][0]['content']='changed'
    assert replay.judge(p)==(None,'cache_miss_offline')
    monkeypatch.setattr('experiments.work_judgments.RUBRIC',RUBRIC+' changed')
    assert replay.judge(packet())==(None,'cache_miss_offline')


def test_hits_do_not_consume_api_budget(tmp_path):
    cache=JudgeCache(tmp_path/'cache')
    judge=LLMJudge(ScriptedJudge(),max_calls=1,cache=cache)
    assert judge.judge(packet())[1] is None
    assert judge.judge(packet())[1] is None
    p=packet();p['node_execution_id']='new'
    assert judge.judge(p)==(None,'judge_budget_exhausted')
    assert len(judge.calls)==2 and judge.calls[1]['cache_hit']


def test_failed_and_corrupt_responses_are_not_reused(tmp_path):
    cache=JudgeCache(tmp_path/'cache')
    bad=LLMJudge(ScriptedJudge('foreign'),max_calls=1,cache=cache)
    assert bad.judge(packet())[0] is None
    assert list(cache.directory.glob('*.json'))==[]
    LLMJudge(ScriptedJudge(),max_calls=1,cache=cache).judge(packet())
    entry=next(cache.directory.glob('*.json'));entry.write_text('{broken')
    replay=LLMJudge(NoNetwork(),max_calls=0,cache=cache,cache_only=True)
    assert replay.judge(packet())==(None,'cache_miss_offline')


def test_import_old_logs_and_recompute_metrics_without_network(tmp_path):
    workflow_batch([config()],tmp_path/'batch',client_factory=factory)
    run=tmp_path/'batch/run_0000'
    report,summary=analyze_work(run,tmp_path/'old',judge=LLMJudge(ScriptedJudge(),max_calls=20))
    # Match the previously produced log format without cache metadata.
    calls=json.loads((tmp_path/'old/judge_calls.json').read_text())
    for c in calls:
        c.pop('cache_key',None);c.pop('cache_hit',None)
    (tmp_path/'old/judge_calls.json').write_text(json.dumps(calls))
    cache=JudgeCache(tmp_path/'cache')
    assert seed_cache(cache,tmp_path/'old')['imported']==len(calls)
    assert seed_cache(cache,tmp_path/'old')['existing']==len(calls)
    judge=LLMJudge(NoNetwork(),max_calls=0,cache=cache,cache_only=True)
    new,derived=analyze_work(run,tmp_path/'replay',judge=judge)
    assert new['judgments']==report['judgments']
    assert new['judge_cost']['model_calls']==0
    assert new['judge_cost']['cache_hits']==len(calls)
    assert new['judge_cost']['input_tokens']==new['judge_cost']['output_tokens']==0
    assert derived['invalid_path_cost']==summary['invalid_path_cost']
    assert derived['total_cost']==summary['total_cost']
