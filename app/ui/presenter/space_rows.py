r"""The Space Report's interactive tables: what each row says and sorts on.

Layer: L5. Part of the presenter package; imports no Qt.

Order 202626270602 (0n) section 3a: "Table + a few plain numbers; sortable;
row -> reveals the copies with their sources." `app/reports/space.py` finds
things; `app/ui/widgets/space_table.py` draws them; this is the part between,
and it is plain data so it can be tested without a display.

**Every cell carries the value it sorts on as well as the words it shows** -
the lesson `sortable_item.py` records: "10 KB" sorts before "3 KB" as text.
A row's `sort` tuple lines up with its `cells`; a cell with nothing worth
ordering by holds `None`, and the widget falls back to the text.

**Near-duplicate photos carry no "space you'd get back" column, on purpose.**
See `NearDuplicatePhotoGroup`'s own docstring: they are not byte-identical, so
deleting all but one is not automatically safe, and the report states what
was found without asserting a saving it cannot guarantee. The sizes are shown
per version and never totalled.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Sequence

from app.reports.space import (
    DuplicateCopy,
    SpaceFindings,
    coverage_sentence,
    formatted_date,
    size_words,
)

__all__ = ["SpaceRow", "SpaceTable", "space_tables", "space_headline"]

#: How many distinct sources a group's "Where" cell names before it says
#: "and N more" - the copies themselves are one click away.
WHERE_NAMED = 3

_VOLUME_KEY = re.compile(r"^leasha-volume://\d+/")


@dataclass(frozen=True, slots=True)
class SpaceRow:
    """One table row: its cells, what each sorts on, a tooltip and its child rows."""
    cells: tuple[str, ...]
    #: Lines up with `cells`. `None` means "sort this cell by its text".
    sort: tuple[Any, ...]
    tooltip: str = ""
    children: tuple["SpaceRow", ...] = ()


@dataclass(frozen=True, slots=True)
class SpaceTable:
    """One of the Space Report's four tables, as plain data for the widget."""
    key: str
    title: str
    #: Plain words: what this table lists and how to use it (its tooltip).
    hint: str
    headers: tuple[str, ...]
    #: `"left"` / `"right"`, per column - the vocabulary `align_headers` uses.
    aligns: tuple[str, ...]
    rows: tuple[SpaceRow, ...]
    #: Said in place of the table when there is nothing to list.
    empty: str
    #: The column the table opens sorted by, or -1 to keep the order given.
    sort_column: int = -1
    descending: bool = True


def _status_note(status: str) -> str:
    return "" if status in ("", "online") else status


def _where(copy: DuplicateCopy) -> str:
    """Where a copy lives, in words: this computer, or the source and its status."""
    if copy.source_kind == "local":
        return "This computer"
    note = _status_note(copy.source_status)
    return f"{copy.source_name} ({note})" if note else copy.source_name


def _location(copy: DuplicateCopy) -> str:
    """A path a person can read: a local file's real path, a catalogued
    volume's path *on that volume* (the stored key is an internal address)."""
    return _VOLUME_KEY.sub("", copy.path) if copy.source_kind != "local" else copy.path


def _where_summary(copies: Sequence[DuplicateCopy]) -> str:
    """The distinct places a group's copies live, up to `WHERE_NAMED` named."""
    seen: list[str] = []
    for copy in copies:
        place = _where(copy)
        if place not in seen:
            seen.append(place)
    if len(seen) <= WHERE_NAMED:
        return ", ".join(seen)
    return ", ".join(seen[:WHERE_NAMED]) + f" and {len(seen) - WHERE_NAMED} more"


def _name(copy: Optional[DuplicateCopy]) -> str:
    """A copy's file name, read the same way for a Windows path on any platform."""
    if copy is None:
        return "(unknown)"
    from app.core.osbridge.pathnames import name_of  # 2026-10-05: `name_of`, so a Windows-shaped path reads the same on a Mac

    return name_of(_location(copy)) or _location(copy)


def _copy_row(copy: DuplicateCopy, *, size: Optional[int] = None, size_column: int = -1,
              width: int = 5) -> SpaceRow:
    """A child row for one copy: its path, optionally its size, and where it lives."""
    cells = [_location(copy)] + [""] * (width - 1)
    sort: list[Any] = [_location(copy).lower()] + [None] * (width - 1)
    place = _where(copy)
    cells[-1], sort[-1] = place, place.lower()
    if size is not None and size_column >= 0:
        cells[size_column], sort[size_column] = size_words(size), size
    return SpaceRow(tuple(cells), tuple(sort), tooltip=_location(copy))


def _duplicates(findings: SpaceFindings) -> SpaceTable:
    """The Duplicates table: biggest saving first, each row opening to its copies."""
    rows = []
    for group in findings.groups:
        copies = len(group.copies)
        name = _name(group.copies[0] if group.copies else None)
        where = _where_summary(group.copies)
        rows.append(SpaceRow(
            cells=(name, f"{copies:,}", size_words(group.size_bytes),
                   size_words(group.reclaimable_bytes), where),
            sort=(name.lower(), copies, group.size_bytes, group.reclaimable_bytes,
                  where.lower()),
            tooltip=(f"{copies} copies of this file, {size_words(group.size_bytes)} each. "
                     f"Keeping one would free {size_words(group.reclaimable_bytes)}. "
                     f"Open the row to see where each copy is."),
            children=tuple(_copy_row(c) for c in group.copies),
        ))
    return SpaceTable(
        key="duplicates", title="Duplicates",
        hint=("Files that exist more than once, biggest saving first. Click a heading "
              "to sort by it; open a row (its arrow, or double-click) to see every copy "
              "and where it lives."),
        headers=("File", "Copies", "Size of each", "Space you'd get back", "Where"),
        aligns=("left", "right", "right", "right", "left"),
        rows=tuple(rows), empty="No duplicate files were found.",
        sort_column=3, descending=True)


