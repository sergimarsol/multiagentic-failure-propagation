# Propagation trace analysis (provisional)

Flags are evidence-based observations, NOT experimentally established causal effects.
`?` means unavailable evidence, NOT false. Unapplied injections are excluded from propagation outcomes.

| Scenario | Attempt | Applied | Exposed | Adopted | Attempted | Executed | State diff | Assessment |
|---|---|---|---|---|---|---|---|---|
| t0_pe_accidental_amount | attempt_0 | True | True | True | True | True | True | state_consequence_candidate |

## Case evidence

### t0_pe_accidental_amount / attempt_0 (fault)

- Source: `D:\下载\multiagentic-failure-propagation-main (1)\multiagentic-failure-propagation-main\runs\c-injection-demo\20261031T000859Z-669364\t0_pe_accidental_amount-fault`
- Payload valid: `True`; expected single payload mutation
- Changed paths: `[{"site": "plan", "changed_paths": ["actions[0].args.amount"], "event_id": "event_471fa31b13274fc1bbffcb53ac8ee3a1"}]`
- Verifier: `[]`
- A strict success: `False`; scorer conflict: `False`
- Evidence IDs (events / A audit calls): `["event_471fa31b13274fc1bbffcb53ac8ee3a1", "event_7851c970c5d44951a49250faaccf9e5f", "event_c5883a87dbc54509834dcc7abc3fa0ab", "event_c267e43e28b241a696315e16d0b10918", "d65b760d4e86470ca552d5e90ef7f1f5"]`
- Suggested outcome: **state_consequence_candidate** (requires review)

## Interpretation safeguards

- R→P adoption uses only actual Planner output; P→E adoption uses actual Executor tool-call output.
- A tool attempt is not an executed operation; execution is not automatically a state change.
- Verifier exposure alone is not downstream exposure; rejection is not automatically correct.
- Audit history matters even when a full restart later finishes successfully.
- No automatic final causal label is assigned; compare a matched clean run and manually review.
