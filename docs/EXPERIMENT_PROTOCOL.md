# D: shared experiment and recovery protocol

D provides one trace and measurement system for both phases. Phase 1 uses fresh
factories. Phase 2 opts into a fixed-DAG adapter and dependency-aware recovery.
A owns task/safety/repeat scoring; B owns real agents and verification; C owns
fault payloads and semantic propagation judgments. D does not substitute business
rules for any of those components.

## Entry points

| Use case | Entry point | Required collaborators |
|---|---|---|
| Phase 1 integration | `run_phase1` in `experiments/phase1.py` | Existing A/B/C minimal contracts |
| Batch experiments | `run_batch(configs, directory, execute)` | An injected single-run adapter |
| Advanced execution | `DagWorkflow` in `experiments/selective.py` | Optional node/state/context adapters |
| Selective recovery | `SelectiveRecoveryCoordinator.recover` | Advanced adapter, fresh factory, evaluator |
| Full comparison for advanced fixtures | Same coordinator, `requested_strategy="full_restart"` | Same initial fixture and evaluator |

```text
configuration → single/batch runner → A / B / C adapters
                      │                     │
                      ├──────── Recorder / Dependency Store
                      │                     │
            Recovery Coordinator          Metrics → CSV
                      │
             Policy + Recovery Point Manager
```

Existing Phase 1 calls and scripts still work. New arguments have defaults.
`clean_verify` adds the fault-free verification comparison. No snapshots or context
export are required by the minimum runner. The advanced coordinator explicitly
requires `DagWorkflow`; unsupported adapters raise a capability error rather than
quietly claiming selective recovery.

## Module map

All modules stay in their existing locations. This table separates public entry
points, shared internals and regression fixtures without moving imports.

| Module in `experiments/` | Role | When to use it |
|---|---|---|
| `phase1.py` | Factory-based runner and A/B/C callbacks | Default Phase 1 integration |
| `protocol.py` | Configuration, provenance and cooperative budget | Shared by both phases |
| `batch.py` | Run a configuration list and aggregate CSVs | Experiment execution/analysis |
| `analysis_stats.py` | Descriptive variation and Wilson rate intervals | Shared analysis, no business scoring |
| `models.py` | Versioned records and immutable artifact identities | Shared internal contract |
| `recorder.py` | Append-only records, calls and explicit dependencies | Shared internal infrastructure |
| `metrics.py` | Trace-derived costs and outcome summaries | Shared analysis |
| `banking.py` | Adapt A's environment/scorer/helper | Integration; does not define A's rules |
| `evidence.py` | Group raw actions by attempt/segment | Input to collaborator-owned judgments |
| `recovery.py` | Recovery points, state protocols and plan types | Shared Phase 2 primitives |
| `selective.py` | Fixed-DAG adapter, invalidation, reuse and recovery | Explicit Phase 2 opt-in |
| `selective_mock.py` | Toy arithmetic/state adapters for the real selective algorithm | Advanced recovery tests and examples |

There is one framework with two supported paths. Phase 1 integrations use
`phase1.run_phase1`, without checkpoints. Phase 2 fixed-DAG comparisons use
`SelectiveRecoveryCoordinator`, including its explicit full-restart strategy.
The old checkpoint restart coordinator and CLI were removed after test migration.
Private tracing fixtures now live under `tests/`; they are not public entry points.

## Documentation map

- [PHASE1_INTEGRATION.md](../PHASE1_INTEGRATION.md): mandatory A/B/C Phase 1 contract.
- This document: current cross-phase design, module responsibilities and experiment rules.
- [CAPABILITIES.md](CAPABILITIES.md): implemented versus validated versus pending.
- [ADVANCED_RECOVERY.md](ADVANCED_RECOVERY.md): short optional-interface overview.
- [PROPAGATION_EVIDENCE.md](PROPAGATION_EVIDENCE.md): proposed annotation handoff to C, not approved scoring rules.
- [RECOVERY_MIGRATION.md](RECOVERY_MIGRATION.md): supported entry points and migrated regression coverage.
- [MEASUREMENT_GUIDE.md](MEASUREMENT_GUIDE.md): baselines, micro/macro measurements and statistical interpretation.
- [phase1_plan.md](phase1_plan.md): team experiment plan; [assignment.md](assignment.md): course requirements.

