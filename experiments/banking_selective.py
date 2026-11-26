"""Real Banking selective recovery: B's stages on D's fixed-DAG engine.

D owns only the wiring. Stage prompts, permissions and message handling stay in
B's session, scoring stays in A's evaluator, and the fault comes from C's injector
through B's InjectionGate. The oracle names the faulty result; it never supplies
a corrected one.
"""
from contextlib import contextmanager
from dataclasses import asdict
from time import perf_counter

from env import BankingEnv
from workflow.client import ModelRequestError
from workflow.contracts import CONTRACT_VERSION, PLAN_SCHEMA, InjectionGate
from workflow.prompts import PROMPTS, RESEARCH_FORMATS
from workflow.session import BankingWorkflowSession, WorkflowConfig

from .banking import BankingAdapter, BankingEvaluator
from .metrics import summarize_run
from .models import Evaluation, detached, fingerprint, new_id
from .protocol import Budget, Limits, normalize_recovery_policy, provenance, validate_config
from .recorder import Recorder
from .recovery import FaultNotice
from .selective import DagWorkflow, Node, NodeOutput, RetryableNodeError, SelectiveRecoveryCoordinator

RUNNER_VERSION = "banking_phase2_v2"
RESTORE_MODES = ("simulated", "real_bank")
BANK_KEYS = ("balance", "iban", "transactions", "scheduled_transactions", "files", "user_account")
AUDIT_PATH = "_d_audit_path"
# Whole-environment granularity, matching BankingAdapter's per-call dependency records.
# Every stage may append to the audit path: a denied or hallucinated call is still audited.
NODES = (
    Node("research", reads=BANK_KEYS, writes=(AUDIT_PATH,)),
    Node("plan", inputs=("research",), writes=(AUDIT_PATH,)),
    Node("execute", inputs=("plan",), reads=BANK_KEYS, writes=BANK_KEYS + (AUDIT_PATH,)),
)


class _StageCallbacks:
    """Record B's stage inside the DAG node execution instead of opening a second node."""
    def __init__(self, callbacks, execution, faulty_artifact_ids):
        self._callbacks, self._execution = callbacks, execution
        self.recorder, self.attempt_id = callbacks.recorder, callbacks.attempt_id
        self.faulty_artifact_ids = faulty_artifact_ids
        self.call, self.artifact, self.content = callbacks.call, callbacks.artifact, callbacks.content

    @contextmanager
    def node(self, node_id, inputs=()):
        yield self._execution


def make_banking_workflow(recorder, task_id, client, session_config=None, *, injector=None, injection_spec=None,
                          restore_mode="simulated", carry_from=None):
    """Fresh environment, fresh audit and fresh stage contexts for one attempt."""
    session_config = session_config or WorkflowConfig()
    env = BankingEnv().load(task_id)
    if carry_from is not None and restore_mode == "real_bank":
        # A real bank cannot start over: a full restart begins from the initial state plus the
        # money the abandoned attempt already sent.
        from env.state import env_from_state
        env.restore(env_from_state(carry_from.expected_restore(env.state_dict())))
    bank = BankingAdapter(env, recorder, segmented_audit=True, segment_on_restore=False,
                          keep_irreversible=restore_mode == "real_bank")
    holder = {}

    def execute(node, inputs, environment, context, callbacks, execution):
        workflow = holder["workflow"]
        # One single-use B session per stage execution: no model history crosses stages or reruns.
        session = BankingWorkflowSession(client, session_config)
        # The last completed stage carries cumulative path counts and original evidence.
        # D's recorder/budget separately retains costs from every abandoned branch.
        completed = workflow.state["completed_nodes"]
        state = workflow.state["contexts"][completed[-1]]["resume_state"] if completed else None
        request = callbacks.artifact(execution, {"user_request": env.prompt})
        try:
            result = session.run_stage(node.name, environment=environment,
                callbacks=_StageCallbacks(callbacks, execution, workflow.faulty_ids),
                request_id=request, inputs={name: workflow.state["artifact_map"][name] for name in node.inputs},
                state=state)
        except ModelRequestError as exc:
            # The request has terminated and A's simulator reports every effect, so a
            # provider-declared transient failure may be retried from a checkpoint.
            if exc.retryable:
                raise RetryableNodeError(f"{node.name}: {exc}") from exc
            raise
        context.update(result.to_context())
        return NodeOutput(result.artifact_id)

    gate = InjectionGate(injector, injection_spec) if injector is not None else None
    workflow = DagWorkflow(recorder, bank, list(NODES), execute,
        config={"runner": RUNNER_VERSION, "task_id": task_id, "session": asdict(session_config),
                "model": client.config.public_dict(), "prompts": fingerprint(PROMPTS),
                "research_format": RESEARCH_FORMATS.get(task_id, ""), "plan_schema": fingerprint(PLAN_SCHEMA),
                "restore_mode": restore_mode},
        injector=gate, fault_node=injection_spec.site if gate and injection_spec else None)
    workflow.gate = gate
    holder["workflow"] = workflow
    return workflow


