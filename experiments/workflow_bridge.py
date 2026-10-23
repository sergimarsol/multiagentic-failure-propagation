"""D-owned configuration binding for B; no agent or business scoring logic."""
from dataclasses import asdict
import importlib
import json
from pathlib import Path

from .banking import cross_attempt_adapter
from .batch import run_batch, aggregate
from .metrics import summarize_run
from .models import detached, fingerprint
from .phase1 import run_phase1
from .protocol import validate_config, Limits
from .recorder import atomic_json
from workflow import BankingWorkflowSession, ModelConfig, WorkflowConfig, OpenAICompatibleClient
from workflow.contracts import InjectionSpec, PLAN_SCHEMA
from workflow.integration import BankingEnvironmentAdapter, workflow_report
from workflow.prompts import PROMPTS, RESEARCH_FORMATS

SITES = {"research_to_plan": "research", "plan_to_execute": "plan"}
ROOT = Path(__file__).resolve().parents[1]


def prompt_hash(task_id):
    return fingerprint({"prompts": PROMPTS, "research_format": RESEARCH_FORMATS.get(task_id, ""),
                        "plan_schema": PLAN_SCHEMA})


def source_hash():
    # Control variables include A/D/B code, permissions, tasks and the dependency lock.
    paths = sorted({p for directory in ("experiments", "workflow", "env", "eval", "tasks")
                    for p in (ROOT / directory).rglob("*") if p.suffix in {".py", ".yaml"}})
    return fingerprint({str(p.relative_to(ROOT)): p.read_text() for p in paths + [ROOT / "uv.lock"]})


def prepare_config(config):
    """Resolve defaults and bind recorded controls BEFORE any model request."""
    c = detached(config)
    if c.get("condition") not in {"clean", "clean_verify", "fault", "full_restart", "always_verify"}:
        raise ValueError("B bridge supports Phase 1 conditions only")
    if c.get("task_id") not in {"user_task_0", "user_task_2"}:
        raise ValueError("B bridge supports selected Banking tasks 0 and 2")
    allowed = {"task_id", "condition", "scenario_id", "comparison_group_id", "repeat_index",
               "workflow", "limits", "synthetic", "fault", "notification_boundary", "initial_state_hash"}
    if set(c) - allowed:
        raise ValueError("unsupported B bridge controls: " + str(sorted(set(c) - allowed)))
    w = c.get("workflow", {})
    if set(w) - {"model", "runtime", "session", "prompt_hash", "source_hash"}:
        raise ValueError("unsupported workflow settings")
    if not isinstance(w.get("model"), dict) or not w["model"].get("model") or not w["model"].get("base_url"):
        raise ValueError("explicit model identifier and base_url required")
    model = ModelConfig(**w["model"])
    session = WorkflowConfig(**w.get("session", {}))
    if not isinstance(w.get("runtime"), str) or not w["runtime"].strip():
        raise ValueError("runtime description required (operator supplied, not server verified)")
    resolved = {**w, "model": model.public_dict(), "session": asdict(session),
                "prompt_hash": prompt_hash(c["task_id"]), "source_hash": source_hash()}
    for key in ("prompt_hash", "source_hash"):
        if key in w and w[key] != resolved[key]:
            raise ValueError(key + " differs from current implementation")
    c["workflow"] = resolved
    c["limits"] = asdict(Limits(**c["limits"]))
    adapter = BankingEnvironmentAdapter(c["task_id"])
    initial = fingerprint(adapter.read_state(adapter.create_initial_environment(c["task_id"])))
    if "initial_state_hash" in c and c["initial_state_hash"] != initial:
        raise ValueError("initial_state_hash differs from actual Banking state")
    c["initial_state_hash"] = initial
    if c["condition"] in {"clean", "clean_verify"}:
        if c.get("fault"):
            raise ValueError("clean conditions must not contain fault settings")
    else:
        fault = c.get("fault", {})
        if set(fault) - {"family", "site", "payload_ref", "injector_factory", "fixture", "payload_hash"}:
            raise ValueError("unsupported fault controls")
        if not fault.get("injector_factory") or not fault.get("payload_ref"):
            raise ValueError("fault requires injector_factory and versioned payload_ref")
        if type(fault.get("fixture")) is not bool:
            raise ValueError("declare whether the payload is a fixture")
        if fault["injector_factory"].startswith("workflow.") and not fault["fixture"]:
            raise ValueError("B reference injections must be labelled fixtures")
        payload = (ROOT / fault["payload_ref"]).resolve()
        if not payload.is_file():
            raise ValueError("payload_ref must name a local versioned payload file")
        import hashlib
        digest = hashlib.sha256(payload.read_bytes()).hexdigest()
        if fault.get("payload_hash", digest) != digest:
            raise ValueError("payload file changed")
        fault["payload_hash"] = digest
        if fault["injector_factory"] == "workflow.threat_fixtures:make_injector":
            from workflow.threat_fixtures import definitions
            if payload != (ROOT / "workflow/fixture_payloads.yaml").resolve():
                raise ValueError("B fixture payload_ref must identify its actual payload file")
            case = definitions()["scenarios"][c["scenario_id"]]
            if (case["task_id"], case["site"], case["family"]) != (
                    c["task_id"], SITES.get(fault["site"]), fault["family"]):
                raise ValueError("fixture metadata differs from executed scenario")
    return validate_config(c)


