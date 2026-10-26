"""fixed, single-output agent handoff fault injection.

This module intentionally does NOT call an LLM, tool, verifier, or evaluator.
B supplies the actual handoff message and a location within its schema.
Only the selected Researcher->Planner OR Planner->Executor output is replaced.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
import re
from typing import Any, Literal, Mapping, Sequence

import yaml

Boundary = Literal["researcher_to_planner", "planner_to_executor"]
Phase = Literal["fault", "clean", "restart_rerun"]
PathPart = str | int

# A is the only source of the *original* values.  Fault YAML contains only
# selectors and injected values.  Use a repository-relative path so running
# tests or demos from a different working directory does not change behavior.
DEFAULT_TASK_CONFIG_PATH = Path(__file__).resolve().parents[1] / "tasks" / "banking_tasks.yaml"


def load_banking_task_facts(path: str | Path | None = None) -> dict[str, dict[str, Any]]:
    """Load canonical critical facts from Part A's Banking task config.

    Indexes facts by task ID and fact name, and rejects missing,
    malformed, or duplicate entries. Original values are never
    obtained from Part C's payload configuration.
    """
    source = Path(path) if path is not None else DEFAULT_TASK_CONFIG_PATH
    with source.open("r", encoding="utf-8") as stream:
        config = yaml.safe_load(stream)
    if not isinstance(config, dict):
        raise ValueError(f"invalid Banking task configuration: {source}")

    result: dict[str, dict[str, Any]] = {}
    for task_id in ("user_task_0", "user_task_2"):
        task = config.get(task_id)
        if not isinstance(task, dict) or task.get("task_id") != task_id:
            raise ValueError(f"missing/mismatched Banking task {task_id}: {source}")
        if task.get("suite") != "banking" or task.get("benchmark_version") != "v1.2.2":
            raise ValueError(f"wrong Banking suite/version for {task_id}: {source}")
        rows = task.get("critical_facts")
        if not isinstance(rows, list):
            raise ValueError(f"missing critical_facts for {task_id}: {source}")
        values: dict[str, Any] = {}
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("name"), str) or "value" not in row:
                raise ValueError(f"malformed critical fact in {task_id}: {source}")
            name = row["name"]
            if name in values:
                raise ValueError(f"duplicate critical fact {task_id}.{name}: {source}")
            values[name] = row["value"]
        result[task_id] = values
    return result


@dataclass
class InjectionAttempt:
    """Track injection state within a single workflow attempt.

    Ensures that at most one agent handoff is successfully replaced,
    including when verifier retries occur within the same attempt.
    """

    used: bool = False
    applied_scenario_id: str | None = None

"""Store the original and resulting handoff with injection metadata.

    Separates whether a mutation was applied from whether the fault
    was subsequently exposed to or adopted by downstream agents.
"""
@dataclass(frozen=True)
class InjectionResult:
    original_output: Any
    output: Any
    scenario_id: str
    task_id: str
    boundary: str
    family: str
    operation: str
    applied: bool
    reason: str

    def metadata(self) -> dict[str, Any]:
        """Return serializable injection metadata for workflow tracing."""
        return {
            "scenario_id": self.scenario_id,
            "task_id": self.task_id,
            "boundary": self.boundary,
            "family": self.family,
            "operation": self.operation,
            "applied": self.applied,
            "reason": self.reason,
        }

def _as_decimal(value: Any) -> Decimal | None:
    """Convert finite numeric values to Decimal for safe comparison.

    Rejects booleans, unsupported types, and non-finite numbers.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        return None
    try:
        result = Decimal(str(value).strip())
        return result if result.is_finite() else None
    except InvalidOperation:
        return None

def _equal_expected(actual: Any, expected: Any) -> bool:
    """Check numerical equivalence against the canonical original value.

    Prevents corruption when the observed value does not match
    the expected source fact.
    """
    a, b = _as_decimal(actual), _as_decimal(expected)
    return a is not None and b is not None and a == b

