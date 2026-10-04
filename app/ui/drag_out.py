r"""Dragging a result out as the file it is. Workspace §3b.

Layer: L5 presenter — Qt-free, so *which* paths may be dragged is decided
where it can be tested, and the widget (`widgets/result_drag_model.py`) is
left with one call.

**Search becomes the source of files, not just the finder.** Drag a result
into Explorer, into an email, into anything that takes a file — the step that
used to be "find it here, then find it again in the folder picker" disappears.

**Adapted from `docs/_superseded/drag_out.py`, not copied.** The draft assumed
a row could carry `attachment_path`/`attachment_file` and a `source_kind` of
its own. Neither exists on `ResultRow` or `ResultGroup` today — checked
against the current `app/ui/presenter.py` rather than against the draft's own
docstring. What is true instead: a mail message and a PST-embedded attachment
both extract to `chunks` at index time and are never written back out to disk
(`extract/email_pst.py` saves an attachment to a *temporary* directory only
long enough to read its text, then deletes it) — so a mail-sourced row simply
has no file to hand to `QDrag`. `mail_view.py` already reaches the same
conclusion for "Open" and "Show in folder": *"a message lives inside a .pst
and has no file on disk to open"*. Dragging follows the same rule, and the
path prefix (`pst://…`) that already means "not a real file" everywhere else
in this codebase (see `presenter.missing_paths`) is what this module checks
too — so a mail row drags nothing, which is also never the whole PST.

Reaching this from disk again — an on-demand re-extraction of one attachment,
cached the way §4e caches a converted PDF — would need new code in
`app/extract/email_pst.py`, which is outside this order's file scope. Noted
in the work order rather than guessed at.

**A path that is no longer there is not offered.** Dropping a broken link
into an email produces an attachment nobody can open and a conversation about
it later; a row whose file has been moved or deleted simply does not drag.
That check is a `stat`, which is why it is optional here and why the caller
does it on a worker if it is doing it at all — see `existing`. The live view
does not even call it: it already knows which paths are missing, computed off
the worker for a different reason (`presenter.missing_paths`), and reuses that
rather than statting a second time at the one moment the interface cannot
afford to block — mid-gesture, with the drag already committed.
"""

from __future__ import annotations

from typing import Any, Iterable, Sequence

__all__ = ["draggable", "paths_for", "existing", "DRAG_ENABLED_KEY"]

#: Whether dragging a result out is on at all. §6: every new behaviour is
#: off-able, and this is the `index_state` flag for this one.
DRAG_ENABLED_KEY = "ui:drag_out_enabled"


def draggable(row: Any) -> str:
    r"""The one path this row would drag, or `""`.

    A real file drags itself. Anything whose path is a synthetic key into
    something else — `pst://store/entry-id/...` for a message or a PST-
    embedded attachment — drags nothing; see the module docstring for why.
    """
    if row is None:
        return ""
    path = str(getattr(row, "path", "") or "").strip()
    # 2026-10-04: nor a file inside a zip (`D:\a.zip/q3/x.docx`) or a key on a
    # catalogued drive (`leasha-volume://`) - neither is a file Explorer can
    # take. Dragging the read-only copy would mean writing it mid-gesture, on
    # this thread; Open makes that copy instead (`tasks.open_target`).
    from app.core.row_facts import is_synthetic_path   # 2026-10-04, code review: an mbox message too
    from app.ui.attachment_open import zip_member_of

    if not path or is_synthetic_path(path) or zip_member_of(path)[0]:
        return ""
    return path


def paths_for(rows: Iterable[Any]) -> tuple:
    r"""Every path a selection would drag, in order, without repeats.

    Order kept, because dropping several files into a folder and finding them
    in a different order than the list showed is a small thing that makes a
    feature feel unreliable.
    """
    found: list = []
    seen: set = set()
    for row in rows or ():
        path = draggable(row)
        if path and path not in seen:
            seen.add(path)
            found.append(path)
    return tuple(found)


def existing(paths: Sequence[str]) -> tuple:
    r"""The paths that are actually on disk. **Worker thread only.**

    One `stat` per path, which on a sleeping external drive or a network
    share is seconds — so this is never called from a drag-start handler,
    where the interface is already committed to the gesture. The caller
    filters ahead of time, or reuses an already-known missing set, or not at
    all — "not at all" is a reasonable answer: the operating system will say
    so on the drop.
    """
    import os

    out: list = []
    for path in paths or ():
        try:
            if os.path.exists(str(path)):
                out.append(str(path))
        except Exception:                        # noqa: BLE001 - a check
            continue
    return tuple(out)
