# multiagentic-failure-propagation

CSE 291A (Fall 2026), Option 5: failure propagation and recovery in multi-agent systems, on AgentDojo Banking.

```bash
uv sync && uv run pytest
uv run python scripts/replay_ground_truth.py
```

- `env/`, `eval/`, `tasks/`, `scripts/replay_ground_truth.py`: environment + strict evaluation. See [`env/README.md`](env/README.md).
- `docs/banking_audit.md`: audit of all 16 Banking user tasks.
