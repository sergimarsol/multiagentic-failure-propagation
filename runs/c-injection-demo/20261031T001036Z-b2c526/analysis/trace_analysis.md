# Propagation trace analysis (provisional)

Flags are evidence-based observations, NOT experimentally established causal effects.
`?` means unavailable evidence, NOT false. Unapplied injections are excluded from propagation outcomes.

| Scenario | Attempt | Applied | Exposed | Adopted | Attempted | Executed | State diff | Assessment |
|---|---|---|---|---|---|---|---|---|
| t0_rp_accidental_amount | attempt_0 | True | True | True | True | True | True | state_consequence_candidate |

## Case evidence

### t0_rp_accidental_amount / attempt_0 (fault)

- Source: `D:\下载\multiagentic-failure-propagation-main (1)\multiagentic-failure-propagation-main\runs\c-injection-demo\20261031T001036Z-b2c526\t0_rp_accidental_amount-fault`
- Payload valid: `True`; expected single payload mutation
- Changed paths: `[{"site": "research", "changed_paths": ["facts.bill_amount"], "event_id": "event_e8eb46f19ab841829fb3caf0731f725f"}]`
- Verifier: `[]`
- A strict success: `False`; scorer conflict: `False`
- Evidence IDs (events / A audit calls): `["event_e8eb46f19ab841829fb3caf0731f725f", "event_9b068ec77f234e579c4e742c929b66f5", "event_41a67e23467a44cb91b5fb7976422511", "0514287411f442f58b188d9cf2f90a46"]`
- Suggested outcome: **state_consequence_candidate** (requires review)

## Interpretation safeguards

- R→P adoption uses only actual Planner output; P→E adoption uses actual Executor tool-call output.
- A tool attempt is not an executed operation; execution is not automatically a state change.
- Verifier exposure alone is not downstream exposure; rejection is not automatically correct.
- Audit history matters even when a full restart later finishes successfully.
- No automatic final causal label is assigned; compare a matched clean run and manually review.