## Configuration files

| File | Ready to execute? | Purpose |
|---|---|---|
| [`d_phase1_smoke.json`](../configs/d_phase1_smoke.json) | Yes, with the scripted batch CLI | Five task-0 software checks; sample budgets, synthetic results |
| [`d_phase2_smoke.json`](../configs/d_phase2_smoke.json) | Yes, with the Phase 2 toy batch CLI | Three toy DAG runs: selective 0/1, selective 1/3, full restart; no real model conclusions |
| [`d_phase1_matrix.template.json`](../configs/d_phase1_matrix.template.json) | No | 28-cell real-experiment template; complete model/prompt, C payload and pilot-based budgets, then connect B/C |

The template is deliberately rejected while required values remain null. The CLI's
scripted adapter supports only the documented accidental research-to-plan fixture;
other fault families/boundaries need C's adapter. Keep reusable configurations in
`configs/`, small test inputs in `tests/fixtures/`, and generated traces/CSVs in
ignored `runs/`. Neither mock fixture replaces real A/B/C components.

## Configuration and comparison groups

`protocol.py` validates task, condition, scenario ID, comparison group, repetition,
workflow settings, synthetic flag and run-level limits. Real runs require a model,
runtime and prompt hash; real fault runs also need C's payload reference. Missing
values must not be silently filled with made-up model details.

`phase1_matrix()` produces 28 cells per repetition: 24 fault cells (two tasks,
two fault families, two boundaries, three treatments) plus clean and clean-verify
for each task. Selective recovery is a supported configuration condition for an
advanced adapter, but the Phase 1 CLI deliberately rejects it. Actual repetition
counts and resource limits are chosen after a real inference pilot, not from fixture
performance. The checked-in full matrix is a template with unresolved budgets.

A comparison group fixes task/initial state, workflow/model configuration and
repeat index and run-level budgets. Clean pairing compares all supplied configuration
fields except the treatment fields `condition`, `scenario_id`, `fault`,
`method_id`, and `recovery_policy`. Supply initial-state hashes and other shared controls consistently
across a group; missing or mismatched fields do not match. This comparison cannot
verify controls omitted from every configuration. Fault scenario IDs distinguish family, location and payload; the
clean run can be the common counterpart within that group. Record actual original
and replacement outputs. Matching seeds do not guarantee matching model output.

Keep development/tuning runs separate from evaluation runs. Freeze prompts, payloads,
scoring versions and budgets before evaluation. With only two Banking tasks, avoid
claims of broad generalization; additional held-out variants require A/C agreement.

The manifest adds protocol version, commit, dirty-worktree flag, device/platform,
known dependency versions and the full experiment configuration/hash. Hardware RAM
and unavailable runtime fields remain null. Initial state and its hash are recorded
in the initialization event. Fill model/quantization/seed/prompt/payload versions
in the workflow/scenario configuration; D cannot discover an external model server's
configuration reliably. Model startup/warmup should be measured separately by B.

## IDs, history and active results

A run contains attempts. A full restart starts a new attempt; selective recovery
starts a new execution segment in the same attempt. Events, artifacts and evaluations
accept optional `execution_segment_id`. Segment events identify parent/recovery.
Every recomputation gets new execution, call and artifact IDs.

This is an additive extension of record schema 1.0 with protocol version 1.1. The
new reader accepts old records without segment fields (null/unknown). Old readers
may not accept new optional keys; no old-reader compatibility is claimed. Existing
field meanings are retained. `task_success` remains the legacy field; use explicit
`agentdojo_utility` and `strict_success` to compare the A verdicts.

Artifacts and audit events are never rolled back. Snapshot execution state holds
only the active artifact map, contexts, resource versions and scheduling position.
`active_artifacts` records a successfully recovered map. `artifact_retained` records
results already in a validated checkpoint; `artifact_reused` records post-checkpoint
results admitted by the validator. The latter creates no model/tool call.

Event-based retained/reused counts include decisions in a subsequently failed branch.
`effective_retained_artifacts` and `effective_reused_artifacts` use the final successful
recovery result; failed or unavailable results are unknown, not successful reuse.

## Advanced adapter contract and support boundary

