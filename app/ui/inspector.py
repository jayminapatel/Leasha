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

from app.ui.presenter import _exact_date, format_when, kind_tag

__all__ = ["preview_facts"]


def preview_facts(row: Any, *, now: Any = None) -> Sequence[tuple[str, str]]:
    """Label/value pairs for `row` - a `ResultRow`, `ResultGroup` or anything
    with the same attribute names. Empty values are left out."""
    if row is None:
        return ()
    kind = str(getattr(row, "kind", "") or getattr(row, "ext", "") or "")
    mtime = int(getattr(row, "mtime_ns", 0) or 0)
    folder = str(getattr(row, "folder", "") or getattr(row, "location", "") or "")
    page = getattr(row, "page", None)
    facts = [
        ("Kind", kind_tag(kind) if kind else ""),
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
