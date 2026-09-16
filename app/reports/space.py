r"""The Space Report: duplicates and uniqueness, across every source.

Layer: L4 (reports) - reads L1 (`SqliteStore`) only, never writes to it and
never touches a user file. Non-negotiable #10 applies in full, the same
guarantee `app/reports/inheritance.py` keeps.

Order 202626270602 (0n) section 3. Two questions, from the same
`content_hash` column, answered in opposite directions:

- **3a**: which content is copied needlessly, and how much space would
  keeping one copy of each actually reclaim?
- **3b, "the backup conscience"**: which content exists exactly once in
  the whole index, and which source is the only place it lives - because
  if that source is a drive in a drawer, that is the content a broken
  drive takes with it.

Both questions are answered per source (a local root, or a catalogued
Offline Media volume) rather than per file, because "372 files, one
drive" is the number somebody can act on and a list of 372 paths is not.

**Scope, stated plainly rather than left implicit**: local-root
uniqueness is reported as one "on this computer" bucket, not split by
individual root. A local root does not disappear the way a drive in a
drawer does, and splitting it would cost a per-row path-prefix match
against every uniquely-hashed local file for a distinction this report's
own "backup conscience" framing does not need. Volumes - the ones a
broken drive or a lost tape actually threatens - are reported individually,
and rank first in the document for exactly that reason.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Sequence

from app.core.logging import logger

__all__ = [
    "DuplicateCopy",
    "DuplicateGroup",
    "SourceUniqueness",
    "find_duplicate_groups",
    "total_reclaimable_bytes",
    "find_source_uniqueness",
    "render_space_document",
    "DUPLICATE_GROUPS_SHOWN",
]

_log = logger.bind(component="reports.space")

#: How many of the largest duplicate groups are named individually - a
#: corpus with thousands of duplicate groups (a photo library synced
#: twice, say) must not turn one report into a document nobody reads to
#: the end. Mirrors `inheritance.TOP_FOLDERS_SHOWN`'s own reasoning.
DUPLICATE_GROUPS_SHOWN = 25


@dataclass(frozen=True)
class DuplicateCopy:
    """One copy of a duplicated file - where it lives, told the same way
    `inheritance.SourceSummary` already tells a source."""

    path: str
    source_name: str
    #: "local" or a `volumes.kind` value - see `SourceSummary`'s own note.
    source_kind: str
    #: "" | "offline" | "locked" | "archived" - a local copy carries none.
    source_status: str = ""


@dataclass(frozen=True)
class DuplicateGroup:
    """Every copy of one piece of content, biggest-reclaim first."""

    content_hash: str
    size_bytes: int
    copies: tuple[DuplicateCopy, ...]

    @property
    def reclaimable_bytes(self) -> int:
        """Keeping one copy reclaims every other one."""
        return max(0, len(self.copies) - 1) * self.size_bytes


@dataclass(frozen=True)
class SourceUniqueness:
    """One source, and how many files exist nowhere else in the index."""

    name: str
    kind: str
    status: str = ""
    #: Unix seconds - a volume's last scan, so "last seen 14 Aug" is
    #: answerable for a source that is not currently connected.
    last_seen: Optional[int] = None
    file_count: int = 0


def _volume_lookup(store: Any) -> dict[int, dict[str, Any]]:
    """Every catalogued volume, keyed by id - one query, reused for every
    per-file source lookup below rather than one query per file."""
    try:
        rows = store.list_volumes()
    except Exception as exc:                      # noqa: BLE001 - see module docstring
        _log.debug("could not list catalogued volumes: {}", exc)
        rows = []
    return {int(row["id"]): row for row in rows if row.get("id") is not None}


def _local_source_name(path: str) -> str:
    """A plain name for "this computer" as a source - see the module
    docstring's scope note on why local files are one bucket."""
    return "This computer"


