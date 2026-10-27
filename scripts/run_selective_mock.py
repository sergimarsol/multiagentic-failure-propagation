"""Execute D's real recovery algorithm with explicitly synthetic adapters."""
import argparse
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from experiments.selective_mock import run_selective_mock

if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output", required=True)
    p.add_argument("--scenario", choices=["late", "early", "dag", "shared"], default="dag")
    p.add_argument("--strategy", choices=["selective", "full_restart"], default="selective")
    p.add_argument("--checkpoint-retries", type=int, default=1)
    p.add_argument("--max-checkpoint-attempts", type=int, default=3)
    a = p.parse_args()
    print(json.dumps(run_selective_mock(a.output, a.scenario, a.strategy,
        checkpoint_retries=a.checkpoint_retries, max_checkpoint_attempts=a.max_checkpoint_attempts), indent=2))
