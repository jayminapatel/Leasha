"""The indexing pipeline: point it at 100GB, walk away, come back to an index.

Layer: L3

**Shape.** A bounded priority queue fed by the walker, N extraction workers, and
one consumer that embeds and writes. Extraction is I/O and parser bound and
parallelises well. Embedding is not parallelised here on purpose: ONNX already
uses every core inside one `embed()` call, so running several would contend for
the same threads and get slower. SQLite has one writer by design. So the shape
is fan-out, fan-in - which is also the only shape whose failure modes are
tractable.

**Both queues are bounded**, which is what stops a fast walker from building a
million-entry list in memory while a slow embedder falls behind. Backpressure is
not a nicety at this scale; without it the process dies of memory somewhere
around hour three, having written nothing.

**Resumability is the `files` table, not a position.** A killed run restarts,
re-walks, and finds most files already `INDEXED` with matching mtime and hash -
so it skips them for the cost of a `stat()`. That is more robust than a saved
offset, which goes wrong the moment the corpus changes underneath it, and it
falls straight out of the incremental logic that has to exist anyway. The cursor
in `index_state` is progress reporting for the UI, not the mechanism.

**A skip is a row, not an exception.** Every failure is recorded against the file
with its `AppError` code, so the skipped-files panel can group thousands of them
by cause, and so the next incremental pass can retry the ones worth retrying.

**Order of writes matters.** Chunks and their vectors are written before the file
is marked `INDEXED`. A crash between the two leaves a file that looks unfinished
and gets redone - which is correct. The reverse would leave a file marked done
with no chunks, invisible to search and never retried.
"""

from __future__ import annotations

import queue
import shutil
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

from app.core.errors import AppError, AppErrorException, make_error, to_app_error
from app.core.logging import logger
from app.extract import chunk_document, extract
from app.index.embedder import Embedder
from app.index.walker import Candidate, WalkConfig, content_hash, has_changed, walk
from app.storage.sqlite_store import FileStatus, SqliteStore
from app.storage.vector_store import VectorStore

__all__ = ["Pipeline", "IndexStats", "PipelineConfig"]

#: Files between cursor commits. A crash costs at most this many files of work,
#: and the cursor write is one small UPDATE, so this can be low.
CHECKPOINT_EVERY = 50

#: Rows per LanceDB append. From the spec; large enough to amortise the write,
#: small enough that a crash loses little.
VECTOR_BATCH = 1000

#: Files between free-space checks. `shutil.disk_usage` is a syscall, so this is
#: cheap, but not free enough to do per file.
DISK_CHECK_EVERY = 200


@dataclass
class IndexStats:
    """What one run did. Returned, logged, and shown by the UI."""

    seen: int = 0
    indexed: int = 0
    unchanged: int = 0
    skipped: int = 0
    deleted: int = 0
    chunks: int = 0
    bytes_read: int = 0
    elapsed_s: float = 0.0
    stopped_early: Optional[AppError] = None
    skipped_by_code: dict[str, int] = field(default_factory=dict)

    @property
    def files_per_minute(self) -> float:
        return (self.indexed / self.elapsed_s * 60) if self.elapsed_s > 0 else 0.0

    @property
    def mb_per_minute(self) -> float:
        return (self.bytes_read / 1_048_576 / self.elapsed_s * 60) if self.elapsed_s > 0 else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "seen": self.seen, "indexed": self.indexed, "unchanged": self.unchanged,
            "skipped": self.skipped, "deleted": self.deleted, "chunks": self.chunks,
            "bytes_read": self.bytes_read, "elapsed_s": round(self.elapsed_s, 2),
            "files_per_minute": round(self.files_per_minute, 1),
            "mb_per_minute": round(self.mb_per_minute, 2),
            "skipped_by_code": dict(self.skipped_by_code),
            "stopped_early": self.stopped_early.code if self.stopped_early else None,
        }


