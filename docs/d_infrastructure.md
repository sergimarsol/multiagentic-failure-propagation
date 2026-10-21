> **Integration note:** This document describes the optional advanced architecture. For the required Phase 1 interfaces, start with [PHASE1_INTEGRATION.md](../PHASE1_INTEGRATION.md). Full restart now has a factory-based entry point that requires neither checkpoints nor context serialization. See [capability status](PHASE1_CAPABILITIES.md) and [optional modules](ADVANCED_RECOVERY.md).

# D: Phase 1 experiment infrastructure

This package records executions, stores application checkpoints, performs a bounded
full restart, and derives metrics. It runs locally with JSON/JSONL and has **no model
dependency**. Selective recovery, dependency traversal/invalidation, reuse validation,
and automatic fault detection are deliberately not implemented.

## Quick start

From the repository root, using Python 3.10 or later:

```bash
# Standard library only. Choose a fresh output directory for each run.
python -m scripts.run_d_mock --scenario restart --output runs/d-example
python -m scripts.run_d_mock --scenario restart --site plan --output runs/d-plan-example

# With the repository's pinned development dependencies installed:
uv run pytest -q
uv run pytest -q tests/test_experiments.py tests/test_experiments_banking.py
```

The mock CLI does not call a model, use a network API, or load AgentDojo. Its toy
rule is one payment of 10 to `fixture-recipient`; these are **test fixtures, not
Banking business rules**. Every mock manifest and summary is marked `synthetic`.
Do not include these outputs in model-performance results.

| Scenario | What it exercises |
| --- | --- |
| `clean` | Successful execution without injection |
| `untriggered` | Scheduled fault that never triggers |
| `ignored` | Fault delivered, then corrected using explicit synthetic independent evidence |
| `rejected` | Incorrect action requested but tool denies it |
| `executed` | Incorrect action changes the environment |
| `restart` | Incorrect execution followed by successful full restart |
| `restore_failure` | Environment restore raises; no recovery tool actions follow |
| `incompatible` | Checkpoint/configuration mismatch; restart fails explicitly |
| `usage_retry` | Missing token usage, failed verifier call, and verifier retry |

Expected `restart` results: initial task incorrect, final task correct, one historical
unauthorized execution, three recomputed nodes, six model calls, four tool calls.
Actual timing and generated IDs vary. Invocation token counts in the mock are fixtures.

## Modules and boundaries

```text
A environment + B workflow + C injector
               |
          Recorder -------> Metrics
               |
       DependencyStore (records only)

RecoveryPointManager <---- A environment + B execution state
               |
FullRestartPolicy -------> RecoveryCoordinator
                              |
                        same B workflow
```

| Module | Responsibility |
| --- | --- |
| `experiments/models.py` | Versioned Event, Artifact, Evaluation, RecoveryPoint contracts |
| `experiments/recorder.py` | Append-only recorder, invocation timing, dependency events |
| `experiments/recovery.py` | Protocols, synchronous checkpoints, full restart policy/coordinator |
| `experiments/metrics.py` | Reproducible summaries; no business-rule inference |
| `experiments/banking.py` | Opt-in adapters to A's environment and scorer |
| `experiments/mock.py` | Executable synthetic workflow/injector/evaluator fixtures |

No existing A/B/C modules or authorization rules are rewritten. The package's core
does not import AgentDojo. Import and construct Banking adapters only when needed.

## Records and IDs

| Record | Meaning |
| --- | --- |
| Event | Observed input/output, invocation, injection, dependencies, or recovery |
| Artifact | Immutable persisted result, with actual input version references |
| RecoveryPoint | Consistent environment + contexts + workflow position + artifact map |
| Evaluation | External judgments, evidence, and optionally execution validity |

All records include `schema_version` and `run_id`. IDs use letters, digits, `_`, and `-`.
`run_id` covers the whole trial. `attempt_id` changes on full restart. `node_id` is a
stable logical role/stage; each execution receives a new `node_execution_id`; each
model/tool invocation receives a new `step_id`. Retry steps link with `retry_of_step_id`.
Artifacts, checkpoints and recoveries have their own unique IDs.

Persistence copies JSON data. Mutating a caller's object or a returned read cannot
modify stored artifacts. Duplicate IDs are rejected; there is no update-artifact API.
Original and injected results are separate artifacts; downstream inputs reference
the version actually delivered. An artifact replacement is not automatically an
evaluation of invalidity.

### Recording a model call

```python
with recorder.call(
    "model", attempt_id,
    node_id="plan", node_execution_id=execution_id,
    messages=actual_messages,
    model_config={"model": model_name, "temperature": temperature},
    input_artifact_ids=[research_artifact_id],
) as terminal:
    response = call_your_model(actual_messages)
    terminal.update(
        output=response.text,
        input_tokens=response.input_tokens,   # None when unavailable
        output_tokens=response.output_tokens,
    )
```

