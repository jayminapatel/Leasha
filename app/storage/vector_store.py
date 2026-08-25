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
from datetime import timedelta
from pathlib import Path
from types import TracebackType
from typing import Any, Iterable, Optional, Sequence, Type

from app.core.errors import AppErrorException, make_error
from app.core.logging import logger

__all__ = [
    "VectorStore", "TABLE_NAME", "INDEX_MIN_ROWS",
    "COMPACT_EVERY_ROWS", "KEEP_VERSIONS_HOURS",
]

_log = logger.bind(component="storage.vectors")

TABLE_NAME = "chunks"

#: Below this many rows a brute-force scan is faster than a trained index.
INDEX_MIN_ROWS = 100_000

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


class VectorStore:
    """Open and operate the LanceDB table.

        with VectorStore(settings.vector_path, dim=384) as vectors:
            vectors.add(chunk_ids, file_ids, embeddings, exts, mtimes)
            hits = vectors.search(query_vector, k=100)
    """

    def __init__(self, uri: Path, *, dim: int = 384, table_name: str = TABLE_NAME):
        self.uri = Path(uri)
        self.dim = int(dim)
        self.table_name = table_name
        self._db: Any = None
        self._table: Any = None
        self._indexed_at_rows = 0
        #: Rows this session has written, and the running total. Counting beats
        #: asking: `count_rows()` on the write path is a scan per batch.
        self._rows_added = 0
        self._approx_rows = 0
        self._since_compact = 0

    # -- lifecycle -----------------------------------------------------------

    def connect(self) -> "VectorStore":
        if self._db is not None:
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
            self._db = lancedb.connect(str(self.uri))
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
        self._table = None
        self._db = None

    def __enter__(self) -> "VectorStore":
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
        if self._db is None:
            raise AppErrorException(make_error(
                "ERR_UNEXPECTED", "storage.vectors",
                details="VectorStore used before connect().",
            ))
        return self._db

    @property
    def exists(self) -> bool:
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

        rows = [
            {
                "chunk_id": int(chunk_ids[i]),
                "file_id": int(file_ids[i]),
                "vector": [float(x) for x in vectors[i]],
                "ext": str(exts[i]),
                "mtime_ns": int(mtimes_ns[i]),
            }
            for i in range(len(chunk_ids))
        ]

        self.ensure_table()
        self._table.add(rows)

        # **Counted, not queried.** `maybe_create_index` called `count_rows()`
        # on every single batch - a full scan of a growing table, on the write
        # path, to answer a question that only matters when it crosses a
        # threshold. The running total is exact between reopens and is corrected
        # from the table whenever one happens.
        self._rows_added += len(rows)
        self._approx_rows += len(rows)
        self._since_compact += len(rows)

        self.maybe_create_index()
        self.maybe_compact()
        return len(rows)

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
        if not ids or self._table is None or self._approx_rows <= 0:
            return
        self._table.delete(f"file_id IN ({', '.join(str(i) for i in ids)})")

    def delete_by_chunk_ids(self, chunk_ids: Iterable[int]) -> None:
        ids = [int(i) for i in chunk_ids]
        if not ids or self._table is None:
            return
        self._table.delete(f"chunk_id IN ({', '.join(str(i) for i in ids)})")

    def drop(self) -> None:
        """Drop the table. Safe: this is derived data, rebuildable from SQLite."""
        if self._db is not None and self.table_name in self._list_tables():
            self._db.drop_table(self.table_name)
        self._table = None
        self._indexed_at_rows = 0

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
            if self._indexed_at_rows and rows < self._indexed_at_rows * 2:
                return False

        partitions = max(1, min(4096, int(math.sqrt(rows))))
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

    def count(self) -> int:
        if self._table is None:
            return 0
        try:
            return int(self._table.count_rows())
        except Exception:  # noqa: BLE001
            return 0

    def stats(self) -> dict[str, Any]:
        return {
            "uri": str(self.uri),
            "table": self.table_name,
            "exists": self.exists,
            "dim": self.dim,
            "rows": self.count(),
            "indexed_at_rows": self._indexed_at_rows,
            "index_threshold": INDEX_MIN_ROWS,
        }
