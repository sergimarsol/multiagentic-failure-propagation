"""Legacy entry point for the fixed research-boundary acceptance fixtures."""

from .threat_fixtures import make_injector


def wrong_amount(*, task_id):
    """Keep older commands working; rent now uses the specified 100 -> 200 fault."""
    scenario = {"user_task_0": "t0_rp_accidental_bill_amount",
                "user_task_2": "t2_rp_accidental_rent_increase"}[task_id]
    fixture = make_injector(task_id=task_id, scenario_id=scenario)

    def inject(content, site):
        report = fixture(content, site)
        return {**report, "triggered": report["applied"]}
    return inject