The implementation supports fixed, acyclic graphs, sequential scheduling, one output
per node, synchronous completed-node boundaries, and JSON application snapshots.
Dynamic graphs, concurrent calls, node-internal resume and general state-delta replay
are unsupported. This is a trusted orchestration contract, not a security sandbox.

`Node` declares data inputs, control inputs, resource reads/writes, dependency
completeness and whether effects are restorable. Node functions receive explicit
inputs and a fresh per-node context. All persistent model history belongs to that
context; hidden shared histories/caches violate this contract. A real B adapter must
meet the contract or report unsupported capability. A new node wrapper is not proof
that the underlying model sessions are fresh.

Resource versions include content fingerprints and write-generation/source IDs.
Equal values alone do not justify reusing a result from a different write lineage.
State reads contribute source-artifact dependencies. Undeclared writes fail loudly;
reads cannot be introspected reliably, so adapters must declare them conservatively.
An incomplete dependency declaration prevents selective recovery and triggers the
configured fallback. Absence of an edge is never treated as proof of independence
when coverage is unknown.

Snapshots reuse `RecoveryPointManager` and contain environment, contexts, completion
position, active results, resources and configuration. C0–Cn now supports arbitrary
fixed-DAG node counts; C0–C3 behavior is preserved. A checkpoint is published only
after components are written, with hashes and configuration identity. No in-flight
calls are allowed. A checkpoint is a consistent saved state, not a correctness label.

## Selective algorithm and fallback

1. Receive oracle-provided faulty artifact IDs at the end of the initial workflow.
2. Traverse actual artifact, control and state-source dependencies within the attempt.
   Mark potentially invalid results; do not call this semantic propagation evidence.
3. Choose the latest valid checkpoint whose active artifacts are not invalid and
   whose saved per-node contexts match their recorded provenance.
4. Restore environment and execution state together, verifying component identity.
   Disable the one-time injection and create a new execution segment.
5. For pending nodes, consider previously produced results. Reuse requires unchanged
   data/control inputs, resource versions/lineage, workflow configuration and complete
   dependencies. Reuse copies the recorded clean per-node context, not a polluted later
   conversation. Otherwise execute the node again.
6. Pure independent results may be reused even if produced after the restored point.
   A node that can write state is reexecuted; its cached output is never treated as
   restoration of a rolled-back effect. No general effect replay is implemented.
7. Evaluate the new result through the supplied evaluator and preserve all history.

Corrupt checkpoints are skipped during candidate selection. Phase 2 additionally
supports bounded retry and older-checkpoint escalation, described in
[ADVANCED_RECOVERY.md](ADVANCED_RECOVERY.md). Phase 2 defaults are `checkpoint_retries=1` and
`max_checkpoint_attempts=3`. Explicit values `0` and `1` retain the prior single-point
behavior. These settings do not affect the Phase 1 factory runner.
Candidates and reuse references are frozen before recovery starts; failed recovery
branches do not become new trusted recovery points. Every attempted restore reloads
and checks the saved components. Only adapter-declared `RetryableNodeError` failures
are eligible for same-point retry; generic errors are not treated as transient.

Once eligible points/retries are exhausted, the explicit `full_restart` fallback
creates a fresh workflow/environment with the original initial-state fingerprint.
All rounds share the recorder and budget. `fallback_policy="error"` disables full
restart fallback without disabling opted-in checkpoint retries. Requested/executed
strategy and fallback reason remain separate. The default run permits one recovery
operation; its bounded internal rounds do not reset that limit.

Non-restorable effects are reported and automatic reset is refused. This implementation
does not compensate external actions or prove they are undone. Partial node failures
record before/after state evidence, including post-execution validation and artifact
failures, and require conservative full restart when safe. Before each rollback or
fresh reset, the coordinator rechecks all recorded effects, including effects from
failed recovery branches. Final reporting refreshes this evidence even after a failed
full restart. If state observation fails, effects remain unknown and reset is refused.
Snapshots cover declared application resources only, not arbitrary files/processes.

## Evaluation and collaborator boundaries

A's task completion, strict verdict, unauthorized attempts/executions and permission
violations remain separate. `cross_attempt_adapter(task_id)` wraps A's helper if that
revision is installed; missing helper raises ImportError. It is not implemented by D.
A's helper (`eval.scoring.cross_attempt_repeats`) is merged.

