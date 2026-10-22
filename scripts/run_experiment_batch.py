"""Generate the matrix template or execute explicit scripted Phase 1 fixtures."""
import argparse
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from experiments.batch import run_batch, aggregate
from experiments.protocol import phase1_matrix
from experiments.phase1 import run_phase1
from scripts.run_phase1_example import AAdapter, ScriptedSession, OneShotFault


def fixture(config, directory):
    if not config["synthetic"]:
        raise ValueError("this CLI only supports scripted fixtures; connect B for real execution")
    fault = config.get("fault", {})
    if fault and (fault.get("family") != "accidental" or fault.get("site") != "research_to_plan"):
        raise ValueError("fixture only supports accidental research-to-plan injection; C adapter required")
    return run_phase1(directory, task_id=config["task_id"], condition=config["condition"],
        environment_adapter=AAdapter(config["task_id"]), create_session=ScriptedSession,
        injector=OneShotFault(), synthetic=True, experiment_config=config)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output", required=True, type=Path)
    p.add_argument("--config", type=Path)
    p.add_argument("--summarize", type=Path, help="existing batch directory; recompute CSVs")
    a = p.parse_args()
    if a.summarize:
        rows = [json.loads(p.read_text()) for p in sorted(a.summarize.glob("result_*.json"))]
        aggregate(rows, a.output)
    elif a.config:
        run_batch(json.loads(a.config.read_text()), a.output, fixture)
    else:
        configs = phase1_matrix(1, {"model": None, "runtime": None, "prompt_hash": None},
                               {"model_calls": 100, "tool_calls": 100, "wall_seconds": 300})
        a.output.write_text(json.dumps(configs, indent=2))
