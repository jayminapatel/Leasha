r"""What the Tidy page offers: copies, similar photos, large files, leftovers. Read-only.

Layer: L4 (tidy) - reads L1 (`SqliteStore`) only, never writes to it and never
touches a user file. The one module in `app/tidy/` that changes anything is
`remove.py`.

Order 1q (2026-10-11). The Space Report (`app/reports/space.py`) answers the same
questions for every source; Tidy asks them of **this computer only** (D1:
`source_kind = 'file'`, `volume_id IS NULL`), because only a file here can be
moved to this computer's Recycle Bin. A copy on a catalogued drive is still
*named* (`TidySet.elsewhere`) - the owner should know it is there - but never
offered, and never counted as the copy that stays (D2).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Optional, Sequence

from app.core.logging import logger

__all__ = [
    "TidyFile", "TidySet", "Overview", "LOCAL", "LEFTOVER_EXTS", "INSTALLER_EXTS",
    "LARGE_SHOWN", "LARGE_MIN_BYTES", "SETS_SHOWN",
    "copy_sets", "copy_totals", "similar_sets", "large_files", "leftovers", "overview",
    "sets_for_file", "is_leftover",
]

_log = logger.bind(component="tidy.find")

#: D1 as SQL: a plain file on this computer, and not one Tidy has already moved to the
#: Recycle Bin (its row stays until the watcher or the next index run forgets it, as
#: for a file deleted in Explorer). `f.` because every query aliases `files`.
LOCAL = ("f.source_kind = 'file' AND f.volume_id IS NULL AND f.path NOT IN "
         "(SELECT path FROM tidy_journal WHERE put_back_at IS NULL AND bin_ref NOT LIKE 'pending:%')")
#: D10. Partial downloads and temporary files, anywhere.
LEFTOVER_EXTS = frozenset({"crdownload", "part", "partial", "tmp", "download"})
#: D10. Installers, but only in a Downloads folder - an installer kept elsewhere was kept on purpose.
INSTALLER_EXTS = frozenset({"msi", "msix", "exe"})
#: How many sets a list loads at once; the page asks for the next page as it is read.
SETS_SHOWN = 200
#: How many of the biggest files "Large files" lists.
LARGE_SHOWN = 200
#: Below this a file is not "large", however few files there are: on a small index the
#: top 200 is every file, which says nothing (seen grabbing the page, 2026-10-11).
LARGE_MIN_BYTES = 100 * 1024 ** 2
_COLUMNS = "f.id, f.path, f.size_bytes, f.mtime_ns, f.content_hash, f.phash, f.taken_at_ns, f.ext"


@dataclass(frozen=True)
class TidyFile:
    """One file on this computer, as the index last saw it."""

    id: int
    path: str
    size_bytes: int
    mtime_ns: int
    content_hash: Optional[str] = None
    phash: Optional[str] = None
    taken_at_ns: Optional[int] = None
    ext: str = ""

    @property
    def name(self) -> str:
        return os.path.basename(self.path)


@dataclass(frozen=True)
class TidySet:
    """Files that belong together on one Tidy screen.

    `kind` is `copies` (the same bytes), `similar` (the same picture), `large` or
    `leftovers` (a list to choose from - D10). `elsewhere` names copies on
    catalogued drives, which are shown and never offered.
    """

    key: str
    kind: str
    files: tuple[TidyFile, ...]
    elsewhere: tuple[str, ...] = ()

    @property
    def size_bytes(self) -> int:
        return sum(f.size_bytes for f in self.files)

    @property
    def spare_bytes(self) -> int:
        """What keeping one of an exact-copy set gives back; 0 for every other kind."""
        if self.kind != "copies" or len(self.files) < 2:
            return 0
        return (len(self.files) - 1) * self.files[0].size_bytes


@dataclass(frozen=True)
class Overview:
    """What is indexed, for the page's first entry: counts and sizes by kind and by place."""

    files_here: int = 0
    bytes_here: int = 0
    files_on_drives: int = 0
    messages: int = 0
    photos_here: int = 0
    kinds: tuple[tuple[str, int, int], ...] = ()          # (ext, files, bytes), most bytes first
    places: tuple[tuple[str, int, int], ...] = ()         # (saved root, files, bytes)
    drives: tuple[tuple[str, int], ...] = field(default_factory=tuple)


def _file(row: Any) -> TidyFile:
    return TidyFile(id=int(row["id"]), path=str(row["path"]), size_bytes=int(row["size_bytes"] or 0),
                    mtime_ns=int(row["mtime_ns"] or 0), content_hash=row["content_hash"],
                    phash=row["phash"], taken_at_ns=row["taken_at_ns"], ext=str(row["ext"] or ""))