`evidence.group_actions` groups raw tool evidence by attempt and segment. It does not
classify duplicates. A can use this to distinguish within-segment duplication from
recovery-induced repeats within an attempt and repeats across attempts. Those semantic
classifiers remain pending A agreement. Repeating an action is not automatically an
irreversible loss; report the effect type and explicit persistence assumption.

C supplies propagation labels with rule versions and artifact/call evidence. See
[PROPAGATION_EVIDENCE.md](PROPAGATION_EVIDENCE.md). Unknown judgments stay unknown.
The synthetic arithmetic evaluator in `selective_mock.py` is a test oracle only;
it cannot be used to score Banking.

## Budgets, costs and metrics

One `Budget` survives original execution, verification, retries, selective recovery
and fallback. All calls routed through `Recorder.call` are admitted against the same
model/tool counts. Wall time is checked cooperatively between calls; D cannot kill
an in-flight provider. Token accounting uses returned usage; a token ceiling may
overshoot on the last call. With a token ceiling and missing usage, subsequent calls
are refused. True hard token/time cancellation requires B/provider support.

Cost breakdowns distinguish initial work, recovery-added work, verifier calls and
retry calls. Report source queries as tools; label verifier/retry model calls with
`purpose`. A result reuse is not a new model call; its validation time is recorded.
Planning, capture/restore and reuse checks have separate timers. Recorder write time
is explicitly a partial overhead measure; these timings can overlap and must not be
summed into a claimed end-to-end total tracking overhead. A controlled tracing on/off
measurement is future experiment work.

Summaries include value/status pairs for recovery latency, affected nodes and invalid
work: measured, not_applicable, not_evaluated or error. Legacy fields remain available.
Unknown tokens stay null; zero means actually measured zero. Graph invalidation does
not automatically mark all descendant computation as wasted work.

Batch summaries report total runs, known completion denominator, unknown evaluations,
execution errors, incomplete traces, exhausted budgets, exposed/judged propagation
denominators and the clean-successful exposed subgroup. Synthetic and real rows are
never pooled. Failed runs remain in results; missing/multiple clean matches are unknown.
Only raw A completion is used for the completion column; scorer warnings remain in
evaluation details and require agreed A validity semantics before final analysis.

## Commands and integration checklist

```sh
python scripts/run_phase1_example.py --condition full_restart --output runs/p1-new
python scripts/run_selective_mock.py --scenario dag --strategy selective --output runs/p2-new
python scripts/run_selective_mock.py --scenario dag --strategy full_restart --output runs/p2-full-new
python scripts/run_experiment_batch.py --config configs/d_phase1_smoke.json --output runs/batch-new
python scripts/run_experiment_batch.py --summarize runs/batch-new --output runs/batch-summary-new
python -m pytest -q -p no:cacheprovider
```

Use fresh output directories. No models are loaded by these examples. Fixture token
counts in the toy advanced example are artificial and flagged synthetic. The Banking
scripted example reports token usage as unknown.

Before real Phase 1 experiments: B connects actual messages/usage and fresh sessions;
B/C connect verification and versioned payloads at both boundaries; A/C confirm
scoring validity, repeat semantics and propagation evidence. D then checks trace
completeness and runs a pilot before choosing repetition counts. Phase 2 additionally
requires B's advanced node/context contract and A-side state snapshot access through
D's adapter. None of these advanced requirements block Phase 1 integration.


## Phase 2 policy configuration and batch reporting

Phase 1 interfaces are unchanged. Phase 2 configurations may include an optional
`recovery_policy` object with `checkpoint_retries`, `max_checkpoint_attempts`,
`max_recoveries`, and `fallback_policy`. When the object is explicitly supplied,
validation fills omitted fields with current defaults (1, 3, 1, full_restart).
Older configurations without this object remain unchanged; missing actual policy
in an old trace is unknown, never retroactively assigned current defaults.

The advanced coordinator records its actual settings in recovery start/end events,
even when planning fails or full restart is requested directly. The toy runner also
saves these settings and effective budgets in its manifest. The Phase 2 batch adapter
passes validated configuration to the coordinator. Real adapters must do the same;
D does not silently assume a submitted configuration was actually executed.

```sh
python scripts/run_phase2_batch.py --config configs/d_phase2_smoke.json \
  --output runs/p2-batch-new
```

