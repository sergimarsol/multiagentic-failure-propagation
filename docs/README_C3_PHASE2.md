# Part C — Phase 2 Propagation Analysis 

## Overview

We extended our Phase 1 propagation analysis to support D's Phase 2 traces, including execution segments and recovery history. The analyzer is read-only and reuses the six candidate propagation labels from `docs/PROPAGATION_EVIDENCE.md`.

## Propagation Analysis

The analyzer first validates the injected fault using our existing Phase 1 payload definitions and action matcher. It then examines the initial execution trace to determine whether the fault was exposed to the downstream agent, adopted in its output, attempted through a tool call, or executed with a confirmed state change.

We use `execution_segment_id` and `recovery_started` to separate initial propagation from recovery. Successful recovery does not change the initial propagation label.

## Integration with A and D

We use D's existing execution and recovery traces to identify exposure, tool calls, retained artifacts, and rerun stages. Recovery metrics are taken directly from D's summary.

We also use A's Banking audit and `details.paths` to verify state-changing actions and distinguish initial execution history from the final effective path.

We do not modify A/D's implementation or recompute their recovery and correctness metrics.

## Current Validation

Since B's real LLM workflow integration is not yet complete, we validated the analyzer using D's deterministic scripted workflow.

- **33 semantic and regression tests passed**
- **9 scripted-trace acceptance tests passed**
- **60 Phase 2 recovery runs analyzed successfully**
- **24 non-recovery runs skipped**

The 60 scripted runs produced 24 `executed_incorrect_action`, 20 `not_exposed`, and 16 `exposed_not_adopted` labels.

Real LLM traces will need additional validation after B completes the workflow integration.

## Files

- `scripts/analyze_phase2_propagation.py`: Main analyzer. Reads Phase 2 traces, determines propagation labels, and exports JSON/CSV results.
- `tests/test_phase2_propagation.py`: 33 deterministic tests covering propagation labels, incomplete evidence, event matching, and recovery history.
- `tests/test_phase2_propagation_e2e.py`: 9 acceptance tests using D's existing scripted traces to check labels, evidence IDs, and A/D integration.

## How to Run

Run the analyzer from the repository root:

```powershell
uv run python scripts/analyze_phase2_propagation.py --runs-root D:\p2runs --output D:\p2-analysis
```

To analyze a single run, use `--run <run_directory>` instead of `--runs-root`.

The output includes `propagation_phase2.json` and `propagation_phase2.csv`.

Run all tests using the existing scripted dataset:

```powershell
$env:PHASE2_RUNS_ROOT = "D:\p2runs"
uv run python -m pytest -v tests/test_phase2_propagation.py tests/test_phase2_propagation_e2e.py
```

## Next Steps

Confirm the candidate labels with the team, integrate the analysis results with D's `phase2_report.py`, and validate the analyzer using real LLM traces once B's workflow integration is ready.

## B public-stage compatibility validation (2026-11-25)

This analyzer and its original tests come from C's PR #27, commit
`7879c57257a3c204cb485c0e9e9e672440118188`. The integration change accepts both
`banking_phase2_v1` and `banking_phase2_v2` in the CLI and acceptance-test trace
selector. Other runner versions remain unsupported. Propagation rules and A/D
scores are unchanged.

The current B public-stage implementation, with the reduced prompts, generated
84 fresh scripted trials. All 60 Phase 2 runs used `run_stage()` (314 calls,
including 212 calls with continuation state). Analysis produced the same expected
24 `executed_incorrect_action`, 20 `not_exposed`, and 16 `exposed_not_adopted`
labels. The analyzer, trace acceptance, stage API, and banking recovery suites
passed 99 tests together.

The analyzer also processed six archived real TritonAI Phase 2 traces generated
with the public-stage API at B commit `795d76d`: three `executed_incorrect_action`,
two `not_exposed`, and one `uncertain` where injection never triggered. These
archived runs used the earlier prompts; this check made no new model requests
and does not establish real-model accuracy for the reduced prompts. Evidence
references and imported A/D metrics were checked, and input trace files remained
unchanged. Detailed local artifacts are in `runs/b-stage-c-compat-20261125/`.
