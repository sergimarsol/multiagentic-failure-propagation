"""Single-writer append-only trace storage and timed invocation scopes."""

from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import time

from .models import (Artifact, Evaluation, Event, SCHEMA_VERSION, canonical,
                     detached, identifier, new_id)


class TraceError(ValueError):
    pass


def read_jsonl(path: Path, *, tolerate_tail=False) -> tuple[list[dict], list[str]]:
    """Only a torn last line is optionally tolerated; malformed middle lines fail."""
    if not path.exists():
        return [], []
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    rows, problems = [], []
    for i, line in enumerate(lines):
        try:
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError("record must be an object")
            canonical(row)
        except (ValueError, TypeError) as exc:
            if tolerate_tail and i == len(lines) - 1 and not line.endswith("\n"):
                problems.append(f"{path.name}:{i + 1}: truncated final line")
                break
            raise TraceError(f"{path}:{i + 1}: malformed JSON record") from exc
        if not line.endswith("\n"):
            if not tolerate_tail:
                raise TraceError(f"{path}: unterminated final record; refusing append")
            problems.append(f"{path.name}:{i + 1}: unterminated final record")
        if row.get("schema_version") != SCHEMA_VERSION:
            raise TraceError(f"{path}:{i + 1}: incompatible schema")
        rows.append(row)
    return rows, problems


