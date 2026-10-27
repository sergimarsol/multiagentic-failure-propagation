"""Factory-based Phase 1 runner. No checkpoint or context serialization dependency."""
from contextlib import contextmanager
from time import perf_counter

from .models import Artifact, Evaluation, detached, fingerprint, new_id
from .recorder import Recorder, DependencyStore
from .metrics import summarize_run
from .protocol import Budget, Limits, provenance, validate_config


class Callbacks:
    """The single B/C-facing recording surface; IDs are allocated by D."""
    def __init__(self, recorder, attempt_id):
        self.recorder, self.attempt_id = recorder, attempt_id
        self.faulty_artifact_ids = []

    @contextmanager
    def node(self, node_id, inputs=()):
        execution = new_id("execution")
        fields = dict(node_id=node_id, node_execution_id=execution)
        DependencyStore(self.recorder).record(self.attempt_id, node_id, execution, inputs=list(inputs))
        self.recorder.log("node_started", self.attempt_id, **fields)
        started, status = perf_counter(), "success"
        try:
            yield execution
        except Exception:
            status = "error"
            raise
        finally:
            self.recorder.log("node_finished", self.attempt_id, **fields,
                duration_ms=(perf_counter()-started)*1000, data={"status": status})

    def call(self, kind, node_id, execution, **payload):
        return self.recorder.call(kind, self.attempt_id, node_id=node_id,
            node_execution_id=execution, **payload)

    def artifact(self, execution, content, inputs=(), *, original=None):
        artifact = Artifact(run_id=self.recorder.run_id, attempt_id=self.attempt_id,
            artifact_id=new_id("artifact"), producer_execution_id=execution,
            artifact_type="intermediate", content=content, input_artifact_ids=list(inputs),
            derived_from_artifact_id=original)
        self.recorder.save_artifact(artifact)
        return artifact.artifact_id

    def inject(self, original_id, execution, injector, *, site, enabled):
        original = self.recorder.artifact(original_id)["content"]
        # Crucially, the injector is never invoked in a clean/restarted attempt.
        report = injector(detached(original), site) if enabled and injector else None
        triggered = bool(report and report["triggered"])
        replacement = self.artifact(execution, report["replacement"], original=original_id) if triggered else original_id
        self.recorder.log("fault_injected", self.attempt_id, data={
            "triggered": triggered, "enabled": enabled, "site": site,
            "fault_type": report.get("fault_type") if report else None,
            "original_artifact_id": original_id, "replacement_artifact_id": replacement})
        if triggered:
            self.faulty_artifact_ids.append(replacement)
        return replacement

    def content(self, artifact_id):
        return self.recorder.artifact(artifact_id)["content"]


