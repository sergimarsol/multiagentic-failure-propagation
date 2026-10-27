# D capability status

This table describes D's original scripted example. The real-model Part B workflow is
now available separately: see [B implementation and C interface](workflow.md) and [reference results](results.md). Its verifier control flow is implemented;
the recorded single-run experiments do not establish reliable model repair or defense performance.

“Real integration” means actual A Banking tools/scorer, not real model inference.
No B/C implementation or A business rules were changed for this work.

| Capability | Implementation | Validation / integration status |
|---|---|---|
| Phase 1 fresh-environment full restart | Implemented, no checkpoint requirement | Real A Banking task 0/2 with scripted B/C |
| Completion, strict verdict, permission evidence | Exposed separately | Real A scorer integration; original fields retained |
| Recorder, artifacts, cost, optional segments | Implemented | Banking fixtures and toy tests; legacy logs readable |
| Clean verification condition and verifier cost | Recording implemented | D fixture is pass-through; B verifier implementation and initial results are documented separately |
| Shared run budget | Cooperative admission implemented | Fixture tests; hard provider cancellation pending B |
| Config/matrix/batch/CSV | Implemented; optional D/B configured entry point | B real session with scripted client; new entry point not yet tested with model inference |
| Repeated-run variation and rate intervals | Implemented; explicit sample counts and missing values | Numerical regressions; no real model variability measured |
| Cross-attempt repeat adapter | Lazy wrapper for A's helper | A helper now merged; Phase 1 integration supplied by main |
| Within-segment vs recovery action grouping | Opt-in segment audit IDs and effective-path selection implemented | Real Banking scripted regression tests; real-model Phase 2 integration pending |
| Dependency-aware invalidation | Implemented for fixed DAG | Toy data/control/state tests |
| Checkpoint selection/context restore | Implemented | Toy contexts; Banking snapshot adapter separately tested |
| Pure-result reuse and state lineage checks | Implemented | Toy late/early/DAG/shared-state cases |
| Selective reruns and explicit full fallback | Implemented | Toy tests including failures and budget limits |
| Optional bounded checkpoint retry and older-point escalation | Implemented; Phase 2 defaults: 1 retry / 3 points | Toy failure-path tests only; Phase 1 unchanged |
| Phase 2 policy provenance and batch metrics | Implemented; additive exports and actual-policy grouping | Three-arm toy batch and reporting regressions; no real model results |
| Irreversible effects | Report and reject unsafe reset | Toy tests; no external-effect compensation |
| Banking propagation/work-validity judgment | Data contract and proposed fixtures | Pending C/A confirmation; unknown in real summaries |
| Actual Banking multi-agent selective recovery | Not integrated | Requires advanced B adapter and A/C integration |
| Dynamic/concurrent graph, intra-node restore, VM cloning | Not implemented | Outside declared scope |
| Automated detection | Not implemented | Controlled oracle only |

Use `python -m pytest -q -p no:cacheprovider` with locked dependencies. Reproducible
commands and timing boundaries are in [EXPERIMENT_PROTOCOL.md](EXPERIMENT_PROTOCOL.md).
Tests establish software behavior, not model recovery rates or universal security.

## Local validation for the cross-phase implementation

- Python 3.11.9 with the existing frozen dependency environment: **299 tests passed** after integrating main (including B tests and Banking segment-audit regressions).
- Original Phase 1 regression tests pass with unchanged callers.
- Five scripted Banking batch conditions complete without dispatch errors.
- Four toy scenarios run both selective and full restart: late, early, independent
  branch, and shared-state dependency. Both strategies reach the expected toy state.
- Selective/full recomputed node counts respectively: late 2/3, early 3/3, DAG 3/4,
  shared 4/5. These are deterministic software fixtures, not model performance data.
- `git diff --check` passes; no changes in A's `env/`, `eval/` or `tasks/`.

A's helper and B's Phase 1 workflow are now included from main. C semantic
judgments and B's Phase 2 advanced workflow remain integration dependencies. Provider cancellation and an
end-to-end tracking-overhead experiment are not claimed by these tests.

## D/B configured Phase 1 entry point

See [WORKFLOW_EXPERIMENTS.md](WORKFLOW_EXPERIMENTS.md) for shared trial budgets,
resolved model/session controls and read-only summary import. Scripted-client tests
use B's actual session and A's simulator/scorer. Old B commands remain unchanged.
Real inference through the new configured entry point remains pending; C semantic
annotations and any Phase 2 workflow integration remain separate.
