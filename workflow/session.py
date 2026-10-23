"""Three independent model roles, two C boundaries, and evidence-based verification."""

from dataclasses import asdict, dataclass
import json

from experiments.banking import BankingAdapter
from experiments.models import detached, fingerprint
from experiments.recorder import DependencyStore
from .client import ModelRequestError
from .contracts import (CONTRACT_VERSION, PLAN_SCHEMA, InjectionGate, WorkflowError, parse_object,
                        validate_content, validate_verdict)
from .prompts import PROMPTS, RESEARCH_FORMATS


@dataclass(frozen=True)
class WorkflowConfig:
    max_turns_per_stage: int = 8
    max_model_calls: int = 48
    max_tool_calls: int = 24
    max_input_chars: int = 100_000
    request_retries: int = 0
    verification_retries: int = 1
    format_retries: int = 1

    def __post_init__(self):
        for key in ("max_turns_per_stage", "max_model_calls", "max_tool_calls", "max_input_chars"):
            if getattr(self, key) <= 0:
                raise ValueError(f"{key} must be positive")
        if any(value not in (0, 1) for value in (self.request_retries, self.verification_retries, self.format_retries)):
            raise ValueError("Phase 1 supports at most one retry of each kind")


class BankingWorkflowSession:
    def __init__(self, client, config=WorkflowConfig(), *, injection_spec=None):
        self.client, self.config = client, config
        self.injection_spec = injection_spec
        self.histories = {role: [] for role in PROMPTS}
        self.model_calls = self.tool_calls = 0
        self.evidence = []
        self._used = False

    def run(self, env, callbacks, injector, *, injection_enabled, always_verify):
        if self._used:
            raise WorkflowError("A session is single-use; restart must create a fresh session")
        self._used = True
        self.env, self.cb = env, callbacks
        self.bank = BankingAdapter(env, callbacks.recorder)
        self.gate = InjectionGate(injector, self.injection_spec)
        self.injection_enabled, self.always_verify = injection_enabled, always_verify
        with self.cb.node("setup") as execution:
            self.request_id = self.cb.artifact(execution, {"user_request": env.prompt})
            self.cb.recorder.log("workflow_config", self.cb.attempt_id, data={
                "contract": CONTRACT_VERSION, "model": self.client.config.public_dict(),
                "limits": asdict(self.config), "prompt_hashes": {k: fingerprint(v) for k, v in PROMPTS.items()},
                "research_format": RESEARCH_FORMATS.get(env.task_id, ""),
                "plan_schema_hash": fingerprint(PLAN_SCHEMA),
                "role_permissions": {r: list(env.allowed_tools(r)) for r in PROMPTS},
            })
            self.cb.recorder.log("injection_config", self.cb.attempt_id, data={
                "scenario_id": self.injection_spec.scenario_id if self.injection_spec else None,
                "site": self.injection_spec.site if self.injection_spec else None,
                "enabled": injection_enabled, "injector_present": injector is not None})

        try:
            self._pipeline()
        finally:
            self.cb.recorder.log("injection_attempt_finished", self.cb.attempt_id,
                                 data=self.gate.finish(injection_enabled))

    def _pipeline(self):
        def research(feedback, generation):
            return self._stage("researcher", "research", {}, [self.request_id],
                               feedback=feedback, generation=generation)
        research_id = self._boundary("research", research)

        def plan(feedback, generation):
            return self._stage("planner", "plan", {"research": self.cb.content(research_id)},
                               [self.request_id, research_id], feedback=feedback, generation=generation)
        plan_id = self._boundary("plan", plan)
        self._stage("executor", "execute", {"plan": self.cb.content(plan_id)}, [self.request_id, plan_id])

    def _boundary(self, site, produce):
        candidate, execution = produce(None, 0)
        try:
            delivered = self.cb.inject(candidate, execution, self.gate, site=site, enabled=self.injection_enabled)
        except Exception as exc:
            self.gate._report({"triggered": False, "applied": False, "reason": "contract_error"}, site)
            self.cb.recorder.log("injection_contract_error", self.cb.attempt_id,
                                 data={"site": site, "error": repr(exc), "original_artifact_id": candidate})
            self._injection_observation(site, candidate, candidate)
            raise
        self._injection_observation(site, candidate, delivered)
        if not self.always_verify:
            return delivered
        for generation in range(self.config.verification_retries + 1):
            # Only immutable original tool results, not the pristine pre-injection summary.
            evidence_ids = list(self.evidence)
            verdict_id, _ = self._stage("verifier", "verify_" + site,
                {"boundary": site, "candidate": self.cb.content(delivered),
                 "original_evidence": [self.cb.content(ref) for ref in evidence_ids]},
                [self.request_id, delivered, *evidence_ids], generation=generation)
            verdict = self.cb.content(verdict_id)
            self.cb.recorder.log("verification_decision", self.cb.attempt_id,
                data={"site": site, "candidate_artifact_id": delivered, "verdict_artifact_id": verdict_id,
                      "accepted": verdict["accepted"], "generation": generation})
            if verdict["accepted"]:
                return delivered
            if generation < self.config.verification_retries:
                # Fresh stage context, same legitimate upstream inputs, explicit verifier feedback.
                # The one-shot C hook is NOT called again for this regeneration.
                delivered, _ = produce(verdict_id, generation + 1)
        raise WorkflowError(f"Verifier rejected {site} after the regeneration limit")

    def _injection_observation(self, site, original, candidate):
        report = self.gate.last_report if self.injection_enabled else {
            "applied": False, "reason": "disabled",
            "scenario_id": self.injection_spec.scenario_id if self.injection_spec else None}
        self.cb.recorder.log("injection_observation", self.cb.attempt_id, data={
            **{k: v for k, v in (report or {}).items() if k not in {"replacement", "triggered"}},
            "site": site, "original_artifact_id": original, "candidate_artifact_id": candidate})

    def _stage(self, role, node, payload, input_refs, *, feedback=None, generation=0):
        refs = list(input_refs)
        body = {"user_request": self.env.prompt, **payload}
        if feedback:
            refs.append(feedback)
            verdict = self.cb.content(feedback)
            body["verification_feedback"] = {"issues": verdict["issues"], "feedback": verdict["feedback"]}
        system = PROMPTS[role]
        if role == "researcher":
            system += "\n" + RESEARCH_FORMATS.get(self.env.task_id, "")
        if role == "planner":
            system += "\nAvailable write-tool definitions (not callable by you):\n" + json.dumps(self.env.tool_schemas("executor"))
        tools = [{"type": "function", "function": schema} for schema in self.env.tool_schemas(role)]
        messages = [{"role": "system", "content": system},
                    {"role": "user", "content": json.dumps(body, ensure_ascii=False, allow_nan=False)}]
        # Store separate histories for each generation, never reuse another role's conversation.
        self.histories[role].append(messages)
        format_retries = 0
        with self.cb.node(node, inputs=refs) as execution:
            for turn in range(self.config.max_turns_per_stage):
                message, response_ref = self._model(role, node, execution, messages, tools, refs, generation, turn, format_retries)
                messages.append(message)
                refs.append(response_ref)
                calls = message.get("tool_calls") or []
                if not calls:
                    content = message.get("content") or ""
                    try:
                        if role in {"researcher", "planner"}:
                            content = validate_content("research" if role == "researcher" else "plan", parse_object(content))
                        elif role == "verifier":
                            content = validate_verdict(parse_object(content))
                    except WorkflowError as exc:
                        self.cb.recorder.log("format_rejected", self.cb.attempt_id, node_id=node,
                            node_execution_id=execution, data={"response_artifact_id": response_ref, "error": str(exc)})
                        if format_retries >= self.config.format_retries:
                            raise
                        format_retries += 1
                        messages.append({"role": "user", "content": "Output format error: " + str(exc)
                            + ". Return the required JSON object using the same evidence. Correct structure only; "
                              "no additional factual information or task answer is supplied."})
                        continue
                    return self.cb.artifact(execution, content, inputs=refs), execution
                for call in calls:
                    if self.tool_calls >= self.config.max_tool_calls:
                        raise WorkflowError("Tool-call budget exhausted")
                    try:
                        name = call["function"]["name"]
                        args = detached(json.loads(call["function"]["arguments"]))
                        if not isinstance(name, str) or not isinstance(args, dict):
                            raise ValueError("tool name and arguments must be a string and object")
                    except (KeyError, TypeError, ValueError) as exc:
                        raise WorkflowError("Malformed model tool call; raw response retained") from exc
                    self.tool_calls += 1
                    result = self.bank.call_tool(role, name, args, attempt_id=self.cb.attempt_id,
                        node_id=node, node_execution_id=execution, inputs=[response_ref])
                    text = result.error if result.error is not None else result.result_text
                    evidence = {"tool": name, "args": args, "result_text": text,
                                "error": result.error, "audit_call_id": result.call_id}
                    tool_ref = self.cb.artifact(execution, evidence, inputs=[response_ref])
                    refs.append(tool_ref)
                    if role in {"researcher", "verifier"}:
                        self.evidence.append(tool_ref)
                    messages.append({"role": "tool", "tool_call_id": call["id"], "content": text or "null"})
            raise WorkflowError(f"{node}: model turn limit exhausted")

    def _model(self, role, node, execution, messages, tools, refs, generation, turn, format_retries):
        # Never silently truncate a request: a truncated injected message is not exposure.
        if len(json.dumps({"messages": messages, "tools": tools})) > self.config.max_input_chars:
            raise WorkflowError("Input budget exceeded; request was not sent")
        for retry in range(self.config.request_retries + 1):
            if self.model_calls >= self.config.max_model_calls:
                raise WorkflowError("Model-call budget exhausted")
            self.model_calls += 1
            purpose = "verifier" if role == "verifier" else "retry" if generation or retry or format_retries else "agent"
            DependencyStore(self.cb.recorder).record(self.cb.attempt_id, node, execution, inputs=refs)
            try:
                with self.cb.call("model", node, execution, role=role, purpose=purpose,
                    model=self.client.config.public_dict(), messages=detached(messages), tools=detached(tools),
                    json_output=role == "planner", input_artifact_ids=list(refs),
                    generation=generation, turn=turn, request_retry=retry, format_retry=format_retries) as terminal:
                    response = self.client.complete(detached(messages), detached(tools), json_output=role == "planner")
                    terminal["response"] = detached(response)
                    # A provider response confirms request receipt. Transport failures do not
                    # establish exposure; the attempted input remains in model_started.
                    for ref in refs:
                        if ref in self.cb.faulty_artifact_ids:
                            event = "verifier_exposure" if role == "verifier" else "fault_exposure"
                            self.cb.recorder.log(event, self.cb.attempt_id, node_id=node, node_execution_id=execution,
                                data={"delivered": True, "artifact_id": ref, "role": role,
                                      "generation": generation, "turn": turn, "request_retry": retry,
                                      "meaning": "input_in_provider_acknowledged_request_not_proof_of_adoption"})
                    usage = response.get("usage") or {}
                    for dest, source in (("input_tokens", "prompt_tokens"), ("output_tokens", "completion_tokens")):
                        value = usage.get(source)
                        terminal[dest] = value if type(value) is int and value >= 0 else None
                    try:
                        choice = response["choices"][0]
                        message = detached(choice["message"])
                        if message.get("role") != "assistant" or not isinstance(message.get("content", ""), (str, type(None))):
                            raise ValueError("invalid assistant message")
                        calls = message.get("tool_calls") or []
                        if not isinstance(calls, list):
                            raise ValueError("tool_calls must be an array")
                        ids = [c["id"] for c in calls]
                        if not all(isinstance(i, str) and i for i in ids) or len(set(ids)) != len(ids):
                            raise ValueError("tool call IDs must be nonempty and unique within a turn")
                        if choice.get("finish_reason") in {"length", "content_filter"}:
                            raise ValueError(f"incomplete completion: {choice['finish_reason']}")
                    except (KeyError, TypeError, ValueError, IndexError) as exc:
                        raise WorkflowError(f"Invalid model response: {exc}") from exc
                    # Only protocol fields are sent back, while the full provider response stays logged.
                    message = {k: message[k] for k in ("role", "content", "tool_calls") if k in message}
                    terminal["output"] = message
                return message, self.cb.artifact(execution, message, inputs=refs)
            except ModelRequestError as exc:
                if not exc.retryable or retry == self.config.request_retries:
                    raise
