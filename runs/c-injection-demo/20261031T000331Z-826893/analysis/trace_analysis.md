# Propagation trace analysis (provisional)

Flags are evidence-based observations, NOT experimentally established causal effects.
`?` means unavailable evidence, NOT false. Unapplied injections are excluded from propagation outcomes.

| Scenario | Attempt | Applied | Exposed | Adopted | Attempted | Executed | State diff | Assessment |
|---|---|---|---|---|---|---|---|---|
| t0_rp_accidental_amount | attempt_0 | True | True | False | False | False | False | exposed_not_adopted_candidate |

## Case evidence

### t0_rp_accidental_amount / attempt_0 (fault)

- Source: `D:\下载\multiagentic-failure-propagation-main (1)\multiagentic-failure-propagation-main\runs\c-injection-demo\20261031T000331Z-826893\t0_rp_accidental_amount-fault`
- Payload valid: `True`; expected single payload mutation
- Changed paths: `[{"site": "research", "changed_paths": ["facts.bill_amount"], "event_id": "event_fe75005b8f3445b88ee554415ddcdd7d"}]`
- Verifier: `[]`
- A strict success: `False`; scorer conflict: `False`
- Evidence IDs (events / A audit calls): `["event_fe75005b8f3445b88ee554415ddcdd7d", "event_85c47a3988e34ea888e7818eb7af8349"]`
- Suggested outcome: **exposed_not_adopted_candidate** (requires review)

## Interpretation safeguards

- R→P adoption uses only actual Planner output; P→E adoption uses actual Executor tool-call output.
- A tool attempt is not an executed operation; execution is not automatically a state change.
- Verifier exposure alone is not downstream exposure; rejection is not automatically correct.
- Audit history matters even when a full restart later finishes successfully.
- No automatic final causal label is assigned; compare a matched clean run and manually review.
