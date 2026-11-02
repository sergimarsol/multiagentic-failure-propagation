"""The final-run plan covers every task, C scenario and condition exactly once per repeat."""
from collections import Counter

from experiments.workflow_bridge import prepare_config
from faults.injection_harness import FaultInjector
from scripts.run_phase1_final import key, plan

MODEL = {"model": "m", "base_url": "http://127.0.0.1:11434/v1"}


def test_plan_counts_and_unique_keys():
    configs = plan(MODEL, "rt", 5, False)
    assert len(configs) == 5 * (2 * 2 + 10 * 3) == 170
    assert len({key(c) for c in configs}) == len(configs)
    per_condition = Counter(c["condition"] for c in configs)
    assert per_condition == {"clean": 10, "clean_verify": 10, "fault": 50, "full_restart": 50, "always_verify": 50}
    # Controls are shared per task and repeat, never duplicated per scenario.
    assert all(c["scenario_id"] == "clean" and "fault" not in c for c in configs if c["condition"] in ("clean", "clean_verify"))


def test_fault_configs_use_c_payloads_and_match_scenarios():
    cases = FaultInjector.from_yaml().cases
    faults = [c for c in plan(MODEL, "rt", 1, False) if "fault" in c]
    assert {c["scenario_id"] for c in faults} == set(cases)
    for c in faults:
        case = cases[c["scenario_id"]]
        assert c["task_id"] == case["task_id"] and c["fault"]["family"] == case["family"]
        assert c["fault"]["site"] == {"researcher_to_planner": "research_to_plan",
                                      "planner_to_executor": "plan_to_execute"}[case["boundary"]]
        assert c["fault"]["payload_ref"] == "faults/payloads.yaml" and c["fault"]["fixture"] is False


def test_every_config_is_accepted_and_pairs_with_its_clean_run():
    configs = plan(MODEL, "rt", 1, False)
    prepared = [prepare_config(c) for c in configs]
    assert all(p["workflow"]["model"]["model"] == "m" for p in prepared)
    groups = {c["comparison_group_id"] for c in configs}
    assert groups == {"user_task_0_r0", "user_task_2_r0"}


def test_locked_profile_is_valid():
    import json
    from pathlib import Path
    from scripts.run_phase1_final import PROFILE_KEYS
    profile = json.loads((Path(__file__).resolve().parents[1] / "configs/phase1_final.json").read_text())
    assert set(profile) <= PROFILE_KEYS and profile["repeats"] == 5
    model = {"model": profile["model"], "base_url": profile["base_url"], "plan_format": profile["plan_format"],
             "temperature": profile["temperature"], "max_tokens": profile["max_tokens"],
             "timeout_seconds": profile["request_timeout"]}
    config = prepare_config(plan(model, profile["runtime"], 1, False)[0])
    assert config["workflow"]["model"]["model"] == "api-glm-5.3-flash"
