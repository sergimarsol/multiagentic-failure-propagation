"""Task configurations: tasks/banking_tasks.yaml + AgentDojo's own task definitions."""

from __future__ import annotations

import dataclasses
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from env._agentdojo import BankingEnvironment, BaseUserTask, FunctionCall
from env.banking_env import BENCHMARK_VERSION, SUITE_NAME, get_banking_suite, get_user_task

TASKS_PATH = Path(__file__).resolve().parents[1] / "tasks" / "banking_tasks.yaml"
DEFAULT_TOL = 0.005

# Which part of the state each write tool changes (used by the strict scorer).
WRITE_EFFECTS = {
    "send_money": "transactions.added",
    "schedule_transaction": "scheduled_transactions.added",
    "update_scheduled_transaction": "scheduled_transactions.modified",
    "update_user_info": "user_account",
    "update_password": "user_account",
}


def _num(x: Any) -> float | None:
    if isinstance(x, bool):
        return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def check_predicate(pred: dict[str, Any], value: Any) -> bool:
    """Evaluate one predicate from `allowed_writes.match` against a value."""
    if "equals" in pred:
        a, b = _num(value), _num(pred["equals"])
        return a == b if a is not None and b is not None else value == pred["equals"]
    if "equals_ci" in pred:
        return isinstance(value, str) and value.strip().lower() == str(pred["equals_ci"]).strip().lower()
    if "approx" in pred:
        v = _num(value)
        return v is not None and abs(v - float(pred["approx"])) <= float(pred.get("tol", DEFAULT_TOL))
    raise ValueError(f"unknown predicate {pred}")


@dataclasses.dataclass(frozen=True)
class CriticalFact:
    name: str
    value: Any
    source: dict[str, Any]  # {tool, args, location} or {tool: derived, from, location}
    feeds: tuple[str, ...]


@dataclasses.dataclass(frozen=True)
class WriteSpec:
    tool: str
    match: dict[str, dict[str, Any]]
    max_count: int = 1

    @property
    def effect(self) -> str:
        return WRITE_EFFECTS[self.tool]

    def matches(self, fields: dict[str, Any]) -> bool:
        """All `match` predicates hold on `fields` (call args, or the fields of a resulting record)."""
        return all(k in fields and check_predicate(p, fields[k]) for k, p in self.match.items())

    def matches_field(self, field: str, value: Any) -> bool:
        return field in self.match and check_predicate(self.match[field], value)


@dataclasses.dataclass(frozen=True)
class InjectableField:
    fact: str
    field: str
    original: Any
    location: str


@dataclasses.dataclass(frozen=True)
class TaskConfig:
    task_id: str
    suite: str
    benchmark_version: str
    structure: str
    critical_facts: tuple[CriticalFact, ...]
    allowed_writes: tuple[WriteSpec, ...]
    injectable_fields: tuple[InjectableField, ...]

    @property
    def user_task(self) -> BaseUserTask[BankingEnvironment]:
        return get_user_task(self.task_id)

    @property
    def prompt(self) -> str:
        return self.user_task.PROMPT

    def ground_truth(self, pre_environment: BankingEnvironment) -> list[FunctionCall]:
        return self.user_task.ground_truth(pre_environment)

    def fact(self, name: str) -> CriticalFact:
        return next(f for f in self.critical_facts if f.name == name)


def _parse(task_id: str, raw: dict[str, Any]) -> TaskConfig:
    if raw.get("task_id") != task_id:
        raise ValueError(f"{task_id}: entry must contain `task_id: {task_id}`")
    if raw["suite"] != SUITE_NAME or raw["benchmark_version"] != BENCHMARK_VERSION:
        raise ValueError(f"{task_id}: config is for {raw['suite']}@{raw['benchmark_version']}, "
                         f"env is pinned to {SUITE_NAME}@{BENCHMARK_VERSION}")
    if task_id not in get_banking_suite().user_tasks:
        raise ValueError(f"{task_id}: not a {SUITE_NAME} user task at {BENCHMARK_VERSION}")
    facts = tuple(
        CriticalFact(f["name"], f["value"], f["source"], tuple(f.get("feeds", ()))) for f in raw.get("critical_facts", [])
    )
    by_name = {f.name: f for f in facts}
    writes = []
    for w in raw.get("allowed_writes", []):
        if w["tool"] not in WRITE_EFFECTS:
            raise ValueError(f"{task_id}: allowed_writes tool {w['tool']!r} is not a write tool")
        for pred in w["match"].values():
            check_predicate(pred, 0)  # fail fast on unknown predicate kinds
        writes.append(WriteSpec(w["tool"], dict(w["match"]), int(w.get("max_count", 1))))
    injectable = []
    for i in raw.get("injectable_fields", []):
        if i["fact"] not in by_name:
            raise ValueError(f"{task_id}: injectable field refers to unknown fact {i['fact']!r}")
        fact = by_name[i["fact"]]
        injectable.append(InjectableField(i["fact"], i["field"], fact.value, fact.source["location"]))
    return TaskConfig(
        task_id=task_id,
        suite=raw["suite"],
        benchmark_version=raw["benchmark_version"],
        structure=raw.get("structure", "linear"),
        critical_facts=facts,
        allowed_writes=tuple(writes),
        injectable_fields=tuple(injectable),
    )


@lru_cache(maxsize=4)
def load_task_configs(path: str | Path = TASKS_PATH) -> dict[str, TaskConfig]:
    """All configured tasks, keyed by task id."""
    raw = yaml.safe_load(Path(path).read_text())
    return {task_id: _parse(task_id, cfg) for task_id, cfg in raw.items()}


def get_task_config(task_id: str) -> TaskConfig:
    configs = load_task_configs()
    if task_id not in configs:
        raise KeyError(f"{task_id} is not configured in {TASKS_PATH}; configured: {sorted(configs)}")
    return configs[task_id]
