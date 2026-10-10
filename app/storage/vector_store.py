"""LanceDB vector store: table lifecycle, batched appends, ANN index.

Layer: L1

This store is DERIVED. Every row here can be regenerated from SQLite's `chunks`
table, so if the two ever disagree, SQLite wins and the vectors are rebuilt.
That asymmetry is what lets a crash mid-write be recoverable rather than fatal.

Index policy: a flat scan beats a badly trained ANN index on a small table, so
the IVF_PQ index is only created past a threshold, and retrained when the row
count roughly doubles.
"""

from __future__ import annotations

import math
import threading
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from types import TracebackType
from typing import Any, Iterable, Optional, Sequence, Type

from app.core.errors import AppErrorException, make_error
from app.core.logging import logger

__all__ = [
    "VectorStore", "TABLE_NAME", "INDEX_MIN_ROWS", "MAX_PARTITIONS",
    "COMPACT_EVERY_ROWS", "KEEP_VERSIONS_HOURS",
    "ImageVectorStore", "IMAGE_TABLE_NAME", "IMAGE_VECTOR_DIM",
    "VideoFrameVectorStore", "VIDEO_FRAME_TABLE_NAME", "FrameHit",
]

_log = logger.bind(component="storage.vectors")

TABLE_NAME = "chunks"

#: How stale a held-open table may be before LanceDB looks for a newer version.
#:
#: **Zero: every read checks.** LanceDB's default is never, so the window -
#: which holds this store open for days - never saw a row the index process
#: wrote after it opened. On 1 October 2026 that left every search
#: keyword-only for eight hours with no error anywhere. Measured on the owner's
#: 87,216-row store, 30 searches each: 216ms median without the check, 219ms
#: with it - inside the noise.
READ_CONSISTENCY = timedelta(0)

#: Work order 0h §1b: the CLIP image-vector table's name, in the same LanceDB
#: directory as `TABLE_NAME` - `lancedb.connect(uri)` opens one directory and
#: serves any number of named tables out of it, so this is a second table, not
#: a second store location.
IMAGE_TABLE_NAME = "image_vectors"

#: `Qdrant/clip-ViT-B-32-vision`'s output width - see `app/index/clip_embedder.py`.
#: Different from the text table's 384, which is the entire reason this is a
#: second table rather than a second row shape in the first one: LanceDB's
#: vector column is a fixed-width Arrow list, so one table cannot hold both.
IMAGE_VECTOR_DIM = 512

#: Below this many rows a brute-force scan is faster than a trained index.
INDEX_MIN_ROWS = 100_000

#: The most IVF partitions this build will ask for.
#:
#: `sqrt(rows)` is the standard heuristic, so this cap starts to bind at about
#: **16.8 million vectors** (`4096**2`) - which a 1.5TB corpus reaches. Past
#: that, partitions grow instead of multiplying and search slows, or loses
#: recall, in proportion. `_partitions_for` says so in the log the first time it
#: happens; moving the number needs the latency and recall measurements that
#: `docs/WORKORDER-terabyte-scale.md` section 6 asks for, not a guess.
MAX_PARTITIONS = 4096

#: Rows appended between compactions.
#:
#: **LanceDB never compacts itself, and nothing here ever asked it to.** Every
#: `add` writes a new fragment and every `delete` writes a new dataset version.
#: A 20-million-chunk index means tens of thousands of fragments and millions of
#: versions, and a scan has to open all of them - so the index gets slower every
#: run and never recovers. There was no `optimize` or `compact_files` call
#: anywhere in the codebase.
#:
#: 50,000 is a few minutes of indexing: often enough that fragments never pile
#: up, rare enough that the compaction cost stays a small fraction of the run.
COMPACT_EVERY_ROWS = 50_000

#: Dataset versions older than this are dropped during compaction. Old versions
#: are what make LanceDB time-travel possible and are of no use to this
#: application, but they are never collected on their own.
KEEP_VERSIONS_HOURS = 1

#: 2026-10-04. How often, and how far apart, `drop` asks again when Windows
#: refuses to delete the table's files because something has them open.
DROP_RETRIES = 3
DROP_RETRY_WAIT_S = 0.5


def _is_access_denied(exc: BaseException) -> bool:
    """Windows' "the file is open elsewhere", as LanceDB reports it."""
    text = str(exc).lower()
    return "access is denied" in text or "os error 5)" in text or "os error 32)" in text


