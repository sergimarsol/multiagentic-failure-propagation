"""Phase 2 Banking runs: selective recovery against matched baselines, repeated.

Per repeat and task: clean, clean_verify, and for each of C's scenarios fault,
always_verify, full_restart and selective, each recovery condition twice: with the
oracle notice at the end of the initial workflow (method <condition>_dag) and right
after the faulty handoff, before downstream stages run (<condition>_dag_early).
full_restart and selective both use the recovery engine (same stages, checkpoints,
notice and trial budget), so within a notice timing they differ only in strategy.
Late notice also runs both methods with a real-bank restore (<condition>_dag_bank):
money already sent stays, as in a real bank, so selective keeps it after its restore
and full restart starts from the initial bank plus that money. With early notice the
executor has not run, so there is nothing to keep and no second restore mode.
All recovery methods of one scenario and repeat share one initial pass: the first trial
records its model replies (<output>/initial_pass/) and the others replay them, so every
method faces the same fault, or the same refusal before it.

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
# Late notice (end of the initial workflow) and early notice (right after the faulty handoff).
BOUNDARIES = (("after_initial_workflow", ""), ("after_faulty_handoff", "_early"))
SMOKE_SCENARIO = "t0_pe_accidental_amount"


def plan(model, runtime, repeats, synthetic):
    policy = normalize_recovery_policy({})
    configs = []
    for config in base.plan(model, runtime, repeats, synthetic):
        if config["condition"] != "full_restart":
            configs.append(config)
            continue
        for boundary, suffix in BOUNDARIES:
            for condition in RECOVERY:
                extra = {"notification_boundary": boundary} if suffix else {}
                configs.append({**config, **extra, "condition": condition,
                                "method_id": condition + "_dag" + suffix,
                                "recovery_policy": dict(policy)})
        for condition in RECOVERY:
            configs.append({**config, "condition": condition, "method_id": condition + "_dag_bank",
                            "recovery_policy": dict(policy), "restore_mode": "real_bank"})
    return configs


if __name__ == "__main__":
    base.main(plan=plan, description=__doc__, smoke_scenario=SMOKE_SCENARIO, share_initial_pass=True)