def _injector(config):
    fault = config.get("fault")
    if not fault:
        return None, None
    module, sep, name = fault["injector_factory"].partition(":")
    if not sep or not module or not name:
        raise ValueError("injector_factory must be module:factory")
    inject = getattr(importlib.import_module(module), name)(task_id=config["task_id"],
                                                          scenario_id=config["scenario_id"])
    if not callable(inject):
        raise ValueError("injector factory must return callable")
    return inject, InjectionSpec(config["scenario_id"], SITES[fault["site"]])


def execute_workflow(config, directory, *, client_factory=None, api_key="ollama"):
    c = prepare_config(config)
    model = ModelConfig(**c["workflow"]["model"])
    if c["synthetic"] and client_factory is None:
        raise ValueError("synthetic runs require an explicit scripted client factory")
    client = client_factory(model) if client_factory else OpenAICompatibleClient(model, api_key=api_key)
    if client.config.public_dict() != model.public_dict():
        raise ValueError("actual client configuration differs from experiment")
    injector, spec = _injector(c)
    session_config = WorkflowConfig(**c["workflow"]["session"])
    result = run_phase1(directory, task_id=c["task_id"], condition=c["condition"],
        environment_adapter=BankingEnvironmentAdapter(c["task_id"]),
        create_session=lambda: BankingWorkflowSession(client, session_config, injection_spec=spec),
        injector=injector, synthetic=c["synthetic"], experiment_config=c,
        cross_attempt_evaluator=cross_attempt_adapter(c["task_id"]))
    workflow_report(directory)
    return result


def workflow_batch(configs, output, *, client_factory=None, api_key="ollama"):
    # Resolve once for grouping; execute rechecks before each trial to reject drift.
    configs = [prepare_config(c) for c in configs]
    def execute(c, directory):
        return execute_workflow(c, directory, client_factory=client_factory, api_key=api_key)
    return run_batch(configs, output, execute)


def summarize_workflow_runs(directories, output):
    """Read-only import of configured traces; never overwrite original reports.

    Legacy B traces without D configuration are rejected instead of guessing their
    controls or repeat identities. They remain usable with B's existing reports.
    """
    output = Path(output).resolve()
    paths = [Path(p).resolve() for p in directories]
    if any(output == p or output in p.parents or p in output.parents for p in paths):
        raise ValueError("summary destination must be separate from source runs")
    rows, ids = [], set()
    for path in paths:
        manifest = json.loads((path / "manifest.json").read_text())
        if not manifest.get("experiment"):
            raise ValueError("legacy run lacks experiment controls: " + str(path))
        if manifest["run_id"] in ids:
            raise ValueError("duplicate source run_id: " + manifest["run_id"])
        ids.add(manifest["run_id"])
        config = validate_config(manifest["experiment"])
        error = None
        try:
            summary = summarize_run(path, write=False)
        except (ValueError, OSError) as exc:
            summary = {"run_id": manifest["run_id"], "trace_complete": False}
            error = str(exc)
        rows.append({"config": config, "summary": summary, "error": error})
    output.mkdir(parents=True, exist_ok=False)
    atomic_json(output / "sources.json", {"directories": [str(p) for p in paths]})
    for i, row in enumerate(rows):
        atomic_json(output / f"result_{i:04d}.json", row)
    aggregate(rows, output)
    return rows