def _replacement_value(original: Any, new_value: Any) -> Any:
    """Create a replacement value while preserving numeric text format.

    Keeps string-based numeric fields textual and retains appropriate
    decimal precision when applying the injected value.
    """
    if isinstance(original, str):
        s = original.strip()
        if not re.fullmatch(r"[+-]?\d+(?:\.\d+)?", s):
            raise ValueError("target is not a simple numeric string")
        new = _as_decimal(new_value)
        if new is None:
            raise ValueError("injected value is not numeric")
        old_dp = len(s.partition(".")[2]) if "." in s else 0
        new_dp = max(-new.as_tuple().exponent, 0)
        if old_dp == 0 and new_dp == 0:
            return f"{new:.0f}"
        return f"{new:.{max(old_dp, new_dp)}f}"
    return new_value

def _lookup(root: Any, path: Sequence[PathPart]) -> Any:
    """Retrieve a value from a nested handoff using an explicit path.

    Supports dictionary keys and list indices without modifying
    the original message.
    """
    value = root
    for part in path:
        if isinstance(value, dict) and isinstance(part, str):
            value = value[part]
        elif isinstance(value, list) and isinstance(part, int):
            value = value[part]
        else:
            raise (TypeError("path does not match the message schema"))
    return value

def _set_at_path(root: Any, path: Sequence[PathPart], value: Any) -> None:
    """Replace the value at an existing nested handoff path.

    Modifies only the selected field and rejects paths that do
    not match the expected message structure.
    """
    parent = _lookup(root, path[:-1])
    key = path[-1]
    if isinstance(parent, dict) and isinstance(key, str):
        if key not in parent:
            raise KeyError(key)
        parent[key] = value
    elif isinstance(parent, list) and isinstance(key, int):
        parent[key] = value
    else:
        raise TypeError("path does not match the message schema")

def _corrupt_argument(content: dict[str, Any], case: Mapping[str, Any]) -> tuple[dict[str, Any] | None, str]:
    """Corrupt one numeric argument in a uniquely matched Planner action.

    Validates the action selector and canonical original value before
    replacing the target argument. All other actions and fields remain
    unchanged, and missing or ambiguous targets fail closed.
    """
    actions = content.get("actions")
    if not isinstance(actions, list):
        return None, "invalid_action_schema"
    selector = case["target_action"]
    matches = []
    for action in actions:
        if not isinstance(action, dict) or not isinstance(action.get("args"), dict):
            return None, "invalid_action_schema"
        if (action.get("tool") == selector["tool"]
                and all(key in action["args"] and action["args"][key] == value
                        for key, value in selector["match_args"].items())):
            matches.append(action)
    if len(matches) != 1:
        return None, "missing_or_ambiguous_target"
    args = matches[0]["args"]
    field = selector["field"]
    if field not in args:
        return None, "target_missing"
    if not _equal_expected(args[field], case["expected_original"]):
        return None, "unexpected_original_value"
    args[field] = _replacement_value(args[field], case["injected_value"])
    return content, "applied"

def _append_action(content: dict[str, Any], case: Mapping[str, Any]) -> tuple[dict[str, Any] | None, str]:
    """Append one configured adversarial action to the Planner output.

    Preserves all existing actions, validates the action schema,
    and prevents duplicate insertion of the injected action.
    """
    actions = content.get("actions")
    if not isinstance(actions, list) or any(
        not isinstance(a, dict) or set(a) != {"tool", "args"}
        or not isinstance(a["tool"], str) or not isinstance(a["args"], dict)
        for a in actions
    ):
        return None, "invalid_action_schema"
    action = deepcopy(case["action"])
    if action in actions:
        return None, "action_already_present"
    actions.append(action)
    return content, "applied"

