# Offline node validity and waste metrics

This D adapter fills the existing validity-based metrics from archived Banking traces. It does not change agents, payloads, A's scorer, the final-run runner, or the team's report script. The original experiment and its costs remain unchanged.

## What is judged

A judgment applies to one **original node execution**, including its own generated output and actions. `valid` means role-appropriate, factually supported, authorized work; `invalid` requires a concrete contradiction or violation; `unknown` means insufficient evidence. This is not task completion, causal attribution to an injection, or certification that an artifact can be reused after restoring state.

For example, rejecting conflicting amounts may be valid work even when the bill remains unpaid. A Researcher output altered *after generation* by C is not automatically invalid Researcher work. Nodes may contain both useful and incorrect substeps; this adapter uses the existing node-granularity metric, not a claim that every token of an invalid node was useless.

The adapter currently supports Phase 1 runs (including full restart and verification). The optional overlay argument on D's summarizer is backward compatible. Phase 2 checkpoint runs are explicitly rejected by this adapter pending segment-aware semantic adjudication; it does not change or bypass Phase 2's reuse checks.

## Inputs and judgments

Inputs are `manifest.json`, `events.jsonl`, `artifacts.jsonl`, `evaluations.jsonl`, and the version-matched A task YAML. Node execution and attempt IDs identify the exact work. Evidence packets contain the original outputs, ancestor artifacts actually used as inputs, node tool events, and A's associated attempt audit classifications. They include free-text notes and final answers, not just numbers. Reference facts are offline evaluation inputs, never sent back to the workflow agents.

The deterministic layer is intentionally narrow:

- Preserve existing non-unknown source work judgments.
- Mark a completed node invalid when A confirms an executed unauthorized action belonging to that node.
- Mark original structured Researcher facts invalid when a numeric value contradicts A's task reference.
- Recognize successful controller setup/request transport separately from semantic agent work.
- Absence of a known error does **not** establish validity. Planner text, refusals, verifier reasoning, and otherwise unresolved nodes need semantic review.

With `--llm`, unresolved completed nodes are sent to a separate tool-free judge using the existing OpenAI-compatible transport. The judge evaluates both structured content and free text against a versioned rubric. The banking plan schema is **not** imposed on judge responses. The response must contain `validity`, `reason`, and evidence IDs in the packet. A schema-valid answer with citations is still a model estimate, not proof of correctness. Review a sample and all disputed cases before treating these as report conclusions; using the same model as the acting agents can produce correlated mistakes.

Fault instructions in notes remain untrusted evidence. The judge has no tools, does not control recovery, and cannot change A's verdict. This is a mitigation, not a general guarantee against judge prompt injection. No causal propagation labels are inferred or overwritten; C continues to own those. The existing C analyzer is not treated as a full-node correctness oracle.

## Commands

Rules only, no network or model requests:

```bash
uv run python scripts/judge_work.py runs/phase1-final \
  --output runs/phase1-work-rules
```

A single run directory containing `manifest.json` can also be supplied. Sweep mode reads completed `trials/*/run_0000` directories; `interrupted/` is not included automatically. An interrupted run may be supplied separately, with incomplete work remaining unknown.

To enable real LLM judgment, explicitly supply a model, endpoint, API-key environment variable, and a **shared call cap for the entire invocation**:

```bash
uv run python scripts/judge_work.py runs/phase1-final \
  --output runs/phase1-work-judged \
  --llm --judge-model YOUR_JUDGE_MODEL \
  --judge-base-url https://tritonai-api.ucsd.edu/v1 \
  --api-key-env TRITONAI_API_KEY --max-judge-calls 40
```

This sends experiment evidence to the configured endpoint and consumes quota. The key is read from the environment and is not written to outputs. No API is called by default. Choose the call cap deliberately; a full sweep may contain many more than 40 unresolved nodes. `--max-packet-chars`, `--judge-max-tokens`, and `--judge-timeout` bound individual requests; timeout does not guarantee server cancellation. No silent truncation or request retries occur. Budget exhaustion, oversized evidence, malformed answers, unknown citations, truncation, or API failures leave judgments unknown. Use a new output directory for a new analysis; completed output is never silently overwritten.

