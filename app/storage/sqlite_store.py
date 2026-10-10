"""SQLite metadata store: files, chunks, messages, FTS5, skip ledger, cursor.

Layer: L1

SQLite is the AUTHORITY. LanceDB is derived from it and can always be rebuilt
from `chunks`. Never the reverse.

Concurrency model: **one connection per thread**, one write lock. SQLite in WAL
mode allows many concurrent readers alongside a single writer, which is exactly
the shape of this application - a pool of extraction workers reading, one writer
committing. Serialising writes in the process avoids `database is locked`
entirely rather than retrying around it.

This said "one connection" until it was tested. Threads sharing a connection
share its *transaction*, so a search running during indexing read uncommitted
rows and could return a result for a document whose transaction then rolled
back. Per-thread connections are what make the WAL sentence above true rather
than aspirational.
"""

from __future__ import annotations

import re
import sqlite3
import threading
import time
import weakref
from contextlib import contextmanager
from dataclasses import dataclass
from itertools import groupby
from operator import itemgetter
from pathlib import Path
from types import TracebackType
from typing import (
    Any, Iterable, Iterator, NamedTuple, Optional, Sequence, Type,
)

from app.storage.like import contains as like_contains, has_wildcard, like_escape

from app.core.errors import AppError, AppErrorException, make_error
from app.core.identifiers import symbol_tokens


def utf8_safe(text: Any) -> Any:
    """`text` with any lone UTF-16 surrogate made encodable, else `text` itself.

    SQLite stores UTF-8, and Python's encoder refuses a lone surrogate
    (`U+D800`-`U+DFFF` on its own). One reached `replace_chunks` on
    2026-10-08 from a `.doc` whose text was decoded piece by piece, splitting
    a surrogate pair across two pieces - and the `UnicodeEncodeError` ended
    the owner's ten-minute index run. The store is the authority, so nothing
    unencodable may reach it: a split pair is put back together (the emoji it
    was), a half with no partner becomes U+FFFD, and ordinary text - the fast
    path, one `encode` - comes back untouched. Not a `str`: returned as is.
    """
    if not isinstance(text, str):
        return text
    try:
        text.encode("utf-8")
        return text
    except UnicodeEncodeError:
        return text.encode("utf-16", "surrogatepass").decode("utf-16", "replace")
from app.core.logging import logger
from app.core.row_facts import is_mail_attachment, listed_files_sql  # noqa: F401 - re-exported
from app.storage.migrations import (
    CONTENT_TRIGGERS, CURRENT_VERSION, apply_migrations, read_version,
)

__all__ = ["SqliteStore", "FileRecord", "ChunkRecord", "FileStatus", "VolumeRecord", "volume_synthetic_path", "VOLUME_PATH_SCHEME", "FaceRecord", "PileRecord", "PileSample", "PendingSuggestion", "PhotoRow"]

_log = logger.bind(component="storage.sqlite")

#: Entities sharing a first word beyond which containment merging is skipped.
#: A bucket that large is a word that begins thousands of names, and grinding
#: through it quadratically costs far more than the handful of merges it would
#: find. Chosen so the worst bucket stays under a second.
MERGE_BUCKET_LIMIT = 400

#: Characters of free text below which a browse returns nothing rather than
#: everything. **Two, and the reason is not the index.** One or two characters
#: match nearly every file, so answering them with a screenful of arbitrary rows
#: shows results that have no relation to what was typed - which reads as a
#: search that worked and gave the wrong answer. An *empty* box is a different
#: request and means "everything", newest first.
NAME_MIN_CHARS = 2

#: Passed as `repo_id` to mean **"this file is in no repository"**, as opposed
#: to `None`, which means "I have no opinion".
#:
#: From `WORKORDER-202626081149-code-tab.md` §2. Attribution was a one-way door:
#: nothing pruned `repos`, nothing ever set `files.repo_id` back to NULL, and the
#: COALESCE in `upsert_file` meant a NULL could not overwrite an attribution -
#: so even `index --force` would not clear one. Each of the three is defensible
#: alone; together the only route back was deleting the whole index, which is
#: what the owner did for what was a bookkeeping error.
#:
#: Negative because every real id is positive, and a distinct object rather than
#: a bare -1 at each call site because "what does minus one mean here" is a
#: question nobody should have to answer twice.
NO_REPO = -1

#: Work order 0x item 5d. The page cache the indexer's writing thread asks for:
#: a quarter of the database file, never less than the floor and never more
#: than the ceiling below (in KiB, SQLite's unit). See
#: `SqliteStore.size_write_cache` for the measurement behind these numbers.
WRITE_CACHE_FLOOR_KIB = 16 * 1024
WRITE_CACHE_CEILING_KIB = 256 * 1024

#: 2026-10-04. Every connection's page cache (KiB) and memory-mapped read
#: ceiling (bytes) - see `_new_connection` for the measurement.
SEARCH_CACHE_KIB = 64 * 1024
SEARCH_MMAP_BYTES = 256 * 1024 * 1024

#: 2026-10-10, storage review S5. **What the write-ahead log is cut back to
#: each time it starts over** (`PRAGMA journal_size_limit`, bytes). SQLite's
#: default is no limit: the `-wal` file stays at its high-water mark until the
#: last connection closes - and the window keeps connections open for days,
#: so one large transaction (a reset, `reembed --all`, a migration, a batch
#: written while a search held a snapshot) left gigabytes on disk for the life
#: of the window. Checked 2026-10-10 on this laptop's Python (SQLite 3.49.1):
#: a 5 MB log with the limit at 64 KiB is cut to exactly 64 KiB by the first
#: write after a checkpoint. 64 MiB, not smaller: auto-checkpoint keeps the log
#: near 4 MiB (1,000 pages of 4 KiB) when nothing pins it, and a write group of
#: a large run can go past that - a limit under the usual high-water mark
#: would truncate and regrow the file at every restart of the log, a file
#: system round trip for nothing. 64 MiB is sixteen times the usual size and
#: under one per cent of a 10 GB index. Fixed, not a setting (non-negotiable
#: 11): nobody would ever change it; a measured `-wal` size that matters is
#: the evidence that would.
WAL_SIZE_LIMIT_BYTES = 64 * 1024 * 1024

#: 2026-10-10, storage review S5. How long `checkpoint_wal` lets a
#: `TRUNCATE` checkpoint wait for readers before it settles for `PASSIVE`.
#: A `TRUNCATE` waits for every reader to finish, through `busy_timeout` -
#: 30 s by default, measured 2026-10-10 to wait out the whole timeout and then
#: answer "busy" while one reader held a snapshot. The window has readers open
#: much of the time, so the wait is kept short and the fallback does the work.
CHECKPOINT_WAIT_S = 2.0

#: 2026-10-10, storage review S6. Pages each step of `optimize_fts` merges
#: (FTS5's `'merge'`, given negated so it does what `'optimize'` does, in
#: steps). Measured 2026-10-10 on this laptop, 300,000 sixty-word passages
#: written 500 to a transaction, CPU shared with four other jobs: one
#: `'optimize'` held the write lock 3.97 s (4.84 s on a second run). In steps
#: of 50 / 100 / 250 / 500 pages: 300 / 153 / 64 / 33 steps, 3.86 / 2.87 /
#: 2.84 / 2.92 s in all, a median step of 8 / 12 / 34 / 92 ms; the longest
#: single step 214-306 ms whatever the size (a WAL checkpoint or the shared
#: CPU, not the merge size). 250: the whole job no slower than one
#: `'optimize'`, each hold far under anything a person notices, and a quarter
#: of the steps that 50 needs. Fixed, not a setting: nobody would tune it.
FTS_MERGE_PAGES = 250

#: 2026-10-04. The most chunk matches a list on the Files or Mail tab reads
#: before it changes how it reads them, and the newest chunks a word's share
#: is estimated from - the same numbers as `keyword.SCORED_MATCHES` and
#: `keyword.COMMON_SAMPLE`; see `SqliteStore.chunk_match_share`.
MATCH_SCORED_MAX = 10_000
MATCH_SAMPLE = 20_000
#: Past this many matches a list ordered by something other than rank
#: (Mail by date, the Code tab's repositories) walks its own order and asks
#: FTS5 per row instead of collecting every match. Measured on the bench:
#: at ~45,000 matches the two cost the same (153 / 135 ms); at 10,000 the
#: walk was 331 ms against 12; at 244,000 it was 17.6 against 538.
MATCH_WALK_MIN = 50_000
#: Past this many matches the Mail count asks each message's chunk ids
#: (`idx_chunks_file_ord`, narrow) against the set of matching ids, which
#: FTS5 hands over without reading a chunk row. Bench, same counts: `pump`
#: 459 -> 138 ms, `pump AND valve` 176 -> 95, with a sender 582 -> 50; a
#: rare word the other way (`invoice` 16 -> 62, ~10,000 matches).
MATCH_PROBE_MIN = 25_000

#: 2026-09-30. SQLite's per-connection switch for triggers, by its number in
#: Python's `sqlite3` (3.12 and later). None on an older Python, where
#: `SqliteStore` then writes the keyword index row by row as it always did.
#: See `SqliteStore._deferred`.
_TRIGGER_SWITCH = getattr(sqlite3, "SQLITE_DBCONFIG_ENABLE_TRIGGER", None)

#: 2026-09-30. The most passages whose keyword-index rows wait in memory for
#: the end of one `batch()`. Past it they are handed to the index there and
#: then, still inside the same transaction, so one enormous document cannot
#: hold its whole text twice. A group of mail messages is a few hundred rows;
#: this is for the 5,000-page PDF.
FTS_DEFER_MAX_ROWS = 2_000


class _DeferredFts:
    """Keyword-index rows one thread's open `batch()` has still to write.

    See `SqliteStore._deferred` for what this is for. Per thread, like the
    connection it belongs to, and only ever non-empty inside a `batch()`.
    """

    __slots__ = ("chunks", "chunk_files", "messages", "triggers_off", "broken")

    def __init__(self) -> None:
        #: `(chunk id, text, symbols)` - exactly what `chunks_ai` would pass.
        self.chunks: list[tuple[int, Any, Any]] = []
        #: The files those passages belong to, so a second write to one of
        #: them in the same batch is noticed.
        self.chunk_files: set[int] = set()
        #: file id -> `(subject, sender, recipients)`, what `messages_ai` passes.
        self.messages: dict[int, tuple[Any, Any, Any]] = {}
        #: Whether this thread's connection has its triggers switched off.
        self.triggers_off = False
        #: Set when writing the waiting rows failed part-way: the batch can no
        #: longer be committed, only rolled back.
        self.broken = False


#: Scheme prefix for a volume-backed file's synthetic `files.path`. Never a
#: real URL and never opened as one - `resolve.py` recognises the prefix and
#: routes to volume resolution instead of treating it as a filesystem path.
VOLUME_PATH_SCHEME = "leasha-volume://"


def volume_synthetic_path(volume_id: int, relative_path: str) -> str:
    r"""The letter-free identity `files.path` holds for a catalogued file.

    **Why `path` carries this instead of a real absolute path.** `files.path`
    is `UNIQUE`, and an absolute path collides the moment two different
    volumes are ever mounted at the same letter - which is exactly the bug
    class 1c exists to kill (`E:\Documents\a.txt` from volume A must never be
    the same row as `E:\Documents\a.txt` from volume B, seen after A is
    unplugged and B takes its letter). Built only from `(volume_id,
    relative_path)`, so it is stable across every remount and never needs the
    letter at all.

    Forward slashes only, whatever the source used, so the same relative file
    always produces the same string - `relative_path` is read back off a
    Windows walk (backslashes) and, for a network share, may arrive already
    slash-normalised from a UNC path.
    """
    normalised = str(relative_path).replace("\\", "/").lstrip("/")
    return f"{VOLUME_PATH_SCHEME}{int(volume_id)}/{normalised}"


def _basename(path: str) -> str:
    """The last component of a path, whichever separator it uses.

    Not `Path(path).name`: these keys are written on Windows and may be read
    back anywhere, and `PurePosixPath` would treat a whole `D:\a\b.pdf` as one
    filename. Splitting on both separators is the portable answer.
    """
    return path.replace("\\", "/").rstrip("/").rpartition("/")[2] or path


def _contains_words(haystack: str, needle: str) -> bool:
    """Is `needle` present in `haystack` as whole words?

    Word boundaries, not `in`. "PI" is a substring of "PIPELINE" and that says
    nothing at all about the two being the same entity.
    """
    if needle not in haystack:
        return False
    start = haystack.find(needle)
    while start != -1:
        before_ok = start == 0 or not haystack[start - 1].isalnum()
        after = start + len(needle)
        after_ok = after == len(haystack) or not haystack[after].isalnum()
        if before_ok and after_ok:
            return True
        start = haystack.find(needle, start + 1)
    return False


def _seconds(value: float) -> str:
    """A duration a person reads: `3s`, `0.5s`, `250ms`.

    `f"{value:.0f}s"` renders every sub-second budget as "0s", which reads as a
    limit of nothing at all.
    """
    if value >= 1:
        return f"{value:g}s"
    if value >= 0.1:
        return f"{value:.1f}s"
    return f"{value * 1000:.0f}ms"


#: Re-exported so the existing call sites in this module keep reading the same.
#: The definition lives in `app.storage.like`, which `filters.py` can import
#: without the cycle a `sqlite_store` import would create - that cycle is why
#: there were two copies of this and one module with none.
_like_escape = like_escape


#: Matching rows counted when the value menu is scoped by what is already typed.
#:
#: Measured on 500,000 files, 2026-08-27, for `folder` scoped by `type:pdf`:
#: 437ms counting all of them, **11.1ms counting the first 20,000**, and both
#: return the same twenty-five folders in the same order. See `distinct_values`.
VALUE_SAMPLE = 20_000


class ValueCount(NamedTuple):
    """One row of a value menu: what it is, how many, and whether that is true.

    `exact` is False when the number came from a sample - see `VALUE_SAMPLE`.
    It exists because the alternative is a menu that states a count it cannot
    stand behind, and this project's rule is that a label says what is so.
    """

    value: str
    count: int
    exact: bool = True


class _ValueShape(NamedTuple):
    """One row of the value catalogue `distinct_values` offers.

    A table rather than four hand-written statements, because `within` has to
    be appended to each of them and four near-identical strings drifting apart
    is how the mail ones ended up scanning while the file ones did not.

    **Every source aliases `files` as `f`**, which is what lets `file_filter_sql`
    - the one filter grammar in this project - be appended unchanged. The mail
    shape joins to it rather than filtering `messages` directly, for the same
    reason: `type:pdf` is a fact about a file.
    """

    source: str
    value: str
    count: str
    guard: str
    group: str


_VALUE_SHAPES: dict[str, _ValueShape] = {
    "ext": _ValueShape(
        "files f", "f.ext", "COUNT(*)", "f.ext <> ''", "f.ext"),
    "folder": _ValueShape(
        "files f", "f.parent_dir", "COUNT(*)", "f.parent_dir <> ''",
        "f.parent_dir"),
    "sender": _ValueShape(
        "messages m JOIN files f ON f.id = m.file_id", "m.sender", "COUNT(*)",
        "m.sender IS NOT NULL AND m.sender <> ''", "m.sender"),
    # **Whole field, not parsed.** `recipients` is a JSON array, and splitting
    # it per row to offer individual addresses would cost a parse per message
    # behind a keystroke. The completer offers the string as stored, which is
    # what `to:` matches against anyway - `filters.py` searches inside it for
    # the same reason.
    "recipient": _ValueShape(
        "messages m JOIN files f ON f.id = m.file_id", "m.recipients",
        "COUNT(*)", "m.recipients IS NOT NULL AND m.recipients <> ''",
        "m.recipients"),
    "repo": _ValueShape(
        "repos r LEFT JOIN files f ON f.repo_id = r.id", "r.name",
        "COUNT(f.id)", "r.name <> ''", "r.id, r.name"),
    # `/on` - order 202626270513 §3c. Same shape as `repo` above: every
    # catalogued volume is offered, including one just Scanned with nothing
    # indexed from it yet, because a source with zero files still exists
    # and a person still needs to be able to type its name.
    "on": _ValueShape(
        "volumes v LEFT JOIN files f ON f.volume_id = v.id", "v.name",
        "COUNT(f.id)", "v.name <> ''", "v.id, v.name"),
    # **The one shape that does not touch `files`, and the one command that
    # cannot be narrowed.** `/saved` carries no `scoped_by`, so `scope_for`
    # returns None, so `_scope_sql` appends nothing - which is what makes it
    # safe for this source to have no `f` to filter on. A `scoped_by` added to
    # `/saved` later would break that silently, which is why it is said here
    # rather than left to be noticed.
    #
    # The count is how often it has been run, not how many results it would
    # return: the second is a search, and a menu that runs a search per row
    # behind a keystroke is the unbounded work this whole method exists to
    # keep out. Run count is a fact already recorded and is also the more
    # useful of the two - it is what puts the search you use every Monday at
    # the top of the list.
    "saved": _ValueShape(
        "saved_searches s", "s.name", "s.run_count", "s.name <> ''",
        "s.id, s.name"),
    # Work order 0i section 1c. file_tags (schema v20) is Florence-2's
    # tag vocabulary - see _v20_file_tags for why it exists as a real
    # table rather than being read out of chunk text.
    "shows": _ValueShape(
        "file_tags ft JOIN files f ON f.id = ft.file_id", "ft.tag",
        "COUNT(*)", "ft.tag <> ''", "ft.tag"),
    # Work order 0i section 4a. A plain column, not a join table - see
    # _v22_places for why a photo's place has different cardinality than
    # its tags.
    "place": _ValueShape(
        "files f", "f.place", "COUNT(*)", "f.place <> ''", "f.place"),
    # Work order 0j section 3a. Named piles only - `p.name IS NOT NULL` is
    # the guard, the same "identity only ever comes from the user" line that
    # keeps every unnamed pile out of anything a search box offers. Counted
    # by distinct file, not by face: two photos of the same two people are
    # two files, not four rows, and a person reading "Daddy (37)" means "37
    # photos", not "37 faces detected".
    "who": _ValueShape(
        "faces fc JOIN piles p ON p.id = fc.pile_id JOIN files f ON f.id = fc.file_id",
        "p.name", "COUNT(DISTINCT fc.file_id)", "p.name IS NOT NULL", "p.id, p.name"),
}


def _scope_sql(within: Any) -> tuple[str, list[Any]]:
    r"""The already-typed filters, as SQL to append. `("", [])` when there are none.

    **Only the filters.** `file_filter_sql` builds clauses for `type:`, `path:`,
    dates, sizes and the mail fields and nothing else - free text never reaches
    it - so a half-typed sentence in the box cannot turn a bounded, indexed
    lookup behind a keystroke into a scan. That is the order's rule and it is
    satisfied by which function is called, not by a check afterwards.

    Never raises. This runs while somebody is typing.
    """
    if within is None:
        return "", []
    try:
        from app.storage.filters import file_filter_sql

        return file_filter_sql(within)
    except Exception:                            # noqa: BLE001 - see docstring
        _log.debug("value scope could not be built; offering unscoped values")
        return "", []


def _bucket_top_level(root: str, parent_dir_counts: list) -> list:
    r"""Reduce (deep parent_dir, count) pairs to (immediate child of root,
    total count) - the Digital Inheritance report's own "top-level folder
    summary," since `parent_dir` is often several levels below `root` and
    only the first segment past it is what a source-level summary wants.
    """
    root_norm = root.replace("\\", "/").rstrip("/")
    buckets: dict = {}
    for parent_dir, n in parent_dir_counts:
        rel = str(parent_dir or "").replace("\\", "/")
        if rel == root_norm:
            key = "(top level)"
        elif rel.startswith(root_norm + "/"):
            key = rel[len(root_norm) + 1:].split("/", 1)[0]
        else:
            key = "(other)"
        buckets[key] = buckets.get(key, 0) + int(n)
    return sorted(buckets.items(), key=lambda kv: (-kv[1], kv[0].lower()))


class FileStatus:
    """The values `files.status` may hold; the CHECK constraint in the schema
    enforces the same list (widened by migrations v10 and v25)."""

    PENDING = "PENDING"
    INDEXED = "INDEXED"
    SKIPPED = "SKIPPED"
    FAILED = "FAILED"
    #: The file exists and is findable by name; its contents were never read.
    #:
    #: **A status, not a failure, and never `INDEXED`.** A row that says
    #: INDEXED while holding no chunks is precisely the bug that made
    #: `--force` necessary - every later run reported it as unchanged, the
    #: totals looked healthy, and its content was not searchable. Recreating
    #: that deliberately, for millions of rows, would be worse.
    #:
    #: It is not `SKIPPED` either: a skip is something that went wrong and
    #: carries an error code. Nothing went wrong here. There is no reader for
    #: a `.mp4`, and there was never going to be.
    NAME_ONLY = "NAME_ONLY"
    #: Chunks are written and keyword-searchable now; embedding has not
    #: caught up yet. Work order 202626270114 (0b) section 6d.
    #:
    #: **Never `PENDING`, which now means "genuinely untouched" and nothing
    #: more.** Before this status existed, a file between `_write_one`
    #: writing its chunks and `_embed_pending` writing their vectors sat as
    #: `PENDING` - indistinguishable from a file the walker has not reached
    #: yet, even though it already has real, keyword-searchable content. The
    #: M6 repair (`Pipeline._drain_unembedded`) already promotes any status
    #: to `INDEXED` once every one of a file's chunks is embedded, so
    #: nothing about that machinery changes - this only makes the interval
    #: before that honest.
    #:
    #: **Never `INDEXED`.** `INDEXED` keeps meaning "chunks exist and this
    #: run believes every one of them is embedded" - the M6 repair's own
    #: crash-recovery reasoning (a file can still legitimately drift back
    #: out of full coverage after a crash, self-healed asynchronously by
    #: the drain) is unaffected by this status existing; it was already
    #: tolerating exactly that gap without a name for it.
    PARTIAL = "PARTIAL"

    ALL = (PENDING, INDEXED, SKIPPED, FAILED, NAME_ONLY, PARTIAL)


@dataclass(frozen=True)
class FileRecord:
    """One `files` row as Python. Built by `from_row` from whatever columns a
    SELECT asked for, so every column added after the first release has a
    default (see the notes beside each)."""

    id: int
    path: str
    parent_dir: str
    ext: str
    size_bytes: int
    mtime_ns: int
    content_hash: Optional[str]
    status: str
    skip_code: Optional[str]
    skip_detail: Optional[str]
    indexed_at: Optional[int]
    source_kind: str
    #: Work order 0h §2a. A perceptual hash, `NULL` until the images pass has
    #: touched this file - see `app/storage/migrations.py`'s `_v17_image_phash`
    #: for why it lives here rather than on the image-vector table. Defaulted
    #: so `from_row`'s generic construction below keeps working against a
    #: database that has not been migrated yet in a test double that builds
    #: rows by hand without this key.
    phash: Optional[str] = None
    #: Work order 0f §3a. A photograph's EXIF shot date, in nanoseconds since
    #: the epoch so it is directly comparable with `mtime_ns`. `NULL` for
    #: every file that is not a photograph, and for a photograph whose EXIF is
    #: absent or unreadable - in which case `mtime_ns` is the date, which is
    #: what `file_filter_sql` falls back to. **Never confuse the two:**
    #: `mtime_ns` is the file's real mtime and answers "did this change" for
    #: H1; this answers "when is this from". See `_v18_photo_taken_at`.
    #: Defaulted for the same reason `phash` is - `from_row` builds from
    #: whatever columns a SELECT actually asked for.
    taken_at_ns: Optional[int] = None
    #: repo_id is read directly by callers that already exist above this
    #: dataclass; not added here to keep this change to what Offline Media
    #: needs. Both are additive and default to None for every row untouched
    #: by a volume Scan - "an ordinary, always-connected file".
    volume_id: Optional[int] = None
    relative_path: Optional[str] = None
    #: Work order 0i section 4b. False for an EXIF-sourced date (the
    #: default, and correct for every row written before this column
    #: existed); True for a folder-year era hint - see
    #: `app.extract.era_hints`.
    taken_at_is_hint: bool = False
    #: Work order 0i section 4a. The nearest town to a photo's EXIF
    #: GPS, offline - None for everything that is not a photo, and
    #: for one with no GPS block. See `app.extract.places`.
    place: Optional[str] = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "FileRecord":
        """Only the columns the row carries: a `SELECT id, path` and a
        `SELECT *` both build a record, the missing fields at their defaults."""
        return cls(**{key: row[key] for key in cls.__dataclass_fields__
                      if key in row.keys()})


@dataclass(frozen=True)
class VolumeRecord:
    """One catalogued Offline Media source - a drive, share, cloud mount,
    phone, or archived location. See `schema.sql`'s `volumes` table."""
    id: int
    kind: str
    identity_key: str
    volume_guid: Optional[str]
    hardware_serial: Optional[str]
    fs_label: Optional[str]
    name: str
    description: Optional[str]
    location_note: Optional[str]
    status: str
    sequential_medium: int
    first_seen: int
    last_seen: int
    last_scanned_at: Optional[int]
    size_bytes: Optional[int]
    file_count: Optional[int]

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "VolumeRecord":
        """Every column, so the row must come from `SELECT * FROM volumes`."""
        return cls(**{key: row[key] for key in cls.__dataclass_fields__})


@dataclass(frozen=True)
class ChunkRecord:
    """One passage of one file. **No `label` field**: the preview reads the
    label out of the text itself (`add_caption_chunk` writes it there), and
    `add_caption_chunk`'s docstring says why rebuilding from these would
    lose it."""

    id: int
    file_id: int
    ordinal: int
    text: str
    char_start: Optional[int] = None
    char_end: Optional[int] = None
    page: Optional[int] = None
    embedded: int = 0


@dataclass(frozen=True)
class FaceRecord:
    """One detected face. Work order 0j. `embedding` is raw bytes - callers
    that need it as a vector go through `app.index.face_clustering`, which
    owns the (de)serialisation, the same separation `vector_store.py` keeps
    from the SQLite layer for chunk embeddings."""

    id: int
    file_id: int
    bbox: tuple[float, float, float, float]
    embedding: bytes
    pile_id: Optional[int]
    confidence: Optional[float]
    suggested_pile_id: Optional[int]


@dataclass(frozen=True)
class PileSample:
    """One face the grid can draw a crop from - section 2a."""

    file_id: int
    path: str
    bbox: tuple[float, float, float, float]


@dataclass(frozen=True)
class PileRecord:
    """One pile - named or not. Work order 0j section 2a: `name is None` is
    an unnamed pile; `face_count` is what the grid sorts "biggest first" by."""

    id: int
    name: Optional[str]
    face_count: int
    samples: tuple[PileSample, ...]


@dataclass(frozen=True)
class PhotoRow:
    """One picture on the Photos page (2026-10-05). Everything the page sorts,
    narrows and lists by, read in `photo_library` - never per photo."""

    file_id: int
    path: str
    ext: str
    size_bytes: int
    mtime_ns: int
    taken_at_ns: Optional[int]
    taken_is_hint: bool
    place: Optional[str]
    people: tuple[str, ...]
    faces: int
    described: bool
    has_text: bool
    page_like: bool
    status: str
    scanned: bool = False
    tags: tuple[str, ...] = ()

    @property
    def when_ns(self) -> int:
        """When it is from: the date taken, else the file's own date."""
        return int(self.taken_at_ns or self.mtime_ns or 0)


@dataclass(frozen=True)
class PendingSuggestion:
    """One "Is this <name>?" chip - section 2c's learning-loop queue. Only
    ever built from a suggestion against an already-named pile; a match
    against an unnamed one has no name to ask about yet, see
    `pending_suggestions`."""

    face_id: int
    file_id: int
    path: str
    bbox: tuple[float, float, float, float]
    pile_id: int
    pile_name: str


# -- closing a connection another thread is using ---------------------------
#
# **Why this exists: `close()` used to crash the whole process.** Each thread
# gets its own connection (see `SqliteStore.__init__`), and `close()` closes
# all of them from whichever thread is closing - usually the UI thread, as the
# window shuts. But a worker can be in the middle of a query on its own
# connection at that moment: the popup counting folder names as somebody
# types, a search still running. Python lets other threads run while SQLite
# is working, so the worker is "inside" `execute()` or `fetchall()` with
# nothing to stop the closer. Closing tears down the connection's internals
# underneath it, and when the worker carries on it reads memory that is no
# longer there. That is not an exception anybody can catch: the process dies
# with a segmentation fault (an access violation on Windows), no traceback.
# Found in the test suite on 2026-09-27 and reproduced on its own by
# `tests/unit/test_close_during_read.py`.
#
# **The fix: a connection counts its callers in, and `close()` waits for none.**
# Every call that makes SQLite do work - running a statement, fetching rows,
# committing - goes through `_GuardedConnection._call` (or its copy in
# `_GuardedCursor.__next__`), which keeps a count of calls in flight. `close()` then:
#
#   1. marks each connection *retired*, so no new call can start on it;
#   2. calls `interrupt()` on any with a call in flight - the one method SQLite
#      documents as safe from another thread - so a long query stops at once
#      rather than running to the end;
#   3. waits (briefly, and with a limit) for the count to reach zero, and only
#      then closes it.
#
# The worker whose query was cut short gets the same plain-words error as one
# that asks for a connection after close, which the workers already recognise
# as "the window is closing" and do not report as a bug.
#
# **Why wrap the connection rather than the ~hundreds of call sites.** Every
# query in this file is `self.conn.execute(...)`, and more take `store.conn`
# elsewhere. Wrapping the connection itself (SQLite lets you choose the class
# it builds) covers every one of them, including code not written yet.

#: How long `close()` waits for a worker's interrupted query to hand its
#: connection back before giving up on closing that one. An interrupted query
#: stops within milliseconds; this is a ceiling for something stuck, not a
#: delay anybody normally sees. A connection still busy after it is left open
#: rather than closed under its user - see `SqliteStore.close`.
_CLOSE_WAIT_S = 5.0


class _ThreadToken:
    """Held only by one thread's `threading.local`; freed when that thread's
    local storage is. `SqliteStore._prune_orphans` watches it through a weak
    reference to tell a live thread's connection from an abandoned one."""

    __slots__ = ("__weakref__",)


def _closed_while_in_use() -> AppErrorException:
    """The error a worker gets when the store closes during its query.

    The same words as `SqliteStore.conn`'s "closed while we waited" path, on
    purpose: `app/ui/workers.py` matches "was closed while a worker was using
    it" to recognise a window close and stay quiet about it."""
    return AppErrorException(make_error(
        "ERR_UNEXPECTED", "storage.sqlite",
        details="SqliteStore was closed while a worker was using it.",
        suggestion=(
            "If this appeared while closing the window, a background "
            "search outlived the store and the message is harmless."
        ),
    ))


class _GuardedConnection(sqlite3.Connection):
    """A `sqlite3.Connection` that knows whether it is in use right now.

    Built by `sqlite3.connect(..., factory=_GuardedConnection)`. Behaves
    exactly like the standard one; the only addition is the count of calls in
    flight and the "retired" flag that `SqliteStore.close()` uses to close it
    safely. See the block comment above for the whole story.

    **What it costs, measured** (1,000,000 rows, Linux sandbox, Python 3.12):
    `fetchall()` and `executemany()` unchanged; a `for row in cursor` loop
    about 0.7 microseconds more per row (0.5 s -> 1.2 s for the million); one
    small `execute().fetchone()` about 2 microseconds more (1.6 -> 3.8). Next
    to a query that reads the disk, noise; a hot loop of tiny lookups should
    prefer one `fetchall()` anyway.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # One small lock per connection, held only for the instant it takes to
        # add or subtract one - never while SQLite is working, so a reader
        # never waits on it for longer than that.
        self._guard = threading.Lock()
        #: Calls in flight. A count, not a yes/no, because calls can nest:
        #: `executemany` pulling its rows from a generator that itself reads.
        self._busy = 0
        #: Set by `close()`. From then on no new call may start.
        self._retired = False
        #: The thread that opened it - the only one expected to use it.
        self._owner = threading.get_ident()

    def _call(self, method: Any, *args: Any) -> Any:
        """Run one SQLite call counted in, and translate a cut-short one.

        A query interrupted by `close()` raises `sqlite3.OperationalError:
        interrupted`, and one on a connection closed between two calls raises
        `ProgrammingError`. Both would reach the person as "This is a bug".
        Once the connection is retired they are neither - they are the window
        closing - so they become the error that says so."""
        guard = self._guard
        with guard:
            if self._retired:
                raise _closed_while_in_use()
            self._busy += 1
        try:
            return method(*args)
        except sqlite3.Error as exc:
            if self._retired:
                raise _closed_while_in_use() from exc
            raise
        finally:
            with guard:
                self._busy -= 1

    # -- used by SqliteStore.close() ------------------------------------------

    def _retire(self) -> int:
        """Refuse every new call; return how many are still in flight."""
        with self._guard:
            self._retired = True
            return self._busy

    def _wait_idle(self, timeout: float) -> bool:
        """Wait for the calls in flight to finish. True once there are none.

        Checks every couple of milliseconds rather than being woken: waking
        would cost every query something, and this runs once, at close."""
        deadline = time.monotonic() + timeout
        while True:
            with self._guard:
                if self._busy == 0:
                    return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.002)

    # -- every method that makes SQLite do work -------------------------------
    #
    # `Connection.execute` and friends are written in C and would build a plain
    # cursor, bypassing ours - so each one is spelled out here as "make one of
    # our cursors and ask it". That is exactly what the C version does, minus
    # the bypass.

    def cursor(self, factory: Any = None) -> sqlite3.Cursor:  # type: ignore[override]
        return sqlite3.Connection.cursor(self, factory or _GuardedCursor)

    def execute(self, sql: str, parameters: Any = (), /) -> sqlite3.Cursor:  # type: ignore[override]
        return self.cursor().execute(sql, parameters)

    def executemany(self, sql: str, parameters: Any, /) -> sqlite3.Cursor:  # type: ignore[override]
        return self.cursor().executemany(sql, parameters)

    def executescript(self, sql_script: str, /) -> sqlite3.Cursor:  # type: ignore[override]
        return self.cursor().executescript(sql_script)

    def commit(self) -> None:
        self._call(sqlite3.Connection.commit, self)

    def rollback(self) -> None:
        self._call(sqlite3.Connection.rollback, self)

    def set_progress_handler(self, handler: Any, n: int) -> None:  # type: ignore[override]
        # Closing frees the handler; setting one while that happens is the
        # same race in miniature.
        self._call(sqlite3.Connection.set_progress_handler, self, handler, n)


_CURSOR_NEXT = sqlite3.Cursor.__next__


class _GuardedCursor(sqlite3.Cursor):
    """A cursor whose every trip into SQLite is counted by its connection.

    `fetchall()` and iterating (`for row in cursor`) do real work - each row is
    another step of the query - so they are counted exactly like `execute()`.
    """

    connection: _GuardedConnection

    def execute(self, sql: str, parameters: Any = (), /) -> "_GuardedCursor":  # type: ignore[override]
        return self.connection._call(sqlite3.Cursor.execute, self, sql, parameters)

    def executemany(self, sql: str, seq_of_parameters: Any, /) -> "_GuardedCursor":  # type: ignore[override]
        return self.connection._call(sqlite3.Cursor.executemany, self, sql, seq_of_parameters)

    def executescript(self, sql_script: str, /) -> "_GuardedCursor":  # type: ignore[override]
        return self.connection._call(sqlite3.Cursor.executescript, self, sql_script)

    def fetchone(self) -> Any:
        return self.connection._call(sqlite3.Cursor.fetchone, self)

    def fetchmany(self, size: Optional[int] = None) -> list[Any]:  # type: ignore[override]
        return self.connection._call(sqlite3.Cursor.fetchmany, self,
                                     self.arraysize if size is None else size)

    def fetchall(self) -> list[Any]:
        return self.connection._call(sqlite3.Cursor.fetchall, self)

    def close(self) -> None:
        # Closing a cursor resets its statement inside SQLite - brief, but work
        # on the connection all the same.
        self.connection._call(sqlite3.Cursor.close, self)

    def __next__(self) -> Any:
        # `_call` written out in place: this runs once per row, and a loop over
        # a hundred thousand rows should not pay for an extra call per row.
        conn = self.connection
        guard = conn._guard
        with guard:
            if conn._retired:
                raise _closed_while_in_use()
            conn._busy += 1
        try:
            return _CURSOR_NEXT(self)
        except sqlite3.Error as exc:
            if conn._retired:
                raise _closed_while_in_use() from exc
            raise
        finally:
            with guard:
                conn._busy -= 1



#: What the Files tab lists: files, and **the attachments inside mail**
#: (owner, 1 October 2026: *"files in emails should come up on the files list
#: tab"*). An attachment is its own row, keyed `<message>/attachments/<name>`
#: by `archive.attachment_key`; the message itself stays on the Mail tab.
#: 2026-10-04, code review: built from `row_facts`, whose `attachment_of` and
#: `is_mail_attachment` are the Python half - the literal here matched
#: `/Attachments/` (LIKE ignores case) where the parsers did not.
LISTED_FILES = listed_files_sql("f")


def _named_in_fts(path: Any, source_kind: Any) -> bool:
    """Whether a row has a `files_fts` entry: files, archives and their
    members, and mail attachments - never a message's key."""
    return str(source_kind or "") in ("file", "archive") or is_mail_attachment(path, source_kind)


