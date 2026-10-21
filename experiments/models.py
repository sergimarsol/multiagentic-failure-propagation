"""Versioned JSON contracts; business judgments belong to external evaluators."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import hashlib
import json
import re
from typing import Any
from uuid import uuid4

SCHEMA_VERSION = "1.0"


def identifier(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise ValueError(f"invalid identifier: {value!r}")
    return value


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def canonical(value: Any) -> str:
    # Strict JSON: no silent repr(), NaN, infinity, tuple or non-string keys.
    def check(v):
        if isinstance(v, dict):
            if not all(isinstance(k, str) for k in v):
                raise ValueError("JSON object keys must be strings")
            for x in v.values():
                check(x)
        elif isinstance(v, list):
            for x in v:
                check(x)
        elif v is not None and not isinstance(v, (str, bool, int, float)):
            raise ValueError(f"not a JSON value: {type(v).__name__}")
    check(value)
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False)


def detached(value: Any) -> Any:
    return json.loads(canonical(value))


def fingerprint(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


@dataclass(frozen=True, kw_only=True)
class Record:
    run_id: str
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self):
        identifier(self.run_id)
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError(f"unsupported schema: {self.schema_version}")

    def to_dict(self) -> dict:
        # Persistence and reads return detached values, never shared references.
        return detached(asdict(self))


@dataclass(frozen=True, kw_only=True)
class Event(Record):
    event_id: str
    event_type: str
    attempt_id: str
    event_seq: int = 0
    timestamp_utc: str = ""
    node_id: str | None = None
    node_execution_id: str | None = None
    step_id: str | None = None
    recovery_id: str | None = None
    duration_ms: float | None = None
    data: dict = field(default_factory=dict)

    def __post_init__(self):
        super().__post_init__()
        for value in (self.event_id, self.attempt_id, self.event_type):
            identifier(value)
        if not isinstance(self.data, dict):
            raise ValueError("event data must be an object")


@dataclass(frozen=True, kw_only=True)
class Artifact(Record):
    artifact_id: str
    attempt_id: str
    producer_execution_id: str
    artifact_type: str
    content: Any
    input_artifact_ids: list[str] = field(default_factory=list)
    derived_from_artifact_id: str | None = None
    supersedes_artifact_id: str | None = None

    def __post_init__(self):
        super().__post_init__()
        for value in (self.artifact_id, self.attempt_id, self.producer_execution_id):
            identifier(value)
        if not isinstance(self.input_artifact_ids, list):
            raise ValueError("input_artifact_ids must be a list")
        for ref in self.input_artifact_ids:
            identifier(ref)


@dataclass(frozen=True, kw_only=True)
class Evaluation(Record):
    evaluation_id: str
    attempt_id: str
    scope: str  # attempt, history, or stage
    evaluator: str
    task_success: bool | None = None
    unauthorized_attempt_ids: list[str] | None = None
    unauthorized_execution_ids: list[str] | None = None
    propagation: list[dict] = field(default_factory=list)
    # Execution ID -> valid / invalid / unknown. Never inferred from dependencies.
    work_validity: dict[str, str] = field(default_factory=dict)
    evidence_ids: list[str] = field(default_factory=list)
    details: dict = field(default_factory=dict)

    def __post_init__(self):
        super().__post_init__()
        identifier(self.evaluation_id)
        identifier(self.attempt_id)
        if self.scope not in {"attempt", "history", "stage"}:
            raise ValueError("unknown evaluation scope")
        if self.task_success is not None and type(self.task_success) is not bool:
            raise ValueError("task_success must be bool or null")
        for ids in (self.unauthorized_attempt_ids, self.unauthorized_execution_ids):
            if ids is not None and (not isinstance(ids, list) or not all(isinstance(x, str) for x in ids)):
                raise ValueError("action IDs must be a list or null")
        labels = {"not_exposed", "exposed_not_adopted", "propagated_to_plan",
                  "attempted_incorrect_action", "executed_incorrect_action", "uncertain"}
        for p in self.propagation:
            if not isinstance(p, dict) or p.get("label") not in labels or not isinstance(p.get("evidence_ids"), list):
                raise ValueError("propagation requires a supported label and evidence_ids")


@dataclass(frozen=True, kw_only=True)
class RecoveryPoint(Record):
    checkpoint_id: str
    attempt_id: str
    boundary: str
    injection_phase: str  # before_injection, after_injection, or no_injection
    environment_ref: str
    execution_ref: str
    manifest_fingerprint: str
    component_hashes: dict[str, str]
    event_cursor: int
    status: str = "ready"