def _similar_photos(findings: SpaceFindings) -> SpaceTable:
    """The Similar photos table. No saving column - see the module docstring."""
    rows = []
    for group in findings.near_duplicates:
        versions = len(group.copies)
        largest = max(group.sizes_bytes, default=0)
        name = _name(group.copies[0] if group.copies else None)
        where = _where_summary(group.copies)
        rows.append(SpaceRow(
            cells=(name, f"{versions:,}", size_words(largest), where),
            sort=(name.lower(), versions, largest, where.lower()),
            tooltip=(f"{versions} versions of the same picture. They are not identical "
                     f"files, so nothing here is counted as space you'd get back. "
                     f"Open the row to see each version."),
            children=tuple(
                _copy_row(c, size=s, size_column=2, width=4)
                for c, s in zip(group.copies, group.sizes_bytes)),
        ))
    return SpaceTable(
        key="similar", title="Similar photos",
        hint=("The same picture saved more than once with different bytes - a resize or "
              "a re-save. Nothing here is totalled as space you'd get back, because a "
              "different version may be a different resolution or crop. Open a row to "
              "see each version."),
        headers=("Picture", "Versions", "Largest version", "Where"),
        aligns=("left", "right", "right", "left"),
        rows=tuple(rows), empty="No similar photos were found.",
        sort_column=1, descending=True)


def _by_source(findings: SpaceFindings) -> SpaceTable:
    """The By source table: how much of each source also exists elsewhere."""
    rows = []
    for source in findings.duplicate_share:
        note = _status_note(source.status)
        name = f"{source.name} ({note})" if note else source.name
        rows.append(SpaceRow(
            cells=(name, f"{source.total_count:,}", f"{source.duplicate_count:,}",
                   f"{source.share:.0%}"),
            sort=(name.lower(), source.total_count, source.duplicate_count, source.share),
            tooltip=(f"{source.duplicate_count:,} of {source.total_count:,} files on "
                     f"{source.name} also exist somewhere else."),
        ))
    return SpaceTable(
        key="by-source", title="By source",
        hint=("For each source, how much of what it holds also exists somewhere else - "
              "a high share means it is a copy of things you have anyway. Click a "
              "heading to sort."),
        headers=("Source", "Files", "Also elsewhere", "Share"),
        aligns=("left", "right", "right", "right"),
        rows=tuple(rows), empty="Nothing is indexed yet.",
        sort_column=3, descending=True)


def _only_copy(findings: SpaceFindings) -> SpaceTable:
    """The only-copy table: files that exist on exactly one source, per source."""
    rows = []
    for source in findings.uniqueness:
        note = _status_note(source.status)
        when = formatted_date(source.last_seen)
        rows.append(SpaceRow(
            cells=(source.name, f"{source.file_count:,}", note.capitalize(), when),
            sort=(source.name.lower(), source.file_count, note, int(source.last_seen or 0)),
            tooltip=(f"{source.file_count:,} files exist nowhere else in the index but "
                     f"{source.name}. If it is lost, so is their content."),
        ))
    return SpaceTable(
        key="only-copy", title="The only copy",
        hint=("Files that exist on exactly one source, counted per source. Drives that "
              "are unplugged come first. Click a heading to sort."),
        headers=("Source", "Files only here", "Right now", "Last seen"),
        aligns=("left", "right", "left", "right"),
        rows=tuple(rows),
        empty="Nothing in the index exists on exactly one source yet.",
        sort_column=-1)


def space_tables(findings: SpaceFindings) -> tuple[SpaceTable, ...]:
    """The four tables, in the order the document reads them."""
    return (_duplicates(findings), _similar_photos(findings),
            _by_source(findings), _only_copy(findings))


def shape(document: Any) -> Any:
    """Attach the headline and the four tables to a `SpaceDocument` as
    `document.shaped`, for the window to draw from. 2026-10-03.

    **Called on the worker that read the findings**, never on the window's
    thread: shaping is one `SpaceRow` per group and per copy inside it, and on
    an index of mail - 25 groups, thousands of copies each - that was the last
    of the Space Report's work still done on the window's thread (measured at
    about 170 ms for 100,000 copies after the rows themselves went lazy). A
    document that is not a `SpaceDocument` is returned as it is; if shaping
    fails, the window shapes for itself as before. Never raises.
    """
    findings = getattr(document, "findings", None)
    if findings is None:
        return document
    try:
        document.shaped = (space_headline(findings), space_tables(findings))
    except Exception:                               # noqa: BLE001 - the window falls back
        pass
    return document


def space_headline(findings: SpaceFindings) -> str:
    """A few plain numbers, in the document's own words."""
    lines = []
    thin = coverage_sentence(findings.files_total, findings.files_compared)
    if findings.groups or findings.total_reclaimable:
        lines.append("Keeping one copy of everything duplicated would free "
                     f"{size_words(findings.total_reclaimable)}.")
    elif findings.files_total and not findings.files_compared:
        lines.append(thin)
        thin = ""
    else:
        lines.append("No duplicate files were found.")
    if thin:
        lines.append(thin)
    for source in findings.uniqueness[:2]:
        seen = formatted_date(source.last_seen)
        note = _status_note(source.status)
        noun = "file" if source.file_count == 1 else "files"
        lines.append(
            f"{source.file_count:,} {noun} exist nowhere else but {source.name}"
            f"{', currently ' + note if note else ''}{f' (last seen {seen})' if seen else ''}.")
    return "\n".join(lines)
