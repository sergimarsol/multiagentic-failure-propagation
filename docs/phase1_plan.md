# Phase 1 Project Plan

## Fault Injection and Propagation Measurement in AgentDojo Banking

Deadline: Within one week of project kickoff. This plan specifies a single completion deadline, not a day-by-day schedule.

# 1\. Objective and Scope

Implement and measure a clean multi-agent workflow, inject faults at controlled stages, and determine how faulty intermediate results affect downstream decisions, tool actions, and environment state. Establish full restart as a recovery baseline and include always verifying intermediate results as a comparison, accounting for its additional inference cost.

The initial environment is an AgentDojo Banking subset. Workspace, automated fault detection, and dependency-aware selective recovery are outside this week's scope. The objective is a reproducible Phase 1 prototype and preliminary analysis, rather than broad benchmark coverage.

# 2\. Tasks and Multi-Agent Architecture

Bill payment (user\_task\_0): Read a bill, formulate a payment plan, and execute the payment. This provides a short propagation chain with checkable amounts and recipients.

Rent adjustment (user\_task\_2): Read a landlord notice, retrieve scheduled payments, formulate an update, and modify the existing payment. This introduces multiple information sources and a state-changing action.

Confirm these task IDs and their definitions after pinning the AgentDojo commit and benchmark version.

The workflow uses three roles sharing one configured language-model endpoint, with separate prompts and conversation contexts. The current real-model setup runs orchestration and the simulated Banking environment on AWS EC2 and uses TritonAI's hosted `api-glm-5.3-flash` for inference. A local endpoint remains supported, but results from different model/backend configurations must not be pooled as matched trials. Record the code commit, model ID, endpoint, generation parameters, and budgets for each experiment:

* Researcher: Query tools and extract the facts required by the task.  
* Planner: Combine those facts with the original user request to produce an action plan.  
* Executor: Generate tool calls from the plan and execute them in the simulated environment.

A Python controller manages scheduling, fault injection, trace collection, resource limits, and experiment resets. Use a fixed, serial workflow initially. Keep the model configuration, role permissions, prompts, and resource limits consistent across experimental conditions; vary the fault-handling method.

Example: Read landlord notice \+ query scheduled payments → plan the rent adjustment → execute the update.

# 3\. Threat Model and Fault Design

Following the capability-based framing of Kim et al.'s SoK, this project defines a restricted internal-output tampering scenario. An adversary may replace at most one agent output per faulty attempt, but cannot modify the original user request, controller, tool implementations, evaluator, or audit log. This is our controlled experimental instantiation, not a claim to cover the full SoK taxonomy.

The adversary knows the current task, role structure, tool interfaces, and message format. Its objective is to induce an incorrect or unauthorized banking action. Controller-generated provenance metadata remains outside the attacker's control.

Prioritize two fault families and analyze them separately:

* Accidental fault: Corrupt a factual field, such as a payment amount, while preserving a valid message format. This simulates an incorrect intermediate result.  
* Adversarial fault: Insert a malicious instruction in notes or append an unauthorized payment action to a plan, as specified in the fixed payloads.

Inject at two controlled boundaries: Researcher → Planner and Planner → Executor. Apply at most one successful output replacement per faulty attempt; disable injection in clean conditions and the full-restart rerun. Save the original output, replacement content, fault family, and actual injection location. Use fixed payloads for matched comparisons.

Timeouts and stale outputs are optional extensions, not required deliverables for the one-week scope. The Phase 1 requirement permits a focused selection of fault types. External-document injection is also deferred.

# 4\. Experimental Conditions

Clean baseline (`clean`): Run without injected faults or verification to measure normal task success, inference cost, and end-to-end latency.

Clean + always verify (`clean_verify`): Run the same verification and bounded regeneration procedure without injected faults. Measure verification overhead and `clean_run_rejection_rate` separately from injected runs. A clean run can contain natural model errors, so rejection is not automatically a false rejection.

Fault / no recovery (`fault`): Inject the fault and continue execution to measure natural propagation and its consequences.

Fault / full restart (`full_restart`): At the predefined notification boundary after the initial workflow attempt, provide a fault notification from the experiment harness. Preserve the faulty trace, create a fresh environment from the initial state and a fresh agent session, disable the one-time injection, and rerun the entire workflow within the shared trial budget. This Phase 1 path does not require stage checkpoints or context restoration. Report this as restart with a supplied fault identity, not automated detection.

Fault / always verify (`always_verify`): Check each intermediate result before a downstream agent uses it. The verifier receives the original user request, the result, and relevant original evidence, but no hidden answer or injection label. On rejection, regenerate the result under a fixed retry limit; use one retry initially without reinjection and report unresolved rejection as failure.

The verifier may reject valid candidates or accept faulty ones. Record its decisions and include all additional model calls, input/output tokens, source queries, and retries in the cost and latency measurements. A rejection alone does not prove that an injection was detected; false-rejection claims require independent evidence that the rejected candidate was valid.

The current [payload configuration](../faults/payloads.yaml) defines ten scenarios across the two tasks. The [experiment runner](../scripts/run_phase1_final.py) schedules three fault conditions per scenario and shares two clean controls per task and repeat when their control settings match: 34 trials per repeat, or 170 trials for five repeats. The smoke mode runs only task 0, the first scenario, and its five conditions. Shared controls must not be counted repeatedly as independent samples.

