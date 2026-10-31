# Propagation trace analysis (provisional)

Flags are evidence-based observations, NOT experimentally established causal effects.
`?` means unavailable evidence, NOT false. Unapplied injections are excluded from propagation outcomes.

| Scenario | Attempt | Applied | Exposed | Adopted | Attempted | Executed | State diff | Assessment |
|---|---|---|---|---|---|---|---|---|
| t0_pe_accidental_amount | attempt_0 | True | True | False | False | False | False | exposed_not_adopted_candidate |

## Case evidence

### t0_pe_accidental_amount / attempt_0 (fault)

- Source: `D:\下载\multiagentic-failure-propagation-main (1)\multiagentic-failure-propagation-main\runs\c-injection-demo\20261031T001047Z-652431\t0_pe_accidental_amount-fault`
- Payload valid: `True`; expected single payload mutation
- Changed paths: `[{"site": "plan", "changed_paths": ["actions[0].args.amount"], "event_id": "event_5aebb0c3929044febc279bbf1805dd21"}]`
- Verifier: `[]`
- A strict success: `False`; scorer conflict: `False`
- Evidence IDs (events / A audit calls): `["event_5aebb0c3929044febc279bbf1805dd21", "event_5400126f7bc2403d8e6a802be36897da"]`
- Suggested outcome: **exposed_not_adopted_candidate** (requires review)

## Interpretation safeguards

- R→P adoption uses only actual Planner output; P→E adoption uses actual Executor tool-call output.
- A tool attempt is not an executed operation; execution is not automatically a state change.
- Verifier exposure alone is not downstream exposure; rejection is not automatically correct.
- Audit history matters even when a full restart later finishes successfully.
- No automatic final causal label is assigned; compare a matched clean run and manually review.
