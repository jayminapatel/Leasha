"""What a button on an account's activity row does, once for every host - and the question Allow asks. Qt-free.

Layer: L5 presenter.

Review 2026-10-14, CS-1: the rows (`widgets/activity_table.py`) are drawn in two places, the Sync window and each
account's panel in Settings > Accounts, and each had its own `if action == ...` ladder. The panel's had no `allow`
and no `fix`, so those two buttons did nothing there. Now both hosts pass a press to `handle`, which knows every key
`account_activity.actions_for` can return (`account_activity.ACTION_KEYS`) and calls one method of the host for
it (`HOST_METHODS`). A key added to the rows and not here fails `tests/unit/test_account_actions.py`; so does a host
that lacks one of the methods.

CS-2: Allow used to run at once - a delete with less ceremony than an undo. `allow_question` words the
confirmation from what was held: the held deletes by name and how many, or, for the churn guard, what changed. The
button says what it does ("Allow and delete 12", a danger button; "Allow and sync" for held changes) and Cancel is
the default (`widgets/confirm.py`).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence

from app.ui.presenter.account_activity import ACTION_KEYS

__all__ = ["HOST_METHODS", "handle", "handled_keys", "AllowQuestion", "allow_question", "name_list"]

#: What a host of the rows provides. `row_sync(account_id, kind)`, `row_stop()`, `row_pause(account_id, paused)`,
#: `row_allow(account_id)`, `row_fix(account_id)`, `row_account(account_id, "test" | "reconnect" | "settings")`,
#: `row_duplicates()`.
HOST_METHODS = ("row_sync", "row_stop", "row_pause", "row_allow", "row_fix", "row_account", "row_duplicates")

_ROUTES: dict[str, tuple[str, str]] = {
    # key -> (host method, how it is called)
    "sync": ("row_sync", "kind"), "retry": ("row_sync", "kind"), "retry_refused": ("row_sync", "contacts"),
    "stop": ("row_stop", ""),
    "pause": ("row_pause", "pause"), "resume": ("row_pause", "resume"),
    "allow": ("row_allow", "id"), "fix": ("row_fix", "id"),
    "test": ("row_account", "action"), "reconnect": ("row_account", "action"), "settings": ("row_account", "action"),
    "duplicates": ("row_duplicates", ""),
}


def handled_keys() -> frozenset[str]:
    return frozenset(_ROUTES)


def handle(host: Any, account_id: int, kind: str, action: str) -> bool:
    """Pass one row button to `host`. False for a key nothing handles (none, while the test holds)."""
    route = _ROUTES.get(action)
    if route is None:
        return False
    method, how = route
    fn = getattr(host, method)
    if how == "kind":
        fn(account_id, kind)
    elif how == "contacts":
        fn(account_id, "contacts")
    elif how in ("pause", "resume"):
        fn(account_id, how == "pause")
    elif how == "id":
        fn(account_id)
    elif how == "action":
        fn(account_id, action)
    else:
        fn()
    return True


assert handled_keys() >= ACTION_KEYS, sorted(ACTION_KEYS - handled_keys())


# ------------------------------------------------------------------------------------------------ CS-2: Allow
@dataclass(frozen=True)
class AllowQuestion:
    title: str
    text: str
    informative: str
    verb: str
    danger: bool
    count: int = 0           # the deletes it lets through


def name_list(names: Sequence[str], total: int, shown: int = 10) -> str:
    """"Ann Lee, Bob Ray and 12 more" - the first `shown` names, and how many more of `total` there are."""
    names = [str(n) for n in names if str(n).strip()]
    head = ", ".join(names[:shown])
    more = max(0, int(total) - min(len(names), shown))
    if not head:
        return f"{int(total):,} contact{'s' if int(total) != 1 else ''}" if total else ""
    return head + (f" and {more:,} more" if more else "")


def allow_question(account: str, plan: Optional[Mapping[str, Any]], error_code: str = "",
                   error_text: str = "") -> AllowQuestion:
    """The confirmation Allow asks, from what the held run recorded (`SyncRun.plan`, its error code and text)."""
    plan = dict(plan or {})
    names = dict(plan.get("names") or {})
    account = account or "this account"
    held = int(plan.get("held_deletes") or 0)
    flagged = int(plan.get("flagged_deleted") or 0)
    what = " ".join(str(error_text or "").split(" FIX:", 1)[0].split())
    gone_there = ("\n\nAlso found deleted at the account, and kept in Leasha: "
                  + name_list(names.get("flagged_deleted") or [], flagged) + ".") if flagged else ""
    if held:
        listed = name_list(names.get("held_deletes") or [], held)
        return AllowQuestion(
            title="Allow these deletes?",
            text=f"Delete {held:,} contact{'s' if held != 1 else ''} from {account}?",
            informative=(f"Held because it is more than your safety limit: {listed}.{gone_there}\n\n"
                         f"Allow deletes them from {account} and syncs again. Cancel leaves them there."),
            verb=f"Allow and delete {held:,}", danger=True, count=held)
    if error_code == "ERR_SYNC_CHANGES_HELD" or "changed in the same way" in what:
        return AllowQuestion(
            title="Allow these changes?",
            text=f"Write the changes held from {account} to every account?",
            informative=((what + "\n\n") if what else "") + (
                "Allow writes them to every account that keeps these contacts, and syncs again. Cancel keeps "
                "them held; if they are wrong, undo that sync in the sync log." + gone_there),
            verb="Allow and sync", danger=False)
    return AllowQuestion(
        title="Allow what was held?",
        text=f"Carry out what was held for {account} and sync again?",
        informative=((what + "\n\n") if what else "") + "Cancel keeps it held." + gone_there,
        verb="Allow", danger=False)
