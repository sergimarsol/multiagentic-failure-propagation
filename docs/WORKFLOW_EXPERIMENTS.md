# D / B Phase 1 experiment integration

This optional D entry point runs B's existing `BankingWorkflowSession` through
`run_phase1`. It does not replace B's pipeline, prompts, verifier, C hooks, or A's
scorer. `scripts/run_workflow.py` and `scripts/run_matrix.py` remain unchanged.
No checkpoint or Phase 2 context export is required.

## Scripted integration check

From the repository root, with locked development dependencies installed:

```sh
uv run python scripts/run_workflow_experiment.py \
  --config configs/d_b_workflow_smoke.json --scripted \
  --output runs/db-smoke-new
```

Use a new output directory. This runs B's actual workflow with the existing
`tests.test_workflow.FakeModel`, A's real Banking simulator/scorer, and explicitly
labelled B reference fault fixtures. It needs the tests checkout and development
dependencies; it makes no HTTP model requests. Its success is software validation,
not model performance. The example contains tasks 0 and 2 with five conditions:

| D condition | Behavior |
|---|---|
| `clean` | No injector, no verifier |
| `fault` | One-shot injected handoff, no recovery |
| `full_restart` | Initial fault, then fresh environment/session with injection disabled |
| `clean_verify` | No injector; B verifies each handoff |
| `always_verify` | Injected handoff plus B verification/regeneration |

B's old CLI can express clean verification as `always_verify` without an injector.
This new configuration path explicitly uses `clean_verify`; it does not relabel
old results. In the new path `always_verify` requires fault metadata and an injector.

## Final-run controls

Keep all five conditions above in the final experiment scope, including `clean_verify`.
The existing `configs/d_phase1_matrix.template.json` and
`configs/d_b_workflow_smoke.json` already include that control for both tasks.
For each task, repeat and locked model/budget configuration, run clean and clean_verify
once as shared controls; do not duplicate those observations for each fault scenario.
The other three conditions run separately for each selected C scenario.

Use C's `faults/payloads.yaml` and `faults.injection_harness:make_injector` for final
fault runs (with `fixture: false`), not the smoke example's B fixtures. Lock the commit,
resolved model settings, budgets and repeats before execution. First check one scenario
through all five conditions, including the two no-injection controls.

`scripts/run_phase1_final.py` builds and runs exactly this scope: tasks 0 and 2, C's 10
scenarios × fault/full_restart/always_verify, plus clean and clean_verify once per task,
each repeated (default 5 → 170 trials). It refuses a working tree with uncommitted
changes and records the commit, machine and arguments in `run_info.json`. Each trial
is its own batch, so the sweep resumes after a stop: finished trials are skipped,
an interrupted one is kept under `interrupted/` and rerun. All finished trials are
summarized together in `summary/` (`runs.csv`, `condition_summary.csv`).

The locked setup is `configs/phase1_final.json`: TritonAI `api-glm-5.3-flash`,
`json_schema` plans, temperature 0, 5 repeats (the settings of the 2026-10-30 smoke
run). Set `TRITONAI_API_KEY` in the shell, never in a file.

```sh
# 1. pipeline check with no model (seconds)
uv run python scripts/run_phase1_final.py --scripted --output runs/final-check
# 2. one scenario through all five conditions with the real model
uv run python scripts/run_phase1_final.py --profile configs/phase1_final.json --smoke --output runs/final-smoke
# 3. the full sweep (new output)
uv run python scripts/run_phase1_final.py --profile configs/phase1_final.json --output runs/phase1-final
```

Do not change the profile, flags or commit midway; a resumed sweep with different
settings is refused. Runs with other settings (e.g. `json_object`) are exploratory and
reported separately.

