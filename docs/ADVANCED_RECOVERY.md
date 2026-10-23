# Optional advanced recovery

Phase 1 teammates may ignore this entire interface. Start with
[PHASE1_INTEGRATION.md](../PHASE1_INTEGRATION.md) for the minimum contract.

D now implements a fixed-DAG application-level selective recovery path in
`experiments/selective.py`, using the existing Recorder, artifacts, RecoveryPlan,
RecoveryPointManager and metrics. See [the unified protocol](EXPERIMENT_PROTOCOL.md)
for algorithm, adapter contracts, failure semantics and runnable examples.

The new path includes dependency invalidation, consistent point selection, context
restoration, pure-result reuse checks, state-version/lineage checks, ordered reruns
and explicit fresh-factory fallback. These mechanisms have executable toy-adapter
tests. Real B contexts, real C injection and real Banking selective recovery are
**not integrated**. No semantic Banking rules were added by D.

Snapshots are synchronous at C0–Cn completed-node boundaries. No node-internal restore,
VM cloning, asynchronous checkpoints, general effects replay or automatic detection.
Non-restorable effects are reported rather than declared recovered.

`experiments/recovery.py` contains shared checkpoint primitives and plan types.
The duplicate legacy restart coordinator and CLI were removed after migrating their
tests. See [RECOVERY_MIGRATION.md](RECOVERY_MIGRATION.md). Phase 1 still uses fresh
factories without requiring checkpoint APIs.

## Optional Phase 2 checkpoint escalation

This is **not a Phase 1 integration requirement**. No changes to `run_phase1`, A's
scoring/environment interface, B's Phase 1 sessions, or C's injector are required.
The new parameters exist only on the optional `SelectiveRecoveryCoordinator`:

```python
coordinator = SelectiveRecoveryCoordinator(
    workflow, fresh_factory, evaluate,
    checkpoint_retries=1,       # one additional execution from each selected point
    max_checkpoint_attempts=3,  # at most three distinct eligible points
    fallback_policy="full_restart",
)
```

Phase 2 defaults are one additional retry per point and at most three distinct
points. Set `checkpoint_retries=0, max_checkpoint_attempts=1` explicitly to use the
previous single-point behavior. Phase 1 remains unchanged. Limits
must be finite integers (`checkpoint_retries >= 0`, `max_checkpoint_attempts >= 1`).
At most `max_checkpoint_attempts * (1 + checkpoint_retries)` checkpoint rounds can
start, followed by at most one full restart if allowed. Fewer points may exist.
These defaults are starting settings, not experimentally optimized limits.

| Failure | Action |
|---|---|
| Adapter raises `RetryableNodeError` during rerun | Restore the same point and retry within its limit |
| Transient retries exhausted | Try the next older prevalidated point within the point limit |
| Selected point cannot load or restore | Skip it and try the next older eligible point |
| Structural error, missing dependencies, or no eligible points left | Full restart if permitted and safe; otherwise stop |
| Non-restorable or unobservable effects | Stop and retain unresolved evidence |
| Budget exhausted | Stop; no new budget for another round or full restart |

B's **optional Phase 2 adapter**, when integrated, may translate a known temporary
provider failure into `RetryableNodeError`. D does not infer retryability from an
error string or treat every timeout as safe. The call must have terminated; the
adapter must report state effects honestly. An ongoing or ambiguous external action
cannot be made safe merely by wrapping its exception. No automatic retry occurs
when the evaluator reports task failure: this is call-error recovery, not additional
oracle-guided attempts to improve a task score.

Before recovery starts, the policy freezes eligible checkpoints in newest-to-oldest
order and their reuse candidates. Every round reloads and validates its point,
restores environment and workflow state, disables injection, and rechecks each reused
result. Outputs/checkpoints produced by failed recovery rounds are not promoted into
the candidate list. Earlier-point recovery may still reuse independently valid work.
Application restore itself must be local/reversible under the trusted adapter contract;
external effects of arbitrary restore implementations are outside this guarantee.

Each round has a new `execution_segment_id` in the same attempt. Full restart creates
a new attempt and segment. Logs preserve failed rounds, calls, artifacts, state evidence,
and checkpoint decisions. `checkpoint_attempt_started/failed/succeeded`,
`checkpoint_retry_scheduled`, and `checkpoint_older_selected` identify the path.
The recovery result and Phase 2 summary include:

- `checkpoint_attempts`: actual started restore/execution rounds.
- `checkpoint_retry_count`: same-point retries scheduled; a budget stop may prevent
  a scheduled retry from starting. Started retries are visible in the events.
- `checkpoint_id`: the successfully executed point, or null when none succeeded.
- `checkpoint_retry_cost`: calls/tokens in started same-point retry segments, already
  included in total/recovery-added cost; do not add it again.

Existing `retry_cost` still counts B/provider calls explicitly labeled `purpose=retry`;
it is not renamed or redefined. Older-point work is included in recovery-added cost.
Effective retained/reused results describe the final successful branch; raw logs also
retain decisions from abandoned branches. No Phase 1 summary fields are required by
this feature.

Run the optional Phase 2 synthetic example (the flags match its defaults):

```sh
python scripts/run_selective_mock.py --scenario dag --strategy selective \
  --checkpoint-retries 1 --max-checkpoint-attempts 3 --output runs/p2-checkpoints-new
```

This CLI example enables the policy but does not inject temporary execution errors.
The regression suite supplies those errors and covers partial writes, context cleanup,
same-point retries, older-point restores, exhausted limits, unsafe effects, shared
budgets, and default compatibility. Validation remains **mock-only**, not real Banking
agent recovery.

## Optional Banking segment audit adapter

`BankingAdapter(env, recorder, segmented_audit=True)` enables Phase 2 audit
segmentation. The default remains `False`: existing Phase 1 callers and audit
keys are unchanged. Construct the segmented adapter on a fresh audit log.

Each tool audit uses `run_id:attempt_id:execution_segment_id`. Checkpoint restore
starts a fresh segment even when called outside the recovery coordinator. The
coordinator can subsequently assign its own segment ID before execution begins.
The raw audit and recorder history are never truncated.

The application snapshot includes `_d_audit_path`, the ordered call IDs on the
current effective execution path. RecoveryPointManager saves and checksums this
metadata together with the environment. Restoring a point restores that path,
so retained calls before the point remain included and abandoned branch calls
are excluded. This metadata is a conservative extra snapshot resource, not a
Banking business field; node adapters must account for it when declaring complete
snapshot resource dependencies. Old snapshots without this metadata are rejected
in segmented mode; they must not silently imply an empty effective history.

`BankingEvaluator.evaluate(attempt_id, "attempt")` selects the effective path in
segmented mode. `evaluate(attempt_id, "history")` preserves all historical calls;
`evaluate(attempt_id, "attempt", audit_view="history")` selects the entire history
of that attempt. Both views delegate classification to A. For scoring only, D
normalizes copied audit run IDs to the attempt ID so retained and rerun calls
within one attempt are not misclassified as cross-attempt repeats. Original
segment audit keys remain in the raw log and evaluation `audit_source_keys`.

A path is a list of call IDs, not simply the latest segment: a valid prefix may
span several segments. Effective success does not erase historical violations,
cost, or irreversible effects. This adapter supports A's simulated, restorable
Banking state only; it does not compensate real payments or authorize restoring
unknown external effects. The coordinator's effect-safety checks still apply.

Validation: regression tests use the real A Banking simulator/scorer with
scripted tool calls and a shared checkpoint manager. Full integration with B's
real-model Phase 2 workflow is still pending. No A scoring rules are changed.
