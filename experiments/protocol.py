"""Shared experiment configuration, provenance and cooperative run budgets."""
from dataclasses import dataclass, asdict
import importlib.metadata
import platform
import subprocess
import time
from pathlib import Path

from .models import detached, fingerprint

PROTOCOL_VERSION = "1.1"
CONDITIONS = {"clean", "fault", "full_restart", "always_verify", "clean_verify", "selective"}


class BudgetExceeded(RuntimeError):
    pass


@dataclass(frozen=True)
class Limits:
    model_calls: int = 100
    tool_calls: int = 100
    wall_seconds: float = 300
    tokens: int | None = None

    def __post_init__(self):
        for value in (self.model_calls, self.tool_calls):
            if type(value) is not int or value <= 0:
                raise ValueError("call limits must be positive integers")
        if not isinstance(self.wall_seconds, (int, float)) or not 0 < self.wall_seconds < float("inf"):
            raise ValueError("wall limit must be positive and finite")
        if self.tokens is not None and (type(self.tokens) is not int or self.tokens <= 0):
            raise ValueError("token limit must be a positive integer or null")


class Budget:
    """One ledger survives resets. No claim of interrupting an in-flight provider."""
    def __init__(self, limits):
        self.limits = limits
        self.started = time.monotonic()
        self.calls = {"model": 0, "tool": 0}
        self.known_tokens = 0
        self.missing_usage = 0

    def check(self):
        if time.monotonic() - self.started >= self.limits.wall_seconds:
            raise BudgetExceeded("run wall-time budget exhausted")
        if self.limits.tokens is not None:
            if self.missing_usage:
                raise BudgetExceeded("cannot enforce token budget after unknown provider usage")
            if self.known_tokens >= self.limits.tokens:
                raise BudgetExceeded("run token budget exhausted")

    def admit(self, kind):
        self.check()
        if self.calls[kind] >= getattr(self.limits, kind + "_calls"):
            raise BudgetExceeded(kind + " call budget exhausted")
        self.calls[kind] += 1

    def finish(self, kind, terminal):
        if kind == "model":
            values = [terminal.get(k) for k in ("input_tokens", "output_tokens")]
            self.missing_usage += any(v is None for v in values)
            self.known_tokens += sum(v for v in values if type(v) is int and v >= 0)

    def report(self):
        return {"limits": asdict(self.limits), "calls": dict(self.calls),
                "known_tokens": self.known_tokens, "calls_missing_usage": self.missing_usage,
                "enforcement": "cooperative_between_calls; token overshoot possible on last call"}


def normalize_recovery_policy(settings):
    """Explicit Phase 2 treatment settings; never infer them for legacy traces."""
    defaults = dict(checkpoint_retries=1, max_checkpoint_attempts=3,
                    max_recoveries=1, fallback_policy="full_restart")
    if not isinstance(settings, dict) or set(settings) - defaults.keys():
        raise ValueError("unsupported recovery_policy fields")
    result = {**defaults, **settings}
    for key in ("checkpoint_retries", "max_checkpoint_attempts", "max_recoveries"):
        if type(result[key]) is not int or result[key] < (0 if key == "checkpoint_retries" else 1):
            raise ValueError("invalid recovery_policy limit: " + key)
    if result["fallback_policy"] not in {"full_restart", "error"}:
        raise ValueError("invalid recovery_policy fallback")
    return result


def validate_config(config):
    config = detached(config)
    required = {"task_id", "condition", "scenario_id", "comparison_group_id", "repeat_index",
                "workflow", "limits", "synthetic"}
    if not required <= config.keys():
        raise ValueError("missing experiment fields: " + str(sorted(required - config.keys())))
    if config["condition"] not in CONDITIONS:
        raise ValueError("unsupported condition")
    if type(config["synthetic"]) is not bool or type(config["repeat_index"]) is not int or config["repeat_index"] < 0:
        raise ValueError("invalid synthetic/repeat fields")
    if not isinstance(config["workflow"], dict):
        raise ValueError("workflow must be a versioned configuration object")
    Limits(**config["limits"])
    if config.get("notification_boundary", "after_initial_workflow") != "after_initial_workflow":
        raise ValueError("only after_initial_workflow notification is implemented")
    if config.get("fallback_policy", "full_restart") not in {"full_restart", "error"}:
        raise ValueError("unsupported fallback policy")
    if config["condition"] not in {"clean", "clean_verify"}:
        fault = config.get("fault", {})
        if fault.get("family") not in {"accidental", "adversarial"} or fault.get("site") not in {"research_to_plan", "plan_to_execute"}:
            raise ValueError("fault runs require a supported family and logical site")
        if not config["synthetic"] and not fault.get("payload_ref"):
            raise ValueError("real fault runs require a versioned C payload")
    if not config["synthetic"]:
        if any(not config["workflow"].get(k) for k in ("model", "runtime", "prompt_hash")):
            raise ValueError("real runs require model, runtime and prompt_hash")
    if "recovery_policy" in config:
        config["recovery_policy"] = normalize_recovery_policy(config["recovery_policy"])
    return config


def provenance(config=None, root=None):
    def git(*args):
        try:
            return subprocess.check_output(["git", *args], cwd=root or Path(__file__).resolve().parents[1],
                stderr=subprocess.DEVNULL, text=True, timeout=3).strip()
        except (OSError, subprocess.SubprocessError):
            return None
    def version(name):
        try:
            return importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            return None
    status = git("status", "--porcelain")
    return {"protocol_version": PROTOCOL_VERSION, "git_commit": git("rev-parse", "HEAD"),
        "working_tree_dirty": status != "" if status is not None else None,
        "device": {"platform": platform.platform(), "machine": platform.machine(),
                   "processor": platform.processor() or None, "ram_bytes": None},
        "versions": {"python": platform.python_version(), "agentdojo": version("agentdojo")},
        "experiment": config, "config_hash": fingerprint(config) if config else None}


def phase1_matrix(repeats, workflow, limits, *, synthetic=True):
    """28 cells/repetition; fault payloads supplied by C, never invented here."""
    if type(repeats) is not int or repeats < 1:
        raise ValueError("repeats must be positive")
    rows = []
    for repeat in range(repeats):
        for task in ("user_task_0", "user_task_2"):
            group = f"{task}_r{repeat}"
            base = dict(task_id=task, comparison_group_id=group, repeat_index=repeat,
                        workflow=workflow, limits=limits, synthetic=synthetic)
            for condition in ("clean", "clean_verify"):
                rows.append({**base, "scenario_id": task + "_clean", "condition": condition})
            for family in ("accidental", "adversarial"):
                for site in ("research_to_plan", "plan_to_execute"):
                    scenario = f"{task}_{family}_{site}"
                    for condition in ("fault", "full_restart", "always_verify"):
                        rows.append({**base, "scenario_id": scenario, "condition": condition,
                                     "fault": {"family": family, "site": site, "payload_ref": None}})
    return rows
