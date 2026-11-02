"""Offline adapter tests: real Banking/B workflow with scripted clients, no API calls."""
import json
from pathlib import Path
from dataclasses import replace

import pytest

from experiments.metrics import summarize_run
from experiments.models import Evaluation
from experiments.recorder import TraceError
from experiments.work_judgments import (LLMJudge, analyze_work, packet_for, rule_judgment,
                                         validate_judgment, _read, RUBRIC)
from experiments.workflow_bridge import workflow_batch
from tests.test_workflow_bridge import config, factory
from workflow.client import ModelConfig


class ScriptedJudge:
    config = ModelConfig(model="scripted-judge", plan_format="json_object")

    def __init__(self, mode="valid"):
        self.mode = mode

    def complete(self, messages, tools, **kw):
        assert tools == [] and messages[0]["content"].startswith(RUBRIC)
        packet = json.loads(messages[1]["content"])
        if self.mode == "error":
            raise RuntimeError("sensitive provider error should not be copied")
        value = {"validity": "valid", "reason": "Scripted test verdict, not model evidence.",
                 "evidence_ids": packet["original_output_ids"][-1:]}
        if self.mode == "foreign":
            value["evidence_ids"] = ["another_node"]
        choice = {"finish_reason": "length" if self.mode == "length" else "stop",
                  "message": {"content": json.dumps(value)}}
        return {"choices": [choice], "usage": {"prompt_tokens": 10, "completion_tokens": 5}}


@pytest.fixture
def run(tmp_path):
    workflow_batch([config("full_restart")], tmp_path / "batch", client_factory=factory)
    return tmp_path / "batch" / "run_0000"


def hashes(run):
    return {str(p): p.read_bytes() for p in run.rglob('*') if p.is_file()}


def test_rules_and_llm_feed_original_metrics_without_editing_trace(run, tmp_path):
    before = hashes(run)
    judge = LLMJudge(ScriptedJudge(), max_calls=50)
    report, summary = analyze_work(run, tmp_path / "analysis", judge=judge)
    assert hashes(run) == before
    assert report["llm_estimates_present"]
    assert any(j["source"] == "rule" and j["validity"] == "invalid" for j in report["judgments"])
    assert summary["work_validity_coverage"]["complete"]
    assert summary["invalid_path_cost"]["model_calls"] > 0
    assert summary["repeated_previously_valid_cost"]["model_calls"] > 0
    assert summary["total_cost"] == summarize_run(run, write=False)["total_cost"]
    assert report["judge_cost"]["input_tokens"] == 10 * len(judge.calls)
    assert json.loads((tmp_path / "analysis/rule_only_summary.json").read_text())["invalid_path_cost"] is None


def test_rules_only_does_not_call_nonadoption_valid(run, tmp_path):
    report, summary = analyze_work(run, tmp_path / "rules")
    assert not report["llm_estimates_present"]
    assert report["judge_cost"]["model_calls"] == 0
    assert any(j["node_id"] == "plan" and j["validity"] == "unknown" for j in report["judgments"])
    assert summary["invalid_path_cost"] is None
    assert summary["known_invalid_path_cost"] is not None


def test_post_generation_injection_not_assigned_to_researcher(run, tmp_path):
    report, _ = analyze_work(run, tmp_path / "rules")
    research = [j for j in report["judgments"] if j["node_id"] == "research"]
    assert all(j["validity"] == "unknown" for j in research)
    packets = json.loads((tmp_path / "rules/evidence_packets.json").read_text())
    for p in packets:
        if p["node_id"] == "research":
            assert not any(a.get("derived_from_artifact_id") for a in p["artifacts"]
                           if a["artifact_id"] in p["original_output_ids"])


@pytest.mark.parametrize("mode", ["foreign", "length", "error"])
def test_bad_judge_responses_stay_unknown_and_cost_is_recorded(run, tmp_path, mode):
    judge = LLMJudge(ScriptedJudge(mode), max_calls=50)
    report, _ = analyze_work(run, tmp_path / mode, judge=judge)
    assert not report["llm_estimates_present"]
    assert judge.calls and all(c["status"] == "error" for c in judge.calls)
    assert any(j["reason"].startswith("judge_error") for j in report["judgments"])
    if mode == "error":
        assert report["judge_cost"]["input_tokens"] is None
        assert "sensitive provider" not in (tmp_path / mode / "judge_calls.json").read_text()