Report `clean_run_rejection_rate` as a clean-run rejection proxy, not as confirmed
unnecessary denial. Inspect its sample count and `clean_verify_runs_without_decision`.
A natural model error may justify a rejection even without an injection; see
[measurement definitions](MEASUREMENT_GUIDE.md#security-and-utility-together).

## Real model execution

Copy the example to your own configuration, then:

- Set `synthetic: false` and omit `--scripted`.
- Set `workflow.model.model` to the deployed model identifier and
  `workflow.model.base_url` to its existing OpenAI-compatible endpoint.
- Specify supported `ModelConfig` fields: temperature, max_tokens,
  timeout_seconds, and plan_format as appropriate. Unspecified fields resolve to
  B's existing defaults and the resolved values are saved. Unsupported settings,
  including a provider seed that B cannot transmit, are rejected.
- Set `workflow.runtime` to the backend/version/quantization description. This is
  operator-supplied provenance, **not** a verified server identity. Pin the server
  and model externally; changing backend state behind the same URL is not detected.
- Set `workflow.session` to any required B WorkflowConfig limits/retries. Defaults
  are expanded in the saved configuration, so the actual settings are comparable.
- Set explicit trial `limits` and your task/scenario/repetition identifiers.
- For faults, set `fault.injector_factory` to C's `module:factory`, called with
  `task_id` and `scenario_id`. The returned callable follows B's existing contract.
  `fault.site` is `research_to_plan` or `plan_to_execute`; D maps it to B's site.
  `fault.family` is accidental/adversarial. `fault.payload_ref` must name a local
  versioned payload file; D records its SHA-256 and rejects changed supplied hashes.
  C remains responsible for ensuring the factory uses that declared payload.
  Declare `fault.fixture` explicitly. B's reference payloads must remain labelled
  fixtures, including when used with a real model; they are not canonical C cases.

```sh
uv run python scripts/run_workflow_experiment.py \
  --config /absolute/path/my-config.json --output runs/db-real-new
```

The script neither starts a model service nor downloads weights. The API key is
read only from the environment variable named by `--api-key-env` (default
`OPENAI_COMPATIBLE_API_KEY`). Do not put credentials in JSON or endpoint URLs.
Runtime labels and payload files should contain no credentials either.

The bridge resolves prompt/schema hashes, source hashes and initial-state hashes
before execution and rechecks pinned values before each trial. It records the
configuration as D's `manifest.json.experiment`. Model configuration on an injected
client is checked before the workflow starts. Fault/controller settings never
enter model conversations. Fresh sessions are created by B on each full restart.

## Budget semantics

| Limit | Lifetime / enforcement |
|---|---|
| D `limits.model_calls`, `tool_calls` | One trial, including both restart attempts, verifier calls and retries; checked before calls |
| D `limits.tokens` | Optional observed token ceiling shared across attempts; checked between calls |
| D `limits.wall_seconds` | Shared cooperative wall-time ceiling; checked between calls/workflow boundaries |
| B `workflow.session` limits | Existing per-session/per-stage ceilings; fresh sessions reset these, not D's ledger |
| B model `timeout_seconds` | HTTP request timeout; not a guarantee of server-side cancellation |

The stricter applicable ceiling wins. Unknown token usage remains null. With a
finite token budget, unknown usage prevents subsequent calls rather than being
counted as zero. A completed call can overshoot a token/time ceiling; this is not
hard preemption. Do not set an overly small trial budget and interpret restart
budget exhaustion as a failure of the recovery algorithm without reporting it.

Verification and retry costs are subsets of total cost, not costs to add again.
Verification includes model calls and verifier read tools. Recovery cost also
contains its constituent calls, so subset categories can overlap. Failed provider
requests consume invocation budget; absent usage stays unknown.

## Outputs and comparisons

The existing `run_batch` and `aggregate` produce `result_*.json`, `runs.csv` and
`condition_summary.csv`. Each run also has D traces and B's `workflow_summary.json`
and `fault_cases.json`. Workflows that fail remain observations, not dropped rows.
The CLI returns nonzero for dispatch/execution errors or incomplete traces; inspect
CSV verdicts for task failures (a completed but incorrect task is still a result).

CSV columns include initial/final task verdicts, initial/final workflow completion,
permissions, historical unauthorized actions, cross-attempt repeat reports,
budgets, verification/retry/recovery cost and explicit clean-pair status. Existing
`completion` columns mean A's task utility; the separate `*_workflow_completed`
columns mean B's control flow finished. Neither is a propagation label.

Pairing uses D's existing configuration key: task, model/backend, prompts/source,
initial state, session/trial limits, comparison group and repeat index must match.
Condition, scenario and fault treatments may differ. The observed initial states
must match too. Missing, mismatched, ambiguous or incomplete clean comparisons
have unknown paired outcomes and cannot enter the matched-clean subgroup.
A matched clean run can itself fail; it is not assumed successful.

Different scenarios remain separate statistical groups. Duplicate repeat indices
are flagged and confidence intervals suppressed as in the existing D statistics;
these rows are observations, not certified independent replications. Duplicate
nonempty run IDs are rejected outright so one source run cannot count twice.
Unannotated C propagation judgments remain unknown even if the injection was
triggered or the model received it. All original failure/error evidence is kept.

## Rebuild summaries without modifying source traces

```sh
uv run python scripts/run_workflow_experiment.py \
  --summarize runs/db-real-new/run_0000 runs/db-real-new/run_0001 \
  --output runs/db-summary-new
```

The destination must be new and separate from source runs. This uses
`summarize_run(write=False)` and existing aggregate/statistics code. Interrupted
traces retain measurable calls and unknown/incomplete outcomes. An unreadable
trace produces a retained error row, not a fabricated success. Source summary
files are not overwritten. Legacy B runs without D experiment configuration are
rejected because their pairing controls/repeat identities cannot be reconstructed
reliably; B's original reporting remains available for those runs. An external
process kill cannot guarantee a terminal event; use this importer on retained
configured directories to report partial traces.

## Team handoff and validation boundary

- **D:** owns configuration binding, shared ledger, provenance, import and metrics.
- **B:** existing session/client and old commands work unchanged. Review the new
  optional settings; no checkpoint implementation is requested.
- **C:** supplies canonical payloads and semantic propagation judgments. B's
  fixture labels and pending evidence are not substitutes for those judgments.
- **A:** scoring and duplicate-action semantics are unchanged.

Tests exercise both Banking tasks, five conditions, shared restart budgets,
verification read costs, request retries, unknown usage, config mismatches,
untriggered injections, interrupted traces and duplicate-source rejection using
B's real workflow class. Real-model trials through this new entry point have not
been run. Phase 2 behavior is unchanged by this integration.