The call scope records a start and one finish, using a monotonic duration. Raised
exceptions produce an error finish and are re-raised. A process crash may leave an
unfinished start; metrics count the invocation and report incomplete/missing usage.
Only actual API input/returned output is required, not private reasoning.

Record the exact post-template/post-truncation messages. Artifact references alone
do not prove exposure. Add `fault_exposure` only when the replacement really enters
the downstream input. Record verifier and retry calls through the same API.

### Dependencies

```python
DependencyStore(recorder).record(
    attempt_id, "plan", execution_id,
    inputs=[research_artifact_id],
    controls=[verification_artifact_id],
    state_reads=[{"resource_key": "scheduled_transactions", "version": version_ref}],
    state_writes=[],
)
```

These are `dependencies_recorded` events, not a separate database. `None` means
unknown; `[]` means known empty. Phase 1 does not traverse these edges or infer
propagation. BankingAdapter conservatively records the whole environment as a
read dependency for an invoked tool, plus changed resources as writes. Content
fingerprints identify resource contents; A's `env_version` remains a reset epoch,
not a per-write resource version. Future precision requires A's explicit semantics.

## Checkpoints

`RecoveryPointManager` binds environment snapshots to B's execution state:

```python
state = {
    "contexts": {"research": initialized_messages, ...},
    "completed_nodes": [],
    "pending_nodes": ["research", "plan", "execute"],
    "position": 0,
    "artifact_map": {},
}
```

Boundaries are C0 (before task calls/injection), C1 (after research), C2 (after plan),
C3 (after execution). The caller explicitly labels `before_injection`,
`after_injection`, or `no_injection`. C0 must contain initial execution state and
may be created only once. No checkpoint operation is allowed with tracked calls
in flight. B must route all relevant calls through the recorder and quiesce any
untracked asynchronous activity; this package does not cancel remote requests.

Components are synchronously written, flushed/fsynced, and checksummed. The ready
manifest is published last. Incomplete folders are never usable checkpoints.
Restore validates schema, run/configuration fingerprint and component integrity
before touching the environment. It calls B's explicit cache cleanup, restores
execution state, and checks that exported state matches the checkpoint.

Cache cleanup must return `{"complete": True, "cleared": [...]}`. An absent or
negative confirmation fails restoration. System/user initialization messages are
restored from C0, not replaced with empty lists. A restore failure can leave partial
state; the coordinator records failure and never proceeds to node execution.

Audit logs and accumulated costs are outside snapshots. A log cursor identifies
the boundary but never truncates history. Snapshot success does not certify content
correctness. This is an application-level design, not a TClone implementation.

## Full restart

```python
notice = FaultNotice((faulty_artifact_id,), source="injection_oracle")
plan = FullRestartPolicy().plan(
    notice, attempt_id=attempt_id, initial_point=c0,
    nodes=("research", "plan", "execute"),
)
result = coordinator.apply(plan)
```

The harness emits `fault_notified` at its configured quiescent boundary. The
coordinator validates the supplied IDs and full-workflow plan, preserves/evaluates
the faulty attempt, restores C0, clears B-owned caches, restores initial contexts,
creates a new attempt, disables C's one-time injection, and executes the same
workflow from its start. Recovery counts persist in the event stream; default
maximum is one. A second request beyond the cap is rejected.

`completed` means the recovery execution and attempt evaluation completed;
`task_success` is the evaluator's judgment. They are separate. Full-history
evaluation is stored separately and cannot erase prior unauthorized actions.
The coordinator does not detect faults, read a clean answer, choose selective
rerun nodes, or claim a real payment has been reversed.

## Banking integration

```python
from env import BankingEnv
from experiments.banking import BankingAdapter, BankingEvaluator

bank = BankingEnv().load(task_id)
adapter = BankingAdapter(bank, recorder)
evaluator = BankingEvaluator(adapter)
result = adapter.call_tool(
    "executor", tool_name, tool_args,
    attempt_id=attempt_id, node_id="execute",
    node_execution_id=execution_id, inputs=[plan_artifact_id],
)
```

A has one audit `run_id` field. The adapter uses `D_run_id:D_attempt_id` there to
filter attempts, while D's trace retains both IDs. It preserves `call_id` and
`step_id` for joins. Only this trial's audit records enter evaluation.

The adapter uses A's existing strict scoring unchanged, including its full-history
policy that repeated authorized payments across resets can count as duplicates.
It does not invent authorization predicates or assume balance deductions.
Scorer warnings produce unknown task correctness and remain in evaluation details.
Propagation and work-validity judgments are **not** supplied by this adapter; C/A
must add task-specific `Evaluation` records with evidence.