def _elsewhere(store: Any, content_hash: str) -> tuple[str, ...]:
    """Where else, off this computer, the same bytes are catalogued - "Archive 2019: Photos\\a.jpg"."""
    rows = store.conn.execute(
        "SELECT v.name, f.relative_path, f.path FROM files f JOIN volumes v ON v.id = f.volume_id "
        "WHERE f.content_hash = ? AND f.source_kind = 'file'", (content_hash,)).fetchall()
    return tuple(f"{name}: {rel or path}" for name, rel, path in rows)


def copy_sets(store: Any, *, limit: int = SETS_SHOWN, offset: int = 0) -> list[TidySet]:
    r"""Exact copies on this computer, the most room given back first.

    Grouped on `idx_files_content_hash`, as `space.find_duplicate_groups` is, but
    counting only copies here: two copies on this computer and one in a drawer is
    a set of two Tidy can choose between, and one here with one in a drawer is no
    set at all (D2).
    """
    try:
        hashes = store.conn.execute(
            "SELECT f.content_hash, MAX(f.size_bytes) AS size, COUNT(*) AS n FROM files f "
            f"WHERE f.content_hash IS NOT NULL AND f.size_bytes > 0 AND {LOCAL} "
            "GROUP BY f.content_hash HAVING COUNT(*) > 1 "
            "ORDER BY (COUNT(*) - 1) * MAX(f.size_bytes) DESC, f.content_hash LIMIT ? OFFSET ?",
            (int(limit), int(offset))).fetchall()
    except Exception as exc:                      # noqa: BLE001 - an empty list, said as such
        _log.debug("could not group copies: {}", exc)
        return []
    sets = []
    for row in hashes:
        files = [_file(r) for r in store.conn.execute(
            f"SELECT {_COLUMNS} FROM files f WHERE f.content_hash = ? AND {LOCAL} ORDER BY f.path",
            (row["content_hash"],))]
        sets.append(TidySet(key=f"copies:{row['content_hash']}", kind="copies", files=tuple(files),
                            elsewhere=_elsewhere(store, row["content_hash"])))
    return sets


def copy_totals(store: Any) -> tuple[int, int]:
    """`(sets, bytes given back by keeping one of each)` over every exact-copy set here."""
    try:
        row = store.conn.execute(
            "SELECT COUNT(*) AS sets, COALESCE(SUM((n - 1) * size), 0) AS spare FROM ("
            "  SELECT COUNT(*) AS n, MAX(f.size_bytes) AS size FROM files f "
            f"  WHERE f.content_hash IS NOT NULL AND f.size_bytes > 0 AND {LOCAL} "
            "  GROUP BY f.content_hash HAVING COUNT(*) > 1)").fetchone()
    except Exception as exc:                      # noqa: BLE001
        _log.debug("could not total copies: {}", exc)
        return 0, 0
    return int(row["sets"] or 0), int(row["spare"] or 0)


def similar_sets(store: Any, *, limit: int = SETS_SHOWN) -> list[TidySet]:
    r"""Photos on this computer that are the same picture without being the same bytes.

    The Space Report's own clustering (`space._cluster_by_phash`, the search
    folding threshold) over one photo per distinct content hash - byte-identical
    copies are `copy_sets`' finding, and are not offered twice.
    """
    from app.reports.space import _cluster_by_phash
    from app.search.folding import PHASH_NEAR_THRESHOLD

    try:
        rows = store.conn.execute(
            f"SELECT {_COLUMNS} FROM files f WHERE f.phash IS NOT NULL AND f.phash != '' "
            f"AND f.content_hash IS NOT NULL AND {LOCAL} AND f.id IN ("
            f"  SELECT MIN(f.id) FROM files f WHERE f.phash IS NOT NULL AND {LOCAL} "
            "  GROUP BY f.content_hash) ORDER BY f.id").fetchall()
    except Exception as exc:                      # noqa: BLE001
        _log.debug("could not read photo hashes: {}", exc)
        return []
    clusters = [c for c in _cluster_by_phash(rows, PHASH_NEAR_THRESHOLD) if len(c) > 1]
    sets = [TidySet(key=f"similar:{c[0]['id']}", kind="similar", files=tuple(_file(r) for r in c))
            for c in clusters]
    sets.sort(key=lambda s: (-s.size_bytes, s.key))
    return sets[:limit]


