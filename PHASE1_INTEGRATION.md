# Phase 1: minimum integration contract

This is the entry point for A, B and C. **Stage checkpoints and context serialization
are not required.** D owns persistence, IDs, dependency records and summaries.

```text
A: fresh environment + scoring ─┐
B: fresh session + workflow ────┼─ run_phase1 → events / artifacts / evaluations → summary
C: one-shot replacement ───────┘       │
                                      └─ full restart: new A + new B, C disabled
```

## Run the integration example

From the repository root, install the locked project dependencies, then run:

```sh
uv run python scripts/run_phase1_example.py --condition full_restart --output runs/minimal-restart
uv run python scripts/run_phase1_example.py --condition clean --output runs/minimal-clean
uv run python scripts/run_phase1_example.py --condition fault --output runs/minimal-fault
uv run python scripts/run_phase1_example.py --condition always_verify --output runs/minimal-verify
```

Use a new output directory for each invocation. Add `--task user_task_2` for the
second fixture; the default is `user_task_0`.

The example uses **A's actual Banking tools and scorer**, but scripted B/C substitutes.
Ground-truth actions are test fixtures, never evidence of agent capability. The verifier
is a pass-through fixture: it demonstrates cost recording, not attack prevention.
No model is invoked; token counts stay `null`. All these runs are marked `synthetic`.

## What A supplies

```python
create_initial_environment(task_id) -> environment
evaluate_execution(initial_state, final_state, trace) -> scoring_result
```

Each factory call must return an independent environment with the same initial state.
Scoring must return task correctness and IDs of unauthorized attempts and executions,
with evidence and uncertainty/warnings. Please document which selected tools read or
write which resources, and whether a successful call actually changes state.

D supplies a thin adapter for existing formats. The example's `AAdapter` maps
`BankingEnv().load(task_id)`, `state_dict()`, `audit_log()` and A's `evaluate()` to this
contract. Its `read_state` and `trace` methods are **D-side accessors**, not a request
for A to build a snapshot manager. D currently conservatively records whole-environment
reads and changed-resource writes in `BankingAdapter`.

The normalized evaluation mapping supports `task_success`, `unauthorized_attempt_ids`,
`unauthorized_execution_ids`, `evidence_ids`, and `details`. Missing judgments are
`None`; an empty list means a completed evaluation found none. Optional `propagation`
and `work_validity` judgments require evidence; dependencies alone do not establish them.

## What B supplies

```python
create_session() -> fresh_workflow_session
session.run(env, callbacks, injector, *, injection_enabled, always_verify)
```

A session includes all agent histories and mutable execution caches. Returning a new
wrapper over shared histories is insufficient. D rejects reuse of the same object;
B must also ensure independence of the underlying state. No `export_state`,
`restore_state`, or arbitrary-stage resume interface is required.

B reports operations through one `Callbacks` object:

```python
with callbacks.node("plan", inputs=[research_id]) as execution:
    with callbacks.call("model", "plan", execution,
                        messages=messages, model=model_config) as terminal:
        response = model_call(messages)
        terminal.update(output=response.text,
                        input_tokens=response.input_tokens,
                        output_tokens=response.output_tokens)
    plan_id = callbacks.artifact(execution, response.text, inputs=[research_id])
```

D allocates node execution, call and artifact IDs. Context managers record failures
and durations. Report every retry and verifier as its own call (`purpose="verifier"`
or `purpose="retry"`), including failed calls. Always pass the actual inputs and
outputs. Unavailable usage is `None`, never an invented zero.

Tools may be instrumented through D's `BankingAdapter`, as in the example, or an
equivalent callback adapter. Report arguments, results, errors, permission outcome,
invocation and actual state effects separately. Preserve tool-result artifacts when
later nodes consume them. B need not construct dependency-store objects itself.

## What C supplies

At a configured boundary, D's adapter calls:

