# Phase 1 results: 170 trials

These are the complete real-model results from source commit `65e89e6eb3aae8cc2606fb444714506044102db8` (2026-11-02). `run_info.json` records the run settings and planned trials.

- `trials/`: all 170 trial directories, including full events, artifacts, evaluations, manifests, and summaries.
- `summary/`: per-trial and per-condition runner metrics.
- `propagation/`: provisional trace analysis with evidence IDs.
- `analysis/`: descriptive aggregate tables, integrity checks, and reviewed examples.
- `report/`: `scripts/phase1_report.py` output: one row per trial (`trials.csv`), per-condition and per-scenario tables, and the report's `tables.tex` (contaminated, discarded and non-discarded redone work).
- `SHA256SUMS`: checksums for every file in this package except itself.

The earlier 50-run exploratory results used the same source code base but `json_object` plans; this formal sweep used the locked `json_schema` profile. Report the batches separately. Node-level `invalid_path_cost` remains unavailable until work-validity judgments are supplied.