def test_budget_and_size_limits_no_silent_truncation(run, tmp_path):
    judge = LLMJudge(ScriptedJudge(), max_calls=1)
    report, _ = analyze_work(run, tmp_path / "limited", judge=judge)
    assert len(judge.calls) == 1
    assert any(j["reason"] == "judge_budget_exhausted" for j in report["judgments"])
    tiny = LLMJudge(ScriptedJudge(), max_calls=20, max_packet_chars=1)
    report, _ = analyze_work(run, tmp_path / "tiny", judge=tiny)
    assert not tiny.calls
    assert any(j["reason"] == "packet_too_large_no_truncation" for j in report["judgments"])


def test_overlay_rejects_foreign_node_scores_and_source_writes(run):
    streams, _ = _read(run)
    node = next(e for e in streams['events'] if e['event_type']=='node_started')
    e = Evaluation(run_id=node['run_id'], attempt_id=node['attempt_id'],
                   execution_segment_id=node['execution_segment_id'], evaluation_id='offline_test',
                   scope='stage', evaluator='test', work_validity={node['node_execution_id']:'valid'},
                   evidence_ids=[node['event_id']])
    with pytest.raises(ValueError):
        summarize_run(run, evaluation_overlay=[e])
    for bad in (replace(e, work_validity={'foreign':'valid'}), replace(e, strict_success=True),
                replace(e, evidence_ids=['foreign']), replace(e, attempt_id='other')):
        with pytest.raises(TraceError):
            summarize_run(run, write=False, evaluation_overlay=[bad])
    with pytest.raises(TraceError):
        summarize_run(run, write=False, evaluation_overlay=[e,e])


def test_refusal_is_semantic_not_automatic_invalid():
    p = {'node_id':'plan', 'original_output_ids':['plan1'], 'artifacts':[
        {'artifact_id':'plan1','content':{'actions':[], 'notes':'Amounts conflict; payment withheld.'}}],
        'events':[], 'a_scoring':[], 'task_reference':{}}
    assert rule_judgment(p) is None
    assert validate_judgment({'validity':'unknown','reason':'Needs semantic review','evidence_ids':[]},p)


def test_existing_judgments_preserved_and_outputs_cannot_overwrite(run,tmp_path):
    report,_=analyze_work(run,tmp_path/'new')
    with pytest.raises(ValueError):analyze_work(run,tmp_path/'new')
    with pytest.raises(ValueError):analyze_work(run,run/'analysis')


def test_transport_sends_judge_rubric_not_planner_schema(monkeypatch):
    import io
    from workflow.client import OpenAICompatibleClient
    from experiments import work_judgments
    packet = {'node_execution_id':'exec1', 'original_output_ids':['a1'],
              'artifacts':[{'artifact_id':'a1'}], 'events':[], 'a_scoring':[]}
    captured = {}
    def fake_open(request, timeout):
        captured.update(json.loads(request.data))
        return io.BytesIO(json.dumps({'choices':[{'finish_reason':'stop','message':{'content':json.dumps({
            'validity':'unknown','reason':'Not enough evidence','evidence_ids':[]})}}]}).encode())
    monkeypatch.setattr('workflow.client.urlopen', fake_open)
    client = OpenAICompatibleClient(ModelConfig(model='judge', plan_format='json_object'), api_key='test-only')
    judge = LLMJudge(client, max_calls=1)
    value, error = judge.judge(packet)
    assert error is None and value['validity']=='unknown'
    assert captured['response_format']=={'type':'json_object'}
    assert 'tools' not in captured
    from experiments.judge_evidence import INSTRUCTIONS
    assert captured['messages'][0]['content']==work_judgments.RUBRIC+'\n'+INSTRUCTIONS
    assert judge.calls[0]['input_tokens'] is None
    assert 'test-only' not in json.dumps(judge.calls)


def test_existing_mock_validity_is_preserved(tmp_path):
    from tests.d_fixtures import run_mock
    run_mock(tmp_path/'mock', 'restart')
    # Mock task is intentionally not Banking: use an explicit test-only reference.
    manifest = json.loads((tmp_path/'mock/manifest.json').read_text())
    reference=tmp_path/'reference.yaml'
    reference.write_text(manifest['task_id']+': {}\n')
    original=summarize_run(tmp_path/'mock',write=False)
    report,summary=analyze_work(tmp_path/'mock',tmp_path/'view',reference_path=reference)
    assert any(j['source']=='existing' for j in report['judgments'])
    assert summary['invalid_path_cost']==original['invalid_path_cost']