Snapshots include A's complete simulated account state, including task-relevant
synthetic credentials if present. Never point this adapter at real accounts or
serialize host environment variables/API credentials into manifests or contexts.

## Metrics and interpretation

```python
from experiments.metrics import summarize_run
summary = summarize_run("runs/d-example")
```

| Level | Fields / interpretation |
| --- | --- |
| Initial attempt | Initial correctness, injection/exposure, stage propagation; per-attempt safety/cost/latency |
| Recovery | Attempted, completed, correctness, latency, recomputed nodes/executions, added cost |
| Whole trial | Final correctness, full-history unauthorized actions, total calls/usage/latency, checkpoint/write overhead |

Calls are counted from starts exactly once; usage is read from the matching finish.
Verifier, retry, failed and incomplete calls are included. Unknown totals are null,
with known usage sums, missing-call counts and coverage alongside. No history
evaluation means historical safety is unknown, not zero.

`invalid_path_cost` and `repeated_previously_valid_cost` use only explicit external
`work_validity` judgments keyed by node execution. Without complete relevant judgments
they are null; `known_invalid_path_cost` can report the judged subset alongside a
coverage count. All-unknown judgments never become zero wasted work. These categories may overlap recovery
cost and must not be summed as independent totals. Dependency edges never mark work
invalid automatically. Propagation remains a list of per-stage judgments and
evidence, with attempt/evaluation IDs; it is not an exclusive run-level enum.

End-to-end and recovery latency come from explicit boundary durations, not summing
nested spans. `known_invocation_duration_ms` is aggregate invocation work, not wall
time. Recorder overhead measures serialization/write time inside append operations,
sampled before `run_finished`; it is not a causal estimate of total tracing overhead.
Checkpoint capture includes serialization/fsync; restore timing includes state/cache
operations after load validation. Recovery wall time also includes validation I/O.

## Output and failure handling

```text
run/
├── manifest.json
├── events.jsonl
├── artifacts.jsonl
├── evaluations.jsonl
├── checkpoints/<checkpoint_id>/
│   ├── environment.json
│   ├── execution.json
│   └── checkpoint.json
└── summary.json
```

Reopening a recorder requires the same manifest. Duplicate IDs, invalid JSON/schema,
non-finite numbers, wrong run IDs, invalid token counts and unmatched invocation
ends are rejected. The read-only summary path reports a torn final JSONL record;
the append writer refuses to reopen it until explicitly repaired. Malformed middle
records fail. Summary generation does not alter source traces.

## A/B/C integration checklist

- **A:** complete JSON snapshot/restore; actual effect semantics; task scorer.
  BankingAdapter provides these connections today, with coarse state reads.
- **B:** execute nodes with stable logical IDs and fresh execution IDs; emit actual
  messages, dependency inputs and usage; export/restore all contexts and controller
  position; explicitly clear runtime caches; return only after the workflow terminates.
- **C:** store original and replacement artifacts; implement one-shot disable;
  emit triggered separately from delivered; supply fault identity at the agreed boundary.
- **Harness:** record run/attempt boundaries and durations, capture C0 before any
  task call, attach stage points, record evaluations, enforce resource budgets and
  emit the supplied notification. The mock is an executable example, not B/C production code.

## Tests and remaining scope

Core tests cover every CLI scenario, actual-version dependencies, immutable storage,
checkpoint isolation, initialized-context restoration, cache cleanup, damaged and
partial checkpoints, lifecycle errors, missing usage, verifier retry, restart caps,
incomplete calls and deterministic aggregation. Banking tests exercise real A tools,
snapshot conversion, audit mapping, restart and strict historical scoring.

Single writer and serial quiescent checkpoints are assumed. No crash-resume scheduler,
distributed locking, remote-call cancellation, malicious filesystem defense, model
runner, production injector or semantic verifier is implemented. Always-verify calls
are supported and counted; B/C still implement the actual verification policy.

Phase 2 can add a new policy and reuse the immutable IDs, dependency events and
checkpoint payloads. It must validate state compatibility, avoid replaying surviving
effects, preserve history and account for fallback/check costs. None of these
selective decisions is silently approximated in Phase 1.

## Validation recorded for this implementation

On 2026-10-21, using Python 3.11.9 and the unchanged frozen `uv.lock`:

```bash
/private/tmp/mafp-d-venv/bin/python -m pytest -q -p no:cacheprovider
# 124 passed (100 existing + 24 D tests)
```

A synthetic full-restart example is available locally at
`runs/d-phase1-example/` (git-ignored). It records an incorrect initial task, a
correct recovered task, one historical unauthorized execution, six model calls
and four tool calls. The temporary interpreter path is specific to this validation;
use the project's usual `uv sync` / `uv run pytest` setup elsewhere.
