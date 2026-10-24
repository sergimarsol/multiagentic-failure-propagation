"""Unit tests of fault operations, failure modes and one-shot semantics."""
from copy import deepcopy
import pytest
from faults.injection_harness import FaultInjector, InjectionAttempt, make_injector

@pytest.fixture
def injector():
    return FaultInjector.from_yaml()

def apply(injector, case, body, attempt=None, phase='fault', **kwargs):
    return injector.apply(body, scenario_id=case['id'], task_id=case['task_id'], boundary=case['boundary'], attempt=attempt or InjectionAttempt(), phase=phase, **kwargs)
# test yaml including 8 scenarios and 4 operations
def test_eight_unique_scenarios(injector):
    assert len(injector.cases) == 8
    assert {c['operation'] for c in injector.cases.values()} == {'corrupt_fact', 'corrupt_argument', 'append_instruction', 'append_action'}
# test P->E (accidental) only change the amount of action not other paramters
@pytest.mark.parametrize('scenario_id', ['t0_pe_accidental_amount', 't2_pe_accidental_new_rent'])
def test_corrupt_argument_preserves_everything_else(injector, case_context, scenario_id):
    case = injector.cases[scenario_id]
    raw, _ = case_context(case)
    untouched = deepcopy(raw)
    res = apply(injector, case, raw)
    assert res.applied, res.reason
    assert raw == untouched
    assert len(res.output['actions']) == len(raw['actions'])
    assert res.output['notes'] == raw['notes']
    for before, after in zip(raw['actions'], res.output['actions']):
        assert after['tool'] == before['tool']
        assert {k:v for k,v in after['args'].items() if k != 'amount'} == {k:v for k,v in before['args'].items() if k != 'amount'}
        assert after['args']['amount'] == case['injected_value']
# test P->E (adversarial) add one more actions and keep the orginal ones unchanged
@pytest.mark.parametrize('scenario_id', ['t0_pe_adversarial_extra_payment', 't2_pe_adversarial_extra_payment'])
def test_append_action_only_adds_one(injector, case_context, scenario_id):
    case = injector.cases[scenario_id]
    raw, _ = case_context(case)
    res = apply(injector, case, raw)
    assert res.applied, res.reason
    assert res.output['actions'][:-1] == raw['actions']
    assert res.output['actions'][-1] == case['action']
    assert res.output['notes'] == raw['notes']
# R->P (accidental) only change the fact
@pytest.mark.parametrize('scenario_id', ['t0_rp_accidental_amount', 't2_rp_accidental_rent_increase'])
def test_corrupt_fact_only_changes_target(injector, case_context, scenario_id):
    case = injector.cases[scenario_id]
    raw, _ = case_context(case)
    res = apply(injector, case, raw)
    assert res.applied, res.reason
    expected = deepcopy(raw)
    expected['facts'][case['target_fact']] = case['injected_value']
    assert res.output == expected
# R->P (adversarial) only add instruction into note and keep the fact unchanged
@pytest.mark.parametrize('scenario_id', ['t0_rp_adversarial_extra_payment', 't2_rp_adversarial_extra_payment'])
def test_append_instruction_only_changes_notes(injector, case_context, scenario_id):
    case = injector.cases[scenario_id]
    raw, _ = case_context(case)
    res = apply(injector, case, raw)
    assert res.applied, res.reason
    assert res.output['facts'] == raw['facts']
    assert res.output['notes'] == raw['notes'] + '\n\n' + case['instruction']
# test the injection states of clean and restart_rerun
@pytest.mark.parametrize('phase', ['clean', 'restart_rerun'])
def test_disabled_phases(injector, case_context, phase):
    case = injector.cases['t0_rp_accidental_amount']
    raw, _ = case_context(case)
    res = apply(injector, case, raw, phase=phase)
    assert not res.applied and res.output == raw

@pytest.mark.parametrize('scenario_id', ['t0_rp_accidental_amount', 't0_pe_accidental_amount'])
def test_wrong_original_fails_closed(injector, case_context, scenario_id):
    case = injector.cases[scenario_id]
    raw, site = case_context(case)
    if site == 'research': raw['facts']['bill_amount'] = 42
    else: raw['actions'][0]['args']['amount'] = 42
    res = apply(injector, case, raw)
    assert not res.applied and res.reason == 'unexpected_original_value'
    assert res.output == raw
# cannot find the action or match with multiple actions
def test_missing_and_ambiguous_actions(injector, case_context):
    case = injector.cases['t0_pe_accidental_amount']
    raw, _ = case_context(case)
    missing = deepcopy(raw)
    missing['actions'] = []
    assert not apply(injector, case, missing).applied
    ambiguous = deepcopy(raw)
    ambiguous['actions'].append(deepcopy(raw['actions'][0]))
    res = apply(injector, case, ambiguous)
    assert not res.applied and res.output == ambiguous
# miss the target fact of research
def test_missing_research_fact(injector, case_context):
    case = injector.cases['t0_rp_accidental_amount']
    raw, _ = case_context(case)
    del raw['facts']['bill_amount']
    res = apply(injector, case, raw)
    assert not res.applied and res.output == raw
# test the attempt of injection more than once
def test_one_replacement_per_attempt(injector, case_context):
    case = injector.cases['t0_rp_accidental_amount']
    raw, _ = case_context(case)
    attempt = InjectionAttempt()
    assert apply(injector, case, raw, attempt).applied
    again = apply(injector, case, raw, attempt)
    assert not again.applied and again.reason == 'one_replacement_already_applied'
# scenarios with wrong bondary
def test_wrong_boundary_no_injection(injector, case_context):
    case = injector.cases['t0_rp_accidental_amount']
    raw, _ = case_context(case)
    res = injector.apply(raw, scenario_id=case['id'], task_id=case['task_id'], boundary='planner_to_executor', attempt=InjectionAttempt())
    assert not res.applied and res.output == raw
# can make_injector generate the schema of B
def test_adapter_report(injector, case_context):
    case = injector.cases['t0_pe_accidental_amount']
    raw, _ = case_context(case)
    fn = make_injector(task_id=case['task_id'], scenario_id=case['id'])
    assert fn(raw, 'research')['applied'] is False
    report = fn(raw, 'plan')
    assert report['applied'] and report['fault_type'] == 'accidental'
    assert report['replacement']['actions'][0]['args']['amount'] == 1337.42
    assert fn(raw, 'plan')['applied'] is False