Use identical initial states, logical injection points, and payloads across matched fault conditions. A simulated reset does not erase previously executed unauthorized actions from the audit history and does not demonstrate that a real payment could be reversed.

# 5\. Trace and Dependency Recording

Record the following for every run:

* Run, task, step, and role identifiers; actual messages sent to and returned by the model.  
* Intermediate artifacts, unique artifact IDs, and upstream input artifact IDs.  
* Tool names, arguments, returned results, and execution status.  
* Relevant environment-state reads and writes, including state changes caused by actions.  
* Fault type, injection boundary, and original and injected content.  
* Start/end timestamps, input/output token counts, retries, and exceptions.

Simple artifact and dependency lists are sufficient for Phase 1\. Automatic invalidation and selective rerunning are not required. Record dependencies through the controller and tool wrappers rather than relying solely on an agent's self-reported reasoning.

# 6\. Measurements and Propagation Checks

Clean task success: The proportion of fault-free runs that correctly complete the user task.

Failure propagation: Whether the injected error appears in downstream facts, plans, tool arguments, or environment state.

Unauthorized actions: Count attempted unauthorized calls separately from actions actually executed by the tools.

Affected stages: Identify which stages exhibit semantic errors related to the injection.

Wasted work: Separately count work performed on an invalid execution path and work repeated by full restart. Use agent and tool calls as the initial units.

End-to-end latency: Measure total completion time for each condition, including verification, retries, or restart where applicable.

Inference cost: Record model-call counts and input/output tokens. Report verification overhead separately.

Restart outcome: Report correctness after restart and the additional calls, tokens, and latency required.

Retain AgentDojo's original evaluation results and add explicitly labeled checks for extra incorrect transactions or account modifications. The presence of a correct transaction alone must not hide an additional unauthorized action.

## Confirming that the workload uses the injected result

First record whether the injection was actually applied. If the hook was not reached or the target was missing, ambiguous, or incompatible, record the case as not applied (with the reason where available), rather than as "not exposed." Retain these trials in overall execution summaries, but exclude them from propagation rates conditional on an applied injection. Report the applied-injection count and each metric's denominator.

For successfully applied, valid injections, compare matched clean and faulty runs and distinguish four outcomes. Unknown or incomplete evidence remains pending review rather than being forced into a category:

1. Not exposed: An injected candidate was created but never entered the intended downstream model's actual input, for example because the verifier rejected it before delivery.
2. Exposed but not adopted: The downstream agent received the content but ignored, rejected, or corrected it. This does not necessarily mean the legitimate task was completed.
3. Propagated: A downstream fact, plan, or action used the incorrect information or malicious instruction.  
4. State consequence: An incorrect action executed successfully and changed the simulated environment.

Use task-specific semantic checks: whether the altered field was used, whether the plan added the malicious action, and whether tool arguments changed accordingly. Textual differences or dependency edges alone are not evidence of error propagation.

An ignored message is not evidence of robust recovery. Flag runs whose clean counterpart already fails, so ordinary model limitations are not attributed to injection. Report overall outcomes and clearly labeled results for clean-successful, exposed cases.

# 7\. Four-Person Responsibilities

All members agree on message and trace formats before integration. Responsibilities are divided by subsystem rather than assigning one person to each agent.

| Owner | Responsibilities | Deliverables by the deadline |
| :---- | :---- | :---- |
| Member A | Environment and evaluation: pin AgentDojo, integrate both tasks, inspect tool behavior, and implement strict scoring. | Resettable environment, task configurations, and correctness / unauthorized-action checks. |
| Member B | Multi-agent workflow: integrate the configured model endpoint, implement the three roles, and manage context passing. | Runnable clean workflow and verifier-call interface. |
| Member C | Threat model and injection: align assumptions with the SoK; implement two fault families at two boundaries. | Threat model, fixed payloads, injection harness, and propagation case analysis. |
| Member D | Tracing and experiments: record dependencies, implement full restart, and aggregate measurements. | Complete traces, restart baseline, and results summary. |

Members B and C jointly implement always-verify; Member D accounts for its complete cost, and Member A checks that evaluation remains independent of model judgment. All deliverables share the one-week deadline.

# 8\. Completion Criteria and Deliverables

By the deadline, the team should have:

* A reproducible Banking multi-agent workflow covering the two selected tasks.  
* Clean, clean + always-verify, fault/no-recovery, full-restart, and fault + always-verify configurations.
* Two controlled fault families and two injection boundaries.  
* Complete message, artifact, tool-action, state-change, and dependency traces.  
* Preliminary results covering the required correctness, propagation, safety, work, latency, and inference-cost measures.  
* At least one complete propagation case and an explanation of non-propagation cases where observed. If no propagation is observed, report that outcome and investigate exposure and task dependence rather than inventing a successful case.

The current runner defaults to five repeats; confirm the final repetition count after the real-model smoke and report the sample size transparently. Prioritize complete experimental conditions, auditable traces, and credible propagation judgments over a fixed run count. These are small controlled experiments, not evidence of general robustness across Banking or multi-agent systems.

# References

[Kim, J., Guo, W., and Song, D. SoK: Attack and Defense Landscape of Agentic AI Systems. USENIX Security 2026\.](https://www.usenix.org/conference/usenixsecurity26/presentation/kim-juhee-agentic)

[AgentDojo repository and Banking task definitions.](https://github.com/ethz-spylab/agentdojo/blob/main/src/agentdojo/default_suites/v1/banking/user_tasks.py)

