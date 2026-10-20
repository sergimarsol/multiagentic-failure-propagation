# Progress: environment and evaluation

Status as of 2026-10-20. PR: https://github.com/sergimarsol/multiagentic-failure-propagation/pull/1

## Done

- **Pinned setup**: agentdojo 0.1.35, benchmark v1.2.2 (the newest available), all dependencies locked to releases at least two weeks old (`uv.lock`). `v1.2.2` does not change `user_task_0` / `user_task_2`. Details are in `env/PINS.md`. Tested on Python 3.12 and 3.10.
- **Environment** (`env/banking_env.py`): `BankingEnv` wraps AgentDojo Banking without modifying it.
  - `load` / `reset` / `snapshot` / `restore`
  - `call_tool` with role permissions from `env/roles.yaml`: researcher and verifier = read, executor = write, planner = none. Blocked calls are logged but not executed.
  - a per-call state diff
  - `tool_schemas(role)` for the LLM, and `state_dict()`
  - an append-only JSONL audit log that survives resets (the "external world")
- **Task configs** (`tasks/banking_tasks.yaml`): user_task_0 (pay the bill) and user_task_2 (adjust the rent).
  - critical facts and where they live
  - allowed writes (the only authorized actions)
  - injectable fields for fault injection
  - Prompts and ground truth come from AgentDojo at runtime. Adding a task only means adding a YAML entry.
- **Strict evaluation** (`eval/scoring.py`): `evaluate()` reports AgentDojo's utility unchanged, plus `strict_success`, unauthorized attempted vs. executed actions, and every unexpected state change. A correct payment cannot hide an extra one. `find_value_in_calls` / `find_value_in_state` check whether a canary value propagated.
- **Scripted replay** (`scripts/replay_ground_truth.py`): no-LLM pipeline. Both tasks are strictly successful. `replay(task, overrides=..., extra_calls=...)` simulates faulty runs.
- **Tests**: 34 pytest tests (`tests/test_env_eval.py`).
- **Docs**: `env/README.md` (API for the workflow, injection and tracing parts), `docs/banking_audit.md` (all 16 Banking tasks).

## Findings to share with the team

- user_task_2's new rent (1200) is not written anywhere. It is 1100 (scheduled tx 7) + the notice's "+100".
- The utilities of user_task_5 and user_task_6 pass with no action at all (broken upstream).
- A full restart re-executes the legitimate payment, so the bill gets paid twice in the audit history.
- AgentDojo quirks: `send_money` doesn't change the balance; `update_*` tools ignore falsy values; nested tool-call args would bypass role checks (we reject them); there is a circular import (use `env/_agentdojo.py`).

## Open / next

- Team decisions: the open questions in the PR (restart reporting, whether denied reads count as unauthorized, executor having no read tools, task 2 injection target).
- Agree on message/trace formats with the workflow and tracing owners. Check `tool_schemas` against the local model's tool-call format.
- Later: add tasks 3/4/13/15 (config-only), and check that the always-verify condition never uses an LLM for scoring.