def find_duplicate_groups(
    store: Any, *, limit: int = DUPLICATE_GROUPS_SHOWN,
) -> list[DuplicateGroup]:
    r"""§3a: every piece of content that exists more than once, biggest
    reclaim first. Capped at `limit` groups - `total_reclaimable_bytes`
    below answers the whole-corpus number without needing every group
    materialised.

    Reads `idx_files_content_hash` (schema v24) for the `GROUP BY` this
    query is built on - see that migration's own docstring for the H2
    lesson it exists to avoid repeating.
    """
    try:
        hashes = store.conn.execute(
            "SELECT content_hash, size_bytes, COUNT(*) AS n "
            "FROM files WHERE content_hash IS NOT NULL "
            "GROUP BY content_hash HAVING COUNT(*) > 1 "
            "ORDER BY (COUNT(*) - 1) * size_bytes DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
    except Exception as exc:                      # noqa: BLE001
        _log.debug("could not compute duplicate groups: {}", exc)
        return []

    volumes = _volume_lookup(store)
    groups: list[DuplicateGroup] = []
    for row in hashes:
        content_hash = row["content_hash"]
        try:
            members = store.conn.execute(
                "SELECT path, volume_id FROM files WHERE content_hash = ?",
                (content_hash,),
            ).fetchall()
        except Exception as exc:                  # noqa: BLE001
            _log.debug("could not list copies of {}: {}", content_hash, exc)
            continue
        copies = []
        for member in members:
            volume_id = member["volume_id"]
            if volume_id is not None and int(volume_id) in volumes:
                volume = volumes[int(volume_id)]
                copies.append(DuplicateCopy(
                    path=member["path"], source_name=str(volume.get("name") or ""),
                    source_kind=str(volume.get("kind") or ""),
                    source_status=str(volume.get("status") or "").lower(),
                ))
            else:
                copies.append(DuplicateCopy(
                    path=member["path"], source_name=_local_source_name(member["path"]),
                    source_kind="local",
                ))
        groups.append(DuplicateGroup(
            content_hash=content_hash, size_bytes=int(row["size_bytes"] or 0),
            copies=tuple(copies),
        ))
    return groups


def total_reclaimable_bytes(store: Any) -> int:
    """The whole-corpus number §3a's headline needs - every duplicate
    group, not only the `DUPLICATE_GROUPS_SHOWN` named individually."""
    try:
        row = store.conn.execute(
            "SELECT SUM((n - 1) * size_bytes) AS reclaimable FROM ("
            "  SELECT size_bytes, COUNT(*) AS n FROM files "
            "  WHERE content_hash IS NOT NULL GROUP BY content_hash "
            "  HAVING COUNT(*) > 1"
            ")"
        ).fetchone()
    except Exception as exc:                      # noqa: BLE001
        _log.debug("could not total reclaimable bytes: {}", exc)
        return 0
    return int(row["reclaimable"] or 0) if row else 0


def find_source_uniqueness(store: Any) -> list[SourceUniqueness]:
    r"""§3b, "the backup conscience": content that exists on exactly one
    source, counted per source - volumes first, since those are the ones
    a broken drive or a lost tape can actually take with it.

    A file counts here only when its `content_hash` appears nowhere else
    in the whole index - genuinely irreplaceable from this index's own
    evidence, not merely "the only copy this report happened to show."
    """
    try:
        rows = store.conn.execute(
            "SELECT f.volume_id AS volume_id, COUNT(*) AS n "
            "FROM files f JOIN ("
            "  SELECT content_hash FROM files WHERE content_hash IS NOT NULL "
            "  GROUP BY content_hash HAVING COUNT(*) = 1"
            ") u ON u.content_hash = f.content_hash "
            "GROUP BY f.volume_id"
        ).fetchall()
    except Exception as exc:                      # noqa: BLE001
        _log.debug("could not compute source uniqueness: {}", exc)
        return []

    volumes = _volume_lookup(store)
    results: list[SourceUniqueness] = []
    local_count = 0
    for row in rows:
        volume_id = row["volume_id"]
        count = int(row["n"] or 0)
        if volume_id is None:
            local_count += count
            continue
        if int(volume_id) not in volumes:
            continue                              # a volume since forgotten; nothing to name
        volume = volumes[int(volume_id)]
        results.append(SourceUniqueness(
            name=str(volume.get("name") or ""), kind=str(volume.get("kind") or ""),
            status=str(volume.get("status") or "").lower(),
            last_seen=volume.get("last_scanned_at"), file_count=count,
        ))
    # Volumes first (the backup conscience's own priority), then "This
    # computer" last - a local root is not the finding this report exists
    # to raise, but it is still true and still worth stating.
    results.sort(key=lambda s: -s.file_count)
    if local_count:
        results.append(SourceUniqueness(
            name="This computer", kind="local", file_count=local_count))
    return results


def _size_words(size_bytes: int) -> str:
    value = float(max(0, size_bytes))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:,.0f} {unit}" if unit == "B" else f"{value:,.1f} {unit}"
        value /= 1024
    return f"{value:,.1f} TB"


