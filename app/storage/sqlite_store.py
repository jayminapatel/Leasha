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

import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import (
    Any, Iterable, Iterator, NamedTuple, Optional, Sequence, Type,
)

from app.storage.like import like_escape

from app.core.errors import AppError, AppErrorException, make_error
from app.core.identifiers import symbol_tokens
from app.core.logging import logger
from app.storage.migrations import (
    CONTENT_TRIGGERS, CURRENT_VERSION, apply_migrations, read_version,
)

__all__ = ["SqliteStore", "FileRecord", "ChunkRecord", "FileStatus", "VolumeRecord", "volume_synthetic_path", "VOLUME_PATH_SCHEME", "FaceRecord", "PileRecord", "PileSample", "PendingSuggestion"]

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
        return cls(**{key: row[key] for key in cls.__dataclass_fields__})


@dataclass(frozen=True)
class ChunkRecord:
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
        except sqlite3.OperationalError as exc:
            try:
                conn.close()
            except sqlite3.Error:
                pass
            raise self._busy_error(str(exc)) from exc

        self._open.append(conn)
        return conn

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
        with self._conns():
            self._closed = False
            conn = getattr(self._local, "conn", None)
            if conn is None:
                conn = self._new_connection()
                self._local.conn = conn
            if not self._migrated:
                # Once per store, not once per connection. Under the lock, so
                # a worker thread opening its first connection cannot race the
                # schema into existence twice.
                apply_migrations(conn)
                self._migrated = True
        return self

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
            return True
        except Exception as exc:                  # noqa: BLE001 - see the docstring
            _log.warning(
                "the query planner was not refreshed, so query plans may "
                "drift stale: {}", exc)
            return False

    def close(self) -> None:
        # Both, in the lock order set out in `__init__`: wait for an in-flight
        # write to finish rather than closing the connection underneath it.
        #
        # §3c: PRAGMA optimize moved to idle - see optimize_query_planner()
        # above. SQLite's own guidance recommends periodic (e.g., hourly)
        # PRAGMA optimize rather than at every close. Running it on the
        # close path delays shutdown while holding the single-instance lock,
        # which makes a relaunch wait unnecessarily.
        with self._write_lock, self._conns_lock:
            self._closed = True
            for conn in self._open:
                try:
                    conn.close()
                except sqlite3.Error:
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
            self._local.conn = conn
            return conn

    @property
    def schema_version(self) -> int:
        return read_version(self.conn)

    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        """One serialised write transaction. Rolls back on any exception.

        **Joins an open `batch()` rather than nesting inside it.** SQLite has
        no nested transactions, so a `write()` called while a batch is open on
        this thread would otherwise commit the batch early - turning the
        grouping into a lie without failing.
        """
        if getattr(self._local, "batch_depth", 0):
            yield self.conn
            return

        with self._write_lock:
            conn = self.conn
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            else:
                conn.execute("COMMIT")

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
            conn.execute("BEGIN IMMEDIATE")
            self._local.batch_depth = 1
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            else:
                conn.execute("COMMIT")
            finally:
                self._local.batch_depth = 0

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

        with self.write() as conn:
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
            row = conn.execute("SELECT id FROM files WHERE path = ?", (str(path),)).fetchone()
            file_id = int(row["id"])
            # Keep the filename index in step. Only real files: a PST message's
            # "path" is a synthetic key nobody typed and nobody would recognise,
            # and mail would outnumber documents ten to one in a Files list.
            if source_kind in ("file", "archive"):
                conn.execute("DELETE FROM files_fts WHERE rowid = ?", (file_id,))
                conn.execute(
                    "INSERT INTO files_fts(rowid, name, folder) VALUES (?, ?, ?)",
                    (file_id, _basename(str(path)), parent_dir),
                )
            self._bump_generation(conn)
        return file_id

    def get_file(self, path: str) -> Optional[FileRecord]:
        row = self.conn.execute("SELECT * FROM files WHERE path = ?", (str(path),)).fetchone()
        return FileRecord.from_row(row) if row else None

    def get_file_by_id(self, file_id: int) -> Optional[FileRecord]:
        row = self.conn.execute("SELECT * FROM files WHERE id = ?", (file_id,)).fetchone()
        return FileRecord.from_row(row) if row else None

    def mark_indexed(self, file_id: int) -> None:
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
                (status, error.code, error.message, file_id),
            )

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

    def optimize_fts(self) -> bool:
        r"""Merge the FTS5 index's b-tree segments into one. Never raises.

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
        """
        try:
            with self.write() as conn:
                conn.execute(
                    "INSERT INTO chunks_fts(chunks_fts) VALUES('optimize')")
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
                return self._suspend_content_triggers(conn)
        except Exception as exc:                  # noqa: BLE001
            _log.warning(
                "FTS content triggers could not be dropped, "
                "word index will update row-by-row: {}", exc)
            return []

    def restore_fts_triggers(self, trigger_sql: list[str]) -> bool:
        """Restore FTS content triggers after a bulk insert.

        Returns whether the restore succeeded. A failure is logged but does not
        fail the run — the triggers are optional optimizations, not required.
        """
        if not trigger_sql:
            return False
        try:
            with self.write() as conn:
                for sql in trigger_sql:
                    if sql:
                        conn.execute(sql)
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
                        pass
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
        clauses: list[str] = []
        params: list[Any] = []

        # LIKE with the value wrapped in wildcards, never interpolated. `%` and
        # `_` typed by a person are escaped, so searching for a literal
        # underscore in an address finds it instead of matching any character.
        def contains(column: str, value: str) -> None:
            escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            clauses.append(f"{column} LIKE ? ESCAPE '\\'")
            params.append(f"%{escaped}%")

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

        where = (" WHERE " + " AND ".join(clauses)) if clauses else " WHERE 1=1"
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
                   f.path, f.size_bytes, f.status
            FROM messages m
            JOIN files f ON f.id = m.file_id
            {where} {file_where}
            -- `sent_at IS NULL` rather than `NULLS LAST`, which needs SQLite
            -- 3.30. The bundled version is newer, but the version a user's
            -- Python happens to ship is not something this should depend on,
            -- and the two forms cost the same.
            ORDER BY m.sent_at IS NULL, m.sent_at {direction}, m.file_id {direction}
            LIMIT ?
        """
        params.extend(file_params or ())
        params.append(max(1, int(limit)))
        return [dict(row) for row in self.conn.execute(sql, params)]

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
            f"""SELECT file_id, subject, sender, recipients, sent_at, has_attach
                FROM messages WHERE file_id IN ({placeholders})""",
            wanted,
        )
        return {int(row["file_id"]): dict(row) for row in rows}

    def count_messages(self) -> int:
        row = self.conn.execute("SELECT COUNT(*) AS n FROM messages").fetchone()
        return int(row["n"]) if row else 0

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
            if not getattr(self, "_stem_ready", False):
                self.conn.execute(
                    "CREATE VIRTUAL TABLE IF NOT EXISTS temp.stem_probe "
                    "USING fts5(text, tokenize='porter unicode61')")
                self.conn.execute(
                    "CREATE VIRTUAL TABLE IF NOT EXISTS temp.stem_probe_v "
                    "USING fts5vocab('stem_probe', 'row')")
                self._stem_ready = True
            self.conn.execute("DELETE FROM temp.stem_probe")
            self.conn.execute(
                "INSERT INTO temp.stem_probe(text) VALUES (?)", (text,))
            row = self.conn.execute(
                "SELECT term FROM temp.stem_probe_v LIMIT 1").fetchone()
        except sqlite3.Error as exc:
            _log.debug("could not stem {}: {}", text, exc)
            return ""
        return str(row["term"]) if row else ""

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
                    WHERE f.source_kind = 'file' AND f.taken_at_ns IS NULL{where}
                    ORDER BY f.mtime_ns {_dir}
                    LIMIT ?""",
                [*params, capped],
            ).fetchall()
            shot_date = self.conn.execute(
                f"""SELECT {columns}
                    FROM files f
                    WHERE f.source_kind = 'file' AND f.taken_at_ns IS NOT NULL{where}
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
                WHERE files_fts MATCH ? {where}

                UNION ALL

                SELECT f.id, f.path, f.ext, f.size_bytes, f.mtime_ns,
                       f.taken_at_ns,
                       f.status, f.skip_code, f.source_kind,
                       f.volume_id, f.relative_path,
                       bm25(chunks_fts) AS score
                FROM chunks_fts
                JOIN chunks c ON c.id = chunks_fts.rowid
                JOIN files  f ON f.id = c.file_id
                WHERE chunks_fts MATCH ? AND f.source_kind = 'file' {where}
            )
            GROUP BY id
            ORDER BY {by_date_outer if wants_sort else 'score'}
            LIMIT ?
        """
        try:
            rows = self.conn.execute(
                sql, [literal, *params, literal, *params, capped]
            ).fetchall()
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
        sql = f"""
            SELECT DISTINCT repo_id FROM (
                SELECT f.repo_id AS repo_id
                FROM files_fts JOIN files f ON f.id = files_fts.rowid
                WHERE files_fts MATCH ? AND f.repo_id IS NOT NULL {where}

                UNION

                SELECT f.repo_id
                FROM chunks_fts
                JOIN chunks c ON c.id = chunks_fts.rowid
                JOIN files  f ON f.id = c.file_id
                WHERE chunks_fts MATCH ? AND f.repo_id IS NOT NULL {where}
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

    def delete_file_by_path(self, path: str) -> Optional[int]:
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

    def iter_files(
        self, status: Optional[str] = None, *, source_kind: Optional[str] = None,
        volume_id: Optional[int] = None,
    ) -> Iterator[FileRecord]:
        """Files, optionally narrowed. Filter in SQL, never in Python.

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

    def skipped_summary(self) -> dict[str, int]:
        """Counts by skip_code, for the 'N files skipped - review' panel."""
        rows = self.conn.execute(
            "SELECT skip_code, COUNT(*) AS n FROM files "
            "WHERE skip_code IS NOT NULL GROUP BY skip_code ORDER BY n DESC"
        )
        return {row["skip_code"]: int(row["n"]) for row in rows}

    # -- chunks --------------------------------------------------------------

    def replace_chunks(self, file_id: int, chunks: Sequence[dict[str, Any]]) -> list[int]:
        """Replace every chunk of one file. Returns the new chunk ids.

        Replacing rather than appending keeps re-indexing a changed file
        idempotent, and the triggers keep chunks_fts in step automatically.
        """
        with self.write() as conn:
            conn.execute("DELETE FROM chunks WHERE file_id = ?", (file_id,))
            ids: list[int] = []
            for ordinal, chunk in enumerate(chunks):
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
                        chunk["text"],
                        # camelCase split forms, so `password` finds
                        # `ResetPasswordHandler`. Empty for prose - see
                        # app/core/identifiers.py for why this is not the
                        # whole text again.
                        symbol_tokens(chunk["text"]),
                        chunk.get("char_start"),
                        chunk.get("char_end"),
                        chunk.get("page"),
                        # Adoptions §6a: `Q3!A14`, or None for the great
                        # majority of documents that have no interior address
                        # anybody could act on.
                        chunk.get("label"),
                    ),
                )
                ids.append(int(cursor.lastrowid))
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
        row = self.conn.execute("SELECT * FROM chunks WHERE id = ?", (chunk_id,)).fetchone()
        if row is None:
            return None
        return ChunkRecord(
            id=row["id"], file_id=row["file_id"], ordinal=row["ordinal"], text=row["text"],
            char_start=row["char_start"], char_end=row["char_end"], page=row["page"],
            embedded=row["embedded"],
        )

    def chunks_for_file(self, file_id: int) -> list[ChunkRecord]:
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
        if row is not None:
            self.sync_people_segment(int(row["file_id"]))

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
        affected = self._files_with_pile(pile_id)
        with self.write() as conn:
            conn.execute("UPDATE piles SET name = ? WHERE id = ?", (cleaned, pile_id))
        for file_id in affected:
            self.sync_people_segment(file_id)

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
        """
        with self.write() as conn:
            cursor = conn.execute(
                "UPDATE files SET taken_at_ns = ?, taken_at_is_hint = 1 "
                "WHERE path LIKE ? ESCAPE '\\' "
                "AND (taken_at_is_hint = 1 OR taken_at_ns IS NULL)",
                (taken_at_ns, like_escape(path_prefix) + "%"),
            )
            return int(cursor.rowcount)

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
        ids = [(int(i),) for i in chunk_ids]
        if not ids:
            return
        with self.write() as conn:
            conn.executemany("UPDATE chunks SET embedded = 1 WHERE id = ?", ids)

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
                   "sender_lc", "recipients_lc", "subject_lc")
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
        with self.write() as conn:
            conn.execute(
                f"INSERT INTO messages (file_id, {', '.join(columns)}) "
                f"VALUES (?, {', '.join('?' * len(columns))}) "
                f"ON CONFLICT(file_id) DO UPDATE SET "
                + ", ".join(f"{c} = excluded.{c}" for c in columns),
                (file_id, *values),
            )

    def get_message(self, file_id: int) -> Optional[dict[str, Any]]:
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
                              "files_fts", "repos"):
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
            SELECT id, path, ext, size_bytes, mtime_ns, status
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
            escaped = (value.replace("\\", "\\\\")
                       .replace("%", "\\%").replace("_", "\\_"))
            clauses.append(f"{column} LIKE ? ESCAPE '\\'")
            params.append(f"%{escaped}%")

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
        row = self.conn.execute(
            "SELECT * FROM volumes WHERE id = ?", (int(volume_id),)
        ).fetchone()
        return VolumeRecord.from_row(row) if row else None

    def get_volume_by_identity(self, identity_key: str) -> Optional[VolumeRecord]:
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

    def get_state(self, key: str, default: Optional[str] = None) -> Optional[str]:
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
        return {r["key"]: r["value"] for r in self.conn.execute("SELECT key, value FROM index_state")}

    # -- generation (search cache invalidation) ------------------------------

    def _bump_generation(self, conn: sqlite3.Connection) -> None:
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
        chunks = int(self.conn.execute(
            "SELECT COUNT(*) AS n FROM chunks").fetchone()["n"])
        rows = max(0, int(vector_rows or 0))
        covered = (rows / chunks) if chunks else 0.0
        return {
            # True only when meaning-based search covers effectively all of it.
            # Not `rows > 0`: a store holding 5% of the corpus is not "ready",
            # and calling it ready is how a half-working search looks healthy.
            "vectors_ready": bool(chunks) and covered >= 0.95,
            "vector_rows": rows,
            "chunks_total": chunks,
            "coverage": round(covered, 4),
            "missing": max(0, chunks - rows),
        }

    def integrity_check(self) -> bool:
        row = self.conn.execute("PRAGMA integrity_check").fetchone()
        return str(row[0]).lower() == "ok"
