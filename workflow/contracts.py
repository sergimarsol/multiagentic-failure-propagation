"""B/C handoff contract v1: editable JSON bodies; provenance stays in D."""

import json
from dataclasses import dataclass
from experiments.models import detached

CONTRACT_VERSION = "b-handoff-v1"
SITES = ("research", "plan")

PLAN_SCHEMA = {
    "type": "object", "required": ["actions", "notes"], "additionalProperties": False,
    "properties": {
        "actions": {"type": "array", "items": {
            "type": "object", "required": ["tool", "args"], "additionalProperties": False,
            "properties": {"tool": {"type": "string"}, "args": {"type": "object", "additionalProperties": True}},
        }},
        "notes": {"type": "string"},
    },
}


class WorkflowError(RuntimeError):
    pass


@dataclass(frozen=True)
class InjectionSpec:
    """Controller-only trial identity; never added to model context."""
    scenario_id: str
    site: str

    def __post_init__(self):
        if not isinstance(self.scenario_id, str) or not self.scenario_id.strip() or self.site not in SITES:
            raise ValueError("An injection specification requires scenario_id and research/plan site")


def parse_object(text):
    if not isinstance(text, str):
        raise WorkflowError("Expected a JSON object in the model's final text")
    text = text.strip()
    if text.startswith("```") and text.endswith("```"):
        _, separator, body = text.partition("\n")
        if not separator:
            raise WorkflowError("Model returned an invalid fenced JSON block")
        text = body.rsplit("```", 1)[0].strip()
    try:
        value = detached(json.loads(text))
    except (ValueError, TypeError) as exc:
        raise WorkflowError("Model returned invalid JSON") from exc
    if not isinstance(value, dict):
        raise WorkflowError("Model output must be a JSON object")
    return value


def validate_content(site, content):
    """Validate structure only. Never check facts, allowed writes or task answers."""
    content = detached(content)
    keys = {"facts", "notes"} if site == "research" else {"actions", "notes"}
    if site not in SITES or not isinstance(content, dict) or set(content) != keys:
        raise WorkflowError(f"Invalid {site} message keys; expected {sorted(keys)}")
    if not isinstance(content["notes"], str):
        raise WorkflowError("notes must be a string")
    if site == "research":
        if not isinstance(content["facts"], dict):
            raise WorkflowError("facts must be an object")
    else:
        if not isinstance(content["actions"], list):
            raise WorkflowError("actions must be an array")
        for action in content["actions"]:
            if (not isinstance(action, dict) or set(action) != {"tool", "args"}
                    or not isinstance(action["tool"], str) or not isinstance(action["args"], dict)):
                raise WorkflowError("Each action must contain tool:string and args:object")
    return content


def validate_verdict(content):
    if (set(content) != {"accepted", "issues", "feedback"} or type(content["accepted"]) is not bool
            or not isinstance(content["issues"], list)
            or not all(isinstance(v, str) for v in content["issues"])
            or not isinstance(content["feedback"], str)):
        raise WorkflowError("Verifier must return accepted:boolean, issues:string[], feedback:string")
    return content


class InjectionGate:
    """Validate C reports and enforce at most one triggered replacement per session.

    D disables injection entirely on restart. C should also be one-shot per trial.
    C receives a detached body and a site, never the environment or callbacks.
    """
    def __init__(self, injector, spec=None):
        self.injector = injector
        self.spec = spec
        self.used = False
        self.last_report = None
        self.selected_report = None
        self.applied_report = None

    def _report(self, report, site):
        report = {"scenario_id": self.spec.scenario_id if self.spec else None, **report, "site": site}
        self.last_report = detached(report)
        if not self.spec or site == self.spec.site:
            self.selected_report = self.last_report
        if report.get("applied"):
            self.applied_report = self.last_report
        return report

    def finish(self, enabled):
        if not enabled or self.injector is None:
            reason = "disabled" if not enabled else "no_injector"
        else:
            reason = "applied" if self.used else (self.selected_report or {}).get("reason", "hook_not_reached")
        identity = self.applied_report or self.selected_report or {}
        return {"scenario_id": self.spec.scenario_id if self.spec else identity.get("scenario_id"),
                "site": self.spec.site if self.spec else identity.get("site"),
                "enabled": enabled, "applied": self.used, "reason": reason}

    def __call__(self, content, site):
        if self.spec and site != self.spec.site:
            return self._report({"triggered": False, "applied": False, "reason": "not_selected_boundary"}, site)
        if self.used or self.injector is None:
            return self._report({"triggered": False, "applied": False,
                                 "reason": "already_applied" if self.used else "no_injector"}, site)
        report = detached(self.injector(detached(content), site))
        if not isinstance(report, dict):
            raise WorkflowError("C must return an injection report object")
        applied = report.get("applied", report.get("triggered"))
        if type(applied) is not bool or ("triggered" in report and
                (type(report["triggered"]) is not bool or report["triggered"] != applied)):
            raise WorkflowError("C must supply consistent applied/triggered booleans")
        if self.spec and report.get("scenario_id", self.spec.scenario_id) != self.spec.scenario_id:
            raise WorkflowError("C report scenario_id differs from the declared trial")
        if "scenario_id" in report and (not isinstance(report["scenario_id"], str) or not report["scenario_id"].strip()):
            raise WorkflowError("C scenario_id must be a nonempty string")
        if "reason" in report and (not isinstance(report["reason"], str) or not report["reason"]):
            raise WorkflowError("C reason must be a nonempty string")
        if "metadata" in report and not isinstance(report["metadata"], dict):
            raise WorkflowError("C metadata must be an object")
        metadata = {k: report[k] for k in ("scenario_id", "metadata") if k in report}
        if not applied:
            return self._report({**metadata, "triggered": False, "applied": False,
                                 "reason": report.get("reason", "not_applied")}, site)
        if not isinstance(report.get("fault_type"), str) or not report["fault_type"]:
            raise WorkflowError("A triggered C report requires fault_type")
        replacement = validate_content(site, report.get("replacement"))
        self.used = True
        return self._report({**metadata, "triggered": True, "applied": True, "reason": "applied",
            "replacement": replacement, "fault_type": report["fault_type"]}, site)
