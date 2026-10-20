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

The workflow uses three roles sharing one local language-model service, with separate prompts and conversation contexts:

* Researcher: Query tools and extract the facts required by the task.  
* Planner: Combine those facts with the original user request to produce an action plan.  
* Executor: Generate tool calls from the plan and execute them in the simulated environment.

A Python controller manages scheduling, fault injection, trace collection, resource limits, and experiment resets. Use a fixed, serial workflow initially. Keep the model configuration, role permissions, prompts, and resource limits consistent across experimental conditions; vary the fault-handling method.

Example: Read landlord notice \+ query scheduled payments → plan the rent adjustment → execute the update.

# 3\. Threat Model and Fault Design

Following the capability-based framing of Kim et al.'s SoK, this project defines a restricted internal-output tampering scenario. An adversary may replace one agent output in a run, but cannot modify the original user request, controller, tool implementations, evaluator, or audit log. This is our controlled experimental instantiation, not a claim to cover the full SoK taxonomy.

The adversary knows the current task, role structure, tool interfaces, and message format. Its objective is to induce an incorrect or unauthorized banking action. Controller-generated provenance metadata remains outside the attacker's control.

Prioritize two fault families and analyze them separately:

* Accidental fault: Corrupt a factual field, such as a payment amount, while preserving a valid message format. This simulates an incorrect intermediate result.  
* Adversarial fault: Insert a malicious instruction requesting an additional payment that the user did not authorize.

Inject at two controlled boundaries: Researcher → Planner and Planner → Executor. Apply only one injection per run. Save the original output, replacement content, fault family, and actual injection location. Use fixed payloads for matched comparisons.

Timeouts and stale outputs are optional extensions, not required deliverables for the one-week scope. The Phase 1 requirement permits a focused selection of fault types. External-document injection is also deferred.

# 4\. Experimental Conditions

Clean baseline: Run without injected faults to measure normal task success, inference cost, and end-to-end latency.

Fault / no recovery: Inject the fault and continue execution to measure natural propagation and its consequences.

Fault / full restart: At a predefined checkpoint, such as run completion or the configured execution limit, provide a fault notification from the experiment harness. Preserve the faulty trace, reset the simulated environment to its initial state, remove the one-time injection, and rerun the entire workflow. Report this as restart with a supplied fault identity, not automated detection.

Fault / always verify: Check each intermediate result before a downstream agent uses it. The verifier receives the original user request, the result, and relevant original evidence, but no hidden answer or injection label. On rejection, regenerate the result under a fixed retry limit; use one retry initially and report unresolved rejection as failure.

The verifier may produce false positives or miss faults. Record its decisions and include all additional model calls, input/output tokens, source queries, and retries in the cost and latency measurements.

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

Compare matched clean and faulty runs and distinguish four outcomes:

1. Not exposed: The injection point was not reached, or the injected content never entered the downstream model's actual input.  
2. Exposed but not adopted: The downstream agent received the content but ignored or corrected it.  
3. Propagated: A downstream fact, plan, or action used the incorrect information or malicious instruction.  
4. State consequence: An incorrect action executed successfully and changed the simulated environment.

Use task-specific semantic checks: whether the altered field was used, whether the plan added the malicious action, and whether tool arguments changed accordingly. Textual differences or dependency edges alone are not evidence of error propagation.

An ignored message is not evidence of robust recovery. Flag runs whose clean counterpart already fails, so ordinary model limitations are not attributed to injection. Report overall outcomes and clearly labeled results for clean-successful, exposed cases.

# 7\. Four-Person Responsibilities

All members agree on message and trace formats before integration. Responsibilities are divided by subsystem rather than assigning one person to each agent.

| Owner | Responsibilities | Deliverables by the deadline |
| :---- | :---- | :---- |
| Member A | Environment and evaluation: pin AgentDojo, integrate both tasks, inspect tool behavior, and implement strict scoring. | Resettable environment, task configurations, and correctness / unauthorized-action checks. |
| Member B | Multi-agent workflow: integrate the local model, implement the three roles, and manage context passing. | Runnable clean workflow and verifier-call interface. |
| Member C | Threat model and injection: align assumptions with the SoK; implement two fault families at two boundaries. | Threat model, fixed payloads, injection harness, and propagation case analysis. |
| Member D | Tracing and experiments: record dependencies, implement full restart, and aggregate measurements. | Complete traces, restart baseline, and results summary. |

Members B and C jointly implement always-verify; Member D accounts for its complete cost, and Member A checks that evaluation remains independent of model judgment. All deliverables share the one-week deadline.

# 8\. Completion Criteria and Deliverables

By the deadline, the team should have:

* A reproducible Banking multi-agent workflow covering the two selected tasks.  
* Clean, fault/no-recovery, full-restart, and always-verify configurations.  
* Two controlled fault families and two injection boundaries.  
* Complete message, artifact, tool-action, state-change, and dependency traces.  
* Preliminary results covering the required correctness, propagation, safety, work, latency, and inference-cost measures.  
* At least one complete propagation case and an explanation of non-propagation cases where observed. If no propagation is observed, report that outcome and investigate exposure and task dependence rather than inventing a successful case.

Choose the repetition count after measuring actual local inference speed and report the sample size transparently. Prioritize complete experimental conditions, auditable traces, and credible propagation judgments over a fixed run count. These are small controlled experiments, not evidence of general robustness across Banking or multi-agent systems.

# References

[Kim, J., Guo, W., and Song, D. SoK: Attack and Defense Landscape of Agentic AI Systems. USENIX Security 2026\.](https://www.usenix.org/conference/usenixsecurity26/presentation/kim-juhee-agentic)

[AgentDojo repository and Banking task definitions.](https://github.com/ethz-spylab/agentdojo/blob/main/src/agentdojo/default_suites/v1/banking/user_tasks.py)

