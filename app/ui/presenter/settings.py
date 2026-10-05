"""Settings-page text, the doctor report as lines and suggested folders.

Layer: L5. Part of the presenter package; imports no Qt.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from app.ui.presenter.formatting import format_size


def history_label_text(searches: int) -> str:
    """How many searches are recorded, or that the number is unavailable."""
    if searches < 0:
        return "The search history could not be read."
    return f"{searches:,} searches recorded."


def pst_status_text(available: bool) -> str:
    """Which route Outlook archives take, and what the other one costs.

    A greyed-out option with no explanation is a dead end, so the unavailable
    case names the command that changes it.
    """
    if available:
        return "Direct reading is available - archives can be indexed without Outlook."
    return (
        "Direct reading is not installed, so archives go through Outlook. "
        "To read them without it: pip install libpff-python "
        "(needs Build Tools for Visual Studio on Windows)."
    )


def pst_outlook_note(show: bool) -> str:
    """The line under "How to read archives" when Outlook is the chosen route
    although reading directly is available - or nothing. 2026-10-05: the
    owner's archives went through Outlook by a choice the page did not show."""
    if not show:
        return ""
    return ("Archives are being read through Outlook because that is what is chosen above. "
            "Outlook has to be open, and it is slower. Reading them directly is available on "
            "this computer: choose \"Automatic - direct if possible, else Outlook\" to use it.")


def doctor_lines(report: Mapping[str, Any]) -> list[str]:
    """Render a doctor report as plain text for the Settings panel.

    Tolerant of a malformed report on purpose: this is the diagnostics view, and
    a formatter that raises on a missing key hides the very output somebody
    opened it to read.
    """
    lines = ["READY" if report.get("ready") else "NOT READY", ""]
    for check in report.get("checks") or ():
        ok = bool(check.get("ok"))
        mark = "PASS" if ok else ("WARN" if check.get("optional") else "FAIL")
        lines.append(f"[{mark}] {check.get('name', '?')}  {check.get('detail', '')}".rstrip())
        if not ok and check.get("fix"):
            lines.append(f"       FIX: {check['fix']}")
    return lines


def logs_cleared_message(outcome: Any) -> str:
    """What to say afterwards. **Always names the next thing to do or know.**

    Three outcomes, three sentences - and the one that matters most is the
    middle: files that would not delete are almost always the ones this session
    is writing to, which is not a fault and must not read as one.
    """
    if not isinstance(outcome, dict):
        return "The logs were cleared."
    removed = int(outcome.get("removed") or 0)
    freed = int(outcome.get("freed") or 0)
    failed = list(outcome.get("failed") or ())
    kept = int(outcome.get("kept") or 0)

    if not removed and not failed:
        return "There were no old log files to remove."

    said = f"Removed {removed:,} log file{'' if removed == 1 else 's'}"
    if freed:
        said += f", {format_size(freed)} freed"
    if kept == 1:
        said += ". One file this session is still writing to was kept"
    elif kept:
        said += f". {kept} files this session is still writing to were kept"
    if failed:
        shown = ", ".join(failed[:3])
        said += (f". {len(failed)} could not be removed ({shown}) - they are in "
                 f"use by another program; close it and clear again")
    return said + "."


#: The profile folders offered on a first run, in the order they are shown.
#:
#: Ordinary places ordinary documents live. Not the whole profile, which drags
#: in AppData and every cache in it, and not a drive.
SUGGESTED_FOLDERS = ("Documents", "Desktop", "Downloads", "Pictures")


def owns_path(candidate: str, home: Optional[str] = None) -> bool:
    r"""Is `candidate` inside *this* account's profile?

    The guard behind "no root that resolves inside another user's profile is
    ever *suggested*". Typing one in stays allowed - the machine belongs to the
    person using it - but a default that reaches into `C:\Users\someone-else`
    is the one thing this order exists to make impossible.

    Compared case-insensitively and separator-insensitively, because
    `C:/Users/jaymin` and `C:\Users\Jaymin` are the same folder and a string
    comparison that says otherwise would wave through exactly the case that
    matters.
    """
    if not candidate:
        return False
    root = (home if home is not None else os.path.expanduser("~"))
    if not root:
        return False

    # A comparison key only - never shown, stored or opened (order 0x
    # section 7a/7b, kept as it was). Both sides are flattened the same way,
    # so on a Mac `/Users/me` and `/Users/me/Documents` compare correctly with
    # the separators turned round. Ignoring case can only make this guard say
    # "yours" for a folder whose name differs from the home folder by case
    # alone; every suggestion is built from the home folder itself, so that
    # cannot happen to a suggested folder.
    def flatten(value: str) -> str:
        return value.replace("/", "\\").rstrip("\\").lower()

    flat_root, flat = flatten(root), flatten(candidate)
    return flat == flat_root or flat.startswith(flat_root + "\\")


def suggested_roots(home: Optional[str] = None,
                    exists: Optional[Any] = None) -> list[str]:
    r"""Folders to offer on a first run. **Offered, never added.**

    Returns only those that exist, in `SUGGESTED_FOLDERS` order, and only ones
    inside this account's own profile - so the list is safe to present without
    anybody checking it, which is the point of computing it here rather than in
    a view.

    `exists` is injectable so this is testable without creating directories,
    and `home` so a test can pretend to be somebody else.
    """
    root = home if home is not None else os.path.expanduser("~")
    if not root:
        return []
    here = exists if exists is not None else (lambda p: Path(p).is_dir())

    # Joined with the separator the root already uses rather than with
    # `Path`, which on Linux leaves `C:\Users\jaymin/Documents` - correct
    # enough to open, and not something to show anybody.
    separator = "\\" if "\\" in root else "/"
    out: list[str] = []
    for name in SUGGESTED_FOLDERS:
        candidate = root.rstrip("\\/") + separator + name
        if not owns_path(candidate, home=root):
            continue                     # unreachable today; the guard is the point
        if here(candidate):
            out.append(candidate)
    return out


def nothing_indexed_yet(roots: Sequence[str]) -> str:
    """What the window says when no folder has been chosen.

    **A blank list and a broken app look identical**, which is the failure this
    project keeps finding in other places. One sentence, and it says what to do.
    """
    if roots:
        return ""
    return ("No folders are being indexed yet, so there is nothing to search. "
            "Choose a folder below and Leasha will index it.")


def cleared_message(outcome: Any) -> str:
    """What the status bar says after a reset. **Always names an amount.**

    Three different things can have happened and they need three different
    sentences. "Index cleared - 0 documents removed" after resetting an index
    that was already empty reads as a failure; a reset that removed thousands of
    documents and freed nothing is a real fault worth pointing at the log for;
    and the ordinary case should say how much disk came back, because that is
    the question somebody resets an index to answer.
    """
    if not isinstance(outcome, dict):            # an older signal shape
        outcome = {"removed": int(outcome or 0), "freed": 0}
    removed = int(outcome.get("removed") or 0)
    freed = int(outcome.get("freed") or 0)

    if not removed:
        return "The index was already empty - there was nothing to remove."
    if freed <= 0:
        return (
            f"Index cleared - {removed:,} documents removed, but the disk space "
            "has not come back yet. Close any other Leasha window or command "
            "line run and reset again; the log line says which step failed.")
    return (f"Index cleared - {removed:,} documents removed, "
            f"{format_size(freed)} freed. Press Start indexing to rebuild.")
