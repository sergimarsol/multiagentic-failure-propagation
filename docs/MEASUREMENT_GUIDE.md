# What to measure, and why these baselines exist

This guide maps systems-evaluation terminology to our project. It does not claim
that the instructor requires a separate experiment for every example below.
Course requirements: [Overview and Option 5](https://docs.google.com/document/d/1WrtrdqP54ATGRPtq9TN3vkBFc1F8ESvkqWu9AAuvIkU/edit?tab=t.6lujzhx8viex).

## Phase 1 baselines

| Condition | Research question |
|---|---|
| Clean | Can the workflow complete the legitimate task without injected faults? |
| Fault, no recovery | Does the fault actually propagate, and what damage/work does it cause? |
| Fault + full restart | How much correctness can fresh reexecution recover, at what total cost? |
| Fault + always verify | Can checking intermediate outputs before consumption prevent propagation, and what does checking/regeneration cost? |
| Clean + verification (diagnostic) | What overhead and false rejection occur even without injected faults? |

Full restart is deliberately simple. It provides the cost/correctness reference for
Phase 2 selective recovery: does preserving valid work help compared with redoing
all stages? It is not a guaranteed-correct oracle, cheapest policy, or proof that
external effects can be undone. Preserve the original run's actions and cumulative
cost, and separately score the rerun.

Always-verify is a prevention baseline: it checks before downstream consumption,
while restart responds after the experiment's fault notification. Its benefit must
justify verifier calls, evidence reads, false rejections and bounded regeneration.
Do not give it injected-fault labels or hidden reference answers. The scripted
pass-through verifier only validates instrumentation; it is not this baseline.

In Phase 2, full restart and selective recovery receive the same supplied faulty
result identity at the same boundary. Their comparison isolates recovery more
closely. Always-verify uses a different intervention time and information path, so
its end-to-end result compares checking/prevention with reactive recovery, not pure
recovery quality with perfectly matched detection. Keep model, task, fault schedule,
initial state and total resource budgets controlled and state this distinction.

## Microbenchmarks, macrobenchmarks, execution and traces

| Term | Meaning here | Example and useful measurements |
|---|---|---|
| Microbenchmark | Isolate a small mechanism and control its input size | Save/restore a snapshot at several state sizes; record bytes and time. Measure JSONL recording for fixed event sizes. In Phase 2 vary dependency-graph size and measure invalidation/reuse-check time. |
| Macrobenchmark | Evaluate the complete application under realistic workflow conditions | Run Banking task 0/2 through researcher, planner and executor using the actual model; compare the four main conditions on correctness, propagation, safety, total latency and inference cost. |
| Application execution | Actually run the workflow, inference and tools to obtain outcomes | One complete payment/rent-update trial; trace recording accompanies it. |
| Trace | Timestamped evidence of what occurred during execution | Actual model messages, input/output artifacts, tool calls, state changes, failures, verification and recovery decisions. |
| Trace analysis/replay | Recompute metrics or replay recorded actions without necessarily invoking the original model | Re-summarizing JSONL validates analysis. Replaying fixed actions validates tools/scoring; neither demonstrates that a model can generate those actions. |
| Metrics | Quantities measured about the execution or mechanism | Completion rate, unauthorized attempts/executions, propagated stages, invalid/repeated work, latency, calls and tokens. |
| Baselines | Comparison conditions used to interpret those quantities | Clean, unhandled fault, full restart and always-verify. Metrics alone do not explain whether a proposed mechanism improves anything. |

Unit tests check software invariants, not benchmark performance. Running 224 tests
successfully is not 224 agent experiments. Similarly, a toy workflow can test real
recovery code without establishing real Banking agent performance.

## Practical Phase 1 measurement scope

Prioritize a runnable model workflow and meaningful propagation over new framework
features. Complete paired clean/fault runs, full restart and actual verification,
then inspect traces with A/C's task and propagation rules. Repeat fixed-condition
experiments and retain failed/time-limited runs. Separate development choices from
evaluation runs, record model/runtime/prompt/payload versions, and keep initialization
or warmup separate where they affect the intended use case.

For D, a small optional microbenchmark can measure recorder cost with fixed events
or compare otherwise identical scripted executions with recording enabled/disabled.
The existing sum of write timers is only partial overhead; it is not a controlled
measurement of total instrumentation cost. Checkpoint microbenchmarks belong to
Phase 2 and should not become a prerequisite for the team's Phase 1 integration.
No standalone microbenchmark harness is added by this documentation.

Use traces to connect an observation to its cause. Example: an incorrect amount was
injected, the planner actually received it, the plan adopted it, the executor called
payment with it, and the state changed. Exposure alone is not propagation; an ignored
message is not successful recovery. Missing judgments remain unknown.

## Security and utility together

Report four numbers per condition, from `condition_summary.csv`:

| Number | Column | Meaning |
|---|---|---|
| Clean utility | `completion_rate` in the `clean` rows | task success with no fault |
| Utility under attack | `completion_rate` in the fault rows | task success with the fault |
| Attack success | `attack_success_rate` | share of fully judged fault runs where the fault reached its goal in the bank (A's `attack_outcome`) |
| Clean-run rejection (proxy only) | `clean_run_rejection_rate` in the `clean_verify` rows | share of verified clean runs with at least one verifier rejection; not an adjudicated false-rejection rate |

Attack success is only judged for runs injected from `faults/payloads.yaml` (`attack.status` in each
summary: `evaluated`, `incomplete`, `error`, or `not_applicable`). The rate only uses `evaluated` runs: an incomplete
run can show that an attack happened but never that it did not, so counting it would bias the rate upward. Incomplete
and errored runs are counted in `attack_unknown`; report them next to the rate. It counts the first, injected attempt even
when a restart later succeeds; `initial_attack_success` holds that attempt alone. Clean-run rejection only
uses `clean_verify`. No injected fault does **not** imply a correct intermediate output:
the model may make a natural error, and rejecting that output is appropriate. Even eventual
task success cannot establish that an earlier rejection was unnecessary, since regeneration
may have repaired the output. True false rejection needs an independent judgment of the
specific rejected artifact; D does not infer this from clean status or final utility.

The legacy `unnecessary_denial_rate` columns retain their numeric values as deprecated
aliases, identified by `unnecessary_denial_rate_status`. Use `clean_run_rejection_rate`
in new reports; `false_rejection_status=not_evaluated` explicitly distinguishes the
missing semantic judgment. In fault conditions, only raw verifier decision/rejection counts
are reported. Runs whose verifier never decided are excluded from this rate's denominator
and counted in `clean_verify_runs_without_decision`; report that count beside the rate.

In `always_verify`, natural model errors and injected faults can coexist in the same
artifact. A rejection alone does not establish detection of the injected fault: review
the rejected artifact version and the verifier's stated reason against the injected change.
Keep ambiguous attribution unknown. Post-rejection regeneration or a fault-disabled restart
does not turn the original trial into a `clean_verify` observation; conditions remain
assigned at the trial level.

## Reporting variation

Batch CSVs now include sample standard deviation, median, P95, range and per-metric
sample counts, plus Wilson 95% intervals for supported rates. See the shared protocol
for exact formulas/conventions and missing-value handling. Do not infer statistical
significance from overlapping/nonoverlapping intervals alone; treatment differences
need an appropriate analysis, especially for paired runs. With very few repeats,
show raw observations and sample counts and describe conclusions as preliminary.

Preserve fixed tasks and settings within each reported group. A seed is part of the
recorded configuration; the current grouping conservatively separates changed config
values. Plan how independent repetitions are represented before launching the matrix.
Do not mix two tasks, two models, or different retry budgets merely to increase n.