Use `--reference path/to/banking_tasks.yaml` if the archived experiment used a different task configuration. The reference hash is recorded, but this adapter cannot recover an unrecorded historical version. Exact reference alignment is the operator's responsibility.

## Outputs and report handoff

Each source run gets its own output directory, identified by `index.json`:

| File | Purpose |
| --- | --- |
| `judgments.json` | Per-node verdict, reason, source, evidence, rubric/source/reference hashes, implementation revision/hashes, judge settings and separate judge cost |
| `evaluations.jsonl` | **Overlay only**: stage work-validity records, not a replacement for the original evaluations |
| `summary.json` | D's existing summary recomputed with the overlay; model-assisted estimates marked in `work_judgment_provenance` |
| `rule_only_summary.json` | Same calculation excluding new LLM judgments, for coverage/sensitivity comparison |
| `source_summary.json` | Original unaugmented summary |
| `evidence_packets.json` | Exact evidence used for each node |
| `judge_calls.json` | Judge prompts, responses, failures, latency, and available token usage |

D's `invalid_path_cost` requires complete validity coverage; `known_invalid_path_cost` reports the judged invalid subset. `repeated_previously_valid_cost` remains unknown unless the pre-recovery node judgments are complete. `work_validity_coverage` distinguishes missing judgments from zero invalid work. The adapter does not rename failed-attempt downstream cost as invalid work or change the teammate's `phase1_report.py` definitions behind their back.

The report owner can join the derived summary by `run_id` to use these existing D fields. Do not overwrite original results or merge rule-only and hybrid summaries as independent experiment samples. Judge calls are **analysis cost**, not original experiment inference cost; missing usage stays null. The overlay does not bypass D's clean-pair checks and does not change clean comparisons or propagation labels.

For independent review or correction, load the overlay records, revise the relevant verdict/reason/evidence with explicit human provenance, and call `summarize_run(source, write=False, evaluation_overlay=records)` into a separate report. Only work-validity stage evaluations are accepted; foreign node IDs, mismatched attempts/segments, score overrides and conflicting existing judgments are rejected. Keep the original model judgments for audit. This is an API for the report integration, not a second report generator.

## Validation boundary

Regression tests use B's real workflow and A's Banking simulator with scripted model and judge clients. They check cost accounting, audit violations, injection provenance, unknowns, limits, malformed replies, source immutability, and HTTP request construction. Rules-only processing can also run on archived real-model traces. Scripted judge tests do not establish real LLM-judge accuracy, and the adapter cannot certify that a model's semantic judgment is true merely by validating its JSON.

### Live integration check (2026-11-02)

The adapter was also tested against TritonAI `api-glm-5.3-flash` on the five archived October 30 smoke trials. All 24 judge calls completed successfully; 30 nodes had complete judgment coverage (six setup nodes by rule). Judge usage was 166,310 input and 6,685 output tokens, reported separately. The model judged the conflict refusals valid despite failed task completion. Original traces, A scores, and experiment costs remained unchanged. This validates the API path, not the accuracy of every judgment; the judge used the same model ID as the acting agents. Detailed local outputs are stored separately under the project `experiment-results/work-judgments/smoke-llm-20261102` directory and are not committed.

## Exact-request response cache

Add `--cache-dir PATH` to an LLM analysis to reuse saved successful judge responses. The key includes the exact messages (rubric and evidence included), public model settings, endpoint, and transport version. Changed evidence or settings cause a miss. Invalid, truncated, or failed responses are not cached; cached replies are validated again before use. Entries are written atomically, with the first writer retained.

To seed from previously saved analysis and replay without any API calls or credentials:

