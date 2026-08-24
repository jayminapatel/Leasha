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

import hashlib
import queue
import threading
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

from app.core.errors import AppError, AppErrorException, make_error, to_app_error
from app.core.logging import logger
from app.extract import chunk_document, extract
from app.extract.base import reads_externally
from app.index.embedder import Embedder
from app.index.resources import ResourceGovernor, ResourceLimits, SystemProbe, Verdict
from app.index.walker import Candidate, WalkConfig, content_hash, has_changed, walk
from app.storage.sqlite_store import FileStatus, SqliteStore
from app.storage.vector_store import VectorStore

class _Unchanged:
    """The "skip this file" answer from `_classify`.

    A singleton with a name rather than `None`, because `None` is also a real
    hash value and conflating the two silently skipped every PST in the corpus.
    """

    __slots__ = ()

    def __repr__(self) -> str:
        return "UNCHANGED"


#: Sentinel: this file has not changed since it was indexed.
UNCHANGED = _Unchanged()

__all__ = ["Pipeline", "IndexStats", "PipelineConfig"]

#: Files between cursor commits. A crash costs at most this many files of work,
#: and the cursor write is one small UPDATE, so this can be low.
CHECKPOINT_EVERY = 50

#: ...but also checkpoint on *time*, whichever comes first.
#:
#: A count alone is wrong whenever files are big or few. A folder of ten
#: documents plus one 100MB mail archive never reaches fifty, so the progress
#: callback fired exactly once - at the end - and the run showed a blank screen
#: for its entire duration. A person watching that has no way to tell it apart
#: from a hang, and the correct response to a hang is to kill it.
#:
#: Two seconds is often enough to look alive and rare enough that the cursor
#: write is free.
CHECKPOINT_SECONDS = 2.0

#: Rows per LanceDB append. From the spec; large enough to amortise the write,
#: small enough that a crash loses little.
VECTOR_BATCH = 1000

#: Chunks per embedding call. The single biggest throughput lever in the whole
#: pipeline: ONNX is efficient on large batches and spends its time on call
#: overhead on small ones. 256 chunks is roughly 400KB of text - nothing next to
#: the memory ceiling - and it keeps a crash's cost to a few seconds of work.
EMBED_BATCH = 256

#: Files between free-space checks. `shutil.disk_usage` is a syscall, so this is
#: cheap, but not free enough to do per file.
DISK_CHECK_EVERY = 200


@dataclass
class IndexStats:
    """What one run did. Returned, logged, and shown by the UI."""

    #: Files the walker looked at.
    seen: int = 0
    #: **Documents** written - which for an archive is messages, not files.
    #: The two are different units and were briefly reported as one, producing
    #: lines like "seen 8, indexed 17" that cannot both be files.
    indexed: int = 0
    #: Files skipped whole by the walker, before anything was read.
    unchanged: int = 0
    #: Documents *inside* a changed archive whose text had not moved. This is
    #: the number that makes re-indexing 30GB of mail cheap, and it is worth
    #: showing separately: 335 unchanged messages and 17 rewritten ones is a
    #: completely different story from 352 unchanged files.
    unchanged_documents: int = 0
    skipped: int = 0
    deleted: int = 0
    chunks: int = 0
    bytes_read: int = 0
    elapsed_s: float = 0.0
    #: Time spent deliberately waiting for the machine to be free, and how many
    #: times. Reported so a run that took four hours because it was being polite
    #: is not mistaken for a run that took four hours because it is slow.
    paused_seconds: float = 0.0
    pauses: int = 0
    #: What a worker is reading right now, and for how long. Set from the
    #: extraction threads and read from the consumer - a plain string swap,
    #: which is atomic enough for something only ever displayed.
    #:
    #: This exists because one 100MB mail archive is a *single file*: nothing
    #: reaches the consumer until the whole thing has been parsed, so without a
    #: name and a clock on screen the run is indistinguishable from a hang for
    #: however many minutes that takes.
    current: str = ""
    current_since: float = 0.0
    #: How many documents have come out of the current file. On an archive this
    #: is the message counter, and it is the difference between "working" and
    #: "hung" on the screen.
    current_item: int = 0
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
            "unchanged_documents": self.unchanged_documents,
            "skipped": self.skipped, "deleted": self.deleted, "chunks": self.chunks,
            "paused_s": round(self.paused_seconds, 1), "pauses": self.pauses,
            "current": self.current,
            "bytes_read": self.bytes_read, "elapsed_s": round(self.elapsed_s, 2),
            "files_per_minute": round(self.files_per_minute, 1),
            "mb_per_minute": round(self.mb_per_minute, 2),
            "skipped_by_code": dict(self.skipped_by_code),
            "stopped_early": self.stopped_early.code if self.stopped_early else None,
        }


