"""SQLite metadata store: files, chunks, messages, FTS5, skip ledger, cursor.

Layer: L1

SQLite is the AUTHORITY. LanceDB is derived from it and can always be rebuilt
from `chunks`. Never the reverse.

Concurrency model: one connection, one write lock. SQLite in WAL mode allows
many concurrent readers alongside a single writer, which is exactly the shape of
this application - a pool of extraction workers reading, one writer committing.
Serialising writes in the process avoids `database is locked` entirely rather
than retrying around it.
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
from app.core.logging import logger
from app.storage.migrations import CURRENT_VERSION, apply_migrations, read_version

__all__ = ["SqliteStore", "FileRecord", "ChunkRecord", "FileStatus"]

_log = logger.bind(component="storage.sqlite")

#: Entities sharing a first word beyond which containment merging is skipped.
#: A bucket that large is a word that begins thousands of names, and grinding
#: through it quadratically costs far more than the handful of merges it would
#: find. Chosen so the worst bucket stays under a second.
MERGE_BUCKET_LIMIT = 400


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


class FileStatus:
    PENDING = "PENDING"
    INDEXED = "INDEXED"
    SKIPPED = "SKIPPED"
    FAILED = "FAILED"

    ALL = (PENDING, INDEXED, SKIPPED, FAILED)


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
        self._lock = threading.RLock()
        self._conn: Optional[sqlite3.Connection] = None

    # -- lifecycle -----------------------------------------------------------

    def connect(self) -> "SqliteStore":
        if self._conn is not None:
            return self

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
                check_same_thread=False,   # writes are serialised by self._lock
                isolation_level=None,      # explicit transactions only
            )
        except sqlite3.Error as exc:
            raise AppErrorException(make_error(
                "ERR_DB_LOCKED", "storage.sqlite",
                details=f"Could not open {self.db_path}: {exc}",
            )) from exc

        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = %d" % int(self._timeout * 1000))

        self._conn = conn
        apply_migrations(conn)
        return self

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.execute("PRAGMA optimize")
                except sqlite3.Error:
                    pass
                self._conn.close()
                self._conn = None

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
        return self._conn is not None

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise AppErrorException(make_error(
                "ERR_UNEXPECTED", "storage.sqlite",
                details="SqliteStore used before connect(), or after close().",
                suggestion=(
                    "If this appeared while closing the window, a background "
                    "search outlived the store and the message is harmless."
                ),
            ))
        return self._conn

    @property
    def schema_version(self) -> int:
        return read_version(self.conn)

    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        """One serialised write transaction. Rolls back on any exception."""
        with self._lock:
            conn = self.conn
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            else:
                conn.execute("COMMIT")

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
    ) -> int:
        """Insert or update one file row. Returns its id."""
        if status not in FileStatus.ALL:
            raise AppErrorException(make_error(
                "ERR_UNEXPECTED", "storage.sqlite",
                details=f"Invalid file status '{status}'.",
            ))

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
                     status, source_kind)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(path) DO UPDATE SET
                    parent_dir   = excluded.parent_dir,
                    ext          = excluded.ext,
                    size_bytes   = excluded.size_bytes,
                    mtime_ns     = excluded.mtime_ns,
                    content_hash = COALESCE(excluded.content_hash, files.content_hash),
                    status       = excluded.status,
                    source_kind  = excluded.source_kind
                """,
                (str(path), parent_dir, ext, size_bytes, mtime_ns,
                 content_hash, status, source_kind),
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
        with self.write() as conn:
            conn.execute(
                "UPDATE files SET status = ?, indexed_at = ?, skip_code = NULL, "
                "skip_detail = NULL WHERE id = ?",
                (FileStatus.INDEXED, int(time.time()), file_id),
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

    def browse_messages(
        self,
        *,
        sender: Optional[str] = None,
        recipient: Optional[str] = None,
        subject: Optional[str] = None,
        has_attachment: Optional[bool] = None,
        after: Optional[int] = None,
        before: Optional[int] = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        """Mail as a table: newest first, filtered by its own columns.

        **Not a search.** `search_files_by_name` and the hybrid engine both go
        through FTS and rank by relevance; this reads `messages` directly and
        orders by date, because a mail list is something you *browse* and the
        useful order is chronological. Ranking a mailbox by BM25 puts an
        eight-year-old thread above this morning's, which is never what somebody
        scanning a list wants.

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

        if sender:
            contains("m.sender", sender.strip())
        if recipient:
            contains("m.recipients", recipient.strip())
        if subject:
            contains("m.subject", subject.strip())
        if has_attachment is not None:
            clauses.append("m.has_attach = ?")
            params.append(1 if has_attachment else 0)
        if after is not None:
            clauses.append("m.sent_at >= ?")
            params.append(int(after))
        if before is not None:
            clauses.append("m.sent_at < ?")
            params.append(int(before))

        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = f"""
            SELECT m.file_id, m.subject, m.sender, m.recipients, m.sent_at,
                   m.has_attach, m.store_path, m.conversation,
                   f.path, f.size_bytes, f.status
            FROM messages m
            JOIN files f ON f.id = m.file_id
            {where}
            -- `sent_at IS NULL` rather than `NULLS LAST`, which needs SQLite
            -- 3.30. The bundled version is newer, but the version a user's
            -- Python happens to ship is not something this should depend on,
            -- and the two forms cost the same.
            ORDER BY m.sent_at IS NULL, m.sent_at DESC, m.file_id DESC
            LIMIT ?
        """
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
            # Without any filter at all, though, one or two characters match
            # nearly everything, and a hundred arbitrary rows is worse than an
            # empty list.
            if not ext:
                return []
            wanted = [e.lower().lstrip(".") for e in ext]
            return [dict(row) for row in self.conn.execute(
                f"""SELECT id, path, ext, size_bytes, mtime_ns, status, skip_code,
                           source_kind, 0.0 AS score
                    FROM files
                    WHERE ext IN ({','.join('?' * len(wanted))})
                      AND source_kind = 'file'
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

    def count_named_files(self) -> int:
        row = self.conn.execute("SELECT COUNT(*) AS n FROM files_fts").fetchone()
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
                    INSERT INTO chunks (file_id, ordinal, text, char_start, char_end, page, embedded)
                    VALUES (?, ?, ?, ?, ?, ?, 0)
                    """,
                    (
                        file_id,
                        chunk.get("ordinal", ordinal),
                        chunk["text"],
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
                   "sender", "recipients", "sent_at", "has_attach")
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
            # `files` cascades to chunks, messages and the FTS tables; the
            # entity tables are deprecated and empty but are cleared anyway so
            # a reset means what it says.
            for table in ("entity_mentions", "entity_edges", "entities",
                          "search_hits", "searches", "files"):
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
        self._vacuum_quietly()
        return count

    def _vacuum_quietly(self) -> None:
        """Give the space back. A failure here is not worth reporting.

        Without it the database file stays the size it was, and somebody who
        just deleted a 4GB index to free space would find they had not.
        """
        try:
            self.conn.execute("VACUUM")
        except sqlite3.Error as exc:
            _log.debug("vacuum after reset skipped: {}", exc)

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

    def integrity_check(self) -> bool:
        row = self.conn.execute("PRAGMA integrity_check").fetchone()
        return str(row[0]).lower() == "ok"