def evaluate_effective(workflow, attempt):
    """A's verdict for the calls on the current effective path of this attempt."""
    return BankingEvaluator(workflow.environment).evaluate(attempt, "attempt")


def _path(label, workflow, attempt, evaluation):
    bank = workflow.environment
    return {"label": label, "attempt_id": attempt, "call_ids": list(bank._effective_call_ids),
            "strict_success": evaluation.strict_success,
            "unauthorized_attempt_ids": evaluation.unauthorized_attempt_ids,
            "unauthorized_execution_ids": evaluation.unauthorized_execution_ids,
            "permission_violations": evaluation.permission_violations}


def _history(rec, task_id, paths, environments, final_attempt, result):
    """A's recovery scoring (eval.scoring.evaluate_recovery) over every audited call.

    Harm is judged on every call, including branches a restore dropped; writes the effective
    path dropped are listed as unresolved (money sent) or compensated (updates).
    """
    from eval.scoring import evaluate_recovery
    audit = {e["call_id"]: e for env in environments for e in env.audit_log()}
    final_env = environments[-1]
    try:
        scored = evaluate_recovery(task_id, final_env.initial_state, final_env.snapshot(),
                                   list(audit.values()), paths[-1]["call_ids"])
        history = scored["history"]
    except Exception as exc:
        scored, history = None, None
        rec.log("attempt_failed", final_attempt, data={"error": repr(exc), "phase": "history_evaluation"})
    repeats = {"status": "not_evaluated", "actions": None}
    if len(paths) == 2:
        try:
            from eval.scoring import cross_attempt_repeats
            first = set(paths[0]["call_ids"])
            groups = [[audit[c] for c in paths[0]["call_ids"]],
                      [audit[c] for c in paths[1]["call_ids"] if c not in first]]
            rows = cross_attempt_repeats(task_id, groups)
            repeats = {"status": "evaluated", "scope": "initial_path_vs_recovery_executions",
                       "prior_effects_restored": result.get("executed_strategy") == "selective",
                       "actions": [{**row, "attempt_id": paths[row["attempt_index"]]["attempt_id"],
                                    "path": paths[row["attempt_index"]]["label"]} for row in rows]}
        except Exception as exc:
            repeats = {"status": "error", "actions": None, "error": repr(exc)}
    ids = lambda key: [e["call_id"] for e in getattr(history, key)] if history else None
    return Evaluation(run_id=rec.run_id, attempt_id=final_attempt, evaluation_id=new_id("evaluation"),
        scope="history", evaluator="A.evaluate_recovery",
        strict_success=history.strict_success if history and not history.warnings else None,
        permission_violations=history.permission_violations if history else None,
        unauthorized_attempt_ids=ids("unauthorized_attempted"),
        unauthorized_execution_ids=ids("unauthorized_executed"),
        evidence_ids=sorted(audit),
        details={"policy": "all_audited_calls", "paths": paths,
                 "external_effect_recovery": "evaluated" if scored else "not_evaluated",
                 "unresolved_effects": [e["call_id"] for e in scored["unresolved_effects"]] if scored else None,
                 "compensated_effects": [e["call_id"] for e in scored["compensated_effects"]] if scored else None,
                 "cross_attempt_repeats": repeats})


