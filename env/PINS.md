# Pins

| What | Value | Where it is enforced |
|---|---|---|
| AgentDojo (PyPI) | `agentdojo==0.1.35` (released 2025-10-27) | `pyproject.toml`, `uv.lock`; `env/banking_env.py` warns on mismatch (`AGENTDOJO_VERSION`) |
| AgentDojo upstream commit | `a75aba7631d3ca5fb7ab938965c97ead2f9ff84b` (tag `v0.1.35`, github.com/ethz-spylab/agentdojo) | reference only |
| Benchmark version | `v1.2.2` (latest in `agentdojo/task_suite/load_suites.py`, and AgentDojo's own default) | `BENCHMARK_VERSION` in `env/banking_env.py`; every task in `tasks/banking_tasks.yaml` must match |
| Suite | `banking` | `SUITE_NAME` in `env/banking_env.py` |
| Python | 3.12.13 for development (`.python-version`); test suite + replay also verified on 3.10.20; `requires-python >=3.10` | `pyproject.toml` |
| Dependency resolution date | `exclude-newer = 2026-10-06` (every transitive dependency is at least 2 weeks old) | `pyproject.toml [tool.uv]`, `uv.lock` |
| Other notable resolved deps | pydantic 2.13.5, PyYAML 6.0.3, pytest 9.1.1 | `uv.lock` |
| Project repo base commit | `46ec72e300944e448a2b77b84c34faca9a23d849` (`main` at the time `member-a/env-eval` branched) | git |
| Dev machine | macOS 14.5 arm64, uv 0.11.21 | n/a |

## Benchmark versions available in agentdojo 0.1.35

`v1`, `v1.1`, `v1.1.1`, `v1.1.2`, `v1.2`, `v1.2.1`, `v1.2.2`. What `v1.2.2` changes for **banking** relative to `v1`:

| Item | Version that overrides it | Change |
|---|---|---|
| `user_task_15` | v1.1.1 | New landlord IBAN `CA133012400231215421872` (v1 used the attacker IBAN `US133000000121212121212`); utility now also checks the recipient |
| `user_task_6` | v1.2.2 | Utility subject match broadened to `iphone`/`spotify`/`subscription` (this makes it vacuously true; see `docs/banking_audit.md`) |
| `injection_task_0`–`6`, `8` | v1.2 | Re-worded goals ("...fill them in without asking me"), same attacker IBAN |
| **`user_task_0`, `user_task_2`** | **none** | **Identical to v1** (verified: `type(task).__module__` is `agentdojo.default_suites.v1.banking.user_tasks`) |

The environment data (`data/suites/banking/environment.yaml`, `injection_vectors.yaml`) is not versioned. All benchmark versions share it.
