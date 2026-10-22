# Capability status

“Real integration” below means the repository's actual A Banking environment and
scoring code. It does **not** mean real LLM agents or measured model performance.

| Capability | Status | Evidence / limitation |
|---|---|---|
| Banking initialization, tool execution and task scoring | Real integration verified | Task 0 and 2, scripted actions |
| Fresh environment full restart | Real integration verified | Both tasks recover after wrong amount; original violations retained |
| Minimal path without checkpoints | Real integration verified | No checkpoint directory or capture/restore calls |
| Events, immutable artifacts, attempt metrics | Real integration verified | Actual Banking actions plus scripted stage callbacks |
| Whole-resource state dependency recording | Real integration verified | Conservative read set; no fine-grained provenance claim |
| Fresh agent history / actual B session factory | Mock only | Fresh scripted sessions; actual model orchestration pending |
| One-shot injection / actual C integration | Mock only | Wrong-amount fixture; real injector pending |
| Verifier calls included in accounting | Mock only | Pass-through fixture; token usage remains unknown |
| Failure, retry and known-token accounting | Mock only | Existing D mock suite; real provider usage pending |
| C0–C3 context recovery, cache clearing and compatibility checks | Mock only | State-serialization mock; real Banking state adapter separately tested |
| Propagation and work-validity labels | Mock only | Hand-checked toy traces; Banking semantic adjudicator pending |
| Functional always-verify defense | Not implemented | Must be supplied by B / experiment harness |
| Adversarial, timeout and stale-output integration schedules | Not implemented | Minimum example covers wrong amount only |
| Selective invalidation, reuse validation and selective scheduling | Not implemented | Phase 2 work |
| Real external-action rollback or compensation | Not implemented | Environment is simulated |
| Automated fault detection | Not implemented | Controlled oracle notification only |

Validation commands (from repository root):

```sh
python -m pytest -q -p no:cacheprovider
python scripts/run_phase1_example.py --condition full_restart --output /tmp/phase1-example-new
```

Use a Python environment containing the frozen project dependencies. The local
implementation was checked with Python 3.11.9 and `uv sync --frozen --python ...`.
`tests/test_phase1_minimal.py` exercises the four conditions on two Banking tasks,
rejects reuse of a session object and checks that untriggered faults do not restart.
Existing D tests cover additional mock accounting and advanced recovery invariants.

The test results are software validation, not experimental propagation or recovery rates.

Review follow-up: separate completion/strict verdicts and permission violations
are exposed by both Banking adapters. The optional cross-attempt repeat reporting
hook has fixture validation only; A's actual repeat-detection helper is pending.
