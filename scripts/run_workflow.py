"""Run Part B's real local-model workflow through A and D; new output directory required."""

import argparse
import importlib
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from experiments.phase1 import run_phase1
from workflow import BankingWorkflowSession, ModelConfig, OpenAICompatibleClient, WorkflowConfig
from workflow.integration import BankingEnvironmentAdapter, workflow_report
from workflow.contracts import InjectionSpec
from workflow.evidence import clean_reference


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task", choices=["user_task_0", "user_task_2"], default="user_task_0")
    parser.add_argument("--condition", choices=["clean", "fault", "full_restart", "always_verify"], default="clean")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model", default="qwen2.5:7b")
    parser.add_argument("--base-url", default="http://127.0.0.1:11434/v1")
    parser.add_argument("--api-key-env", default="OPENAI_COMPATIBLE_API_KEY")
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--plan-format", choices=["json_schema", "json_object", "prompt_only"], default="json_schema")
    parser.add_argument("--max-tokens", type=int, default=1536)
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument("--max-turns", type=int, default=8)
    parser.add_argument("--max-model-calls", type=int, default=48)
    parser.add_argument("--max-tool-calls", type=int, default=24)
    parser.add_argument("--request-retries", type=int, choices=[0, 1], default=0)
    parser.add_argument("--injector", help="C plugin: importable.module:factory; factory(task_id=...) returns injector")
    parser.add_argument("--scenario-id", help="Controller-only C scenario; also passed to the injector factory")
    parser.add_argument("--injection-site", choices=["research", "plan"], help="Selected replacement boundary")
    parser.add_argument("--matched-clean", type=Path, help="Existing clean B run of the same task, for C comparison")
    args = parser.parse_args()
    if args.condition in {"fault", "full_restart"} and not args.injector:
        parser.error("fault/full_restart requires --injector; no default hidden fault")
    if args.condition == "clean" and args.injector:
        parser.error("clean does not use an injector")
    if bool(args.scenario_id) != bool(args.injection_site) or (args.scenario_id and not args.injector):
        parser.error("--scenario-id and --injection-site require each other and --injector")
    spec = InjectionSpec(args.scenario_id, args.injection_site) if args.scenario_id else None
    if args.matched_clean:
        clean_reference(args.matched_clean, args.task)
    injector = None
    if args.injector:
        module, separator, factory = args.injector.partition(":")
        if not separator or not module or not factory:
            parser.error("--injector must be module:factory")
        factory_args = {"task_id": args.task, **({"scenario_id": args.scenario_id} if spec else {})}
        injector = getattr(importlib.import_module(module), factory)(**factory_args)
        if not callable(injector):
            parser.error("injector factory must return a callable")
    model = ModelConfig(base_url=args.base_url, model=args.model, temperature=args.temperature,
                        max_tokens=args.max_tokens, timeout_seconds=args.timeout, plan_format=args.plan_format)
    config = WorkflowConfig(max_turns_per_stage=args.max_turns, max_model_calls=args.max_model_calls,
                            max_tool_calls=args.max_tool_calls, request_retries=args.request_retries)
    client = OpenAICompatibleClient(model, api_key=os.environ.get(args.api_key_env, "ollama"))
    result = run_phase1(args.output, task_id=args.task, condition=args.condition,
        environment_adapter=BankingEnvironmentAdapter(args.task),
        create_session=lambda: BankingWorkflowSession(client, config, injection_spec=spec), injector=injector, synthetic=False)
    report = workflow_report(args.output, matched_clean=args.matched_clean)
    # Plugin identity/configuration is runner metadata, never sent to the model.
    (args.output / "b_config.json").write_text(json.dumps({"model": model.public_dict(),
        "injector": args.injector, "scenario_id": args.scenario_id, "injection_site": args.injection_site,
        "matched_clean": str(args.matched_clean) if args.matched_clean else None,
        "task": args.task, "condition": args.condition}, indent=2) + "\n")
    print(json.dumps({"evaluation": result, "workflow": report}, indent=2))
    return 0 if report["final_workflow_completed"] and result["final_strict_success"] is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
