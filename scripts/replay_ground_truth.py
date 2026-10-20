"""Scripted, no-LLM pipeline: replay AgentDojo's ground-truth calls through BankingEnv and score them.

    uv run python scripts/replay_ground_truth.py                 # all configured tasks
    uv run python scripts/replay_ground_truth.py --task user_task_0

Importable for teammates who need a "faulty run" before the LLM workflow exists:

    from scripts.replay_ground_truth import replay
    out = replay("user_task_0", overrides={"send_money": {"amount": 1337.42}})
    out = replay("user_task_0", extra_calls=[("executor", "send_money", {...})])
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from env._agentdojo import BankingEnvironment  # noqa: E402
from env.banking_env import BankingEnv  # noqa: E402
from env.tasks import load_task_configs  # noqa: E402
from eval.scoring import EvalResult, evaluate  # noqa: E402

DEFAULT_OUT = ROOT / "runs" / "replay"


@dataclasses.dataclass
class ReplayResult:
    task_id: str
    run_id: str
    eval: EvalResult
    pre_state: BankingEnvironment
    post_state: BankingEnvironment
    audit: list[dict[str, Any]]  # this run's audit entries
    env: BankingEnv  # still loaded, in the post state


def replay(
    task_id: str,
    overrides: dict[int | str, dict[str, Any]] | None = None,
    extra_calls: list[tuple[str, str, dict[str, Any]]] | None = None,
    *,
    env: BankingEnv | None = None,
    run_id: str | None = None,
    audit_path: str | Path | None = None,
) -> ReplayResult:
    """Run `task_id`'s ground truth (reads as researcher, writes as executor) and evaluate it.

    overrides: change args of ground-truth calls before they run. Keys are either the call's index
        in `ground_truth()` (int) or a tool name (str, applies to every call of that tool), e.g.
        {"send_money": {"amount": 1337.42}} or {2: {"id": 6}}.
    extra_calls: (role, tool, args) calls executed after the ground truth, e.g. an injected extra payment.
    env: reuse an existing BankingEnv (it is re-loaded, its audit log is kept).
    """
    env = env or BankingEnv()
    if audit_path is not None:
        env.set_audit_path(audit_path)
    env.load(task_id)
    run_id = run_id or f"replay-{task_id}-{uuid.uuid4().hex[:8]}"
    pre = env.snapshot()
    overrides = overrides or {}

    for i, call in enumerate(env.user_task.ground_truth(env.snapshot())):
        args = dict(call.args) | overrides.get(call.function, {}) | overrides.get(i, {})
        role = "executor" if env.roles.kind(call.function) == "write" else "researcher"
        env.call_tool(role, call.function, args, run_id=run_id, step_id=f"gt-{i}")
    for j, (role, tool, args) in enumerate(extra_calls or []):
        env.call_tool(role, tool, args, run_id=run_id, step_id=f"extra-{j}")

    post = env.snapshot()
    audit = env.audit_log(run_id)
    result = evaluate(task_id, pre, post, audit, model_output=env.user_task.GROUND_TRUTH_OUTPUT)
    return ReplayResult(task_id, run_id, result, pre, post, audit, env)


def _print_table(rows: list[dict[str, Any]]) -> None:
    cols = list(rows[0])
    widths = {c: max(len(c), *(len(str(r[c])) for r in rows)) for c in cols}
    print("  ".join(c.ljust(widths[c]) for c in cols))
    print("  ".join("-" * widths[c] for c in cols))
    for r in rows:
        print("  ".join(str(r[c]).ljust(widths[c]) for c in cols))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--task", action="append", help="task id (repeatable); default: all configured tasks")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="output directory for audit JSONL")
    a = parser.parse_args(argv)

    a.out.mkdir(parents=True, exist_ok=True)
    rows, ok = [], True
    for task_id in a.task or list(load_task_configs()):
        audit_path = a.out / f"{task_id}.audit.jsonl"
        audit_path.unlink(missing_ok=True)
        r = replay(task_id, audit_path=audit_path, run_id=f"replay-{task_id}")
        (a.out / f"{task_id}.eval.json").write_text(json.dumps(r.eval.to_dict(), indent=2))
        rows.append(r.eval.summary() | {"audit": str(audit_path.relative_to(ROOT) if audit_path.is_relative_to(ROOT) else audit_path)})
        ok &= r.eval.strict_success
        for reason in r.eval.failure_reasons:
            print(f"[{task_id}] FAIL: {reason}")
        for warning in r.eval.warnings:
            print(f"[{task_id}] WARNING: {warning}")
    _print_table(rows)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