@dataclass
class PipelineConfig:
    walk: WalkConfig
    #: 0 -> `resources.default_workers()`: half the cores, capped at four.
    #: Deliberately not `cpu_count - 1`; see `app/index/resources.py` for why
    #: that is the wrong answer on a machine somebody is using.
    workers: int = 0
    #: Memory, CPU, battery and disk ceilings. The indexer asks this before
    #: every checkpoint and pauses rather than competing with its owner.
    limits: Optional[ResourceLimits] = None
    queue_size: int = 256
    checkpoint_every: int = CHECKPOINT_EVERY
    #: Seconds between checkpoints, whichever limit is reached first. A count
    #: alone leaves a corpus of few large files showing nothing at all.
    checkpoint_seconds: float = CHECKPOINT_SECONDS
    vector_batch: int = VECTOR_BATCH
    #: Chunks gathered before one embedding call. Per-document embedding meant
    #: batches of ~3 for an email and ONNX throughput collapsed; a few hundred
    #: restores it without holding much text in memory.
    embed_batch: int = EMBED_BATCH
    min_free_gb: int = 5
    #: Re-hash files whose mtime moved, rather than trusting mtime alone.
    verify_hash: bool = True
    #: Remove rows for files that no longer exist. Off for a partial run over a
    #: subset of roots, where "missing" only means "not in this walk".
    prune_missing: bool = True
    #: Retry files previously skipped as locked - the program holding them may
    #: well have closed since.
    retry_locked: bool = True

    def resolved_limits(self) -> ResourceLimits:
        """Limits with `workers` and `min_free_gb` reconciled.

        Both exist in two places for backwards compatibility - callers built
        `PipelineConfig(workers=..., min_free_gb=...)` before there was a
        governor. An explicit field on the config wins, so no existing caller
        changes behaviour by upgrading.
        """
        limits = self.limits or ResourceLimits()
        if self.workers > 0:
            limits = replace(limits, workers=self.workers)
        if self.min_free_gb != ResourceLimits().min_free_gb:
            limits = replace(limits, min_free_gb=self.min_free_gb)
        return limits

    def worker_count(self) -> int:
        return self.resolved_limits().resolved_workers()


@dataclass
class _Extracted:
    """**One document's** worth of work, ready to embed and write.

    Not one *file*. A `.pst` yields one of these per message, and that
    distinction is the whole reason a 30GB archive is tractable:

    * memory stays bounded - the old version built `list(extract(path))` for the
      entire archive, then every chunk, then embedded the lot in a single call.
      A 100MB archive took minutes with nothing written and nothing on screen; a
      3GB one would simply run out of memory.
    * work is committed as it goes, so an interrupted archive keeps what it read.
    * a search result points at the message rather than at `2007.pst`.
    * re-indexing a changed archive re-reads only the messages that changed.
    """

    candidate: Candidate
    content_hash: Optional[str]
    #: What `files.path` should hold. The file's own path for an ordinary
    #: document; a per-message key for anything inside an archive.
    key: str = ""
    chunks: list[dict[str, Any]] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)
    source_kind: str = "file"
    warnings: tuple[AppError, ...] = ()
    error: Optional[AppError] = None
    unchanged: bool = False
    #: True on the first document from a file, so bytes are counted once per
    #: file rather than once per message.
    first_of_file: bool = True
    #: The closing record for a container: a row for the archive *itself*,
    #: carrying its mtime and size and holding no chunks.
    #:
    #: Without it nothing in `files` describes the `.pst`, so the walker has
    #: nothing to compare against and re-reads the whole archive on every run
    #: forever - destroying the one property the incremental design exists for.
    file_marker: bool = False

    @property
    def row_key(self) -> str:
        return self.key or str(self.candidate.path)


_STOP = object()


