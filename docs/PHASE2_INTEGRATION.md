# Phase 2 integration: Banking selective recovery

Status of D's recovery integration and what each owner needs to decide or supply.
Phase 1 entry points and results are unchanged.

## What runs today

`experiments/banking_selective.py` runs B's Researcher, Planner and Executor as three
nodes of D's recovery engine, on A's simulator and scorer, with C's injector.

| Condition | Runner | Recovery |
|---|---|---|
| `clean`, `clean_verify`, `fault`, `always_verify` | Phase 1 (`run_phase1`) | none |
| `full_restart` without `recovery_policy` | Phase 1 | fresh environment and sessions |
| `full_restart` with `recovery_policy` | Phase 2 | fresh environment and stages, same adapter as selective |
| `selective` | Phase 2 | restore newest uncontaminated checkpoint, rerun affected stages |

```sh
# Pipeline check with B's scripted client (no model): 6 trials, or all 44 with --repeats 1.
uv run python scripts/run_phase2_banking.py --scripted --smoke --output runs/p2-check

# Real model (commit first; key only in the shell).
export TRITONAI_API_KEY=...
uv run python scripts/run_phase2_banking.py --profile configs/phase1_final.json --smoke --output runs/p2-smoke
uv run python scripts/run_phase2_banking.py --profile configs/phase1_final.json --output runs/phase2
```

A single trial is `experiments.workflow_bridge.execute_workflow(config, directory)`; add
`"condition": "selective"` (and optionally `recovery_policy`, `method_id`) to a Phase 1 config.

Behaviour with the scripted client (software check, not a model result):

| Fault site | Selective | Full restart |
|---|---|---|
| Planner output (`plan_to_execute`) | restores the point after research, reruns plan and execute (3 added model calls on task 0) | reruns all three stages (5) |
| Researcher output (`research_to_plan`) | restores the initial point, reruns all three stages (5) | reruns all three stages (5) |

In both strategies the wrong action of the initial execution stays in the history scores and
in A's attack judgment. Only selective removes it from the final simulated state.

## How it is wired

- **Stages.** One fresh B session per stage execution; D calls B's existing stage routine
  with the delivered upstream artifact. Prompts, permissions, formats and retries are B's.
- **Saved at each completed-stage boundary (C0-C3).** Bank state, the effective audit path,
  and per stage: role, message history, model/tool call counts, delivered output.
- **Fault.** C's injector runs behind B's `InjectionGate` at the configured handoff. The
  recovery notice is the replaced artifact's ID. Injection is disabled for every rerun
  and for the fallback restart.
- **Notice timing.** `notification_boundary` is `after_initial_workflow` (default, methods
  `selective_dag` / `full_restart_dag`): the wrong action has already run, so recovery
  can only save cost. `after_faulty_handoff` (methods `*_dag_early`): the pass stops as
  soon as the faulty result is handed off, before any downstream stage, so recovery can
  also prevent the wrong action. For a faulty plan, selective reruns only the planner
  (keeping research) and then runs the executor; full restart reruns research too. Early
  notice is accepted only on the recovery engine; the report pairs runs by notice timing.
  Early notice is a best case: the oracle names the faulty result for free, which a real
  system cannot do, while always_verify pays to catch it. Present early-notice rows as an
  upper bound, not as a fair win over always_verify (the report labels them so).
- **Scores.** A's scorer judges (1) the initial path, (2) the effective path after recovery.
  The history evaluation is A's `evaluate_recovery` over every audited call, including
  branches a restore dropped. Dropped writes are listed as `unresolved_effects` (money sent,
  which a real bank cannot take back) or `compensated_effects` (updates another update could
  undo); the summary and report take unresolved effects from it. Nodes stay restorable: the
  simulation can be reset, and the effects are reported, not hidden. `environment_finalized`
  carries the full audit, so A's `attack_outcome` still sees actions that a restore removed.
- **Trace.** Every event has `execution_segment_id`. `segment_started`, `checkpoint_restored`,
  `artifact_retained`, `reuse_checked`, `recovery_started/finished` describe the path.
