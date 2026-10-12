"""The indexed files the local enrichment sources read: vCards (5a) and calendar invitations (5d).

Layer: L7

Order 1s section 5. The index knows *which* `.vcf` and `.ics` files exist (the `files` table, `ext`
without the dot); their structure - one card, one attendee - is not in `chunks` in any form a parser
can trust, because the chunker cuts text at passage boundaries, not at `END:VCARD`. So these sources
re-read the file itself from disk, **read-only** (non-negotiable 10: opened with `open(..., "rb")`,
never written, moved or touched).

Only `source_kind = 'file'` rows are read: a vCard inside a zip or attached to a message has no path
on disk to open. A file on an external drive is resolved through `offline_media.resolve_file_path`,
the one place that turns a volume row into today's drive letter; a drive that is not connected is
skipped. A file that has gone or cannot be read is skipped and logged, never an error: enrichment is
a convenience, and one stale row must not cost the person every other suggestion.

**Parsed once, not once a contact.** `enrich_selection` asks each source about one contact at a time;
re-reading 146 vCards for each of 1,445 contacts would be 200,000 file reads. `cached` keeps what
each file parsed to, keyed by its path, size and modified time - a file that changed is parsed again,
one that did not is never re-read.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Optional, Sequence, TypeVar

from app.core.logging import logger

__all__ = ["LocalFile", "indexed_files", "read_bytes", "cached", "MAX_FILE_BYTES", "clear_cache"]

_log = logger.bind(component="contacts.enrich.files")

#: A vCard or calendar file larger than this is not read. Not a setting: a real address book with
#: photos is a few megabytes, and a 100 MB ".vcf" is a backup of something else, which a person would
#: not expect enrichment to load into memory a contact at a time. Raise it if a real export is cut.
MAX_FILE_BYTES = 16 * 1024 * 1024

T = TypeVar("T")


@dataclass(frozen=True)
class LocalFile:
    path: Path                                           # where it is on disk now
    shown: str                                           # what the receipt names: the indexed path
    mtime: int                                           # Unix seconds, the receipt's date and `Proposal.when`
    size: int


def indexed_files(store: Any, exts: Sequence[str], *, since: Optional[int] = None) -> list[LocalFile]:
    """The indexed files with one of `exts` ("vcf", no dot), newest first.

    `since` is the age window (order 1s D1) applied to the file's modified date: a file last changed
    before it is not read at all."""
    wanted = tuple(e.lower().lstrip(".") for e in exts)
    if not wanted:
        return []
    marks = ",".join("?" for _ in wanted)
    sql = (f"SELECT id, path, mtime_ns, size_bytes, volume_id, relative_path FROM files "
           f"WHERE ext IN ({marks}) AND source_kind = 'file' AND status = 'INDEXED' ")
    params: list[Any] = list(wanted)
    if since is not None:
        sql += "AND mtime_ns >= ? "
        params.append(int(since) * 1_000_000_000)
    sql += "ORDER BY mtime_ns DESC, id"
    out: list[LocalFile] = []
    connected: Optional[dict] = None
    for r in store.conn.execute(sql, params):
        if r["volume_id"] is None:
            path: Optional[Path] = Path(r["path"])
        else:
            try:                                         # only for a drive row: it enumerates volumes
                from app.index.offline_media import connected_volumes, resolve_file_path  # noqa: PLC0415
                if connected is None:
                    connected = connected_volumes(store)
                path = resolve_file_path(store, SimpleNamespace(path=r["path"], volume_id=r["volume_id"],
                                                                relative_path=r["relative_path"]), connected)
            except Exception as exc:                     # noqa: BLE001 - a drive we cannot see is skipped
                _log.info("enrichment skipped {}: its drive could not be resolved ({})", r["path"], exc)
                path = None
        if path is None:
            continue
        out.append(LocalFile(path, str(r["path"]), int(r["mtime_ns"] or 0) // 1_000_000_000,
                             int(r["size_bytes"] or 0)))
    return out


def read_bytes(f: LocalFile) -> Optional[bytes]:
    """The file's bytes, read-only, or None (logged) when it is gone, unreadable or too big."""
    try:
        size = f.path.stat().st_size
        if size > MAX_FILE_BYTES:
            _log.info("enrichment skipped {}: {:,} bytes is more than {:,}", f.shown, size, MAX_FILE_BYTES)
            return None
        with open(f.path, "rb") as fh:
            return fh.read(MAX_FILE_BYTES + 1)
    except OSError as exc:
        _log.info("enrichment skipped {}: {}", f.shown, exc)
        return None


_CACHE: dict[tuple[str, str, int, int], Any] = {}


def cached(kind: str, f: LocalFile, parse: Callable[[bytes], T]) -> Optional[T]:
    """`parse(read_bytes(f))`, remembered until the file changes. None when the file cannot be read or parsed."""
    key = (kind, str(f.path), f.size, f.mtime)
    if key in _CACHE:
        return _CACHE[key]
    data = read_bytes(f)
    value: Optional[T] = None
    if data is not None:
        try:
            value = parse(data)
        except Exception as exc:                         # noqa: BLE001 - one odd file, not a failed run
            _log.info("enrichment could not read {}: {}", f.shown, exc)
            value = None
    _CACHE[key] = value
    return value


def clear_cache() -> None:
    _CACHE.clear()