def atomic_json(path: Path, data: dict):
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("x", encoding="utf-8") as f:
        f.write(canonical(data) + "\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


class Recorder:
    """One process / one writer per run directory. Reopen never overwrites a run."""

    def __init__(self, directory, manifest: dict):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.manifest = detached({**manifest, "schema_version": SCHEMA_VERSION})
        self.run_id = identifier(self.manifest["run_id"])
        path = self.directory / "manifest.json"
        if path.exists():
            if json.loads(path.read_text()) != self.manifest:
                raise TraceError("manifest does not match existing run")
        else:
            if any(self.directory.iterdir()):
                raise TraceError("refusing to adopt a nonempty directory without a manifest")
            atomic_json(path, self.manifest)
        self._rows = {}
        self._ids = set()
        self._active = {}
        self._started_steps = set()
        self._broken = False
        self.write_ms = 0.0
        for stream, key, cls in (("events", "event_id", Event),
                                 ("artifacts", "artifact_id", Artifact),
                                 ("evaluations", "evaluation_id", Evaluation)):
            rows, _ = read_jsonl(self.directory / f"{stream}.jsonl")
            for row in rows:
                try:
                    cls(**row)
                    if row["run_id"] != self.run_id or row[key] in self._ids:
                        raise ValueError("foreign run or duplicate ID")
                    identifier(row[key])
                except (TypeError, ValueError, KeyError) as exc:
                    raise TraceError(f"invalid {stream} record") from exc
                self._ids.add(row[key])
            self._rows[stream] = rows
        for seq, event in enumerate(self._rows["events"], 1):
            if event["event_seq"] != seq:
                raise TraceError("non-contiguous event sequence")
            self._transition(event, apply=True)

    @property
    def event_cursor(self):
        return len(self._rows["events"])

    @property
    def active_calls(self):
        return detached(self._active)

    def records(self, stream):
        return detached(self._rows[stream])

    def _append(self, stream, row, key):
        if self._broken:
            raise TraceError("writer failed; inspect the trace before reopening")
        identifier(row[key])
        if row[key] in self._ids or row["run_id"] != self.run_id:
            raise TraceError("duplicate ID or foreign run")
        value = detached(row)
        started = time.perf_counter()
        try:
            with (self.directory / f"{stream}.jsonl").open("a", encoding="utf-8") as f:
                f.write(canonical(value) + "\n")
                f.flush()
        except OSError:
            self._broken = True
            raise
        self.write_ms += (time.perf_counter() - started) * 1000
        self._ids.add(row[key])
        self._rows[stream].append(value)

    def _transition(self, e, *, apply=False):
        typ, step = e["event_type"], e.get("step_id")
        if typ in {"model_started", "tool_started"}:
            if not step or step in self._started_steps:
                raise TraceError("missing or reused step ID")
            if apply:
                self._started_steps.add(step)
                self._active[step] = {"kind": typ.split("_")[0], "attempt_id": e["attempt_id"]}
        elif typ in {"model_finished", "tool_finished"}:
            current = self._active.get(step)
            if current != {"kind": typ.split("_")[0], "attempt_id": e["attempt_id"]}:
                raise TraceError("call end without matching active start")
            for key in ("input_tokens", "output_tokens"):
                n = e["data"].get(key)
                if n is not None and (type(n) is not int or n < 0):
                    raise TraceError(f"invalid {key}")
            if apply:
                del self._active[step]

    def emit(self, event: Event):
        row = replace(event, event_seq=self.event_cursor + 1,
                      timestamp_utc=datetime.now(timezone.utc).isoformat()).to_dict()
        identifier(row["attempt_id"])
        if row["duration_ms"] is not None and row["duration_ms"] < 0:
            raise TraceError("negative duration")
        self._transition(row)
        self._append("events", row, "event_id")
        self._transition(row, apply=True)
        return detached(row)

    def log(self, event_type, attempt_id, *, data=None, **kwargs):
        return self.emit(Event(run_id=self.run_id, attempt_id=attempt_id,
                               event_id=new_id("event"), event_type=event_type,
                               data=data or {}, **kwargs))

    def artifact(self, artifact_id):
        for row in self._rows["artifacts"]:
            if row["artifact_id"] == artifact_id:
                return detached(row)
        raise TraceError(f"unknown artifact {artifact_id}")

    def save_artifact(self, artifact: Artifact):
        row = artifact.to_dict()
        identifier(row["attempt_id"])
        refs = row["input_artifact_ids"] + [row[k] for k in
               ("derived_from_artifact_id", "supersedes_artifact_id") if row[k]]
        for ref in refs:
            self.artifact(ref)
        self._append("artifacts", row, "artifact_id")
        return row["artifact_id"]

    def save_evaluation(self, evaluation: Evaluation):
        row = evaluation.to_dict()
        if row["scope"] not in {"attempt", "history", "stage"}:
            raise TraceError("unknown evaluation scope")
        if row["task_success"] is not None and type(row["task_success"]) is not bool:
            raise TraceError("task_success must be bool or null")
        if any(v not in {"valid", "invalid", "unknown"} for v in row["work_validity"].values()):
            raise TraceError("invalid work validity judgment")
        self._append("evaluations", row, "evaluation_id")

    @contextmanager
    def call(self, kind, attempt_id, *, node_id=None, node_execution_id=None,
             step_id=None, **payload):
        """Yield a terminal payload; fill usage/results even for verification/retries."""
        if kind not in {"model", "tool"}:
            raise ValueError("call kind must be model or tool")
        fields = dict(step_id=step_id or new_id("step"), node_id=node_id,
                      node_execution_id=node_execution_id)
        self.log(f"{kind}_started", attempt_id, data=payload, **fields)
        result = {"status": "success", "input_tokens": None, "output_tokens": None}
        start = time.perf_counter()
        try:
            yield result
        except BaseException as exc:
            result.update(status="error", error=f"{type(exc).__name__}: {exc}")
            raise
        finally:
            self.log(f"{kind}_finished", attempt_id, data=result,
                     duration_ms=(time.perf_counter() - start) * 1000, **fields)


class DependencyStore:
    """Records only; no traversal, invalidation or reuse decisions in Phase 1."""

    def __init__(self, recorder: Recorder):
        self.recorder = recorder

    def record(self, attempt_id, node_id, node_execution_id, *, inputs,
               state_reads=None, state_writes=None, controls=()):
        for ref in [*inputs, *controls]:
            self.recorder.artifact(ref)
        return self.recorder.log("dependencies_recorded", attempt_id,
            node_id=node_id, node_execution_id=node_execution_id,
            data={"input_artifact_ids": list(inputs), "control_artifact_ids": list(controls),
                  "state_reads": state_reads, "state_writes": state_writes})
