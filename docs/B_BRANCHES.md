# B workflow delivery branches

The compatible branch uses main commit `ce7d6f8`. The interface branch additionally
incorporates main commit `897b78c` (PR #26, shared initial passes). Both include C's
original PR #27 commit `7879c57` with its original authorship and remove task-specific retrieval and
calculation hints and add the offline error-source report described in
[workflow.md](workflow.md#separating-native-errors-from-injection-observations).
Task requests, permissions, scoring rules and C's fault payloads are unchanged.

| Branch | Stage integration | C analysis |
| --- | --- | --- |
| `dev/b-phase2-compatible` | Existing D adapter calls B's private stage routine; runner v1 | Original PR #27 analyzer and tests |
| `dev/b-stage-interface-v2` | Public `run_stage()` with continuation state and output provenance; runner v2 | Same rules, with explicit v1/v2 runner support |

The interface branch additionally changes D's `experiments/banking_selective.py`
to call the public API and save stage state. In `experiments/selective.py`,
`NodeOutput` links the outer handoff to B's recorded output, and context records
produced and delivered artifact IDs. C's analyzer/test trace selectors accept
both runner versions. Recovery policies and A/C scoring/classification rules
are unchanged. Existing JSON-returning D adapters remain supported. Checkpoints
must be produced by the matching implementation; cross-version resume is not
promised. Verification/regeneration primitives are exposed by the API, but are
not added as automatic nodes in D's recovery graph.

In the updated interface branch, recovery methods for the same scenario and
repeat reuse the recorded initial model responses, as implemented in PR #26.
Recovery calls still reach the model client. Trace call counts include replayed
responses; use the `initial_pass` event's `live_calls` and `replayed_calls` to
distinguish new initial-pass requests from replay. This update preserves both
the shared-pass tests from main and B's stage-state/provenance/retry tests.

## Run and analyze

Use a new output directory for each experiment. These commands work on both
branches and make no model calls:

```sh
.venv/bin/python scripts/run_phase2_banking.py --scripted --repeats 1 --output runs/b-check
.venv/bin/python scripts/analyze_phase2_propagation.py --runs-root runs/b-check --output runs/b-analysis
PHASE2_RUNS_ROOT=runs/b-check .venv/bin/python -m pytest -q tests/test_phase2_propagation.py tests/test_phase2_propagation_e2e.py
```

This runs 84 scripted trials: 60 recovery trials and 24 non-recovery trials.
The Phase 2 analyzer processes the 60 recovery traces; Phase 1 analysis handles
the others. Scripted success validates integration, not real-model accuracy.
For an existing real-model trace directory, use the same analysis command with
its path; analysis makes no new model calls.

## Prompt and error findings

The 12-trial real-model prompt comparison covered two tasks, three conditions
and two prompt versions, with one repeat. Removing retrieval/calculation hints
exposed baseline mistakes. In one rent trial, an incorrect base rent of 1000
plus an injected increase of 200 reached the correct final 1200 by cancellation.
Final task success alone therefore does not demonstrate fault resistance.

The new report separates clean-run failures, known mistakes already present
before injection, observed adoption and final task outcomes. Cancellation is a
manual-review candidate. Independently sampled runs do not establish injection
causality. See `scripts/analyze_error_sources.py --help` and the workflow guide.

Full local traces live under ignored `runs/` directories and are not included
in these branches. No credentials or new real-model runs are required to use
the scripted validation and offline analysis above.
