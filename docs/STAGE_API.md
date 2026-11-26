# B stage API and completed-boundary state

`BankingWorkflowSession.run_stage` lets D execute B's Researcher, Planner,
Executor, and Verifier without assigning session fields or calling `_stage`.
It uses the same stage loop, prompts, permissions, request handling, format
validation, and retries as Phase 1. `run()` and the Phase 1 pipeline are unchanged.

## Call contract

```python
from workflow import BankingWorkflowSession, StageState

# bank is the existing BankingAdapter; callbacks belongs to the same recorder.
# request_id names {"user_request": bank.env.prompt} in that recorder.
# research_id is the result actually delivered after the research handoff.
result = BankingWorkflowSession(client, session_config).run_stage(
    "plan",
    environment=bank,
    callbacks=callbacks,
    request_id=request_id,
    inputs={"research": research_id},
    state=StageState.from_dict(research_context["resume_state"]),
)
planner_output_id = result.artifact_id
planner_context = result.to_context()
```

Each session executes once: either one `run()` or one `run_stage()`. A stage
always starts with a fresh dialogue. Recovery restarts whole stages at completed
boundaries; this is not an API for resuming a partially completed model turn.
After a failure there is no successful `StageResult` to checkpoint.

| Stage | Input artifact map | Model-visible input beyond the user request |
| --- | --- | --- |
| `research` | `{}` | None; the model reads through its permitted tools |
| `plan` | `{"research": delivered_id}` | Delivered research content |
| `execute` | `{"plan": delivered_id}` | Delivered plan content |
| `verify_research` | `{"candidate": delivered_id}` | Research candidate, original tool evidence |
| `verify_plan` | `{"candidate": delivered_id}` | Plan candidate, original tool evidence |

Only an initial `research` call accepts `state=None`. Continuation stages require
the preceding completed-stage state. Input artifacts, evidence and feedback must
belong to the same recorder and attempt. The request must match the bank's task.
Role and payload shape are derived from the stage name. As in Phase 1, validation
checks handoff structure, not the correct amount, recipient or authorized action.

The caller selects the *delivered* upstream IDs. It must not substitute the
producer's pre-injection output or load old dialogue messages into a rerun.

## State ownership

`StageState` is immutable and serializes through `to_dict()` / `from_dict()`.
Version `b-stage-state-v1` requires all fields; unknown fields and versions are
rejected. There is no fallback that silently drops missing evidence or counters.

| Owner | Data | Meaning |
| --- | --- | --- |
| B `StageState` | `run_id`, `attempt_id`, `config_hash`, `version` | Reject foreign or incompatible continuation state before model calls |
| B `StageState` | `model_calls`, `tool_calls` | Cumulative counts on the effective workflow path, including verification/regeneration |
| B `StageState` | `evidence_ids` | Original researcher/verifier tool-result artifacts, available to subsequent verification |
| B `StageResult` | `artifact_id`, `execution_id`, input/control IDs | Actual stage output, supplied data inputs and regeneration feedback |
| B node context | `messages`, per-stage call deltas, `generation`, `resume_state` | Audit history and successful boundary state; messages are never restored as a new dialogue |
| D workflow state | Completed/pending nodes, artifact map, resource versions | Execution position, delivered upstream selection and bank-state dependencies |
| D bank checkpoint | Bank snapshot and effective audit path | Simulator restore, including the selected real-bank policy |
| D coordinator | Injection enablement, fault notice, retry policy | Initial injection and recovery scheduling; reruns disable injection |
| D recorder/budget | All events, calls, tokens, timing and effects | Trial-wide history and limits, never rolled back with a checkpoint |

B's configuration fingerprint binds the task/request, model settings, session
limits, prompts, research format, plan schema, role permissions and tool schemas.
D additionally checks its checkpoint/configuration hashes. A state is valid only
with its original artifact store and D-selected consistent bank/workflow snapshot;
`run_stage` does not decide whether a checkpoint is uncontaminated.

The current Banking adapter is a chain. D takes the cumulative `resume_state`
from the last completed node in the restored prefix. An independent-branch
workflow would need an explicit state/evidence merge contract, not this rule.

## Output lineage and delivery

D's adapter returns `NodeOutput(result.artifact_id)`. `DagWorkflow` verifies that
this source belongs to the current node execution and makes its handoff artifact
depend on that source. The dependency chain is therefore:

```text
request + upstream artifacts + model responses + tool evidence
    -> B stage_artifact_id
    -> D produced_artifact_id
    -> D delivered_artifact_id (a replacement only when injection applied)
    -> downstream model requests and outputs
```

`stage_artifact_id` is the B result with detailed model/tool ancestry.
`produced_artifact_id` is the outer DAG handoff before injection.
`delivered_artifact_id` and `delivered_output` record what the consumer actually
receives. Plain JSON-returning DAG adapters remain supported.

## Verification and regeneration

The public stage API supports the existing B verification semantics:

1. Run the producer and apply the single injection at the handoff, if enabled.
2. Run `verify_research` / `verify_plan` with the delivered candidate and the
   producer's continuation state. Evidence is reconstructed from original tool
   artifacts; a pristine pre-injection summary is not supplied as an answer.
3. On rejection, run the producer in a fresh session with the same legitimate
   upstream input IDs, `feedback=verdict.artifact_id`, `generation=1`, and the
   verifier's continuation state. No injection is performed by the stage API.
4. Verify the regenerated output with `generation=1`. Stop on another rejection,
   matching the configured Phase 1 regeneration limit.

Feedback is recorded as a control dependency and is included in actual model
inputs. Verifier tool calls extend the evidence list and count against the same
path/session limits and D's trial budget. Executor regeneration is not supported.

**Integration decision for B/D review:** keep Phase 2's current three-node
oracle-recovery experiment unchanged in this patch. `always_verify` continues to
use Phase 1. Verifier/regeneration primitives are available and tested through the
public API, but adding them to D's recovery graph requires deciding checkpoint
placement and representing the conditional retry explicitly. They are not yet
automatic fault detection or recovery nodes, and this proposal is not team approval.

## Failures and costs

`ModelRequestError.retryable` describes a terminated model request, not an
idempotent stage. Earlier tool calls may already have changed the bank. D records
those effects, restores a checkpoint under its bank policy, then creates a fresh
session. A real-bank restore still retains irreversible payments. Never retry an
in-flight/ambiguous external action by treating it as a model-provider error.

Restored path counters enforce the original per-pass session limits. Trial-wide
costs retain failed/replaced executions. For example, the scripted faulty-plan
case uses 5 initial calls plus 3 recovery calls: the final continuation state has
5 calls on the effective path, while the recorder/budget reports all 8.

## Validation

```sh
.venv/bin/python -m pytest -q tests/test_stage_api.py tests/test_banking_selective.py \
    tests/test_selective_recovery.py tests/test_workflow.py
.venv/bin/python scripts/run_phase2_banking.py --scripted --repeats 1 \
    --output runs/b-stage-api-scripted
```

Tests compare clean and verified public-stage requests/tools/costs with Phase 1
for both tasks, and compare rejected research/plan regeneration with Phase 1.
They also check serialization, configuration/attempt rejection, session isolation,
input/control ancestry, evidence retained through checkpoints, global versus path
costs, and a retryable provider failure after a recovery tool has already written.

For the real-model smoke, use the committed local revision with
`configs/phase1_final.json`, `TRITONAI_API_KEY` in the process environment, and
`scripts/run_phase2_banking.py --profile configs/phase1_final.json --smoke`.
Use a fresh output directory. The final-run CLI requires committed tracked changes
so its recorded revision is reproducible; a local commit does not require a push.