def _formatted_date(value: Optional[int]) -> str:
    if not value:
        return ""
    return time.strftime("%d %b %Y", time.localtime(int(value)))


def _copy_line(copy: DuplicateCopy) -> str:
    name = Path(copy.path).name or copy.path
    if copy.source_kind == "local":
        where = "this computer"
    else:
        status = (f", currently {copy.source_status}"
                  if copy.source_status and copy.source_status != "online" else "")
        where = f"{copy.source_name!r}{status}"
    return f"  - {name} - on {where}"


def render_space_document(
    groups: Sequence[DuplicateGroup], uniqueness: Sequence[SourceUniqueness],
    *, total_reclaimable: int = 0, generated_at: Optional[int] = None,
) -> str:
    r"""§3a and §3b, as one document - the whole-corpus headline first,
    then the largest duplicate groups, then the uniqueness warning last
    (read last, acted on first - the order does not change which finding
    matters more).
    """
    generated = time.strftime("%d %B %Y", time.localtime(time.time()))
    lines = [f"# The Space Report", f"Generated {generated}", ""]
    if generated_at:
        when = time.strftime("%d %B %Y", time.localtime(int(generated_at)))
        lines.append(f"From the index as of last run, {when}.")
    else:
        lines.append("This index has nothing recorded yet.")
    lines.append("")

    lines.append("## Duplicates")
    lines.append("")
    if not groups and not total_reclaimable:
        lines.append("No duplicate files were found.")
    else:
        lines.append(
            f"Keeping one copy of everything duplicated would reclaim "
            f"**{_size_words(total_reclaimable)}**.")
        lines.append("")
        lines.append(f"The {min(len(groups), DUPLICATE_GROUPS_SHOWN)} largest:")
        lines.append("")
        for group in groups:
            example = Path(group.copies[0].path).name if group.copies else "(unknown)"
            lines.append(
                f"**{example}** - {len(group.copies)} copies, "
                f"{_size_words(group.size_bytes)} each, "
                f"{_size_words(group.reclaimable_bytes)} reclaimable")
            for copy in group.copies:
                lines.append(_copy_line(copy))
            lines.append("")

    lines.append("## The only copy")
    lines.append("")
    if not uniqueness:
        lines.append("Nothing in the index exists on exactly one source yet.")
    else:
        for source in uniqueness:
            when = _formatted_date(source.last_seen)
            seen = f" (last seen {when})" if when else ""
            status = (f", currently {source.status}"
                      if source.status and source.status not in ("", "online") else "")
            noun = "file" if source.file_count == 1 else "files"
            lines.append(
                f"**{source.file_count:,} {noun} exist nowhere else but "
                f"{source.name!r}{status}{seen}.**")
        lines.append("")
        lines.append(
            "This index has no other copy of any of these - if that source "
            "is lost, so is the content.")

    return "\n".join(lines).rstrip() + "\n"