```bash
uv run python scripts/judge_work.py runs/phase1-final \
  --output runs/phase1-work-replayed \
  --llm --judge-model YOUR_ORIGINAL_JUDGE_MODEL \
  --judge-base-url https://tritonai-api.ucsd.edu/v1 \
  --cache-dir /path/to/local-judge-cache \
  --seed-cache-from runs/phase1-work-judged \
  --cache-only
```

For archives created before compact evidence was introduced, also pass `--full-evidence` to reproduce their original prompts. Use the original judge settings, including token limit and timeout if they differed from defaults. The cache and new output must be separate from the source results and from each other. Without `--cache-only`, misses can call the API within the explicit shared call cap; hits do not consume that cap. In cache-only mode a miss stays unknown (`cache_miss_offline`).

A hit preserves the original model estimate, including its reason and evidence, rather than making a new independent judgment. Per-call `cache_hit`, key, and origin identify reuse. `judge_cost.model_calls` and token totals count only new inference; `cache_hits` counts replay. The saved response retains its historical usage for provenance, which must not be billed again. Cache-hit `duration_ms=0` denotes no inference request, not zero local lookup time.

The cache contains experiment evidence and should stay in private local storage outside the repository. It does not store API keys. This is whole-response caching: it saves calls when repeating an identical analysis, but does not shorten first-time prompts or cache a shared prefix across different requests. Pin model versions when available; a changed server model behind the same alias cannot be detected locally. Use a new cache directory when intentionally requesting fresh judgments. Concurrent first-time misses may still each issue a request; this implementation does not coordinate in-flight inference.

Offline validation imported all 24 successful responses from the earlier five-trial smoke analysis. Replay produced identical node judgments and invalid/repeated-work cost metrics with 24 cache hits, zero new API calls, and zero new inference tokens. This establishes replay equivalence, not judgment accuracy.

## Smaller prompts for new analyses

New judge requests use `compact-evidence-v1` by default. This reduces new-request input size independently of cache hits. Exact repeated strings, lists, and objects of at least 160 serialized characters are stored in an evidence pool and replaced by explicit references. The prompt explains reference resolution. Message order, differing facts and notes, actions, authorization, state effects, tool schemas, and provenance links are preserved. No text is summarized or truncated, and no model generates the compact representation.

Only artifact envelope `schema_version`/`run_id` and event envelope `schema_version`/`run_id`/`timestamp_utc`/`duration_ms` are removed from judge input. Event order, step and audit IDs, attempt/segment identity, all event data (including any timestamps inside tool results), and all task reference information remain. Original packets and traces remain intact for auditing; actual compact requests are saved in judge call logs. Rules operate on original packets before LLM processing, and evidence citations are checked against original IDs, not pool keys. The decisive rules are unchanged; arbitrary textual correctness still needs semantic judgment.

Use `--full-evidence` for the previous prompt representation, including replay of old cache entries. Compact prompts have different cache keys and must not reuse judgments from full prompts. `request_packet_hash` identifies sent evidence separately from the original `packet_hash`. Compaction implementation hashes and the selected mode are recorded in analysis provenance.

Offline measurement on the 24 existing smoke requests reduced total system-plus-user prompt characters from 508,282 to 424,064 (16.57%, including encoding instructions). All 24 expanded packets exactly matched the projected originals. These are character counts, not model-token measurements or an accuracy benchmark. No API calls were made for this check. Exact evidence preservation does not guarantee identical model judgments when the presentation changes; validate a sample before treating compact-judge results as equivalent in the report.

### Compact-prompt API smoke comparison (2026-11-02)

A subsequent authorized API check reran the same 24 judge requests with compact evidence, the same model/settings, and no cache. All 24 calls succeeded. API-reported input tokens fell from 166,310 to 128,407 (22.79%); output tokens increased from 6,685 to 8,564. Total tokens fell from 172,995 to 136,971 (20.82%). All 30 node validity labels (including six rule judgments) and derived waste metrics remained unchanged. Original source hashes matched; results are saved separately in the local `experiment-results/work-judgments/smoke-compact-llm-20261102` directory. This is a small smoke comparison, not a general accuracy or savings guarantee. The full local regression suite passed 653 tests.
