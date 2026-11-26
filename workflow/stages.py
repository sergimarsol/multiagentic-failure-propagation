"""B's completed-stage contract. D owns checkpoints, scheduling and trial budgets."""

from dataclasses import asdict, dataclass

from experiments.models import detached
from .contracts import WorkflowError

STAGE_STATE_VERSION = "b-stage-state-v1"
STAGE_ROLES = {"research": "researcher", "plan": "planner", "execute": "executor",
               "verify_research": "verifier", "verify_plan": "verifier"}


@dataclass(frozen=True)
class StageState:
    """Minimal continuation state, not a suspended model conversation.

    Counts cover the effective workflow path. D's append-only recorder/budget keeps
    the cost of *all* calls, including discarded paths and failed retries.
    Evidence IDs refer to immutable tool results in that same run and attempt.
    """

    run_id: str
    attempt_id: str
    config_hash: str
    model_calls: int = 0
    tool_calls: int = 0
    evidence_ids: tuple[str, ...] = ()
    version: str = STAGE_STATE_VERSION

    def __post_init__(self):
        if self.version != STAGE_STATE_VERSION:
            raise WorkflowError("Unsupported stage state version")
        if any(not isinstance(v, str) or not v for v in
               (self.run_id, self.attempt_id, self.config_hash)):
            raise WorkflowError("Stage state requires run, attempt and configuration identity")
        if any(type(v) is not int or v < 0 for v in (self.model_calls, self.tool_calls)):
            raise WorkflowError("Stage call counts must be nonnegative integers")
        if (not isinstance(self.evidence_ids, (list, tuple))
                or any(not isinstance(v, str) or not v for v in self.evidence_ids)
                or len(set(self.evidence_ids)) != len(self.evidence_ids)):
            raise WorkflowError("Stage evidence must contain unique artifact IDs")
        object.__setattr__(self, "evidence_ids", tuple(self.evidence_ids))

    def to_dict(self):
        return detached({**asdict(self), "evidence_ids": list(self.evidence_ids)})

    @classmethod
    def from_dict(cls, value):
        # All fields, including the version, must be present in persisted state.
        if not isinstance(value, dict) or set(value) != set(cls.__dataclass_fields__):
            raise WorkflowError("Incomplete or unknown stage state fields")
        return cls(**detached(value))


@dataclass(frozen=True)
class StageResult:
    """One successful stage. Injection/delivery and bank restoration belong to D.

    messages are audit data; they are never loaded into the next stage's dialogue.
    Input/control IDs describe the supplied inputs; the output artifact also links
    to every intermediate model response and tool result consumed by this stage.
    """

    stage: str
    role: str
    artifact_id: str
    execution_id: str
    input_artifact_ids: tuple[str, ...]
    control_artifact_ids: tuple[str, ...]
    messages: list
    model_calls: int
    tool_calls: int
    generation: int
    state: StageState

    def to_context(self):
        """Detached JSON for D's node context and completed-stage checkpoint."""
        return detached({
            "role": self.role, "messages": self.messages,
            "stage_artifact_id": self.artifact_id,
            "input_artifact_ids": list(self.input_artifact_ids),
            "control_artifact_ids": list(self.control_artifact_ids),
            "model_calls": self.model_calls, "tool_calls": self.tool_calls,
            "generation": self.generation, "resume_state": self.state.to_dict(),
        })
