"""JSON views of the Banking environment state and diffs between them.

A *state dict* is the flat, JSON-serializable view used in logs:

    {"balance", "iban", "transactions", "scheduled_transactions", "files", "user_account"}

A *state diff* only contains the keys that changed (an empty dict means "no change"):

    {
      "balance": [before, after],
      "iban": [before, after],
      "transactions":           {"added": [tx], "removed": [tx], "modified": [{"id", "changes": {field: [b, a]}}]},
      "scheduled_transactions": {... same as transactions ...},
      "user_account": {field: [before, after]},          # includes "password"
      "files": {"added": [name], "removed": [name], "modified": [name]},
    }
"""

from __future__ import annotations

from typing import Any

from env._agentdojo import BankingEnvironment

State = dict[str, Any]
StateDiff = dict[str, Any]


def state_dict(env: BankingEnvironment) -> State:
    """Flat JSON-serializable view of a BankingEnvironment."""
    dump = env.model_dump(mode="json")
    bank = dump["bank_account"]
    return {
        "balance": bank["balance"],
        "iban": bank["iban"],
        "transactions": bank["transactions"],
        "scheduled_transactions": bank["scheduled_transactions"],
        "files": dump["filesystem"]["files"],
        "user_account": dump["user_account"],
    }


def env_from_state(state: State) -> BankingEnvironment:
    """Inverse of `state_dict`."""
    return BankingEnvironment.model_validate(
        {
            "bank_account": {
                "balance": state["balance"],
                "iban": state["iban"],
                "transactions": state["transactions"],
                "scheduled_transactions": state["scheduled_transactions"],
            },
            "filesystem": {"files": state["files"]},
            "user_account": state["user_account"],
        }
    )


def as_state(x: BankingEnvironment | State) -> State:
    return state_dict(x) if isinstance(x, BankingEnvironment) else x


def as_env(x: BankingEnvironment | State) -> BankingEnvironment:
    return x if isinstance(x, BankingEnvironment) else env_from_state(x)


def _diff_records(before: list[dict], after: list[dict]) -> dict[str, list]:
    """Diff two lists of transactions keyed by their `id`."""
    b = {t["id"]: t for t in before}
    a = {t["id"]: t for t in after}
    out: dict[str, list] = {}
    added = [a[i] for i in a if i not in b]
    removed = [b[i] for i in b if i not in a]
    modified = []
    for i in b:
        if i in a and a[i] != b[i]:
            changes = {k: [b[i].get(k), a[i].get(k)] for k in sorted(set(a[i]) | set(b[i])) if a[i].get(k) != b[i].get(k)}
            modified.append({"id": i, "changes": changes})
    if added:
        out["added"] = added
    if removed:
        out["removed"] = removed
    if modified:
        out["modified"] = modified
    return out


def diff_states(before: BankingEnvironment | State, after: BankingEnvironment | State) -> StateDiff:
    """Field-level diff between two states. Returns {} when nothing changed."""
    b, a = as_state(before), as_state(after)
    diff: StateDiff = {}
    for key in ("balance", "iban"):
        if b[key] != a[key]:
            diff[key] = [b[key], a[key]]
    for key in ("transactions", "scheduled_transactions"):
        if d := _diff_records(b[key], a[key]):
            diff[key] = d
    ua = {k: [b["user_account"].get(k), a["user_account"].get(k)]
          for k in sorted(set(b["user_account"]) | set(a["user_account"]))
          if b["user_account"].get(k) != a["user_account"].get(k)}
    if ua:
        diff["user_account"] = ua
    bf, af = b["files"], a["files"]
    files = {
        "added": sorted(set(af) - set(bf)),
        "removed": sorted(set(bf) - set(af)),
        "modified": sorted(k for k in set(af) & set(bf) if af[k] != bf[k]),
    }
    files = {k: v for k, v in files.items() if v}
    if files:
        diff["files"] = files
    return diff