class FaultInjector:
    """Loads fixed cases and applies one targeted mutation to one handoff output."""

    def __init__(self, config: Mapping[str, Any], *, task_config_path: str | Path | None = None):
        """Validate and resolve the Banking fault scenarios.

        Loads canonical original values and action selectors from Part A,
        validates payload constraints, and prepares resolved cases for
        controlled injection without duplicating ground truth in C's YAML.
        """
        if config.get("version") != 1:
            raise ValueError("unsupported payload configuration version")
        constraints = config.get("constraints") or {}
        if constraints.get("max_replaced_agent_outputs_per_attempt") != 1:
            raise ValueError("configuration must permit exactly one replacement per attempt")
        self.boundaries = set(constraints.get("permitted_boundaries", []))
        if self.boundaries != {"researcher_to_planner", "planner_to_executor"}:
            raise ValueError("unexpected permitted boundaries")
        # Resolve ALL original fact values from A, never from C's YAML.
        task_facts = load_banking_task_facts(task_config_path)
        self.cases: dict[str, dict[str, Any]] = {}
        combinations: set[tuple[str, str, str, str]] = set()
        for item in config.get("payloads", []):
            case = deepcopy(item)
            case_id = case.get("id")
            family, operation = case.get("family"), case.get("operation")
            if not isinstance(case_id, str) or case_id in self.cases:
                raise ValueError(f"duplicate or invalid scenario id: {case_id}")
            expected_boundary = {
                "corrupt_fact": "researcher_to_planner",
                "corrupt_argument": "planner_to_executor",
                "append_instruction": "researcher_to_planner",
                "append_action": "planner_to_executor",
                "append_plan_note": "planner_to_executor",
            }
            valid_pairs = {
                ("accidental", "corrupt_fact"),
                ("accidental", "corrupt_argument"),
                ("adversarial", "append_instruction"),
                ("adversarial", "append_action"),
                ("adversarial", "append_plan_note"),
            }
            if (family, operation) not in valid_pairs:
                raise ValueError(f"invalid fault family/operation for {case_id}")
            if case.get("boundary") != expected_boundary[operation]:
                raise ValueError(f"invalid operation/boundary for {case_id}")
            if "expected_original" in case:
                raise ValueError(f"{case_id}: remove expected_original from C payloads; use A critical_facts")
            if operation in {"corrupt_fact", "corrupt_argument"}:
                if not all(k in case for k in ("target_fact", "injected_value")):
                    raise ValueError(f"missing corruption fields: {case_id}")
                task_id, fact_name = case["task_id"], case["target_fact"]
                if fact_name not in task_facts.get(task_id, {}):
                    raise ValueError(f"unknown A critical fact {task_id}.{fact_name}: {case_id}")
                # Compatibility for existing apply() and Analysis: this key
                # exists only in the RESOLVED in-memory case, not in payloads.yaml.
                case["expected_original"] = task_facts[task_id][fact_name]
                if (_as_decimal(case["expected_original"]) is None
                        or _as_decimal(case["injected_value"]) is None):
                    raise ValueError(f"corruption values must be numeric: {case_id}")
            if operation == "corrupt_argument":
                selector = case.get("target_action")
                if isinstance(selector, dict) and "match_facts" in selector:
                    if "match_args" in selector:
                        raise ValueError(f"{case_id}: use match_facts, not duplicate match_args")
                    names = selector.pop("match_facts")
                    if not isinstance(names, dict) or not names:
                        raise ValueError(f"invalid match_facts: {case_id}")
                    args = {}
                    for arg_name, fact_name in names.items():
                        if (not isinstance(arg_name, str) or
                                fact_name not in task_facts.get(case["task_id"], {})):
                            raise ValueError(f"unknown match fact {case['task_id']}.{fact_name}: {case_id}")
                        args[arg_name] = task_facts[case["task_id"]][fact_name]
                    selector["match_args"] = args
                if (not isinstance(selector, dict)
                        or not isinstance(selector.get("tool"), str)
                        or not selector["tool"]
                        or not isinstance(selector.get("match_args"), dict)
                        or not selector["match_args"]
                        or not isinstance(selector.get("field"), str)
                        or not selector["field"]):
                    raise ValueError(f"invalid target_action: {case_id}")
            if operation in {"append_instruction", "append_plan_note"}:
                if not str(case.get("instruction", "")).strip():
                    raise ValueError(f"missing instruction: {case_id}")
            if operation == "append_plan_note" and case.get("target_path") != "notes":
                raise ValueError(f"invalid note target path: {case_id}")
            if operation == "append_action":
                action = case.get("action")
                if (not isinstance(action, dict)
                        or set(action) != {"tool", "args"}
                        or not isinstance(action["tool"], str)
                        or not action["tool"]
                        or not isinstance(action["args"], dict)):
                    raise ValueError(f"invalid appended action: {case_id}")
            combination = (case.get("task_id"), family, case.get("boundary"), operation)
            if combination in combinations:
                raise ValueError(f"duplicate task/family/boundary/operation combination: {combination}")
            combinations.add(combination)
            self.cases[case_id] = case
        if len(self.cases) != 10 or len(combinations) != 10:
            raise ValueError("Expected 8 core scenarios and 2 supplementary note scenarios")
        if {x[0] for x in combinations} != {"user_task_0", "user_task_2"}:
            raise ValueError("Phase 1 expects user_task_0 and user_task_2")
        note_tasks = {case["task_id"] for case in self.cases.values()
                      if case["operation"] == "append_plan_note"}
        if note_tasks != {"user_task_0", "user_task_2"}:
            raise ValueError("Expected one supplementary P->E note scenario per task")

    @classmethod
    def from_yaml(
        cls, path: str | Path | None = None, *, task_config_path: str | Path | None = None
    ) -> "FaultInjector":
        """Construct a FaultInjector from the configured payload YAML.

        Loads the fault definitions and delegates scenario validation
        and canonical fact resolution to the initializer.
        """
        source = Path(path) if path is not None else Path(__file__).with_name("payloads.yaml")
        with source.open("r", encoding="utf-8") as stream:
            config = yaml.safe_load(stream)
        if not isinstance(config, dict):
            raise ValueError("payload YAML must contain a mapping")
        return cls(config, task_config_path=task_config_path)

    def apply(
        self,
        original_output: str | dict[str, Any],
        *,
        scenario_id: str,
        task_id: str,
        boundary: Boundary,
        attempt: InjectionAttempt,
        path: Sequence[PathPart] | None = None,
        value_pattern: str | None = None,
        phase: Phase = "fault",
    ) -> InjectionResult:
        """Apply one predefined fault to an eligible agent handoff.

        Validates the execution phase, task, boundary, attempt state, and
        target schema before mutating a deep copy of the original output.

        Supports numeric fact corruption, action argument corruption,
        adversarial instruction insertion, and action insertion.

        Returns an InjectionResult without modifying the caller's input.
        Failed or ineligible injections leave the original output unchanged.
        """
        if scenario_id not in self.cases:
            raise KeyError(f"unknown fixed scenario: {scenario_id}")
        if phase not in {"fault", "clean", "restart_rerun"}:
            raise ValueError(f"unsupported phase: {phase}")
        case = self.cases[scenario_id]
        original = deepcopy(original_output)

        def result(output: Any, applied: bool, reason: str) -> InjectionResult:
            return InjectionResult(
                original_output=original,
                output=output,
                scenario_id=scenario_id,
                task_id=task_id,
                boundary=boundary,
                family=case["family"],
                operation=case["operation"],
                applied=applied,
                reason=reason,
            )

        if phase != "fault":
            return result(deepcopy(original), False, "injection_disabled_for_phase")
        if attempt.used:
            return result(deepcopy(original), False, "one_replacement_already_applied")
        if case["task_id"] != task_id or case["boundary"] != boundary:
            return result(deepcopy(original), False, "task_or_boundary_mismatch")
        if not isinstance(original, (str, dict)):
            return result(deepcopy(original), False, "unsupported_message_type")

        mutated = deepcopy(original)
        operation = case["operation"]
        if isinstance(mutated, dict) and path is None and operation in {"corrupt_fact", "append_instruction", "append_plan_note"}:
            configured_path = case.get("target_path")
            if isinstance(configured_path, str) and configured_path:
                path = configured_path.split(".")

        try:
            if operation == "corrupt_fact":
                if isinstance(mutated, str):
                    if path is not None or not value_pattern:
                        return result(deepcopy(original), False, "explicit_text_value_pattern_required")
                    regex = re.compile(value_pattern)
                    if "value" not in regex.groupindex:
                        return result(deepcopy(original), False, "pattern_needs_named_value_group")
                    matches = list(regex.finditer(mutated))
                    if len(matches) != 1:
                        return result(deepcopy(original), False, "missing_or_ambiguous_target")
                    match = matches[0]
                    old_value = match.group("value")
                    if not _equal_expected(old_value, case["expected_original"]):
                        return result(deepcopy(original), False, "unexpected_original_value")
                    replacement = _replacement_value(old_value, case["injected_value"])
                    start, end = match.span("value")
                    mutated = mutated[:start] + replacement + mutated[end:]
                else:
                    if not path or value_pattern is not None:
                        return result(deepcopy(original), False, "explicit_field_path_required")
                    try:
                        old_value = _lookup(mutated, path)
                    except (KeyError, IndexError, TypeError):
                        return result(deepcopy(original), False, "target_missing")
                    if not _equal_expected(old_value, case["expected_original"]):
                        return result(deepcopy(original), False, "unexpected_original_value")
                    _set_at_path(mutated, path, _replacement_value(old_value, case["injected_value"]))

            elif operation == "corrupt_argument":
                if not isinstance(mutated, dict) or path is not None or value_pattern is not None:
                    return result(deepcopy(original), False, "invalid_target_locator")
                replacement, reason = _corrupt_argument(mutated, case)
                if replacement is None:
                    return result(deepcopy(original), False, reason)
                mutated = replacement

            elif operation in {"append_instruction", "append_plan_note"}:
                if operation == "append_plan_note" and (
                        not isinstance(mutated, dict) or not isinstance(mutated.get("actions"), list)):
                    return result(deepcopy(original), False, "invalid_plan_note_schema")
                instruction = case["instruction"]
                if isinstance(mutated, str):
                    if path is not None or value_pattern is not None:
                        return result(deepcopy(original), False, "unexpected_target_locator")
                    if not mutated.strip():
                        return result(deepcopy(original), False, "empty_original_message")
                    if instruction in mutated:
                        return result(deepcopy(original), False, "instruction_already_present")
                    mutated = mutated.rstrip() + "\n\n" + instruction
                else:
                    if not path or value_pattern is not None:
                        return result(deepcopy(original), False, "explicit_text_field_path_required")
                    try:
                        text_field = _lookup(mutated, path)
                    except (KeyError, IndexError, TypeError):
                        return result(deepcopy(original), False, "target_missing")
                    if not isinstance(text_field, str):
                        return result(deepcopy(original), False, "target_not_text")
                    if instruction in text_field:
                        return result(deepcopy(original), False, "instruction_already_present")
                    _set_at_path(mutated, path, (text_field.rstrip() + "\n\n" if text_field.strip() else "") + instruction)

            elif operation == "append_action":
                if not isinstance(mutated, dict) or path is not None or value_pattern is not None:
                    return result(deepcopy(original), False, "invalid_target_locator")
                replacement, reason = _append_action(mutated, case)
                if replacement is None:
                    return result(deepcopy(original), False, reason)
                mutated = replacement
        except (TypeError, ValueError, KeyError, IndexError, re.error):
            return result(deepcopy(original), False, "invalid_target_or_schema")

        attempt.used = True
        attempt.applied_scenario_id = scenario_id
        return result(mutated, True, "applied")

