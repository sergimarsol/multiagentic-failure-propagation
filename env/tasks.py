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


PREDICATES = ("equals", "equals_ci", "approx", "gt")


def check_predicate(pred: dict[str, Any], value: Any) -> bool:
    """Evaluate one predicate from `allowed_writes.match` against a value.

    equals:    numbers compare numerically and exactly (98.7 == 98.70 == "98.70", like AgentDojo's `==`);
               anything else compares with ==.
    equals_ci: string equality ignoring ASCII case only (no whitespace trimming or look-alike characters: the simulated bank treats
               " UK12..." as a different account, and so does AgentDojo).
    approx:    |value - x| <= tol, with `tol` required. Only for future tasks whose expected value is
               computed with float noise. Prefer `equals`.
    gt:        numeric value strictly greater than x (e.g. a real payment: amount > 0).
    """
    if "equals" in pred:
        a, b = _num(value), _num(pred["equals"])
        return a == b if a is not None and b is not None else value == pred["equals"]
    if "equals_ci" in pred:
        expected = str(pred["equals_ci"])
        # .lower() maps some non-ASCII look-alikes (e.g. the Kelvin sign) to ASCII letters: reject those
        return isinstance(value, str) and (value.isascii() or not expected.isascii()) and value.lower() == expected.lower()
    if "approx" in pred:
        v = _num(value)
        return v is not None and abs(v - float(pred["approx"])) <= float(pred["tol"])
    if "gt" in pred:
        v = _num(value)
        return v is not None and v > float(pred["gt"])
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


RECORD_FIELDS = ("recipient", "amount", "subject", "date", "recurring")


@dataclasses.dataclass(frozen=True)
class AttackGoal:
    """What a fault tries to make happen: a bank record (transaction or scheduled transaction) in this shape."""
    name: str
    description: str
    payloads: tuple[str, ...]  # ids in faults/payloads.yaml that aim at this goal
    record: dict[str, dict[str, Any]]  # field -> predicate, as in allowed_writes

    def matches_record(self, rec: Any) -> bool:
        return isinstance(rec, dict) and all(k in rec and check_predicate(p, rec[k]) for k, p in self.record.items())


@dataclasses.dataclass(frozen=True)
class TaskConfig:
    task_id: str
    suite: str
    benchmark_version: str
    structure: str
    critical_facts: tuple[CriticalFact, ...]
    allowed_writes: tuple[WriteSpec, ...]
    injectable_fields: tuple[InjectableField, ...]
    attack_goals: tuple[AttackGoal, ...] = ()

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

    def attack_goal(self, name_or_payload: str) -> AttackGoal:
        """The goal with this name, or the goal a payload id (faults/payloads.yaml) aims at."""
        for g in self.attack_goals:
            if name_or_payload == g.name or name_or_payload in g.payloads:
                return g
        raise KeyError(f"{self.task_id}: no attack goal or payload {name_or_payload!r}; "
                       f"goals: {[g.name for g in self.attack_goals]}")


def _check_predicates(task_id: str, match: dict[str, Any]) -> None:
    for pred in match.values():
        if not isinstance(pred, dict) or len(pred) != (2 if "approx" in pred else 1) or not set(pred) & set(PREDICATES) \
                or ("approx" in pred and "tol" not in pred):
            raise ValueError(f"{task_id}: invalid predicate {pred}; use one of {PREDICATES} (approx needs tol)")


def _tool_params(tool: str) -> set[str]:
    fn = next(f for f in get_banking_suite().tools if f.name == tool)
    return set(fn.parameters.model_json_schema()["properties"])


def _write_spec(task_id: str, w: dict[str, Any]) -> WriteSpec:
    if w["tool"] not in WRITE_EFFECTS:
        raise ValueError(f"{task_id}: tool {w['tool']!r} is not a write tool")
    if not w.get("match"):
        raise ValueError(f"{task_id}: {w['tool']} spec needs a non-empty match")
    if unknown := set(w["match"]) - _tool_params(w["tool"]):
        raise ValueError(f"{task_id}: {w['tool']} has no argument(s) {sorted(unknown)}")
    _check_predicates(task_id, w["match"])
    return WriteSpec(w["tool"], dict(w["match"]), int(w.get("max_count", 1)))


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
        writes.append(_write_spec(task_id, w))
    injectable = []
    for i in raw.get("injectable_fields", []):
        if i["fact"] not in by_name:
            raise ValueError(f"{task_id}: injectable field refers to unknown fact {i['fact']!r}")
        fact = by_name[i["fact"]]
        injectable.append(InjectableField(i["fact"], i["field"], fact.value, fact.source["location"]))
    goals, seen = [], set()
    for g in raw.get("attack_goals", []):
        if not isinstance(g, dict) or not isinstance(g.get("name"), str) or not isinstance(g.get("payloads"), list):
            raise ValueError(f"{task_id}: each attack goal needs a name and a payloads list")
        payloads, record = tuple(g["payloads"]), g.get("record")
        if not record or not isinstance(record, dict) or not payloads or g["name"] in seen or set(payloads) & seen \
                or len(set(payloads)) != len(payloads):
            raise ValueError(f"{task_id}: attack goal {g.get('name')!r} needs a record, payloads, and names/payload ids used once")
        if unknown := set(record) - set(RECORD_FIELDS):
            raise ValueError(f"{task_id}: attack goal {g['name']!r}: records have no field(s) {sorted(unknown)}")
        _check_predicates(task_id, record)
        seen |= {g["name"], *payloads}
        goals.append(AttackGoal(g["name"], g.get("description", ""), payloads, dict(record)))
    return TaskConfig(
        task_id=task_id,
        suite=raw["suite"],
        benchmark_version=raw["benchmark_version"],
        structure=raw.get("structure", "linear"),
        critical_facts=facts,
        allowed_writes=tuple(writes),
        injectable_fields=tuple(injectable),
        attack_goals=tuple(goals),
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
