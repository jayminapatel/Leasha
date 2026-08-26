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
from typing import Any, Iterable, Iterator, Mapping, Optional, Sequence, Type

from app.core.errors import AppError, AppErrorException, make_error
from app.core.identifiers import symbol_tokens
from app.core.logging import logger
from app.storage.migrations import CURRENT_VERSION, apply_migrations, read_version

__all__ = ["SqliteStore", "FileRecord", "ChunkRecord", "FileStatus"]

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


def _like_escape(value: str) -> str:
    r"""Make `value` a literal inside a `LIKE ... ESCAPE '\'` pattern.

    `%` and `_` are wildcards in `LIKE`, and both are ordinary characters in a
    Windows path and an email address: `Q1_2024%_final.pst` is a filename
    somebody has. Unescaped, it matches - and in a *delete* it deletes - rows
    belonging to entirely unrelated files.

    The backslash goes first, or escaping `%` would then have its own escape
    escaped. One function so the three call sites that already do this inline
    cannot drift apart from each other.
    """
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


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

    ALL = (PENDING, INDEXED, SKIPPED, FAILED, NAME_ONLY)


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

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "FileRecord":
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
        self._open: dict[int, sqlite3.Connection] = {}
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
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = %d" % int(self._timeout * 1000))

        self._open[threading.get_ident()] = conn
        return conn

    def connect(self) -> "SqliteStore":
        with self._conns_lock:
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

    def close(self) -> None:
        # Both, in the lock order set out in `__init__`: wait for an in-flight
        # write to finish rather than closing the connection underneath it.
        with self._write_lock, self._conns_lock:
            self._closed = True
            for conn in self._open.values():
                try:
                    conn.execute("PRAGMA optimize")
                except sqlite3.Error:
                    pass
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

        with self._conns_lock:
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
    ) -> int:
        r"""Insert or update one file row. Returns its id.

        `repo_id` is additive and optional: every existing caller keeps working
        and writes NULL, which is what a file outside any repository is.

        **`NO_REPO` is how a caller says "no repository" and means it.** NULL
        cannot: the `ON CONFLICT` below COALESCEs it so that callers which know
        nothing about repositories - `_record_skip`, the PST path, every test -
        cannot blank an attribution the indexer established. That guard is
        right on its own and became a trap in combination with two others; see
        `forget_repo`. A defensive default that cannot be overridden is not a
        guard, it is a one-way door, so this is the door's handle.
        """
        if status not in FileStatus.ALL:
            raise AppErrorException(make_error(
                "ERR_UNEXPECTED", "storage.sqlite",
                details=f"Invalid file status '{status}'.",
            ))

        clearing = 1 if repo_id == NO_REPO else 0
        stored_repo = None if clearing else repo_id

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
                     status, source_kind, repo_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(path) DO UPDATE SET
                    parent_dir   = excluded.parent_dir,
                    ext          = excluded.ext,
                    size_bytes   = excluded.size_bytes,
                    mtime_ns     = excluded.mtime_ns,
                    content_hash = COALESCE(excluded.content_hash, files.content_hash),
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
                    END
                """,
                (str(path), parent_dir, ext, size_bytes, mtime_ns,
                 content_hash, status, source_kind, stored_repo, clearing),
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
            clause = (f"AND ext IN ({','.join('?' * len(wanted))})"
                      if wanted else "")
            return [dict(row) for row in self.conn.execute(
                f"""SELECT id, path, ext, size_bytes, mtime_ns, status, skip_code,
                           source_kind, 0.0 AS score
                    FROM files
                    WHERE source_kind = 'file' {clause}
                    ORDER BY mtime_ns DESC
                    LIMIT ?""",
                [*wanted, max(1, int(limit))],
            )]

        # A trigram index takes the query as a literal string, so the whole
        # thing is quoted and internal quotes are doubled. No user input reaches
        # the FTS expression parser, which is what makes this crash-proof
        # against `AND`, `*`, `"` and every other operator someone types by
        # accident while looking for a file.
        expression = '"' + cleaned.replace('"', '""') + '"'

        sql = """
            SELECT f.id, f.path, f.ext, f.size_bytes, f.mtime_ns, f.status,
                   f.skip_code, f.source_kind,
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
        params.append(limit)

        try:
            rows = self.conn.execute(sql, params).fetchall()
        except sqlite3.OperationalError:
            # An index built before v4, or a corrupt FTS table. An empty result
            # is the right answer; taking the window down with it is not.
            return []
        return [dict(row) for row in rows]

    def vocabulary_terms(
        self, pattern: str, *, limit: int = 200
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
        """
        try:
            rows = self.conn.execute(
                r"""SELECT term FROM chunks_vocab
                    WHERE term LIKE ? ESCAPE '\'
                    ORDER BY doc DESC, term
                    LIMIT ?""",
                (str(pattern), max(1, int(limit))),
            ).fetchall()
        except sqlite3.OperationalError as exc:
            _log.debug("vocabulary lookup unavailable: {}", exc)
            return []
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
        from app.storage.filters import file_filter_sql

        where, params = file_filter_sql(parsed)
        # Only ever `newest` or `oldest` out of the parser, so this is a choice
        # between two constants rather than anything interpolated.
        wants_sort = str(getattr(parsed, "sort", "") or "").lower()
        # Two spellings of one order: the listing below reads `files` directly,
        # while the scored query orders the *outer* select, where the column has
        # already been projected and `f.` no longer resolves.
        _dir = "ASC" if wants_sort == "oldest" else "DESC"
        by_date, by_date_outer = f"f.mtime_ns {_dir}", f"mtime_ns {_dir}"
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
            return [dict(row) for row in self.conn.execute(
                f"""SELECT f.id, f.path, f.ext, f.size_bytes, f.mtime_ns,
                           f.status, f.skip_code, f.source_kind, 0.0 AS score
                    FROM files f
                    WHERE f.source_kind = 'file' {where}
                    ORDER BY {by_date}
                    LIMIT ?""",
                [*params, capped],
            )]

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
            SELECT id, path, ext, size_bytes, mtime_ns, status, skip_code,
                   source_kind, MIN(score) AS score
            FROM (
                SELECT f.id AS id, f.path AS path, f.ext AS ext,
                       f.size_bytes AS size_bytes, f.mtime_ns AS mtime_ns,
                       f.status AS status, f.skip_code AS skip_code,
                       f.source_kind AS source_kind,
                       bm25(files_fts, 10.0, 1.0) AS score
                FROM files_fts
                JOIN files f ON f.id = files_fts.rowid
                WHERE files_fts MATCH ? {where}

                UNION ALL

                SELECT f.id, f.path, f.ext, f.size_bytes, f.mtime_ns,
                       f.status, f.skip_code, f.source_kind,
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
                named.sort(key=lambda row: int(row.get("mtime_ns") or 0),
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

    def iter_files(
        self, status: Optional[str] = None, *, source_kind: Optional[str] = None
    ) -> Iterator[FileRecord]:
        """Files, optionally narrowed. Filter in SQL, never in Python.

        `source_kind` matters at scale rather than for tidiness: an archive of
        200,000 emails is 200,000 rows, and a caller that wants only the few
        thousand real files would otherwise build a `FileRecord` for every
        message first and discard 98% of them.
        """
        clauses: list[str] = []
        params: list[Any] = []
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        if source_kind is not None:
            clauses.append("source_kind = ?")
            params.append(source_kind)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        for row in self.conn.execute(f"SELECT * FROM files{where} ORDER BY id", params):
            yield FileRecord.from_row(row)

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
                                        char_start, char_end, page, embedded)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 0)
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
                    ),
                )
                ids.append(int(cursor.lastrowid))
            self._bump_generation(conn)
        return ids

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
                   "quoted_removed")
        # has_attach is NOT NULL DEFAULT 0, so it cannot be passed through as
        # None when the caller omits it.
        defaults: dict[str, Any] = {"has_attach": 0}
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
            for table in ("entity_mentions", "entity_edges", "entities",
                          "search_hits", "searches", "files",
                          "files_fts", "repos"):
                try:
                    conn.execute(f"DELETE FROM {table}")
                except sqlite3.OperationalError:
                    # A table that does not exist in this schema version is not
                    # an error: there is nothing in it to delete.
                    continue
            # Cursors point at chunk ids that no longer exist. Left behind, the
            # next run would resume past the beginning of an empty index and
            # quietly index nothing.
            conn.execute(
                "DELETE FROM index_state WHERE key LIKE 'graph:%' OR key LIKE 'index:%'"
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

    def entity_ids_for(
        self, conn: sqlite3.Connection, rows: Sequence[tuple[str, str, str, str]]
    ) -> dict[str, int]:
        """Get-or-create by `key`; returns `{key: entity_id}` for every input.

        Rows are `(key, display, kind, source)`. Existing entities keep the
        display form they were first seen with - the alternative is the last
        chunk processed silently renaming a node, so a graph label depends on
        where the build happened to stop.

        One INSERT OR IGNORE and one SELECT rather than a query per row: this is
        called with a few hundred keys per batch, tens of thousands of times, so
        the round trips are the whole cost.

        Takes the connection because it must run inside the caller's transaction
        - see `commit_graph_batch` for why that is not optional.
        """
        if not rows:
            return {}
        conn.executemany(
            "INSERT OR IGNORE INTO entities (key, display, kind, source) VALUES (?, ?, ?, ?)",
            rows,
        )
        keys = [row[0] for row in rows]
        found: dict[str, int] = {}
        for start in range(0, len(keys), 500):  # SQLite caps host parameters
            window = keys[start:start + 500]
            placeholders = ",".join("?" * len(window))
            for record in conn.execute(
                f"SELECT id, key FROM entities WHERE key IN ({placeholders})", window
            ):
                found[record["key"]] = int(record["id"])
        return found

    def commit_graph_batch(
        self,
        *,
        entities: Sequence[tuple[str, str, str, str]],
        mentions: Sequence[tuple[str, int, int, int]],
        pairs: Mapping[tuple[str, str], int],
        cursor: int,
        cursor_key: str = "graph:cursor",
    ) -> dict[str, int]:
        """Write one batch of the graph build, cursor included, atomically.

        **The atomicity is the point, not a nicety.** Edge weights *accumulate* -
        `weight = weight + excluded.weight` - so they are not idempotent, and a
        crash between writing weights and moving the cursor would replay the
        batch and count every pair in it twice. Nothing would raise; the graph
        would simply be wrong, more wrong the more often the build was
        interrupted, and there is no later check that could detect it. One
        transaction makes replay impossible rather than merely unlikely.

        Mentions are keyed by entity *key* rather than id, because the caller
        cannot know the ids until this transaction has assigned them.

        `cursor_key` exists because two jobs walk the same chunks independently -
        the co-occurrence build and the LLM enrichment - and they are nowhere
        near each other's position. Sharing one cursor would mean whichever ran
        last dictated where the other resumed, silently skipping every chunk in
        between.

        Accumulating pair weights in SQL instead of a dict held to the end is
        what keeps a 100GB corpus inside memory: the pair table is far larger
        than the entity table, and holding it is how this dies at hour three.
        """
        with self.write() as conn:
            ids = self.entity_ids_for(conn, entities)
            if mentions:
                conn.executemany(
                    "INSERT INTO entity_mentions (entity_id, chunk_id, file_id, count) "
                    "VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(entity_id, chunk_id) DO UPDATE SET count = excluded.count",
                    [
                        (ids[key], chunk_id, file_id, count)
                        for key, chunk_id, file_id, count in mentions
                        if key in ids
                    ],
                )
            edge_rows = []
            for (key_a, key_b), weight in pairs.items():
                a_id, b_id = ids.get(key_a), ids.get(key_b)
                if a_id is None or b_id is None or a_id == b_id:
                    continue
                edge_rows.append((a_id, b_id, weight) if a_id < b_id else (b_id, a_id, weight))
            if edge_rows:
                conn.executemany(
                    "INSERT INTO entity_edges (a_id, b_id, weight) VALUES (?, ?, ?) "
                    "ON CONFLICT(a_id, b_id) DO UPDATE SET weight = weight + excluded.weight",
                    edge_rows,
                )
            conn.execute(
                "INSERT INTO index_state (key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
                "updated_at = excluded.updated_at",
                (cursor_key, str(cursor), int(time.time())),
            )
        return ids

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

    def recount_entities(self) -> None:
        """Recompute `mentions`, `chunk_count` and `doc_count` from the mentions.

        Derived rather than incremented, because incrementing across a resumed
        or partially replayed build is exactly where a count drifts from the rows
        it claims to describe - and a wrong `chunk_count` silently corrupts every
        PMI score without changing anything visible until the graph looks odd.
        """
        with self.write() as conn:
            conn.execute("""
                UPDATE entities SET
                    mentions    = COALESCE((SELECT SUM(count)              FROM entity_mentions m WHERE m.entity_id = entities.id), 0),
                    chunk_count = COALESCE((SELECT COUNT(*)                FROM entity_mentions m WHERE m.entity_id = entities.id), 0),
                    doc_count   = COALESCE((SELECT COUNT(DISTINCT file_id) FROM entity_mentions m WHERE m.entity_id = entities.id), 0)
            """)

    def retype_entities(self, rows: Sequence[tuple[str, str]]) -> int:
        """Apply LLM-assigned kinds to entities, by key. Returns rows changed.

        `source` becomes 'llm' at the same time, so the graph can always show
        which typing was a guess from capitalisation and which came from a model
        - and so a later run can revisit only the ones a model has not seen.

        Never creates: an entity the LLM named that co-occurrence missed was
        already inserted by `commit_graph_batch` in the same run. Keeping this
        to an UPDATE means it cannot be the path that admits an unvalidated name.
        """
        if not rows:
            return 0
        changed = 0
        with self.write() as conn:
            for key, kind in rows:
                cursor = conn.execute(
                    "UPDATE entities SET kind = ?, source = 'llm' "
                    "WHERE key = ? AND (kind != ? OR source != 'llm')",
                    (kind, key, kind),
                )
                changed += cursor.rowcount
        return changed

    def graph_chunk_total(self) -> int:
        """Distinct chunks the graph was actually built from - PMI's denominator.

        Counted from `entity_mentions` rather than taken from the build's own
        tally, because on a resumed build those differ: this run may have read
        400 chunks on top of 40,000 already in the graph. Using the smaller
        number inflates every probability by the same factor, which is the worst
        kind of wrong - every score shifts together, so nothing looks broken.

        Chunks that produced no entities at all are correctly excluded: they
        contribute to no probability in the model.
        """
        row = self.conn.execute(
            "SELECT COUNT(DISTINCT chunk_id) AS n FROM entity_mentions"
        ).fetchone()
        return int(row["n"]) if row else 0

    def entity_chunk_counts(self) -> dict[int, int]:
        return {
            int(row["id"]): int(row["chunk_count"])
            for row in self.conn.execute("SELECT id, chunk_count FROM entities")
        }

    def iter_edges_for_scoring(self, batch_size: int = 5000) -> Iterator[list[tuple[int, int, int]]]:
        cursor = self.conn.execute("SELECT a_id, b_id, weight FROM entity_edges")
        while True:
            rows = cursor.fetchmany(batch_size)
            if not rows:
                return
            yield [(int(r["a_id"]), int(r["b_id"]), int(r["weight"])) for r in rows]

    def set_edge_scores(self, rows: Sequence[tuple[float, int, int]]) -> None:
        """`(pmi, a_id, b_id)` - argument order matches the UPDATE, not the table."""
        if not rows:
            return
        with self.write() as conn:
            conn.executemany(
                "UPDATE entity_edges SET pmi = ? WHERE a_id = ? AND b_id = ?", rows
            )

    def merge_contained_entities(self) -> int:
        """Fold a shorter name into a longer one it never appears without.

        The graph showed "AVEVA Group" and "AVEVA Group Limited" as two nodes,
        connected to each other and to all the same things. They are one company.

        **The test is evidential, not textual.** Being a prefix is not enough -
        "AVEVA" is a prefix of "AVEVA Group Limited" and is a far more important
        entity in its own right, appearing in twice as many documents. The rule
        is: merge the shorter into the longer only if **every chunk mentioning
        the shorter also mentions the longer**. That is the definition of "the
        short form is never used on its own here", which is the only evidence
        that would justify collapsing them.

        Word boundaries matter too. "PI" is a prefix of "PIPELINE" as a string,
        and nothing about that is a containment relationship.

        Returns the number of entities merged away. Mentions move to the
        survivor; edges are rebuilt by the caller's scoring pass.
        """
        rows = [
            (int(r["id"]), str(r["key"]), int(r["chunk_count"]))
            for r in self.conn.execute(
                "SELECT id, key, chunk_count FROM entities WHERE chunk_count > 0"
            )
        ]

        # **Bucketed by first word, not compared all-against-all.**
        #
        # The naive version is O(n^2) with a SQL query per surviving pair. On a
        # corpus of 200,000 emails the entity table runs to tens of thousands of
        # rows, which is billions of comparisons - it does not finish, and the
        # symptom is an index run that appears to hang at the very end.
        #
        # Containment that matters in practice shares a first word: "AVEVA
        # Group" inside "AVEVA Group Limited". A short form buried mid-phrase is
        # both rarer and less valuable, and is deliberately not chased.
        buckets: dict[str, list[tuple[int, str, int]]] = {}
        for row in rows:
            buckets.setdefault(row[1].split(" ", 1)[0], []).append(row)

        merges: list[tuple[int, int]] = []   # (loser, winner)
        for bucket in buckets.values():
            if len(bucket) > MERGE_BUCKET_LIMIT:
                # A pathological bucket - a word that starts thousands of
                # entities. Skipping it costs a few merges; grinding through it
                # costs the run.
                continue
            merges.extend(self._merges_within(bucket))

        if not merges:
            return 0
        return self._apply_merges(merges)

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

    def prune_graph(self, *, min_weight: int, min_pmi: float) -> int:
        """Drop weak edges, then entities no edge and no mention refers to.

        Returns the number of edges removed. Pruning happens after scoring, not
        during the build, because an edge's weight is not final until the last
        chunk has been seen - discarding it early would delete the pairs that
        become significant late in a corpus, which on an archive sorted roughly
        by date means anything recent.
        """
        with self.write() as conn:
            cursor = conn.execute(
                "DELETE FROM entity_edges WHERE weight < ? OR pmi IS NULL OR pmi <= ?",
                (min_weight, min_pmi),
            )
            removed = cursor.rowcount
            conn.execute("""
                DELETE FROM entities WHERE id NOT IN (SELECT a_id FROM entity_edges)
                                       AND id NOT IN (SELECT b_id FROM entity_edges)
            """)
        return int(removed)

    def clear_graph(self) -> None:
        """Wipe the graph. Safe at any time - it is derived from `chunks`."""
        with self.write() as conn:
            conn.execute("DELETE FROM entity_edges")
            conn.execute("DELETE FROM entity_mentions")
            conn.execute("DELETE FROM entities")

    def graph_stats(self) -> dict[str, Any]:
        entities = self.conn.execute("SELECT COUNT(*) AS n FROM entities").fetchone()["n"]
        edges = self.conn.execute("SELECT COUNT(*) AS n FROM entity_edges").fetchone()["n"]
        by_kind = {
            row["kind"]: int(row["n"])
            for row in self.conn.execute("SELECT kind, COUNT(*) AS n FROM entities GROUP BY kind")
        }
        return {
            "entities": int(entities),
            "edges": int(edges),
            "by_kind": by_kind,
            "cursor": self.get_state("graph:cursor", "0"),
        }

    def top_entities(self, limit: int = 200, kind: Optional[str] = None) -> list[dict[str, Any]]:
        sql = "SELECT id, key, display, kind, source, mentions, chunk_count, doc_count FROM entities"
        params: list[Any] = []
        if kind:
            sql += " WHERE kind = ?"
            params.append(kind)
        sql += " ORDER BY doc_count DESC, mentions DESC, key ASC LIMIT ?"
        params.append(limit)
        return [dict(row) for row in self.conn.execute(sql, params)]

    def edges_among(self, entity_ids: Sequence[int]) -> list[dict[str, Any]]:
        """Every scored edge whose *both* ends are in the given set.

        Both ends, not either: a node dangling off the visible set is an edge to
        something that is not drawn, which renders as a line into empty space.
        """
        if len(entity_ids) < 2:
            return []
        placeholders = ",".join("?" * len(entity_ids))
        return [
            dict(row)
            for row in self.conn.execute(
                f"SELECT a_id, b_id, weight, pmi FROM entity_edges "
                f"WHERE a_id IN ({placeholders}) AND b_id IN ({placeholders}) "
                f"ORDER BY pmi DESC",
                list(entity_ids) * 2,
            )
        ]

    def chunks_mentioning(self, entity_id: int, limit: int = 20) -> list[dict[str, Any]]:
        """The evidence behind a node: which passages it actually came from."""
        return [
            dict(row)
            for row in self.conn.execute(
                "SELECT c.id AS chunk_id, c.text, c.page, f.path, m.count "
                "FROM entity_mentions m "
                "JOIN chunks c ON c.id = m.chunk_id "
                "JOIN files  f ON f.id = m.file_id "
                "WHERE m.entity_id = ? ORDER BY m.count DESC, c.id ASC LIMIT ?",
                (entity_id, limit),
            )
        ]

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

    def distinct_values(self, kind: str, *, prefix: str = "",
                        limit: int = 40) -> list[str]:
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
        table = {
            "ext": ("SELECT ext AS v, COUNT(*) AS n FROM files "
                    "WHERE ext <> '' AND ext LIKE ? ESCAPE '\\' "
                    "GROUP BY ext ORDER BY n DESC, v LIMIT ?"),
            "folder": ("SELECT parent_dir AS v, COUNT(*) AS n FROM files "
                       "WHERE parent_dir <> '' AND parent_dir LIKE ? ESCAPE '\\' "
                       "GROUP BY parent_dir ORDER BY n DESC, v LIMIT ?"),
            "sender": ("SELECT sender AS v, COUNT(*) AS n FROM messages "
                       "WHERE sender IS NOT NULL AND sender <> '' "
                       "AND sender LIKE ? ESCAPE '\\' "
                       "GROUP BY sender ORDER BY n DESC, v LIMIT ?"),
            "repo": ("SELECT r.name AS v, COUNT(f.id) AS n FROM repos r "
                     "LEFT JOIN files f ON f.repo_id = r.id "
                     "WHERE r.name <> '' AND r.name LIKE ? ESCAPE '\\' "
                     "GROUP BY r.id, r.name ORDER BY n DESC, v LIMIT ?"),
        }.get(str(kind))
        if table is None:
            return []

        # Escaped, never interpolated: `%` and `_` typed by a person mean those
        # characters. The same rule `browse_messages` follows, for the same
        # reason - a person searching for a literal underscore should find it.
        escaped = (str(prefix or "").replace("\\", "\\\\")
                   .replace("%", "\\%").replace("_", "\\_"))
        rows = self.conn.execute(table, (f"%{escaped}%", max(1, int(limit))))
        return [str(row["v"]) for row in rows]

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

    def all_state(self) -> dict[str, str]:
        return {r["key"]: r["value"] for r in self.conn.execute("SELECT key, value FROM index_state")}

    # -- generation (search cache invalidation) ------------------------------

    def _bump_generation(self, conn: sqlite3.Connection) -> None:
        conn.execute("UPDATE index_generation SET generation = generation + 1 WHERE id = 1")

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
