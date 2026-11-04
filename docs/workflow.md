# Model Workflow

## Workflow and prompts

The workflow runs **Researcher → Planner → Executor**, with independent conversation histories:

- **Researcher:** reads bank data and returns facts.
- **Planner:** turns those facts into a plan; it has no callable tools.
- **Executor:** generates actual write-tool calls from the plan.

All tool calls pass through `BankingEnv` for permission checks and audit logging. The workflow does not receive ground-truth answers.

Prompts live in [workflow/prompts.py](../workflow/prompts.py): shared rules in `COMMON`, role instructions in `RESEARCHER` / `PLANNER` / `EXECUTOR` / `VERIFIER`, and task-specific field guidance in `RESEARCH_FORMATS`.
[workflow/session.py](../workflow/session.py) assembles the prompts, user request, upstream messages, and tools.

## Connect a model and run

From the repository root, install dependencies with `uv sync --frozen` and start your OpenAI-compatible model service with tool-calling support.
Set these variables for your own deployment:

| Variable | Meaning |
| --- | --- |
| `MODEL_ID` | Model identifier accepted by your server |
| `MODEL_BASE_URL` | API base URL, including `/v1` where required |
| `PLAN_FORMAT` | `json_schema` if supported, otherwise `prompt_only` |

Run one task:

```sh
.venv/bin/python scripts/run_workflow.py \
  --model "${MODEL_ID:?Set MODEL_ID}" \
  --base-url "${MODEL_BASE_URL:?Set MODEL_BASE_URL}" \
  --plan-format "${PLAN_FORMAT:?Set PLAN_FORMAT}" \
  --task user_task_0 --output runs/new-clean
```

This entry point supports task 0 (bill) and task 2 (rent). For authenticated services, set `OPENAI_COMPATIBLE_API_KEY`.
Use a **new output directory** for every run. See `--help` for configurable budgets; defaults include 1536 output tokens/request and 8 turns/stage.

Run all **16 Banking queries**, once each, without injection or verification:

```sh
.venv/bin/python scripts/run_all_queries.py \
  --model "${MODEL_ID:?Set MODEL_ID}" \
  --base-url "${MODEL_BASE_URL:?Set MODEL_BASE_URL}" \
  --plan-format "${PLAN_FORMAT:?Set PLAN_FORMAT}" \
  --output runs/new-all-queries
```

The all-query runner currently targets local services and does not expose API-key configuration.
Single-task success requires workflow completion plus A's strict score; all-query success requires completion plus native AgentDojo utility. These are different scoring criteria.

### Optional MLX server

For a compatible Qwen3.5-family checkpoint, use an environment with `mlx-lm==0.30.7` and `mlx==0.30.6`.
Set `MLX_PYTHON` to its Python executable, `MLX_MODEL_PATH` to your checkpoint directory, and `MLX_PORT` to an available port:

```sh
HF_HUB_OFFLINE=1 "${MLX_PYTHON:?Set MLX_PYTHON}" scripts/serve_mlx_banking.py \
  --model "${MLX_MODEL_PATH:?Set MLX_MODEL_PATH}" \
  --host 127.0.0.1 --port "${MLX_PORT:?Set MLX_PORT}" \
  --chat-template-args '{"enable_thinking":false}' \
  --decode-concurrency 1 --prompt-concurrency 1
```

The adapter skips unused vision-tower weights in memory; checkpoint files remain unchanged. It serves text only and requires locally available model/tokenizer files.
In another terminal, configure the client variables for this server and use `PLAN_FORMAT=prompt_only`.

## C injection interface

Pass `--injector package.module:make_injector`. For a named scenario, also pass `--scenario-id` and `--injection-site`.

| Site | Boundary | Replaceable body |
| --- | --- | --- |
| `research` | Researcher → Planner | `{"facts": {...}, "notes": "..."}` |
| `plan` | Planner → Executor | `{"actions": [{"tool": "...", "args": {...}}], "notes": "..."}` |

```python
def make_injector(*, task_id, scenario_id):
    def inject(content, site):
        # Locate and validate the target in this detached JSON copy.
        # This skeleton declines injection until a payload is implemented.
        return {"applied": False, "reason": "target_missing"}
    return inject
```

Without a named scenario, the factory receives only `task_id`. After modifying a valid target, return `{"applied": True, "replacement": modified_body, "fault_type": "accidental"}` with the complete body and unrelated content preserved. Never guess a missing target.
B permits at most one replacement per session. Clean/restarted attempts and producer regeneration do not reinject. Injection metadata stays outside model messages; invalid replacement structures fail explicitly.

The eight [reference fixtures](../workflow/fixture_payloads.yaml) are B acceptance fixtures, not C's canonical payload library. Example:

```sh
.venv/bin/python scripts/run_workflow.py \
  --model "${MODEL_ID:?Set MODEL_ID}" \
  --base-url "${MODEL_BASE_URL:?Set MODEL_BASE_URL}" \
  --plan-format "${PLAN_FORMAT:?Set PLAN_FORMAT}" \
  --task user_task_2 --condition fault \
  --injector workflow.threat_fixtures:make_injector \
  --scenario-id t2_rp_accidental_rent_increase --injection-site research \
  --output runs/new-fault
```

## Verification and recovery

Change `--condition` in the fault example to:

- **`always_verify`:** checks each candidate against the user request and original tool evidence before downstream use. Rejection allows one producer regeneration; a second rejection stops the workflow. Omit the injector to test verification on clean inputs.
- **`full_restart`:** D creates a fresh environment and session after controlled fault notification, without reinjection. Earlier violations remain in the historical audit.

Verifier judgments do not replace task scoring. Full restart is simulated recovery, not reversal of an external payment. Automatic fault detection and selective recovery are not implemented.

## Saved traces and results

Successful and failed runs both retain evidence under the selected output directory:

| File | Contents |
| --- | --- |
| `events.jsonl` | Actual model requests/responses, tool calls/results, errors, costs, and state/audit events |
| `artifacts.jsonl` | Intermediate outputs and original/replaced handoffs |
| `evaluations.jsonl`, `summary.json` | Scores and aggregate metrics |
| `workflow_summary.json` | Completion, verification decisions, injection status, and errors |
| `fault_cases.json` | Linked evidence for C's propagation review (labels come from `scripts/analyze_injection_trace.py`) |
| `final_answer.txt` | Final answer saved by the all-query runner |

All-query batches also save `results.json`, `results.csv`, and configuration/source hashes. **`runs/` is Git-ignored:** committing code does not publish experiment traces.

Model pass rates and per-query outcomes are in [results.md](results.md). Run software checks with `.venv/bin/python -m pytest -q`.
