"""deterministic B/C boundary contract tests (no live LLM).

These tests check gate behavior; actual workflow, verifier source isolation,
retry ordering, audit reconciliation and traces need the full repository.
"""
import pytest
from faults.injection_harness import FaultInjector, make_injector

from workflow.contracts import InjectionGate, InjectionSpec, validate_content
# test b can accept the injection from c
@pytest.mark.parametrize('scenario_id', list(FaultInjector.from_yaml().cases))
def test_b_gate_accepts_c_reports(scenario_id, case_context):
    case = FaultInjector.from_yaml().cases[scenario_id]
    raw, site = case_context(case)
    gate = InjectionGate(make_injector(task_id=case['task_id'], scenario_id=scenario_id), InjectionSpec(scenario_id, site))
    assert validate_content(site, raw) == raw
    report = gate(raw, site)
    assert report['applied'] is True
    assert validate_content(site, report['replacement']) == report['replacement']
    assert gate.finish(True)['applied'] is True
    again = gate(raw, site)
    assert again['applied'] is False
    assert again['reason'] == 'already_applied'
# test a fault can inject in a certain boundary 
def test_b_gate_only_selected_boundary(case_context):
    case = FaultInjector.from_yaml().cases['t0_pe_accidental_amount']
    raw, _ = case_context(case)
    gate = InjectionGate(make_injector(task_id=case['task_id'], scenario_id=case['id']), InjectionSpec(case['id'], 'plan'))
    wrong = gate({'facts': {'bill_amount': 98.70}, 'notes': 'source'}, 'research')
    assert not wrong['applied'] and wrong['reason'] == 'not_selected_boundary'
    assert gate(raw, 'plan')['applied']
