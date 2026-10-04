r"""The facts header at the top of the preview pane. Qt-free.

Layer: L5 (presenter half)

UI Redesign (202626160950 §5a). The preview pane gained a small grid of
facts between its title and its content: Kind, Modified, Folder, Page. Only
fields a result row already carries - there is no size on a `SearchResult`
or a `ResultRow` today, so none is shown rather than a "—" that looks like
an answer. The mockup's "Also in" (a mail-attachment twin) is not built for
the same reason: nothing in the store answers it.

`preview_facts(row)` returns `((label, value), …)` with empty values already
dropped, so the pane draws exactly what it is given.
"""

from __future__ import annotations

from typing import Any, Sequence

from app.ui.presenter import _exact_date, folder_words, format_when, kind_tag

__all__ = ["preview_facts"]


def preview_facts(row: Any, *, now: Any = None) -> Sequence[tuple[str, str]]:
    """Label/value pairs for `row` - a `ResultRow`, `ResultGroup` or anything
    with the same attribute names. Empty values are left out.

    2026-10-04, the owner ("the same code should run"): each value is the one
    the list beside it shows - the badge is `kind_tag` (a Files row's is
    already one, and reads the same again), the date is the row's shown date
    (a photo's own, an attachment's message's - `facts.shown_date_ns`, which
    the lists stamp into `mtime_ns`; a Code row's `seen_at`), and the folder
    is the row's own or `folder_words` - **never the location**, which made a
    passage's folder read "page 3" beside a Page fact saying 3.
    """
    if row is None:
        return ()
    kind = str(getattr(row, "kind", "") or getattr(row, "ext", "") or "")
    mtime = int(getattr(row, "mtime_ns", 0) or getattr(row, "seen_at", 0) or 0)
    folder = str(getattr(row, "folder", "") or "") or folder_words(
        getattr(row, "path", ""), relative_path=getattr(row, "relative_path", ""),
        volume_id=getattr(row, "volume_id", None))
    page = getattr(row, "page", None)
    facts = [
        ("Kind", kind_tag(kind)),
        ("Modified", _when(mtime, now)),
        ("Folder", folder),
        ("Page", str(page) if page else ""),
    ]
    return tuple((label, value) for label, value in facts if value)


def _when(mtime_ns: int, now: Any) -> str:
    if not mtime_ns:
        return ""
    age = format_when(mtime_ns, now=now)
    exact = _exact_date(mtime_ns)
    return f"{age} ({exact})" if age and exact and age != exact else (exact or age)