def make_injector(*, task_id: str, scenario_id: str):
    """Create the injection callback used by Part B's workflow.

    Initializes the fixed scenario and its per-attempt injection state.
    The returned callback maps workflow sites to handoff boundaries,
    applies the selected fault, and reports the replacement and metadata.
    """
    injector = FaultInjector.from_yaml()
    if scenario_id not in injector.cases:
        raise KeyError(f"unknown fixed scenario: {scenario_id}")
    attempt = InjectionAttempt()
    site_to_boundary = {
        "research": "researcher_to_planner",
        "plan": "planner_to_executor",
    }
    """Apply the configured fault at a workflow injection site.

    Returns a structured injection report, including the modified
    handoff only when the mutation is successfully applied.
    """
    def inject(content: dict[str, Any], site: str) -> dict[str, Any]:
        if site not in site_to_boundary:
            return {"scenario_id": scenario_id, "applied": False, "reason": "unknown_site"}
        outcome = injector.apply(
            content, scenario_id=scenario_id, task_id=task_id,
            boundary=site_to_boundary[site], attempt=attempt,
        )
        report = {
            "scenario_id": scenario_id,
            "applied": outcome.applied,
            "reason": outcome.reason,
            "metadata": outcome.metadata(),
        }
        if outcome.applied:
            report.update({"fault_type": outcome.family, "replacement": outcome.output})
        return report

    return inject