class VectorStore:
    """Open and operate the LanceDB table.

        with VectorStore(settings.vector_path, dim=384) as vectors:
            vectors.add(chunk_ids, file_ids, embeddings, exts, mtimes)
            hits = vectors.search(query_vector, k=100)
    """

    # Class-level defaults for the deferred-connect machinery: a store built without
    # `__init__` (a test double, `__new__`) is an ordinary eager store, as it always was.
    _deferred: bool = False
    _connect_thread: Optional[threading.Thread] = None
    _connect_error: Optional[BaseException] = None

    def __init__(self, uri: Path, *, dim: int = 384, table_name: str = TABLE_NAME,
                 deferred: bool = False):
        self.uri = Path(uri)
        self.dim = int(dim)
        self.table_name = table_name
        #: Work order 0r item 2b. **Importing LanceDB is the largest single cost
        #: between the splash and the window** (it builds hundreds of pydantic
        #: models), and nothing the window paints needs it. A deferred store
        #: does nothing on `__enter__`; `warm()` connects on a background thread
        #: and *every* read of `_db` / `_table` waits for that to finish, so a
        #: search typed the instant the window appears still sees the connected
        #: store. Off by default: everything but the window opens it eagerly.
        self._deferred = bool(deferred)
        # Guards the hand-over between the one background connect thread and
        # every reader of `_db`/`_table`: without it two early callers could
        # each start a connect, and LanceDB opened twice on one directory is
        # two handles to the same files.
        self._connect_lock = threading.Lock()
        self._connect_thread: Optional[threading.Thread] = None
        self._connect_error: Optional[BaseException] = None
        self._db_value: Any = None
        self._table_value: Any = None
        self._indexed_at_rows = 0
        #: Said once per store, not once per rebuild - see `_partitions_for`.
        self._warned_partitions = False
        #: Rows this session has written, and the running total. Counting beats
        #: asking: `count_rows()` on the write path is a scan per batch.
        self._rows_added = 0
        self._approx_rows = 0
        self._since_compact = 0

    # -- deferred connection (work order 0r item 2b) ---------------------------

    def _settle(self, *, raise_error: bool) -> None:
        """Wait for a background connect, starting it first if nobody has.

        A no-op on the connecting thread itself (it is the one writing `_db`)
        and on every store that was never deferred.
        """
        if not self._deferred:
            return
        with self._connect_lock:
            thread = self._connect_thread
            if thread is None and self._db_value is None and self._connect_error is None:
                if not raise_error:
                    return
                thread = self._start_connect_locked()
        if thread is not None and thread is not threading.current_thread():
            thread.join()
        if raise_error and self._connect_error is not None:
            raise self._connect_error

    def _start_connect_locked(self) -> threading.Thread:
        thread = threading.Thread(target=self._connect_worker, name="vector-connect", daemon=True)
        self._connect_thread = thread
        thread.start()
        return thread

    def _connect_worker(self) -> None:
        # `BaseException`, not `Exception`: this runs on a daemon thread, where
        # a KeyboardInterrupt or SystemExit would otherwise die silently and
        # leave every later read waiting on a connect that never finished.
        # Kept and re-raised on the first read instead, so the caller that
        # needed the store is the one that sees why it is not there.
        try:
            self.connect()
        except BaseException as exc:                # noqa: BLE001 - re-raised at first use
            self._connect_error = exc

    def warm(self) -> None:
        """Start connecting in the background and return at once. Idempotent."""
        if not self._deferred:
            return
        with self._connect_lock:
            if self._connect_thread is None and self._db_value is None                     and self._connect_error is None:
                self._start_connect_locked()

    def wait(self) -> None:
        """Block until a background connect has finished; never raises."""
        self._settle(raise_error=False)
        with self._connect_lock:
            thread = self._connect_thread
        if thread is not None and thread is not threading.current_thread():
            thread.join()

    def deferred_error(self) -> Optional[BaseException]:
        """The error a background connect ended with, or None. Never blocks."""
        return self._connect_error

    @property
    def connected(self) -> bool:
        """True once `connect()` has finished, here or on the background thread.
        Never blocks, so a GUI poll (`main._watch_vector_connect`) can ask."""
        return self._db_value is not None

    @property
    def _db(self) -> Any:
        self._settle(raise_error=True)
        return self._db_value

    @_db.setter
    def _db(self, value: Any) -> None:
        self._db_value = value

    @property
    def _table(self) -> Any:
        self._settle(raise_error=True)
        return self._table_value

    @_table.setter
    def _table(self, value: Any) -> None:
        self._table_value = value

    # -- lifecycle -----------------------------------------------------------

    def connect(self) -> "VectorStore":
        """Open the LanceDB directory and the table, if it exists yet.

        Raises `ERR_MODEL_LOAD` when lancedb is not importable, `ERR_CONFIG_
        INVALID` when the directory cannot be opened or the stored vectors are
        not `dim` wide. Idempotent: a connected store returns itself.
        """
        # A deferred store that is being (or has been) connected in the
        # background is connected once, there - never twice.
        if self._connect_thread is not None:
            self._settle(raise_error=False)
        if self._db_value is not None:
            return self
        try:
            import lancedb
        except ImportError as exc:
            raise AppErrorException(make_error(
                "ERR_MODEL_LOAD", "storage.vectors",
                details=f"lancedb is not importable: {exc}",
                suggestion="Re-run the installer, or: venv\\Scripts\\python.exe -m pip install lancedb",
            )) from exc

        try:
            self.uri.mkdir(parents=True, exist_ok=True)
            self._db = lancedb.connect(str(self.uri),
                                       read_consistency_interval=READ_CONSISTENCY)
        except Exception as exc:  # noqa: BLE001 - third-party raises broadly
            raise AppErrorException(make_error(
                "ERR_CONFIG_INVALID", "storage.vectors",
                key="VECTOR_PATH", reason=f"could not open '{self.uri}'",
                details=f"{type(exc).__name__}: {exc}",
            )) from exc

        if self.table_name in self._list_tables():
            self._table = self._db.open_table(self.table_name)
            self._verify_dimension()
            self._indexed_at_rows = self.count()
            # Corrected from the table on every open, so the running total can
            # never drift further than one session.
            self._approx_rows = self._indexed_at_rows
        return self

    def _open_if_created_since(self) -> None:
        """Open the table if another process has created it since `connect`.

        `connect` opens the table only if it is already there, and nothing
        looked again - so a window opened before the first index run, or
        during one that rebuilt the store, answered every search from an
        empty store until it was restarted. One directory listing, paid only
        while there is no table.
        """
        if self._table is not None or self._db_value is None:   # waits for a deferred connect
            return
        try:
            if self.table_name not in self._list_tables():
                return
            self._table = self._db.open_table(self.table_name)
        except Exception as exc:                  # noqa: BLE001 - try again next read
            _log.debug("the {} table appeared but could not be opened yet: {}",
                       self.table_name, exc)
            return
        self._verify_dimension()
        # 2026-10-04, code review: counted as `connect` counts an existing
        # table. Left at 0, `delete_by_file_ids` took the table for empty and
        # deleted nothing - a file removed later kept answering by meaning.
        self._indexed_at_rows = self.count()
        self._approx_rows = self._indexed_at_rows
        _log.info("the {} table was created since this store opened; using it now",
                  self.table_name)

    def _list_tables(self) -> list[str]:
        """Table names, across LanceDB API versions.

        `table_names()` is deprecated in 0.37 in favour of `list_tables()`,
        which returns a paginated response object rather than a list. Both are
        handled so a version bump does not silently break table detection.
        """
        db = self._db
        lister = getattr(db, "list_tables", None)
        if lister is not None:
            response = lister()
            tables = getattr(response, "tables", None)
            if tables is not None:
                return list(tables)
            return list(response)
        return list(db.table_names())

    def close(self) -> None:
        """Drop the handles. Safe to call more than once, and before connect."""
        # A connect still running must finish before the handles are dropped, or
        # it would re-open the store behind the close.
        self.wait()
        self._connect_error = None
        self._connect_thread = None
        self._table_value = None
        self._db_value = None

    def __enter__(self) -> "VectorStore":
        if self._deferred:
            return self
        return self.connect()

    def __exit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc: Optional[BaseException],
        tb: Optional[TracebackType],
    ) -> None:
        self.close()

    @property
    def db(self) -> Any:
        """The LanceDB connection, or `ERR_UNEXPECTED` when there is none."""
        if self._db is None:
            # **Two ways to get here, and the message used to name only one.**
            #
            # `_db` is None before `connect()` *and* after `close()`, and the
            # second is by far the more common: a background worker outliving
            # the window at shutdown. Saying "used before connect()" for a store
            # that had been open for ten minutes sends the reader looking for a
            # start-up bug that is not there.
            #
            # It also broke the shutdown filter in `ui/workers.py`, which
            # matches on this text: SqliteStore says ", or after close()" and
            # this did not, so an ordinary close printed "This is a bug" with a
            # request to send the log. Closing a window is not a bug.
            raise AppErrorException(make_error(
                "ERR_UNEXPECTED", "storage.vectors",
                details="VectorStore used before connect(), or after close().",
                suggestion=(
                    "If this appeared while closing the window, a background "
                    "search outlived the store and the message is harmless."
                ),
            ))
        return self._db

    @property
    def exists(self) -> bool:
        """Whether the table has been created. False before the first index run."""
        return self._table is not None

    def _verify_dimension(self) -> None:
        """A dimension mismatch means the model changed under an existing index.

        Silently appending 768-dim vectors to a 384-dim table would poison every
        future search, so it is refused with an actionable error instead.
        """
        try:
            field = self._table.schema.field("vector")
            actual = field.type.list_size
        except Exception:  # noqa: BLE001 - schema shape varies by version
            return
        if actual and int(actual) != self.dim:
            raise AppErrorException(make_error(
                "ERR_CONFIG_INVALID", "storage.vectors",
                key="EMBED_DIM",
                reason=f"the existing index holds {actual}-dimension vectors, but EMBED_DIM is {self.dim}",
                suggestion=(
                    "The embedding model changed. Either restore the previous EMBED_MODEL, "
                    "or rebuild the vectors from SQLite - they are derived data and safe to drop."
                ),
            ))

    # -- writing -------------------------------------------------------------

    def ensure_table(self) -> None:
        """Create the table with an explicit schema if it does not exist.

        Declaring the schema up front rather than inferring it from the first
        batch means the fixed-size vector width is enforced by Arrow itself, and
        an empty index is a real, inspectable thing rather than an absence.
        """
        if self._table is not None:
            return
        try:
            import pyarrow as pa

            schema = pa.schema([
                pa.field("chunk_id", pa.int64()),
                pa.field("file_id", pa.int64()),
                pa.field("vector", pa.list_(pa.float32(), self.dim)),
                pa.field("ext", pa.string()),
                pa.field("mtime_ns", pa.int64()),
            ])
            self._table = self.db.create_table(self.table_name, schema=schema)
        except AppErrorException:
            # **Already diagnosed. Let it through.**
            #
            # `self.db` raises `AppErrorException` when the store was never
            # connected. The broad handler below caught that finished error and
            # wrapped it in `ERR_UNEXPECTED` - and because
            # `str(AppErrorException)` renders only the headline, the inner
            # message was *destroyed* rather than nested. What the user got was:
            #
            #   [ERR_UNEXPECTED] An unexpected error occurred in storage.vectors.
            #     DETAIL: Could not create the 'chunks' table:
            #             AppErrorException: [ERR_UNEXPECTED] An unexpected
            #             error occurred in storage.vectors.
            #
            # A detail line describing itself, with the real cause -
            # "VectorStore used before connect()" - nowhere in it or in the
            # log. Wrapping an error that already carries a code and a fix
            # downgrades a diagnosable failure into an undiagnosable one.
            raise
        except Exception as exc:  # noqa: BLE001
            raise AppErrorException(make_error(
                "ERR_UNEXPECTED", "storage.vectors",
                details=f"Could not create the '{self.table_name}' table: {type(exc).__name__}: {exc}",
            )) from exc

    def add(
        self,
        chunk_ids: Sequence[int],
        file_ids: Sequence[int],
        vectors: Sequence[Sequence[float]],
        exts: Optional[Sequence[str]] = None,
        mtimes_ns: Optional[Sequence[int]] = None,
    ) -> int:
        """Append vectors. Returns the number of rows written."""
        if not (len(chunk_ids) == len(file_ids) == len(vectors)):
            raise AppErrorException(make_error(
                "ERR_UNEXPECTED", "storage.vectors",
                details=(
                    f"Mismatched batch: {len(chunk_ids)} chunk ids, "
                    f"{len(file_ids)} file ids, {len(vectors)} vectors."
                ),
            ))
        if not chunk_ids:
            return 0

        for position, vector in enumerate(vectors):
            if len(vector) != self.dim:
                raise AppErrorException(make_error(
                    "ERR_UNEXPECTED", "storage.vectors",
                    details=(
                        f"Vector {position} has {len(vector)} dimensions, expected {self.dim}. "
                        "Writing it would corrupt every future search."
                    ),
                ))

        exts = exts or [""] * len(chunk_ids)
        mtimes_ns = mtimes_ns or [0] * len(chunk_ids)

        self.ensure_table()
        # **One Arrow table built directly from the batch, not a list of
        # per-chunk dicts for LanceDB to convert a second time.** Order 0b
        # index-tuning, section 6c. The dict-of-boxed-floats version below is
        # what this replaced:
        #
        #     rows = [{"chunk_id": int(chunk_ids[i]), "file_id": int(file_ids[i]),
        #              "vector": [float(x) for x in vectors[i]], ...}
        #             for i in range(len(chunk_ids))]
        #     self._table.add(rows)
        #
        # `self._table.add(rows)` still converts a list of dicts to Arrow
        # internally to match the schema `ensure_table` already declares
        # (`vector: list_(float32(), dim)`) - so the boxing loop above ran,
        # and then LanceDB's own conversion ran again on top of it. A batch
        # of 256 chunks at 384 dimensions boxed and reboxed roughly 98,000
        # floats for a column that was float32 the whole time.
        # `_arrow_table` builds the same schema once, from a stacked numpy
        # block, and hands LanceDB the finished `pyarrow.Table` directly.
        self._table.add(self._arrow_table(chunk_ids, file_ids, vectors, exts, mtimes_ns))

        # **Counted, not queried.** `maybe_create_index` called `count_rows()`
        # on every single batch - a full scan of a growing table, on the write
        # path, to answer a question that only matters when it crosses a
        # threshold. The running total is exact between reopens and is corrected
        # from the table whenever one happens.
        self._rows_added += len(chunk_ids)
        self._approx_rows += len(chunk_ids)
        self._since_compact += len(chunk_ids)

        # **`maybe_create_index()` is deliberately NOT called here any more.**
        #
        # Training IVF_PQ is minutes at 6.4M rows and tens of minutes at 12.8M,
        # and it ran *synchronously on the consumer thread* the moment a batch
        # happened to cross a growth threshold. The queues into that thread are
        # bounded, so every extraction worker blocks behind it, the progress
        # numbers stop moving, and the window reads as hung - with no pause
        # reason on screen, because nothing knew a pause had begun.
        #
        # Nothing needs the index mid-run. An unindexed table answers correctly
        # by brute force, which is what it was doing for the whole run up to
        # that point anyway. `Pipeline.run` already calls `maybe_create_index()`
        # at the end, where the wait is expected and nothing is queued behind
        # it, and `reembed` does the same.
        #
        # Compaction stays: it is bounded, incremental, and the fragmentation it
        # prevents makes the *rest of the run* slower if it is deferred.
        self.maybe_compact()
        return len(chunk_ids)

    def _arrow_table(
        self,
        chunk_ids: Sequence[int],
        file_ids: Sequence[int],
        vectors: Sequence[Sequence[float]],
        exts: Sequence[str],
        mtimes_ns: Sequence[int],
    ) -> "Any":
        """One `pyarrow.Table` per batch, built from arrays - no per-float
        Python boxing. Order 0b index-tuning, section 6c.

        `vectors` arrives three different shapes across this codebase's
        callers - a 2D `numpy.ndarray` straight from `Embedder.embed()`, a
        Python `list` of 1D numpy rows from `Embedder.embed_all()`
        (`Pipeline._embed_texts`'s dedup path re-indexes them into a plain
        list), or a `list[list[float]]` from the image lane
        (`clip_embedder.py`, which returns plain floats) and from hand-built
        test data. `np.asarray(vectors, dtype=np.float32)` accepts all three
        and produces one contiguous `(n, dim)` block either way - the
        dimension check above has already proved every row is `self.dim`
        wide, so the reshape below cannot silently misalign a ragged input.

        Chunk and file ids go through `numpy` too, for the same reason:
        two `int64` columns of length n cost nothing next to the vector
        column, but building them the same way keeps this one function
        boxing-free rather than fixing three quarters of the problem.
        """
        import numpy as np
        import pyarrow as pa

        stacked = np.asarray(vectors, dtype=np.float32)
        flat_values = pa.array(np.ascontiguousarray(stacked).reshape(-1),
                                type=pa.float32())
        vector_array = pa.FixedSizeListArray.from_arrays(flat_values, self.dim)

        return pa.Table.from_arrays(
            [
                pa.array(np.asarray(chunk_ids, dtype=np.int64), type=pa.int64()),
                pa.array(np.asarray(file_ids, dtype=np.int64), type=pa.int64()),
                vector_array,
                pa.array([str(e) for e in exts], type=pa.string()),
                pa.array(np.asarray(mtimes_ns, dtype=np.int64), type=pa.int64()),
            ],
            schema=self._table.schema,
        )

    def maybe_compact(self, *, force: bool = False) -> bool:
        """Merge fragments and drop old versions. True if it ran.

        **Never fatal.** A table that has not been compacted answers correctly,
        just more slowly, so a failure here is logged and the run continues -
        the same reasoning as the ANN index build above it.
        """
        if self._table is None:
            return False
        if not force and self._since_compact < COMPACT_EVERY_ROWS:
            return False

        self._since_compact = 0
        try:
            # `optimize` is the modern entry point and does both jobs. The
            # older `compact_files` is tried after it so this keeps working on
            # an installation that has not been upgraded.
            if hasattr(self._table, "optimize"):
                self._table.optimize(
                    cleanup_older_than=timedelta(hours=KEEP_VERSIONS_HOURS))
            elif hasattr(self._table, "compact_files"):
                self._table.compact_files()
                if hasattr(self._table, "cleanup_old_versions"):
                    self._table.cleanup_old_versions(
                        older_than=timedelta(hours=KEEP_VERSIONS_HOURS))
            else:
                _log.debug("this LanceDB has no compaction entry point")
                return False
        except Exception as exc:                 # noqa: BLE001 - see docstring
            _log.warning(
                "could not compact the vector index: {}. Search still works; the "
                "index will be slower until a later run compacts it.", exc)
            return False

        _log.info("compacted the vector index")
        return True

    def delete_by_file_ids(self, file_ids: Iterable[int]) -> None:
        """Remove every vector belonging to these files.

        SQLite's cascade cannot reach here, so the caller must invoke this
        alongside delete_file() or the index will keep answering with rows whose
        source no longer exists.

        **A delete against an empty table is not free.** It writes a new dataset
        version regardless, and the pipeline called this once per document -
        including on a first index, where by definition there is nothing to
        remove. On a hundred thousand files that was a hundred thousand versions
        created to delete nothing at all.
        """
        ids = [int(i) for i in file_ids]
        if not ids:
            return
        self._open_if_created_since()       # 2026-10-04, code review: as `count` does
        if self._table is None or self._approx_rows <= 0:
            return
        self._table.delete(f"file_id IN ({', '.join(str(i) for i in ids)})")

    def delete_by_chunk_ids(self, chunk_ids: Iterable[int]) -> None:
        """Remove the vectors of these chunks, in one delete. A no-op on an
        empty list or before the table exists."""
        ids = [int(i) for i in chunk_ids]
        if not ids or self._table is None:
            return
        self._table.delete(f"chunk_id IN ({', '.join(str(i) for i in ids)})")

    def drop(self) -> None:
        """Drop the table. Safe: this is derived data, rebuildable from SQLite.

        **Every cached count is reset, not two of the four.** `_approx_rows` and
        `_since_compact` survived, so the run after a `reembed --all` believed
        an empty table still held thousands of rows. That re-enabled the
        per-document delete which `db17d1c` removed precisely because it costs a
        dataset version per document on a first index - and a rebuild *is* a
        first index. A stale count is not a cosmetic problem when other code
        branches on it.
        """
        if self._db is not None and self.table_name in self._list_tables():
            self._drop_or_empty()
        self._table = None
        self._indexed_at_rows = 0
        self._approx_rows = 0
        self._since_compact = 0

    def _drop_or_empty(self) -> None:
        """Drop the table; if Windows will not let its files go, empty it.

        **The owner's Reset index, 2026-10-04 13:02**: `drop_table` raised
        `Access is denied. (os error 5)` and the reset ended in "This is a
        bug" - with SQLite already cleared and every vector still there. On
        Windows a file another handle has open cannot be deleted: an index
        run, a folder watch still exiting, the MCP server's own store, a virus
        scanner. It did not reproduce in one process, so the cause is outside
        this store; what this can do is not depend on deleting files.

        Asked again `DROP_RETRIES` times - those locks are usually brief - and
        then every row is deleted instead. That writes a new version rather
        than removing files, so no lock stops it, and the table is empty
        either way. The files go at the next compaction. Any other failure is
        raised as before.
        """
        import time

        self._table = None                       # never hold our own handle open
        for attempt in range(DROP_RETRIES + 1):
            try:
                self._db.drop_table(self.table_name)
                return
            except (RuntimeError, OSError) as exc:
                if not _is_access_denied(exc):
                    raise
                if attempt < DROP_RETRIES:
                    time.sleep(DROP_RETRY_WAIT_S * (attempt + 1))
                    continue
                _log.warning(
                    "the vector table's files are open elsewhere, so it was "
                    "emptied rather than deleted; the space returns at the next "
                    "compaction: {}", exc)
        self._db.open_table(self.table_name).delete("true")

    # -- indexing ------------------------------------------------------------

    def maybe_create_index(self, *, force: bool = False) -> bool:
        """Create or retrain the ANN index when the table is big enough.

        Returns True if an index was built. Failure is not fatal: an unindexed
        table still answers correctly, just by brute force, so a build failure
        is swallowed rather than allowed to fail an indexing run.
        """
        if self._table is None:
            return False

        rows = self._approx_rows or self.count()
        if not force:
            if rows < INDEX_MIN_ROWS:
                return False
            if self._indexed_at_rows and rows < self._indexed_at_rows * self._growth_needed(rows):
                return False

        partitions = self._partitions_for(rows)
        try:
            self._table.create_index(
                metric="cosine",
                num_partitions=partitions,
                num_sub_vectors=max(1, self.dim // 16),
                replace=True,
            )
        except Exception:  # noqa: BLE001 - correctness does not depend on the index
            return False

        self._indexed_at_rows = rows
        return True

    def _partitions_for(self, rows: int) -> int:
        r"""`sqrt(rows)`, capped - and it says when the cap starts to bind.

        **The cap is a real limit, not a formality.** `sqrt(rows)` is the
        standard IVF heuristic and it holds until 4096 partitions, which is
        `4096**2` = about **16.8 million vectors**. Past that the partitions
        simply grow: at 20M each one holds ~4,900 vectors instead of ~4,500, at
        50M ~12,000, and search either slows in proportion or loses recall
        because `nprobe` covers a smaller share of the space.

        The cap is not moved here, and deliberately: `docs/WORKORDER-terabyte-
        scale.md` §6 asks for query latency and recall to be *measured* at 5M,
        10M and 20M rows first, and this project has learned what happens to
        numbers chosen without measuring. What it does instead is say so, once
        per build, so the moment the heuristic stops being followed is a line in
        the log rather than a slow search nobody can explain.
        """
        wanted = int(math.sqrt(rows))
        if wanted > MAX_PARTITIONS and not self._warned_partitions:
            self._warned_partitions = True
            _log.warning(
                "{:,} vectors would want {:,} IVF partitions but the cap is "
                "{:,}, so each partition now holds about {:,.0f} vectors "
                "instead of the {:,.0f} the heuristic asks for. Search stays "
                "correct; it gets slower, or loses recall, in proportion. "
                "Measure latency and recall before raising the cap - see "
                "docs/WORKORDER-terabyte-scale.md section 6.",
                rows, wanted, MAX_PARTITIONS,
                rows / MAX_PARTITIONS, rows / max(1, wanted),
            )
        return max(1, min(MAX_PARTITIONS, wanted))

    def _growth_needed(self, rows: int) -> int:
        r"""How much the table must grow before the index is rebuilt.

        **Doubling, until the cap binds - then four times.**

        "Retrain when rows double" means a rebuild at 100k, 200k, 400k ... 12.8M,
        and each one is expensive and lands *during* indexing, when the machine
        is already busy. That is the right trade while the index is genuinely
        getting better: below the cap, doubling the rows means `sqrt` asks for
        41% more partitions, so the rebuild changes the index's shape.

        Above the cap it does not. The partition count is pinned at
        `MAX_PARTITIONS` however many rows arrive, so a rebuild only reassigns
        vectors to the same number of centroids - worth doing as the data
        drifts, but not at every doubling, and least of all at the sizes where
        a rebuild costs the most.

        Four is not measured, and is not pretending to be: it is half as often,
        chosen because the thing the rebuild used to buy has stopped being
        bought. The measurement §6 asks for is the one that should replace it.
        """
        return 2 if math.sqrt(rows) <= MAX_PARTITIONS else 4

    # -- reading -------------------------------------------------------------

    def search(
        self,
        vector: Sequence[float],
        *,
        k: int = 100,
        where: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """ANN search. Returns rows without the vector payload.

        An empty table returns [] rather than raising: searching before the
        first index run is a normal state, not an error.
        """
        self._open_if_created_since()
        if self._table is None:
            return []
        if len(vector) != self.dim:
            raise AppErrorException(make_error(
                "ERR_UNEXPECTED", "storage.vectors",
                details=f"Query vector has {len(vector)} dimensions, expected {self.dim}.",
            ))

        query = self._table.search([float(x) for x in vector]).limit(k)
        if where:
            query = query.where(where, prefilter=True)

        results = []
        for row in query.to_list():
            row.pop("vector", None)
            results.append(row)
        return results

    def vector_for(self, chunk_id: int) -> Optional[list]:
        r"""The stored vector for one chunk, or None if it has none.

        **Read back rather than re-embedded**, which is what "more like this"
        needs: the passage was embedded once at index time, and embedding it
        again costs a model load, tens of milliseconds, and - if the model has
        changed since - gives a vector that does not live in the same space as
        the rows it is about to be compared against.

        `search` strips `vector` from every row it returns, so this is the only
        way to get one back out.
        """
        if self._table is None:
            return None
        try:
            rows = (self._table.search()
                    .where(f"chunk_id = {int(chunk_id)}")
                    .limit(1).to_list())
        except Exception as exc:                   # noqa: BLE001 - a lookup
            _log.debug("could not read the vector for chunk {}: {}",
                       chunk_id, exc)
            return None
        for row in rows:
            found = row.get("vector")
            if found is not None:
                return [float(value) for value in found]
        return None

    def bad_vectors(self, batch_size: int = 8192) -> list[tuple[int, int]]:
        r"""`(chunk_id, file_id)` of every row whose vector is empty or not a number.

        2026-10-10, order 1h item 2a. Before 2026-09-30, DirectML embedding on the
        owner's Iris Xe returned all-zero vectors without raising, and they were
        written and flagged embedded - a passage nobody can find by meaning, and
        nothing that would ever notice. A full scan in batches (`batch_size` rows
        at a time, never the whole table in memory); reads only.
        """
        import numpy as np

        self._open_if_created_since()
        if self._table is None:
            return []
        found: list[tuple[int, int]] = []
        query = self._table.search().select(["chunk_id", "file_id", "vector"]).limit(None)
        for batch in query.to_batches(batch_size):
            column = batch.column("vector")
            values = np.asarray(column.values.to_numpy(zero_copy_only=False),
                                dtype=np.float32).reshape(len(column), -1)
            bad = ~np.isfinite(values).all(axis=1) | (np.abs(values).sum(axis=1) < 1e-6)
            if bad.any():
                chunk_ids = batch.column("chunk_id").to_pylist()
                file_ids = batch.column("file_id").to_pylist()
                found.extend((int(chunk_ids[i]), int(file_ids[i]))
                             for i in np.flatnonzero(bad))
        return found

    def count(self) -> int:
        """Rows in the table: a scan, so not for the write path (see `add`).
        0 when there is no table yet or LanceDB cannot count it."""
        self._open_if_created_since()
        if self._table is None:
            return 0
        try:
            return int(self._table.count_rows())
        except Exception:  # noqa: BLE001
            return 0

    def stats(self) -> dict[str, Any]:
        """Plain data for `doctor`, the diagnostic bundle and `cli stats`.
        Counts the rows, so it costs a scan."""
        return {
            "uri": str(self.uri),
            "table": self.table_name,
            "exists": self.exists,
            "dim": self.dim,
            "rows": self.count(),
            "indexed_at_rows": self._indexed_at_rows,
            "index_threshold": INDEX_MIN_ROWS,
        }


class ImageVectorStore(VectorStore):
    """Work order 0h §1b: the second LanceDB table, one row per image *file*.

    Same LanceDB directory as the text table (`VectorStore`), a different
    table name and a different width - 512 for `Qdrant/clip-ViT-B-32-vision`
    against 384 for the text tower. Subclassed rather than reimplemented
    because every guarantee this table needs already lives on `VectorStore`
    and depends on nothing about what a row represents:

    - **M6 (crash ordering)**: text passages are written PENDING, embedded,
      then marked INDEXED - never the reverse, so a crash mid-embed leaves a
      file that looks unfinished rather than one marked done with a hole in
      its vectors (`pipeline._embed_pending`, `pipeline._write_marker`'s
      flush-before-marker comment). The image lane's own feeder in
      `pipeline.py` follows the identical order against this table: the CLIP
      vector is written before the file's row is allowed to read as complete.
      Nothing about that ordering lives in `VectorStore` itself - it is a
      pipeline discipline - which is exactly why inheriting the class changes
      none of it: the same discipline applies unchanged to a second table.
    - **M8 (no synchronous IVF_PQ retrain)**: `VectorStore.add` never calls
      `maybe_create_index()` - only `maybe_compact()` runs inline, and index
      training happens once, at the end of a run. Inherited unchanged: an
      image batch cannot stall the pipeline the way a mid-run text retrain
      used to (tens of minutes at 6.4M/12.8M rows).
    - **H7 (batched delete, not one Lance version per file)**: `delete_by_file_ids`
      already takes an iterable of ids and issues one `IN (...)` delete rather
      than the one-version-per-file loop H7 removed. Deleting a folder of
      10,000 photos costs one new dataset version here too, not 10,000.

    **`chunk_id` is set equal to `file_id` on every write**, via `add_images`
    below. The column stays in the inherited schema so every tested method
    on `VectorStore` - `search`, `delete_by_file_ids`, `maybe_compact`,
    `maybe_create_index`, `vector_for` - needs no schema-shaped branching
    between the two tables. A second schema and a second copy of each of
    those methods would be exactly the "just copying code silently" this
    item warns against; there is no chunk-splitting concept for a photo the
    way there is for a passage of text, so `file_id` is already the whole key
    and reusing the column costs nothing.
    """

    def __init__(
        self,
        uri: Path,
        *,
        dim: int = IMAGE_VECTOR_DIM,
        table_name: str = IMAGE_TABLE_NAME,
        deferred: bool = False,
    ) -> None:
        super().__init__(uri, dim=dim, table_name=table_name, deferred=deferred)
        self._frames: Optional["VideoFrameVectorStore"] = None

    def video_frames(self) -> "VideoFrameVectorStore":
        """The per-frame table beside this one, opened once and shared.

        Work order 202626270515. **Reached through this store rather than passed
        to the pipeline as a third argument**, so the four places that build a
        pipeline (window, CLI, evaluate, tests) need no new parameter: whoever
        has the image table has the frame table, in the same LanceDB directory.
        """
        if self._frames is None:
            self._frames = VideoFrameVectorStore(self.uri, dim=self.dim)
            self._frames.connect()
        return self._frames

    def delete_by_file_ids(self, file_ids: Iterable[int]) -> None:
        """Remove a file's vectors - **and its frames**, which live in a second table.

        A video deleted or replaced must not go on answering "which moment looks
        like this" with pictures from a film that is gone. Chained here because
        every deletion path in the pipeline already calls this one method.
        """
        ids = [int(i) for i in file_ids]
        super().delete_by_file_ids(ids)
        if ids:
            try:
                self.video_frames().delete_by_file_ids(ids)
            except Exception as exc:               # noqa: BLE001 - housekeeping only
                _log.debug("frame vectors not cleared for {} file(s): {}", len(ids), exc)

    def maybe_compact(self, *, force: bool = False) -> bool:
        """Compact this table and, when it has been opened, the frame table.
        A frame-table failure is logged, never raised: housekeeping only."""
        done = super().maybe_compact(force=force)
        if self._frames is not None:
            try:
                done = self._frames.maybe_compact(force=force) or done
            except Exception as exc:               # noqa: BLE001
                _log.debug("frame table not compacted: {}", exc)
        return done

    def maybe_create_index(self, *, force: bool = False) -> bool:
        """Index this table and, when it has been opened, the frame table.
        Same contract as the base method: a failed build is never fatal."""
        done = super().maybe_create_index(force=force)
        if self._frames is not None:
            try:
                done = self._frames.maybe_create_index(force=force) or done
            except Exception as exc:               # noqa: BLE001
                _log.debug("frame table not indexed: {}", exc)
        return done

    def add_images(
        self,
        file_ids: Sequence[int],
        vectors: Sequence[Sequence[float]],
        exts: Optional[Sequence[str]] = None,
        mtimes_ns: Optional[Sequence[int]] = None,
    ) -> int:
        """Append one CLIP vector per image file. `file_id` is the whole key.

        A thin, file-id-only public surface over `VectorStore.add`, which
        still wants a `chunk_id` per row - satisfied here by handing it the
        same value as `file_id`, so callers of this table never have to know
        or invent a chunk id that does not mean anything for a photo.
        """
        return self.add(
            chunk_ids=file_ids, file_ids=file_ids, vectors=vectors,
            exts=exts, mtimes_ns=mtimes_ns,
        )


#: Work order 202626270515. One row per *picture taken from a video*.
VIDEO_FRAME_TABLE_NAME = "video_frame_vectors"

#: `chunk_id` on a frame row is `file_id * _MOMENTS + second`. A film is not
#: longer than 100,000 seconds (27 hours), so the second fits below the file id
#: and the row needs no schema of its own beyond the inherited one - which is what
#: lets `delete_by_file_ids`, compaction and indexing be the same tested code.
_MOMENTS = 100_000


@dataclass(frozen=True)
class FrameHit:
    """One picture from one video that looked like the query."""

    file_id: int
    seconds: int
    distance: float


class VideoFrameVectorStore(VectorStore):
    """The CLIP vector of every scene-change picture, keyed by file **and second**.

    The image table has one row per *file*, so a video is one vector there - the
    normalised mean of its pictures, good for "which video looks like this" and
    unable to say "which minute". This is the second answer: with a row per
    picture, "the birthday cake" finds the film **and 12:41**. Both tables are
    written; the mean stays because the image lane already reads it.
    """

    def __init__(self, uri: Path, *, dim: int = IMAGE_VECTOR_DIM,
                 table_name: str = VIDEO_FRAME_TABLE_NAME) -> None:
        super().__init__(uri, dim=dim, table_name=table_name)

    @staticmethod
    def key_for(file_id: int, seconds: float) -> int:
        """The `chunk_id` of one frame: `file_id * _MOMENTS + second`.

        The second is clamped into `[0, _MOMENTS)` so a film longer than the
        ceiling cannot roll a frame over into the next file's key range.
        """
        return int(file_id) * _MOMENTS + max(0, min(int(seconds), _MOMENTS - 1))

    def replace_frames(
        self, file_id: int, moments: Sequence[tuple[float, Sequence[float]]],
        *, ext: str = "", mtime_ns: int = 0,
    ) -> int:
        """Make `moments` - `(seconds, vector)` pairs - the file's frames. Returns rows.

        Delete then add, because a re-indexed film has different pictures (a new
        interval or cap) and a stale row at second 300 would answer for a scene
        that is no longer there. Two pictures in the same whole second are one
        row: the first wins, which is the earlier and so the one to seek to.
        """
        self.delete_by_file_ids([file_id])
        seen: dict[int, Sequence[float]] = {}
        for seconds, vector in moments:
            seen.setdefault(self.key_for(file_id, seconds), vector)
        if not seen:
            return 0
        keys = list(seen)
        return self.add(
            chunk_ids=keys, file_ids=[int(file_id)] * len(keys),
            vectors=[seen[k] for k in keys],
            exts=[ext] * len(keys), mtimes_ns=[int(mtime_ns)] * len(keys))

    def search_frames(
        self, vector: Sequence[float], *, k: int = 20, where: Optional[str] = None,
    ) -> list[FrameHit]:
        """The nearest pictures, nearest first, as `(file, second)`. [] on an empty table."""
        hits = []
        for row in self.search(vector, k=k, where=where):
            key = int(row["chunk_id"])
            hits.append(FrameHit(
                file_id=key // _MOMENTS, seconds=key % _MOMENTS,
                distance=float(row.get("_distance", 0.0))))
        return hits
