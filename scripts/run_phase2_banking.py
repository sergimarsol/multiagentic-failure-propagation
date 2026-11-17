"""Phase 2 Banking runs: selective recovery against matched baselines, repeated.

Per repeat and task: clean, clean_verify, and for each of C's scenarios fault,
always_verify, full_restart and selective. full_restart and selective both use the
recovery engine (same stages, checkpoints, oracle notice at the end of the initial
workflow and trial budget), so they differ only in the recovery strategy.

  uv run python scripts/run_phase2_banking.py --scripted --smoke --output runs/p2-check
  uv run python scripts/run_phase2_banking.py --profile configs/phase1_final.json --smoke --output runs/p2-smoke
  uv run python scripts/run_phase2_banking.py --profile configs/phase1_final.json --output runs/phase2

Options, resume behaviour and the summary layout are those of run_phase1_final.py.
--smoke runs task 0 with one faulty-planner scenario, where recovery can keep the research.
--scripted uses B's test client: a pipeline check, not a model result.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from experiments.protocol import normalize_recovery_policy
from scripts import run_phase1_final as base

RECOVERY = ("full_restart", "selective")
SMOKE_SCENARIO = "t0_pe_accidental_amount"


def plan(model, runtime, repeats, synthetic):
    policy = normalize_recovery_policy({})
    configs = []
    for config in base.plan(model, runtime, repeats, synthetic):
        if config["condition"] != "full_restart":
            configs.append(config)
            continue
        for condition in RECOVERY:
            configs.append({**config, "condition": condition, "method_id": condition + "_dag",
                            "recovery_policy": dict(policy)})
    return configs


if __name__ == "__main__":
    base.main(plan=plan, description=__doc__, smoke_scenario=SMOKE_SCENARIO)
