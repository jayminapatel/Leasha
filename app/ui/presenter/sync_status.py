"""The words of the Sync window: what a progress event or a finished run says. Qt-free.

Layer: L5 presenter. Drawn by `app.ui.contacts_tools.sync_dialog.SyncDialog`.
"""

from __future__ import annotations

from typing import Any, Mapping

__all__ = ["STATUS_WORDS", "eta_words", "step_words", "result_words", "summary_words", "counts_words"]

#: What a finished account says, in the words the sync log uses.
STATUS_WORDS = {
    "ok": "Done", "partial": "Done, with problems", "stopped": "Stopped", "failed": "Failed", "held": "Held",
    "needs-approval": "Waiting for you", "dry-run": "Checked, nothing written", "interrupted": "Stopped",
    "running": "Running", "": "Not yet synced",
}


def eta_words(done: int, total: int, elapsed_s: float) -> str:
    """"about 12 min left", once there is enough to go on; "" before that."""
    if total <= 0 or done <= 0 or done >= total or elapsed_s < 4:
        return ""
    left = (total - done) * elapsed_s / done
    if left < 45:
        return "less than a minute left"
    if left < 5400:
        return f"about {max(1, round(left / 60))} min left"
    return f"about {left / 3600:.1f} hours left"


def step_words(event: Mapping[str, Any], elapsed_s: float = 0.0) -> str:
    """The step an account is on, from a progress event."""
    phase = str(event.get("phase", ""))
    done, total = int(event.get("done") or 0), int(event.get("total") or 0)
    if phase == "reading":
        return f"Reading the account - {done:,} so far" if done else "Reading the account"
    if phase == "writing":
        if not total:
            return "Nothing to write"
        eta = eta_words(done, total, elapsed_s)
        return f"Writing {done:,} of {total:,}" + (f" - {eta}" if eta else "")
    if phase == "done":
        return STATUS_WORDS.get(str(event.get("status", "")), "Done")
    return ""


def counts_words(event: Mapping[str, Any]) -> str:
    """"12 added, 40 changed, 1 deleted, 2 problems" - only what is not zero."""
    bits = []
    for key, word in (("added", "added"), ("changed", "changed"), ("deleted", "deleted")):
        n = int(event.get(key) or 0)
        if n:
            bits.append(f"{n:,} {word}")
    failed = int(event.get("failed") or 0)
    if failed:
        bits.append(f"{failed:,} problem{'s' if failed != 1 else ''}")
    return ", ".join(bits) or "nothing to write"


def result_words(status: str, error: str = "") -> str:
    word = STATUS_WORDS.get(status, status.capitalize())
    return f"{word}: {error}" if error and status in ("failed", "held") else word


def summary_words(running: bool, stopping: bool, rows: int, problems: int) -> str:
    """The line above the table."""
    if stopping:
        return "Stopping after the contact it is on. What was written stays, and the next sync carries on."
    if running:
        return "Syncing now. You can close this window - the sync carries on - and open it again from Sync."
    if not rows:
        return "No account is set to sync contacts. Add one in Settings > Accounts."
    if problems:
        return (f"The last sync finished with {problems:,} problem{'s' if problems != 1 else ''}. The accounts "
                "refused those contacts one by one; the rest was written. Sync now tries them again.")
    return "Idle. Sync now brings every account and Leasha level."
