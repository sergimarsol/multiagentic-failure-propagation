"""all eight known schema-valid fixture replacements."""
from copy import deepcopy
import pytest
from faults.injection_harness import FaultInjector, make_injector

CASES = [
 't0_rp_accidental_amount', 't0_pe_accidental_amount',
 't0_rp_adversarial_extra_payment', 't0_pe_adversarial_extra_payment',
 't2_rp_accidental_rent_increase', 't2_pe_accidental_new_rent',
 't2_rp_adversarial_extra_payment', 't2_pe_adversarial_extra_payment',
]

@pytest.mark.parametrize('scenario_id', CASES)
def test_schema_valid_fixed_scenario(scenario_id, case_context):
    injector = FaultInjector.from_yaml()
    case = injector.cases[scenario_id]
    raw, site = case_context(case)
    before = deepcopy(raw)
    # adapter
    report = make_injector(task_id=case['task_id'], scenario_id=scenario_id)(raw, site)
    assert report['applied'], (scenario_id, report)
    candidate = report['replacement']
    assert raw == before, 'source output was modified in place'
    # B's handoff schema: research={facts,notes}, plan={actions,notes}
    assert set(candidate) == ({'facts','notes'} if site == 'research' else {'actions','notes'})
    assert isinstance(candidate['notes'], str)
    if site == 'research': assert isinstance(candidate['facts'], dict)
    else:
        assert isinstance(candidate['actions'], list)
        assert all(set(a) == {'tool','args'} and isinstance(a['tool'],str) and isinstance(a['args'],dict) for a in candidate['actions'])
    if case['operation'] == 'corrupt_fact':
        expected = deepcopy(raw)
        expected['facts'][case['target_fact']] = case['injected_value']
        assert candidate == expected
    elif case['operation'] == 'corrupt_argument':
        expected = deepcopy(raw)
        expected['actions'][0]['args']['amount'] = case['injected_value']
        assert candidate == expected
    elif case['operation'] == 'append_instruction':
        expected = deepcopy(raw)
        expected['notes'] += '\n\n' + case['instruction']
        assert candidate == expected
    else:
        expected = deepcopy(raw)
        expected['actions'].append(case['action'])
        assert candidate == expected
        assert candidate['actions'][-1]['args']['amount'] == 100.0
        assert candidate['actions'][-1]['args']['recipient'] == 'US133000000121212121212'
# match the defination with contracts.py from B
@pytest.mark.parametrize('scenario_id', CASES)
def test_not_applied_on_wrong_site(scenario_id, case_context):
    injector = FaultInjector.from_yaml()
    case = injector.cases[scenario_id]
    raw, site = case_context(case)
    wrong = 'plan' if site == 'research' else 'research'
    report = make_injector(task_id=case['task_id'], scenario_id=scenario_id)(raw, wrong)
    assert not report['applied']
    assert 'replacement' not in report