def _all_words_match(words: str) -> str:
    """An FTS5 expression requiring every word, or `""` when there is none.

    Each word is quoted, so nothing a person types can be read as FTS syntax -
    `NEAR`, `*`, a stray quote. Quoted words separated by spaces are ANDed.
    """
    found = re.findall(r"[^\W_]+", str(words or ""), flags=re.UNICODE)
    return " ".join('"' + word.replace('"', '""') + '"' for word in found)

def skip_sentence(error: Any) -> str:
    """What `files.skip_detail` holds for a skipped file: the error's sentence.

    **For a fault in Leasha itself, the last line of its trace as well**
    (2026-10-05). `ERR_UNEXPECTED`'s sentence is the same for every such fault
    - "An unexpected error occurred in ..." - so the row said a file had been
    skipped by a bug and nothing about which bug; the panel then asked the
    owner to "report it with the detail below" and had none to show. The last
    line of a trace is the exception and its message, which is the detail.
    """
    sentence = str(getattr(error, "message", "") or "")
    if getattr(error, "code", "") != "ERR_UNEXPECTED":
        return sentence
    lines = [line.strip() for line in str(getattr(error, "details", "") or "").splitlines()
             if line.strip()]
    return f"{sentence} {lines[-1]}" if lines else sentence


class SqliteStore:
    """Open, migrate and operate the metadata database.

        with SqliteStore(settings.fts_db) as store:
            file_id = store.upsert_file(...)
            store.replace_chunks(file_id, chunks)
    """

    def __init__(self, db_path: Path, *, timeout: float = 30.0):
        self.db_path = Path(db_path)
        self._timeout = timeout
        # **Two locks, because they guard two unrelated things.**
        #
        # They were one, and a reader opening its first connection then had to
        # wait for whatever write was in flight - so the very blocking this
        # change exists to remove came back through the registry. Caught by
        # `test_a_reader_is_not_blocked_by_a_write_in_flight`, which hung.
        #
        # Lock order is always write -> conns, never the reverse: `write()`
        # takes the write lock and may then open a connection, and `close()`
        # takes both in that same order. Nothing takes them the other way
        # round, which is what keeps this deadlock-free.
        self._write_lock = threading.RLock()
        self._conns_lock = threading.RLock()
        # **One connection per thread**, not one shared between them.
        #
        # A single connection with `check_same_thread=False` was not merely
        # slow, it was wrong. A reader on the shared connection reads *inside*
        # whatever transaction another thread has open on it, so during
        # indexing a search saw uncommitted rows - and when the indexing
        # transaction rolled back, the user was left holding a result for a
        # document that had never entered the index. Reproduced, and now
        # pinned by `test_connection_per_thread.py`.
        #
        # WAL is what makes the fix cheap: separate connections each get a
        # consistent snapshot of *committed* data, readers never block the
        # writer, and the writer never blocks them.
        self._local = threading.local()
        #: Every connection handed out, so `close()` can close all of them.
        #: `threading.local` cannot be enumerated, and a connection left open
        #: holds a file handle and its share of the WAL.
        #:
        #: A list, not a dict keyed by `threading.get_ident()` - a QThreadPool
        #: worker thread can be handed a second task without this code seeing
        #: a new native thread: the ident is unchanged, but a fresh
        #: `PyThreadState` for that task means `self._local.conn` no longer
        #: has anything cached, so `conn` (below) opens a second connection.
        #: Keyed by ident, that second connection overwrote the first here
        #: and `close()` never saw it again - a real file handle, leaked
        #: every time a pooled thread picked up more than one task. A list
        #: just keeps every connection this store has ever handed out, so
        #: `close()` closes all of them regardless of how many any one
        #: thread accumulated.
        self._open: list[sqlite3.Connection] = []
        self._closed = False
        self._migrated = False
        #: Whether `messages_fts` exists, once asked. None means "not asked".
        #: A property of the file, so one answer serves every connection.
        self._message_index: Optional[bool] = None
        #: 2026-09-30. Whether a `batch()` writes the keyword-index rows of
        #: new passages and new messages once, at its end, instead of one at a
        #: time through the triggers - see `_deferred`. True is the fast way;
        #: False is the way every release before this wrote them, kept so the
        #: two can be compared (`tests/unit/test_fts_deferred_writes.py`
        #: proves they give the same index) and as a switch if a fault is ever
        #: suspected. Read when a batch first writes a passage or a message.
        self.defer_fts = True
        #: True between `drop_fts_triggers` and the triggers coming back: a
        #: bulk run has taken the keyword index out of step on purpose and
        #: rebuilds it at the end, so nothing is written to it meanwhile.
        self._content_triggers_dropped = False

    # -- lifecycle -----------------------------------------------------------

    def _new_connection(self) -> sqlite3.Connection:
        """One configured connection. Caller holds `self._conns_lock`."""
        try:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise AppErrorException(make_error(
                "ERR_CONFIG_INVALID", "storage.sqlite",
                key="FTS_DB", reason=f"'{self.db_path.parent}' could not be created",
                details=str(exc),
            )) from exc

        try:
            conn = sqlite3.connect(
                str(self.db_path),
                timeout=self._timeout,
                # Still False, but for a different reason than it used to be:
                # not so that threads may share a connection - they no longer
                # do - but so that `close()` can close every thread's
                # connection from whichever thread is doing the closing. A
                # worker that has already exited cannot close its own.
                check_same_thread=False,
                isolation_level=None,      # explicit transactions only
                # Counts its calls in flight, so `close()` never closes it
                # while another thread is inside a query - see
                # `_GuardedConnection`.
                factory=_GuardedConnection,
            )
        except sqlite3.Error as exc:
            raise AppErrorException(make_error(
                "ERR_DB_LOCKED", "storage.sqlite",
                details=f"Could not open {self.db_path}: {exc}",
            )) from exc

        conn.row_factory = sqlite3.Row
        # WAL is a property of the file, not the connection, so setting it
        # repeatedly is harmless. The rest are per-connection and must be set
        # on every one of them - a worker thread with `foreign_keys` off would
        # silently skip the cascade deletes that keep chunks with their file.
        #
        # **Bounded, and said in words.** The busy timeout is what stops these
        # from waiting for ever on a file another process holds; when it runs
        # out sqlite raises "database is locked", which used to escape as a bare
        # OperationalError from a worker with `_conns_lock` held - so every other
        # worker then queued behind it. It is now closed and reported.
        try:
            conn.execute("PRAGMA busy_timeout = %d" % int(self._timeout * 1000))
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
            conn.execute("PRAGMA foreign_keys = ON")
            # 2026-10-04, measured on a million chunks (`tools/fts_scale_bench.py`):
            # SQLite's default 2 MB page cache re-reads the word index's pages
            # on every search. 64 MB of cache and 256 MB of memory-mapped reads
            # took a middling word from 31 to 14 ms and with `type:pdf` from 71
            # to 15. Both are ceilings, not allocations - a connection holds
            # only what it has read - and the indexer's own larger cache for a
            # big job (`size_write_cache`) sits on top as before.
            conn.execute(f"PRAGMA cache_size = -{SEARCH_CACHE_KIB}")
            conn.execute(f"PRAGMA mmap_size = {SEARCH_MMAP_BYTES}")
            # 2026-10-10: per connection, like the two above - the connection
            # that restarts the log is the one that cuts it back. See
            # `WAL_SIZE_LIMIT_BYTES`.
            conn.execute(f"PRAGMA journal_size_limit = {WAL_SIZE_LIMIT_BYTES}")
        except sqlite3.OperationalError as exc:
            try:
                conn.close()
            except sqlite3.Error:
                # The connection is being thrown away; the PRAGMA failure is
                # the error worth reporting, not a second one from closing it.
                pass
            raise self._busy_error(str(exc)) from exc

        self._prune_orphans()
        self._open.append(conn)
        return conn

    def _adopt(self, conn: sqlite3.Connection) -> None:
        """Make `conn` this thread's, and give it a way to know when the thread
        is gone: a token only this thread's `threading.local` holds."""
        token = _ThreadToken()
        self._local.conn = conn
        self._local.token = token
        try:
            conn._holder = weakref.ref(token)        # type: ignore[attr-defined]
        except (AttributeError, TypeError):          # a plain connection: never pruned
            pass

    def _prune_orphans(self) -> int:
        """Close the connections whose thread has gone. Caller holds `_conns_lock`.

        2026-10-04, code review: `_open` kept every connection until `close()`,
        and a pooled worker that ends - or is handed a task under a fresh
        thread state (see `__init__`) - leaves its connection behind: a file
        handle and a share of the WAL each, for the life of the window. A
        connection's thread is gone when the token its `threading.local` held
        has been freed. One still inside a call is left for next time.
        Returns how many were closed.
        """
        closed = 0
        for conn in list(self._open):
            holder = getattr(conn, "_holder", None)
            if holder is None or holder() is not None:
                continue
            guard = getattr(conn, "_guard", None)
            if guard is None:
                continue
            with guard:
                if conn._busy:                       # type: ignore[attr-defined]
                    continue
                conn._retired = True                 # type: ignore[attr-defined]
            try:
                conn.close()
            except sqlite3.Error:
                # Its thread is gone, so nothing can use it again whether or
                # not SQLite managed to close it cleanly; drop it either way.
                pass
            self._open.remove(conn)
            closed += 1
        return closed

    def _busy_error(self, details: str = "") -> AppErrorException:
        return AppErrorException(make_error(
            "ERR_DB_BUSY", "storage.sqlite", seconds=int(self._timeout),
            details=details or f"Could not get a connection to {self.db_path} within "
                               f"{self._timeout:g} s.",
        ))

    @contextmanager
    def _conns(self) -> Iterator[None]:
        """`self._conns_lock`, but never for ever.

        The lock is held while a connection is opened (and, on the first call,
        while the schema is migrated), so one thread stuck there used to leave
        every other worker waiting on it with no way out - a hung Scan with
        workers all parked in `_new_connection`. Waiting is now bounded by the
        store's own timeout, and the wait ends in a plain-words error."""
        if not self._conns_lock.acquire(timeout=self._timeout):
            raise self._busy_error(
                f"Waited {self._timeout:g} s for the lock that hands out connections "
                f"to {self.db_path}.")
        try:
            yield
        finally:
            self._conns_lock.release()

    def connect(self) -> "SqliteStore":
        """Open this thread's connection and, once per store, migrate the schema.

        Raises `ERR_CONFIG_INVALID` for a database written by a newer build,
        `ERR_MIGRATION_FAILED` for a step that would not apply (the file is
        left as it was), `ERR_DB_LOCKED`/`ERR_DB_BUSY` when it cannot be
        opened. Other threads need not call this: `conn` opens theirs lazily.
        """
        with self._conns():
            self._closed = False
            conn = getattr(self._local, "conn", None)
            if conn is None:
                conn = self._new_connection()
                self._adopt(conn)
            if not self._migrated:
                # Once per store, not once per connection. Under the lock, so
                # a worker thread opening its first connection cannot race the
                # schema into existence twice.
                apply_migrations(conn)
                self._migrated = True
                # 2026-09-30: before any other connection is opened, so every
                # one of them reads the statistics without these rows.
                if self._forget_fts_statistics(conn):
                    # This connection read them when it opened, and SQLite
                    # keeps a table's row count once read - deleting the row
                    # does not take it back (tried, with and without
                    # `ANALYZE sqlite_master`). Only a connection opened
                    # afterwards is free of it, and on the command line this
                    # is the very connection the indexer writes with.
                    self._open.remove(conn)
                    conn.close()
                    self._adopt(self._new_connection())
        return self

    #: The tables FTS5 keeps behind each of its indexes (`<name>_data` ...).
    _FTS_SHADOW_SUFFIXES = ("data", "idx", "docsize", "content", "config")

    def _forget_fts_statistics(self, conn: sqlite3.Connection) -> int:
        r"""Take FTS5's own tables out of the planner's statistics. Never raises.

        **This is why writing got slower as the index grew** (measured
        2026-09-30, order 0z, "writing measured"). `ANALYZE` records a row
        count for every table that has rows, and four migrations run it - on
        a new database, while it is still empty. At that moment
        `chunks_fts_data`, `messages_fts_data` and `files_fts_data` hold two
        rows each (FTS5's own bookkeeping), and `sqlite_stat1` says so for as
        long as nothing analyses them again. `PRAGMA optimize` does not: it
        only revisits tables whose queries it saw on the connection it runs
        on, and these are written by the indexer's.

        FTS5 removes a merged piece of its index with
        `DELETE FROM <name>_data WHERE id>=? AND id<=?`. Told the table has
        two rows, SQLite's planner reads the whole table to find them
        (`SCAN`), where with no statistics at all it goes straight to the
        rows (`SEARCH ... USING INTEGER PRIMARY KEY`). FTS5 runs that
        statement for every piece it merges away, so each message written
        read the whole of two indexes that grow with every message: at 10,000
        messages 2.4 ms a call, twenty times what it was at 2,000, and most of
        the time writing took.

        Nothing FTS5 asks of these tables is answered better with statistics
        - every one of its statements is a lookup or a range on a primary key
        - so the rows are removed rather than refreshed, which also cannot go
        stale again. **A connection that has already read a row keeps its
        count**; only one opened afterwards is free of it, which is why
        `connect()` does this before any other connection exists and then
        replaces its own. Called when the store opens and after
        `PRAGMA optimize`. An index that has the rows loses them the first
        time it is opened by this build: a few rows deleted from a table of a
        few dozen, no passage or message read.

        Returns how many rows were removed.
        """
        try:
            if conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE name = 'sqlite_stat1'"
            ).fetchone() is None:
                return 0
            indexes = [row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND lower(sql) LIKE 'create virtual table%using fts5%'")]
            shadow = [f"{name}_{suffix}" for name in indexes
                      for suffix in self._FTS_SHADOW_SUFFIXES]
            if not shadow:
                return 0
            marks = ", ".join("?" * len(shadow))
            found = conn.execute(
                f"SELECT tbl, stat FROM sqlite_stat1 WHERE tbl IN ({marks})", shadow
            ).fetchall()
            if not found:
                return 0
            conn.execute(f"DELETE FROM sqlite_stat1 WHERE tbl IN ({marks})", shadow)
            # The counts are logged because they say whether this index was
            # affected: a `_data` table recorded with a handful of rows was
            # being read whole on every merge; one recorded in the thousands
            # was not. Said out loud only for an index that has something
            # in it: a new database always has them, and that is not news.
            indexed = conn.execute("SELECT 1 FROM chunks LIMIT 1").fetchone() is not None
            _log.log(
                "INFO" if indexed else "DEBUG",
                "the query planner no longer holds row counts for the word "
                "index's own tables ({} removed: {})", len(found),
                ", ".join(f"{row[0]}={row[1]}" for row in found
                          if str(row[0]).endswith("_data")) or "none of them data tables")
            return len(found)
        except Exception as exc:                  # noqa: BLE001 - speed only
            _log.debug("could not tidy the planner's statistics: {}", exc)
            return 0

    def size_write_cache(self) -> int:
        r"""Give this thread's connection a page cache big enough to write with.

        Work order 0x item 5d. **This is why writing got slower as the index
        grew.** SQLite keeps recently used pages of the database in a cache
        per connection, 2MB by default. Writing a passage updates the keyword
        index (`chunks_fts`), and keeping that index in order means reading
        back parts of it that grow with the index. Once those no longer fit in
        2MB, pages are read again and again. Measured 2026-09-27 on the Linux
        sandbox by writing the medium benchmark corpus's 19,077 passages into
        a fresh store on one thread (a 68MB database at the end):

        * 2MB (the default): 3.65 ms a document on average, rising from 0.74
          to 4.94 as the file grew - the "grows faster than the corpus" in
          the 5a note;
        * 4MB: 3.25 ms; 8MB: 1.58 ms; 16MB: 1.16 ms, rising only from 0.72 to
          1.21; 64MB: 1.22 ms - no better than 16MB.

        Four copies of that corpus (a 256MB database): 16MB of cache gave
        10.96 ms a document and 64MB 2.85 ms; 128MB, 2.67 ms. So the cache
        stops mattering at about **a quarter of the database file**, which is
        the size asked for here - at least `WRITE_CACHE_FLOOR_KIB`, at most
        `WRITE_CACHE_CEILING_KIB` (256MB: a large index keeps some of the
        slowdown rather than let one connection hold more memory than that).

        End to end, `app.cli bench-pipeline --full-speed`, fake embedder, same
        sandbox and day, runs interleaved with the version before: medium
        corpus 110.5 s -> 85.3 s median over 3 runs each (-22.8%, the ranges
        do not overlap), peak memory about 13MB higher (278 -> 291MB); small
        corpus 13.45 s -> 12.97 s over 5 each (-3.6%).

        **Only for the calling thread's connection**, because the setting
        belongs to a connection: the indexer's writing thread calls this, and
        the window's connections, the readers and search keep the default.
        The memory is only used as pages are actually read, and
        `restore_write_cache` gives it back. Asked again as the file grows
        (the pipeline does, at each checkpoint): a first index starts from an
        empty file. Nothing about what is written changes - only how much of
        the file SQLite keeps in memory while writing it.

        Returns the cache size now set, in KiB. Never raises: a failure here
        costs speed, never the run.
        """
        try:
            size = self.db_path.stat().st_size
        except OSError:
            size = 0
        kib = max(WRITE_CACHE_FLOOR_KIB, min(WRITE_CACHE_CEILING_KIB, size // 4 // 1024))
        local = self._local
        if getattr(local, "write_cache_kib", None) == kib:
            return kib
        try:
            conn = self.conn
            if getattr(local, "cache_before", None) is None:
                row = conn.execute("PRAGMA cache_size").fetchone()
                local.cache_before = int(row[0]) if row else -2000
            conn.execute(f"PRAGMA cache_size = -{int(kib)}")
            local.write_cache_kib = kib
        except Exception as exc:                  # noqa: BLE001 - speed only
            _log.debug("could not size the write cache: {}", exc)
        return kib

    def restore_write_cache(self) -> None:
        """Put this thread's page cache back as it was, and free the memory.

        The other half of `size_write_cache`, called when an index run ends.
        Never raises, for the same reason.
        """
        local = self._local
        before = getattr(local, "cache_before", None)
        if before is None:
            return
        local.cache_before = None
        local.write_cache_kib = None
        try:
            conn = self.conn
            conn.execute(f"PRAGMA cache_size = {int(before)}")
            conn.execute("PRAGMA shrink_memory")
        except Exception as exc:                  # noqa: BLE001 - speed only
            _log.debug("could not restore the page cache: {}", exc)

    def optimize_query_planner(self) -> bool:
        r"""Run `PRAGMA optimize`, refreshing the query planner's statistics.

        **§3c: moved here from the close path, and this is the other half of
        that move that was missing.** The close-path call was deleted with a
        comment saying it belonged on an idle timer instead - but nothing was
        ever added to call it from anywhere, silently losing the optimize
        entirely rather than relocating it. `MainWindow` schedules this on a
        coarse timer (hourly) while the app is open; this method is what
        that timer calls.

        Never raises - the same reasoning as `optimize_fts`: an older
        SQLite, a locked database mid-write, a store already closed by the
        time this fires - none of those are worth losing to. Returns
        whether it actually ran.
        """
        try:
            with self.write() as conn:
                conn.execute("PRAGMA optimize")
                # 2026-09-30: whatever it has just recorded about FTS5's own
                # tables must not stay - see `_forget_fts_statistics`.
                self._forget_fts_statistics(conn)
            return True
        except Exception as exc:                  # noqa: BLE001 - see the docstring
            _log.warning(
                "the query planner was not refreshed, so query plans may "
                "drift stale: {}", exc)
            return False

    def close(self) -> None:
        """Close every thread's connection. Safe to call more than once.

        Waits up to `_CLOSE_WAIT_S` for a worker mid-query; a connection still
        busy after that is retired and left to close itself (see below).
        """
        # Both, in the lock order set out in `__init__`: wait for an in-flight
        # write to finish rather than closing the connection underneath it.
        #
        # §3c: PRAGMA optimize moved to idle - see optimize_query_planner()
        # above. SQLite's own guidance recommends periodic (e.g., hourly)
        # PRAGMA optimize rather than at every close. Running it on the
        # close path delays shutdown while holding the single-instance lock,
        # which makes a relaunch wait unnecessarily.
        #
        # **Readers are the other half, and the lock does not cover them.** A
        # worker reading on its own connection holds neither lock, so it can be
        # mid-query right now - and closing a connection underneath a running
        # query crashes the process outright (see `_GuardedConnection`). So
        # each connection is retired first, any query in flight is interrupted,
        # and it is closed only once its thread has let go of it.
        with self._write_lock, self._conns_lock:
            self._closed = True
            me = threading.get_ident()
            for conn in self._open:
                if isinstance(conn, _GuardedConnection) and conn._retire() \
                        and conn._owner != me:
                    conn.interrupt()      # documented safe from any thread
            deadline = time.monotonic() + _CLOSE_WAIT_S
            for conn in self._open:
                if isinstance(conn, _GuardedConnection):
                    # Our own thread's connection cannot be mid-query while we
                    # are here - unless close() was called from inside a query
                    # on it, and then waiting would wait for ourselves.
                    in_use = (conn._busy > 0 if conn._owner == me else
                              not conn._wait_idle(max(0.0, deadline - time.monotonic())))
                    if in_use:
                        # **Left open rather than closed under its user.** It is
                        # retired, so nothing can start on it, and it closes
                        # itself when the thread holding it lets go of it.
                        _log.warning(
                            "a connection to {} was still busy {}s after close() "
                            "and was left to close itself", self.db_path, _CLOSE_WAIT_S)
                        continue
                try:
                    conn.close()
                except sqlite3.Error:
                    # A connection that will not close cleanly is still gone
                    # from `_open`; shutdown reports nothing it cannot act on.
                    pass
            self._open.clear()
            self._local = threading.local()
            self._migrated = False

    def __enter__(self) -> "SqliteStore":
        return self.connect()

    def __exit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc: Optional[BaseException],
        tb: Optional[TracebackType],
    ) -> None:
        self.close()

    @property
    def is_open(self) -> bool:
        """Whether the store can be read. Ask before a background thread tries.

        A worker started before the window closed can still be running after the
        store has been closed underneath it. Checking is far better than the
        alternative, which was three ERR_UNEXPECTED reports per keystroke about
        a database nobody is using any more.
        """
        return not self._closed and self._migrated

    @property
    def conn(self) -> sqlite3.Connection:
        """This thread's connection, opened on first use.

        **Opening lazily is the point.** Qt hands work to threads this code
        never sees created, so requiring each one to call `connect()` would
        mean every worker either remembering to, or quietly sharing the main
        thread's connection again - which is the bug this replaced.
        """
        if self._closed:
            raise AppErrorException(make_error(
                "ERR_UNEXPECTED", "storage.sqlite",
                details="SqliteStore used before connect(), or after close().",
                suggestion=(
                    "If this appeared while closing the window, a background "
                    "search outlived the store and the message is harmless."
                ),
            ))

        conn = getattr(self._local, "conn", None)
        if conn is not None:
            return conn

        with self._conns():
            if self._closed:                       # closed while we waited
                raise AppErrorException(make_error(
                    "ERR_UNEXPECTED", "storage.sqlite",
                    details="SqliteStore was closed while a worker was using it.",
                    suggestion=(
                        "If this appeared while closing the window, a background "
                        "search outlived the store and the message is harmless."
                    ),
                ))
            if not self._migrated:
                raise AppErrorException(make_error(
                    "ERR_UNEXPECTED", "storage.sqlite",
                    details="SqliteStore used before connect().",
                    suggestion="Open the store with `with SqliteStore(path) as store:`.",
                ))
            conn = self._new_connection()
            self._adopt(conn)
            return conn

    @property
    def schema_version(self) -> int:
        """The version recorded in the file; `CURRENT_VERSION` after connect."""
        return read_version(self.conn)

    @contextmanager
    def write(self, *, deferring: bool = False) -> Iterator[sqlite3.Connection]:
        """One serialised write transaction. Rolls back on any exception.

        **Joins an open `batch()` rather than nesting inside it.** SQLite has
        no nested transactions, so a `write()` called while a batch is open on
        this thread would otherwise commit the batch early - turning the
        grouping into a lie without failing.

        **`deferring` is for the three writes that know about `_deferred`**
        (`upsert_file`, `replace_chunks`, `set_message`). Every other write
        that joins a batch first has the waiting keyword-index rows written
        and the triggers switched back on, so it runs against exactly the
        database it would have found before 2026-09-30 - whatever it deletes,
        updates or inserts. Nothing has to be remembered when a new write
        method is added: it is safe by default, and only slower if it happens
        to run between two documents of a bulk write.
        """
        if getattr(self._local, "batch_depth", 0):
            conn = self.conn
            if not deferring:
                self._finish_deferred(conn)
            yield conn
            return

        with self._write_lock:
            conn = self.conn
            self._begin_write(conn)
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            else:
                conn.execute("COMMIT")

    def _begin_write(self, conn: sqlite3.Connection) -> None:
        """`BEGIN IMMEDIATE`, asked twice before giving up - and giving up says so.

        2026-10-07. Naming a face in the window while an index run was sorting
        faces failed with a bare `database is locked`, reported as "an
        unexpected error ... this is a bug". The run is another process making
        one small write after another; SQLite's wait for the write lock is a
        poll, not a queue, so the window can lose every poll for the whole of
        `busy_timeout` (UNCONFIRMED that this is what happened - it is what the
        log is consistent with: face-sorting writes throughout the 30 s the
        rename waited). A second wait costs nothing when the lock is free, and
        a lock that outlasts both is `ERR_DB_BUSY`, which says what to do."""
        for attempt in (1, 2):
            try:
                conn.execute("BEGIN IMMEDIATE")
                return
            except sqlite3.OperationalError as exc:
                text = str(exc).lower()
                if "locked" not in text and "busy" not in text:
                    raise
                if attempt == 2:
                    raise self._busy_error(
                        f"Another process kept the write lock on {self.db_path} for "
                        f"{2 * self._timeout:g} s ({exc}).") from exc
                _log.warning(
                    "the index's write lock was held for {:g} s by another process "
                    "- waiting once more ({})", self._timeout, exc)

    @contextmanager
    def batch(self) -> Iterator[sqlite3.Connection]:
        """Group many writes into one transaction. All of them, or none.

            with store.batch():
                store.upsert_file(...)
                store.replace_chunks(...)

        Indexing one mail message cost **six** commits - `upsert_file`,
        `replace_chunks`, `set_message`, two `set_state` calls for the
        checkpoint, and `mark_indexed`. At twenty million messages that is
        rather over an hour of the run spent committing, for work that is one
        step as far as anyone reading the code is concerned.

        **Keep slow work out of the block.** The write lock is held for the
        whole batch, so anything inside it delays every other thread's writes.
        Embedding, extraction and LanceDB calls belong outside; only the SQLite
        statements belong in.

        The depth counter is per-thread, so a batch on the indexing thread
        never quietly strips the transaction from a write on the UI thread.
        """
        if getattr(self._local, "batch_depth", 0):
            # Already inside one. Re-entering must not start a second
            # transaction, and must not commit the outer one on the way out.
            self._local.batch_depth += 1
            try:
                yield self.conn
            finally:
                self._local.batch_depth -= 1
            return

        with self._write_lock:
            conn = self.conn
            # 2026-10-10, storage review S3: through `_begin_write`, as `write()`
            # already was. A bare `BEGIN IMMEDIATE` here asked for the write lock
            # once, and when another process (an index run beside the window)
            # held it past `busy_timeout` the batch failed with a bare
            # `sqlite3.OperationalError: database is locked` - the "this is a
            # bug" report `_begin_write` was written to end. Now the same second
            # wait and the same `ERR_DB_BUSY` that says what to do. Nothing of the
            # batch's own state is set until the lock is held, so a refusal
            # leaves this thread exactly as it was.
            self._begin_write(conn)
            self._local.batch_depth = 1
            # 0x 5d: this transaction has not moved the generation yet. See
            # `_bump_generation`.
            self._local.bumped = False
            try:
                yield conn
                # 2026-09-30: the keyword-index rows that waited for the end
                # of the batch, written in the same transaction as the rows
                # they index - so the commit below lands both or neither.
                # Inside the `try`: if this fails the whole batch rolls back.
                self._finish_deferred(conn)
            except BaseException:
                self._abandon_deferred(conn)
                conn.execute("ROLLBACK")
                raise
            else:
                conn.execute("COMMIT")
            finally:
                self._local.batch_depth = 0
                self._local.deferred = None

    # -- the keyword index, written once per batch ---------------------------

    def _deferred(self) -> Optional[_DeferredFts]:
        r"""Where this thread's batch keeps keyword-index rows until its end.

        None when nothing may wait: outside a `batch()`, with `defer_fts` off,
        during a bulk run that dropped the triggers, or on a Python too old to
        switch a connection's triggers.

        **Why rows wait at all (measured 2026-09-30, order 0z, "writing
        measured").** `chunks_fts` and `messages_fts` are kept in step by
        triggers. FTS5 gathers what a transaction writes in memory and writes
        it to the index as one piece (a "segment") at the commit - unless a
        statement opens a savepoint first, when it writes what it holds there
        and then (`fts5SavepointMethod`). Every `INSERT` on a table with a
        trigger opens one. So with the triggers doing the work, **each passage
        and each message became its own segment, however many documents shared
        the transaction**, and FTS5 then merged those segments sixteen at a
        time: about 3,850 and 6,500 index pages written for every 1,000
        messages, and 1,090 and 1,650 segments removed, against roughly a
        fifth of the pages and about 50 removals when the rows wait. (What
        made the cost *rise* with the index was each removal reading the whole
        table - `_forget_fts_statistics`. This is the smaller change: with
        that fixed, 1,000 messages took 1.5-1.8 s of processor time written
        through the triggers and 0.8-1.1 s written this way, this laptop,
        20,000-30,000 messages in the index.)

        **What happens instead.** Inside a batch, a passage or message that is
        *new* is inserted with this connection's triggers switched off
        (`sqlite3.Connection.setconfig`, which also makes SQLite prepare the
        affected statements again, so a cached statement cannot keep its
        trigger), and the row its trigger would have written is kept here. At
        the end of the batch the rows are written to the index with one
        `executemany` - no trigger, no savepoint, one segment - the triggers
        are switched back on, and the batch commits.

        **What is not changed, and why it is safe.**

        * The schema, the triggers and the index's contents are what they
          were: the rows written are the rows the triggers would have written
          (`test_fts_deferred_writes.py` builds the same mail both ways and
          compares every search).
        * The content rows and their index rows are in **one transaction**. A
          run killed at any point leaves both or neither; there is nothing to
          repair and no flag to read on the next start.
        * The triggers are only ever off on the writing thread's own
          connection, only inside its batch, and only for inserts of rows
          known to be new. A file that already has passages, a message that
          already has a row, and every other write (`write()` without
          `deferring`) get the waiting rows written and the triggers back on
          first, and then run as they always did.
        * Another thread or process sees nothing until the commit, as before.

        **The one thing that is different inside the batch:** a keyword search
        run by the writing thread itself, on its own connection, before the
        batch ends would not see that batch's new rows. Nothing does that -
        the indexer's writing thread does not search - and every other
        connection only ever saw committed rows.
        """
        local = self._local
        if not getattr(local, "batch_depth", 0):
            return None
        state = getattr(local, "deferred", None)
        if state is not None:
            return state
        if (not self.defer_fts or self._content_triggers_dropped
                or _TRIGGER_SWITCH is None):
            return None
        state = local.deferred = _DeferredFts()
        return state

    @staticmethod
    def _switch_triggers(conn: sqlite3.Connection, state: _DeferredFts, *, on: bool) -> None:
        """Switch this connection's triggers, if they are not already that way."""
        if state.triggers_off == (not on):
            return
        # Through the connection's own guard where it has one, so a window
        # closing under the indexer is reported as that and not as a bug.
        guarded = getattr(conn, "_call", None)
        if guarded is not None:
            guarded(sqlite3.Connection.setconfig, conn, _TRIGGER_SWITCH, on)
        else:
            conn.setconfig(_TRIGGER_SWITCH, on)   # type: ignore[arg-type]
        state.triggers_off = not on

    def _index_deferred(self, conn: sqlite3.Connection, state: _DeferredFts) -> None:
        """Write the waiting rows to the keyword index. The triggers stay as they are.

        Two `executemany` calls straight into the FTS tables: what `chunks_ai`
        and `messages_ai` do, a row at a time, for a row they are told about.
        """
        if state.broken:
            raise AppErrorException(make_error(
                "ERR_UNEXPECTED", "storage.sqlite",
                details="The keyword index could not be written earlier in this "
                        "batch, so the batch cannot be committed.",
            ))
        if not state.chunks and not state.messages:
            return
        try:
            if state.chunks:
                conn.executemany(
                    "INSERT INTO chunks_fts(rowid, text, symbols) VALUES (?, ?, ?)",
                    state.chunks)
            if state.messages:
                conn.executemany(
                    "INSERT INTO messages_fts(rowid, subject, sender, recipients) "
                    "VALUES (?, ?, ?, ?)",
                    [(file_id, *values) for file_id, values in state.messages.items()])
        except BaseException:
            # Some rows may be in and some not, and nothing here can tell
            # which. The batch must not commit; `batch()` rolls it back.
            state.broken = True
            raise
        state.chunks = []
        state.chunk_files.clear()
        state.messages = {}

    def _finish_deferred(self, conn: sqlite3.Connection) -> None:
        """Waiting rows written, triggers on: the database as any write expects it."""
        state = getattr(self._local, "deferred", None)
        if state is None:
            return
        try:
            self._index_deferred(conn, state)
        finally:
            self._switch_triggers(conn, state, on=True)

    def _abandon_deferred(self, conn: sqlite3.Connection) -> None:
        """The batch is being rolled back: forget the waiting rows. Never raises.

        The rows they index are rolled back with it, so there is nothing to
        write. The triggers go back on whatever else happens - a connection
        left with them off would stop keeping the keyword index at all.
        """
        state = getattr(self._local, "deferred", None)
        if state is None:
            return
        state.chunks = []
        state.chunk_files.clear()
        state.messages = {}
        try:
            self._switch_triggers(conn, state, on=True)
        except Exception as exc:                  # noqa: BLE001 - a rollback must go on
            _log.warning("could not switch the index triggers back on: {}", exc)

    # -- files ---------------------------------------------------------------

    def upsert_file(
        self,
        path: str,
        *,
        size_bytes: int,
        mtime_ns: int,
        ext: Optional[str] = None,
        parent_dir: Optional[str] = None,
        content_hash: Optional[str] = None,
        status: str = FileStatus.PENDING,
        source_kind: str = "file",
        repo_id: Optional[int] = None,
        clear_hash: bool = False,
        taken_at_ns: Optional[int] = None,
        volume_id: Optional[int] = None,
        relative_path: Optional[str] = None,
        taken_at_is_hint: bool = False,
        place: Optional[str] = None,
    ) -> int:
        r"""Insert or update one file row. Returns its id.

        **`clear_hash=True` says "I have no hash for what is there now".**
        `content_hash` is COALESCEd for the same reason `repo_id` is, and that
        default is wrong for a caller that has just written new content without
        hashing it: `--fast` and the name-only pass both leave the digest of the
        *previous* contents sitting beside the new size and mtime. A later
        verifying run then compares against it, and the one case where that is
        not merely wasteful is a file restored to an older version - the old
        hash matches, the row is declared unchanged, and the index keeps serving
        text that is no longer in the file. A row with no hash is honest; a row
        with somebody else's hash is not.

        `repo_id` is additive and optional: every existing caller keeps working
        and writes NULL, which is what a file outside any repository is.

        **`taken_at_ns` is a photograph's EXIF shot date, and it is COALESCEd
        on conflict for exactly the reason `repo_id` is.** Work order 0f §3a.
        `_record_skip`, the PST path and most tests call this without it, and
        none of them should be able to erase a shot date the images pass
        established - a photo that failed to re-OCR on a later run would
        otherwise silently revert to filtering by its copy date. It is written
        beside `mtime_ns`, never into it: `mtime_ns` remains the file's real
        mtime because H1's change detection compares against it.

        **`NO_REPO` is how a caller says "no repository" and means it.** NULL
        cannot: the `ON CONFLICT` below COALESCEs it so that callers which know
        nothing about repositories - `_record_skip`, the PST path, every test -
        cannot blank an attribution the indexer established. That guard is
        right on its own and became a trap in combination with two others; see
        `forget_repo`. A defensive default that cannot be overridden is not a
        guard, it is a one-way door, so this is the door's handle.

        `volume_id`/`relative_path` are additive and optional, the same shape
        as `repo_id`: NULL for a file outside any catalogued source, COALESCEd
        on conflict so a caller that knows nothing about Offline Media -
        every extractor, `_record_skip`, every existing test - cannot blank an
        attribution a volume Scan established. They always travel together;
        nothing sets one without the other.
        """
        if status not in FileStatus.ALL:
            raise AppErrorException(make_error(
                "ERR_UNEXPECTED", "storage.sqlite",
                details=f"Invalid file status '{status}'.",
            ))

        clearing = 1 if repo_id == NO_REPO else 0
        stored_repo = None if clearing else repo_id
        forget_hash = 1 if clear_hash else 0

        as_path = Path(path)
        # **Normalised here, not trusted from the caller.** `files.ext` is stored
        # without a leading dot, and `type:pdf` compares against exactly that.
        # A caller passing ".pdf" would write a row no `type:` filter could ever
        # match - the file would be indexed, searchable by text, and invisible to
        # every filter, with nothing anywhere to explain it. The store owns this
        # invariant because the store is what depends on it.
        ext = (ext if ext is not None else as_path.suffix).lower().lstrip(".") or ""
        parent_dir = parent_dir if parent_dir is not None else str(as_path.parent)

        # `deferring`: a row in `files` has no trigger, and no keyword-index
        # row of its own that could be waiting - see `_deferred`.
        with self.write(deferring=True) as conn:
            # 2026-10-04, code review: what the row was, so the filename index
            # is written only when it has to be (below). One lookup on the
            # unique index, in place of the one that followed the upsert.
            before = conn.execute(
                "SELECT id, parent_dir, source_kind FROM files WHERE path = ?",
                (str(path),)).fetchone()
            conn.execute(
                """
                INSERT INTO files
                    (path, parent_dir, ext, size_bytes, mtime_ns, content_hash,
                     status, source_kind, repo_id, taken_at_ns, volume_id,
                     relative_path, taken_at_is_hint, place)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(path) DO UPDATE SET
                    parent_dir   = excluded.parent_dir,
                    ext          = excluded.ext,
                    size_bytes   = excluded.size_bytes,
                    mtime_ns     = excluded.mtime_ns,
                    -- The flag, like `repo_id` below: NULL cannot carry the
                    -- difference between "no hash to offer" and "forget the
                    -- one you have", and the COALESCE exists to protect the
                    -- first of those.
                    content_hash = CASE
                        WHEN ? = 1 THEN NULL
                        ELSE COALESCE(excluded.content_hash, files.content_hash)
                    END,
                    status       = excluded.status,
                    -- 2026-09-30: a NAME_ONLY row says "nothing went wrong, no
                    -- reader", so a skip left from an earlier pass is cleared
                    -- with it (a local file once misread as a cloud one kept
                    -- `ERR_CLOUD_ONLY` beside its new status).
                    skip_code    = CASE WHEN excluded.status = 'NAME_ONLY'
                                        THEN NULL ELSE files.skip_code END,
                    skip_detail  = CASE WHEN excluded.status = 'NAME_ONLY'
                                        THEN NULL ELSE files.skip_detail END,
                    source_kind  = excluded.source_kind,
                    -- COALESCE, so a caller that does not know about
                    -- repositories - `_record_skip`, the PST path, any test -
                    -- does not blank an attribution the indexer established.
                    -- **The flag, not the value.** `NO_REPO` cannot travel as
                    -- -1 in `repo_id` itself: the column has a foreign key to
                    -- `repos(id)`, so inserting a new row with -1 would be
                    -- refused. It is translated to a bound flag in Python and
                    -- the value bound as NULL, which the INSERT accepts and
                    -- this CASE can still tell apart from "did not say".
                    repo_id      = CASE
                        WHEN ? = 1 THEN NULL
                        ELSE COALESCE(excluded.repo_id, files.repo_id)
                    END,
                    -- COALESCEd for the same reason `repo_id` above is: the
                    -- callers that write a row for a photo without knowing
                    -- its shot date must not erase one. See the docstring.
                    taken_at_ns  = COALESCE(excluded.taken_at_ns,
                                            files.taken_at_ns),
                    -- Offline Media's own attribution, same COALESCE reasoning
                    -- as repo_id above. The two always travel together, so one
                    -- CASE covers both rather than two independent flags.
                    volume_id     = COALESCE(excluded.volume_id, files.volume_id),
                    relative_path = COALESCE(excluded.relative_path, files.relative_path),
                    -- Work order 0i section 4b. Paired with taken_at_ns
                    -- above, not independently COALESCEd: whether a date is
                    -- a fact or a guess only means anything alongside the
                    -- date itself, so the flag follows the same "only a
                    -- caller providing a new date gets to say" rule.
                    taken_at_is_hint = CASE
                        WHEN excluded.taken_at_ns IS NOT NULL THEN excluded.taken_at_is_hint
                        ELSE files.taken_at_is_hint
                    END,
                    -- Work order 0i section 4a. COALESCEd for the same
                    -- reason taken_at_ns is: a caller re-touching a file
                    -- without a GPS answer of its own (a resume, a retry)
                    -- must not blank a place an earlier pass established.
                    place = COALESCE(excluded.place, files.place)
                """,
                (str(path), parent_dir, ext, size_bytes, mtime_ns,
                 content_hash, status, source_kind, stored_repo,
                 None if taken_at_ns is None else int(taken_at_ns),
                 volume_id, relative_path,
                 1 if taken_at_is_hint else 0,
                 place,
                 forget_hash, clearing),
            )
            if before is not None:
                file_id = int(before["id"])
            else:
                row = conn.execute("SELECT id FROM files WHERE path = ?",
                                   (str(path),)).fetchone()
                file_id = int(row["id"])
            # Keep the filename index in step. Only real files: a PST message's
            # "path" is a synthetic key nobody typed and nobody would recognise,
            # and mail would outnumber documents ten to one in a Files list.
            # An attachment is found by its name too, like a file (2026-10-01).
            #
            # 2026-10-04, code review: **only when the name or folder can have
            # changed.** It was deleted and written again on every read of
            # every file - and names-first (`add_waiting_files`) had already
            # written it for each one. The name is the path's, which is the
            # row's key, so only a new row, a new `parent_dir` or a row that
            # was not named before needs it.
            named = _named_in_fts(path, source_kind)
            was_named = before is not None and _named_in_fts(path, before["source_kind"])
            if named and not (was_named and before["parent_dir"] == parent_dir):
                conn.execute("DELETE FROM files_fts WHERE rowid = ?", (file_id,))
                conn.execute(
                    "INSERT INTO files_fts(rowid, name, folder) VALUES (?, ?, ?)",
                    (file_id, _basename(str(path)), parent_dir),
                )
            elif was_named and not named:
                conn.execute("DELETE FROM files_fts WHERE rowid = ?", (file_id,))
            self._bump_generation(conn)
        return file_id

    def add_waiting_files(self, rows: Sequence[dict[str, Any]]) -> int:
        """A `PENDING` row for each file not in the index yet; returns how many.

        2026-10-04, the owner: "it should get the names first and then scan
        faces or text ... file list comes up first". The run's scan writes the
        names of the files it is about to read, so the Files page lists every
        one - by name, searchable - before the first is read.

        **INSERT OR IGNORE, so a row already there is never touched**: an
        INDEXED file that changed keeps its row, and its words, until it is
        read again. A `PENDING` row is never taken for a finished one -
        `Pipeline._classify` settles only INDEXED, NAME_ONLY and skipped rows,
        which is how an interrupted run already resumed. `rows` carry `path`,
        `parent_dir`, `ext`, `size_bytes`, `mtime_ns` and optionally
        `volume_id` and `relative_path`. One transaction for the batch.
        """
        added = 0
        with self.write(deferring=True) as conn:
            for row in rows:
                cursor = conn.execute(
                    """
                    INSERT OR IGNORE INTO files
                        (path, parent_dir, ext, size_bytes, mtime_ns, status, source_kind,
                         volume_id, relative_path)
                    VALUES (?, ?, ?, ?, ?, 'PENDING', 'file', ?, ?)
                    """,
                    (str(row["path"]), str(row["parent_dir"]), str(row.get("ext") or ""),
                     int(row.get("size_bytes") or 0), int(row.get("mtime_ns") or 0),
                     row.get("volume_id"), row.get("relative_path")),
                )
                if cursor.rowcount == 1:
                    added += 1
                    conn.execute(
                        "INSERT INTO files_fts(rowid, name, folder) VALUES (?, ?, ?)",
                        (cursor.lastrowid, _basename(str(row["path"])), str(row["parent_dir"])))
            if added:
                self._bump_generation(conn)
        return added

    def get_file(self, path: str) -> Optional[FileRecord]:
        """The row keyed exactly by `path` (case as stored), or None."""
        row = self.conn.execute("SELECT * FROM files WHERE path = ?", (str(path),)).fetchone()
        return FileRecord.from_row(row) if row else None

    def get_file_by_id(self, file_id: int) -> Optional[FileRecord]:
        """The row with this id, or None once it has been deleted."""
        row = self.conn.execute("SELECT * FROM files WHERE id = ?", (file_id,)).fetchone()
        return FileRecord.from_row(row) if row else None

    def restamp_files(self, rows: Iterable[tuple[str, int, int]]) -> int:
        """Record `(path, size_bytes, mtime_ns)` for files whose bytes were
        hashed and found unchanged. One transaction. Returns how many rows.

        2026-10-10, indexing review W4 follow-up. A file whose date moved but
        whose content did not - a robocopy copy, a restore, a cloud sync - was
        hashed, matched, and skipped, but its row kept the old date, so the
        next run hashed it again, and every run after that. Writing the date
        the hash vouched for makes the cheap tier answer next time."""
        params = [(int(size), int(mtime), str(path)) for path, size, mtime in rows]
        if not params:
            return 0
        with self.write() as conn:
            conn.executemany(
                "UPDATE files SET size_bytes = ?, mtime_ns = ? WHERE path = ?", params)
        return len(params)

    def mark_indexed(self, file_id: int) -> None:
        """One file as INDEXED, its skip cleared. See `mark_indexed_many`."""
        self.mark_indexed_many((file_id,))

    def mark_indexed_many(self, file_ids: Iterable[int]) -> None:
        """Every file in one transaction, not one transaction each.

        `_embed_pending` finishes a batch of a few hundred chunks and then has
        to mark every file they came from - dozens of files, and dozens of
        commits, for one logical step. A commit is roughly fourteen times the
        cost of the same write inside an open transaction, so this is the
        difference on its own.
        """
        ids = [(int(file_id),) for file_id in file_ids]
        if not ids:
            return
        now = int(time.time())
        with self.write() as conn:
            conn.executemany(
                "UPDATE files SET status = ?, indexed_at = ?, skip_code = NULL, "
                "skip_detail = NULL WHERE id = ?",
                [(FileStatus.INDEXED, now, file_id) for (file_id,) in ids],
            )

    def set_phashes(self, phashes: dict[int, str]) -> None:
        r"""Write perceptual hashes for photos, one transaction for the batch.

        Work order 0h §2a. Same batching reasoning as `mark_indexed_many`
        just above: `Pipeline._flush_pending_phashes` gathers one pHash per
        photo, computed synchronously and independently of the CLIP vector
        (see `_maybe_compute_phash`'s docstring), and flushes them together
        at the same checkpoints the CLIP vectors are flushed at - so this is
        called once per flush, not once per photo. A run over a folder of
        ten thousand photos costs one commit here per flush, not ten
        thousand.

        Rows whose hash is empty or `None` are dropped rather than written
        as an empty string - `files.phash` is nullable precisely so "not
        computed yet" stays distinguishable from "computed and empty", and
        the latter can never legitimately happen (`PhashComputer.compute`
        raises rather than returning one).
        """
        items = [(str(value), int(file_id))
                 for file_id, value in phashes.items() if value]
        if not items:
            return
        with self.write() as conn:
            conn.executemany(
                "UPDATE files SET phash = ? WHERE id = ?", items)

    def set_file_tags(self, file_id: int, tags: Sequence[str]) -> None:
        r"""Replace one file's Florence-2 tags. Work order 0i section 1c.

        **Replace, not append.** Re-indexing a photo (a re-tag after a model
        upgrade, a forced re-run) must not accumulate duplicate rows forever -
        the old set for this `file_id` is cleared first, in the same
        transaction, so a crash between the two leaves either the old tags
        or the new ones, never both and never neither.

        Called once per photo from `Pipeline._write_one`, straight after
        `upsert_file` gives it a `file_id` - not batched across a flush the
        way `set_phashes` is, because tags are produced per-image already
        (the Florence-2 call itself is the expensive part; this write is one
        DELETE and a handful of INSENTs) and 0i's own `_write_one` call site
        already holds a transaction it can reuse.

        An empty or all-blank `tags` still clears any old row for this file
        and writes nothing new - the correct state for a photo that used to
        have tags before a re-tag found none.
        """
        # Deduplicated, order preserved - `dict.fromkeys` rather than a
        # set, the same convention `florence_tagger.tag_image` already uses
        # for its own label dedup, so "Dog" and "dog" collapse to one row.
        raw = [str(tag).strip().lower() for tag in tags if str(tag).strip()]
        cleaned = list(dict.fromkeys(raw))
        with self.write() as conn:
            conn.execute("DELETE FROM file_tags WHERE file_id = ?", (file_id,))
            if cleaned:
                conn.executemany(
                    "INSERT INTO file_tags (file_id, tag) VALUES (?, ?)",
                    [(file_id, tag) for tag in cleaned],
                )

    def mark_skipped(self, file_id: int, error: AppError) -> None:
        """Record why a file was skipped, so the UI can group and retry.

        A single bad file must never halt a 100GB run: this is how it is
        remembered instead of raised.
        """
        status = FileStatus.SKIPPED if not error.is_fatal else FileStatus.FAILED
        with self.write() as conn:
            conn.execute(
                "UPDATE files SET status = ?, skip_code = ?, skip_detail = ? WHERE id = ?",
                (status, error.code, skip_sentence(error), file_id),
            )

    def skip_details(self, code: str, *, limit: int = 3) -> list[dict[str, str]]:
        """A few of the files skipped for `code`, each with what was recorded:
        `{"path", "detail"}`, oldest first. For the skipped panel's "detail
        below" (2026-10-05) - read on a worker, never on the window's thread."""
        rows = self.conn.execute(
            "SELECT path, skip_detail FROM files WHERE skip_code = ? ORDER BY id LIMIT ?",
            (str(code), max(1, int(limit))))
        return [{"path": row["path"], "detail": row["skip_detail"] or ""} for row in rows]

    #: A trigram index holds no trigram for a shorter term, so it cannot answer
    #: one. Two characters fall back to the scan, which is what they did before.
    TRIGRAM_MIN_CHARS = 3

    def _header_match(self, column: str, value: str) -> Optional[str]:
        r"""An FTS5 query for `column LIKE '%value%'`, or None to scan instead.

        None for three reasons, all of them "the index cannot answer this":
        the term is shorter than a trigram, the table is not there (an index
        built before v8, or a SQLite without trigram support), or the value is
        empty.

        **The term is quoted as a phrase**, which is not decoration: an address
        or a subject line is full of characters FTS5 reads as operators, and
        `MATCH acme.com` is a syntax error rather than a search. Doubling any
        embedded quote is what stops a subject line ending the phrase early -
        the same reasoning as escaping `%` in the LIKE path below.
        """
        text = (value or "").strip()
        if len(text) < self.TRIGRAM_MIN_CHARS or not self._has_message_index():
            return None
        if has_wildcard(text):
            # A phrase cannot say "any characters here"; the scan can.
            return None
        phrase = '"' + text.replace('"', '""') + '"'
        return f"{column} : {phrase}"

    def _has_message_index(self) -> bool:
        """Is `messages_fts` present? Asked once, then remembered.

        A property of the database file rather than of a connection, so one
        answer serves every thread - and two threads racing to compute it both
        arrive at the same one. This is on the query path of a tab that filters
        live; `sqlite_master` is a query like any other and does not want asking
        per keystroke.
        """
        if self._message_index is None:
            row = self.conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='messages_fts'"
            ).fetchone()
            self._message_index = row is not None
        return bool(self._message_index)

    def optimize_fts(self, *, time_budget_s: Optional[float] = None) -> bool:
        r"""Merge the FTS5 index's b-tree segments into one. Never raises.

        *Note, 2026-10-04: no longer true - `Pipeline` calls this after a
        large run. The paragraph below is kept as written.*

        **This has never been run in this application.** `PRAGMA optimize` is
        called on close, and that is the query planner's statistics - a
        different thing entirely. FTS5 keeps its own segmented index, one
        segment per batch of inserts, and every query touches all of them. An
        index built over tens of millions of chunks across a week-long run
        accumulates thousands, and keyword search gets slower in proportion,
        for ever, with nothing to explain it.

        `INSERT INTO chunks_fts(chunks_fts) VALUES('optimize')` is the merge.
        It rewrites the whole index, so it is **not** something to do after
        every incremental pass - the caller decides, and `Pipeline` only asks
        after a run that added a meaningful number of chunks.

        Returns whether it ran. A failure costs a slower index, never a run:
        an older SQLite without FTS5, a locked database, an index that is not
        there yet - none of those are reasons to fail a week of work at the
        very end of it.

        2026-10-04: the filename and mail-header indexes are merged too. Only
        `chunks_fts` was, so `files_fts` and `messages_fts` kept a segment per
        write for the life of the index. Either may be absent (an old schema,
        a SQLite without trigram); that skips the one, not the merge.

        *Note, 2026-10-10 (storage review S6): no longer one `'optimize'`.* That
        rewrote all three indexes inside one write transaction, so at ten
        million passages the write lock was held for minutes and every write
        from the window - a rename, a tag, a face named - waited behind it or
        failed. The same end state is now reached in steps: `'merge'` with a
        negative page count (`FTS_MERGE_PAGES`) is FTS5's incremental form of
        `'optimize'` - every segment is put on one level and merged a few
        hundred pages at a time - each step its own short transaction, the
        lock let go between steps. A step that changes fewer than two rows did
        nothing (FTS5's documented test, checked 2026-10-10: the step after
        the last real one changes exactly 1, and an `'optimize'` afterwards
        also changes 1 - one segment is left, as `'optimize'` leaves).
        `time_budget_s` stops between steps once spent - the next call carries
        on where this one stopped - and is unlimited by default, which keeps
        the old meaning for `Pipeline`. True when every step ran, a stop on
        the budget included.
        """
        deadline = (None if time_budget_s is None
                    else time.monotonic() + max(0.0, float(time_budget_s)))
        try:
            for fts in ("chunks_fts", "files_fts", "messages_fts"):
                if fts != "chunks_fts" and self.conn.execute(
                        "SELECT 1 FROM sqlite_master WHERE name = ?",
                        (fts,)).fetchone() is None:
                    continue
                steps = 0
                while True:
                    if deadline is not None and time.monotonic() >= deadline:
                        _log.info(
                            "keyword index merge stopped at its time budget in {} "
                            "after {} step(s); the next merge carries on", fts, steps)
                        return True
                    with self.write() as conn:
                        before = conn.total_changes
                        conn.execute(
                            f"INSERT INTO {fts}({fts}, rank) VALUES('merge', ?)",
                            (-FTS_MERGE_PAGES,))
                        changed = conn.total_changes - before
                    steps += 1
                    if changed < 2:
                        break
            return True
        except Exception as exc:                  # noqa: BLE001 - see the docstring
            # **Deliberately every exception, not just `sqlite3.Error`.** A
            # closed store raises `AppErrorException` from `write()`, and a
            # background job that outlives the window hits exactly that - at
            # which point "the keyword index could not be merged" must not be
            # the thing that fails a week of work.
            _log.warning(
                "the keyword index was not merged, so searches stay slower "
                "than they need to be: {}", exc)
            return False

    def drop_fts_triggers(self) -> list[str]:
        """Drop FTS content triggers for bulk insert, setting the dirty flag.

        **Always sets the dirty flag first.** If this call or a crash happens
        after it but before the triggers are dropped, resume will see the flag
        and rebuild FTS, protecting against loss of searchability.

        Returns the SQL needed to restore the triggers, or an empty list if the
        drop failed. A failure to drop the triggers is logged but does not fail
        the run — FTS will simply update row by row as it always has, costing
        performance but not correctness.
        """
        try:
            # Mark FTS as dirty *before* dropping the triggers, so an
            # interrupted run knows it needs rebuilding on resume.
            self.set_state("fts_dirty", "1")
            with self.write() as conn:
                dropped = self._suspend_content_triggers(conn)
            # From here until they come back nothing writes the keyword index,
            # `_deferred` included: the run rebuilds it whole at its end.
            self._content_triggers_dropped = bool(dropped)
            return dropped
        except Exception as exc:                  # noqa: BLE001
            _log.warning(
                "FTS content triggers could not be dropped, "
                "word index will update row-by-row: {}", exc)
            return []

    def restore_fts_triggers(self, trigger_sql: list[str]) -> bool:
        """Restore FTS content triggers after a bulk insert.

        Returns whether the restore succeeded. A failure is logged but does not
        fail the run — the triggers are optional optimizations, not required.

        **2026-09-30: and rebuilds the two indexes, in the same transaction.**
        It did not. A bulk run that *finished* ("Word index: Always bulk-load",
        `INDEX_BULK_FTS=on`) put the triggers back, cleared the dirty flag and
        merged - and nothing ever indexed what the run had written while the
        triggers were away, so every passage and message of that run was
        missing from keyword search for good. Only a run that was *killed* got
        the rebuild, from `check_and_rebuild_fts_if_dirty`. Reproduced at the
        store (`test_a_finished_bulk_run_can_be_searched`). The rebuild reads
        `chunks` and `messages`; no file is read again. It is the whole index,
        so it takes as long as the index is large - which is what a bulk load
        is. If it fails this returns False, the caller leaves the dirty flag
        set, and the next run repairs it.
        """
        if not trigger_sql:
            return False
        try:
            with self.write() as conn:
                for sql in trigger_sql:
                    if sql:
                        conn.execute(sql)
                conn.execute("INSERT INTO chunks_fts(chunks_fts) VALUES('rebuild')")
                if conn.execute(
                        "SELECT 1 FROM sqlite_master WHERE name = 'messages_fts'"
                ).fetchone() is not None:
                    conn.execute(
                        "INSERT INTO messages_fts(messages_fts) VALUES('rebuild')")
            self._content_triggers_dropped = False
            return True
        except Exception as exc:                  # noqa: BLE001
            _log.warning(
                "FTS content triggers could not be restored: {}", exc)
            return False

    def check_and_rebuild_fts_if_dirty(self) -> None:
        """Check for FTS dirty flag and rebuild if set.

        Called at the start of a run to recover from an interrupted bulk
        insert that dropped the triggers but was killed before rebuilding.
        """
        if self.get_state("fts_dirty"):
            _log.info("rebuilding word index after interrupted bulk run")
            try:
                with self.write() as conn:
                    # **Put the triggers back first.** The interrupted run dropped
                    # them and died before restoring them, and the drop is stored
                    # in the database. A rebuild alone repairs the rows written so
                    # far and leaves nothing to index the *next* chunk - every
                    # document indexed after a resume was silently missing from
                    # the word index (found 2026-09-20 by reproducing it).
                    has_messages = conn.execute(
                        "SELECT 1 FROM sqlite_master WHERE name = 'messages_fts'").fetchone()
                    for sql in CONTENT_TRIGGERS:
                        # The mail triggers write into `messages_fts`; where that table
                        # could not be created (no trigram) they never existed, and
                        # creating them now would make every message insert fail.
                        if sql.startswith("CREATE TRIGGER IF NOT EXISTS messages_") and not has_messages:
                            continue
                        conn.execute(sql)
                    conn.execute(
                        "INSERT INTO chunks_fts(chunks_fts) VALUES('rebuild')")
                    try:
                        conn.execute(
                            "INSERT INTO messages_fts(messages_fts) VALUES('rebuild')")
                    except sqlite3.OperationalError:
                        # No `messages_fts` on this index (a SQLite without
                        # trigram, or a schema before v8): nothing to rebuild,
                        # and the chunk index above is already repaired.
                        pass
                self._content_triggers_dropped = False
                self.set_state("fts_dirty", "")
                _log.info("word index rebuilt successfully")
            except Exception as exc:                  # noqa: BLE001
                _log.error(
                    "word index rebuild failed (searches will be incomplete): {}",
                    exc)

    def browse_messages(
        self,
        *,
        sender: Optional[str] = None,
        recipient: Optional[str] = None,
        subject: Optional[str] = None,
        has_attachment: Optional[bool] = None,
        after: Optional[int] = None,
        before: Optional[int] = None,
        words: str = "",
        file_where: str = "",
        file_params: Sequence[Any] = (),
        sort: str = "",
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        """Mail as a table: newest first, filtered by its own columns.

        **Not a search.** `search_files_by_name` and the hybrid engine both go
        through FTS and rank by relevance; this reads `messages` directly and
        orders by date, because a mail list is something you *browse* and the
        useful order is chronological. Ranking a mailbox by BM25 puts an
        eight-year-old thread above this morning's, which is never what somebody
        scanning a list wants.

        `sort` is `parsed.sort` - `""`, `"newest"` or `"oldest"`. Newest is what
        this already did, so only `/oldest` changes anything; it is accepted so
        that a switch the tab **offers** is a switch the tab **honours**, which
        is the whole contract behind the shared catalogue. A command that parses
        and then does nothing is worse than one that is not offered at all.

        Every filter is a plain LIKE or comparison against an indexed column, so
        this stays fast without touching a single chunk of text. Searching what
        the messages *say* is still the search tab's job.

        `sender` and `recipient` match as substrings deliberately: `/from dave`
        must find `dave.smith@acme.com`. Exact `IN` matching was a real bug here
        once, and it made the filter look broken to anybody who did not know the
        full address by heart.
        """
        where, params = self._message_where(
            sender=sender, recipient=recipient, subject=subject,
            has_attachment=has_attachment, after=after, before=before,
            words=words, walk_words=True)
        # **The file-level switches, built by the one shared definition.**
        # `browse_messages` has always joined `files`, so `/type`, `/path`,
        # `/name`, `/size` and `/repo` cost nothing to honour here - they were
        # simply never passed. The mail columns are deliberately *not* in this
        # fragment: the caller clears them, because `sender` and friends above
        # can use the trigram header index and `after`/`before` belong on
        # `m.sent_at` - the date the message was sent - rather than on the
        # file's mtime.
        # Not interpolated from anything a person typed: the parser only ever
        # produces `newest` or `oldest`, and anything else is newest.
        direction = "ASC" if str(sort or "").lower() == "oldest" else "DESC"
        sql = f"""
            SELECT m.file_id, m.subject, m.sender, m.recipients, m.sent_at,
                   m.has_attach, m.store_path, m.conversation, m.quoted_removed,
                   f.path, f.size_bytes, f.status, f.skip_code
            FROM messages m
            JOIN files f ON f.id = m.file_id
            {where} {file_where}
        """
        # `sent_at IS NULL` rather than `NULLS LAST`, which needs SQLite
        # 3.30. The bundled version is newer, but the version a user's
        # Python happens to ship is not something this should depend on,
        # and the two forms cost the same.
        #
        # *Note, 2026-10-04: the last clause above was not so.* Either form
        # sorts every message before `LIMIT`, because `idx_messages_sent`
        # cannot be read in `sent_at IS NULL, sent_at` order - 583 ms for the
        # newest 500 of 35,000 (`tools/fts_scale_bench.py`). So two queries,
        # the way `browse_files` lists by date: dated messages walk the index
        # (its entries are `(sent_at, rowid)` and `file_id` is the rowid, so
        # the tie-break is free) and stop at the limit; undated ones fill
        # whatever is left. The order is unchanged.
        params.extend(file_params or ())
        capped = max(1, int(limit))
        rows = self.conn.execute(
            sql + f" AND m.sent_at IS NOT NULL "
                  f"ORDER BY m.sent_at {direction}, m.file_id {direction} LIMIT ?",
            [*params, capped]).fetchall()
        if len(rows) < capped:
            rows += self.conn.execute(
                sql + f" AND m.sent_at IS NULL ORDER BY m.file_id {direction} LIMIT ?",
                [*params, capped - len(rows)]).fetchall()
        return [dict(row) for row in rows]

    def _message_where(
        self,
        *,
        sender: Optional[str] = None,
        recipient: Optional[str] = None,
        subject: Optional[str] = None,
        has_attachment: Optional[bool] = None,
        after: Optional[int] = None,
        before: Optional[int] = None,
        words: str = "",
        walk_words: bool = False,
        probe_words: bool = False,
    ) -> tuple[str, list[Any]]:
        """`(" WHERE ...", params)` over `messages m` for the Mail tab's filters.

        `walk_words` (the list, not the count): common words are matched per
        message as the date index is walked - see the words clause below.
        `probe_words` (the count): common words are matched by probing each
        message's chunk ids against the matching set - exact, as the count
        must be.

        Shared by `browse_messages` and `count_messages_matching`, so the
        count under the list is the count of exactly the rows it pages through.
        """
        clauses: list[str] = []
        params: list[Any] = []

        # LIKE with the value wrapped in wildcards, never interpolated. `%` and
        # `_` typed by a person are escaped, so searching for a literal
        # underscore in an address finds it instead of matching any character.
        def contains(column: str, value: str) -> None:
            # 2026-10-05: through the shared pattern, so a `*` or `?` typed
            # into `/from` or `/subject` is a wildcard here as everywhere.
            clauses.append(f"{column} LIKE ? ESCAPE '\\'")
            params.append(like_contains(value, fold=False))

        # **The index, when it can answer; the scan, when it cannot.**
        #
        # `LIKE '%dave%'` cannot use an index however many are declared - a
        # leading wildcard defeats every one - so this filtered by reading every
        # message, on every keystroke, on a tab that filters live.
        # `idx_messages_sender` has never once been used by this query.
        #
        # `messages_fts` is a **trigram** index, which answers `LIKE '%x%'`
        # exactly: mid-token, punctuation and all. So this is the same filter
        # with an index under it, not a narrower filter that is faster - which
        # matters, because substring matching here is deliberate and was a
        # reported bug once. See `migrations._v8_mail_header_index`.
        def narrow(column: str, value: str) -> None:
            match = self._header_match(column, value)
            if match is None:
                contains(f"m.{column}", value)
                return
            clauses.append(
                "m.file_id IN (SELECT rowid FROM messages_fts "
                "WHERE messages_fts MATCH ?)")
            params.append(match)

        if sender:
            narrow("sender", sender.strip())
        if recipient:
            narrow("recipients", recipient.strip())
        if subject:
            narrow("subject", subject.strip())
        if has_attachment is not None:
            clauses.append("m.has_attach = ?")
            params.append(1 if has_attachment else 0)
        if after is not None:
            clauses.append("m.sent_at >= ?")
            params.append(int(after))
        if before is not None:
            clauses.append("m.sent_at < ?")
            params.append(int(before))
        # **1 October 2026: words narrow the list by what the message says.**
        # "mail about holiday from maya" used to have "holiday" thrown away
        # here with a note saying so. A message's chunks open with its
        # subject, sender and recipients, so one FTS match over them covers
        # the headers and the body alike. Every word must appear.
        expression = _all_words_match(words)
        if expression and walk_words and self._match_is_broad(expression):
            # 2026-10-04: **common words, read newest first.** Collecting every
            # message the words match cost what they match - 538 ms for a word
            # in a quarter of a million chunks. Walking the date index and
            # asking FTS5 about each message's own chunks (`CROSS JOIN` keeps
            # that order; without it SQLite scanned the index once per message,
            # 112 s) stops at the page: 17.6 ms, the same rows. For a rare word
            # the walk is the slow way (1.5 s), so it is used only past
            # `MATCH_WALK_MIN` matches. The count keeps the other form.
            clauses.append(
                "EXISTS (SELECT 1 FROM chunks c CROSS JOIN chunks_fts "
                "ON chunks_fts.rowid = c.id "
                "WHERE c.file_id = m.file_id AND chunks_fts MATCH ?)")
            params.append(expression)
        elif expression and probe_words and self._match_count(expression) > MATCH_PROBE_MIN:
            # 2026-10-04: **the count, for common words.** Collecting each
            # matching chunk's message read every one of those chunk rows,
            # text and all - 459 ms for `pump`. The set of matching chunk ids
            # comes from FTS5's own index; each message's ids come from the
            # narrow `idx_chunks_file_ord`. Same number, 138 ms; with a sender
            # filter 582 -> 50. See `MATCH_PROBE_MIN` for where it stops paying.
            clauses.append(
                "EXISTS (SELECT 1 FROM chunks c WHERE c.file_id = m.file_id "
                "AND c.id IN (SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH ?))")
            params.append(expression)
        elif expression:
            clauses.append(
                "m.file_id IN (SELECT c.file_id FROM chunks_fts "
                "JOIN chunks c ON c.id = chunks_fts.rowid WHERE chunks_fts MATCH ?)")
            params.append(expression)

        where = (" WHERE " + " AND ".join(clauses)) if clauses else " WHERE 1=1"
        return where, params

    def count_messages_matching(
        self,
        *,
        sender: Optional[str] = None,
        recipient: Optional[str] = None,
        subject: Optional[str] = None,
        has_attachment: Optional[bool] = None,
        after: Optional[int] = None,
        before: Optional[int] = None,
        words: str = "",
        file_where: str = "",
        file_params: Sequence[Any] = (),
        cap: int = 100_000,
        **_ignored: Any,
    ) -> int:
        """How many messages `browse_messages` would return with no limit, up to
        `cap + 1`.

        Asked for directly: *"when searching for mails the search displays
        maximum 500 but does not tell how much total"*. **Bounded**, because a
        count over two hundred thousand messages behind a substring filter is
        a scan, and the Mail tab filters on every keystroke. A result above
        `cap` means "more than `cap`", and the summary says exactly that.

        `**_ignored` takes `sort` and `limit`, so the caller can pass the same
        keyword arguments it passes to `browse_messages`.
        """
        where, params = self._message_where(
            sender=sender, recipient=recipient, subject=subject,
            has_attachment=has_attachment, after=after, before=before,
            words=words, probe_words=True)
        sql = f"""
            SELECT COUNT(*) AS n FROM (
                SELECT 1 FROM messages m
                JOIN files f ON f.id = m.file_id
                {where} {file_where}
                LIMIT ?
            )
        """
        row = self.conn.execute(
            sql, [*params, *(file_params or ()), max(1, int(cap)) + 1]).fetchone()
        return int(row["n"]) if row else 0

    def messages_for(self, file_ids: Sequence[int]) -> dict[int, dict[str, Any]]:
        """Mail metadata for a page of results, keyed by `file_id`.

        **One query for the whole page, never one per row.** The search box runs
        on a debounce, so a per-row lookup over ten results is ten queries per
        keystroke - fifty at the fetch depth grouping needs. That is the shape
        of slowness that gets blamed on the search itself.

        Files that are not messages are simply absent from the result, which is
        the common case and must not be an error: most results are documents.
        """
        wanted = [int(file_id) for file_id in file_ids or ()]
        if not wanted:
            return {}
        placeholders = ",".join("?" * len(wanted))
        rows = self.conn.execute(
            # `conversation` since order 0z F2: what "One row per conversation"
            # folds a page of results by. Same query, one more column.
            f"""SELECT file_id, subject, sender, recipients, sent_at, has_attach,
                       conversation
                FROM messages WHERE file_id IN ({placeholders})""",
            wanted,
        )
        return {int(row["file_id"]): dict(row) for row in rows}

    def messages_by_path(self, paths: Sequence[str]) -> dict[str, dict[str, Any]]:
        """`messages_for`'s columns for the messages at these paths, keyed by path.

        2026-10-04: what an attachment's row is shown with - its message's
        sender, subject and sent date - on the Files list and in the Search
        list, **one statement for a page**, on the unique index over
        `files.path`. It replaced one `get_file` per message. Paths that are
        not messages are absent.
        """
        wanted = list(dict.fromkeys(str(path) for path in paths or () if path))
        found: dict[str, dict[str, Any]] = {}
        # SQLite's bound-variable limit is 32,766 on the bundled build; a page
        # is 500 rows, so one chunk in practice.
        for start in range(0, len(wanted), 900):
            chunk = wanted[start:start + 900]
            rows = self.conn.execute(
                f"""SELECT f.path, m.file_id, m.subject, m.sender, m.recipients,
                           m.sent_at, m.has_attach, m.conversation
                    FROM files f JOIN messages m ON m.file_id = f.id
                    WHERE f.path IN ({','.join('?' * len(chunk))})""",
                chunk,
            )
            for row in rows:
                record = dict(row)
                found[str(record.pop("path"))] = record
        return found

    def conversation_messages(
        self, conversation: Optional[str], *, limit: int = 26
    ) -> list[dict[str, Any]]:
        """The messages of one conversation, oldest first. Order 0y section 4c.

        **One statement, on `idx_messages_conv`.** The rows carry what
        `browse_messages` returns for a message - so the Mail list's own row
        shape can be built from them - plus `opening`, the start of each
        message's first passage, for the line that stands for it in the list.
        That is a lookup on `idx_chunks_file_ord` per message listed, inside
        the same statement, never a second round trip.

        **Bounded, newest kept.** A conversation key is whatever the mail said
        it was, and an archive with no threading headers falls back to the
        subject line - so "Hello" can be ten thousand unrelated messages. The
        newest `limit` are returned (oldest of them first); a caller that wants
        to know whether there were more asks for one more than it shows.

        `[]` for a message with no conversation: `NULL` and `''` are "not
        known", not a conversation every such message shares.
        """
        key = str(conversation or "").strip()
        if not key:
            return []
        rows = self.conn.execute(
            """
            SELECT m.file_id, m.subject, m.sender, m.recipients, m.sent_at,
                   m.has_attach, m.store_path, m.entry_id, m.conversation,
                   m.quoted_removed,
                   f.path, f.size_bytes, f.status, f.skip_code,
                   (SELECT substr(c.text, 1, 2000) FROM chunks c
                     WHERE c.file_id = m.file_id AND c.ordinal = 0) AS opening
            FROM messages m
            JOIN files f ON f.id = m.file_id
            WHERE m.conversation = ?
            ORDER BY m.sent_at IS NULL, m.sent_at DESC, m.file_id DESC
            LIMIT ?
            """,
            (key, max(1, int(limit))),
        ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def count_messages(self) -> int:
        """Every message row. A full count, so for a panel, not a keystroke."""
        row = self.conn.execute("SELECT COUNT(*) AS n FROM messages").fetchone()
        return int(row["n"]) if row else 0

    def _unchanged_since(self, key: str, compute: Any) -> Any:
        r"""`compute()`, or its last answer when no row has been written since.

        2026-10-04, code review: `count_listed_files` (246 ms at 1.36M rows,
        asked by every Files query) and `status_counts` (112 ms, every 5 s)
        read the whole of `files` to give the same number again. **Not keyed
        on `generation`**: `mark_indexed_many` and `mark_skipped` change
        statuses without moving it. `PRAGMA data_version` changes whenever
        *another* connection - in this process or the index run's - commits,
        and `total_changes` counts this connection's own writes; together they
        say "nothing written since". The answer is kept on this thread's
        connection, because a `data_version` means nothing on another one.
        """
        conn = self.conn
        try:
            stamp = (int(conn.execute("PRAGMA data_version").fetchone()[0]),
                     int(conn.total_changes))
            answers = conn.__dict__.setdefault("_unchanged_answers", {})
        except (AttributeError, TypeError, sqlite3.Error):
            return compute()
        held = answers.get(key)
        if held is not None and held[0] == stamp:
            return held[1]
        value = compute()
        answers[key] = (stamp, value)
        return value

    def count_listed_files(self) -> int:
        """How many rows the Files tab lists with an empty box - its "in the
        index" figure, so the summary can say how far a filter has narrowed.
        Counted again only after a write (`_unchanged_since`)."""
        def count() -> int:
            row = self.conn.execute(
                f"SELECT COUNT(*) AS n FROM files f WHERE {LISTED_FILES}").fetchone()
            return int(row["n"]) if row else 0

        return int(self._unchanged_since("count_listed_files", count))

    def search_files_by_name(
        self, text: str, *, limit: int = 100, ext: Optional[Sequence[str]] = None
    ) -> list[dict[str, Any]]:
        """Files whose NAME or FOLDER matches, substring-wise, ranked by name.

        This is not the document search. It never touches chunk text, never
        embeds anything and never reranks - it is one FTS5 lookup over a table
        of filenames, so it can run on every keystroke.

        The name column is weighted far above the folder: typing "invoice"
        should surface `Invoice 2024.pdf` before the forty files that merely
        live in an `Invoices` directory.

        Returns `[]` for a query too short to be meaningful rather than the
        whole index - one or two characters match nearly everything, and a
        hundred arbitrary rows is worse than an empty list.
        """
        cleaned = (text or "").strip()
        if len(cleaned) < 2:
            # **A filter on its own is a complete request.** `/type pdf` means
            # "every PDF", and refusing it for want of two characters of name
            # makes the dropdown look broken on the simplest thing it offers.
            #
            # **And so is an empty box: it means "everything".** Asked for -
            # *"initially should display everything and it filters as you
            # type"*. A list that is blank until you type cannot be browsed,
            # cannot show you what is in the index, and looks identical to an
            # index that is empty. Newest first, which is the same order the
            # mail list uses and for the same reason: it is what somebody
            # scanning a list wants at the top.
            #
            # **One or two characters is still nothing**, and that is not the
            # same case. Those match nearly every file, so answering them with
            # a hundred arbitrary rows would show results that have no relation
            # to what was typed - worse than showing none, because it looks
            # like a search that worked.
            if cleaned and not ext:
                return []
            wanted = [e.lower().lstrip(".") for e in (ext or ())]
            params: list[Any] = list(wanted)
            clause = (f"AND ext IN ({','.join('?' * len(wanted))})"
                      if wanted else "")
            if cleaned:
                # **The two characters still count.** `/type pdf` plus "q" used
                # to answer with every PDF newest-first, the typed letter
                # discarded - so the list did not change as the person typed
                # and looked stuck. The trigram index cannot serve fewer than
                # three characters, so this one is a substring scan; it is only
                # reachable from a query this short, and it is bounded by the
                # extension filter that must accompany it.
                #
                # `LIKE` folds ASCII only (see M20), so `A` will not find `á`.
                # At one character that is a limit worth having over silence.
                clause += " AND path LIKE ? ESCAPE '\\'"
                params.append(f"%{_like_escape(cleaned)}%")
            params.append(max(1, int(limit)))
            # **`taken_at_ns` projected but the `ORDER BY` left on `mtime_ns`,
            # deliberately.** Work order 0f §3a's third clause targets
            # `browse_files`'s `/newest`/`/oldest` handling below, which
            # falls back to this function on a `files_fts`/`chunks_fts`
            # `OperationalError` and needs the column to sort by there; this
            # branch's own default "empty box means everything, newest
            # first" ordering is a separate, narrower browse (no filters, no
            # sort switch) that this item does not name, so it is left as it
            # was rather than guessed at.
            return [dict(row) for row in self.conn.execute(
                f"""SELECT id, path, ext, size_bytes, mtime_ns, taken_at_ns,
                           status, skip_code, source_kind, 0.0 AS score
                    FROM files
                    WHERE source_kind = 'file' {clause}
                    ORDER BY mtime_ns DESC
                    LIMIT ?""",
                params,
            )]

        # A trigram index takes the query as a literal string, so the whole
        # thing is quoted and internal quotes are doubled. No user input reaches
        # the FTS expression parser, which is what makes this crash-proof
        # against `AND`, `*`, `"` and every other operator someone types by
        # accident while looking for a file.
        expression = '"' + cleaned.replace('"', '""') + '"'

        sql = """
            SELECT f.id, f.path, f.ext, f.size_bytes, f.mtime_ns, f.taken_at_ns,
                   f.status, f.skip_code, f.source_kind,
                   bm25(files_fts, 10.0, 1.0) AS score
            FROM files_fts
            JOIN files f ON f.id = files_fts.rowid
            WHERE files_fts MATCH ?
        """
        params: list[Any] = [expression]
        if ext:
            wanted = [e.lower().lstrip(".") for e in ext]
            sql += f" AND f.ext IN ({','.join('?' * len(wanted))})"
            params.extend(wanted)
        sql += " ORDER BY score LIMIT ?"
        # Clamped, as the short-query branch above already is. A caller that
        # computes a page size can arrive at 0 or a negative; SQLite reads a
        # negative LIMIT as "no limit", so an arithmetic slip that should have
        # shown nothing would instead hand back the whole index.
        params.append(max(1, int(limit)))

        try:
            rows = self.conn.execute(sql, params).fetchall()
        except sqlite3.OperationalError:
            # An index built before v4, or a corrupt FTS table. An empty result
            # is the right answer; taking the window down with it is not.
            return []
        return [dict(row) for row in rows]

    #: How often the vocabulary scan looks at the clock, in VM instructions.
    #:
    #: Small enough that a budget of a second or two is honoured to within a
    #: few milliseconds, large enough that the check itself is not the cost.
    PROGRESS_STEP = 1_000

    def vocabulary_terms(
        self, pattern: str, *, limit: int = 200,
        budget_s: Optional[float] = None,
        problems: Optional[list[str]] = None,
    ) -> list[str]:
        r"""Indexed terms matching a `LIKE` pattern, commonest first.

        Reads `chunks_vocab` - an `fts5vocab` view over the term dictionary
        `chunks_fts` already keeps, so this holds no rows of its own and cost no
        disk to create. See migration 11.

        **Ordered by document frequency**, which is what makes the caller's cap
        defensible: when more terms match than may be used, the ones kept are
        the words the corpus actually contains rather than an alphabetical
        slice ending at `ab`.

        `ESCAPE '\'` is not optional - `like_pattern` escapes a literal `%` or
        `_` the person typed, and without the clause SQLite would read the
        backslash as an ordinary character and the escape as part of the search.

        Returns `[]` when the vocabulary is missing, which is an index built
        before migration 11 on a database that could not create it. That costs
        wildcards and nothing else.

        **`budget_s` is enforced, not observed.** The caller used to time this
        call and log afterwards if it had taken too long, which is not a budget
        - the scan had already finished by then, and on a corpus with millions
        of distinct terms `*a*` could sit there for as long as it liked. A
        progress handler checks the clock every `PROGRESS_STEP` instructions
        and aborts the statement, so the ceiling is real.

        Being cut short is **reported through `problems`**, never swallowed: a
        wildcard that silently matched a fraction of the vocabulary would look
        exactly like one that matched a fraction of the corpus.
        """
        deadline = (time.perf_counter() + float(budget_s)
                    if budget_s and budget_s > 0 else None)
        interrupted = False

        def past_deadline() -> int:
            nonlocal interrupted
            if deadline is not None and time.perf_counter() > deadline:
                interrupted = True
                return 1                     # non-zero aborts the statement
            return 0

        if deadline is not None:
            self.conn.set_progress_handler(past_deadline, self.PROGRESS_STEP)
        try:
            rows = self.conn.execute(
                r"""SELECT term FROM chunks_vocab
                    WHERE term LIKE ? ESCAPE '\'
                    ORDER BY doc DESC, term
                    LIMIT ?""",
                (str(pattern), max(1, int(limit))),
            ).fetchall()
        except sqlite3.OperationalError as exc:
            if interrupted:
                if problems is not None:
                    problems.append(f"gave up after {_seconds(budget_s)}")
                _log.info("vocabulary lookup for {} hit its {} budget",
                          pattern, _seconds(budget_s))
            else:
                _log.debug("vocabulary lookup unavailable: {}", exc)
            return []
        finally:
            if deadline is not None:
                self.conn.set_progress_handler(None, 0)
        return [str(row["term"]) for row in rows]

    def fts_stem(self, word: str) -> str:
        r"""What FTS5 stores for one word. `""` if it stores nothing.

        **Asked of SQLite rather than reimplemented**, and that is the whole
        point of this method existing at all. `chunks_fts` uses
        `porter unicode61`, so the stored form of *voice* is `voic` and a
        wildcard matched against the vocabulary has to be stemmed to meet it. A
        second Porter implementation in Python would agree with this one until
        the day it did not, and that day would present as a wildcard quietly
        matching nothing.

        A scratch FTS5 table with the same tokenizer, one insert, one read.
        `TEMP` so it never touches the index file, and reused across calls
        because creating it per query would cost more than the lookup it serves.
        """
        text = str(word or "").strip()
        if not text:
            return ""
        try:
            # 2026-10-04: ready per *connection*. A `TEMP` table exists only on
            # the connection that made it, and each thread has its own - a flag
            # on the store made the second thread to stem get "no such table"
            # and an empty stem, so its wildcards matched nothing.
            if getattr(self._local, "stem_conn", None) is not self.conn:
                self.conn.execute(
                    "CREATE VIRTUAL TABLE IF NOT EXISTS temp.stem_probe "
                    "USING fts5(text, tokenize='porter unicode61')")
                self.conn.execute(
                    "CREATE VIRTUAL TABLE IF NOT EXISTS temp.stem_probe_v "
                    "USING fts5vocab('stem_probe', 'row')")
                self._local.stem_conn = self.conn
            self.conn.execute("DELETE FROM temp.stem_probe")
            self.conn.execute(
                "INSERT INTO temp.stem_probe(text) VALUES (?)", (text,))
            row = self.conn.execute(
                "SELECT term FROM temp.stem_probe_v LIMIT 1").fetchone()
        except sqlite3.Error as exc:
            _log.debug("could not stem {}: {}", text, exc)
            return ""
        return str(row["term"]) if row else ""

    def chunk_match_share(self, expression: str) -> tuple[float, int]:
        """`(share, top)`: the share of the newest `MATCH_SAMPLE` chunks that
        match `expression`, and the highest chunk id. Never raises.

        **Constant cost** - under 1.5 ms for any word at a million chunks,
        because FTS5 seeks a rowid range inside its own index. The vocabulary
        table answers exactly but costs what the word matches (50 ms for
        `the`), which is the cost this exists to avoid.
        """
        try:
            top = int(self.conn.execute("SELECT max(id) FROM chunks").fetchone()[0] or 0)
            sample = min(MATCH_SAMPLE, top)
            if sample <= 0 or not expression:
                return 0.0, top
            found = self.conn.execute(
                "SELECT count(*) FROM chunks_fts WHERE chunks_fts MATCH ? AND rowid > ?",
                (expression, top - sample)).fetchone()[0]
            return int(found) / sample, top
        except (sqlite3.Error, AppErrorException):
            return 0.0, 0

    def _match_count(self, expression: str) -> float:
        """About how many chunks match `expression`, from the newest sample."""
        share, top = self.chunk_match_share(expression)
        return share * top

    def _match_is_broad(self, expression: str) -> bool:
        """Whether walking and asking per row beats collecting every match."""
        share, top = self.chunk_match_share(expression)
        return share * top > MATCH_WALK_MIN

    def _widening(self, floor: int) -> list[int]:
        """`floor`, then floors each holding four times as many chunks, then 0."""
        if not floor:
            return [0]
        try:
            top = int(self.conn.execute("SELECT max(id) FROM chunks").fetchone()[0] or 0)
        except sqlite3.Error:
            return [floor, 0]
        floors, lower = [], floor
        while lower > 0:
            floors.append(lower)
            lower = max(0, top - 4 * (top - lower))
        return floors + [0]

    def _match_floor(self, expression: str) -> int:
        """The chunk id a ranked list scores above so it scores about
        `MATCH_SCORED_MAX` matches - the newest; 0 to score them all."""
        share, top = self.chunk_match_share(expression)
        if share * top <= MATCH_SCORED_MAX:
            return 0
        return max(0, top - int(MATCH_SCORED_MAX / share))

    def browse_files(
        self,
        parsed: Any,
        *,
        limit: int = 200,
        extra_ext: Optional[Sequence[str]] = None,
    ) -> list[dict[str, Any]]:
        r"""Files matching a whole parsed query - **every switch, one meaning.**

        `search_files_by_name` answers "what is this file called". This answers
        the question the Files and Code tabs are actually asked, which is the
        same one the search box is asked, narrowed to file rows:

          * free text matches the **name, the folder, or the contents** - asked
            for directly, so that typing the same words in Files and in Search
            does not quietly produce two different sets;
          * every `/switch` is applied through `filters.file_filter_sql`, the
            one definition, so `path:`, `after:`, `size:`, `repo:` and the mail
            fields work here exactly as they do in the generic search.

        **`/name` needs no special case, and that is the nice part.** It is an
        ordinary conjunctive filter on the basename, so a row that matched only
        on its *contents* cannot satisfy it. Typing `/name invoice` therefore
        narrows to filenames without any mode flag, second code path, or switch
        that means something different here than it does anywhere else.

        `extra_ext` is the Code tab's configured "what counts as code" set. It
        is deliberately separate from `parsed.ext`: a typed `/type cs` must beat
        the configuration rather than intersect with it, so the caller passes
        one or the other and never both.

        Ordering is deliberate and mixed. A row that matched by name is ranked
        by the name index; one that matched only by content carries its BM25
        score; a query with no text at all is newest-first, because that is a
        list somebody is browsing rather than searching.

        **`parsed.sort` overrides all of that**, which is the point of it. The
        catalogue offers `/newest` and `/oldest` on every tab, so every tab has
        to honour them - a switch that parses cleanly and then changes nothing
        is the kind of quiet lie that makes somebody stop trusting the rest of
        the switches too.
        """
        from app.storage.filters import file_filter_sql, merge_by_date

        where, params = file_filter_sql(parsed)
        # Only ever `newest` or `oldest` out of the parser, so this is a choice
        # between two constants rather than anything interpolated.
        wants_sort = str(getattr(parsed, "sort", "") or "").lower()
        newest_first = wants_sort != "oldest"
        # Two spellings of one order: the listing below reads `files` directly,
        # while the scored query orders the *outer* select, where the column has
        # already been projected and `f.` no longer resolves.
        _dir = "ASC" if wants_sort == "oldest" else "DESC"
        # **Not sargable, and left that way here on purpose - see the
        # no-search-text branch below for the case where that matters.** This
        # `by_date_outer` orders the scored, FTS-matched UNION a few lines
        # down, whose rows are already bounded by `files_fts`/`chunks_fts
        # MATCH` before either `ORDER BY` runs - the same rows `ORDER BY
        # score` already sorts with a temp B-tree, because `bm25()` is a
        # computed value no index can order by either. Wrapping the date in
        # `COALESCE` here adds no new class of cost to a query that already
        # pays for a full sort of its (FTS-bounded, not table-sized) match
        # set - confirmed with `EXPLAIN QUERY PLAN` against the real
        # `browse_files` statement in `test_query_plans.py`, not assumed
        # from this reasoning alone.
        by_date_outer = f"COALESCE(taken_at_ns, mtime_ns) {_dir}"
        wanted = [e.lower().lstrip(".") for e in (extra_ext or ())]
        if wanted and not getattr(parsed, "ext", ()):
            where += f" AND f.ext IN ({','.join('?' * len(wanted))})"
            params.extend(wanted)

        cleaned = " ".join(
            str(part) for part in (
                *(getattr(parsed, "terms", ()) or ()),
            )
        ).strip() or str(getattr(parsed, "text", "") or "").strip()
        capped = max(1, int(limit))

        if len(cleaned) < NAME_MIN_CHARS:
            # **An empty box means everything, and a filter alone is a complete
            # request.** Both were settled when the Files tab was built; this
            # keeps them, and adds that one or two characters still mean
            # nothing rather than a hundred arbitrary rows.
            if cleaned:
                return []
            # **Two queries and a merge, not `ORDER BY COALESCE(f.taken_at_ns,
            # f.mtime_ns) {_dir}`.** Unlike the scored branch below, this one
            # can be the *whole* `files` table (no filter, no search text is
            # exactly "browse everything") - the case `app.storage.filters.
            # merge_by_date` measured directly: COALESCE here is not
            # sargable and turns an indexed walk that stops at `LIMIT` into a
            # full sort of every row, 0.011ms vs 39.5ms at 200,000 files.
            columns = """f.id, f.path, f.ext, f.size_bytes, f.mtime_ns,
                           f.taken_at_ns, f.status, f.skip_code,
                           f.source_kind, f.volume_id, f.relative_path,
                           0.0 AS score"""
            no_shot_date = self.conn.execute(
                f"""SELECT {columns}
                    FROM files f
                    WHERE {LISTED_FILES} AND f.taken_at_ns IS NULL{where}
                    ORDER BY f.mtime_ns {_dir}
                    LIMIT ?""",
                [*params, capped],
            ).fetchall()
            shot_date = self.conn.execute(
                f"""SELECT {columns}
                    FROM files f
                    WHERE {LISTED_FILES} AND f.taken_at_ns IS NOT NULL{where}
                    ORDER BY f.taken_at_ns {_dir}
                    LIMIT ?""",
                [*params, capped],
            ).fetchall()
            return merge_by_date(
                [dict(row) for row in no_shot_date],
                [dict(row) for row in shot_date],
                limit=capped, newest_first=newest_first,
            )

        # Quoted whole, exactly as `search_files_by_name` does: a trigram index
        # takes its query as a literal, so nothing a person types ever reaches
        # the FTS expression parser.
        literal = '"' + cleaned.replace('"', '""') + '"'
        # **`bm25()` cannot be wrapped in an aggregate.** SQLite refuses it with
        # "unable to use function bm25 in the requested context", because an
        # auxiliary function is only defined on a row of the FTS query itself.
        # So each half is scored in its own subquery, where `bm25` is legal, and
        # the outer statement only ever sees a plain number.
        #
        # This mattered more than a syntax error usually does: the refusal was
        # an `OperationalError`, the fallback below caught it, and the tab went
        # on returning name-only matches while looking entirely healthy. The
        # fallback is now narrow enough that it cannot hide this again.
        sql = f"""
            SELECT id, path, ext, size_bytes, mtime_ns, taken_at_ns, status,
                   skip_code, source_kind, volume_id, relative_path,
                   MIN(score) AS score
            FROM (
                SELECT f.id AS id, f.path AS path, f.ext AS ext,
                       f.size_bytes AS size_bytes, f.mtime_ns AS mtime_ns,
                       f.taken_at_ns AS taken_at_ns,
                       f.status AS status, f.skip_code AS skip_code,
                       f.source_kind AS source_kind,
                       f.volume_id AS volume_id,
                       f.relative_path AS relative_path,
                       bm25(files_fts, 10.0, 1.0) AS score
                FROM files_fts
                JOIN files f ON f.id = files_fts.rowid
                -- 2026-10-04, code review (the owner: "do the recommended"):
                -- the name half lists what the rest of the tab lists. A zip's
                -- members are in `files_fts` and were found by name here while
                -- the contents half, the count and the empty box left them out.
                WHERE files_fts MATCH ? AND {LISTED_FILES} {where}

                UNION ALL

                SELECT f.id, f.path, f.ext, f.size_bytes, f.mtime_ns,
                       f.taken_at_ns,
                       f.status, f.skip_code, f.source_kind,
                       f.volume_id, f.relative_path,
                       bm25(chunks_fts) AS score
                FROM chunks_fts
                JOIN chunks c ON c.id = chunks_fts.rowid
                JOIN files  f ON f.id = c.file_id
                WHERE chunks_fts MATCH ? AND chunks_fts.rowid > ?
                      AND {LISTED_FILES} {where}
            )
            GROUP BY id
            ORDER BY {by_date_outer if wants_sort else 'score'}
            LIMIT ?
        """
        # 2026-10-04: the contents half scores the newest matches only once a
        # word matches more than `MATCH_SCORED_MAX` chunks - ranked, as the
        # search box is (`keyword._bounded`). 1,103 ms for `pump` on the bench.
        # The list is filtered (`LISTED_FILES` leaves mail out, and every switch
        # applies), so the newest slice can hold too few listed files: it widens
        # four-fold at a time, down to every match, until the page is full.
        floor = self._match_floor(literal)
        try:
            for lower in self._widening(floor):
                rows = self.conn.execute(
                    sql, [literal, *params, literal, lower, *params, capped]
                ).fetchall()
                if len(rows) >= capped:
                    break
        except sqlite3.OperationalError as exc:
            # **Only a missing table.** An index built before `chunks_fts` or
            # `files_fts` existed is a real case and the name half alone is a
            # far better answer than a broken window. Anything else is a fault
            # in the statement above, and swallowing it is how a tab quietly
            # stops searching contents - so it is raised.
            if "no such table" not in str(exc).lower():
                raise
            _log.warning("browse_files fell back to names only: {}", exc)
            named = self.search_files_by_name(
                cleaned, limit=capped,
                ext=list(getattr(parsed, "ext", ()) or wanted) or None,
            )
            if wants_sort:
                # A small, already-fetched Python list (`capped` rows at
                # most) - the same "no index to lose" reasoning `engine.py`'s
                # own post-fusion `/newest`/`/oldest` sort documents, so the
                # plain fallback is fine here and the two-query split above
                # would be pointless ceremony.
                named.sort(
                    key=lambda row: int(row.get("taken_at_ns") or row.get("mtime_ns") or 0),
                    reverse=wants_sort != "oldest")
            return named
        # `UNION` can return one row per branch for a file that matched both.
        # The better score wins, and the first is the better one after ORDER BY.
        # Under `/newest` both rows carry the same mtime, so the first is still
        # the right one to keep - the file is one file however it matched.
        best: dict[int, dict[str, Any]] = {}
        for row in rows:
            record = dict(row)
            best.setdefault(int(record["id"]), record)
        return list(best.values())

    def count_browse_files(
        self,
        parsed: Any,
        *,
        extra_ext: Optional[Sequence[str]] = None,
        cap: int = 10_000,
    ) -> Optional[int]:
        """How many files `browse_files` would return with no limit, up to
        `cap + 1`. `None` when it cannot say (an index with no FTS tables).

        The Files list shows its first page and used to stop there silently -
        the same fault the owner reported on Mail. **The same filter and the
        same two halves as `browse_files`** (a name match or a contents match),
        counted as distinct files, so the number under the list is the number
        of rows it would page through.

        **Bounded**: a common word matches most of a corpus, and the list
        filters as somebody types. Above `cap` the summary says "more than".
        """
        from app.storage.filters import file_filter_sql

        where, params = file_filter_sql(parsed)
        wanted = [e.lower().lstrip(".") for e in (extra_ext or ())]
        if wanted and not getattr(parsed, "ext", ()):
            where += f" AND f.ext IN ({','.join('?' * len(wanted))})"
            params.extend(wanted)
        cleaned = " ".join(
            str(part) for part in (getattr(parsed, "terms", ()) or ())
        ).strip() or str(getattr(parsed, "text", "") or "").strip()
        bound = max(1, int(cap)) + 1
        if len(cleaned) < NAME_MIN_CHARS:
            if cleaned:
                return 0
            row = self.conn.execute(
                f"""SELECT COUNT(*) AS n FROM (
                        SELECT 1 FROM files f
                        WHERE {LISTED_FILES}{where}
                        LIMIT ?)""", [*params, bound]).fetchone()
            return int(row["n"]) if row else 0
        literal = '"' + cleaned.replace('"', '""') + '"'
        sql = f"""
            SELECT COUNT(*) AS n FROM (
                -- 2026-10-04: `DISTINCT` over `UNION ALL` streams, so `LIMIT`
                -- stops it once it has seen `cap + 1` files; `UNION` built the
                -- whole set first (434 ms for a common word on the bench). The
                -- same number.
                SELECT DISTINCT id FROM (
                    SELECT f.id AS id
                    FROM files_fts
                    JOIN files f ON f.id = files_fts.rowid
                    WHERE files_fts MATCH ? AND {LISTED_FILES} {where}
                    UNION ALL
                    SELECT f.id
                    FROM chunks_fts
                    JOIN chunks c ON c.id = chunks_fts.rowid
                    JOIN files  f ON f.id = c.file_id
                    WHERE chunks_fts MATCH ? AND {LISTED_FILES} {where}
                )
                LIMIT ?
            )
        """
        try:
            row = self.conn.execute(
                sql, [literal, *params, literal, *params, bound]).fetchone()
        except sqlite3.OperationalError as exc:
            # The one fallback `browse_files` allows itself, for the same
            # reason: an index older than the FTS tables. Anything else raises.
            if "no such table" not in str(exc).lower():
                raise
            return None
        return int(row["n"]) if row else 0

    def repos_with_matches(
        self,
        parsed: Any,
        *,
        extra_ext: Optional[Sequence[str]] = None,
    ) -> set[int]:
        r"""Which repositories hold at least one file matching this query.

        **The same filter as `browse_files`, a different projection.** That is
        the whole design: the git tree used to list every repository whatever
        was typed, so a term matching files in one checkout left the other three
        sitting there as though they matched too - a tree reads as *"these are
        the repositories that have what you asked for"*, which made it the most
        misleading pane in the application.

        A second, differently-worded filter would let the tree and the list
        disagree, which is the fault `WORKORDER-202626081149-code-tab.md` §5 is
        about. So this composes `file_filter_sql` exactly as `browse_files`
        does, and if that definition changes both move together.

        **Deliberately uncapped**, unlike the list. "Does this repository hold a
        match" is not a question about the first 500 rows, and answering it from
        a capped list would hide a repository whose matches fell past the cap.
        One aggregate over an indexed column costs nothing worth saving here.

        No subprocess, ever - this runs on a keystroke. A branch scope is
        answered from the listing the tree already fetched, never from a fresh
        `git ls-tree`; see `presenter.code_rows_for`.
        """
        from app.storage.filters import file_filter_sql

        where, params = file_filter_sql(parsed)
        wanted = [e.lower().lstrip(".") for e in (extra_ext or ())]
        if wanted and not getattr(parsed, "ext", ()):
            where += f" AND f.ext IN ({','.join('?' * len(wanted))})"
            params.extend(wanted)

        cleaned = " ".join(
            str(part) for part in (getattr(parsed, "terms", ()) or ())
        ).strip() or str(getattr(parsed, "text", "") or "").strip()

        base = ("SELECT DISTINCT f.repo_id AS repo_id FROM files f "
                f"WHERE f.repo_id IS NOT NULL {where}")

        if len(cleaned) < NAME_MIN_CHARS:
            # No text, or too little to mean anything: the switches alone decide,
            # which is the same reading `browse_files` gives an empty box.
            if cleaned:
                return set()
            rows = self.conn.execute(base, params)
            return {int(row["repo_id"]) for row in rows}

        # With text, a repository matches if any of its files matches by name or
        # by contents - the same two halves `browse_files` unions, so a file
        # that would appear in the list cannot fail to light up its repository.
        literal = '"' + cleaned.replace('"', '""') + '"'
        contents = f"""
                SELECT f.repo_id
                FROM chunks_fts
                JOIN chunks c ON c.id = chunks_fts.rowid
                JOIN files  f ON f.id = c.file_id
                WHERE chunks_fts MATCH ? AND f.repo_id IS NOT NULL {where}"""
        if self._match_is_broad(literal):
            # 2026-10-04: a common word asks each repository whether any of its
            # chunks match, stopping at the first - not every match for the
            # few repositories there are (378 ms for `pump` on the bench).
            # `CROSS JOIN` keeps the order: files by repository, their chunks,
            # then FTS5 asked about one chunk at a time.
            contents = f"""
                SELECT r.id FROM repos r
                WHERE EXISTS (
                    SELECT 1 FROM files f
                    CROSS JOIN chunks c ON c.file_id = f.id
                    CROSS JOIN chunks_fts ON chunks_fts.rowid = c.id
                    WHERE f.repo_id = r.id AND chunks_fts MATCH ? {where})"""
        sql = f"""
            SELECT DISTINCT repo_id FROM (
                SELECT f.repo_id AS repo_id
                FROM files_fts JOIN files f ON f.id = files_fts.rowid
                WHERE files_fts MATCH ? AND f.repo_id IS NOT NULL {where}

                UNION
                {contents}
            )
        """
        try:
            rows = self.conn.execute(sql, [literal, *params, literal, *params])
            return {int(row["repo_id"]) for row in rows if row["repo_id"] is not None}
        except sqlite3.OperationalError as exc:
            # An index built before one of the FTS tables existed. The tree
            # showing everything is the old behaviour and a safe answer; a tree
            # showing nothing would read as "no repositories match", which is a
            # different and wrong claim.
            if "no such table" not in str(exc).lower():
                raise
            _log.warning("repo matching fell back to all repositories: {}", exc)
            rows = self.conn.execute(base, params)
            return {int(row["repo_id"]) for row in rows}

    def count_named_files(self) -> int:
        """How many files the Files tab can find by name.

        **Joined to `files`, not a bare count of the FTS table.** `files_fts` is
        standalone, so a row in it outlives its file unless something deletes it
        by hand - and the list this labels joins `files` (see
        `search_files_by_name`), so a bare count could report more than the list
        could ever show. It did exactly that after a reset: an emptied index
        with the old number under it.

        The join is what makes the label and the list answer the same question,
        which is the only reason to show a count at all.
        """
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM files_fts "
            "JOIN files f ON f.id = files_fts.rowid"
        ).fetchone()
        return int(row["n"]) if row else 0

    def delete_file(self, file_id: int) -> None:
        """Delete a file and everything derived from it.

        ON DELETE CASCADE removes chunks and messages; the chunks_ad trigger
        removes the FTS rows. LanceDB vectors are deleted separately by the
        caller, because SQLite cannot reach them.

        `files_fts` is a standalone FTS table, not an external-content one, so
        nothing cascades into it and it has to be cleared by hand. Forgetting
        leaves a deleted file findable by name forever, which looks exactly like
        the index being wrong.
        """
        with self.write() as conn:
            conn.execute("DELETE FROM files_fts WHERE rowid = ?", (file_id,))
            conn.execute("DELETE FROM files WHERE id = ?", (file_id,))
            self._bump_generation(conn)

    def file_ids_under_archive(self, archive_path: str) -> list[int]:
        r"""Every row that lives *inside* this archive file.

        A message or a zip member is stored as `<archive path>#<key>`, so the
        contents are found by prefix. Ids only: an archive of 200,000 emails is
        200,000 rows, and materialising them as records to throw the rest away
        would cost minutes and hundreds of megabytes at the end of a run.

        **`ESCAPE` is not optional here.** A real path can contain `%` or `_` -
        `Q1_2024%_final.pst` is an ordinary Windows filename - and in a `LIKE`
        pattern those are wildcards. Without escaping, that archive would match
        and delete rows belonging to entirely unrelated files.
        """
        pattern = _like_escape(str(archive_path)) + "#%"
        rows = self.conn.execute(
            "SELECT id FROM files WHERE path LIKE ? ESCAPE '\\'", (pattern,)
        ).fetchall()
        return [int(row[0]) for row in rows]

    def file_ids_at_or_under(self, path: str) -> list[int]:
        r"""Every row that is this path, lies under it, or came out of it.

        Work order 0z F1 (the folder watch). Told that `D:\Docs\Old` has gone,
        the index must let go of the folder's files; told that `mail.pst` has
        gone, of its messages. Nobody can ask the disk which it was - it is no
        longer there - so all three shapes are matched:

        * the path itself (a file);
        * `path\...` and `path/...` (a folder's contents, or a zip's members);
        * `path#...` (a mailbox's messages - see `file_ids_under_archive`).

        Ids only, for the reason given there.

        **Ranges on the path, not `LIKE`.** `path LIKE 'D:\Docs\Old\%'` cannot
        use the index on `path` (SQLite's `LIKE` ignores the case of A-Z and
        the index does not), so each call would read the whole table - and a
        folder deleted with two thousand files in it is two thousand calls.
        "Starts with `X\`" is the same as "from `X\` up to, not including,
        `X]`" (`]` is the character after `\`), which the index answers
        directly. Checked with `EXPLAIN QUERY PLAN` in `test_folder_watch.py`.
        It also needs no escaping: nothing here is a pattern. The price is
        that the letters must match exactly, so a row stored as `d:\docs\...`
        is not found from `D:\Docs` - it waits for the next ordinary run's
        clean-up, which compares without case.
        """
        text = str(path).rstrip("\\/")
        if not text:
            return []
        found = [int(row[0]) for row in self.conn.execute(
            "SELECT id FROM files WHERE path = ?", (text,)).fetchall()]
        # One statement per shape, so each is a search of the index on its
        # own; joined with OR the planner may give up and scan.
        for separator in ("\\", "/", "#"):
            rows = self.conn.execute(
                "SELECT id FROM files WHERE path >= ? AND path < ?",
                (text + separator, text + chr(ord(separator) + 1))).fetchall()
            found.extend(int(row[0]) for row in rows)
        return found

    def iter_file_origins(self) -> Iterator[tuple[int, str, bool, Optional[str]]]:
        r"""`(id, path, is_message, mailbox)` for every row not on a catalogued
        drive.

        2026-10-07, for `app.index.forget_folder`: which folder each row came
        from. `mailbox` is the `.pst` a message was read from
        (`messages.store_path`), because **a message's path is not under any
        folder** - it is `pst://<mailbox name>/<entry id>`. None for a message
        read through Outlook, and for everything that is not a message,
        attachments included. A drive's rows (`volume_id`) are the Offline
        tab's and are left out.
        """
        for row in self.conn.execute(
                "SELECT f.id, f.path, m.file_id IS NOT NULL, m.store_path FROM files f "
                "LEFT JOIN messages m ON m.file_id = f.id "
                "WHERE f.volume_id IS NULL"):
            yield int(row[0]), str(row[1]), bool(row[2]), row[3]

    def mail_archives(self) -> list[dict[str, Any]]:
        r"""Every `.pst` and `.ost` in the index, with how many messages were
        read out of each. Ordered by path.

        2026-10-07, for the Mail archives box in Settings (the owner: "for each
        pst file it can be configured how to index outlook or direct"). The
        archive's own row - its marker (`source_kind='archive'`) once it has
        been read, or a plain `file` row while it is waiting or was skipped -
        with `path`, `status`, `skip_code`, and `messages`: the `messages` rows
        whose `store_path` is that path, counted in **one grouped query**, not
        one per archive. A drive's rows (`volume_id`) are the Offline tab's,
        and an archive attached to a message (`pst://...`) is not a file on
        disk anybody could read again; neither is listed.
        """
        rows = self.conn.execute(
            "SELECT f.path, f.status, f.skip_code, COALESCE(m.n, 0) AS messages "
            "FROM files f "
            "LEFT JOIN (SELECT store_path, COUNT(*) AS n FROM messages "
            "           WHERE store_path IS NOT NULL GROUP BY store_path) m "
            "       ON m.store_path = f.path "
            "WHERE f.ext IN ('pst', 'ost') AND f.source_kind IN ('archive', 'file') "
            "  AND f.volume_id IS NULL AND f.path NOT LIKE 'pst://%' "
            "ORDER BY f.path").fetchall()
        return [{"path": str(row[0]), "status": str(row[1] or ""),
                 "skip_code": row[2], "messages": int(row[3] or 0)}
                for row in rows]

    def delete_file_by_path(self, path: str) -> Optional[int]:
        """`delete_file` for a path; the id removed, or None if it was not
        in the index (not an error: the walker reports deletions it infers)."""
        record = self.get_file(path)
        if record is None:
            return None
        self.delete_file(record.id)
        return record.id

    def paths_with_skip_code(self, code: str, *, limit: Optional[int] = None) -> list[str]:
        """Paths of the ordinary files whose row carries `code`, oldest first.

        Work order 202626270515 (the media backlog): a queue kept as skip rows is
        read back with one query on `idx_files_skip`, not by iterating every
        skipped file in Python. `source_kind = 'file'` so a message inside an
        archive is never offered to something that expects a path on disk.
        """
        sql = ("SELECT path FROM files WHERE skip_code = ? AND source_kind = 'file' "
               "ORDER BY id")
        params: list[Any] = [code]
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
        return [row["path"] for row in self.conn.execute(sql, params).fetchall()]

    # -- timed-out files (order 0z F3) ----------------------------------------
    #
    # Which rows count: the ones `app.core.file_state` calls TimedOut - a
    # SKIPPED or FAILED row whose code is in `TIMEOUT_CODES` - that are whole
    # files on an ordinary drive. `source_kind = 'file'` keeps a message inside
    # a mailbox out (the limit is on the mailbox, and its row is the mailbox's);
    # `volume_id IS NULL` keeps Offline Media's rows out, whose `path` is not a
    # path on disk (`volume_synthetic_path`) and which a rescan of that drive
    # reads, not an index run.
    #
    # **The unary `+` is load-bearing.** It tells SQLite not to use an index
    # for that term, which leaves `skip_code` - the partial `idx_files_skip`,
    # a few rows - as the only way in. Without it the planner chose
    # `idx_files_source_kind` (`source_kind = 'file'`: most of the table) for
    # the grouped statement and would be free to choose `idx_files_ext` (every
    # `.pdf` in the corpus) for the other; `test_timed_out_retry.py` caught
    # the first and holds both.

    _TIMED_OUT_WHERE = ("skip_code IN ({codes}) AND +status IN ('SKIPPED', 'FAILED') "
                        "AND +source_kind = 'file' AND +volume_id IS NULL")

    def _timed_out_where(self) -> tuple[str, list[Any]]:
        from app.core.file_state import TIMEOUT_CODES

        codes = sorted(TIMEOUT_CODES)
        return (self._TIMED_OUT_WHERE.format(codes=",".join("?" * len(codes))),
                list(codes))

    def timed_out_groups(self) -> list[dict[str, Any]]:
        """The timed-out files, one row per file type, most files first.

        `[{"ext": "pdf", "count": 12, "example": "D:/Docs/big.pdf"}, ...]` -
        `ext` as `files.ext` holds it (no dot; `""` for a file with none).
        **By type because the limit is by type**: text and code get one limit,
        documents ten times it, mailboxes and archives a limit on no progress
        (`app/index/file_watch.py`), so "every .pdf that timed out" is a group
        that one longer limit suits.

        One statement, answered from the partial `idx_files_skip` - the rows
        that carry the code, never the table (`test_timed_out_retry.py` holds
        the plan).
        """
        where, params = self._timed_out_where()
        rows = self.conn.execute(
            f"SELECT ext, COUNT(*) AS n, MIN(path) AS example FROM files "
            f"WHERE {where} GROUP BY ext ORDER BY n DESC, ext", params)
        return [{"ext": row["ext"] or "", "count": int(row["n"]),
                 "example": row["example"]} for row in rows]

    def timed_out_files(self, ext: Optional[str] = None) -> list[dict[str, Any]]:
        """The timed-out files of one type (`ext`, as `timed_out_groups` names
        it), or all of them for None. Oldest row first.

        Each is `{"path", "size_bytes", "mtime_ns", "skip_detail"}`: the size
        and date the file had when it timed out, so a caller can tell a file
        that has changed since, and the sentence recorded with it.
        """
        where, params = self._timed_out_where()
        if ext is not None:
            where += " AND +ext = ?"
            params.append(str(ext))
        rows = self.conn.execute(
            f"SELECT path, size_bytes, mtime_ns, skip_detail FROM files "
            f"WHERE {where} ORDER BY id", params)
        return [{"path": row["path"], "size_bytes": int(row["size_bytes"]),
                 "mtime_ns": int(row["mtime_ns"]),
                 "skip_detail": row["skip_detail"] or ""} for row in rows]

    def iter_files(
        self, status: Optional[str] = None, *, source_kind: Optional[str] = None,
        volume_id: Optional[int] = None, skip_codes: Optional[Iterable[str]] = None,
    ) -> Iterator[FileRecord]:
        """Files, optionally narrowed. Filter in SQL, never in Python.

        `skip_codes` (2026-10-04, code review): only rows whose `skip_code` is
        one of these - "the skipped files a run reads again" without building
        a record for every other skip. With `status`, `idx_files_status_skip`
        (schema v33) answers it. An empty collection matches nothing. Each of
        the index run's re-queues wants one code, and an index with 100,000
        held pictures (`ERR_OCR_HELD`) built a record for each, every run, to
        find a few locked files.

        `source_kind` matters at scale rather than for tidiness: an archive of
        200,000 emails is 200,000 rows, and a caller that wants only the few
        thousand real files would otherwise build a `FileRecord` for every
        message first and discard 98% of them.

        `volume_id` is the same idea for Offline Media: a rescan or a delete
        only ever cares about one source's rows, and `idx_files_volume` makes
        the filter free.

        """
        clauses: list[str] = []
        params: list[Any] = []
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        if source_kind is not None:
            clauses.append("source_kind = ?")
            params.append(source_kind)
        if volume_id is not None:
            clauses.append("volume_id = ?")
            params.append(volume_id)
        if skip_codes is not None:
            codes = sorted({str(code) for code in skip_codes})
            if not codes:
                return
            clauses.append(f"skip_code IN ({','.join('?' * len(codes))})")
            params.extend(codes)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        for row in self.conn.execute(f"SELECT * FROM files{where} ORDER BY id", params):
            yield FileRecord.from_row(row)

    def volume_file_ids(self, volume_id: int) -> list[int]:
        """Ids only, for a batched cascade delete. See `file_ids_under_archive`
        for why materialising full records first would be the wrong shape at
        scale - a media drive can hold hundreds of thousands of rows."""
        rows = self.conn.execute(
            "SELECT id FROM files WHERE volume_id = ?", (int(volume_id),)
        ).fetchall()
        return [int(row[0]) for row in rows]

    def case_twins(self) -> list[tuple[int, str, str]]:
        """`(id, path, source_kind)` of file and archive rows whose path is
        matched by another row's in everything but letter case.

        2026-09-30, for the index run's clean-up (`Pipeline._old_spellings`):
        `files.path` is compared exactly, so a file renamed `Report.docx` ->
        `report.docx` on a disk that ignores case gets a second row and keeps
        the first. This only *finds* such pairs; whether two spellings are one
        file is the pipeline's question (`osbridge.pathnames.path_key`), not
        this table's - on a disk that respects case they are two files.

        One pass over the file and archive rows (`idx_files_source_kind`; mail
        messages are not read), sorted in SQLite, and only the twins come back
        - none at all on an ordinary run. SQLite's `lower()` folds `A`-`Z`
        only, so two spellings that differ in an accented letter are not
        found here.
        """
        rows = self.conn.execute(
            """
            SELECT id, path, source_kind FROM files
            WHERE source_kind IN ('file', 'archive')
              AND lower(path) IN (
                    SELECT lower(path) FROM files
                    WHERE source_kind IN ('file', 'archive')
                    GROUP BY lower(path) HAVING COUNT(*) > 1)
            ORDER BY lower(path), id
            """
        ).fetchall()
        return [(int(row[0]), str(row[1]), str(row[2])) for row in rows]

    def skipped_summary(self) -> dict[str, int]:
        """Counts by skip_code, for the 'N files skipped - review' panel."""
        rows = self.conn.execute(
            "SELECT skip_code, COUNT(*) AS n FROM files "
            "WHERE skip_code IS NOT NULL GROUP BY skip_code ORDER BY n DESC"
        )
        return {row["skip_code"]: int(row["n"]) for row in rows}

    def status_counts(self, offline_volume_ids: Sequence[int] = ()) -> dict[str, Any]:
        """The Indexing page's funnel, as the three groupings `file_state` needs.

        Returns `{"by_status": {status: n}, "coded": [(status, code, n)],
        "offline": {status: n}}` - see `app.core.file_state.funnel_counts`,
        which turns them into one count per word.

        **Three small statements, each answered from an index it already has,
        rather than one `GROUP BY status, skip_code, volume_id` over the
        table.** That one reads every row of `files` - twenty million at the
        target scale - on a refresh that runs every few seconds during a run.
        Checked with `EXPLAIN QUERY PLAN` (`test_status_funnel.py` holds it):

        * `by_status` is a covering scan of `idx_files_status`, the same
          statement `stats()` has always run;
        * `coded` is a search of the partial `idx_files_skip` for the handful
          of codes that change a file's word - deferred and timed-out rows, not
          every skip;
        * `offline` searches `idx_files_volume` for the disconnected volumes'
          rows only, and is not run at all when every volume is connected.

        Offline rows are left out of `coded`, so a held picture on a drive in a
        drawer is counted once, as Offline.

        2026-10-04, code review: counted again only after a write
        (`_unchanged_since`); between runs the 5-second refresh costs one
        `PRAGMA`. A fresh copy is returned each time.
        """
        offline = tuple(sorted(int(v) for v in offline_volume_ids or ()))
        held = self._unchanged_since(f"status_counts:{offline}",
                                     lambda: self._count_statuses(offline))
        return {"by_status": dict(held["by_status"]), "coded": list(held["coded"]),
                "offline": dict(held["offline"])}

    def _count_statuses(self, offline_volume_ids: Sequence[int]) -> dict[str, Any]:
        """`status_counts`, read from the table."""
        from app.core.file_state import DEFERRED_CODES, DUPLICATE_CODES, TIMEOUT_CODES

        by_status = {
            row["status"]: int(row["n"])
            for row in self.conn.execute(
                "SELECT status, COUNT(*) AS n FROM files GROUP BY status")
        }
        codes = sorted(DEFERRED_CODES | TIMEOUT_CODES | DUPLICATE_CODES)
        offline = [int(v) for v in offline_volume_ids or ()]
        not_offline = ""
        params: list[Any] = list(codes)
        if offline:
            not_offline = (" AND (volume_id IS NULL OR volume_id NOT IN "
                           f"({','.join('?' * len(offline))}))")
            params.extend(offline)
        coded = [
            (row["status"], row["skip_code"], int(row["n"]))
            for row in self.conn.execute(
                f"SELECT status, skip_code, COUNT(*) AS n FROM files "
                f"WHERE skip_code IN ({','.join('?' * len(codes))}){not_offline} "
                f"GROUP BY status, skip_code", params)
        ]
        away: dict[str, int] = {}
        if offline:
            away = {
                row["status"]: int(row["n"])
                for row in self.conn.execute(
                    f"SELECT status, COUNT(*) AS n FROM files "
                    f"WHERE volume_id IN ({','.join('?' * len(offline))}) "
                    f"GROUP BY status", offline)
            }
        return {"by_status": by_status, "coded": coded, "offline": away}

    def offline_volume_ids(self) -> list[int]:
        """Volumes whose last known status is not ONLINE. Cheap - `volumes` is
        one row per catalogued drive or share.

        **Last known, not live.** The live answer (`offline_media.
        connected_volumes`) is a Windows volume lookup per drive, which the
        results lists pay once per page; the funnel refreshes every few seconds
        during a run and reads the status the Offline Media page last recorded.
        """
        return [int(row["id"]) for row in self.conn.execute(
            "SELECT id FROM volumes WHERE status != 'ONLINE'")]

    def file_states(self, file_ids: Sequence[int]) -> dict[int, dict[str, Any]]:
        """`{file_id: {"status", "skip_code", "volume_id"}}` for a page of results.

        **One query for the page, never one per row** - the same rule as
        `messages_for`, for the same reason: the search box runs on a debounce.
        The Search list's Status column is filled from this, on the worker that
        already decorates the page. Ids that are not in `files` are absent.
        """
        wanted = sorted({int(file_id) for file_id in file_ids or ()})
        if not wanted:
            return {}
        out: dict[int, dict[str, Any]] = {}
        # Chunked under SQLite's host-parameter limit; a page is 20 to 500 rows.
        for start in range(0, len(wanted), 900):
            part = wanted[start:start + 900]
            for row in self.conn.execute(
                f"SELECT id, status, skip_code, volume_id FROM files "
                f"WHERE id IN ({','.join('?' * len(part))})", part):
                out[int(row["id"])] = {"status": row["status"], "skip_code": row["skip_code"],
                                       "volume_id": row["volume_id"]}
        return out

    # -- chunks --------------------------------------------------------------

    def replace_chunks(self, file_id: int, chunks: Sequence[dict[str, Any]]) -> list[int]:
        """Replace every chunk of one file. Returns the new chunk ids.

        Replacing rather than appending keeps re-indexing a changed file
        idempotent, and the triggers keep chunks_fts in step automatically.

        **2026-09-30: inside a `batch()`, a file with no passages yet has its
        keyword-index rows written at the end of the batch** rather than one
        at a time by `chunks_ai` - see `_deferred` for why that is the
        difference between a flat cost and one that rises with the index. A
        file that already has passages (a changed document, a forced re-read)
        takes the path it always took: the waiting rows are written, the
        triggers go back on, and the DELETE and the INSERTs below are mirrored
        by them.
        """
        with self.write(deferring=True) as conn:
            state = self._deferred()
            if state is not None and (
                file_id in state.chunk_files
                or conn.execute(
                    "SELECT 1 FROM chunks WHERE file_id = ? LIMIT 1", (file_id,)
                ).fetchone() is not None
            ):
                state = None
            if state is None:
                # Not a new file, or nothing may wait: exactly as before.
                self._finish_deferred(conn)
                conn.execute("DELETE FROM chunks WHERE file_id = ?", (file_id,))
            elif chunks:
                # New: there is nothing to delete, and no trigger to fire.
                self._switch_triggers(conn, state, on=False)
            ids: list[int] = []
            for ordinal, chunk in enumerate(chunks):
                # `utf8_safe`: a lone surrogate in the text is a crashed run
                # otherwise (2026-10-08). The cleaned text is what the symbols,
                # the row and the deferred FTS write all see.
                text = utf8_safe(chunk["text"])
                symbols = symbol_tokens(text)
                cursor = conn.execute(
                    """
                    INSERT INTO chunks (file_id, ordinal, text, symbols,
                                        char_start, char_end, page, label,
                                        embedded)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)
                    """,
                    (
                        file_id,
                        chunk.get("ordinal", ordinal),
                        text,
                        # camelCase split forms, so `password` finds
                        # `ResetPasswordHandler`. Empty for prose - see
                        # app/core/identifiers.py for why this is not the
                        # whole text again.
                        symbols,
                        chunk.get("char_start"),
                        chunk.get("char_end"),
                        chunk.get("page"),
                        # Adoptions §6a: `Q3!A14`, or None for the great
                        # majority of documents that have no interior address
                        # anybody could act on.
                        utf8_safe(chunk.get("label")),
                    ),
                )
                ids.append(int(cursor.lastrowid))
                if state is not None:
                    # What `chunks_ai` would have been handed for this row.
                    state.chunks.append((ids[-1], text, symbols))
            if state is not None and ids:
                state.chunk_files.add(file_id)
                if len(state.chunks) >= FTS_DEFER_MAX_ROWS:
                    self._index_deferred(conn, state)
            self._bump_generation(conn)
        return ids

    def has_ai_caption(self, file_id: int, *, label: str = "AI caption") -> bool:
        """Has a vision-model caption already been fetched for this file?

        Work order 0i section 3. Cheap cache check for the Describe button
        ("second click instant" - the item's own test wording) and for the
        corpus-wide trickle drain choosing which images still need one. A
        separate label from Florence's own "AI description" (section 1) is
        deliberate - see `app/extract/vision_caption.py`'s module docstring
        for why reusing that label would have been a real bug, not a
        simplification.
        """
        row = self.conn.execute(
            "SELECT 1 FROM chunks WHERE file_id = ? AND label = ? LIMIT 1",
            (file_id, label),
        ).fetchone()
        return row is not None

    #: What `note_photo_untaggable` writes, so `iter_untagged_photos` does not
    #: offer the same photo to Florence-2 on every run.
    PHOTO_TAGS_TRIED = "no text, and Florence-2 found nothing to describe"

    def iter_untagged_photos(
        self, extensions: Sequence[str], *, batch_size: int = 16,
    ) -> Iterator[list[tuple[int, str]]]:
        r"""`(file_id, path)` for photos read with no text and not yet tagged.

        2026-10-04: Florence-2 tagging moved from the photo's own read to the
        end of a run (`Pipeline._drain_photo_tags`), because at ~10 s a photo
        it was three quarters of the time a photo library took - faces, text
        and picture search waited behind it. Such a photo is recorded the way
        a photo with no text always was (`SKIPPED`, `ERR_NO_TEXT_LAYER`) until
        its tags arrive. Newest first, the order the run read them in.
        """
        cleaned = [str(ext).lstrip(".").lower() for ext in extensions if str(ext).strip()]
        if not cleaned:
            return
        placeholders = ",".join("?" for _ in cleaned)
        last_id: Optional[int] = None
        while True:
            rows = self.conn.execute(
                f"""
                SELECT f.id, f.path FROM files f
                WHERE f.status = 'SKIPPED' AND f.skip_code = 'ERR_NO_TEXT_LAYER'
                  AND f.ext IN ({placeholders})
                  AND COALESCE(f.skip_detail, '') != ?
                  AND (? IS NULL OR f.id < ?)
                ORDER BY f.id DESC LIMIT ?
                """,
                [*cleaned, self.PHOTO_TAGS_TRIED, last_id, last_id, int(batch_size)],
            ).fetchall()
            if not rows:
                return
            yield [(int(row[0]), str(row[1])) for row in rows]
            last_id = int(rows[-1][0])

    #: `skip_detail` on an `ERR_PICTURE_TEXT_LATER` photo once its description
    #: has been tried - the end of the run then reads its text.
    PICTURE_DESCRIBED = "described; its text is read next"

    def iter_pictures_waiting(
        self, extensions: Sequence[str], *, code: str = "ERR_PICTURE_TEXT_LATER",
        described: Optional[bool] = None, batch_size: int = 16,
    ) -> Iterator[list[tuple[int, str]]]:
        """`(file_id, path)` for pictures waiting on the end of a run, newest
        first, by their code (`ERR_PICTURE_TEXT_LATER` a photo,
        `ERR_PAGE_TEXT_LATER` a page). `described`: False - not yet described;
        True - described, text still to read; None - either. 2026-10-04."""
        cleaned = [str(ext).lstrip(".").lower() for ext in extensions if str(ext).strip()]
        if not cleaned:
            return
        marks = ",".join("?" for _ in cleaned)
        test = {True: "AND f.skip_detail = ?", False: "AND f.skip_detail IS NOT ?",
                None: "AND ? IS NOT NULL"}[described]
        last_id: Optional[int] = None
        while True:
            rows = self.conn.execute(
                f"""SELECT f.id, f.path FROM files f
                    WHERE f.status = 'SKIPPED' AND f.skip_code = ?
                      AND f.ext IN ({marks}) {test}
                      AND (? IS NULL OR f.id < ?)
                    ORDER BY f.id DESC LIMIT ?""",
                [code, *cleaned, self.PICTURE_DESCRIBED, last_id, last_id, int(batch_size)],
            ).fetchall()
            if not rows:
                return
            yield [(int(r[0]), str(r[1])) for r in rows]
            last_id = int(rows[-1][0])

    def note_picture_described(self, file_id: int) -> None:
        """Half-way marker for a waiting picture: described, text still to
        read. `iter_pictures_waiting(described=...)` reads it back."""
        with self.write() as conn:
            conn.execute("UPDATE files SET skip_detail = ? WHERE id = ?",
                         (self.PICTURE_DESCRIBED, int(file_id)))

    def note_picture_text_read(self, file_id: int, *, found_any: bool) -> None:
        """The end of a picture's run: indexed when its description or its text
        gave it anything, else the settled "no text" every such photo had."""
        if found_any:
            self.mark_indexed(file_id)
            return
        with self.write() as conn:
            conn.execute(
                "UPDATE files SET skip_code = 'ERR_NO_TEXT_LAYER', skip_detail = ? "
                "WHERE id = ?", (self.PHOTO_TAGS_TRIED, int(file_id)))

    def face_scanned(self, file_id: int) -> bool:
        """Has the face detector looked at this file? "Looked" is recorded
        apart from "found", so a photo with no faces is not re-scanned."""
        row = self.conn.execute("SELECT 1 FROM face_scans WHERE file_id = ?",
                                (int(file_id),)).fetchone()
        return row is not None

    def note_photo_untaggable(self, file_id: int) -> None:
        """Florence-2 found nothing to say: leave it skipped, and stop asking."""
        with self.write() as conn:
            conn.execute("UPDATE files SET skip_detail = ? WHERE id = ?",
                         (self.PHOTO_TAGS_TRIED, int(file_id)))

    def add_caption_chunk(self, file_id: int, caption: str,
                           *, label: str = "AI caption") -> int:
        r"""Append one labelled segment to a file that is already indexed.

        Work order 0i section 3. Deliberately additive, unlike
        `replace_chunks` - this file may already carry chunks with other
        labels (`OCR text`, Florence's `AI description`) that must survive
        untouched. `replace_chunks` was considered and rejected for this:
        it deletes and rewrites every chunk of the file, and `ChunkRecord`
        (what `chunks_for_file` returns) has no `label` field, so rebuilding
        the full chunk list from it would silently drop every existing
        chunk's label on the next call - a real bug, not a hypothetical one.

        **The label is written into the stored text itself**, the same
        `prefix_label` convention `DocumentBuilder.add` already offers for
        "a label with no column of its own" (spreadsheet sheet names, slide
        notes markers) - `chunks.label` alone is read back nowhere a preview
        renders from (`chunks_for_file`/`join_chunks` return plain text), so
        a label that lives only in that column would be invisible to
        whoever opens the file, and the standing rule is that AI-written
        text is always marked as AI-written *in what a person reads*, not
        only in a column a query can filter on.

        `embedded=0`, the same starting state every new chunk gets
        (`replace_chunks`) - `Pipeline._drain_unembedded` (the enrichment
        backlog's own `unembedded_chunk` kind) picks it up at the next run,
        which is the existing, tested mechanism for "chunks written first,
        vectorised a batch later" rather than a new one invented here.
        FTS stays in step through the same triggers `replace_chunks` already
        relies on - they are on the table, not on any one write path.
        """
        text = f"{label}: {caption}"
        with self.write() as conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(ordinal), -1) FROM chunks WHERE file_id = ?",
                (file_id,),
            ).fetchone()
            next_ordinal = int(row[0]) + 1
            cursor = conn.execute(
                """
                INSERT INTO chunks (file_id, ordinal, text, symbols,
                                    char_start, char_end, page, label,
                                    embedded)
                VALUES (?, ?, ?, ?, NULL, NULL, NULL, ?, 0)
                """,
                (file_id, next_ordinal, text, symbol_tokens(text), label),
            )
            self._bump_generation(conn)
            return int(cursor.lastrowid)

    def get_chunk(self, chunk_id: int) -> Optional[ChunkRecord]:
        """One passage by id, or None. `label` and `symbols` are not carried
        (see `ChunkRecord`)."""
        row = self.conn.execute("SELECT * FROM chunks WHERE id = ?", (chunk_id,)).fetchone()
        if row is None:
            return None
        return ChunkRecord(
            id=row["id"], file_id=row["file_id"], ordinal=row["ordinal"], text=row["text"],
            char_start=row["char_start"], char_end=row["char_end"], page=row["page"],
            embedded=row["embedded"],
        )

    def chunks_for_file(self, file_id: int) -> list[ChunkRecord]:
        """Every passage of one file in reading order - what the preview joins.
        A 5,000-page PDF is thousands of rows, so this is for one file at a
        time on a worker, never for a page of results."""
        rows = self.conn.execute(
            "SELECT * FROM chunks WHERE file_id = ? ORDER BY ordinal", (file_id,)
        )
        return [
            ChunkRecord(
                id=r["id"], file_id=r["file_id"], ordinal=r["ordinal"], text=r["text"],
                char_start=r["char_start"], char_end=r["char_end"], page=r["page"],
                embedded=r["embedded"],
            )
            for r in rows
        ]

    def iter_unembedded(self, batch_size: int = 256) -> Iterator[list[ChunkRecord]]:
        """Chunks awaiting a vector. Drives both indexing and vector rebuild."""
        last_id = 0
        while True:
            rows = self.conn.execute(
                "SELECT * FROM chunks WHERE embedded = 0 AND id > ? ORDER BY id LIMIT ?",
                (last_id, batch_size),
            ).fetchall()
            if not rows:
                return
            batch = [
                ChunkRecord(
                    id=r["id"], file_id=r["file_id"], ordinal=r["ordinal"], text=r["text"],
                    char_start=r["char_start"], char_end=r["char_end"], page=r["page"],
                    embedded=r["embedded"],
                )
                for r in rows
            ]
            last_id = batch[-1].id
            yield batch

    def unembedded_by_file(
        self, batch_size: int = 256, first_folders: Sequence[str] = (),
    ) -> list[list[tuple[int, int]]]:
        r"""Chunks awaiting a vector, as `(chunk_id, file_id)` batches of **whole files**.

        2026-10-08. `iter_unembedded` cuts batches by chunk id, so one file's
        passages can land in two batches - and the pipeline replaces a file's
        vectors with `delete_by_file_ids` just before each batch's add, so the
        second batch deleted the vectors the first had just written. A
        spreadsheet with 5,000 rows left unembedded kept only its last
        batch's worth. Never splitting a file is what makes that delete safe.

        A batch holds at least `batch_size` passages unless the backlog runs
        out, and more when its last file is long - a file is never cut. One
        sorted read of two integers per passage, not of the text: the caller
        fetches text with `chunk_texts` one batch at a time.

        **Newest file first** (2026-10-10, storage review S2). The order was
        `file_id, id` - oldest stored first - while a run reads files newest
        first (`app.index.read_order`). With a backlog of days (5.9 million
        passages measured on the owner's index) the files a person is most
        likely to search for got their vectors last. Now files in the order a
        run reads them: those under `first_folders` first, in the order given
        (the run's `--first` folders), then by modified time, newest first;
        `file_id` breaks ties so the order is the same every time. Passages
        within a file stay in id order, and a file is still never split.

        `first_folders` matches as `walker._priority_for` does - the folder
        itself or anything under it at a separator, so `C:\Docs` does not take
        `C:\Docs2` - without regard to the case of A-Z. Storage cannot import
        the walker (it is the layer below), so the rule is repeated here.

        Measured 2026-10-10 on this laptop, synthetic stores with 120-word
        passages, 60% awaiting a vector, CPU shared with four other jobs (so
        each figure is a range over two runs of a median of 3): 120k waiting
        passages, 0.37-0.66 s before, 0.49-0.57 s now; 600k, 3.42-4.36 s
        before, 3.28-4.27 s now; two first folders add nothing measurable
        beyond the noise. No slower: the time is reading each passage's row for
        its `file_id` (`idx_chunks_pending` holds only the id), which both
        versions pay; the file's date is one primary-key lookup per passage,
        and the sort is per file, in Python. Sorting the passages in SQL
        instead (`ORDER BY f.mtime_ns DESC, ...`) was slower (0.59 s against
        0.37 s at 120k): a temporary B-tree of every passage. A covering index
        `chunks(file_id) WHERE embedded = 0` would take the 600k case to about
        1.8 s, at the price of a second index written for every passage and a
        full read of `chunks` to build it - not taken for a read made once at
        the start of a run that then embeds for hours. Plan, pinned in
        `test_backlog_order.py`: `SEARCH c USING INDEX idx_chunks_pending`,
        then `SEARCH f USING INTEGER PRIMARY KEY`, no temporary B-tree.
        """
        groups: dict[int, list[tuple[int, int]]] = {}
        modified: dict[int, int] = {}
        # Plain tuples, not `sqlite3.Row`, and `fetchall()` rather than a loop
        # over the cursor: `_GuardedCursor.__next__` takes its guard once per
        # row, which profiled at half this method's time over 120k rows.
        cursor = self.conn.cursor()
        cursor.row_factory = None
        rows = cursor.execute(
            # LEFT: a passage whose file row is missing (foreign keys off in a
            # repair or a test) is still handed back, as it always was - last,
            # with no date.
            "SELECT c.id, c.file_id, f.mtime_ns FROM chunks c "
            "LEFT JOIN files f ON f.id = c.file_id WHERE c.embedded = 0").fetchall()
        # Rows arrive in passage-id order, and one file's passages are written
        # together (`replace_chunks`), so they come as runs: grouped a run at a
        # time rather than a row at a time. A file met again later (its
        # passages not contiguous) is joined to its first run and re-sorted.
        for file_id, run in groupby(rows, key=itemgetter(1)):
            run_rows = list(run)
            pairs = [(row[0], file_id) for row in run_rows]
            found = groups.get(file_id)
            if found is None:
                groups[file_id] = pairs
                modified[file_id] = int(run_rows[0][2] or 0)
            else:
                found.extend(pairs)
                found.sort()
        rank = self._first_folder_ranks(groups, first_folders)
        unranked = len(tuple(first_folders))         # after every first folder
        order = sorted(groups, key=lambda f: (rank.get(f, unranked), -modified[f], f))
        batches: list[list[tuple[int, int]]] = []
        batch: list[tuple[int, int]] = []
        for file_id in order:
            if batch and len(batch) >= batch_size:
                batches.append(batch)
                batch = []
            batch.extend(groups[file_id])
        if batch:
            batches.append(batch)
        return batches

    def _first_folder_ranks(self, file_ids: Iterable[int],
                            first_folders: Sequence[str]) -> dict[int, int]:
        """`{file_id: position of the first folder it is under}` for the files
        under any of `first_folders`; others are absent. One lookup of paths
        per 500 files, and none at all when no folder is given."""
        prefixes = [str(folder or "").strip().rstrip("\\/").lower()
                    for folder in first_folders]
        prefixes = [p for p in prefixes if p]
        if not prefixes:
            return {}
        ids = [int(f) for f in file_ids]
        ranks: dict[int, int] = {}
        for start in range(0, len(ids), 500):
            part = ids[start:start + 500]
            marks = ",".join("?" * len(part))
            for file_id, path in self.conn.execute(
                    f"SELECT id, path FROM files WHERE id IN ({marks})", part):
                text = str(path).lower()
                for position, prefix in enumerate(prefixes):
                    if text == prefix or (text.startswith(prefix)
                                          and text[len(prefix):len(prefix) + 1] in ("\\", "/")):
                        ranks[int(file_id)] = position
                        break
        return ranks

    def chunk_texts(self, chunk_ids: Iterable[int]) -> dict[int, str]:
        """`{chunk_id: text}` for the given chunks that still await a vector.

        A chunk that is gone (its file was read again since) or already has a
        vector is simply absent from the answer - the caller decides what that
        means for the rest of its file.
        """
        ids = [int(one) for one in chunk_ids]
        found: dict[int, str] = {}
        for start in range(0, len(ids), 500):
            part = ids[start:start + 500]
            marks = ",".join("?" * len(part))
            for row in self.conn.execute(
                f"SELECT id, text FROM chunks WHERE embedded = 0 AND id IN ({marks})",
                part,
            ):
                found[int(row["id"])] = row["text"]
        return found

    def iter_uncaptioned_images(
        self, extensions: Sequence[str], *, batch_size: int = 32,
        label: str = "AI caption",
    ) -> Iterator[list[tuple[int, str]]]:
        r"""`(file_id, path)` pairs for indexed photos with no vision caption yet.

        Work order 0i section 3b - the caption trickle's own candidate query.
        Filtered in SQL, not iterated in Python: non-negotiable §"anything
        that iterates all of `files` is a bug" (`HANDOFF.md`), the same
        reasoning `_prune_missing`'s all-files scan was fixed for. `NOT
        EXISTS` against `chunks` rather than a Python-side set of already
        -captioned ids, so this scales the same way whether the backlog is
        ten photos or a million.

        `extensions` is passed in rather than imported - storage does not
        depend on `app.extract` (the layering rule `app/ui/` may call below
        it, nothing below may import from `app/ui/` has the same shape one
        layer down: `app/storage/` stays free of `app/extract/`). Callers
        pass `app.extract.ocr.OcrExtractor.extensions`, the same photo-class
        set the OCR ladder and Florence-2 already route on.
        """
        # **Normalised here, not trusted from the caller.** `files.ext` is
        # stored without its leading dot (`indexed_ext`/`_norm_ext`'s own
        # convention) but `OcrExtractor.extensions` - the set every caller
        # actually has to hand - carries one (`.jpg`), the shape the
        # extractor registry itself uses. Comparing the two unnormalised
        # silently matched nothing at all: found running this drain for
        # real, not assumed - the exact "a filter that looks like it works
        # and cannot" shape `file_filter_sql`'s own comment on `repo:`
        # warns about.
        cleaned = [str(ext).lstrip(".").lower() for ext in extensions if str(ext).strip()]
        if not cleaned:
            return
        placeholders = ",".join("?" for _ in cleaned)
        last_id = 0
        while True:
            rows = self.conn.execute(
                f"""
                SELECT f.id, f.path FROM files f
                WHERE f.id > ? AND f.status = 'INDEXED' AND f.ext IN ({placeholders})
                  AND NOT EXISTS (
                      SELECT 1 FROM chunks c
                      WHERE c.file_id = f.id AND c.label = ?
                  )
                ORDER BY f.id LIMIT ?
                """,
                (last_id, *cleaned, label, batch_size),
            ).fetchall()
            if not rows:
                return
            batch = [(int(r["id"]), str(r["path"])) for r in rows]
            last_id = batch[-1][0]
            yield batch

    # -- faces and piles (work order 0j) --------------------------------------
    #
    # **Automatic (`faces`) and identity (`piles.name`) stay separate all the
    # way down**, per the order's own guardrails: nothing here ever infers or
    # suggests a name - only a person typing one does.

    def add_face(self, file_id: int, bbox: tuple[float, float, float, float],
                 embedding: bytes) -> int:
        r"""Record one detection. Section 1a. Never pre-assigns a pile - the
        caller (`app.index.face_clustering`, driven from the images pass or
        the backfill drain) decides that separately, in its own transaction,
        so a clustering failure never loses the detection itself.
        """
        with self.write() as conn:
            cursor = conn.execute(
                """
                INSERT INTO faces (file_id, bbox_x, bbox_y, bbox_w, bbox_h,
                                   embedding, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (file_id, *bbox, embedding, int(time.time())),
            )
            return int(cursor.lastrowid)

    def mark_face_scanned(self, file_id: int) -> None:
        """This file has been through the face detector, whatever it found -
        see `face_scans`' own migration note for why "looked and found
        nothing" needs recording as its own fact."""
        with self.write() as conn:
            conn.execute(
                "INSERT INTO face_scans (file_id, scanned_at) VALUES (?, ?) "
                "ON CONFLICT(file_id) DO UPDATE SET scanned_at = excluded.scanned_at",
                (file_id, int(time.time())),
            )

    def forget_face_scans_of_mail_pictures(self) -> int:
        """Un-mark every mail attachment recorded as face-scanned with no face
        found, so the next run looks at it. Returns how many. 2026-10-07.

        Until that day the scan could not open a picture that arrived in mail
        (its key is not a place on disk), found nothing in what it could not
        read, and marked it as looked at - 1,188 of the owner's. A mark beside
        a face that *was* found is real and is kept."""
        from app.core.row_facts import attachment_sql

        with self.write() as conn:
            cursor = conn.execute(
                "DELETE FROM face_scans WHERE file_id IN ("
                f"SELECT f.id FROM files f WHERE {attachment_sql('f')} "
                "AND NOT EXISTS (SELECT 1 FROM faces WHERE faces.file_id = f.id))")
            return int(cursor.rowcount or 0)

    @staticmethod
    def _face_from_row(row: sqlite3.Row) -> FaceRecord:
        return FaceRecord(
            id=row["id"], file_id=row["file_id"],
            bbox=(row["bbox_x"], row["bbox_y"], row["bbox_w"], row["bbox_h"]),
            embedding=row["embedding"], pile_id=row["pile_id"],
            confidence=row["confidence"], suggested_pile_id=row["suggested_pile_id"],
        )

    def clear_faces_for_file(self, file_id: int) -> int:
        """Forget the faces found in one file, so it can be scanned again. Returns rows.

        Work order 202626270515: a video that changed on disk is read again and its
        pictures are different, so the faces recorded from the old ones are stale.
        Only ever called for a video - a photograph's faces are left alone by every
        existing path.
        """
        with self.write() as conn:
            cursor = conn.execute("DELETE FROM faces WHERE file_id = ?", (int(file_id),))
            conn.execute("DELETE FROM face_scans WHERE file_id = ?", (int(file_id),))
            return int(cursor.rowcount or 0)

    def faces_for_file(self, file_id: int) -> list[FaceRecord]:
        """Every detection in one photo, in detection order, embeddings included."""
        rows = self.conn.execute(
            "SELECT * FROM faces WHERE file_id = ? ORDER BY id", (file_id,)
        ).fetchall()
        return [self._face_from_row(r) for r in rows]

    def iter_unclustered_faces(self, batch_size: int = 64) -> Iterator[list[FaceRecord]]:
        """Faces neither assigned nor suggested yet. Section 1b's own queue.

        Filtered by `idx_faces_unassigned`, the partial index built for
        exactly this query - the same reasoning `iter_unembedded` and
        `iter_uncaptioned_images` already use.
        """
        last_id = 0
        while True:
            rows = self.conn.execute(
                "SELECT * FROM faces WHERE id > ? AND pile_id IS NULL "
                "AND suggested_pile_id IS NULL ORDER BY id LIMIT ?",
                (last_id, batch_size),
            ).fetchall()
            if not rows:
                return
            batch = [self._face_from_row(r) for r in rows]
            last_id = batch[-1].id
            yield batch

    def pile_centroids(self) -> dict[int, list[bytes]]:
        r"""Every pile's own face embeddings, keyed by pile id.

        Returns the raw embeddings rather than one pre-averaged centroid -
        `app.index.face_clustering.centroid_of` does the averaging, so the
        maths that decides "is this the same person" lives in one pure,
        tested module rather than being duplicated in SQL. A pile with
        thousands of faces makes this expensive; nothing here claims that
        scale is solved, the same honest gap `distinct_value_counts` records
        for an unscoped GROUP BY - see its own docstring.
        """
        rows = self.conn.execute(
            "SELECT pile_id, embedding FROM faces WHERE pile_id IS NOT NULL"
        ).fetchall()
        out: dict[int, list[bytes]] = {}
        for row in rows:
            out.setdefault(int(row["pile_id"]), []).append(row["embedding"])
        return out

    def create_pile(self, name: Optional[str] = None) -> int:
        """A new pile, unnamed unless `name` is given. Returns its id.

        Raises `sqlite3.IntegrityError` for a name already taken: names are
        unique (`idx_piles_name`), and the caller (`rename_pile`) checks first.
        """
        with self.write() as conn:
            cursor = conn.execute(
                "INSERT INTO piles (name, created_at) VALUES (?, ?)",
                (name, int(time.time())),
            )
            return int(cursor.lastrowid)

    def assign_face(self, face_id: int, pile_id: int, *,
                     confidence: Optional[float] = None) -> None:
        """Section 1b/2c. Clears any pending suggestion - a face is either
        assigned or awaiting a decision, never both. Re-syncs this file's
        `People:` segment (section 3a) - a no-op text-wise unless `pile_id`
        is already named, but always correct rather than sometimes stale."""
        with self.write() as conn:
            row = conn.execute(
                "SELECT file_id FROM faces WHERE id = ?", (face_id,)).fetchone()
            conn.execute(
                "UPDATE faces SET pile_id = ?, confidence = ?, "
                "suggested_pile_id = NULL WHERE id = ?",
                (pile_id, confidence, face_id),
            )
        if row is not None:
            self.sync_people_segment(int(row["file_id"]))

    def suggest_face(self, face_id: int, pile_id: int) -> None:
        """Section 2c's borderline queue - "Is this Daddy?" - never assigns."""
        with self.write() as conn:
            conn.execute(
                "UPDATE faces SET suggested_pile_id = ? WHERE id = ?",
                (pile_id, face_id),
            )

    def picture_counts(self, extensions: Sequence[str]) -> dict[str, int]:
        """The Indexing page's picture line, read in one statement. 2026-10-04,
        the owner: "status of pictures indexing ... like it has for files".

        `extensions` from the caller (storage does not import `app.extract`).
        Each count rides an index that already exists: `idx_files_ext`,
        `idx_files_status_skip`, `idx_faces_unassigned`, `face_scans`' key.
        How many photos *have* a description is left out on purpose - it needs
        every chunk's label, which is a scan, five-secondly, during a run.
        """
        cleaned = [str(ext).lstrip(".").lower() for ext in extensions if str(ext).strip()]
        empty = {"pictures": 0, "read": 0, "faces_looked": 0, "faces": 0,
                 "people": 0, "unsorted": 0, "to_describe": 0, "text_to_read": 0}
        if not cleaned:
            return empty
        marks = ",".join("?" for _ in cleaned)
        try:
            row = self.conn.execute(f"""
                SELECT
                  (SELECT count(*) FROM files WHERE ext IN ({marks})),
                  (SELECT count(*) FROM files WHERE ext IN ({marks}) AND status != 'PENDING'),
                  (SELECT count(*) FROM face_scans),
                  (SELECT count(*) FROM faces),
                  (SELECT count(*) FROM piles),
                  (SELECT count(*) FROM faces
                     WHERE pile_id IS NULL AND suggested_pile_id IS NULL),
                  (SELECT count(*) FROM files
                     WHERE status = 'SKIPPED' AND ext IN ({marks}) AND (
                       (skip_code = 'ERR_NO_TEXT_LAYER' AND COALESCE(skip_detail, '') != ?)
                       OR (skip_code = 'ERR_PICTURE_TEXT_LATER'
                           AND skip_detail IS NOT ?))),
                  (SELECT count(*) FROM files
                     WHERE status = 'SKIPPED'
                       AND skip_code IN ('ERR_PICTURE_TEXT_LATER', 'ERR_PAGE_TEXT_LATER')
                       AND ext IN ({marks}))
                """, [*cleaned, *cleaned, *cleaned, self.PHOTO_TAGS_TRIED,
                      self.PICTURE_DESCRIBED, *cleaned]).fetchone()
        except sqlite3.OperationalError:
            return empty
        return dict(zip(empty, (int(v or 0) for v in row)))

    def faces_stamp(self) -> tuple[int, int, int, int]:
        """`(faces, piles, grouped faces, suggested faces)` - what the naming
        page compares to decide whether to re-read. Four counts over two small
        tables; 2026-10-04, so the page follows a run without re-reading every
        pile on a timer. `(0, 0, 0, 0)` on an index without the tables."""
        try:
            row = self.conn.execute(
                "SELECT (SELECT count(*) FROM faces), (SELECT count(*) FROM piles), "
                "(SELECT count(*) FROM faces WHERE pile_id IS NOT NULL), "
                "(SELECT count(*) FROM faces WHERE suggested_pile_id IS NOT NULL)"
            ).fetchone()
        except sqlite3.OperationalError:
            return (0, 0, 0, 0)
        return tuple(int(v or 0) for v in row)  # type: ignore[return-value]

    def pending_suggestions(self, limit: int = 20) -> list["PendingSuggestion"]:
        r"""Section 2c's own queue: every face suggested against an
        already-*named* pile, oldest first, capped so the strip never grows
        past a screenful. A suggestion against a pile nobody has named yet
        is excluded - "Is this None?" has nothing to ask - and stays
        reachable through the ordinary unclustered flow until someone names
        that pile, at which point the next backfill pass (or a fresh
        `classify` call) can suggest it again.
        """
        rows = self.conn.execute(
            "SELECT f.id AS face_id, f.file_id, f.bbox_x, f.bbox_y, "
            "f.bbox_w, f.bbox_h, p.id AS pile_id, p.name AS pile_name, "
            "files.path AS path "
            "FROM faces f "
            "JOIN piles p ON p.id = f.suggested_pile_id "
            "JOIN files ON files.id = f.file_id "
            "WHERE f.suggested_pile_id IS NOT NULL AND p.name IS NOT NULL "
            "ORDER BY f.id LIMIT ?",
            (limit,),
        ).fetchall()
        return [
            PendingSuggestion(
                face_id=row["face_id"], file_id=row["file_id"], path=row["path"],
                bbox=(row["bbox_x"], row["bbox_y"], row["bbox_w"], row["bbox_h"]),
                pile_id=row["pile_id"], pile_name=row["pile_name"],
            )
            for row in rows
        ]

    def confirm_suggestion(self, face_id: int, accept: bool, *,
                            confidence: Optional[float] = None) -> None:
        """The yes/no chip. Section 2c. Declining returns the face to the
        unclustered pool rather than anywhere else - the suggestion was
        wrong, not a request to try a different pile."""
        row = self.conn.execute(
            "SELECT file_id, suggested_pile_id FROM faces WHERE id = ?", (face_id,)
        ).fetchone()
        pile_id = row["suggested_pile_id"] if row else None
        with self.write() as conn:
            if accept and pile_id is not None:
                conn.execute(
                    "UPDATE faces SET pile_id = ?, confidence = ?, "
                    "suggested_pile_id = NULL WHERE id = ?",
                    (pile_id, confidence, face_id),
                )
            else:
                conn.execute(
                    "UPDATE faces SET suggested_pile_id = NULL WHERE id = ?",
                    (face_id,),
                )
                # 2026-10-05: and remembered, so the No holds (`face_declines`).
                if pile_id is not None:
                    self._decline(conn, face_id, int(pile_id))
        if row is not None:
            self.sync_people_segment(int(row["file_id"]))

    #: The labels `add_caption_chunk` writes for a picture's description and
    #: the text read from it (`pipeline._drain_photo_tags` / `_drain_picture_text`).
    PHOTO_DESCRIPTION_LABEL = "AI description"
    PHOTO_TEXT_LABEL = "Text read from the image"

    def photo_library(self, extensions: Sequence[str]) -> list[PhotoRow]:
        """Every picture, newest first, for the Photos page. 2026-10-05.

        Three statements merged here rather than one with correlated
        subqueries: measured on the owner's 15,010 pictures, the one-statement
        form took 103 s, these three 0.06 s together."""
        cleaned = sorted({str(e).lstrip(".").lower() for e in extensions if str(e).strip()})
        if not cleaned:
            return []
        marks = ",".join("?" for _ in cleaned)
        try:
            files = self.conn.execute(
                f"SELECT id, path, ext, size_bytes, mtime_ns, taken_at_ns, "
                f"taken_at_is_hint, place, status, skip_code FROM files "
                f"WHERE ext IN ({marks})", cleaned).fetchall()
            people: dict[int, list[str]] = {}
            faces: dict[int, int] = {}
            for file_id, name in self.conn.execute(
                    f"SELECT x.file_id, p.name FROM faces x "
                    f"JOIN files f ON f.id = x.file_id "
                    f"LEFT JOIN piles p ON p.id = x.pile_id "
                    f"WHERE f.ext IN ({marks})", cleaned):
                faces[file_id] = faces.get(file_id, 0) + 1
                if name and name not in people.setdefault(file_id, []):
                    people[file_id].append(name)
            labels: dict[int, set[str]] = {}
            # Index-driven: `files` outer, `chunks` through idx_chunks_file_ord. The
            # plain JOIN with `c.label IN` scanned all 6.5 million chunks on every
            # Photos load - 247 s on the owner's index, 2026-10-09. Same rows, same
            # predicate; CROSS JOIN is what keeps the planner from scanning `chunks`.
            for file_id, label in self.conn.execute(
                    f"SELECT c.file_id, c.label FROM files f CROSS JOIN chunks c "
                    f"WHERE c.file_id = f.id AND f.ext IN ({marks}) AND c.label IN (?, ?)",
                    [*cleaned, self.PHOTO_DESCRIPTION_LABEL, self.PHOTO_TEXT_LABEL]):
                labels.setdefault(file_id, set()).add(label)
            scanned = {int(r[0]) for r in self.conn.execute(
                f"SELECT s.file_id FROM face_scans s JOIN files f ON f.id = s.file_id "
                f"WHERE f.ext IN ({marks})", cleaned)}
            tags: dict[int, list[str]] = {}
            for file_id, tag in self.conn.execute(
                    f"SELECT t.file_id, t.tag FROM file_tags t JOIN files f ON f.id = t.file_id "
                    f"WHERE f.ext IN ({marks})", cleaned):
                tags.setdefault(file_id, []).append(str(tag))
        except sqlite3.OperationalError:
            return []
        rows = []
        for r in files:
            file_id = int(r[0])
            found = labels.get(file_id, ())
            rows.append(PhotoRow(
                file_id=file_id, path=r[1], ext=r[2], size_bytes=int(r[3] or 0),
                mtime_ns=int(r[4] or 0), taken_at_ns=r[5], taken_is_hint=bool(r[6]),
                place=r[7], people=tuple(sorted(people.get(file_id, ()), key=str.casefold)),
                faces=faces.get(file_id, 0),
                described=self.PHOTO_DESCRIPTION_LABEL in found,
                has_text=self.PHOTO_TEXT_LABEL in found or (
                    r[8] == "INDEXED" and file_id in scanned),
                page_like=r[9] == "ERR_PAGE_TEXT_LATER", status=r[8],
                scanned=file_id in scanned, tags=tuple(tags.get(file_id, ()))))
        rows.sort(key=lambda row: row.when_ns, reverse=True)
        return rows

    def note_photo_rewritten(self, file_id: int, size_bytes: int, mtime_ns: int) -> None:
        """"Write names into photos" changed this photo's bytes, not its
        pictures: record the new size so the next run's unchanged check
        (`walker`, size and modified time) does not read it all again."""
        with self.write() as conn:
            conn.execute("UPDATE files SET size_bytes = ?, mtime_ns = ? WHERE id = ?",
                         (int(size_bytes), int(mtime_ns), int(file_id)))

    def photo_metadata_rows(self, file_ids: Optional[Sequence[int]] = None
                            ) -> list[tuple[int, str, tuple[str, ...], str]]:
        """`(file_id, path, people, description)` for every photo with a
        person named or a description - or just `file_ids` - for "Write
        names into photos"."""
        params: list[Any] = []
        where = ""
        if file_ids is not None:
            ids = [int(f) for f in file_ids]
            if not ids:
                return []
            where = f"WHERE f.id IN ({','.join('?' for _ in ids)})"
            params = ids
        names: dict[int, list[str]] = {}
        paths: dict[int, str] = {}
        for file_id, path, name in self.conn.execute(
                f"SELECT f.id, f.path, p.name FROM files f JOIN faces x ON x.file_id = f.id "
                f"JOIN piles p ON p.id = x.pile_id {where} "
                f"{'AND' if where else 'WHERE'} p.name IS NOT NULL", params):
            paths[file_id] = path
            if name not in names.setdefault(file_id, []):
                names[file_id].append(name)
        described: dict[int, str] = {}
        for file_id, path, text in self.conn.execute(
                f"SELECT f.id, f.path, c.text FROM files f JOIN chunks c ON c.file_id = f.id "
                f"{where} {'AND' if where else 'WHERE'} c.label = ?",
                [*params, self.PHOTO_DESCRIPTION_LABEL]):
            paths[file_id] = path
            body = str(text or "")
            prefix = self.PHOTO_DESCRIPTION_LABEL + ":"
            described[file_id] = body[len(prefix):].strip() if body.startswith(prefix) else body
        return [(fid, paths[fid], tuple(sorted(names.get(fid, ()), key=str.casefold)),
                 described.get(fid, "")) for fid in sorted(paths)]

    def photo_details(self, file_id: int) -> dict[str, str]:
        """The description and the text read from one picture, for the
        Photos page's info panel - `{label: text}`, the label prefix removed."""
        out: dict[str, str] = {}
        for label, text in self.conn.execute(
                "SELECT label, text FROM chunks WHERE file_id = ? ORDER BY ordinal",
                (int(file_id),)):
            key = label or "Text"
            body = str(text or "")
            if label and body.startswith(label + ":"):
                body = body[len(label) + 1:].lstrip()
            out[key] = (out[key] + "\n" + body) if key in out else body
        return out

    def suggestion_counts(self) -> list[tuple[int, str, int]]:
        """`(pile_id, name, waiting)` for every named person with suggestions
        waiting, most first - what "Accept all" asks about. 2026-10-05, the
        owner: "need to mass accept names as most cases the system was right"."""
        try:
            rows = self.conn.execute(
                "SELECT p.id, p.name, count(*) FROM faces f "
                "JOIN piles p ON p.id = f.suggested_pile_id "
                "WHERE p.name IS NOT NULL GROUP BY p.id ORDER BY count(*) DESC, p.name"
            ).fetchall()
        except sqlite3.OperationalError:
            return []
        return [(int(r[0]), str(r[1]), int(r[2])) for r in rows]

    def accept_all_suggestions(self, pile_id: Optional[int] = None) -> int:
        """Every waiting "Is this ...?" answered Yes - for one person, or for
        everyone named - in one write; returns how many faces were filed. Each
        photo touched has its `People:` line rebuilt once, as a single Yes does."""
        where = "f.suggested_pile_id IS NOT NULL AND p.name IS NOT NULL"
        args: list[Any] = []
        if pile_id is not None:
            where += " AND f.suggested_pile_id = ?"
            args.append(int(pile_id))
        rows = self.conn.execute(
            f"SELECT f.id, f.file_id, f.suggested_pile_id FROM faces f "
            f"JOIN piles p ON p.id = f.suggested_pile_id WHERE {where}", args).fetchall()
        if not rows:
            return 0
        with self.write() as conn:
            conn.executemany(
                "UPDATE faces SET pile_id = ?, confidence = NULL, "
                "suggested_pile_id = NULL WHERE id = ?",
                [(int(r[2]), int(r[0])) for r in rows])
        for file_id in sorted({int(r[1]) for r in rows}):
            self.sync_people_segment(file_id)
        return len(rows)

    @staticmethod
    def _decline(conn: sqlite3.Connection, face_id: int, pile_id: int) -> None:
        try:
            conn.execute("INSERT OR IGNORE INTO face_declines (face_id, pile_id) "
                         "VALUES (?, ?)", (int(face_id), int(pile_id)))
        except sqlite3.OperationalError:            # an index from before v34
            pass

    def declined_piles(self, face_ids: Sequence[int]) -> dict[int, set[int]]:
        """`{face_id: {pile_id, ...}}` - the people each face was told it is
        not, for grouping to leave alone. 2026-10-05."""
        ids = [int(f) for f in face_ids]
        if not ids:
            return {}
        marks = ",".join("?" for _ in ids)
        try:
            rows = self.conn.execute(
                f"SELECT face_id, pile_id FROM face_declines WHERE face_id IN ({marks})",
                ids).fetchall()
        except sqlite3.OperationalError:
            return {}
        out: dict[int, set[int]] = {}
        for face_id, pile_id in rows:
            out.setdefault(int(face_id), set()).add(int(pile_id))
        return out

    def not_this_person(self, face_id: int) -> None:
        """The manage dialog's "Not this person": out of its group, back to the
        unsorted faces, and never filed or suggested there again."""
        row = self.conn.execute("SELECT pile_id FROM faces WHERE id = ?",
                                (int(face_id),)).fetchone()
        pile_id = row["pile_id"] if row else None
        self.remove_face_from_pile(face_id)
        if pile_id is not None:
            with self.write() as conn:
                self._decline(conn, face_id, int(pile_id))

    def faces_in_pile(self, pile_id: int) -> list[tuple[int, str, tuple]]:
        """`(face_id, photo path, bbox)` for every face in a group, for the
        manage dialog - read on a worker. 2026-10-05."""
        rows = self.conn.execute(
            "SELECT f.id, files.path, f.bbox_x, f.bbox_y, f.bbox_w, f.bbox_h "
            "FROM faces f JOIN files ON files.id = f.file_id "
            "WHERE f.pile_id = ? ORDER BY f.id", (int(pile_id),)).fetchall()
        return [(int(r[0]), str(r[1]), (r[2], r[3], r[4], r[5])) for r in rows]

    def remove_face_from_pile(self, face_id: int) -> None:
        """Section 2b's "remove-from-pile" - back to the unclustered pool,
        not deleted. Re-clustering (section 1b) may put it straight back;
        that is a correct re-decision, not a bug."""
        row = self.conn.execute(
            "SELECT file_id FROM faces WHERE id = ?", (face_id,)).fetchone()
        with self.write() as conn:
            conn.execute(
                "UPDATE faces SET pile_id = NULL, confidence = NULL, "
                "suggested_pile_id = NULL WHERE id = ?",
                (face_id,),
            )
        if row is not None:
            self.sync_people_segment(int(row["file_id"]))

    def _files_with_pile(self, pile_id: int) -> list[int]:
        rows = self.conn.execute(
            "SELECT DISTINCT file_id FROM faces WHERE pile_id = ?", (pile_id,)
        ).fetchall()
        return [int(r["file_id"]) for r in rows]

    def rename_pile(self, pile_id: int, name: Optional[str]) -> None:
        """Section 2a's "click -> name it". `name=None` un-names it - used by
        `forget_person` below rather than duplicating this UPDATE. Every
        file this pile's faces belong to gets its `People:` segment
        rebuilt - a rename changes what every one of those files should say."""
        cleaned = (name or "").strip() or None
        # 2026-10-05, the owner: "there are two sets both are jason they need
        # to be merged". Names are unique (`idx_piles_name`), so naming a
        # second group after a person who already has one raised
        # `UNIQUE constraint failed` and the page said "Could not rename". The
        # page now asks first (`PhotoTaggerPage._rename`); if the name is taken
        # by the time this runs, the two are combined rather than refused.
        taken = self.pile_id_named(cleaned, exclude=pile_id) if cleaned else None
        if taken is not None:
            self.combine_piles(pile_id, taken)
            return
        affected = self._files_with_pile(pile_id)
        with self.write() as conn:
            conn.execute("UPDATE piles SET name = ? WHERE id = ?", (cleaned, pile_id))
        for file_id in affected:
            self.sync_people_segment(file_id)

    def pile_id_named(self, name: Optional[str], *, exclude: Optional[int] = None
                      ) -> Optional[int]:
        """The group already called `name` (any case), other than `exclude`."""
        cleaned = (name or "").strip()
        if not cleaned:
            return None
        row = self.conn.execute(
            "SELECT id FROM piles WHERE name = ? COLLATE NOCASE AND id IS NOT ? LIMIT 1",
            (cleaned, exclude)).fetchone()
        return int(row[0]) if row else None

    def combine_piles(self, source_id: int, target_id: int) -> int:
        r"""Section 2b's drag-pile-onto-pile. Every face `source_id` owns -
        assigned or only suggested - moves to `target_id`, then the now-empty
        source pile is deleted. Returns how many faces moved.

        **Deliberately silent about names.** Combining a named pile into an
        unnamed one, or two named piles into each other, is a real choice a
        person makes by which pile they dragged onto which - this method
        does exactly what it is told and does not guess which name should
        win. The caller (the Photo Tagger page) decides that from which
        pile the person dropped onto.
        """
        if source_id == target_id:
            return 0
        affected = self._files_with_pile(source_id)
        with self.write() as conn:
            moved = conn.execute(
                "UPDATE faces SET pile_id = ? WHERE pile_id = ?",
                (target_id, source_id),
            ).rowcount
            conn.execute(
                "UPDATE faces SET suggested_pile_id = ? WHERE suggested_pile_id = ?",
                (target_id, source_id),
            )
            conn.execute("DELETE FROM piles WHERE id = ?", (source_id,))
        for file_id in affected:
            self.sync_people_segment(file_id)
        return int(moved)

    def split_pile(self, face_ids: Sequence[int],
                    *, new_name: Optional[str] = None) -> Optional[int]:
        """Section 2b's "split a mixed pile" - the chosen faces become a
        fresh pile of their own. Returns the new pile id, or `None` for an
        empty selection (nothing to split)."""
        ids = [int(i) for i in face_ids]
        if not ids:
            return None
        placeholders = ",".join("?" for _ in ids)
        rows = self.conn.execute(
            f"SELECT DISTINCT file_id FROM faces WHERE id IN ({placeholders})", ids
        ).fetchall()
        affected = [int(r["file_id"]) for r in rows]
        with self.write() as conn:
            cursor = conn.execute(
                "INSERT INTO piles (name, created_at) VALUES (?, ?)",
                (new_name, int(time.time())),
            )
            new_id = int(cursor.lastrowid)
            conn.execute(
                f"UPDATE faces SET pile_id = ?, suggested_pile_id = NULL "
                f"WHERE id IN ({placeholders})",
                (new_id, *ids),
            )
        for file_id in affected:
            self.sync_people_segment(file_id)
        return new_id

    def forget_person(self, pile_id: int, *, delete_faces: bool = False) -> None:
        r"""Section 2e. Always removes the name - that half is never optional,
        because a name kept anywhere after "Forget" is the guardrail broken.

        `delete_faces=False` (the default): the pile keeps its faces, just
        anonymously - it goes back to being an unnamed pile, exactly as if
        clustering had found it and nobody had named it yet. `True` also
        deletes every face row that points at it (assigned or suggested) and
        the pile itself - "the face data" the guardrails' own wording names
        as the second, explicit thing "Forget this person" may remove. The
        two are separate confirmations in the UI (section 2e); this method
        does exactly what it is asked, nothing inferred.
        """
        affected = self._files_with_pile(pile_id)
        with self.write() as conn:
            if delete_faces:
                conn.execute(
                    "DELETE FROM faces WHERE pile_id = ? OR suggested_pile_id = ?",
                    (pile_id, pile_id),
                )
                conn.execute("DELETE FROM piles WHERE id = ?", (pile_id,))
            else:
                conn.execute("UPDATE piles SET name = NULL WHERE id = ?", (pile_id,))
        for file_id in affected:
            self.sync_people_segment(file_id)

    def piles_with_counts(self, *, limit: int = 200,
                           samples: int = 4) -> list[PileRecord]:
        """Section 2a's grid: every pile, biggest first, with a few sample
        file ids the UI draws crops from. `samples` per pile, not the whole
        set - the grid shows a handful of thumbnails, not every photo."""
        rows = self.conn.execute(
            "SELECT p.id, p.name, COUNT(f.id) AS n FROM piles p "
            "JOIN faces f ON f.pile_id = p.id "
            "GROUP BY p.id, p.name ORDER BY n DESC, p.id LIMIT ?",
            (limit,),
        ).fetchall()
        out: list[PileRecord] = []
        for row in rows:
            sample_rows = self.conn.execute(
                "SELECT f.bbox_x, f.bbox_y, f.bbox_w, f.bbox_h, "
                "       fl.id AS file_id, fl.path AS path "
                "FROM faces f JOIN files fl ON fl.id = f.file_id "
                "WHERE f.pile_id = ? ORDER BY f.id LIMIT ?",
                (row["id"], samples),
            ).fetchall()
            out.append(PileRecord(
                id=row["id"], name=row["name"], face_count=row["n"],
                samples=tuple(
                    PileSample(
                        file_id=r["file_id"], path=r["path"],
                        bbox=(r["bbox_x"], r["bbox_y"], r["bbox_w"], r["bbox_h"]),
                    )
                    for r in sample_rows
                ),
            ))
        return out

    def sync_people_segment(self, file_id: int, *, label: str = "People") -> None:
        r"""Rebuild this file's `People:` labelled segment. Section 3a.

        Called after any assignment change that could touch this file's set
        of *named* people (`assign_face`, `confirm_suggestion`, a rename, a
        combine, a forget) - never a store trigger, because only the caller
        knows a change just happened and a chunk-table trigger recomputing
        an aggregate on every unrelated write would be the kind of hidden
        cost this project measures against, not assumes acceptable.

        **Replace, not append** - unlike `add_caption_chunk`, which only
        ever grows: the set of named people showing in one photo can shrink
        (a rename to nothing, a forget) as well as grow, so the old segment
        must go before the new one is written, in the same transaction.
        """
        with self.write() as conn:
            names = [
                r["name"] for r in conn.execute(
                    "SELECT DISTINCT p.name FROM faces f "
                    "JOIN piles p ON p.id = f.pile_id "
                    "WHERE f.file_id = ? AND p.name IS NOT NULL "
                    "ORDER BY p.name", (file_id,),
                ).fetchall()
            ]
            conn.execute(
                "DELETE FROM chunks WHERE file_id = ? AND label = ?",
                (file_id, label),
            )
            if names:
                text = f"{label}: " + ", ".join(names)
                row = conn.execute(
                    "SELECT COALESCE(MAX(ordinal), -1) FROM chunks WHERE file_id = ?",
                    (file_id,),
                ).fetchone()
                next_ordinal = int(row[0]) + 1
                conn.execute(
                    """
                    INSERT INTO chunks (file_id, ordinal, text, symbols,
                                        char_start, char_end, page, label,
                                        embedded)
                    VALUES (?, ?, ?, ?, NULL, NULL, NULL, ?, 0)
                    """,
                    (file_id, next_ordinal, text, symbol_tokens(text), label),
                )
            self._bump_generation(conn)

    def apply_batch_era(self, path_prefix: str, taken_at_ns: int) -> int:
        r"""Section 2d's batch-era control - "these are roughly 1998-2002".

        Only ever overrides an existing **hint** (`taken_at_is_hint = 1`) or
        a row with no date at all - never a fact EXIF already wrote, which
        stays ranked above every hint per 0511 section 4b's own ordering.
        Scoped by a path prefix (a folder or a batch of scans share one),
        not by file id one at a time - a person selects a folder, not four
        hundred individual rows. Returns how many files changed.

        **2026-10-04, code review: the folder, not a prefix of its name.** It
        was `path LIKE '<folder>%'` with the folder as Qt's dialog gives it -
        `D:/Photos/Scans`, forward slashes - against paths stored with
        backslashes, so on Windows it changed nothing; and `Scans` also
        matched `Scans2\\`. It also read the whole table under the write lock
        (`LIKE` ignores case and the index on `path` does not). The folder is
        now put in the platform's own form and matched as `file_ids_at_or_under`
        matches one - "starts with `folder\\`" as a range the index answers.
        """
        import os

        folder = os.path.normpath(str(path_prefix or "").strip()).rstrip("\\/")
        if not folder or folder == ".":
            return 0
        changed = 0
        with self.write() as conn:
            for separator in ("\\", "/"):
                cursor = conn.execute(
                    "UPDATE files SET taken_at_ns = ?, taken_at_is_hint = 1 "
                    "WHERE path >= ? AND path < ? "
                    "AND (taken_at_is_hint = 1 OR taken_at_ns IS NULL)",
                    (taken_at_ns, folder + separator, folder + chr(ord(separator) + 1)),
                )
                changed += max(0, int(cursor.rowcount))
            if changed:
                self._bump_generation(conn)
        return changed

    def iter_photos_without_face_scan(
        self, extensions: Sequence[str], *, batch_size: int = 16,
    ) -> Iterator[list[tuple[int, str]]]:
        r"""`(file_id, path)` for indexed photos `faces` has never seen.

        Work order 0j section 1a's own "Backfill for already-indexed images
        runs as an enrichment-backlog job kind" - the face-detection half.
        `NOT EXISTS` against `faces`, filtered in SQL - the same reasoning
        `iter_uncaptioned_images` already gives for its own identical shape.
        A photo that has been through the scan and genuinely has no face in
        it never appears here again: this asks "have we looked", not "did
        we find one" - the second question has no wrong answer to retry.
        """
        # Normalised here for the same reason `iter_uncaptioned_images`
        # normalises - see that method's own comment.
        cleaned = [str(ext).lstrip(".").lower() for ext in extensions if str(ext).strip()]
        if not cleaned:
            return
        placeholders = ",".join("?" for _ in cleaned)
        last_id = 0
        while True:
            rows = self.conn.execute(
                f"""
                SELECT f.id, f.path FROM files f
                WHERE f.id > ? AND f.status = 'INDEXED' AND f.ext IN ({placeholders})
                  AND NOT EXISTS (SELECT 1 FROM face_scans WHERE face_scans.file_id = f.id)
                ORDER BY f.id LIMIT ?
                """,
                (last_id, *cleaned, batch_size),
            ).fetchall()
            if not rows:
                return
            batch = [(int(r["id"]), str(r["path"])) for r in rows]
            last_id = batch[-1][0]
            yield batch

    def mark_all_unembedded(self) -> int:
        """Put every chunk back in the embedding queue. Returns how many.

        For `app.cli reembed --all`, after the vector table has been dropped.
        Touches no text and re-reads no document: the chunks stay exactly as they
        are, only the flag saying a vector exists for them is cleared. That is
        the whole point of SQLite being the authority - the derived store can be
        thrown away and rebuilt without going near the corpus.
        """
        with self.write() as conn:
            cursor = conn.execute("UPDATE chunks SET embedded = 0 WHERE embedded = 1")
            return int(cursor.rowcount)

    def mark_embedded(self, chunk_ids: Iterable[int]) -> None:
        """These chunks now have a vector. One transaction for the batch.

        Called after `VectorStore.add` returns, never before: a crash between
        the two leaves a chunk unembedded (re-done next run) rather than a flag
        claiming a vector that was never written.
        """
        ids = [(int(i),) for i in chunk_ids]
        if not ids:
            return
        with self.write() as conn:
            conn.executemany("UPDATE chunks SET embedded = 1 WHERE id = ?", ids)

    #: 2026-10-08. `chunks.embedded` for a passage found by its words only, on
    #: purpose (a spreadsheet, `pipeline.KEYWORD_ONLY_EXTS`). Not 0, which every
    #: repair reads as "lost its vector, fill it in"; not 1, which says a vector
    #: exists. `mark_all_unembedded` leaves it alone for the same reason.
    KEYWORD_ONLY = 2

    def mark_keyword_only(self, chunk_ids: Iterable[int]) -> None:
        ids = [(int(i),) for i in chunk_ids]
        if not ids:
            return
        with self.write() as conn:
            conn.executemany(
                f"UPDATE chunks SET embedded = {self.KEYWORD_ONLY} WHERE id = ?", ids)

    def reset_keyword_only(self) -> int:
        """Put keyword-only passages in the embedding queue. Returns how many.

        For "Find spreadsheets by meaning" switched back on: the run's start
        repair (`Pipeline._drain_unembedded`) then gives them vectors without
        reading a single spreadsheet again.
        """
        with self.write() as conn:
            cursor = conn.execute(
                f"UPDATE chunks SET embedded = 0 WHERE embedded = {self.KEYWORD_ONLY}")
            return int(cursor.rowcount)

    def has_embedded_chunks(self, file_id: int) -> bool:
        """Does this file have any passage with a vector? One indexed lookup."""
        return self.conn.execute(
            "SELECT 1 FROM chunks WHERE file_id = ? AND embedded = 1 LIMIT 1",
            (int(file_id),),
        ).fetchone() is not None

    # -- messages ------------------------------------------------------------

    def set_message(self, file_id: int, **fields: Any) -> None:
        """Attach email metadata to a file row."""
        columns = ("store_path", "entry_id", "conversation", "subject",
                   "sender", "recipients", "sent_at", "has_attach",
                   # How much of the body was a quoted reply or signature. NULL
                   # for a message indexed before schema v12, which the preview
                   # reads as "not known" rather than as zero - see
                   # `migrations._v12_quoted_removed`.
                   "quoted_removed",
                   # **Folded copies, written here and nowhere else.** SQLite's
                   # LIKE and its `lower()` are both ASCII-only, so `from:josé`
                   # could never match `JOSÉ@…` however the query was written -
                   # while the FTS half of the same search folded it correctly
                   # and disagreed. Python's `str.lower()` does fold Unicode;
                   # doing it once at write time is the only place it can be
                   # done. See `migrations._v14_folded_mail_columns`.
                   "sender_lc", "recipients_lc", "subject_lc",
                   # Schema 32: where it sits in its archive (`pst_attachment`).
                   "folder_path", "folder_index")
        # has_attach is NOT NULL DEFAULT 0, so it cannot be passed through as
        # None when the caller omits it.
        defaults: dict[str, Any] = {"has_attach": 0}
        # Derived, never passed in: a caller that supplied its own fold would
        # be free to disagree with the one the filters assume.
        for source, folded in (("sender", "sender_lc"),
                               ("recipients", "recipients_lc"),
                               ("subject", "subject_lc")):
            value = fields.get(source)
            fields[folded] = str(value).lower() if value is not None else None
        values = [
            fields.get(name, defaults.get(name)) if fields.get(name) is not None
            else defaults.get(name)
            for name in columns
        ]
        # 2026-09-30: inside a `batch()` a message with no row yet has its
        # header-index row written at the end of the batch, not by
        # `messages_ai` - the same change as `replace_chunks`, for the same
        # reason (`_deferred`). A message that already has a row is updated
        # with the triggers on, as it always was. Where `messages_fts` could
        # not be built there are no mail triggers and nothing to write.
        with self.write(deferring=True) as conn:
            state = self._deferred() if self._has_message_index() else None
            if state is not None:
                if (file_id in state.messages
                        or conn.execute(
                            "SELECT 1 FROM messages WHERE file_id = ?", (file_id,)
                        ).fetchone() is not None):
                    # Already has a row: `messages_au` must see this update.
                    # 2026-10-10 (schema v36): it still does. The trigger now
                    # fires only for `UPDATE OF subject, sender, recipients`,
                    # and the upsert below names all three in `DO UPDATE SET`
                    # - SQLite fires an `UPDATE OF` trigger for a column the
                    # SET names, whether or not its value changed.
                    self._finish_deferred(conn)
                    state = None
                else:
                    self._switch_triggers(conn, state, on=False)
            conn.execute(
                f"INSERT INTO messages (file_id, {', '.join(columns)}) "
                f"VALUES (?, {', '.join('?' * len(columns))}) "
                f"ON CONFLICT(file_id) DO UPDATE SET "
                + ", ".join(f"{c} = excluded.{c}" for c in columns),
                (file_id, *values),
            )
            if state is not None:
                bound = dict(zip(columns, values))
                # What `messages_ai` would have been handed for this row.
                state.messages[file_id] = (
                    bound["subject"], bound["sender"], bound["recipients"])

    def known_read_stamps(self, store_path: str) -> dict[str, str]:
        """`{entry_id: read_stamp}` for the indexed messages of one archive
        that were read to the end before. Schema 35; see its migration."""
        rows = self.conn.execute(
            "SELECT m.entry_id, m.read_stamp FROM messages m "
            "JOIN files f ON f.id = m.file_id "
            "WHERE m.store_path = ? AND m.read_stamp IS NOT NULL "
            "AND m.entry_id IS NOT NULL AND f.status = 'INDEXED'",
            (str(store_path),)).fetchall()
        return {str(row["entry_id"]): str(row["read_stamp"]) for row in rows}

    def set_read_stamps(self, stamps: Iterable[tuple[str, str]]) -> int:
        """Keep `(message key, stamp)` pairs, in one transaction. Returns how
        many. Written when an archive has been read to its end, never before:
        a stamp says "this message and its attachments are in the index"."""
        pairs = [(str(stamp), str(key)) for key, stamp in stamps if key and stamp]
        if not pairs:
            return 0
        with self.write() as conn:
            conn.executemany(
                "UPDATE messages SET read_stamp = ? "
                "WHERE file_id = (SELECT id FROM files WHERE path = ?)", pairs)
        return len(pairs)

    def get_message(self, file_id: int) -> Optional[dict[str, Any]]:
        """The mail metadata of one file, every column, or None for a file
        that is not a message."""
        row = self.conn.execute("SELECT * FROM messages WHERE file_id = ?", (file_id,)).fetchone()
        return dict(row) if row else None

    # -- keyword search ------------------------------------------------------

    def search_bm25(self, query: str, limit: int = 100) -> list[dict[str, Any]]:
        """FTS5 BM25 search. Layer 4 builds the full pipeline on top of this.

        A malformed query returns no results rather than raising: users type
        unbalanced quotes constantly, and a search box must not explode.
        """
        try:
            rows = self.conn.execute(
                """
                SELECT c.id AS chunk_id, c.file_id, c.text, c.page,
                       f.path, bm25(chunks_fts) AS score
                FROM chunks_fts
                JOIN chunks c ON c.id = chunks_fts.rowid
                JOIN files  f ON f.id = c.file_id
                WHERE chunks_fts MATCH ?
                ORDER BY score
                LIMIT ?
                """,
                (query, limit),
            ).fetchall()
        except sqlite3.OperationalError:
            return []
        return [dict(row) for row in rows]

    # -- usage logging (schema v2) -------------------------------------------

    def log_search(
        self,
        query: str,
        *,
        filters: Optional[str] = None,
        hits: int = 0,
        elapsed_ms: int = 0,
        rerank_on: bool = False,
    ) -> int:
        """Record one search. Returns its id, for attaching hits and opens.

        Local only, and never on the critical path: a failure to log must never
        fail a search, which is why the caller wraps this rather than the other
        way round.
        """
        with self.write() as conn:
            cursor = conn.execute(
                """
                INSERT INTO searches (query, filters, hits, elapsed_ms, rerank_on, searched_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (query, filters, int(hits), int(elapsed_ms), int(rerank_on), int(time.time())),
            )
            return int(cursor.lastrowid)

    def log_hits(self, search_id: int, hits: Sequence[dict[str, Any]]) -> None:
        """Record what came back and in what order. `rank` is 1-based."""
        if not hits:
            return
        with self.write() as conn:
            conn.executemany(
                "INSERT INTO search_hits (search_id, chunk_id, rank, sources) VALUES (?, ?, ?, ?)",
                [
                    (search_id, int(hit["chunk_id"]), index, str(hit.get("sources", "")))
                    for index, hit in enumerate(hits, start=1)
                ],
            )

    def open_counts(self, chunk_ids: Iterable[int]) -> dict:
        r"""How many times each of these passages has been opened. Never raises.

        **One query for the whole page, and it rides the partial index.**
        `idx_hits_opened` covers `opened = 1` only, and opened rows are a tiny
        fraction of `search_hits` - fifty hits are logged per search and
        approximately one is opened - so this visits almost nothing. Asking
        per row would be fifty statements against a table that grows with
        every search anybody has ever run, which is the shape of the keyword
        path this project already had to fix once.

        Missing keys mean zero: **"never opened" is the ordinary state** and
        does not deserve a row.
        """
        wanted = [int(chunk_id) for chunk_id in chunk_ids or ()]
        if not wanted:
            return {}
        found: dict = {}
        # Chunked, because SQLite's parameter limit is 999 by default and a
        # caller with a long result list should get an answer rather than an
        # exception about a limit they have never heard of.
        for start in range(0, len(wanted), 500):
            batch = wanted[start:start + 500]
            marks = ",".join("?" * len(batch))
            try:
                rows = self.conn.execute(
                    f"SELECT chunk_id, COUNT(*) FROM search_hits "
                    f"WHERE opened = 1 AND chunk_id IN ({marks}) "
                    f"GROUP BY chunk_id", batch).fetchall()
            except Exception as exc:               # noqa: BLE001 - a courtesy
                _log.debug("could not count opens: {}", exc)
                return found
            for row in rows:
                found[int(row[0])] = int(row[1])
        return found

    def mark_opened(self, search_id: int, chunk_id: int) -> None:
        """The single most valuable signal in the system: this one was useful."""
        with self.write() as conn:
            conn.execute(
                "UPDATE search_hits SET opened = 1, opened_at = ? "
                "WHERE search_id = ? AND chunk_id = ?",
                (int(time.time()), search_id, chunk_id),
            )

    def recent_searches(self, limit: int = 50) -> list[dict[str, Any]]:
        """The last `limit` searches, newest first - what the empty search box
        offers (`SEARCH_OFFER_RECENT`)."""
        rows = self.conn.execute(
            "SELECT * FROM searches ORDER BY searched_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(row) for row in rows]

    # -- saved searches (Adoptions §3) ---------------------------------------

    def save_search(self, name: str, query: str, scope: str = "all") -> bool:
        r"""Store a named search, replacing one of the same name. False if empty.

        **Replaces rather than refuses.** Saving over a name is what somebody
        means when they type one they already used - the alternative is an
        error message about a name they can see on the list in front of them.
        The run count survives the replacement, because it is a fact about how
        often they reach for that search and not about the text of it.

        Names are folded and compared by `saved.name_key`, so `Leeds` and
        `leeds` are one saved search rather than two nobody can tell apart.
        """
        from app.search.saved import clean_name, name_key

        cleaned = clean_name(name)
        text = str(query or "").strip()
        if not cleaned or not text:
            return False
        with self.write() as conn:
            conn.execute(
                "INSERT INTO saved_searches "
                "  (name, name_lc, query, scope, created_at, run_count) "
                "VALUES (?, ?, ?, ?, ?, 0) "
                "ON CONFLICT(name_lc) DO UPDATE SET "
                "  name = excluded.name, query = excluded.query, "
                "  scope = excluded.scope",
                (cleaned, name_key(cleaned), text, str(scope or "all"),
                 int(time.time())),
            )
        return True

    def saved_searches(self, limit: int = 100) -> list[dict[str, Any]]:
        """Every saved search, the ones actually used first.

        Ordered by run count rather than by name or by age: the list is read
        at a glance and the one somebody wants is nearly always one of the two
        or three they run every week. Ties break alphabetically, so the order
        is stable rather than whatever the table happens to return.
        """
        rows = self.conn.execute(
            "SELECT name, query, scope, run_count, created_at, last_run_at "
            "FROM saved_searches ORDER BY run_count DESC, name COLLATE NOCASE "
            "LIMIT ?", (max(1, int(limit)),)
        ).fetchall()
        return [dict(row) for row in rows]

    def saved_search(self, name: str) -> Optional[dict[str, Any]]:
        """One saved search by name, or None. Case-insensitive, Unicode-aware."""
        from app.search.saved import name_key

        key = name_key(name)
        if not key:
            return None
        row = self.conn.execute(
            "SELECT name, query, scope, run_count, created_at, last_run_at "
            "FROM saved_searches WHERE name_lc = ?", (key,)).fetchone()
        return dict(row) if row else None

    def rename_saved_search(self, old: str, new: str) -> bool:
        """Rename one. False if it does not exist, or the new name is taken.

        **False rather than an exception**, and rather than silently merging
        two saved searches into one: the caller is a dialog with a name box,
        and "that name is already used" is something it can say.
        """
        from app.search.saved import clean_name, name_key

        cleaned = clean_name(new)
        if not cleaned or self.saved_search(old) is None:
            return False
        key = name_key(cleaned)
        if key != name_key(old) and self.saved_search(cleaned) is not None:
            return False
        with self.write() as conn:
            conn.execute(
                "UPDATE saved_searches SET name = ?, name_lc = ? "
                "WHERE name_lc = ?", (cleaned, key, name_key(old)))
        return True

    def delete_saved_search(self, name: str) -> bool:
        """Forget one. False if there was nothing by that name."""
        from app.search.saved import name_key

        key = name_key(name)
        if not key:
            return False
        with self.write() as conn:
            cursor = conn.execute(
                "DELETE FROM saved_searches WHERE name_lc = ?", (key,))
        return bool(cursor.rowcount)

    def note_saved_search_run(self, name: str) -> None:
        """Record that this one was just run. **Never raises.**

        The count is what orders the menu, so it has to be written - but it is
        a convenience, and a locked database during an index run must cost the
        ordering of a list rather than the search somebody just asked for.
        """
        from app.search.saved import name_key

        key = name_key(name)
        if not key:
            return
        try:
            with self.write() as conn:
                conn.execute(
                    "UPDATE saved_searches SET run_count = run_count + 1, "
                    "last_run_at = ? WHERE name_lc = ?", (int(time.time()), key))
        except sqlite3.Error as exc:
            _log.debug("could not record a saved-search run: {}", exc)

    # -- chat sessions (work order 202626270611, 3d) ---------------------------

    def save_session(self, session_id: Optional[int], title: str,
                     turns: Sequence[dict[str, Any]], shelf: Sequence[dict[str, Any]] = (),
                     model: str = "") -> int:
        r"""Insert or update one Chat conversation. Returns its id.

        `turns` and `shelf` are lists of plain JSON-able dicts - `app.chat.
        sessions` converts to and from `ChatTurn`/`Receipt`, so this layer knows
        nothing about chat. `session_id=None` makes a new one; an id that no
        longer exists (deleted in another window) also makes a new one rather
        than raising, because losing a conversation to a stale id is worse than
        keeping a duplicate.
        """
        import json

        now = int(time.time())
        cleaned = " ".join(str(title or "").split())[:120] or "New conversation"
        payload = (cleaned, str(model or ""), json.dumps(list(turns), ensure_ascii=False),
                   json.dumps(list(shelf), ensure_ascii=False))
        with self.write() as conn:
            if session_id is not None:
                cursor = conn.execute(
                    "UPDATE chat_sessions SET title = ?, model = ?, turns_json = ?, "
                    "shelf_json = ?, updated_at = ? WHERE id = ?",
                    (*payload, now, int(session_id)))
                if cursor.rowcount:
                    return int(session_id)
            cursor = conn.execute(
                "INSERT INTO chat_sessions "
                "  (title, model, turns_json, shelf_json, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)", (*payload, now, now))
            return int(cursor.lastrowid)

    def load_sessions(self, *, with_turns: bool = True) -> list[dict[str, Any]]:
        """Every saved conversation, most recently used first.

        `with_turns=False` leaves `turns` and `shelf` empty - for a sidebar that
        lists titles and dates and should not parse every conversation ever had.
        A row whose JSON is damaged comes back with empty turns rather than
        raising: one bad session never hides the others.
        """
        import json

        out: list[dict[str, Any]] = []
        for row in self.conn.execute(
                "SELECT id, title, model, turns_json, shelf_json, created_at, updated_at "
                "FROM chat_sessions ORDER BY updated_at DESC, id DESC"):
            turns: list = []
            shelf: list = []
            if with_turns:
                try:
                    turns = json.loads(row["turns_json"] or "[]")
                    shelf = json.loads(row["shelf_json"] or "[]")
                except ValueError:
                    turns, shelf = [], []
            out.append({"id": int(row["id"]), "title": row["title"], "model": row["model"],
                        "turns": turns, "shelf": shelf,
                        "created_at": int(row["created_at"]),
                        "updated_at": int(row["updated_at"])})
        return out

    def delete_session(self, session_id: int) -> bool:
        """Delete one conversation. False when there was none by that id."""
        with self.write() as conn:
            cursor = conn.execute("DELETE FROM chat_sessions WHERE id = ?", (int(session_id),))
        return bool(cursor.rowcount)

    @staticmethod
    def _suspend_content_triggers(conn: sqlite3.Connection) -> list[str]:
        """Drop the FTS content triggers, returning the SQL that recreates them.

        Only the triggers that mirror a row into an external-content FTS table
        are touched, identified by what they are named after rather than by a
        list kept here.
        """
        rows = conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type = 'trigger' "
            "AND (name LIKE 'chunks_a%' OR name LIKE 'messages_a%')"
        ).fetchall()
        restore = [row[1] for row in rows if row[1]]
        for row in rows:
            conn.execute(f"DROP TRIGGER IF EXISTS {row[0]}")
        return restore

    def clear_index(self) -> int:
        """Delete everything indexed. Returns how many documents were removed.

        **Never touches a document on disk.** The index is derived from the
        corpus and is rebuilt by pointing the indexer at the same folders again;
        the only cost of this is the time to do that.

        **Settings survive.** `index_state` holds the folders to index, the
        schedule, the theme and the resource ceilings alongside the indexing
        cursors - so the cursors go and the choices stay. A reset that also
        forgot which folders to index would be a reset nobody could recover
        from without setting the application up again.

        One transaction, so a crash half-way leaves either the old index or no
        index, never a half-deleted one that reports success.
        """
        with self.write() as conn:
            count = int(conn.execute("SELECT COUNT(*) FROM files").fetchone()[0])
            # `files` cascades to chunks and messages, and their triggers clear
            # `chunks_fts` and `messages_fts` - both are external-content tables
            # with `content=`, so that is genuinely automatic. The entity tables
            # are deprecated and empty but are cleared anyway so a reset means
            # what it says.
            #
            # **`files_fts` and `repos` are here because nothing else removes
            # them.** `files_fts` is a *standalone* FTS table - no `content=`,
            # no triggers - so nothing cascades into it, which `delete_file`
            # already documents and handles by hand. `repos` has no owner at
            # all: no cascade, no pruning, and no command to forget one. Both
            # were added to the schema (v4 and v6), wired into writes, and not
            # added to this list, which is the failure this loop keeps having.
            #
            # Reported by the owner as a rule: *"when an index is reset all data
            # must be reset, i.e. all db with nothing."* The surviving `repos`
            # rows were also why a reset did not clear a wrong repository
            # attribution, which had been recorded as "the only route back" when
            # in fact there was none.
            #
            # `test_a_reset_leaves_every_table_empty` enumerates `sqlite_master`
            # rather than repeating this list, so the next table to be added is
            # covered without anybody remembering to come back here.
            #
            # **The content triggers are lifted for the duration.** Deleting a
            # row from `chunks` fires `chunks_ad`, which writes one `'delete'`
            # command into `chunks_fts` carrying that row's whole text back -
            # so emptying an index of ten million chunks meant ten million
            # single-row FTS deletions to reach a table that is about to be
            # empty anyway. `'delete-all'` is the operation FTS5 provides for
            # exactly this, and it does not care how many rows there were.
            #
            # The trigger SQL is read back from `sqlite_master` and replayed
            # verbatim rather than restated here: a second copy of the DDL would
            # be one more thing that can fall behind the schema, and getting it
            # subtly wrong would leave searches quietly missing new documents.
            restore = self._suspend_content_triggers(conn)
            try:
                for table in ("entity_mentions", "entity_edges", "entities",
                              "search_hits", "searches", "files",
                              "files_fts", "repos", "image_hashes"):
                    try:
                        conn.execute(f"DELETE FROM {table}")
                    except sqlite3.OperationalError:
                        # A table that does not exist in this schema version is
                        # not an error: there is nothing in it to delete.
                        continue
                for fts in ("chunks_fts", "messages_fts"):
                    try:
                        conn.execute(
                            f"INSERT INTO {fts}({fts}) VALUES('delete-all')")
                    except sqlite3.OperationalError:
                        continue
                # 2026-10-04: `DELETE FROM` a standalone FTS5 table leaves its
                # segments and the delete markers that cancel them; only a merge
                # removes either. The owner's index, reset, held 6.9 MB of
                # nothing here - 93% of the file. `'delete-all'` is refused on
                # a table with its own content, so the merge does it.
                try:
                    conn.execute("INSERT INTO files_fts(files_fts) VALUES('optimize')")
                except sqlite3.OperationalError:
                    pass
            finally:
                # In the same transaction as the delete: a rollback that left
                # the triggers off would give a database that indexes nothing
                # it is told about afterwards, and says nothing about it.
                for statement in restore:
                    conn.execute(statement)
            # Cursors point at chunk ids that no longer exist. Left behind, the
            # next run would resume past the beginning of an empty index and
            # quietly index nothing.
            #
            # `resume:%` is item 1b's per-file mbox message-index cursor,
            # keyed on content hash - the same failure mode applies: a reset
            # followed by re-indexing the exact same (unchanged) mbox bytes
            # would otherwise resume from a mid-file position into an index
            # that no longer has anything before it.
            conn.execute(
                "DELETE FROM index_state WHERE key LIKE 'graph:%' "
                "OR key LIKE 'index:%' OR key LIKE 'resume:%'"
            )
            # **In the same transaction as the delete.** The search cache is
            # keyed on this, so without it a cache built from the index that was
            # just destroyed keeps answering - which is the exact failure the
            # generation counter exists to prevent, and it would be at its most
            # convincing right after a reset, when the results still look right.
            self._bump_generation(conn)
        self.reclaim_space()
        return count

    def file_bytes(self) -> int:
        """The database **and its journal**, as they sit on disk right now.

        The `-wal` file is not an implementation detail here: deleting a large
        index writes every one of those deletions into it first, so immediately
        after a reset the write-ahead log can be larger than the database ever
        was. Counting only `knowledge.db` would report a reset that freed
        nothing as having freed a great deal, or the reverse.
        """
        total = 0
        for suffix in ("", "-wal", "-shm"):
            try:
                total += Path(str(self.db_path) + suffix).stat().st_size
            except OSError:
                continue
        return total

    def checkpoint_wal(self) -> Optional[tuple[int, int, int]]:
        r"""Fold the write-ahead log into the database, and empty it if it can.

        2026-10-10, storage review S5. Nothing checkpointed during or after a
        run but SQLite's own auto-checkpoint, which is `PASSIVE`: it copies
        what no reader still needs and never makes the file smaller. With the
        window's readers open beside a run, the log could grow for the whole
        run. Meant for the end of a run (the pipeline calls it), and safe at
        any moment no write is open on this thread.

        `TRUNCATE` first: copies everything and cuts the log to nothing. It has
        to wait for every reader, so it is given `CHECKPOINT_WAIT_S` rather
        than the store's 30 s busy timeout, and when a reader or a writer
        outlasts that SQLite answers busy (`(1, ...)`, not an exception -
        measured 2026-10-10). Then `PASSIVE`, which copies what it can without
        waiting for anyone; `journal_size_limit` cuts the file back the next
        time the log starts over.

        Returns SQLite's `(busy, log pages, pages checkpointed)` from the last
        checkpoint that ran, or None when none could run. **Never raises** -
        an uncheckpointed log costs disk and a little read speed, never a
        result - so every failure is a warning saying what to do.
        """
        try:
            conn = self.conn
        except Exception as exc:                  # noqa: BLE001 - a closed store
            _log.warning("the write-ahead log was not checkpointed: {}", exc)
            return None
        if conn.in_transaction:
            # Inside a `batch()` on this thread: a checkpoint cannot run in a
            # transaction, and committing someone else's batch to make room
            # would break its all-or-nothing promise.
            _log.warning(
                "the write-ahead log was not checkpointed: a write is open on "
                "this thread. It will be checkpointed at the end of the next run.")
            return None
        result: Optional[tuple[int, int, int]] = None
        with self._write_lock:
            try:
                conn.execute("PRAGMA busy_timeout = %d" % int(CHECKPOINT_WAIT_S * 1000))
                try:
                    for mode in ("TRUNCATE", "PASSIVE"):
                        row = conn.execute(f"PRAGMA wal_checkpoint({mode})").fetchone()
                        if row is None:
                            continue
                        result = (int(row[0]), int(row[1]), int(row[2]))
                        if result[0] == 0:
                            break
                        _log.info(
                            "the write-ahead log could not be emptied: a reader or "
                            "writer kept it for {:g} s; copying what it can instead",
                            CHECKPOINT_WAIT_S)
                finally:
                    conn.execute("PRAGMA busy_timeout = %d" % int(self._timeout * 1000))
            except Exception as exc:              # noqa: BLE001 - see the docstring
                _log.warning(
                    "the write-ahead log was not checkpointed, so {}-wal stays "
                    "larger than it needs to be: {}. Nothing is lost; close other "
                    "Leasha windows and command-line runs and it is done next time.",
                    self.db_path.name, exc)
        # A `PASSIVE` answers 0 even when a reader stopped it part way; the
        # page counts say so. (-1, -1) is a database not in WAL mode.
        if result is not None and (result[0] or 0 <= result[2] < result[1]):
            _log.warning(
                "the write-ahead log was only partly checkpointed ({} of {} pages); "
                "another Leasha window or run is reading the index. The rest is "
                "copied once it stops.", result[2], result[1])
        return result

    def reclaim_space(self) -> int:
        """Return deleted space to the filesystem. Answers with bytes freed.

        **Three steps, and the reset needed all three.** `VACUUM` alone was what
        this did, and the owner reported *"i reset the index the index size
        remained the same"*.

        * The **first checkpoint** folds the deletions out of the write-ahead
          log and truncates it. In WAL mode a `DELETE FROM files` over a large
          index leaves every removed page sitting in `knowledge.db-wal`, so
          without this the file group can be no smaller after a reset than
          before - and on a big index, visibly larger.
        * `VACUUM` rebuilds the database itself, which is what actually returns
          the freed pages. Without it SQLite keeps the file at its high-water
          mark and reuses the space later.
        * The **second checkpoint** truncates the log `VACUUM` has just written.

        Failure is reported at `warning` with something to do about it, not
        swallowed at `debug`. This is the step whose silence made a reset look
        like it had not happened, and a locked database is a real and fixable
        cause: the space comes back on the next reset once whatever holds it
        has let go. Never raises - the index *is* cleared either way, and the
        rebuild is what the person came for.
        """
        before = self.file_bytes()
        try:
            self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            self.conn.execute("VACUUM")
            self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error as exc:
            _log.warning(
                "could not give the disk space back after clearing the index: "
                "{}. The index is empty and searching is correct; only the file "
                "size is wrong. Another process still has the database open - "
                "close any other Leasha window and any command line run, then "
                "reset again to reclaim it.", exc)
            return 0
        freed = before - self.file_bytes()
        _log.info("reclaimed {} bytes from {}", max(0, freed), self.db_path.name)
        return max(0, freed)

    def count_searches(self) -> int:
        """How many searches are recorded. One row out, not all of them.

        Settings displays this number. It was previously obtained by fetching a
        hundred thousand rows and calling `len()` on the list - on the UI thread,
        every time the panel was built.
        """
        return int(self.conn.execute("SELECT COUNT(*) FROM searches").fetchone()[0])

    def clear_usage_log(self) -> int:
        """Delete every recorded search. Returns how many were removed.

        Settings needs this: a log of what someone searched on their own machine
        is theirs to erase, and a system that records it with no way to clear it
        is not one to trust.
        """
        with self.write() as conn:
            count = conn.execute("SELECT COUNT(*) FROM searches").fetchone()[0]
            conn.execute("DELETE FROM search_hits")
            conn.execute("DELETE FROM searches")
        return int(count)

    # -- knowledge graph (schema v3) -----------------------------------------



    def iter_chunks_after(
        self, chunk_id: int, batch_size: int = 500
    ) -> Iterator[list[tuple[int, int, str]]]:
        """`(chunk_id, file_id, text)` in id order, starting after `chunk_id`.

        Id order, not file order, because ids only ever increase - so "everything
        after N" is a complete and stable description of what is left to do, and
        stays true while indexing continues in another thread.
        """
        last = chunk_id
        while True:
            rows = self.conn.execute(
                "SELECT id, file_id, text FROM chunks WHERE id > ? ORDER BY id LIMIT ?",
                (last, batch_size),
            ).fetchall()
            if not rows:
                return
            yield [(int(r["id"]), int(r["file_id"]), r["text"]) for r in rows]
            last = int(rows[-1]["id"])








    def _merges_within(
        self, bucket: list[tuple[int, str, int]]
    ) -> Iterator[tuple[int, int]]:
        """Containment merges inside one first-word bucket."""
        by_length = sorted(bucket, key=lambda row: len(row[1]))
        for index, (short_id, short_key, short_chunks) in enumerate(by_length):
            best: Optional[tuple[int, int]] = None
            for long_id, long_key, long_chunks in by_length[index + 1:]:
                if len(long_key) == len(short_key) or not _contains_words(long_key, short_key):
                    continue
                if long_chunks < short_chunks:
                    continue  # cannot possibly cover every chunk of the shorter
                overlap = self.conn.execute(
                    "SELECT COUNT(*) AS n FROM entity_mentions a "
                    "JOIN entity_mentions b ON a.chunk_id = b.chunk_id "
                    "WHERE a.entity_id = ? AND b.entity_id = ?",
                    (short_id, long_id),
                ).fetchone()["n"]
                if int(overlap) < short_chunks:
                    continue  # the short form is used on its own somewhere
                if best is None or long_chunks > best[1]:
                    best = (long_id, long_chunks)
            if best is not None:
                yield (short_id, best[0])

    def _apply_merges(self, merges: list[tuple[int, int]]) -> int:
        with self.write() as conn:
            for loser, winner in merges:
                conn.execute(
                    "INSERT INTO entity_mentions (entity_id, chunk_id, file_id, count) "
                    "SELECT ?, chunk_id, file_id, count FROM entity_mentions WHERE entity_id = ? "
                    "ON CONFLICT(entity_id, chunk_id) DO UPDATE SET "
                    "count = count + excluded.count",
                    (winner, loser),
                )
                conn.execute("DELETE FROM entities WHERE id = ?", (loser,))
        return len(merges)







    # -- repositories --------------------------------------------------------

    def upsert_repo(self, root_path: str, *, kind: str, name: Optional[str] = None,
                    last_seen: Optional[int] = None) -> int:
        """Insert or refresh one repository root. Returns its id.

        `root_path` is unique, so re-walking an unchanged tree updates
        `last_seen` rather than accumulating duplicates.
        """
        # `_basename`, not `Path(...).name`. These paths are written on Windows
        # and read back anywhere, and `PurePosixPath` treats the whole of
        # `D:\SearchProject` as one filename - so the stored name became the
        # full path, and `repo:leasha` matched nothing. The same trap this
        # module already documents for `files.name`.
        name = name if name is not None else (_basename(str(root_path)) or str(root_path))
        last_seen = int(time.time()) if last_seen is None else int(last_seen)

        with self.write() as conn:
            conn.execute(
                """
                INSERT INTO repos (root_path, name, kind, last_seen)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(root_path) DO UPDATE SET
                    name      = excluded.name,
                    kind      = excluded.kind,
                    last_seen = excluded.last_seen
                """,
                (str(root_path), name, kind, last_seen),
            )
            row = conn.execute(
                "SELECT id FROM repos WHERE root_path = ?", (str(root_path),)
            ).fetchone()
            return int(row["id"])

    def repo_files(self, repo_id: int, *, limit: int = 500) -> list[dict[str, Any]]:
        """Indexed files in one repository, newest first.

        Requested by the UI thread (`HANDOFF-ui-to-backend.md` B3). The Code
        tab worked without it by walking `iter_files(source_kind="file")` and
        matching the root as a prefix - correct, and a scan of the whole
        `files` table per expansion, when `files.repo_id` is right there and
        indexed.

        `source_kind = 'file'` is in the WHERE clause so an email archive can
        never appear in a list of source files.

        **`limit` is not clamped, deliberately.** The caller asks for
        `limit + 1` to tell "exactly 500" from "more than 500", and quietly
        capping it would make those two indistinguishable - which is the
        difference between a complete list and a truncated one presented as
        complete.
        """
        rows = self.conn.execute(
            """
            SELECT id, path, ext, size_bytes, mtime_ns, status, skip_code
            FROM files
            WHERE repo_id = ? AND source_kind = 'file'
            ORDER BY mtime_ns DESC, id DESC
            LIMIT ?
            """,
            (int(repo_id), int(limit)),
        ).fetchall()
        return [dict(row) for row in rows]

    def code_files(self, text: str = "", *, repo: str = "",
                   ext: Optional[Sequence[str]] = None,
                   limit: int = 500) -> list[dict[str, Any]]:
        """Indexed files that live in a repository, newest first, with its name.

        **One list across every repository**, which is what the Code tab needed
        once it became a search rather than a tree. `repo_files` answers "what
        is in *this* repository" and is still the right query for that; this
        answers "where is that file", which is the question somebody actually
        arrives with - and it cannot be answered by picking a repository first,
        because not knowing which one it is in is the reason they are looking.

        Every clause is index-backed: `files.repo_id` has an index and carries
        the join, `source_kind` has one, and `LIMIT` is applied in SQL. This
        runs on a debounce while somebody types.

        `text` matches the path substring-wise, so "order" finds
        `src/OrderService.cs`. Not FTS: this is a *name* search over a column,
        the same thing `search_files_by_name` does for documents, and a
        trigram index over paths would be a second index to keep in step for a
        list that is already bounded by `repo_id`.
        """
        clauses = ["f.repo_id IS NOT NULL", "f.source_kind = 'file'"]
        params: list[Any] = []

        def contains(column: str, value: str) -> None:
            clauses.append(f"{column} LIKE ? ESCAPE '\\'")
            params.append(like_contains(value, fold=False))

        if text.strip():
            contains("f.path", text.strip())
        if repo.strip():
            contains("r.name", repo.strip())
        if ext:
            wanted = [str(e).lstrip(".").lower() for e in ext if str(e).strip()]
            if wanted:
                clauses.append(
                    f"LOWER(f.ext) IN ({','.join('?' * len(wanted))})")
                params += wanted

        params.append(max(1, int(limit)))
        rows = self.conn.execute(
            f"""
            SELECT f.id, f.path, f.ext, f.size_bytes, f.mtime_ns, f.status,
                   r.name AS repo, r.root_path AS repo_root
            FROM files f
            JOIN repos r ON r.id = f.repo_id
            WHERE {' AND '.join(clauses)}
            ORDER BY f.mtime_ns DESC, f.id DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
        return [dict(row) for row in rows]

    def forget_repo(self, root_path: str) -> int:
        r"""Disown one repository. Returns how many files were released.

        **The way back from the accident in `WORKORDER-202626081149-code-tab.md`
        §1.** A copy of a project's `.git` was dragged into a document archive,
        and 1,179 files - 44% of the corpus - were attributed to a repository
        that was never a checkout anybody worked in. `scope:code` then matched
        the whole archive, and there was no command, flag or control anywhere
        that could undo it. The only route back was deleting the index, which
        is what the owner did, for what was a bookkeeping error.

        Three things happen together and all three are necessary:

        * every file's `repo_id` goes back to NULL - the attribution released;
        * the `repos` row goes, so nothing lists it or offers it again;
        * **the index generation is bumped**, so the search cache cannot serve
          results built while those files were code. Without it the first
          search after forgetting still answers from the old attribution, which
          reads as the command having done nothing.

        The root is remembered in the ignore list by the caller - see
        `ignored_repo_roots` - because detection would otherwise re-adopt it on
        the very next walk.
        """
        root = str(root_path or "").rstrip("\\/")
        if not root:
            return 0
        with self.write() as conn:
            row = conn.execute(
                "SELECT id FROM repos WHERE root_path = ? COLLATE NOCASE", (root,)
            ).fetchone()
            if row is None:
                return 0
            repo_id = int(row["id"])
            released = conn.execute(
                "UPDATE files SET repo_id = NULL WHERE repo_id = ?", (repo_id,)
            ).rowcount
            conn.execute("DELETE FROM repos WHERE id = ?", (repo_id,))
            self._bump_generation(conn)
        _log.info("forgot repository {} and released {} file(s)", root, released)
        return int(released or 0)

    #: Where the disowned roots live. `index_state`, not `.env`: it is a fact
    #: about this index rather than configuration anybody maintains.
    IGNORED_REPOS_KEY = "repos:ignored"

    def ignored_repo_roots(self) -> list[str]:
        """Roots that must never be adopted as repositories again."""
        raw = self.get_state(self.IGNORED_REPOS_KEY, "") or ""
        return [line.strip() for line in str(raw).splitlines() if line.strip()]

    def ignore_repo_root(self, root_path: str) -> None:
        r"""Remember that this root is not a repository.

        **Without this, forgetting is undone by the next walk.** Detection finds
        a `.git` and adopts it; that is the whole of how repositories are
        registered, and it is right. So "I have looked at this and it is not a
        checkout" has to be recorded somewhere detection reads, or the command
        that releases 1,179 files is silently reversed a minute later.
        """
        root = str(root_path or "").rstrip("\\/")
        if not root:
            return
        known = self.ignored_repo_roots()
        if any(root.lower() == one.lower() for one in known):
            return
        self.set_state(self.IGNORED_REPOS_KEY, "\n".join([*known, root]))

    def unignore_repo_root(self, root_path: str) -> bool:
        """Stop ignoring a root. True if it was being ignored.

        A decision somebody can reverse; the alternative is a setting that can
        only ever be added to, which is how a corpus quietly shrinks.
        """
        root = str(root_path or "").rstrip("\\/").lower()
        known = self.ignored_repo_roots()
        kept = [one for one in known if one.lower() != root]
        if len(kept) == len(known):
            return False
        self.set_state(self.IGNORED_REPOS_KEY, "\n".join(kept))
        return True

    def repo_undo_record(self, root_path: str) -> Optional[dict[str, Any]]:
        r"""Everything `restore_repo` needs to put a repository back exactly.

        Order 0y §1c. Read *before* `forget_repo`, by the window's "Ignore this
        repository", so its Undo is immediate and exact: the same name, kind
        and files, with no index run in between. `app.cli repos --remember`
        stays what it was (the next run re-adopts the folder); a person who
        clicked the wrong row deserves the row back now. None when the root is
        not a known repository.
        """
        root = str(root_path or "").rstrip("\\/")
        if not root:
            return None
        row = self.conn.execute(
            "SELECT id, root_path, name, kind FROM repos WHERE root_path = ? COLLATE NOCASE",
            (root,),
        ).fetchone()
        if row is None:
            return None
        ids = [int(r[0]) for r in self.conn.execute(
            "SELECT id FROM files WHERE repo_id = ?", (int(row["id"]),))]
        return {"root_path": str(row["root_path"]), "name": str(row["name"]),
                "kind": str(row["kind"]), "file_ids": ids}

    def restore_repo(self, record: dict[str, Any]) -> int:
        """Undo "Ignore this repository". Returns how many files came back.

        Stops ignoring the root, registers it again, and gives back exactly the
        files it held - only those still unattributed, so a file some other
        repository has claimed since is left alone. The index generation is
        bumped for the same reason `forget_repo` bumps it.
        """
        root = str(record.get("root_path") or "")
        if not root:
            return 0
        self.unignore_repo_root(root)
        repo_id = self.upsert_repo(root, kind=str(record.get("kind") or "work"),
                                   name=str(record.get("name") or "") or None)
        ids = [int(i) for i in record.get("file_ids") or ()]
        restored = 0
        with self.write() as conn:
            for start in range(0, len(ids), 500):
                part = ids[start:start + 500]
                marks = ",".join("?" * len(part))
                restored += conn.execute(
                    f"UPDATE files SET repo_id = ? WHERE id IN ({marks}) AND repo_id IS NULL",
                    (repo_id, *part),
                ).rowcount or 0
            self._bump_generation(conn)
        _log.info("restored repository {} with {} file(s)", root, restored)
        return int(restored)

    def prune_repos(self, alive: Sequence[str]) -> list[str]:
        r"""Remove repositories whose root is no longer one. Returns their roots.

        **Deleted files are pruned and repositories were not**, which is the
        second of the three mechanisms that made attribution permanent. A `.git`
        that has been removed or renamed left its row, its name in the tree and
        its `repo_id` on every file, for ever.

        `alive` is what the walk just found. Only roots the walk actually
        covered can be judged - a repository on an unmounted drive has not
        disappeared, it is simply not being looked at, and pruning it would
        release every one of its files the moment somebody indexed a different
        folder. The caller passes the roots it walked under; this removes the
        known repositories beneath them that were not seen.
        """
        seen = {str(root).rstrip("\\/").lower() for root in alive or ()}
        gone: list[str] = []
        for row in self.repos_list():
            root = str(row.get("root_path") or "").rstrip("\\/")
            if root and root.lower() not in seen:
                gone.append(root)
        for root in gone:
            self.forget_repo(root)
        return gone

    def repos_list(self) -> list[dict[str, Any]]:
        """Every known repository with its indexed file count, most files first.

        Returns: id, name, kind, root_path, last_seen, files.

        **`LEFT JOIN`, not `JOIN`.** A repository detected on a walk that then
        indexed none of its files - everything excluded by type, or a first
        pass that has not reached it - still exists and must still be listed.
        Dropping it would make the Code tab disagree with the walker for
        reasons nobody could see.
        """
        rows = self.conn.execute("""
            SELECT r.id, r.name, r.kind, r.root_path, r.last_seen,
                   COUNT(f.id) AS files
            FROM repos r
            LEFT JOIN files f ON f.repo_id = r.id
            GROUP BY r.id, r.name, r.kind, r.root_path, r.last_seen
            ORDER BY files DESC, r.name COLLATE NOCASE
        """).fetchall()
        return [dict(row) for row in rows]

    def repo_code_counts(self, extensions: Sequence[str]) -> dict[int, int]:
        """`{repo id: indexed files with one of these extensions}`.

        Order 202626081149 section 3's `repo_health` judges a repository by
        the share of its files that are code, and needs the count without
        fetching every file. One grouped query over the indexed `repo_id` and
        `ext` columns; a repository with none simply has no entry.
        """
        wanted = sorted({str(one).lstrip(".").lower() for one in extensions or () if one})
        if not wanted:
            return {}
        marks = ", ".join("?" for _ in wanted)
        rows = self.conn.execute(
            f"SELECT repo_id, COUNT(*) AS n FROM files "
            f"WHERE repo_id IS NOT NULL AND ext IN ({marks}) GROUP BY repo_id",
            wanted).fetchall()
        return {int(row[0]): int(row[1]) for row in rows}

    # -- Offline Media: volumes ------------------------------------------------
    #
    # Orders 202626270513 (drives) and 202626270514 (network, cloud, tape).
    # One catalogue for every kind - see the reasoning beside `volumes` in
    # schema.sql - so this section grows rather than being duplicated later.

    def upsert_volume(
        self, identity_key: str, *, kind: str, name: str,
        description: Optional[str] = None, volume_guid: Optional[str] = None,
        hardware_serial: Optional[str] = None, fs_label: Optional[str] = None,
        location_note: Optional[str] = None, sequential_medium: bool = False,
        status: Optional[str] = None, size_bytes: Optional[int] = None,
        file_count: Optional[int] = None, seen_at: Optional[int] = None,
    ) -> int:
        r"""Insert a new source, or refresh an existing one by `identity_key`.

        **Fully manual, per the owner's model**: this is called only from a
        Scan the user pressed, never from anything that runs on its own. A
        rescan of a known source updates `last_seen`/`last_scanned_at` and the
        measured `size_bytes`/`file_count`; `first_seen`, the name and the
        description are never touched by a re-scan - `name`/`description` are
        only written again when the caller explicitly asks to rename, because
        this same call is what every rescan uses and a rescan must never
        silently overwrite words the user typed.
        """
        now = int(time.time()) if seen_at is None else int(seen_at)
        resolved_status = status or "ONLINE"
        with self.write() as conn:
            conn.execute(
                """
                INSERT INTO volumes
                    (kind, identity_key, volume_guid, hardware_serial, fs_label,
                     name, description, location_note, status, sequential_medium,
                     first_seen, last_seen, last_scanned_at, size_bytes, file_count)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(identity_key) DO UPDATE SET
                    volume_guid     = COALESCE(excluded.volume_guid, volumes.volume_guid),
                    hardware_serial = COALESCE(excluded.hardware_serial, volumes.hardware_serial),
                    fs_label        = COALESCE(excluded.fs_label, volumes.fs_label),
                    status          = excluded.status,
                    sequential_medium = excluded.sequential_medium,
                    last_seen       = excluded.last_seen,
                    last_scanned_at = excluded.last_scanned_at,
                    size_bytes      = COALESCE(excluded.size_bytes, volumes.size_bytes),
                    file_count      = COALESCE(excluded.file_count, volumes.file_count)
                """,
                (kind, identity_key, volume_guid, hardware_serial, fs_label,
                 name, description, location_note, resolved_status,
                 1 if sequential_medium else 0,
                 now, now, now, size_bytes, file_count),
            )
            row = conn.execute(
                "SELECT id FROM volumes WHERE identity_key = ?", (identity_key,)
            ).fetchone()
            return int(row["id"])

    def get_volume(self, volume_id: int) -> Optional[VolumeRecord]:
        """One catalogued source by id, or None."""
        row = self.conn.execute(
            "SELECT * FROM volumes WHERE id = ?", (int(volume_id),)
        ).fetchone()
        return VolumeRecord.from_row(row) if row else None

    def get_volume_by_identity(self, identity_key: str) -> Optional[VolumeRecord]:
        """The source whose stable identity (a volume GUID, a UNC root, a Mac
        volume UUID) is `identity_key` - how a rescan finds a drive again
        whatever letter it came back under."""
        row = self.conn.execute(
            "SELECT * FROM volumes WHERE identity_key = ?", (identity_key,)
        ).fetchone()
        return VolumeRecord.from_row(row) if row else None

    def set_volume_status(self, volume_id: int, status: str) -> None:
        """The cheap write for a panel refresh - §2a checks passively, no
        device watcher, so this is called often and must cost one row write."""
        with self.write() as conn:
            conn.execute(
                "UPDATE volumes SET status = ? WHERE id = ?",
                (status, int(volume_id)),
            )

    def rename_volume_identity(self, volume_id: int, new_identity_key: str) -> bool:
        r"""202626270514 1a, the acceptance half of "softened by
        structure-match offer": the user has confirmed a new UNC path (or
        other identity) is the *same* catalogued source, renamed or moved,
        not a second one. Repoints `identity_key`; every existing `files` row
        is untouched, because those key on `volume_id`, never on
        `identity_key` - see `volume_synthetic_path`. So nothing about the
        catalogue's history moves, and the next Rescan simply finds the
        source at its new address.
        """
        with self.write() as conn:
            cursor = conn.execute(
                "UPDATE volumes SET identity_key = ? WHERE id = ?",
                (new_identity_key, int(volume_id)),
            )
            return cursor.rowcount > 0

    def volume_top_level_names(self, volume_id: int) -> frozenset[str]:
        r"""The distinct first path segment of every file catalogued under
        this volume - "Invoices", "2019", "report.pdf" for a share whose
        root holds those three. Used only for 1a's structure-match offer: a
        cheap, name-only fingerprint of what a source's root looked like on
        its last scan, never its full listing and never its content.
        """
        rows = self.conn.execute(
            """
            SELECT DISTINCT
                CASE WHEN instr(relative_path, '/') = 0
                     THEN relative_path
                     ELSE substr(relative_path, 1, instr(relative_path, '/') - 1)
                END AS top
            FROM files
            WHERE volume_id = ? AND relative_path IS NOT NULL
            """,
            (int(volume_id),),
        ).fetchall()
        return frozenset(row["top"] for row in rows if row["top"])

    def archive_volume(self, volume_id: int, location_note: str) -> bool:
        r"""202626270514 3b-1: detach a catalogued source into kind='archived'
        - "a name + free-text location", nothing more. `identity_key`,
        `volume_guid` and every `files` row are left exactly as they are:
        Browse (via `app.index.offline_media.resolve_file_path`, which
        already returns None for anything not in `connected_volumes`) and
        Delete both keep working unchanged, because neither depends on
        `kind`. Only Rescan stops meaning anything - `connected_volumes`
        never attempts to resolve an archived source, the same safe default
        it already applies to kind='cloud'/'phone'.
        """
        with self.write() as conn:
            cursor = conn.execute(
                "UPDATE volumes SET kind = 'archived', status = 'ARCHIVED', "
                "location_note = ? WHERE id = ?",
                (location_note, int(volume_id)),
            )
            return cursor.rowcount > 0

    def rename_volume(self, volume_id: int, *, name: Optional[str] = None,
                      description: Optional[str] = None) -> bool:
        """The one place `name`/`description` change after the first Scan."""
        if name is None and description is None:
            return False
        with self.write() as conn:
            if name is not None:
                conn.execute(
                    "UPDATE volumes SET name = ? WHERE id = ?",
                    (name, int(volume_id)),
                )
            if description is not None:
                conn.execute(
                    "UPDATE volumes SET description = ? WHERE id = ?",
                    (description, int(volume_id)),
                )
            return conn.total_changes > 0

    def list_volumes(self) -> list[dict[str, Any]]:
        """Every known source with its indexed file count, most recent first.

        `LEFT JOIN`, not `JOIN` - a freshly-scanned volume with zero indexable
        files (an empty drive, a share nothing could read) still exists and
        must still be listed, the same reasoning as `repos_list`.
        """
        rows = self.conn.execute("""
            SELECT v.*, COUNT(f.id) AS indexed_files
            FROM volumes v
            LEFT JOIN files f ON f.volume_id = v.id
            GROUP BY v.id
            ORDER BY v.last_seen DESC
        """).fetchall()
        return [dict(row) for row in rows]

    def local_root_summary(self, root: str) -> dict[str, Any]:
        r"""Count, total size and date span of ordinary (non-volume) indexed
        files under one local root. Order 202626270602 (0n) section 2a: a
        local root predates Offline Media and has no catalogue row of its
        own, so the Digital Inheritance report builds its numbers straight
        from `files` rather than from a `volumes` row that does not exist.

        Read-only, like every report query - this never writes.
        """
        cleaned = str(root or "").rstrip("\\/")
        escaped = like_escape(cleaned)
        pattern = f"{escaped}%"
        row = self.conn.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(size_bytes), 0) AS total_bytes, "
            "MAX(mtime_ns) AS newest, MIN(mtime_ns) AS oldest "
            "FROM files WHERE volume_id IS NULL AND source_kind = 'file' "
            "AND (path = ? OR path LIKE ? ESCAPE '\\')",
            (cleaned, pattern),
        ).fetchone()
        if row is None:
            return {"n": 0, "total_bytes": 0, "newest": None, "oldest": None}
        return dict(row)

    def local_root_folder_counts(self, root: str) -> list[tuple[str, int]]:
        r"""One `(top-level subfolder, file count)` pair per subfolder
        directly under `root` - the Digital Inheritance report's own
        "top-level folder summary per source, derived from the index."
        Commonest first.

        Grouped by `parent_dir` in SQL (bounded by folder count, not file
        count) and bucketed to the root's immediate children in Python -
        `parent_dir` is often several levels deeper than `root` and only
        the first segment past it is what a source-level summary wants.
        """
        cleaned = str(root or "").rstrip("\\/")
        escaped = like_escape(cleaned)
        pattern = f"{escaped}%"
        rows = self.conn.execute(
            "SELECT parent_dir, COUNT(*) AS n FROM files "
            "WHERE volume_id IS NULL AND source_kind = 'file' "
            "AND (parent_dir = ? OR parent_dir LIKE ? ESCAPE '\\') "
            "GROUP BY parent_dir",
            (cleaned, pattern),
        ).fetchall()
        return _bucket_top_level(cleaned, [(r["parent_dir"], int(r["n"])) for r in rows])

    def volume_folder_counts(self, volume_id: int) -> list[tuple[str, int]]:
        r"""Same shape as `local_root_folder_counts`, for a catalogued
        Offline Media volume - `relative_path`'s own first segment is
        already root-relative, unlike an ordinary file's `parent_dir`.
        """
        rows = self.conn.execute(
            "SELECT relative_path, COUNT(*) AS n FROM files "
            "WHERE volume_id = ? AND source_kind = 'file' "
            "GROUP BY relative_path",
            (int(volume_id),),
        ).fetchall()
        counts: dict[str, int] = {}
        for row in rows:
            rel = str(row["relative_path"] or "").replace("\\", "/")
            top = rel.split("/", 1)[0] if rel else "(top level)"
            counts[top] = counts.get(top, 0) + int(row["n"])
        return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0].lower()))

    def move_volume_file(self, volume_id: int, old_relative_path: str,
                         new_relative_path: str, *, size_bytes: int,
                         mtime_ns: int) -> bool:
        r"""1e: "same content hash at a new relative path moves the rows
        instead of re-extracting." Updates identity and stat in place -
        `content_hash`, chunks and vectors are exactly as correct as they
        were before the move, because the *bytes* have not changed.

        Called before the pipeline walk reaches this file, so its updated
        `path`/`mtime_ns`/`size_bytes` already match what the walk is about
        to find, and the ordinary incremental check - same path, same size,
        same mtime - skips it as unchanged. That is the whole mechanism;
        nothing downstream needs to know a move happened at all.
        """
        new_path = volume_synthetic_path(volume_id, new_relative_path)
        with self.write() as conn:
            cursor = conn.execute(
                """
                UPDATE files SET
                    path = ?, relative_path = ?, size_bytes = ?, mtime_ns = ?,
                    parent_dir = ?
                WHERE volume_id = ? AND relative_path = ?
                """,
                (new_path, new_relative_path, size_bytes, mtime_ns,
                 volume_synthetic_path(volume_id, str(Path(new_relative_path).parent))
                 if Path(new_relative_path).parent != Path(".")
                 else volume_synthetic_path(volume_id, ""),
                 volume_id, old_relative_path),
            )
            moved = cursor.rowcount > 0
            if moved:
                self._bump_generation(conn)
        return moved

    def delete_volume_row(self, volume_id: int) -> None:
        """Remove the catalogue entry itself. Callers delete the files first -
        see `app.index.offline_media.delete_volume` for the full cascade;
        this alone would leave `files.volume_id` pointing at nothing until
        `ON DELETE SET NULL` runs, which is the wrong order for a Delete that
        must report an accurate file count."""
        with self.write() as conn:
            conn.execute("DELETE FROM volumes WHERE id = ?", (int(volume_id),))

    def distinct_values(self, kind: str, *, prefix: str = "",
                        limit: int = 40, within: Any = None) -> list[str]:
        """Just the values. See `distinct_value_counts` for the numbers."""
        return [row.value for row in self.distinct_value_counts(
            kind, prefix=prefix, limit=limit, within=within)]

    def distinct_value_counts(self, kind: str, *, prefix: str = "",
                              limit: int = 40,
                              within: Any = None) -> list["ValueCount"]:
        """Values actually present in the index, commonest first.

        What `/type`, `/from`, `/path` and `/repo` offer once somebody has
        chosen the filter. **The difference between a filter you can use and one
        you have to guess at**: `/type <type>` says a filter exists, `/type`
        offering `pdf`, `docx`, `msg` says what is in there - and a filter typed
        from a guess that matches nothing is the commonest way a working feature
        looks broken.

        **Every query here is index-backed and bounded.** This runs behind a
        keystroke, and the first non-negotiable is that no unbounded work sits
        in that path: `ext`, `parent_dir`, `sender` and `repos.name` all have an
        index, `LIMIT` is applied in SQL rather than in Python, and `kind` is
        looked up in a table rather than interpolated - so a caller cannot ask
        for a column, or a scan, that was not planned for.

        Ordered by frequency, not alphabetically. The extension somebody wants
        is nearly always one of the three they have thousands of.
        """
        shape = _VALUE_SHAPES.get(str(kind))
        if shape is None:
            return []

        # Escaped, never interpolated: `%` and `_` typed by a person mean those
        # characters. The same rule `browse_messages` follows, for the same
        # reason - a person searching for a literal underscore should find it.
        escaped = like_escape(str(prefix or ""))
        where, params = _scope_sql(within)
        head = (f"FROM {shape.source} "
                f"WHERE {shape.guard} AND {shape.value} LIKE ? ESCAPE '\\'{where}")

        if where:
            # **Sampled, because the measurement said so.** Grouping the whole
            # of a filtered corpus cannot use an index for both the filter and
            # the grouping: `folder` scoped by `type:pdf` measured **437ms on
            # 500,000 files**, ten times the unscoped query, behind a keystroke.
            # This order's own rule is that a scope must not widen the query
            # cost, so it does not get to.
            #
            # Counting the first `VALUE_SAMPLE` matching rows instead: **11.1ms,
            # and the same twenty-five folders**. The menu is ordered by
            # frequency and capped at forty, so what it needs is the *order* and
            # the *values*, and a sample gives both; only the absolute counts
            # are proportional, which matters when they are shown and is said
            # where they are.
            #
            # A value rare enough to be absent from the first 20,000 matching
            # files is by definition not among the commonest - and typing one
            # more character narrows the candidates, so the sample closes in on
            # the exact answer exactly as somebody types towards it.
            sql = (f"SELECT v, {shape.count} AS n FROM "
                   f"(SELECT {shape.value} AS v {head} LIMIT {VALUE_SAMPLE}) "
                   f"GROUP BY v ORDER BY n DESC, v LIMIT ?")
        else:
            sql = (f"SELECT {shape.value} AS v, {shape.count} AS n {head} "
                   f"GROUP BY {shape.group} ORDER BY n DESC, v LIMIT ?")

        rows = self.conn.execute(
            sql, (f"%{escaped}%", *params, max(1, int(limit)))).fetchall()
        counted = [(str(row["v"]), int(row["n"])) for row in rows]

        # **Exact unless the sample was spent.** An unscoped query counts every
        # row, so its numbers are the corpus's. A scoped one counts the first
        # `VALUE_SAMPLE`, and when that ceiling is reached the numbers are a
        # fraction of the truth - `pdf 200 files` for a corpus holding 5,000 of
        # them. A label that is wrong by a factor of twenty-five is worse than
        # no label, so the caller is told which it has rather than left to
        # guess; `2b` shows the count only when it is exact.
        exact = not where or sum(n for _v, n in counted) < VALUE_SAMPLE
        return [ValueCount(value, n, exact) for value, n in counted]

    # -- indexing state ------------------------------------------------------

    def set_state(self, key: str, value: str) -> None:
        """Persist the resumability cursor. Committed before it is needed."""
        with self.write() as conn:
            conn.execute(
                "INSERT INTO index_state (key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
                "updated_at = excluded.updated_at",
                (key, value, int(time.time())),
            )

    def set_states(self, values: dict[str, str]) -> None:
        """Several keys, one transaction.

        The settings panel saves five ceilings together. Five calls to
        `set_state` is five commits - five fsyncs on the UI thread for one
        change nobody thinks of as five. This is the same work in one.
        """
        if not values:
            return
        now = int(time.time())
        with self.write() as conn:
            conn.executemany(
                "INSERT INTO index_state (key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
                "updated_at = excluded.updated_at",
                [(key, str(value), now) for key, value in values.items()],
            )

    def image_hashes(self) -> list[tuple[str, int, Optional[int], Optional[str], int, int]]:
        """Every row of the junk-image filter's book (schema v29), as plain tuples.

        `(hash, seen, words, phash, width, height)`. Read once per run, the
        first time a picture inside a mail archive is met. Order 0z lane D.
        """
        rows = self.conn.execute(
            "SELECT hash, seen, words, phash, width, height FROM image_hashes").fetchall()
        return [(str(r[0]), int(r[1] or 0), None if r[2] is None else int(r[2]),
                 r[3], int(r[4] or 0), int(r[5] or 0)) for r in rows]

    def save_image_hashes(
        self, rows: list[tuple[str, int, Optional[int], Optional[str], int, int]],
    ) -> None:
        """Write rows of the book, replacing what was there. One transaction."""
        if not rows:
            return
        now = int(time.time())
        with self.write() as conn:
            conn.executemany(
                "INSERT INTO image_hashes (hash, seen, words, phash, width, height, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(hash) DO UPDATE SET seen = excluded.seen, "
                "words = excluded.words, phash = excluded.phash, "
                "width = excluded.width, height = excluded.height, "
                "updated_at = excluded.updated_at",
                [(*row, now) for row in rows])

    def get_state(self, key: str, default: Optional[str] = None) -> Optional[str]:
        """One `index_state` value, or `default` when the key was never written.
        Values are strings: callers parse their own (`json.loads`, `int`)."""
        row = self.conn.execute("SELECT value FROM index_state WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def delete_state(self, key: str) -> None:
        """Forget one cursor. Used once what it pointed at is fully done.

        A per-file resume cursor (`resume:{content_hash}`) has nothing left
        to resume once the file's own closing marker is written - left
        behind, it would just be a harmless orphan keyed on a digest nothing
        will ever look up again, *unless* the exact same bytes are re-indexed
        after a reset, which would then wrongly skip messages a fresh run
        needs to re-see. Clearing it here is cheap and removes the question.
        """
        with self.write() as conn:
            conn.execute("DELETE FROM index_state WHERE key = ?", (key,))

    def all_state(self) -> dict[str, str]:
        """Every `index_state` row, for the diagnostic bundle. Includes the
        cursors, the settings kept here and the UI preferences alike."""
        return {r["key"]: r["value"] for r in self.conn.execute("SELECT key, value FROM index_state")}

    # -- generation (search cache invalidation) ------------------------------

    def _bump_generation(self, conn: sqlite3.Connection) -> None:
        r"""Move the generation on, so the search cache drops what it holds.

        Work order 0x item 5d. **Once per `batch()` transaction, not once per
        call inside it.** Another connection only ever sees a transaction
        whole - WAL gives each reader a snapshot of *committed* data - so the
        search cache, which reads the number from another thread, can never
        tell one bump from five inside the same transaction: either way the
        number it sees has changed exactly when the rows it is keyed on have.
        The extra statements were pure cost - two per indexed document
        (`upsert_file` and `replace_chunks` each bumped), and every statement
        pays to get Python's interpreter lock back afterwards, which is what
        made the indexer's writes slow (see `Pipeline._begin_write_group`).

        Outside a batch, every `write()` is its own transaction and bumps as
        it always did.
        """
        if getattr(self._local, "batch_depth", 0):
            if getattr(self._local, "bumped", False):
                return
            self._local.bumped = True
        conn.execute("UPDATE index_generation SET generation = generation + 1 WHERE id = 1")

    def bump_generation(self) -> None:
        """Invalidate the search cache from outside a write batch.

        Every other caller of `_bump_generation` already holds a `write()`
        connection because it just changed rows the cache is keyed on - a
        file-type mapping change is different: it edits a config file on
        disk, not a table, so there is no natural `write()` block to piggy-
        back the bump onto. This is that missing public entry point.
        """
        with self.write() as conn:
            self._bump_generation(conn)

    @property
    def generation(self) -> int:
        """Increments on every write. The search cache keys on it, so stale
        results cannot be served after an incremental index run.

        Returns 0 rather than raising when the value is missing or NULL. A
        machine that has just run out of memory produces exactly that, and the
        first symptom was `int() argument must be ... not 'NoneType'` repeated
        once per keystroke - noise on top of the real problem, from the one
        place that had promised to be robust.
        """
        row = self.conn.execute("SELECT generation FROM index_generation WHERE id = 1").fetchone()
        if row is None or row["generation"] is None:
            return 0
        return int(row["generation"])

    # -- reporting -----------------------------------------------------------

    def has_any_files(self) -> bool:
        r"""Is there anything in the index at all? **O(1), whatever the size.**

        `stats()` answers this too, and that is the problem: it is three
        `COUNT(*)`, two of them over `chunks`, which SQLite has to scan. Measured
        on a 2,000,000-chunk fixture: 93ms, so roughly 460ms at the ten million
        this is designed for. Three views called it on the UI thread to decide
        between two sentences, behind a comment describing it as cheap.

        `LIMIT 1` stops at the first row.
        """
        return self.conn.execute("SELECT 1 FROM files LIMIT 1").fetchone() is not None

    def holds_ext(self, extensions: Any) -> bool:
        r"""Does the index hold at least one file of any of these extensions?

        **A seek, not a census.** The search tab asks this before applying
        `type:mail` to "mail from 2017" (`translate_rules.apply`), on every
        dispatch. `distinct_values("ext")` answers it too, by grouping every
        row: measured 25.5 ms on 200,000 files, where this - `idx_files_ext`,
        `LIMIT 1` - measured 0.006 ms on the same table.
        """
        wanted = [str(ext).lstrip(".").lower() for ext in extensions or () if str(ext)]
        if not wanted:
            return False
        marks = ", ".join("?" for _ in wanted)
        return self.conn.execute(
            f"SELECT 1 FROM files WHERE ext IN ({marks}) LIMIT 1", wanted,
        ).fetchone() is not None

    def stats(self) -> dict[str, Any]:
        """Counts for `cli stats`, `doctor` and the diagnostic bundle.

        **Not cheap**: two `COUNT(*)` over `chunks` are a scan (93 ms at two
        million chunks). The window asks `has_any_files`, `status_counts` and
        `count_listed_files` instead, each of which rides an index or a cache.
        """
        counts = {
            row["status"]: int(row["n"])
            for row in self.conn.execute("SELECT status, COUNT(*) AS n FROM files GROUP BY status")
        }
        chunk_total = self.conn.execute("SELECT COUNT(*) AS n FROM chunks").fetchone()["n"]
        embedded = self.conn.execute(
            "SELECT COUNT(*) AS n FROM chunks WHERE embedded = 1"
        ).fetchone()["n"]
        return {
            "db_path": str(self.db_path),
            "schema_version": self.schema_version,
            "expected_schema_version": CURRENT_VERSION,
            "generation": self.generation,
            "files": counts,
            "files_total": sum(counts.values()),
            "chunks_total": int(chunk_total),
            "chunks_embedded": int(embedded),
            # 2026-10-08: found by their words only, on purpose - never a gap
            # in meaning-based search. See `KEYWORD_ONLY`.
            "chunks_keyword_only": self.keyword_only_count(),
            "skipped_by_code": self.skipped_summary(),
        }

    def vector_coverage(self, vector_rows: Optional[int]) -> dict[str, Any]:
        """Whether meaning-based search can actually see the corpus.

        Requested by the UI thread (`HANDOFF-ui-to-backend.md` B2) so the
        window can say "meaning-based search is off" rather than leaving it in
        a log nobody reads. **`vectors_ready` is the field to bind to.**

        The row count has to be passed in: it lives in LanceDB, and this store
        deliberately knows nothing about the vector store - SQLite is the
        authority and the vectors are derived from it, never the reverse.

        **Measured against `chunks_total`, not `chunks_embedded`.** The flag
        answers "did the vectors get written for the chunks we tried", which is
        not the question. The question is "can meaning-based search see my
        corpus", and only the total can answer it - on an index where 154 of
        3,355 passages were ever attempted, the flag and the row count agree
        perfectly and a check comparing those two reports everything healthy.
        `semantic_search_warnings` in the CLI already learned this the hard way.
        """
        total = int(self.conn.execute(
            "SELECT COUNT(*) AS n FROM chunks").fetchone()["n"])
        # 2026-10-08: passages kept by their words only on purpose are not
        # missing anything, so they are not counted against the vectors.
        keyword_only = self.keyword_only_count()
        chunks = max(0, total - keyword_only)
        rows = max(0, int(vector_rows or 0))
        covered = min(1.0, rows / chunks) if chunks else (1.0 if total else 0.0)
        return {
            # True only when meaning-based search covers effectively all of it.
            # Not `rows > 0`: a store holding 5% of the corpus is not "ready",
            # and calling it ready is how a half-working search looks healthy.
            "vectors_ready": bool(chunks) and covered >= 0.95,
            "vector_rows": rows,
            "chunks_total": total,
            "chunks_keyword_only": keyword_only,
            "coverage": round(covered, 4),
            "missing": max(0, chunks - rows),
        }

    def keyword_only_count(self) -> int:
        """Passages found by their words only, on purpose. See `KEYWORD_ONLY`."""
        # 2026-10-10 (schema v36): answered from the partial index
        # `idx_chunks_keyword_only`, not a scan of every passage and its text.
        # The `WHERE` must stay `embedded = 2` written exactly so: the planner
        # uses a partial index only for a term that matches its own.
        return int(self.conn.execute(
            f"SELECT COUNT(*) AS n FROM chunks WHERE embedded = {self.KEYWORD_ONLY}"
        ).fetchone()["n"])

    def integrity_check(self) -> bool:
        """SQLite's own full check: reads the whole file, so minutes on a big
        index. For `diagnose` and `doctor`, never a window refresh."""
        row = self.conn.execute("PRAGMA integrity_check").fetchone()
        return str(row[0]).lower() == "ok"
