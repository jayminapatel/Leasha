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
from app.core.row_facts import archived_message_sql

__all__ = [
    "DuplicateCopy",
    "DuplicateGroup",
    "NearDuplicatePhotoGroup",
    "SourceUniqueness",
    "SourceDuplicateShare",
    "SpaceFindings",
    "SpaceDocument",
    "document_for",
    "size_words",
    "formatted_date",
    "find_duplicate_groups",
    "total_reclaimable_bytes",
    "find_near_duplicate_photo_groups",
    "find_source_uniqueness",
    "find_source_duplicate_share",
    "hash_coverage",
    "coverage_sentence",
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
class NearDuplicatePhotoGroup:
    """Photos that look like the same picture without being the same bytes
    - a recompression, a resize, a re-save by a different program.

    **Deliberately carries no `reclaimable_bytes`.** Unlike `DuplicateGroup`,
    these are not byte-identical: deleting all but one can lose real
    information (a higher resolution, a different crop), so this report
    states what was found and leaves the choice to the person rather than
    asserting a space saving it cannot guarantee.
    """

    representative_phash: str
    copies: tuple[DuplicateCopy, ...]
    #: One size per copy, same order as `copies` - shown, not summed, for
    #: the reason `reclaimable_bytes` does not exist on this class.
    sizes_bytes: tuple[int, ...]


@dataclass(frozen=True)
class SourceDuplicateShare:
    """One source, and how much of what it holds also exists elsewhere.

    The companion question to `SourceUniqueness`: that one finds what is
    irreplaceable, this one finds what is redundant - "40% of what's on
    this drive is duplicated somewhere else" is the number that tells
    somebody a source is safe to retire, the way `SourceUniqueness` tells
    them a source is not.
    """

    name: str
    kind: str
    status: str = ""
    #: Files on this source whose content exists on at least one other
    #: source or elsewhere on this one - i.e. genuinely duplicated, not
    #: merely present.
    duplicate_count: int = 0
    total_count: int = 0

    @property
    def share(self) -> float:
        """0.0-1.0. 0.0 on a source with nothing indexed, not a division
        error - an empty source is not a duplicated one."""
        return (self.duplicate_count / self.total_count) if self.total_count else 0.0


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


@dataclass(frozen=True)
class SpaceFindings:
    """Everything the Space Report found, before any of it is worded.

    The document (`render_space_document`) and the interactive table on the
    Reports page (`app/ui/widgets/space_table.py`) are two ways of showing
    this one object - so they cannot disagree about what was found.
    """

    groups: tuple[DuplicateGroup, ...] = ()
    near_duplicates: tuple[NearDuplicatePhotoGroup, ...] = ()
    duplicate_share: tuple[SourceDuplicateShare, ...] = ()
    uniqueness: tuple[SourceUniqueness, ...] = ()
    total_reclaimable: int = 0
    generated_at: Optional[int] = None
    #: How many of the index's files Leasha has read the contents of, out of
    #: how many there are. Both 0 means "not measured" (an empty index, or a
    #: caller that predates this). See `coverage_sentence`.
    files_total: int = 0
    files_compared: int = 0


class SpaceDocument(str):
    """The rendered Markdown document, which also remembers the findings it
    was rendered from.

    A plain `str` to everything that only wants the document - Export writes
    it to a PDF, `app.cli report space` prints it, the tests compare it - and
    it carries `findings` for the one caller that wants the same facts as a
    sortable table. One object, so what is exported and what is on screen
    come from the same query run.
    """

    findings: SpaceFindings

    def __new__(cls, text: str, findings: SpaceFindings) -> "SpaceDocument":
        obj = super().__new__(cls, text)
        obj.findings = findings
        return obj


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


#: A message read out of a mail archive - no size of its own (2026-10-04).
_ARCHIVED_MESSAGE = archived_message_sql("")


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
        # 2026-10-04: a message inside a mail archive carries the archive's
        # size (`row_facts.archived_message_sql`), so two copies of one
        # message were "reclaimable" at the size of the whole `.pst`. They are
        # left out of every size here - there is no file of theirs to delete.
        hashes = store.conn.execute(
            "SELECT content_hash, size_bytes, COUNT(*) AS n "
            "FROM files WHERE content_hash IS NOT NULL "
            f"AND NOT {_ARCHIVED_MESSAGE} "
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
                "SELECT path, volume_id FROM files WHERE content_hash = ? "
                f"AND NOT {_ARCHIVED_MESSAGE}",
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
            f"  WHERE content_hash IS NOT NULL AND NOT {_ARCHIVED_MESSAGE} "
            "  GROUP BY content_hash "
            "  HAVING COUNT(*) > 1"
            ")"
        ).fetchone()
    except Exception as exc:                      # noqa: BLE001
        _log.debug("could not total reclaimable bytes: {}", exc)
        return 0
    return int(row["reclaimable"] or 0) if row else 0


def _cluster_by_phash(rows: Sequence[Any], threshold: int) -> list[list[Any]]:
    r"""Greedy clustering of `rows` by pHash - each row joins the first
    earlier cluster whose *representative* (its first member) is within
    `threshold` bits, else starts its own. Exactly the rule
    `app.search.folding.phash_distance` defines, only faster.

    The rule is unchanged from the version that called `phash_distance` once
    per pair; what changed is the cost. Hashes are held as 64-bit integers
    and one photo is compared against every representative in a single
    NumPy XOR + popcount, instead of parsing two hex strings per pair. A
    hash that is not 64-bit hex is its own cluster and never matches
    anything - `phash_distance`'s own answer for a malformed one (999).
    Falls back to `int.bit_count` per pair where NumPy's `bitwise_count`
    (NumPy 2) is missing: slower, same clusters.
    """
    clusters: list[list[Any]] = []
    try:
        import numpy as np
        popcount = np.bitwise_count
    except (ImportError, AttributeError):
        np = None
        popcount = None

    reps = np.empty(max(1, len(rows)), dtype=np.uint64) if np is not None else None
    rep_ints: list[int] = []            # the fallback's representatives
    rep_cluster: list[int] = []         # representative slot -> index in `clusters`
    for row in rows:
        try:
            value = int(str(row["phash"]), 16)
            if value < 0 or value >= 1 << 64:
                raise ValueError
        except ValueError:
            clusters.append([row])
            continue
        slot = -1
        count = len(rep_cluster)
        if count:
            if reps is not None:
                hits = np.flatnonzero(popcount(reps[:count] ^ np.uint64(value)) <= threshold)
                slot = int(hits[0]) if hits.size else -1
            else:
                slot = next((i for i, r in enumerate(rep_ints)
                             if (r ^ value).bit_count() <= threshold), -1)
        if slot >= 0:
            clusters[rep_cluster[slot]].append(row)
            continue
        if reps is not None:
            reps[count] = value
        else:
            rep_ints.append(value)
        rep_cluster.append(len(clusters))
        clusters.append([row])
    return clusters


def find_near_duplicate_photo_groups(
    store: Any, *, limit: int = DUPLICATE_GROUPS_SHOWN,
) -> list[NearDuplicatePhotoGroup]:
    r"""§3a: photos that are the same picture without being the same bytes.

    Reuses `app.search.folding.phash_distance` and `PHASH_NEAR_THRESHOLD`
    rather than a second near-duplicate rule - the same threshold that
    decides two search results are "the same photo" decides it here.

    **One representative row per distinct `content_hash`.** A photo with
    three byte-identical copies is one entry in the clustering, not three -
    those three are already `find_duplicate_groups`' own finding, and
    counting them again here would double-report the same bytes as two
    different kinds of duplicate. This groups distinct *versions* of a
    picture, which is the gap `find_duplicate_groups` cannot see at all.

    **Still every distinct photo hash against every cluster so far - but as
    one vectorised XOR-and-popcount per photo (`_cluster_by_phash`), not one
    Python call per pair.** Measured on the 200,000-file scale fixture
    (`tests/fixtures/space_scale.py`, ~12,700 distinct photo hashes): the
    pair-at-a-time version this replaced would have taken minutes, and was
    already 13 s at a quarter of that scale. See order 0n section 3c's note.
    """
    try:
        rows = store.conn.execute(
            "SELECT MIN(id) AS id, phash, MIN(path) AS path, "
            "MAX(size_bytes) AS size_bytes, MIN(volume_id) AS volume_id "
            "FROM files WHERE phash IS NOT NULL AND phash != '' "
            "AND content_hash IS NOT NULL "
            "GROUP BY content_hash"
        ).fetchall()
    except Exception as exc:                      # noqa: BLE001
        _log.debug("could not read photo hashes for near-duplicate matching: {}", exc)
        return []

    from app.search.folding import PHASH_NEAR_THRESHOLD

    clusters = _cluster_by_phash(rows, PHASH_NEAR_THRESHOLD)

    volumes = _volume_lookup(store)
    groups: list[NearDuplicatePhotoGroup] = []
    for cluster in clusters:
        if len(cluster) < 2:
            continue
        copies = []
        sizes = []
        for member in cluster:
            volume_id = member["volume_id"]
            size = int(member["size_bytes"] or 0)
            sizes.append(size)
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
        groups.append(NearDuplicatePhotoGroup(
            representative_phash=str(cluster[0]["phash"]),
            copies=tuple(copies), sizes_bytes=tuple(sizes),
        ))
    groups.sort(key=lambda g: -sum(g.sizes_bytes))
    return groups[:limit]


def find_source_duplicate_share(store: Any) -> list[SourceDuplicateShare]:
    r"""§3a: per source, how much of what it holds is duplicated elsewhere -
    the companion question to `find_source_uniqueness`.

    A file counts as duplicated when its `content_hash` appears more than
    once anywhere in the index, whichever source each copy is on - the same
    "genuinely duplicated, not merely present" definition
    `find_duplicate_groups` already uses.
    """
    try:
        totals = store.conn.execute(
            "SELECT volume_id, COUNT(*) AS n FROM files GROUP BY volume_id"
        ).fetchall()
        duplicated = store.conn.execute(
            "SELECT f.volume_id AS volume_id, COUNT(*) AS n "
            "FROM files f JOIN ("
            "  SELECT content_hash FROM files WHERE content_hash IS NOT NULL "
            "  GROUP BY content_hash HAVING COUNT(*) > 1"
            ") d ON d.content_hash = f.content_hash "
            "GROUP BY f.volume_id"
        ).fetchall()
    except Exception as exc:                      # noqa: BLE001
        _log.debug("could not compute source duplicate share: {}", exc)
        return []

    total_by_volume = {row["volume_id"]: int(row["n"] or 0) for row in totals}
    dup_by_volume = {row["volume_id"]: int(row["n"] or 0) for row in duplicated}

    volumes = _volume_lookup(store)
    results: list[SourceDuplicateShare] = []
    local_total = total_by_volume.pop(None, 0)
    local_dup = dup_by_volume.pop(None, 0)
    for volume_id, total_count in total_by_volume.items():
        if volume_id is None or int(volume_id) not in volumes:
            continue
        volume = volumes[int(volume_id)]
        results.append(SourceDuplicateShare(
            name=str(volume.get("name") or ""), kind=str(volume.get("kind") or ""),
            status=str(volume.get("status") or "").lower(),
            duplicate_count=dup_by_volume.get(volume_id, 0), total_count=total_count,
        ))
    results.sort(key=lambda s: -s.share)
    if local_total:
        results.append(SourceDuplicateShare(
            name="This computer", kind="local",
            duplicate_count=local_dup, total_count=local_total))
    return results


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


def hash_coverage(store: Any) -> dict[str, int]:
    r"""How much of the index the duplicate findings can possibly cover.

    Duplicates and "the only copy" both come from `content_hash`, which is
    empty for a file whose contents have not been read (a name-only pass, a
    first run still going). An empty column must not be reported as an empty
    finding: **"no duplicates were found" is a statement about the owner's
    files, and with nothing compared it is not one this report can make.**
    Returned as keyword arguments for `SpaceFindings`; never raises.
    """
    try:
        total = store.conn.execute(
            "SELECT COUNT(*) FROM files WHERE source_kind = 'file'").fetchone()[0]
        compared = store.conn.execute(
            "SELECT COUNT(*) FROM files WHERE source_kind = 'file' "
            "AND content_hash IS NOT NULL").fetchone()[0]
    except Exception as exc:                      # noqa: BLE001 - a note, not the report
        _log.debug("could not measure how much has been compared: {}", exc)
        return {"files_total": 0, "files_compared": 0}
    return {"files_total": int(total or 0), "files_compared": int(compared or 0)}


def coverage_sentence(files_total: int, files_compared: int) -> str:
    """What the report cannot say yet, in plain words - empty when it can say it all."""
    total, compared = int(files_total or 0), int(files_compared or 0)
    if total <= 0 or compared >= total:
        return ""
    if compared <= 0:
        return (f"None of the {total:,} {'file' if total == 1 else 'files'} has been compared "
                "with the others yet - Leasha compares files by what is inside them, and has "
                "not read these - so this report cannot say what is duplicated or what exists "
                "only once.")
    return (f"Leasha has compared {compared:,} of {total:,} files. The other {total - compared:,} "
            "have not been read yet, so everything below covers only the ones that have.")


def _size_words(size_bytes: int) -> str:
    # 2026-10-04, the owner: one size formatter everywhere (`row_facts`).
    from app.core.row_facts import format_size

    return format_size(size_bytes)


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
    near_duplicates: Sequence[NearDuplicatePhotoGroup] = (),
    duplicate_share: Sequence[SourceDuplicateShare] = (),
    files_total: int = 0, files_compared: int = 0,
) -> str:
    r"""§3a and §3b, as one document - the whole-corpus headline first,
    then the largest duplicate groups, then similar photos and the
    per-source share, then the uniqueness warning last (read last, acted
    on first - the order does not change which finding matters more).

    `near_duplicates` and `duplicate_share` default to empty rather than
    being required, so every existing caller (`app.cli report space`
    among them, built before this pass) keeps producing the document it
    always did until it is updated to pass the new findings too.
    """
    generated = time.strftime("%d %B %Y", time.localtime(time.time()))
    lines = [f"# The Space Report", f"Generated {generated}", ""]
    if generated_at:
        when = time.strftime("%d %B %Y", time.localtime(int(generated_at)))
        lines.append(f"From the index as of last run, {when}.")
    else:
        lines.append("This index has nothing recorded yet.")
    lines.append("")
    thin = coverage_sentence(files_total, files_compared)
    if thin:
        lines.extend([thin, ""])

    lines.append("## Duplicates")
    lines.append("")
    if not groups and not total_reclaimable:
        lines.append("Nothing can be said about duplicates yet." if files_total and not files_compared
                     else "No duplicate files were found.")
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

    if near_duplicates:
        lines.append("## Similar photos")
        lines.append("")
        lines.append(
            "The same picture, saved more than once with different bytes - a "
            "resize, a recompression, a re-save by a different program. "
            "Deleting all but one is not automatically safe here the way it "
            "is above: a different version may be a different resolution or "
            "crop, so nothing is totalled as reclaimable."
        )
        lines.append("")
        for group in near_duplicates:
            lines.append(f"**{len(group.copies)} versions of the same picture:**")
            for copy, size in zip(group.copies, group.sizes_bytes):
                lines.append(_copy_line(copy) + f" ({_size_words(size)})")
            lines.append("")

    if duplicate_share:
        lines.append("## Duplication by source")
        lines.append("")
        lines.append("How much of what each source holds also exists elsewhere.")
        lines.append("")
        for source in duplicate_share:
            status = (f", currently {source.status}"
                      if source.status and source.status not in ("", "online") else "")
            lines.append(
                f"- **{source.name!r}{status}**: {source.duplicate_count:,} of "
                f"{source.total_count:,} files ({source.share:.0%}) are "
                f"duplicated elsewhere")
        lines.append("")

    lines.append("## The only copy")
    lines.append("")
    if not uniqueness:
        lines.append("Nothing can be said about what exists only once yet."
                     if files_total and not files_compared
                     else "Nothing in the index exists on exactly one source yet.")
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


#: Public names for the two formatters the Reports table shares with the
#: document, so the words cannot drift apart.
size_words = _size_words
formatted_date = _formatted_date


def document_for(findings: SpaceFindings) -> SpaceDocument:
    """`render_space_document` over `findings`, keeping them attached."""
    text = render_space_document(
        findings.groups, findings.uniqueness,
        total_reclaimable=findings.total_reclaimable,
        generated_at=findings.generated_at,
        near_duplicates=findings.near_duplicates,
        duplicate_share=findings.duplicate_share,
        files_total=findings.files_total, files_compared=findings.files_compared)
    return SpaceDocument(text, findings)