class Pipeline:
    """Walk, extract, embed and write - resumably, and without falling over."""

    def __init__(
        self,
        store: SqliteStore,
        vectors: VectorStore,
        embedder: Embedder,
        config: PipelineConfig,
        governor: Optional[ResourceGovernor] = None,
    ) -> None:
        self.store = store
        self.vectors = vectors
        self.embedder = embedder
        self.config = config
        # Injected in tests with a fake probe, so every pause and resume path is
        # exercised without needing a machine that is actually short of memory.
        self.governor = governor or ResourceGovernor(
            config.resolved_limits(),
            probe=SystemProbe(lambda: getattr(self.vectors, "uri", None)).read,
            on_state_change=self._on_throttle,
        )
        self._log = logger.bind(component="index.pipeline")
        self._throttle: Optional[Verdict] = None
        self._stats_ref = IndexStats()
        # Two different meanings, and conflating them cost a silent bug: the
        # prune step never ran, because run()'s cleanup sets the event and the
        # prune was guarded on it.
        self._stop = threading.Event()   # unwind the threads (always set at the end)
        self._interrupted = False        # the run was deliberately cut short

    def _on_throttle(self, found: Verdict) -> None:
        """Remember the last throttle so progress can say why it went quiet.

        A background job that slows down without saying so is indistinguishable
        from one that has hung, and the person watching will kill it.
        """
        self._throttle = found if found.action != "run" else None

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
        self._stats_ref = stats          # workers announce the file they are on
        started = time.perf_counter()
        self._stop.clear()
        self._interrupted = False

        # Below-normal CPU and background I/O priority, before a single file is
        # read. The cheapest courtesy available and the most effective: the
        # scheduler simply prefers whatever the person is actually doing.
        if self.governor.apply_priority():
            self._log.debug("running at below-normal priority")

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

                # Wait here, not in the consumer. This thread holds nothing but
                # one candidate path, so pausing it starves the workers of new
                # work while everything already in flight keeps draining - which
                # is what actually brings memory down.
                verdict = self.governor.wait_while_throttled(should_stop=self._stop.is_set)
                stats.paused_seconds = self.governor.paused_seconds
                stats.pauses = self.governor.pauses
                if verdict.action == "stop":
                    # Say why. Breaking silently here would end the run
                    # reporting complete success having indexed nothing - the
                    # exact failure shape that hid every PST for two days.
                    if not self._stop.is_set() and stats.stopped_early is None:
                        stats.stopped_early = make_error(
                            "ERR_DISK_SPACE", "index.pipeline",
                            free_gb="low", drive=str(self.vectors.uri),
                            details=verdict.reason,
                        )
                        self._log.error("{}", stats.stopped_early.render())
                        self.request_stop()
                    break

                decision = self._classify(candidate)
                if decision is UNCHANGED:
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
            # Loud, and recorded in the stats. The silent version of this cost a
            # whole run: it logged one line nobody saw and reported success.
            stats.stopped_early = to_app_error(
                exc, "index.pipeline",
                suggestion="The file scan stopped early, so some folders were not reached. "
                           "The files already indexed are safe - re-run to continue.",
            )
            self._log.error("walker stopped early: {}", stats.stopped_early.render())
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

    def _classify(self, candidate: Candidate) -> Optional[str]:
        """`UNCHANGED` to skip the file; otherwise its content hash, or None.

        **The sentinel is a distinct object, not `None`.** It used to be `None`,
        which is also the perfectly ordinary "changed, but there is no hash"
        answer for any file read through another application - a `.pst`, an
        `.ost`. So every archive was classified as unchanged **on the very first
        run**, before it had ever been indexed, and silently never indexed at
        all. It reported `unchanged`, not `skipped`, so nothing anywhere said a
        word about it. That is the bug behind "the Outlook file did not index":
        the run was a success by every number it printed.

        Never raises. This runs on the walker thread, where one escaping
        exception abandons every file not yet reached - which is precisely what
        happened on the first real run: a `.pst` held open by Outlook could not
        be hashed, the permission error escaped, and the whole index run ended
        having done nothing, reporting no skips and no error the user could see.
        """
        try:
            record = self.store.get_file(str(candidate.path))
            changed, digest = has_changed(
                candidate,
                known_mtime_ns=record.mtime_ns if record else None,
                known_size=record.size_bytes if record else None,
                known_hash=record.content_hash if record else None,
                # A file read through another application is held open by it, so
                # hashing its bytes fails - and those bytes are not what gets
                # parsed anyway. mtime and size are all there is, and enough.
                verify_hash=self.config.verify_hash and not reads_externally(candidate.path),
            )
        except Exception as exc:            # noqa: BLE001 - see the docstring
            self._log.warning(
                "could not classify {}, queuing it anyway: {}", candidate.path, exc
            )
            return ""                        # queue it; the worker reports properly

        if not changed and record is not None and record.status == FileStatus.INDEXED:
            return UNCHANGED
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
            self._stats_ref.current = candidate.path.name
            self._stats_ref.current_since = time.monotonic()
            self._stats_ref.current_item = 0
            try:
                for item in self._extract_stream(candidate, digest):
                    if self._stop.is_set():
                        break
                    self._offer(results, item)
            except Exception as exc:            # noqa: BLE001 - never let a worker die silently
                self._offer(results, _Extracted(
                    candidate=candidate, content_hash=digest,
                    error=to_app_error(exc, "index.pipeline", path=str(candidate.path)),
                ))
            finally:
                self._stats_ref.current = ""
                self._stats_ref.current_item = 0
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

    def _extract_stream(
        self, candidate: Candidate, digest: Optional[str]
    ) -> Iterator[_Extracted]:
        """Yield one `_Extracted` per document, as it is read.

        **A generator, not a list, and that is the entire point.** The previous
        version did `documents = list(extract(path))`, which for a `.pst` meant
        materialising every message and every attachment's text before making a
        single chunk - then chunking all of it, then embedding all of it in one
        call. A 100MB archive spent minutes doing that with nothing written,
        nothing committed and nothing on screen; a 3GB archive would have run
        out of memory before finishing.

        Streaming makes the unit of work a message. Memory stays flat, progress
        is visible, an interrupted run keeps what it read, and a search result
        names the email rather than the archive it came from.
        """
        if digest is None and not reads_externally(candidate.path):
            try:
                digest = content_hash(candidate.path)
            except OSError as exc:
                yield _Extracted(candidate, None, error=to_app_error(
                    exc, "index.pipeline", code="ERR_FILE_LOCKED", path=str(candidate.path)))
                return

        produced = 0
        seen_keys: set[str] = set()
        try:
            for index, document in enumerate(extract(candidate.path)):
                chunks: list[dict[str, Any]] = []
                for ordinal, chunk in enumerate(chunk_document(document)):
                    chunks.append({
                        "ordinal": ordinal, "text": chunk.text, "page": chunk.page,
                        "char_start": chunk.char_start, "char_end": chunk.char_end,
                    })

                if not chunks:
                    # One empty message in an archive is ordinary and silent.
                    # An empty *file* is a finding worth reporting - it is the
                    # scanned PDF case, and the person needs to know why their
                    # document is not searchable.
                    continue

                key = document.key
                if key in seen_keys:
                    # An extractor yielding many documents must give each a
                    # `virtual_path`. Without one they all share the file's path,
                    # every message overwrites the last, and the archive ends up
                    # as a single row holding only its final email - silently.
                    # Made unique rather than dropped: losing mail to a bug in an
                    # extractor is far worse than an ugly key, and the warning
                    # names the file so it can be fixed.
                    self._log.warning(
                        "{} produced document {} with a duplicate key {!r}; "
                        "the extractor is not setting virtual_path",
                        candidate.path.name, index, key,
                    )
                    key = f"{key}#{index}"
                seen_keys.add(key)

                produced += 1
                self._stats_ref.current_item = produced
                yield _Extracted(
                    candidate=candidate,
                    content_hash=digest,
                    key=key,
                    chunks=chunks,
                    meta=document.meta,
                    source_kind=document.source_kind,
                    warnings=document.warnings,
                    first_of_file=(index == 0),
                )
        except AppErrorException as exc:
            # A failure part-way through an archive costs the rest of that
            # archive, never the messages already handed over and written.
            yield _Extracted(candidate, digest, error=exc.error)
            return

        if produced == 0:
            yield _Extracted(candidate, digest, error=make_error(
                "ERR_NO_TEXT_LAYER", "index.pipeline", path=str(candidate.path),
                details="Extracted successfully but produced no text."))
            return

        if seen_keys != {str(candidate.path)}:
            # This file was a container. Close it with a row for the archive
            # itself so the next run can see it is unchanged and skip it whole.
            yield _Extracted(
                candidate=candidate, content_hash=digest,
                key=str(candidate.path), source_kind="archive",
                first_of_file=False, file_marker=True,
            )

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
        last_checkpoint = time.monotonic()
        pending_vectors: list[tuple[int, int, str]] = []

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
                # Nothing has finished, but a worker may be minutes into a large
                # archive. Say so, rather than leaving a blank screen that reads
                # as a crash.
                now = time.monotonic()
                if on_progress is not None and (now - last_checkpoint) >= self.config.checkpoint_seconds:
                    last_checkpoint = now
                    on_progress(stats)
                continue
            if item is _STOP:
                finished += 1
                continue

            if item.first_of_file and item.error is None:
                # Attributed on arrival, not on write. Counting it only when the
                # first document is *written* means an archive whose first
                # message happens to be unchanged reports zero bytes read for
                # the whole file - which is how a 64-second run over 100MB came
                # back saying "0.0 MB".
                stats.bytes_read += item.candidate.size_bytes

            if item.file_marker:
                # No chunks, no embedding, no count - just the record that says
                # "this archive was read at this size and time".
                self._write_marker(item)
                continue

            if item.error is None and self._already_current(item):
                # A changed archive still contains mostly unchanged mail. Every
                # message skipped here is an embedding not paid for - which on a
                # 30GB archive with one new email is the entire difference
                # between seconds and hours.
                stats.unchanged_documents += 1
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


            if len(pending_vectors) >= self.config.embed_batch:
                self._embed_pending(pending_vectors)

            since_checkpoint += 1
            now = time.monotonic()
            due = (
                since_checkpoint >= self.config.checkpoint_every
                or (now - last_checkpoint) >= self.config.checkpoint_seconds
            )
            if due:
                since_checkpoint = 0
                last_checkpoint = now
                self._checkpoint(item.candidate, stats)
                if on_progress is not None:
                    on_progress(stats)
                if not self._disk_ok(stats):
                    break

        self._embed_pending(pending_vectors)
        if on_progress is not None:
            on_progress(stats)

    def _write_marker(self, item: _Extracted) -> None:
        """Record a container as indexed without giving it any chunks."""
        candidate = item.candidate
        file_id = self.store.upsert_file(
            item.row_key,
            size_bytes=candidate.size_bytes,
            mtime_ns=candidate.mtime_ns,
            content_hash=item.content_hash,
            status=FileStatus.PENDING,
            source_kind=item.source_kind,
        )
        self.store.mark_indexed(file_id)

    def _already_current(self, item: _Extracted) -> bool:
        """Has this exact document already been indexed, unchanged?

        Only asked of documents *inside* something - an ordinary file was
        already decided by the walker, and asking again would cost a second
        lookup per file for no new information.

        The comparison is a hash of the extracted text, not of the archive: an
        archive's bytes change whenever Outlook so much as opens it, while a
        fifteen-year-old email does not change at all.
        """
        if item.source_kind == "file" or not item.key:
            return False
        record = self.store.get_file(item.key)
        if record is None or record.status != FileStatus.INDEXED:
            return False
        return record.content_hash == _text_digest(item.chunks)

    def _write_one(self, item: _Extracted) -> list[tuple[int, int, list[float]]]:
        """Chunks and vectors first, INDEXED last.

        A crash between them leaves a file that looks unfinished and is redone.
        The reverse would leave it marked done with no chunks - invisible to
        search, and never retried by anything.
        """
        candidate = item.candidate
        file_id = self.store.upsert_file(
            item.row_key,
            size_bytes=candidate.size_bytes,
            mtime_ns=candidate.mtime_ns,
            content_hash=(
                _text_digest(item.chunks) if item.source_kind != "file" else item.content_hash
            ),
            status=FileStatus.PENDING,
            source_kind=item.source_kind,
            # A message key is `<archive>#<EntryID>`, so its parent directory
            # must come from the archive rather than from splitting a path that
            # is not one. Without this, `path:` filters stop matching mail.
            parent_dir=str(candidate.path.parent),
            ext=candidate.path.suffix.lower().lstrip("."),
        )

        chunk_ids = self.store.replace_chunks(file_id, item.chunks)
        self.vectors.delete_by_file_ids([file_id])      # a re-index must not leave the old ones

        if item.meta:
            self._store_message_meta(file_id, item.meta)

        for warning in item.warnings:
            self._log.warning("{} | {}", warning.message, warning.suggestion)

        # Deliberately does NOT embed. Embedding one document at a time means a
        # batch of three chunks per email, and ONNX throughput collapses at that
        # size - the model spends its time on call overhead rather than on
        # matrix work. The consumer batches across documents instead; see
        # `_embed_pending`.
        #
        # The file stays PENDING until its vectors exist. A crash between the
        # two leaves it looking unfinished and it is redone; the reverse would
        # mark it done with no vectors - invisible to semantic search, and never
        # retried by anything.
        return [
            (chunk_id, file_id, chunk["text"])
            for chunk_id, chunk in zip(chunk_ids, item.chunks, strict=True)
        ]

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

    def _embed_pending(self, pending: list[tuple[int, int, str]]) -> None:
        """Embed everything accumulated so far, in one call, and write it.

        **Batching across documents is the whole reason this exists.** Embedding
        per document meant batches of about three chunks for an email, where
        ONNX spends its time on per-call overhead rather than on the matrix
        work it is good at. Gathering a few hundred chunks first restored the
        throughput that per-message indexing had cost.

        Files are marked INDEXED here, after their vectors are safely written,
        never before.
        """
        if not pending:
            return

        texts = [text for _cid, _fid, text in pending]
        vectors = list(self.embedder.embed_all(texts))

        self.vectors.add(
            chunk_ids=[cid for cid, _f, _t in pending],
            file_ids=[fid for _c, fid, _t in pending],
            vectors=vectors,
        )
        self.store.mark_embedded(cid for cid, _fid, _t in pending)
        for file_id in dict.fromkeys(fid for _c, fid, _t in pending):
            self.store.mark_indexed(file_id)
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
        """The only resource check the consumer makes, and it never blocks.

        **The consumer must never wait. This is not a preference.**

        The first version of this called `wait_while_throttled` here, which
        deadlocks: the consumer is the only thread that drains `results`, so
        while it waits the extraction workers block in `_offer` still holding
        every chunk they have parsed. Memory therefore never falls, so a memory
        pause never clears, and the run hangs for good - looking exactly like a
        slow index over a large archive.

        Backpressure belongs at the **intake** (`_produce`), never at the drain.
        So waiting happens there, and the consumer only ever checks for the one
        condition that must end the run rather than delay it.
        """
        found = self.governor.check()
        stats.paused_seconds = self.governor.paused_seconds
        stats.pauses = self.governor.pauses

        if found.action != "stop":
            return True
        if self._stop.is_set():
            return False                        # a user stop, already reported

        stats.stopped_early = make_error(
            "ERR_DISK_SPACE", "index.pipeline",
            free_gb="low", drive=str(self.vectors.uri),
            details=found.reason,
        )
        self._log.error("{}", stats.stopped_early.render())
        self.request_stop()
        return False

    def _prune_missing(self, seen: set[str]) -> int:
        """Delete rows for files that are no longer on disk.

        Only over paths this walk covered: a run restricted to one root must not
        conclude that everything under the others has been deleted.
        """
        # Narrowed in SQL, and only the ids to delete are held. An archive of
        # 200,000 emails is 200,000 rows: materialising every one of them as a
        # FileRecord just to discard the mail is minutes and hundreds of
        # megabytes, at the very end of a run, for nothing.
        #
        # Collected before deleting rather than deleted while iterating: a
        # cursor being read while its table is written underneath it is exactly
        # the kind of thing that works until it does not.
        doomed = [
            record.id
            for record in self.store.iter_files(source_kind="file")
            if str(record.path).lower() not in seen and not Path(record.path).exists()
        ]
        for file_id in doomed:
            self.vectors.delete_by_file_ids([file_id])
            self.store.delete_file(file_id)
        return len(doomed)


def _text_digest(chunks: list[dict[str, Any]]) -> str:
    """A stable hash of a document's text, for change detection inside archives.

    A message has no bytes of its own to hash - it lives inside a file whose
    mtime and size move whenever the mail client touches it. Its *text* is the
    only thing that genuinely identifies whether it has changed, and a
    fifteen-year-old email's text never does.
    """
    digest = hashlib.blake2b(digest_size=16)
    for chunk in chunks:
        digest.update(chunk["text"].encode("utf-8", "replace"))
        digest.update(b"\x00")
    return digest.hexdigest()


def _drain(work: "queue.Queue[Any]") -> None:
    """Empty a queue so a blocked producer can finish and the thread can exit."""
    try:
        while True:
            work.get_nowait()
            work.task_done()
    except queue.Empty:
        return
