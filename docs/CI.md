# Continuous integration

`Python CI` runs on all pull requests (including drafts and stacked PRs), pushes to
`main`, and manual dispatch. The stable check name is **Python tests**.

The job uses Ubuntu 24.04, Python 3.11.9, uv 0.11.26, and `uv sync --locked --group dev`.
The lock file must already match the project; CI must not silently update it. Action
versions are pinned to immutable commits. It runs the complete pytest suite, then
Phase 1 scripted Banking and Phase 2 toy batch smoke checks and validates their traces.
No model weights, API keys, live banking services, or paid inference are used. Package
installation needs network access; model downloads are disabled. These checks establish
software behavior, not model performance. The job has read-only repository permissions.

## Local equivalent

```sh
export UV_PYTHON=3.11.9
uv sync --locked --group dev --python 3.11.9
uv run --no-sync python -m pytest -q -p no:cacheprovider
uv run --no-sync python scripts/run_experiment_batch.py --config configs/d_phase1_smoke.json --output runs/ci-phase1-local
uv run --no-sync python scripts/run_phase2_batch.py --config configs/d_phase2_smoke.json --output runs/ci-phase2-local
```

Use fresh output directories. Batch commands preserve dispatch errors as result rows;
CI explicitly checks those rows rather than relying solely on the script exit status.

## Merge protection and stacked PRs

A workflow reports checks; it does not itself prevent merging. A repository administrator
can require **Python tests** in the target branch's ruleset after the check is available.
Do not require this new check on unrelated PRs that do not contain the workflow yet.
No branch-protection settings are changed by this work.

This work is stacked on PR #2 (`codex/d-phase1-infrastructure`). Review the follow-up
against that branch. Keep the follow-up draft until dependencies and team integration
are ready. Merge PR #2 first; then verify the follow-up base/diff and retarget to `main`
if needed. A squash/rebase merge of #2 may require rebasing the follow-up onto the new
main history before merge. Re-run CI after changing the base or resolving conflicts.
