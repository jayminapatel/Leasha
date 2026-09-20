r"""The Digital Inheritance report: everything Leasha knows exists.

Layer: L4 (reports) - reads L1 (`SqliteStore`) only, never writes to it and
never touches a user file. Non-negotiable #10 applies in full: a report is
read-only against user data, the same guarantee the indexer itself keeps.

Order 202626270602 (0n) section 2. One document mapping every source -
local roots and catalogued Offline Media volumes alike, told the same
way - written for a reader who is not the owner: a family member, an
executor, whoever finds this beside the will.

**The order's own example sentence** ("the drive labelled 'Projects 2019'
... holds 41,205 files - photos 2004-2019, project documents...") is more
narrative than what this module produces. Classifying a folder's *content*
("photos", "project documents") from nothing but extensions would be a
guess dressed as a fact, and this project's own standing rule is to
measure or say so rather than assert past what the index actually knows.
What is genuinely derivable - folder names and how many files are in each,
sorted commonest first - is what `catalogue_sources` reports; a reader
still learns "Invoices holds 1,204 files, Photos holds 41,000", which
answers the same question the example does, honestly rather than
impressionistically.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Sequence

from app.core.logging import logger

__all__ = [
    "SourceSummary",
    "catalogue_sources",
    "report_generated_at",
    "render_inheritance_document",
    "data_timestamp_sentence",
    "TOP_FOLDERS_SHOWN",
]

_log = logger.bind(component="reports.inheritance")

#: How many of a source's top-level folders are named individually before
#: the rest are folded into "and N more folders" - a source with two
#: thousand top-level folders (a badly organised drive, or one indexed at
#: too shallow a root) must not turn one row of the report into a page of
#: its own.
TOP_FOLDERS_SHOWN = 12


@dataclass(frozen=True)
class SourceSummary:
    """One row of the catalogue - a local root or a catalogued Offline
    Media volume, told the same way so the report reads as one document
    rather than two different tables stitched together."""

    name: str
    #: "local" for an ordinary index root, else `volumes.kind` - see that
    #: column's own CHECK constraint in schema.sql for the full list.
    kind: str
    description: str = ""
    location_note: str = ""
    #: "online" | "offline" | "locked" | "archived" | "" - a local root is
    #: always reachable while the machine is on, so it carries no status
    #: word at all rather than a misleading "online".
    status: str = ""
    file_count: int = 0
    size_bytes: int = 0
    #: Unix seconds - last scan (a volume) or the newest file seen (a
    #: local root, which has no "scan" of its own to date).
    snapshot_date: Optional[int] = None
    #: `((folder_name, file_count), ...)`, commonest first, already capped
    #: to `TOP_FOLDERS_SHOWN` by `catalogue_sources`.
    top_folders: tuple = ()
    #: How many top-level folders exist beyond what `top_folders` shows.
    more_folders: int = 0
    #: 2c's per-source opt-out - defaults to included; the view flips this
    #: before export, never this module.
    include: bool = True


def catalogue_sources(store: Any, *, roots: Sequence[str] = ()) -> list[SourceSummary]:
    r"""Every source Leasha knows exists, local roots then catalogued
    volumes - the report's whole subject.

    **Read-only**, and one bad source never loses the rest of the report -
    a source whose folder counts cannot be read (an unreadable path, a
    closed store) is still listed, with empty folder detail, rather than
    dropped silently; non-negotiable #3's "one bad file never halts a run"
    applies just as well to one bad source in a report.

    `roots` is the caller's own list of local index roots - this module
    has no opinion on where that comes from (`index_state`'s `ui:roots`
    today), only on what to do with each one once it is given.
    """
    sources: list[SourceSummary] = []
    for root in roots or ():
        cleaned = str(root or "").strip()
        if not cleaned:
            continue
        try:
            info = store.local_root_summary(cleaned)
        except Exception as exc:                  # noqa: BLE001 - see docstring
            _log.debug("could not summarise local root {}: {}", cleaned, exc)
            info = {}
        try:
            folders = store.local_root_folder_counts(cleaned)
        except Exception as exc:                  # noqa: BLE001
            _log.debug("could not count folders under {}: {}", cleaned, exc)
            folders = []
        name = Path(cleaned).name or cleaned
        sources.append(SourceSummary(
            name=name,
            kind="local",
            description=cleaned,
            file_count=int(info.get("n") or 0),
            size_bytes=int(info.get("total_bytes") or 0),
            snapshot_date=_ns_to_seconds(info.get("newest")),
            top_folders=tuple(folders[:TOP_FOLDERS_SHOWN]),
            more_folders=max(0, len(folders) - TOP_FOLDERS_SHOWN),
        ))

    try:
        volume_rows = store.list_volumes()
    except Exception as exc:                      # noqa: BLE001
        _log.debug("could not list catalogued volumes: {}", exc)
        volume_rows = []

    for row in volume_rows:
        volume_id = int(row.get("id", 0) or 0)
        try:
            folders = store.volume_folder_counts(volume_id)
        except Exception as exc:                  # noqa: BLE001
            _log.debug("could not count folders on volume {}: {}", volume_id, exc)
            folders = []
        sources.append(SourceSummary(
            name=str(row.get("name") or ""),
            kind=str(row.get("kind") or ""),
            description=str(row.get("description") or ""),
            location_note=str(row.get("location_note") or ""),
            status=str(row.get("status") or "").lower(),
            file_count=int(row.get("indexed_files") or 0),
            size_bytes=int(row.get("size_bytes") or 0) if row.get("size_bytes") else 0,
            snapshot_date=row.get("last_scanned_at"),
            top_folders=tuple(folders[:TOP_FOLDERS_SHOWN]),
            more_folders=max(0, len(folders) - TOP_FOLDERS_SHOWN),
        ))
    return sources


def _ns_to_seconds(value: Any) -> Optional[int]:
    """`mtime_ns`/`taken_at_ns` to unix seconds, or None. A local root's
    "snapshot date" is the newest file's own timestamp - it has no scan
    date of its own the way a catalogued volume does."""
    try:
        nanoseconds = int(value)
    except (TypeError, ValueError):
        return None
    if nanoseconds <= 0:
        return None
    return nanoseconds // 1_000_000_000


def report_generated_at(store: Any) -> Optional[int]:
    r"""1b: "every report view states its data timestamp." A report is a
    snapshot of the catalogue, honest like everything else - this is the
    moment that snapshot was taken from, not the moment the report was
    drawn on screen.

    `MAX(files.indexed_at)` - the most recent file the index actually
    read - rather than inventing a separate "last run finished" marker
    that would need its own migration and its own place to be wrong.
    """
    try:
        row = store.conn.execute(
            "SELECT MAX(indexed_at) AS latest FROM files"
        ).fetchone()
    except Exception as exc:                      # noqa: BLE001 - a timestamp, not the report
        _log.debug("could not read the report's data timestamp: {}", exc)
        return None
    latest = row["latest"] if row is not None else None
    return int(latest) if latest else None


def data_timestamp_sentence(generated_at: Optional[int]) -> str:
    r"""1b's own words: "from the index as of last run, <date>." `""` for
    a fresh, empty index with nothing indexed yet - stating a timestamp
    that does not exist would be inventing one.
    """
    if not generated_at:
        return "This index has nothing recorded yet."
    when = time.strftime("%d %B %Y", time.localtime(int(generated_at)))
    return f"From the index as of last run, {when}."


def _kind_heading(kind: str) -> str:
    """Plain words for a group heading - never the raw `kind` column
    value, which a reader beside the will has no reason to know."""
    return {
        "local": "Folders on this computer",
        "drive": "Drives",
        "network": "Network shares",
        "cloud": "Cloud storage",
        "phone": "Phones",
        "archived": "Archived (tape, DVD, or handed to someone else)",
    }.get(kind, kind.title() or "Other sources")


def _status_words(source: SourceSummary) -> str:
    """The one clause a reader needs about reachability - "last seen
    <date>" for anything not currently connected, nothing at all for a
    local folder (always reachable) or a currently-online source."""
    if source.kind == "archived":
        when = _formatted_date(source.snapshot_date)
        return f", archived{f' {when}' if when else ''}"
    if source.status == "online":
        return ""
    if source.status in ("offline", "locked"):
        when = _formatted_date(source.snapshot_date)
        return f", last seen{f' {when}' if when else ' date unknown'}"
    return ""


def _formatted_date(value: Optional[int]) -> str:
    if not value:
        return ""
    return time.strftime("%B %Y", time.localtime(int(value)))


def _source_paragraph(source: SourceSummary) -> str:
    r"""One source, one paragraph - the order's own example shape:
    "the drive labelled 'Projects 2019', last seen Nov 2026, holds 41,205
    files" - with the folder breakdown as the sentence after it, since a
    single run-on sentence carrying both reads as a wall of text.
    """
    status = _status_words(source)
    # **The owner's own words for a source, whatever kind it is** ("the old work
    # drive"). This used to be printed for a local root only - where the
    # "description" is merely its path - and dropped for every drive, share and
    # tape, which are exactly the sources a family member holding the printout
    # needs the owner's description of. Order 0n 2a: "its NAME, user
    # description, physical location text".
    where = f" ({source.description})" if source.description else ""
    location = f" - {source.location_note}" if source.location_note else ""
    lines = [
        f"{source.name!r}{where}{status}{location} - "
        f"{_count_words(source.file_count)}, {_size_words(source.size_bytes)}."
    ]
    if source.top_folders:
        parts = [f"{folder} ({_count_words(count)})"
                 for folder, count in source.top_folders]
        more = f", and {source.more_folders} more folder(s)" if source.more_folders else ""
        lines.append("Holds: " + ", ".join(parts) + more + ".")
    elif source.file_count == 0:
        lines.append("Nothing indexed from this source yet.")
    return "\n".join(lines)


def _count_words(n: int) -> str:
    return f"{n:,} file{'s' if n != 1 else ''}"


def _size_words(size_bytes: int) -> str:
    value = float(max(0, size_bytes))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:,.0f} {unit}" if unit == "B" else f"{value:,.1f} {unit}"
        value /= 1024
    return f"{value:,.1f} TB"


def render_inheritance_document(sources: Sequence[SourceSummary], *,
                                generated_at: Optional[int] = None,
                                owner_name: str = "") -> str:
    r"""2a/2b: the whole printable document - grouped by kind, one
    paragraph per source, the data timestamp first. Only sources with
    `include=True` are rendered - 2c's per-source opt-out, applied here
    rather than asking every caller to pre-filter the same way.

    Plain text with light markdown-ish headings (`#`/`##`), which both
    the on-screen view and the PDF export (via `QTextDocument.setMarkdown`)
    render identically - one document, not two.
    """
    title = (f"A map of {owner_name}'s files"
             if owner_name else "A map of these files")
    generated = time.strftime("%d %B %Y", time.localtime(time.time()))
    lines = [f"# {title}", f"Generated {generated}", ""]
    lines.append(data_timestamp_sentence(generated_at))
    lines.append("")
    lines.append(
        "This is a map, not the files themselves - names, locations and "
        "how much is in each place, so whoever is holding this can find "
        "what they are looking for without having to search."
    )
    lines.append("")

    included = [s for s in sources if s.include]
    if not included:
        lines.append("Nothing to show - every source was left out of this map.")
        return "\n".join(lines)

    by_kind: dict[str, list[SourceSummary]] = {}
    for source in included:
        by_kind.setdefault(source.kind, []).append(source)

    kind_order = ["local", "drive", "network", "cloud", "phone", "archived"]
    ordered_kinds = [k for k in kind_order if k in by_kind]
    ordered_kinds += sorted(k for k in by_kind if k not in kind_order)

    for kind in ordered_kinds:
        lines.append(f"## {_kind_heading(kind)}")
        lines.append("")
        for source in by_kind[kind]:
            lines.append(_source_paragraph(source))
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"
