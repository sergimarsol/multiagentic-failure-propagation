"""Phase 2 reporting smoke test using toy adapters only; no real agents or A rules."""
import argparse
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from experiments.batch import run_batch
from experiments.selective_mock import run_selective_mock


def fixture(config, directory):
    scenarios = {"toy_" + s: s for s in ("late", "early", "dag", "shared")}
    if config["synthetic"] is not True or config["task_id"] not in scenarios:
        raise ValueError("Phase 2 fixture supports synthetic toy tasks only")
    if config["condition"] not in {"selective", "full_restart"}:
        raise ValueError("Phase 2 fixture supports selective/full_restart only")
    fault = config.get("fault", {})
    expected_site = "research_to_plan" if scenarios[config["task_id"]] in {"early", "dag"} else "plan_to_execute"
    if (fault.get("family") != "accidental" or fault.get("site") != expected_site
            or fault.get("payload_ref") != "toy_wrong_number_999"):
        raise ValueError("toy fixture requires its documented wrong-number fault schedule")
    if config["workflow"] != {"model": "synthetic_arithmetic_fixture", "version": "1"}:
        raise ValueError("toy fixture requires its exact synthetic workflow configuration")
    return run_selective_mock(directory, scenarios[config["task_id"]], config["condition"],
                              experiment_config=config)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    run_batch(json.loads(args.config.read_text()), args.output, fixture)