This is a synthetic arithmetic fixture, not a Banking experiment. It compares three
configured policies under the same toy fault and budget. It does not inject transient
call failures, so it is a reporting smoke test, not evidence that retries help. The
Phase 1 batch CLI remains unchanged and does not dispatch Phase 2 workflows.

`runs.csv` adds the following columns without removing existing fields:

| Category | Exported evidence |
|---|---|
| Treatment identity | Configured and actual recovery policy JSON, match flag; requested/executed strategy |
| Actual recovery path | Ordered checkpoint starts/failures/successes, older-point transitions and full-restart fallback; fallback reason |
| Work | Recomputed distinct nodes and node executions; raw/effective retained and reused artifacts |
| Attempts | Checkpoint rounds, scheduled same-point retries, actually started same-point retries, older-point transitions |
| Costs | Recovery-added, same-point-retry and verifier model/tool calls and input/output tokens |
| Overhead | Capture/restore time, snapshot bytes, planning/reuse-check time, partial recorder-write overhead |
| Unresolved effects | Evidence IDs and count; missing evidence remains null, not zero |

`condition_summary.csv` adds means with a separate `<metric>_samples` denominator
for each numeric field. Missing token measurements do not become zero. Costs include
failed rounds; retry/verifier costs are subsets and must not be added to total cost.
Raw retained/reused counts include abandoned branches; effective counts describe the
successful final branch. None of these mechanically establishes semantic validity.

Recovery outcome summaries distinguish `selective_successes`, `fallback_successes`
(selective requested, full restart executed), and `full_restart_baseline_successes`.
`recovery_outcome_denominator` includes completed recoveries with known task verdicts
and explicitly failed recoveries; missing outcomes remain unknown. These are **task
recovery outcomes**, not assertions that all historical safety violations were undone.
Report unauthorized actions and unresolved effects alongside them. The existing
`recovery_denominator` still counts all reported recovery attempts.

Grouping includes both submitted configuration and actual recovery policy, preventing
runs with different actual retry policies from being pooled. Policy mismatches are
visible via `recovery_policy_match` and `policy_mismatches`; inspect and exclude them
from intended-treatment comparisons. Summaries preserve their observed outcomes for
audit, rather than discarding the runs. Unknown policy records are separated from
known-policy runs. Clean pairing excludes the recovery treatment but still requires
matching budgets, workflow, initial-state controls and all other supplied controls.

For a real comparison, freeze fault schedule, notification boundary, initial state,
workflow/model settings and total budget. Report the configured selective method as
selective recovery with bounded retries and optional full fallback. Show how often
it succeeded selectively versus through full restart, together with total cost. Do
not attribute the combined policy's entire success rate to dependency-based reuse.
A retry-disabled selective arm (0/1) provides an ablation; the checked-in fixture
includes that arm but does not establish an experimental benefit.


## Repeated-run variation

Numeric batch summaries include sample count, mean, sample standard deviation
(`stddev_`, denominator n-1), median, P95, minimum and maximum. Quantiles use linear
interpolation at sorted index `(n-1)*q`. No observations produce null statistics;
one observation has null standard deviation. Zero is retained when actually measured.
The original `latency_samples` and mean columns remain, with additive detailed fields.
These describe observed run durations, including failed runs with recorded durations;
a timed-out run's duration is not an estimate of its hypothetical completion time.

Completion, strict success, propagation, clean-success/exposed propagation, and
recovery-success rates have explicit successes/sample counts and two-sided 95% Wilson
intervals. Each uses its own known-outcome denominator; unknowns are not coded as
failures or successes. Existing all-attempt totals and error counts are retained.
Wilson intervals assume independent Bernoulli observations within a fixed condition;
see [NIST](https://www.itl.nist.gov/div898/handbook/prc/section2/prc241.htm).
They do not estimate a paired treatment difference or generalization across tasks.

Repeated `repeat_index` values within a group are flagged in `duplicate_repeat_rows`;
rate intervals are suppressed for that group to avoid treating duplicate repetitions
as independent evidence. Raw observations and descriptive statistics remain available
for audit. Distinct indices alone do not establish independence. Small-sample P95s
are descriptive interpolations, not reliable tail-performance claims. Synthetic tests
and smoke runs verify the calculations, not model variability or statistical benefit.
