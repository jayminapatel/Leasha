r"""Dragging a result out as the file it is. Workspace §3b.

Layer: L5 presenter — Qt-free, so *which* paths may be dragged is decided
where it can be tested, and the widget is left with one call.

**Search becomes the source of files, not just the finder.** Drag a result
into Explorer, into an email, into anything that takes a file — the step that
used to be "find it here, then find it again in the folder picker" disappears.

**A path that is not there is not offered.** Dropping a broken link into an
email produces an attachment nobody can open and a conversation about it
later; a row whose file has been moved or deleted simply does not drag. That
check is a `stat`, which is why it is optional here and why the caller does it
on a worker if it is doing it at all — see `existing`.

**A mail message drags its attachment, and nothing else.** §3b's words. There
is no file on disk that *is* a message — a PST is one file holding a hundred
thousand — so dragging the row would either drag the whole archive or drag
nothing, and the first is much worse than the second.
"""

from __future__ import annotations

from typing import Any, Iterable, Sequence

__all__ = ["paths_for", "draggable", "attachment_of", "existing"]


def attachment_of(row: Any) -> str:
    """The file a mail row can offer, or `""`.

    Read from whichever of the two names a row uses. Mail rows carry the
    saved attachment path when one was extracted; nothing else does.
    """
    for name in ("attachment_path", "attachment_file"):
        found = str(getattr(row, name, "") or "").strip()
        if found:
            return found
    return ""


def draggable(row: Any) -> str:
    r"""The one path this row would drag, or `""`.

    A file drags itself. A message drags its attachment if it has one, and
    otherwise drags nothing - see the module docstring for why dragging the
    PST would be worse than refusing.
    """
    if row is None:
        return ""
    attachment = attachment_of(row)
    if attachment:
        return attachment

    path = str(getattr(row, "path", "") or "").strip()
    if not path:
        return ""
    # **A synthetic path is not a file.** `archive.zip/q3/report.docx` and
    # `mail.pst/12345` are keys into something, and handing either to Explorer
    # would produce a file-not-found from the operating system rather than
    # from us. `source_kind` says which is which where a row carries it.
    kind = str(getattr(row, "source_kind", "") or "").lower()
    if kind in ("archive", "pst_message", "pst-message"):
        return ""
    return path


def paths_for(rows: Iterable[Any]) -> tuple:
    r"""Every path a selection would drag, in order, without repeats.

    Order kept, because dropping five files into a folder and finding them in
    a different order than the list showed is a small thing that makes a
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
    share is seconds - so this is never called from a drag-start handler,
    where the interface is already committed to the gesture. The caller
    filters ahead of time or not at all, and "not at all" is a reasonable
    answer: the operating system will say so on the drop.
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