@dataclass
class PipelineConfig:
    walk: WalkConfig
    workers: int = 0                       # 0 -> cpu_count - 1
    queue_size: int = 256
    checkpoint_every: int = CHECKPOINT_EVERY
    vector_batch: int = VECTOR_BATCH
    min_free_gb: int = 5
    #: Re-hash files whose mtime moved, rather than trusting mtime alone.
    verify_hash: bool = True
    #: Remove rows for files that no longer exist. Off for a partial run over a
    #: subset of roots, where "missing" only means "not in this walk".
    prune_missing: bool = True
    #: Retry files previously skipped as locked - the program holding them may
    #: well have closed since.
    retry_locked: bool = True

    def worker_count(self) -> int:
        if self.workers > 0:
            return self.workers
        import os

        return max(1, (os.cpu_count() or 2) - 1)


@dataclass
class _Extracted:
    """One file's worth of work, ready to embed and write."""

    candidate: Candidate
    content_hash: Optional[str]
    chunks: list[dict[str, Any]] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)
    source_kind: str = "file"
    warnings: tuple[AppError, ...] = ()
    error: Optional[AppError] = None
    unchanged: bool = False


_STOP = object()


class Pipeline:
    """Walk, extract, embed and write - resumably, and without falling over."""

    def __init__(
        self,
        store: SqliteStore,
        vectors: VectorStore,
        embedder: Embedder,
        config: PipelineConfig,
    ) -> None:
        self.store = store
        self.vectors = vectors
        self.embedder = embedder
        self.config = config
        self._log = logger.bind(component="index.pipeline")
        # Two different meanings, and conflating them cost a silent bug: the
        # prune step never ran, because run()'s cleanup sets the event and the
        # prune was guarded on it.
        self._stop = threading.Event()   # unwind the threads (always set at the end)
        self._interrupted = False        # the run was deliberately cut short

    def request_stop(self) -> None:
        """Ask the run to finish the file in flight and return cleanly.

        Used by the UI's pause button and by the disk guard. Not a kill: the
        point of stopping cleanly is that the cursor and every completed file
        survive, so resuming costs nothing.
        """
        self._interrupted = True
        self._stop.set()

    # -- the run ------------------------------------------------------------

    def run(
        self,
        *,
        on_progress: Optional[Callable[[IndexStats], None]] = None,
    ) -> IndexStats:
        stats = IndexStats()
        started = time.perf_counter()
        self._stop.clear()
        self._interrupted = False

        self.vectors.ensure_table()
        work: queue.PriorityQueue = queue.PriorityQueue(maxsize=self.config.queue_size)
        results: queue.Queue = queue.Queue(maxsize=self.config.queue_size)

        seen_paths: set[str] = set()
        producer = threading.Thread(
            target=self._produce, args=(work, stats, seen_paths), name="walker", daemon=True
        )
        workers = [
            threading.Thread(target=self._extract_worker, args=(work, results),
                             name=f"extract-{i}", daemon=True)
            for i in range(self.config.worker_count())
        ]

        producer.start()
        for worker in workers:
            worker.start()

        try:
            self._consume(results, workers, stats, on_progress)
        finally:
            self._stop.set()                    # unblock producer and workers
            _drain(work)
            _drain(results)
            producer.join(timeout=5)
            for worker in workers:
                worker.join(timeout=5)

        # Guarded on `_interrupted`, never on the event: an interrupted walk
        # did not see the whole corpus, so "missing" would mean "not reached
        # yet" and pruning would delete perfectly good rows.
        if self.config.prune_missing and not self._interrupted:
            stats.deleted = self._prune_missing(seen_paths)

        stats.elapsed_s = time.perf_counter() - started
        self.store.set_state("last_run", str(int(time.time())))
        self.store.set_state("last_run_stats", repr(stats.as_dict()))
        self.vectors.maybe_create_index()
        self._log.info("index run: {}", stats.as_dict())
        return stats

    # -- stage 1: walk ------------------------------------------------------

    def _produce(self, work: queue.PriorityQueue, stats: IndexStats, seen: set[str]) -> None:
        """Walk, decide what needs doing, and queue it. Runs in one thread.

        The unchanged decision happens *here*, before anything is queued, so an
        incremental pass over a settled corpus never wakes a worker at all.
        """
        sequence = 0
        try:
            for candidate in self._candidates():
                if self._stop.is_set():
                    break
                seen.add(str(candidate.path).lower())
                stats.seen += 1

                decision = self._classify(candidate)
                if decision is None:
                    stats.unchanged += 1
                    continue

                # (priority, sequence) keeps PriorityQueue from ever comparing
                # Candidates, which are not orderable, while preserving the
                # walker's deterministic order within a priority band.
                sequence += 1
                while not self._stop.is_set():
                    try:
                        work.put((candidate.priority, sequence, candidate, decision), timeout=0.25)
                        break
                    except queue.Full:
                        continue                # bounded on purpose: this is backpressure
        except Exception as exc:                # noqa: BLE001 - a walker crash must not hang the run
            self._log.error("walker failed: {}", exc)
        finally:
            for _ in range(self.config.worker_count()):
                work.put((10_000, sequence + 1, _STOP, None))

    def _candidates(self) -> Iterator[Candidate]:
        # Snapshotted BEFORE the walk, deliberately. Read lazily afterwards, the
        # query would pick up files this very run had just marked locked and
        # queue them a second time - doubling the work, double-counting the
        # skips, and retrying a file whose lock is by definition still held.
        retry = list(self._locked_candidates()) if self.config.retry_locked else []
        walked: set[str] = set()

        for candidate in walk(self.config.walk):
            walked.add(str(candidate.path).lower())
            yield candidate

        for candidate in retry:
            if str(candidate.path).lower() not in walked:
                yield candidate

    def _locked_candidates(self) -> Iterator[Candidate]:
        """Files skipped as locked last time. The program holding them may have
        closed since, and nothing else would ever look at them again."""
        for record in self.store.iter_files(status=FileStatus.SKIPPED):
            if record.skip_code != "ERR_FILE_LOCKED":
                continue
            path = Path(record.path)
            try:
                stat = path.stat()
            except OSError:
                continue
            yield Candidate(path=path, size_bytes=stat.st_size, mtime_ns=stat.st_mtime_ns,
                            priority=0)          # retried first: they are few and cheap

    def _classify(self, candidate: Candidate) -> Optional[Optional[str]]:
        """None if unchanged; otherwise the content hash (which may be None)."""
        record = self.store.get_file(str(candidate.path))
        changed, digest = has_changed(
            candidate,
            known_mtime_ns=record.mtime_ns if record else None,
            known_size=record.size_bytes if record else None,
            known_hash=record.content_hash if record else None,
            verify_hash=self.config.verify_hash,
        )
        if not changed and record is not None and record.status == FileStatus.INDEXED:
            return None
        return digest

    # -- stage 2: extract (N threads) ---------------------------------------

    def _extract_worker(self, work: queue.PriorityQueue, results: queue.Queue) -> None:
        while True:
            try:
                _priority, _sequence, candidate, digest = work.get(timeout=0.25)
            except queue.Empty:
                if self._stop.is_set():
                    return
                continue

            if candidate is _STOP:
                self._offer(results, _STOP)
                work.task_done()
                return
            try:
                self._offer(results, self._extract_one(candidate, digest))
            except Exception as exc:            # noqa: BLE001 - never let a worker die silently
                self._offer(results, _Extracted(
                    candidate=candidate, content_hash=digest,
                    error=to_app_error(exc, "index.pipeline", path=str(candidate.path)),
                ))
            finally:
                work.task_done()

    def _offer(self, results: queue.Queue, item: Any) -> None:
        """Hand a result to the consumer, giving up if the run is stopping.

        A plain `put` on a bounded queue blocks forever once the consumer has
        left, which would hang every worker and turn a clean stop into a hung
        process - the exact failure a pause button must not have.
        """
        while not self._stop.is_set():
            try:
                results.put(item, timeout=0.25)
                return
            except queue.Full:
                continue

    def _extract_one(self, candidate: Candidate, digest: Optional[str]) -> _Extracted:
        if digest is None:
            try:
                digest = content_hash(candidate.path)
            except OSError as exc:
                return _Extracted(candidate, None, error=to_app_error(
                    exc, "index.pipeline", code="ERR_FILE_LOCKED", path=str(candidate.path)))

        try:
            documents = list(extract(candidate.path))
        except AppErrorException as exc:
            return _Extracted(candidate, digest, error=exc.error)

        chunks: list[dict[str, Any]] = []
        meta: dict[str, Any] = {}
        warnings: tuple[AppError, ...] = ()
        source_kind = "file"
        ordinal = 0

        for document in documents:
            source_kind = document.source_kind
            meta = meta or document.meta
            warnings = warnings + document.warnings
            for chunk in chunk_document(document):
                chunks.append({
                    "ordinal": ordinal, "text": chunk.text, "page": chunk.page,
                    "char_start": chunk.char_start, "char_end": chunk.char_end,
                })
                ordinal += 1

        if not chunks:
            return _Extracted(candidate, digest, error=make_error(
                "ERR_NO_TEXT_LAYER", "index.pipeline", path=str(candidate.path),
                details="Extracted successfully but produced no chunks."))

        return _Extracted(candidate, digest, chunks=chunks, meta=meta,
                          source_kind=source_kind, warnings=warnings)

    # -- stage 3: embed and write (one thread: this one) --------------------

    def _consume(
        self,
        results: queue.Queue,
        workers: list[threading.Thread],
        stats: IndexStats,
        on_progress: Optional[Callable[[IndexStats], None]],
    ) -> None:
        finished = 0
        since_checkpoint = 0
        pending_vectors: list[tuple[int, int, list[float]]] = []

        while finished < len(workers):
            if self._stop.is_set():
                # Asked to stop - by the UI's pause button, or by the disk
                # guard. Everything already written stays written; the files
                # not reached are simply still PENDING, which is what a resumed
                # run looks for. Checked here, at the only point that commits
                # anything, so a stop can never land mid-write.
                break

            try:
                item = results.get(timeout=0.25)
            except queue.Empty:
                continue
            if item is _STOP:
                finished += 1
                continue

            if item.error is not None:
                self._record_skip(item)
                stats.skipped += 1
                stats.skipped_by_code[item.error.code] = (
                    stats.skipped_by_code.get(item.error.code, 0) + 1
                )
            else:
                pending_vectors.extend(self._write_one(item))
                stats.indexed += 1
                stats.chunks += len(item.chunks)
                stats.bytes_read += item.candidate.size_bytes

            if len(pending_vectors) >= self.config.vector_batch:
                self._flush_vectors(pending_vectors)

            since_checkpoint += 1
            if since_checkpoint >= self.config.checkpoint_every:
                since_checkpoint = 0
                self._checkpoint(item.candidate, stats)
                if on_progress is not None:
                    on_progress(stats)
                if not self._disk_ok(stats):
                    break

        self._flush_vectors(pending_vectors)
        if on_progress is not None:
            on_progress(stats)

    def _write_one(self, item: _Extracted) -> list[tuple[int, int, list[float]]]:
        """Chunks and vectors first, INDEXED last.

        A crash between them leaves a file that looks unfinished and is redone.
        The reverse would leave it marked done with no chunks - invisible to
        search, and never retried by anything.
        """
        candidate = item.candidate
        file_id = self.store.upsert_file(
            str(candidate.path),
            size_bytes=candidate.size_bytes,
            mtime_ns=candidate.mtime_ns,
            content_hash=item.content_hash,
            status=FileStatus.PENDING,
            source_kind=item.source_kind,
        )

        chunk_ids = self.store.replace_chunks(file_id, item.chunks)
        self.vectors.delete_by_file_ids([file_id])      # a re-index must not leave the old ones

        if item.meta:
            self._store_message_meta(file_id, item.meta)

        texts = [chunk["text"] for chunk in item.chunks]
        vectors = list(self.embedder.embed_all(texts))
        self.store.mark_indexed(file_id)

        for warning in item.warnings:
            self._log.warning("{} | {}", warning.message, warning.suggestion)

        return [(chunk_id, file_id, vector) for chunk_id, vector in zip(chunk_ids, vectors, strict=True)]

    def _store_message_meta(self, file_id: int, meta: dict[str, Any]) -> None:
        fields = {
            key: meta.get(key)
            for key in ("store_path", "entry_id", "conversation", "subject",
                        "sender", "recipients", "sent_at")
            if meta.get(key) is not None
        }
        if not fields:
            return
        fields["has_attach"] = int(meta.get("has_attach", 0) or 0)
        try:
            self.store.set_message(file_id, **fields)
        except Exception as exc:                # noqa: BLE001 - metadata is not worth a failed file
            self._log.warning("message metadata for file {} not stored: {}", file_id, exc)

    def _flush_vectors(self, pending: list[tuple[int, int, list[float]]]) -> None:
        if not pending:
            return
        self.vectors.add(
            chunk_ids=[cid for cid, _f, _v in pending],
            file_ids=[fid for _c, fid, _v in pending],
            vectors=[vec for _c, _f, vec in pending],
        )
        self.store.mark_embedded(cid for cid, _fid, _vec in pending)
        pending.clear()

    def _record_skip(self, item: _Extracted) -> None:
        assert item.error is not None
        candidate = item.candidate
        file_id = self.store.upsert_file(
            str(candidate.path),
            size_bytes=candidate.size_bytes,
            mtime_ns=candidate.mtime_ns,
            content_hash=item.content_hash,
            status=FileStatus.PENDING,
        )
        self.store.mark_skipped(file_id, item.error)

    # -- guards and bookkeeping ---------------------------------------------

    def _checkpoint(self, candidate: Candidate, stats: IndexStats) -> None:
        """Progress for the UI. The `files` table is what actually resumes."""
        self.store.set_state("cursor:last_path", str(candidate.path))
        self.store.set_state("cursor:indexed", str(stats.indexed))

    def _disk_ok(self, stats: IndexStats) -> bool:
        """Stop before filling the disk, preserving everything done so far."""
        try:
            free_gb = shutil.disk_usage(self.vectors.uri).free / 1_073_741_824
        except OSError:
            return True                          # cannot check is not a reason to stop
        if free_gb >= self.config.min_free_gb:
            return True

        stats.stopped_early = make_error(
            "ERR_DISK_SPACE", "index.pipeline",
            free_gb=f"{free_gb:.1f}", drive=str(self.vectors.uri),
        )
        self._log.error("{}", stats.stopped_early.render())
        self.request_stop()
        return False

    def _prune_missing(self, seen: set[str]) -> int:
        """Delete rows for files that are no longer on disk.

        Only over paths this walk covered: a run restricted to one root must not
        conclude that everything under the others has been deleted.
        """
        removed = 0
        for record in list(self.store.iter_files()):
            if record.source_kind != "file":
                continue                         # PST messages have no path to check
            path = Path(record.path)
            if str(path).lower() in seen or path.exists():
                continue
            self.vectors.delete_by_file_ids([record.id])
            self.store.delete_file(record.id)
            removed += 1
        return removed


def _drain(work: "queue.Queue[Any]") -> None:
    """Empty a queue so a blocked producer can finish and the thread can exit."""
    try:
        while True:
            work.get_nowait()
            work.task_done()
    except queue.Empty:
        return
