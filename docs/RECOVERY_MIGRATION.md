# Recovery entry points and legacy cleanup

D is one framework with two supported integration paths sharing records, costs,
scoring adapters and experiment protocols:

| Path | Entry point | Responsibilities |
|---|---|---|
| Phase 1 | `experiments.phase1.run_phase1` | Fresh environment/session, one-shot injection, full restart, recording and scoring |
| Phase 2 | `experiments.selective.SelectiveRecoveryCoordinator` | Dependency-aware recovery, checkpoint selection, retries, reuse, and optional fresh full restart |

The old `FullRestartPolicy` and `RecoveryCoordinator` in `experiments/recovery.py`
have been removed. Their C0-based restart orchestration duplicated the supported
paths. `RecoveryPointManager`, `FaultNotice`, `RecoveryPlan` and the state protocols
remain in that module because the Phase 2 implementation needs them.

The obsolete `scripts/run_d_mock.py` entry point was removed. Use the Phase 1
Banking example or Phase 2 selective example documented in the README. No A/B/C
Phase 1 interface or business rule changed. Persisted JSONL schemas are unchanged;
existing traces remain readable. Imports of the removed legacy classes are no longer
supported; there is deliberately no third compatibility wrapper.

## Regression coverage migration

| Original coverage | Current location / behavior |
|---|---|
| Full restart at research/plan boundaries; immutable history, costs, clean contexts | `tests/test_experiments.py` delegates restart fixtures to `run_phase1` |
| Restore failure and incompatible checkpoints must not execute more calls or report success | `tests/test_selective_recovery.py`, fallback explicitly disabled |
| Restart limit; execution completion is distinct from task correctness | `tests/test_selective_recovery.py`, explicit full-restart strategy |
| Banking snapshot restores state but retains earlier audit evidence | `tests/test_experiments_banking.py`, direct shared checkpoint-manager test |
| Actual Banking factory restart | Existing `tests/test_phase1_minimal.py`, tasks 0 and 2 |
| Snapshot isolation, cache cleanup, incomplete calls, file corruption, record immutability | Existing `tests/test_experiments.py`, unchanged shared primitives |

Toy tracing/scoring helpers now live in `tests/d_fixtures.py`. They are private test
fixtures, not another production recovery system. Their restart helper invokes the
production Phase 1 runner; it does not implement a recovery loop. Three legacy
failure/completion cases were migrated to three equivalent current-path cases.
The migration retained the full 208-test suite before adding statistical tests.