- **Failures.** A retryable provider error during a rerun retries the same checkpoint, then
  an older one, then full restart (per `recovery_policy`). The trial budget is shared.

Tests: `tests/test_banking_selective.py`, `tests/test_phase2_report.py`.

## Analysis and packaging

```sh
# Work validity per node execution: rules only, or add --llm (see OFFLINE_WORK_JUDGMENTS.md).
uv run python scripts/judge_work.py runs/phase2 --output runs/phase2-work

# Tables: outcome, recovery cost, matched selective/full-restart pairs, work categories, overhead.
uv run python scripts/phase2_report.py runs/phase2 --judgments runs/phase2-work

# Committed results: traces, summaries, report, judgments, checksums -> results/phase2/.
uv run python scripts/package_phase2_results.py runs/phase2 --judgments runs/phase2-work
```

The report keeps unknown values empty, lists unmatched pairs instead of pairing them, and
reports judge cost separately from agent cost. Definitions are in the header of
`scripts/phase2_report.py`.

## Known limits

- With three stages in a chain, a result is either kept as part of the restored prefix or
  rerun. Reuse of a saved result *after* a restore does not occur: Researcher results made
  audited tool calls, so the engine recomputes them. Showing reuse on Banking needs a
  workflow with independent branches.
- Verification is not part of the recovery path. `always_verify` uses the Phase 1 runner.
- All simulated Banking effects are treated as restorable.
- Work validity is unknown for most stages unless the LLM judge is run; rules only establish
  unauthorized executions and wrong research facts. Judged values need a spot check.
- The report has no LaTeX tables yet.
- No real-model Phase 2 run has been made yet.

## Requests to each owner

### A (Sergi): scoring and state

1. Review the scoring handoff above and `_history` in `experiments/banking_selective.py`.
   Is "union of initial path and effective recovery path" the history policy you want?
2. `attack_outcome` works on a restored run only because every usable restore point (before
   the executor) has the initial bank state, which matches its replay-from-`pre_state` rule
   per `env_version`. Please confirm this is acceptable or make it segment-aware.
3. An allowed action redone during recovery is reported through `cross_attempt_repeats`
   with `prior_effects_restored` true (selective) or false (full restart). Decide how such a
   redo should be named and counted in each case.
4. Tell D which effects, if any, count as non-restorable (for example an executed
   `send_money`). D will mark them, and recovery will then stop and report them as
   unresolved instead of restoring.

### B (Zhenghao): stages

1. D currently calls the private `BankingWorkflowSession._stage` and sets `env`, `bank`,
   `cb`, `model_calls`, `tool_calls` by hand (`execute` in `experiments/banking_selective.py`).
   Please expose a public stage entry point with the same behaviour; D will switch to it.
2. Check the saved stage context (role, messages, call counts, delivered output). Name any
   other state a resumed stage needs.
3. Decide with D whether verification and regeneration should become recovery nodes. That
   needs the verifier's evidence list saved at the boundary.
4. Confirm that a retryable `ModelRequestError` (429, 5xx, timeout) is safe to retry from a
   checkpoint.
5. Run the real-model smoke command above and report failures.

### C (Yenan): faults and propagation

1. Both notice timings are in the sweep (agreed with D): late, where the faulty action has
   already run, and early, right after the faulty handoff. Check that early selective on
   task 2 reruns only the planner and prevents the wrong rent update.
2. Phase 2 traces contain `fault_injected`, `injection_config` and
   `injection_attempt_finished`, but not B's `injection_observation`. Say if you need it.
3. Extend the propagation analysis using `execution_segment_id` and the history
   evaluation's `details.paths` to separate initial execution, retained stages and reruns.
   Generate sample traces with the scripted command above.
4. Propose with A/B the shared-state case and a case where a saved result can be reused
   (see the first limit), and the development/evaluation split of scenarios. The smoke
   scenario is `t0_pe_accidental_amount`.