def large_files(store: Any, *, limit: int = LARGE_SHOWN, at_least: int = LARGE_MIN_BYTES) -> TidySet:
    """The biggest files on this computer, `at_least` each, as one list to choose from (D10)."""
    rows = store.conn.execute(
        f"SELECT {_COLUMNS} FROM files f WHERE {LOCAL} AND f.size_bytes >= ? "
        "ORDER BY f.size_bytes DESC, f.path LIMIT ?", (int(at_least), int(limit))).fetchall()
    return TidySet(key="large", kind="large", files=tuple(_file(r) for r in rows))


def is_leftover(path: str, ext: str) -> bool:
    """D10: a partial download, a temporary or Office owner file, or an installer in Downloads."""
    ext = (ext or "").lower().lstrip(".")
    if ext in LEFTOVER_EXTS or os.path.basename(path).startswith("~$"):
        return True
    folders = path.replace("/", "\\").lower().split("\\")[:-1]
    return ext in INSTALLER_EXTS and "downloads" in folders


def leftovers(store: Any, *, limit: int = LARGE_SHOWN * 5) -> TidySet:
    """Every leftover on this computer, biggest first (D10)."""
    exts = sorted(LEFTOVER_EXTS | INSTALLER_EXTS)
    marks = ",".join("?" * len(exts))
    rows = store.conn.execute(
        f"SELECT {_COLUMNS} FROM files f WHERE {LOCAL} AND (f.ext IN ({marks}) "
        "OR f.path LIKE '%~$%') ORDER BY f.size_bytes DESC, f.path", exts).fetchall()
    found = [_file(r) for r in rows if is_leftover(str(r["path"]), str(r["ext"] or ""))]
    return TidySet(key="leftovers", kind="leftovers", files=tuple(found[:limit]))


def overview(store: Any, roots: Sequence[str] = ()) -> Overview:
    """What is indexed: this computer by kind and by saved folder, the drives, the mail."""
    one = store.conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(f.size_bytes), 0), "
        "SUM(CASE WHEN f.phash IS NOT NULL AND f.phash != '' THEN 1 ELSE 0 END) "
        f"FROM files f WHERE {LOCAL}").fetchone()
    on_drives = store.conn.execute(
        "SELECT COUNT(*) FROM files WHERE volume_id IS NOT NULL").fetchone()[0]
    try:
        messages = store.conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    except Exception:                             # noqa: BLE001 - an index without mail
        messages = 0
    kinds = tuple((str(ext or "other"), int(n), int(size)) for ext, n, size in store.conn.execute(
        "SELECT f.ext, COUNT(*), COALESCE(SUM(f.size_bytes), 0) FROM files f "
        f"WHERE {LOCAL} GROUP BY f.ext ORDER BY 3 DESC LIMIT 12"))
    places = []
    for root in roots:
        prefix = root.rstrip("\\/") + "\\"
        n, size = store.conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(f.size_bytes), 0) FROM files f "
            f"WHERE {LOCAL} AND (f.path LIKE ? ESCAPE '!' OR f.path = ?)",
            (_like_prefix(prefix), root)).fetchone()
        places.append((root, int(n), int(size)))
    from app.storage.side_counts import drive_counts

    drives = tuple((name, count) for name, _status, count in drive_counts(store))
    return Overview(files_here=int(one[0] or 0), bytes_here=int(one[1] or 0),
                    photos_here=int(one[2] or 0), files_on_drives=int(on_drives or 0),
                    messages=int(messages or 0), kinds=kinds, places=tuple(places), drives=drives)


def sets_for_file(store: Any, path: str) -> list[TidySet]:
    """D5, "Find copies of this...": the file's exact copies here, and its similar photos."""
    row = store.conn.execute(
        f"SELECT {_COLUMNS} FROM files f WHERE f.path = ? AND {LOCAL}", (path,)).fetchone()
    if row is None:
        return []
    found: list[TidySet] = []
    if row["content_hash"]:
        files = [_file(r) for r in store.conn.execute(
            f"SELECT {_COLUMNS} FROM files f WHERE f.content_hash = ? AND {LOCAL} ORDER BY f.path",
            (row["content_hash"],))]
        found.append(TidySet(key=f"copies:{row['content_hash']}", kind="copies", files=tuple(files),
                             elsewhere=_elsewhere(store, row["content_hash"])))
    if row["phash"]:
        for candidate in similar_sets(store, limit=10_000):
            if any(f.content_hash == row["content_hash"] for f in candidate.files):
                found.append(candidate)
                break
    return found


def _like_prefix(prefix: str) -> str:
    """`prefix%` for LIKE with `!` escaping, so `_` and `%` in a folder name mean themselves."""
    return prefix.replace("!", "!!").replace("%", "!%").replace("_", "!_") + "%"
