"""Resettable, permission-checked, audited wrapper around the AgentDojo Banking suite.

Typical use:

    env = BankingEnv(audit_path="runs/x/audit.jsonl").load("user_task_0")
    r = env.call_tool("researcher", "read_file", {"file_path": "bill-december-2023.txt"}, run_id="r1")
    r = env.call_tool("executor", "send_money", {...}, run_id="r1")
    env.reset()                      # simulated state back to initial; audit log keeps everything

AgentDojo is wrapped, never modified.
"""

from __future__ import annotations

import copy
import dataclasses
import importlib.metadata
import json
import time
import uuid
import warnings
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ValidationError

from env._agentdojo import BankingEnvironment, BaseUserTask, FunctionsRuntime, TaskSuite, get_suite, tool_result_to_str
from env.state import State, StateDiff, diff_states, state_dict

AGENTDOJO_VERSION = "0.1.35"
BENCHMARK_VERSION = "v1.2.2"
SUITE_NAME = "banking"
ROLES_PATH = Path(__file__).with_name("roles.yaml")
ROLES = ("researcher", "planner", "executor", "verifier")

if (_installed := importlib.metadata.version("agentdojo")) != AGENTDOJO_VERSION:
    warnings.warn(f"agentdojo {_installed} installed, but the project is pinned to {AGENTDOJO_VERSION}", stacklevel=1)


@lru_cache(maxsize=1)
def get_banking_suite() -> TaskSuite[BankingEnvironment]:
    """The Banking suite at the pinned benchmark version."""
    return get_suite(BENCHMARK_VERSION, SUITE_NAME)


def default_initial_state(task_id: str) -> State:
    """State dict of the default (no-attack) environment for `task_id`, as `BankingEnv.load` builds it."""
    return state_dict(get_user_task(task_id).init_environment(get_banking_suite().load_and_inject_default_environment({})))


def get_user_task(task_id: str) -> BaseUserTask[BankingEnvironment]:
    """AgentDojo user task (prompt, ground_truth, utility) at the pinned benchmark version."""
    return get_banking_suite().get_user_task_by_id(task_id)


@dataclasses.dataclass(frozen=True)
class RoleConfig:
    read_tools: tuple[str, ...]
    write_tools: tuple[str, ...]
    roles: dict[str, tuple[str, ...]]

    @classmethod
    def load(cls, path: str | Path = ROLES_PATH) -> RoleConfig:
        raw = yaml.safe_load(Path(path).read_text())
        cfg = cls(
            read_tools=tuple(raw["read_tools"]),
            write_tools=tuple(raw["write_tools"]),
            roles={role: tuple(tools or ()) for role, tools in raw["roles"].items()},
        )
        suite_tools = {f.name for f in get_banking_suite().tools}
        classified = set(cfg.read_tools) | set(cfg.write_tools)
        if set(cfg.read_tools) & set(cfg.write_tools):
            raise ValueError(f"{path}: tools listed as both read and write")
        if classified != suite_tools:
            raise ValueError(f"{path}: read/write classification must cover exactly the suite tools; "
                             f"missing={suite_tools - classified}, unknown={classified - suite_tools}")
        for role, tools in cfg.roles.items():
            if unknown := set(tools) - suite_tools:
                raise ValueError(f"{path}: role {role!r} lists unknown tools {unknown}")
        return cfg

    def kind(self, tool: str) -> str:
        """'read', 'write' or 'unknown'."""
        return "read" if tool in self.read_tools else "write" if tool in self.write_tools else "unknown"

    def allows(self, role: str, tool: str) -> bool:
        return tool in self.roles.get(role, ())