```python
injector(original_content, site) -> {
    "triggered": True,
    "replacement": replacement_content,
    "fault_type": "accidental_wrong_amount",
}
```

An untriggered report needs only `{"triggered": False}`. C maintains one-shot behavior
within a trial. B passes the supplied `injection_enabled` flag to `callbacks.inject`.
When disabled, this helper does not call C at all. D saves original and replacement
as separate immutable artifacts and returns the actual delivered artifact ID.
Downstream inputs must reference that ID. Existing C formats can be adapted here.

Fault classes must distinguish accidental and adversarial cases. For malicious
instructions, record the actual replacement text. Timeouts and stale-output schedules
need their own adapters; the supplied example implements only wrong-amount replacement.
Do not classify an untriggered or undelivered injection as successful resistance.

## Full restart and measurement semantics

`experiments.phase1.run_phase1` is the minimum orchestration entry point. It takes
the A adapter, B session factory, optional C injector, task ID and condition.
The current controlled schedule issues an injection-oracle notice **after the initial
workflow finishes or fails**, using the injected artifact IDs. A full restart occurs
once only when an injection actually triggered. Detection is not evaluated.

1. Persist the first attempt's trace, final state and evaluation.
2. Create a new environment and a new session, and check the initial-state fingerprint.
3. Disable injection and execute the entire workflow with a new attempt ID.
4. Preserve both evaluations, all actions and all call costs.

No C0–C3 restore, checkpoint file, or context serialization is used. If initialization,
execution or scoring fails, record that explicitly; a failed recovery is not success.

Each directory contains `manifest.json`, `events.jsonl`, `artifacts.jsonl`,
`evaluations.jsonl` and `summary.json`. Summaries separate initial correctness,
recovered correctness, historical violations, recovery work and total cost.
Unjudged propagation and wasted work remain unknown rather than inferred from graph edges.

**Historical safety policy:** this entry point unions the action IDs classified by A
within each independent attempt. It retains initial violations after successful recovery.
It does not run A's legacy cumulative scorer against a final state from a different
environment. Cross-attempt duplicate external payments are not evaluated by this policy;
these simulated runs do not establish rollback of real-world effects. Per-attempt raw
audit records and scores remain available even if an incomplete later score makes the
aggregate unknown. Agree on any additional cross-attempt safety policy before experiments.

For a real always-verify comparison, B must implement verification at each relevant
intermediate boundary and its response to rejection. Use the same injection schedule,
model configuration and resource budget as the other conditions. This infrastructure
records the calls but supplies no working verification defense.

See [capability status](docs/PHASE1_CAPABILITIES.md) for what is actually verified.
See [optional advanced features](docs/ADVANCED_RECOVERY.md) for Phase 2 preparation.

## Scoring fields and cross-attempt repeats

Both Banking adapters now expose `agentdojo_utility`, `strict_success`, and full
`permission_violations` records at the evaluation level. Summaries preserve both
verdicts for each attempt and for the initial/final attempts. The existing
`task_success` field remains a legacy strict-success mapping (unknown on warnings);
use the explicit fields when distinguishing completion from safety. Raw A verdicts
are preserved even when warnings make the legacy field unknown; inspect `details.warnings`.

D also accepts an optional `cross_attempt_evaluator(attempt_audits)` callback in
`run_phase1`. Its input is a list of `{attempt_id, audit}` records, one per attempt;
its output is a list of repeated-action evidence records. A's upcoming helper can
be wrapped to match this contract without modifying the per-attempt scorer.
The summary's `cross_attempt_repeats` reports `status` and `actions`. Until the
helper is connected, status is `not_evaluated` and actions is `null`, not an empty
list. Helper errors are recorded separately. This hook has fixture tests only.

Repeated actions across attempts are distinct from within-attempt violations.
Their persistence or irreversibility needs an explicit environment/effect policy;
repeat detection alone does not establish that simulated effects survived a reset.