def run_banking_phase2(directory, *, task_id, condition, client, session_config=None, injector=None,
                       injection_spec=None, synthetic=False, experiment_config=None, limits=None,
                       recovery_policy=None, notification_boundary="after_initial_workflow",
                       restore_mode="simulated"):
    """One trial: initial workflow, oracle notice, then the requested recovery.

    The notice arrives at the end of the initial workflow (default), or with
    "after_faulty_handoff" as soon as the faulty result is handed off, before any
    downstream stage runs. condition "selective" restores the newest uncontaminated
    checkpoint and reruns the affected stages; "full_restart" is the matched baseline on
    the same adapter, using a fresh environment and fresh stages. An untriggered fault
    starts no recovery.
    """
    if condition not in {"selective", "full_restart"}:
        raise ValueError("Phase 2 runner supports selective and full_restart")
    policy = normalize_recovery_policy(recovery_policy or {})
    if experiment_config is not None:
        notification_boundary = experiment_config.get("notification_boundary", "after_initial_workflow")
        restore_mode = experiment_config.get("restore_mode", "simulated")
    if notification_boundary not in {"after_initial_workflow", "after_faulty_handoff"}:
        raise ValueError("unsupported notification_boundary")
    early = notification_boundary == "after_faulty_handoff"
    # simulated: a restore undoes every effect in the simulator. real_bank: money already sent
    # stays (A's restore(keep_irreversible=True)); only what a real bank could undo is restored.
    if restore_mode not in RESTORE_MODES:
        raise ValueError("unsupported restore_mode")
    if experiment_config is not None:
        experiment_config = validate_config(experiment_config)
        if (experiment_config["task_id"], experiment_config["condition"],
                experiment_config["synthetic"]) != (task_id, condition, synthetic):
            raise ValueError("runner arguments differ from experiment configuration")
        policy = normalize_recovery_policy(experiment_config.get("recovery_policy", policy))
        limits = Limits(**experiment_config["limits"])
    limits = limits or Limits()
    session_config = session_config or WorkflowConfig()
    rec = Recorder(directory, {"run_id": new_id("run"), "task_id": task_id, "condition": condition,
        "synthetic": synthetic, "runner": RUNNER_VERSION, "recovery_policy": policy,
        "limits": asdict(limits), "history_policy": "all_audited_calls",
        "notification_boundary": notification_boundary, "restore_mode": restore_mode,
        **provenance(experiment_config)})
    rec.budget = Budget(limits)
    started = tick = perf_counter()
    attempt = "attempt_0"
    rec.segment_id = new_id("segment")
    rec.log("segment_started", attempt, data={"parent_segment_id": None})
    rec.log("attempt_started", attempt)
    workflow = make_banking_workflow(rec, task_id, client, session_config,
                                     injector=injector, injection_spec=injection_spec,
                                     restore_mode=restore_mode)
    env = workflow.environment.env
    initial = detached(env.state_dict())
    rec.log("environment_initialized", attempt,
            data={"initial_state": initial, "initial_state_fingerprint": fingerprint(initial)})
    rec.log("workflow_config", attempt, data={
        "contract": CONTRACT_VERSION, "model": client.config.public_dict(), "limits": asdict(session_config),
        "prompt_hashes": {k: fingerprint(v) for k, v in PROMPTS.items()},
        "research_format": RESEARCH_FORMATS.get(task_id, ""), "plan_schema_hash": fingerprint(PLAN_SCHEMA),
        "role_permissions": {r: list(env.allowed_tools(r)) for r in PROMPTS}})
    rec.log("injection_config", attempt, data={
        "scenario_id": injection_spec.scenario_id if injection_spec else None,
        "site": injection_spec.site if injection_spec else None,
        "enabled": True, "injector_present": injector is not None})
    workflow.points.capture(attempt, "C0", injection_phase="before_injection")
    completed, stopped_for_notice = False, False
    try:
        rec.budget.check()
        # An early notice halts the pass at the faulty handoff; recovery runs the rest.
        stopped_for_notice = not workflow.run(attempt, stop_after_fault=early)
        rec.budget.check()
        completed = not stopped_for_notice
    except Exception as exc:
        rec.log("workflow_failed", attempt, data={"error": repr(exc)})
    if workflow.gate is not None:
        rec.log("injection_attempt_finished", attempt, data=workflow.gate.finish(True))

    def score(label, target, target_attempt):
        try:
            evaluation = evaluate_effective(target, target_attempt)
        except Exception as exc:
            rec.log("attempt_failed", target_attempt, data={"error": repr(exc), "phase": "evaluation"})
            evaluation = Evaluation(run_id=rec.run_id, attempt_id=target_attempt,
                evaluation_id=new_id("evaluation"), scope="attempt", evaluator="D.unavailable")
        return evaluation, _path(label, target, target_attempt, evaluation)

    if hasattr(client, "end_initial_pass"):
        # Methods of one scenario and repeat replay the same recorded initial pass.
        rec.log("initial_pass", attempt, data=client.end_initial_pass())

    evaluation, first = score("initial", workflow, attempt)
    rec.save_evaluation(evaluation)
    paths, environments = [first], [env]
    rec.log("attempt_finished", attempt, duration_ms=(perf_counter() - tick) * 1000,
            data={"completed": completed, "stopped_for_notice": stopped_for_notice,
                  "pending_nodes": list(workflow.state["pending_nodes"])})

    result, final_attempt = {}, attempt
    if workflow.faulty_ids:
        tick = perf_counter()
        coordinator = SelectiveRecoveryCoordinator(
            workflow, lambda r: make_banking_workflow(r, task_id, client, session_config,
                                                      restore_mode=restore_mode,
                                                      carry_from=workflow.environment),
            evaluate_effective, **policy)
        result = coordinator.recover(FaultNotice(tuple(workflow.faulty_ids)), attempt,
                                     requested_strategy=condition,
                                     notification_boundary=notification_boundary)
        final, final_attempt = coordinator.workflow, result["attempt_id"]
        if final is not workflow:
            environments.append(final.environment.env)
        if final is not workflow or result.get("checkpoint_attempts"):
            # The coordinator stored A's verdict when recovery completed; this only records the path.
            paths.append(score("recovery_effective", final, final_attempt)[1])
        rec.log("attempt_finished", final_attempt, duration_ms=(perf_counter() - tick) * 1000,
                data={"completed": bool(result.get("completed"))})
    # One finalization per attempt, after recovery: the audit keeps abandoned branches,
    # so harm that a restore undid in the simulator is still judged.
    for target_attempt, target_env in zip(dict.fromkeys(p["attempt_id"] for p in paths), environments):
        rec.log("environment_finalized", target_attempt,
                data={"final_state": detached(target_env.state_dict()), "audit": detached(target_env.audit_log())})
    rec.save_evaluation(_history(rec, task_id, paths, environments, final_attempt, result))
    rec.log("run_finished", final_attempt, duration_ms=(perf_counter() - started) * 1000,
            data={"recording_overhead_ms": rec.write_ms, "budget": rec.budget.report()})
    return summarize_run(directory)
