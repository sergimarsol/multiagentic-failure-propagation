"""Opt-in adapters to A; preserve A's authorization and historical scoring policy."""

from .models import Evaluation, fingerprint, new_id


class BankingAdapter:
    def __init__(self, env, recorder):
        self.env, self.recorder = env, recorder

    def snapshot(self):
        return self.env.state_dict()

    def restore(self, snapshot):
        from env.state import env_from_state
        self.env.restore(env_from_state(snapshot))

    def audit_key(self, attempt_id):
        # A's run_id is an execution filter. D's top-level run_id remains unchanged.
        return f"{self.recorder.run_id}:{attempt_id}"

    def call_tool(self, role, name, args, *, attempt_id, node_id, node_execution_id, inputs=()):
        for ref in inputs:
            self.recorder.artifact(ref)
        step = new_id("step")
        before = self.snapshot()
        with self.recorder.call("tool", attempt_id, step_id=step, node_id=node_id,
            node_execution_id=node_execution_id, role=role, tool=name, args=args,
            input_artifact_ids=list(inputs)) as end:
            result = self.env.call_tool(role, name, args, run_id=self.audit_key(attempt_id), step_id=step)
            after = self.snapshot()
            # Conservatively depend on every state resource for any invoked function.
            # Hashes are content references, not a claim of fine-grained read tracking.
            reads = [{"resource_key": k, "version": fingerprint(v)} for k, v in before.items()] if result.executed else []
            writes = [{"resource_key": k, "before_version": fingerprint(before[k]),
                       "after_version": fingerprint(after[k])} for k in before if before[k] != after[k]]
            end.update(status="error" if result.error else "success", error=result.error,
                allowed=result.allowed, invoked=result.executed, state_changed=bool(writes),
                result=result.result, result_text=result.result_text, audit_call_id=result.call_id,
                state_reads=reads, state_writes=writes, env_epoch=self.env.env_version,
                effect_ids=[result.call_id] if writes else [], effect_kind="simulated_restorable")
        self.recorder.log("dependencies_recorded", attempt_id, node_id=node_id,
            node_execution_id=node_execution_id, step_id=step,
            data={"input_artifact_ids": list(inputs), "control_artifact_ids": [],
                  "state_reads": reads, "state_writes": writes, "granularity": "whole_environment"})
        return result


class BankingEvaluator:
    def __init__(self, adapter):
        self.adapter = adapter

    def evaluate(self, attempt_id, scope):
        from eval.scoring import evaluate
        adapter, env = self.adapter, self.adapter.env
        if scope not in {"attempt", "history"}:
            raise ValueError("Banking adapter evaluates attempts/history, not propagation")
        prefix = adapter.recorder.run_id + ":"
        entries = [e for e in env.audit_log() if (e.get("run_id") or "").startswith(prefix)]
        if scope == "attempt":
            entries = [e for e in entries if e["run_id"] == adapter.audit_key(attempt_id)]
        result = evaluate(env.task_id, env.initial_state, env.snapshot(), entries)
        return Evaluation(run_id=adapter.recorder.run_id, attempt_id=attempt_id,
            evaluation_id=new_id("evaluation"), scope=scope, evaluator="A.strict_scoring",
            task_success=result.strict_success if not result.warnings else None,
            unauthorized_attempt_ids=[e["call_id"] for e in result.unauthorized_attempted],
            unauthorized_execution_ids=[e["call_id"] for e in result.unauthorized_executed],
            evidence_ids=[e["call_id"] for e in entries], details=result.to_dict())
