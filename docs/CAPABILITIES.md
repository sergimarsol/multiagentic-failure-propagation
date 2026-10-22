# D capability status

“Real integration” means actual A Banking tools/scorer, not real model inference.
No B/C implementation or A business rules were changed for this work.

| Capability | Implementation | Validation / integration status |
|---|---|---|
| Phase 1 fresh-environment full restart | Implemented, no checkpoint requirement | Real A Banking task 0/2 with scripted B/C |
| Completion, strict verdict, permission evidence | Exposed separately | Real A scorer integration; original fields retained |
| Recorder, artifacts, cost, optional segments | Implemented | Banking fixtures and toy tests; legacy logs readable |
| Clean verification condition and verifier cost | Recording implemented | Scripted pass-through only; real B/C verifier pending |
| Shared run budget | Cooperative admission implemented | Fixture tests; hard provider cancellation pending B |
| Config/matrix/batch/CSV | Implemented | Fixture tests; real model matrix not run |
| Repeated-run variation and rate intervals | Implemented; explicit sample counts and missing values | Numerical regressions; no real model variability measured |
| Cross-attempt repeat adapter | Lazy wrapper for A's helper | A helper absent in current checkout; pending A integration |
| Within-segment vs recovery action grouping | Evidence grouping implemented | Semantic classification pending A, not reinvented by D |
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

- Python 3.11.9 with the existing frozen dependency environment: **224 tests passed** (including optional Phase 2 retry regressions).
- Original Phase 1 regression tests pass with unchanged callers.
- Five scripted Banking batch conditions complete without dispatch errors.
- Four toy scenarios run both selective and full restart: late, early, independent
  branch, and shared-state dependency. Both strategies reach the expected toy state.
- Selective/full recomputed node counts respectively: late 2/3, early 3/3, DAG 3/4,
  shared 4/5. These are deterministic software fixtures, not model performance data.
- `git diff --check` passes; no changes in A's `env/`, `eval/` or `tasks/`.

The open A helper PR has not been imported. A/C semantic judgments and B's real
advanced workflow remain integration dependencies. Provider cancellation and an
end-to-end tracking-overhead experiment are not claimed by these tests.