def run_phase1(directory, *, task_id, condition, environment_adapter,
               create_session, injector=None, synthetic=False, cross_attempt_evaluator=None, experiment_config=None, limits=None):
    """A adapter: create_initial_environment, read_state, trace, evaluate_execution.

    B factory returns an object with run(env, callbacks, injector, injection_enabled,
    always_verify). read_state/trace are D adaptation hooks, not new A systems.
    Restart uses the injection oracle at the end-of-workflow boundary, once only.
    """
    if condition not in {"clean", "fault", "full_restart", "always_verify", "clean_verify"}:
        raise ValueError("unknown condition")
    if experiment_config is not None:
        experiment_config = validate_config(experiment_config)
        if (experiment_config["task_id"], experiment_config["condition"], experiment_config["synthetic"]) != (task_id, condition, synthetic):
            raise ValueError("runner arguments differ from experiment configuration")
        limits = Limits(**experiment_config["limits"])
    rec = Recorder(directory, {"run_id": new_id("run"), "task_id": task_id,
        "condition": condition, "synthetic": synthetic,
        "history_policy": "union_of_per_attempt_classifications",
        "notification_boundary": "after_initial_workflow", **provenance(experiment_config)})
    rec.budget = Budget(limits) if limits else None
    started = perf_counter()
    evaluations, environments, sessions = [], [], []
    initial_hash = None
    faulty_ids = []
    attempt_audits = []
    for index in range(2 if condition == "full_restart" else 1):
        attempt = f"attempt_{index}"
        if index and not faulty_ids:
            break
        tick = perf_counter()
        if index:
            rec.log("recovery_started", attempt, data={"new_attempt_id": attempt,
                "strategy": "fresh_factories", "requested_strategy": "full_restart", "executed_strategy": "full_restart", "faulty_artifact_ids": faulty_ids,
                "source": "injection_oracle"})
        rec.segment_id = new_id("segment")
        rec.log("segment_started", attempt, data={"parent_segment_id": None})
        rec.log("attempt_started", attempt)
        cb = Callbacks(rec, attempt)
        completed = False
        try:
            env = environment_adapter.create_initial_environment(task_id)
            session = create_session()
            if any(env is old for old in environments) or any(session is old for old in sessions):
                raise ValueError("factories must return fresh environment and session objects")
            environments.append(env)
            sessions.append(session)
            initial = detached(environment_adapter.read_state(env))
            if initial_hash is not None and fingerprint(initial) != initial_hash:
                raise ValueError("restart initial state differs from original")
            initial_hash = fingerprint(initial)
            rec.log("environment_initialized", attempt, data={"initial_state": initial, "initial_state_fingerprint": initial_hash})
            execution_error = None
            try:
                if rec.budget:
                    rec.budget.check()
                session.run(env, cb, injector, injection_enabled=index == 0 and condition not in {"clean", "clean_verify"},
                            always_verify=condition in {"always_verify", "clean_verify"})
                if rec.budget:
                    rec.budget.check()
                completed = True
            except Exception as exc:
                execution_error = repr(exc)
                rec.log("workflow_failed", attempt, data={"error": execution_error})
            final = detached(environment_adapter.read_state(env))
            trace = detached(environment_adapter.trace(env))
            rec.log("environment_finalized", attempt, data={"final_state": final, "audit": trace})
            attempt_audits.append({"attempt_id": attempt, "audit": trace})
            result = environment_adapter.evaluate_execution(initial, final, trace)
            ev = Evaluation(run_id=rec.run_id, attempt_id=attempt, evaluation_id=new_id("evaluation"),
                scope="attempt", evaluator="A.adapter", **result)
            rec.save_evaluation(ev)
            evaluations.append(ev)
        except Exception as exc:
            completed = False
            rec.log("attempt_failed", attempt, data={"error": repr(exc)})
            ev = Evaluation(run_id=rec.run_id, attempt_id=attempt, evaluation_id=new_id("evaluation"),
                scope="attempt", evaluator="D.unavailable", task_success=None)
            rec.save_evaluation(ev)
            evaluations.append(ev)
        faulty_ids = cb.faulty_artifact_ids
        rec.log("attempt_finished", attempt, duration_ms=(perf_counter()-tick)*1000,
                data={"completed": completed})
        if index:
            rec.log("recovery_finished", attempt, duration_ms=(perf_counter()-tick)*1000,
                    data={"completed": completed, "task_success": ev.task_success, "requested_strategy": "full_restart", "executed_strategy": "full_restart"})
    def union(field):
        values = [getattr(e, field) for e in evaluations]
        return sorted({v for group in values for v in group}) if all(v is not None for v in values) else None
    repeat_report = {"status": "not_evaluated", "actions": None}
    if cross_attempt_evaluator is not None and len(attempt_audits) == len(evaluations):
        try:
            actions = detached(cross_attempt_evaluator(detached(attempt_audits)))
            if not isinstance(actions, list) or not all(isinstance(a, dict) for a in actions):
                raise ValueError("cross-attempt evaluator must return a list of evidence records")
            repeat_report = {"status": "evaluated", "actions": actions}
        except Exception as exc:
            repeat_report = {"status": "error", "actions": None, "error": repr(exc)}
    permissions = [e.permission_violations for e in evaluations]
    rec.save_evaluation(Evaluation(run_id=rec.run_id, attempt_id=attempt,
        evaluation_id=new_id("evaluation"), scope="history", evaluator="D.per_attempt_union",
        permission_violations=[v for group in permissions for v in group] if all(v is not None for v in permissions) else None,
        task_success=None, unauthorized_attempt_ids=union("unauthorized_attempt_ids"),
        unauthorized_execution_ids=union("unauthorized_execution_ids"),
        details={"policy": "per_attempt_union", "external_effect_recovery": "not_evaluated",
                 "cross_attempt_repeats": repeat_report}))
    rec.log("run_finished", attempt, duration_ms=(perf_counter()-started)*1000,
            data={"recording_overhead_ms": rec.write_ms, "budget": rec.budget.report() if rec.budget else None})
    return summarize_run(directory)
