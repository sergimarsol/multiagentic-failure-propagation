"""Run synthetic D infrastructure scenarios without any model/dependencies."""

import argparse
import json
from pathlib import Path

from experiments.mock import SCENARIOS, run_mock


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="fresh run directory")
    parser.add_argument("--scenario", choices=SCENARIOS, default="restart")
    parser.add_argument("--site", choices=("research", "plan"), default="research")
    args = parser.parse_args()
    print(json.dumps(run_mock(args.output, args.scenario, site=args.site), indent=2))


if __name__ == "__main__":
    main()