@dataclasses.dataclass
class ToolResult:
    """Outcome of one `call_tool`. Also the schema of one audit-log line (see env/README.md)."""

    call_id: str
    run_id: str | None
    step_id: str | None
    task_id: str | None
    env_version: int
    role: str
    tool: str
    kind: str  # "read" | "write" | "unknown"
    args: dict[str, Any]
    allowed: bool  # role permission check passed
    executed: bool  # the tool function was actually invoked
    result: Any  # JSON-serializable tool return value (None if not executed)
    result_text: str | None  # AgentDojo's string rendering of `result`, as an LLM would see it
    error: str | None  # "PermissionDenied: ...", "ValidationError: ...", or the tool's exception
    state_diff: StateDiff | None  # see env/state.py; {} for reads; None if not executed
    t_start: float  # unix time, seconds
    t_end: float

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


def _jsonable(x: Any) -> Any:
    if isinstance(x, BaseModel):
        return x.model_dump(mode="json")
    if isinstance(x, dict):
        return {str(k): _jsonable(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_jsonable(v) for v in x]
    return x


def _check_plain_args(args: Any) -> str | None:
    """Only plain JSON values are accepted. AgentDojo's runtime would execute nested
    FunctionCall args, which would bypass role permissions."""
    if not isinstance(args, dict):
        return f"args must be a dict, got {type(args).__name__}"

    def ok(v: Any) -> bool:
        if v is None or isinstance(v, (str, int, float, bool)):
            return True
        if isinstance(v, list):
            return all(ok(i) for i in v)
        if isinstance(v, dict):
            return all(isinstance(k, str) and ok(i) for k, i in v.items())
        return False

    bad = [k for k, v in args.items() if not isinstance(k, str) or not ok(v)]
    return f"non-JSON values for args {bad}" if bad else None


class BankingEnv:
    """AgentDojo Banking environment with roles, snapshots and an append-only audit log."""

    def __init__(self, audit_path: str | Path | None = None, roles_path: str | Path = ROLES_PATH):
        self.suite = get_banking_suite()
        self.roles = RoleConfig.load(roles_path)
        self.runtime = FunctionsRuntime(self.suite.tools)
        self.task_id: str | None = None
        self.user_task: BaseUserTask[BankingEnvironment] | None = None
        self._env: BankingEnvironment | None = None
        self._initial: BankingEnvironment | None = None
        self._env_version = 0
        self._audit: list[dict[str, Any]] = []
        self.audit_path: Path | None = None
        if audit_path is not None:
            self.set_audit_path(audit_path)

    # ---- lifecycle -------------------------------------------------------------------------

    def load(self, task_id: str) -> BankingEnv:
        """Build the default (no-attack) environment for `task_id` and remember it as the initial state."""
        self.user_task = get_user_task(task_id)
        self.task_id = task_id
        environment = self.suite.load_and_inject_default_environment({})
        self._env = self.user_task.init_environment(environment)
        self._initial = self._env.model_copy(deep=True)
        self._env_version += 1
        return self

    def reset(self) -> None:
        """Restore the initial state. The audit log is NOT cleared."""
        self._env = self._require_initial().model_copy(deep=True)
        self._env_version += 1

    def snapshot(self) -> BankingEnvironment:
        """Deep copy of the current simulated state."""
        return self.env.model_copy(deep=True)

    def restore(self, snapshot: BankingEnvironment) -> None:
        """Replace the current state with a deep copy of `snapshot`. The audit log is NOT touched."""
        if not isinstance(snapshot, BankingEnvironment):
            raise TypeError(f"expected BankingEnvironment, got {type(snapshot).__name__}")
        self._require_initial()
        self._env = snapshot.model_copy(deep=True)
        self._env_version += 1

    @property
    def env(self) -> BankingEnvironment:
        """The live AgentDojo environment object (mutated by tool calls)."""
        if self._env is None:
            raise RuntimeError("BankingEnv.load(task_id) must be called first")
        return self._env

    @property
    def initial_state(self) -> BankingEnvironment:
        return self._require_initial().model_copy(deep=True)

    @property
    def env_version(self) -> int:
        """Epoch counter, +1 on every load/reset/restore. Tells apart calls made on different timelines."""
        return self._env_version

    @property
    def prompt(self) -> str:
        if self.user_task is None:
            raise RuntimeError("BankingEnv.load(task_id) must be called first")
        return self.user_task.PROMPT

    def state_dict(self) -> State:
        """JSON-serializable view of the current state (see env/state.py)."""
        return state_dict(self.env)

    def _require_initial(self) -> BankingEnvironment:
        if self._initial is None:
            raise RuntimeError("BankingEnv.load(task_id) must be called first")
        return self._initial

    # ---- tools -----------------------------------------------------------------------------

    def allowed_tools(self, role: str) -> tuple[str, ...]:
        return self.roles.roles.get(role, ())

    def tool_schemas(self, role: str) -> list[dict[str, Any]]:
        """[{name, description, parameters (JSON schema)}] for the tools `role` may call."""
        if role not in self.roles.roles:
            raise ValueError(f"unknown role {role!r}; known: {sorted(self.roles.roles)}")
        functions = self.runtime.functions
        return [
            {
                "name": name,
                "description": functions[name].description,
                "parameters": functions[name].parameters.model_json_schema(),
            }
            for name in self.allowed_tools(role)
        ]

    def call_tool(
        self,
        role: str,
        tool_name: str,
        args: dict[str, Any] | None = None,
        run_id: str | None = None,
        step_id: str | None = None,
    ) -> ToolResult:
        """Execute one tool call as `role`. Denied/invalid calls are not executed but are always logged."""
        env = self.env
        t_start = time.time()
        args = copy.deepcopy(args) if args is not None else {}
        allowed = self.roles.allows(role, tool_name)
        executed, result, result_text, error, diff = False, None, None, None, None

        if not allowed:
            error = f"PermissionDenied: role {role!r} may not call {tool_name!r}"
        elif problem := _check_plain_args(args):
            error = f"InvalidArguments: {problem}"
        else:
            function = self.runtime.functions[tool_name]
            try:
                function.parameters.model_validate(args)
            except ValidationError as e:
                error = f"ValidationError: {e}"
            else:
                before = state_dict(env)
                raw, error = self.runtime.run_function(env, tool_name, args)
                executed = True
                diff = diff_states(before, env)
                if error is None:
                    result = _jsonable(raw)
                    try:
                        result_text = tool_result_to_str(raw)
                    except TypeError:
                        result_text = str(raw)

        record = ToolResult(
            call_id=uuid.uuid4().hex,
            run_id=run_id,
            step_id=step_id,
            task_id=self.task_id,
            env_version=self._env_version,
            role=role,
            tool=tool_name,
            kind=self.roles.kind(tool_name),
            args=args if isinstance(args, dict) else {"_invalid": repr(args)},
            allowed=allowed,
            executed=executed,
            result=result,
            result_text=result_text,
            error=error,
            state_diff=diff,
            t_start=t_start,
            t_end=time.time(),
        )
        self._append_audit(record)
        return record

    # ---- audit log -------------------------------------------------------------------------

    def set_audit_path(self, path: str | Path) -> None:
        """Future audit lines are appended to `path` (JSONL). Existing in-memory entries are not rewritten."""
        self.audit_path = Path(path)
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)

    def audit_log(self, run_id: str | None = None) -> list[dict[str, Any]]:
        """Copy of all audit entries ever recorded by this env (optionally only one run's)."""
        entries = self._audit if run_id is None else [e for e in self._audit if e["run_id"] == run_id]
        return copy.deepcopy(entries)

    def _append_audit(self, record: ToolResult) -> None:
        entry = record.to_dict()
        try:
            line = json.dumps(entry)
        except TypeError:  # should not happen; keep the log writable no matter what
            line = json.dumps(entry, default=repr)
            entry = json.loads(line)
        self._audit.append(entry)
        if self.audit_path is not None:
            with self.audit_path.open("a") as f:
                f.write(line + "\n")
