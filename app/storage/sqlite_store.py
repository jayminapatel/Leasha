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
from typing import Any, Iterable, Iterator, Optional, Sequence, Type

from app.core.errors import AppError, AppErrorException, make_error
from app.storage.migrations import CURRENT_VERSION, apply_migrations, read_version

__all__ = ["SqliteStore", "FileRecord", "ChunkRecord", "FileStatus"]


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
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise AppErrorException(make_error(
                "ERR_UNEXPECTED", "storage.sqlite",
                details="SqliteStore used before connect().",
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
        ext = (ext if ext is not None else as_path.suffix.lower().lstrip(".")) or ""
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
            self._bump_generation(conn)
        return int(row["id"])

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

    def delete_file(self, file_id: int) -> None:
        """Delete a file and everything derived from it.

        ON DELETE CASCADE removes chunks and messages; the chunks_ad trigger
        removes the FTS rows. LanceDB vectors are deleted separately by the
        caller, because SQLite cannot reach them.
        """
        with self.write() as conn:
            conn.execute("DELETE FROM files WHERE id = ?", (file_id,))
            self._bump_generation(conn)

    def delete_file_by_path(self, path: str) -> Optional[int]:
        record = self.get_file(path)
        if record is None:
            return None
        self.delete_file(record.id)
        return record.id

    def iter_files(self, status: Optional[str] = None) -> Iterator[FileRecord]:
        if status is None:
            rows = self.conn.execute("SELECT * FROM files ORDER BY id")
        else:
            rows = self.conn.execute("SELECT * FROM files WHERE status = ? ORDER BY id", (status,))
        for row in rows:
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
        results cannot be served after an incremental index run."""
        row = self.conn.execute("SELECT generation FROM index_generation WHERE id = 1").fetchone()
        return int(row["generation"]) if row else 0

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
