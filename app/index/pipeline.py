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
import os
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
from app.core.run_lock import COMMAND_LINE, publish, stop_requested
from app.extract.source_types import indexed_ext
from app.index.embedder import EMBED_BATCH as _EMBED_BATCH
from app.index.embedder import Embedder
from app.index.resources import ResourceGovernor, ResourceLimits, SystemProbe, Verdict
from app.index.stages import WAITING, StageClock
from app.index.walker import (
    Candidate,
    WalkConfig,
    content_hash,
    enclosing_repo,
    has_changed,
    repo_kind_at,
    walk,
)
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
#: overhead on small ones.
#:
#: **Imported, not declared.** This was 256 here and 64 in `embedder.py`, and
#: `embed_all` re-split every gathered batch down to its own number - so the
#: lever was connected to nothing. One constant, in the module that uses it.
EMBED_BATCH = _EMBED_BATCH

#: Files between free-space checks. `shutil.disk_usage` is a syscall, so this is
#: cheap, but not free enough to do per file.
DISK_CHECK_EVERY = 200

#: The three passes, and the words the CLI and Settings both use.
#:
#: `both` is what every run did before this existed, and remains the default -
#: it is right up to about 100GB. `text` and `images` are the split that makes a
#: terabyte tractable: the first pass makes search useful in a day or two and
#: the second fills in the images behind it, with nobody waiting.
OCR_MODES = ("both", "text", "images")

#: New chunks in one run above which the FTS5 index is merged afterwards.
#:
#: The merge rewrites the whole keyword index - minutes at ten million chunks -
#: so it is pure waste after an incremental pass that added four, and necessary
#: after a first pass that added four million. 10,000 is the line between the
#: two: a run that added that many has written enough segments for the merge to
#: pay for itself, and is long enough that a few extra seconds at the end is not
#: noticed.
FTS_OPTIMIZE_AFTER_CHUNKS = 10_000

#: The window the reported throughput covers, in seconds.
#:
#: **A rate averaged since the start is useless on a run of days.** After
#: seventy hours the lifetime average barely moves, so a run that has slowed to
#: a crawl - or stopped making progress entirely - still reports the number it
#: was managing on day one. Fifteen minutes is long enough not to jump about
#: while one large archive is parsed, and short enough to notice a change on
#: the day it happens.
RATE_WINDOW_S = 15 * 60

#: Seconds between the summary lines written to the run log on a long run.
#:
#: A week-long run produces millions of progress lines and nobody reads them.
#: One line a day - files, bytes, skips by cause, hours - is a run somebody can
#: review afterwards in under a minute.
SUMMARY_EVERY_S = 24 * 60 * 60


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
    #: Waiting right now, and why - as opposed to `paused_seconds`, which is a
    #: total. A pause used to freeze the progress bar with no explanation.
    paused: bool = False
    pause_reason: str = ""
    #: True once the walker has finished finding files, which is the moment
    #: `seen` stops being a running tally and becomes a total. Nothing can show
    #: an honest percentage before it.
    walk_complete: bool = False
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
    #: Skips this run **left alone**, by code, because the file has not changed
    #: since it was skipped. Reported for the same reason `skipped_roots` is:
    #: settling them is right, and settling them in silence would make a corpus
    #: of 100k unreadable PDFs disappear from every summary. A number that
    #: stopped being printed reads as a problem that stopped existing.
    settled_by_code: dict[str, int] = field(default_factory=dict)
    #: Chunks an earlier run left without a vector, filled in at the start of
    #: this one. Non-zero means coverage was incomplete and has been repaired -
    #: worth saying, because the only previous symptom was search quietly
    #: getting worse. See `Pipeline._drain_unembedded`.
    vectors_repaired: int = 0
    #: Files the walk could not `stat`, by reason. **Not skips**: a skip has
    #: a row explaining itself, and these have no row at all. Reported so
    #: that files invisible to the whole application are at least a number -
    #: see `walker._record_stat_failure`.
    unreachable_by_reason: dict[str, int] = field(default_factory=dict)
    #: Roots the walk could not use, as `path -> reason`. Distinct from
    #: `skipped_roots`, which is a deliberate archival decision: this is a
    #: folder that is missing or shut out, and always wants somebody's
    #: attention. See `WalkConfig.root_problems`.
    root_problems: dict[str, str] = field(default_factory=dict)
    #: Archival roots this run did not walk, as `RootPlan.as_dict()`. Reported
    #: rather than merely acted on: *"skip cheaply, but never silently"*. A root
    #: skipped in silence is indistinguishable from one that was never indexed,
    #: and the person who concludes the second will delete their index.
    skipped_roots: list[dict[str, Any]] = field(default_factory=list)
    #: Files seen per archival root this run, keyed as `archives.normalise`
    #: gives them. Written by the walker thread, read once at the end.
    root_counts: dict[str, int] = field(default_factory=dict)
    #: Files recorded by name because nothing can read them, and the count per
    #: extension. **Not skips**: nothing went wrong, there is no reader for a
    #: `.mp4`. Reported so an invisible absence becomes a number - which is how
    #: somebody discovers a corpus is 30% `.dwg`.
    name_only: int = 0
    name_only_by_ext: dict[str, int] = field(default_factory=dict)
    #: Things worth saying before or during the run that are not failures.
    #: Shown by the CLI and by the Indexing panel. A run that is going to take a
    #: week should say what it can see coming at the start of it, not at hour
    #: sixty when the disk fills.
    notices: list[str] = field(default_factory=list)
    #: Warning code -> how many documents carried it.
    #:
    #: Warnings live on documents that indexed *successfully*, so none of them
    #: reached `skipped_by_code` and the only record was a log line. That made
    #: "412 decks are mostly images" - the input to the Office OCR decision -
    #: a question nobody could answer without grepping.
    warned_by_code: dict[str, int] = field(default_factory=dict)
    #: Vectors actually written this run, against `chunks` written.
    #:
    #: **The number whose absence hid the embedding gap for weeks.** A run that
    #: wrote 3,355 chunks and 0 vectors reported `"chunks": 3355` and nothing
    #: else - success, by every measure the run itself produced. The gap was
    #: discoverable only afterwards, by `stats` or `doctor` comparing the two
    #: stores, which is a question nobody thinks to ask about a run that said it
    #: worked.
    #:
    #: `embed_failures` counts flushes that produced nothing, so "the model
    #: never loaded" and "there was nothing to embed" are different answers.
    vectors: int = 0
    embed_failures: int = 0
    #: Passages the model was **not** asked about because an identical one had
    #: already been embedded this batch - §6e. Reported because it is the
    #: number that says whether the feature earns its place: under about 15% of
    #: chunks it is not worth the code, and the only way to know is to look at
    #: a real corpus.
    chunks_deduped: int = 0
    #: Which pass this is - `both`, `text` or `images`. Carried on the stats so
    #: the progress line can say "reading with OCR", because seconds per page
    #: looks exactly like a stall on a line built for hundreds of files a minute.
    ocr_mode: str = "both"
    #: Where the run's time went, on the consumer's critical path - §6a. See
    #: `app/index/stages.py` for why extraction appears as `waiting` rather
    #: than as its own worker-seconds total: four workers busy for a minute is
    #: four worker-minutes and one wall minute, and a percentage built from the
    #: first is meaningless.
    #:
    #: Empty on a run too short to measure, which prints nothing rather than a
    #: row of zeroes.
    stages: dict[str, float] = field(default_factory=dict)
    #: The tuning values this run actually used - §5c. **Recorded rather than
    #: reconstructed**: settings change between runs, so reading them back
    #: afterwards answers a question about now instead of about the run. A
    #: measurement whose configuration cannot be recovered is a measurement
    #: nobody can learn from, which is the whole point of taking it.
    resolved: dict[str, Any] = field(default_factory=dict)
    #: Parallel work, unweighted and separately named so it can never be
    #: mistaken for wall time.
    worker_seconds: dict[str, float] = field(default_factory=dict)

    #: `(monotonic, indexed, bytes_read)` samples, for the windowed rates.
    #: Bounded by time rather than by count in `sample`, so the memory cost is
    #: one small tuple every couple of seconds for fifteen minutes - about 450
    #: of them - however long the run lasts.
    recent: list[tuple[float, int, int]] = field(default_factory=list)

    @property
    def files_per_minute(self) -> float:
        return (self.indexed / self.elapsed_s * 60) if self.elapsed_s > 0 else 0.0

    @property
    def mb_per_minute(self) -> float:
        return (self.bytes_read / 1_048_576 / self.elapsed_s * 60) if self.elapsed_s > 0 else 0.0

    def sample(self, *, now: Optional[float] = None) -> None:
        """Record a point for the windowed rates, and drop what has aged out.

        Called at every checkpoint - about every two seconds - which is often
        enough for the window to be meaningful and rare enough to cost nothing.
        """
        current = now if now is not None else time.monotonic()
        self.recent.append((current, self.indexed, self.bytes_read))
        cutoff = current - RATE_WINDOW_S
        # Keep one sample from before the cutoff: it is the *start* of the
        # window, and dropping it leaves the first tick after a trim comparing
        # a point with itself and reporting a rate of zero.
        keep = 0
        for index, (stamp, _files, _bytes) in enumerate(self.recent):
            if stamp >= cutoff:
                keep = max(0, index - 1)
                break
        else:
            keep = max(0, len(self.recent) - 1)
        if keep:
            del self.recent[:keep]

    def _window(self) -> Optional[tuple[float, int, int]]:
        """`(seconds, files, bytes)` covered by the window, or None."""
        if len(self.recent) < 2:
            return None
        first, last = self.recent[0], self.recent[-1]
        seconds = last[0] - first[0]
        if seconds <= 0:
            return None
        return seconds, last[1] - first[1], last[2] - first[2]

    @property
    def recent_files_per_minute(self) -> Optional[float]:
        r"""Throughput over the last `RATE_WINDOW_S`, or None while unknown.

        **An average over four days says nothing about whether it is still
        moving.** A run that indexed 400,000 files in three days and then hit a
        folder of scanned PDFs still reports a healthy lifetime average while
        doing almost nothing - which is precisely the moment somebody needs to
        know. `None` rather than 0 until there are two samples, because "no
        measurement yet" and "stopped" must not print the same.
        """
        window = self._window()
        return None if window is None else window[1] / window[0] * 60

    @property
    def recent_mb_per_minute(self) -> Optional[float]:
        window = self._window()
        return None if window is None else window[2] / 1_048_576 / window[0] * 60

    def as_dict(self) -> dict[str, Any]:
        return {
            "seen": self.seen, "indexed": self.indexed, "unchanged": self.unchanged,
            "unchanged_documents": self.unchanged_documents,
            "skipped": self.skipped, "deleted": self.deleted, "chunks": self.chunks,
            "paused_s": round(self.paused_seconds, 1), "pauses": self.pauses,
            "paused": self.paused,
            "current": self.current,
            "bytes_read": self.bytes_read, "elapsed_s": round(self.elapsed_s, 2),
            "files_per_minute": round(self.files_per_minute, 1),
            "mb_per_minute": round(self.mb_per_minute, 2),
            "vectors": self.vectors,
            "embed_failures": self.embed_failures,
            "chunks_deduped": self.chunks_deduped,
            "name_only": self.name_only,
            "name_only_by_ext": dict(self.name_only_by_ext),
            "skipped_by_code": dict(self.skipped_by_code),
            "settled_by_code": dict(self.settled_by_code),
            "vectors_repaired": self.vectors_repaired,
            "unreachable_by_reason": dict(self.unreachable_by_reason),
            "root_problems": dict(self.root_problems),
            "skipped_roots": list(self.skipped_roots),
            "notices": list(self.notices),
            "warned_by_code": dict(self.warned_by_code),
            "ocr_mode": self.ocr_mode,
            "stopped_early": self.stopped_early.code if self.stopped_early else None,
            # §6a. Omitted entirely when nothing was measured, so a run too
            # short to time prints nothing rather than a row of zeroes that
            # reads as "every stage took no time".
            **({"stages": dict(self.stages)} if self.stages else {}),
            **({"worker_seconds": dict(self.worker_seconds)}
               if self.worker_seconds else {}),
            **({"resolved": dict(self.resolved)} if self.resolved else {}),
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
    #: §6e. Send each *distinct* passage to the model once and reuse the
    #: result. Signatures, disclaimers and boilerplate repeat across thousands
    #: of documents, and every copy costs a full forward pass.
    #:
    #: **Changes no result.** Identical text produces an identical vector, so
    #: this is arithmetic avoided rather than a trade-off taken - which is why
    #: it is on by default and why it needed no quality gate, only a saving to
    #: report. `IndexStats.chunks_deduped` is that number.
    dedup_chunks: bool = True
    #: §6d. Words searchable as soon as a file is read, with the meaning model
    #: catching up behind. `auto | on | off` for the word index; see
    #: `_optimise_keyword_index`.
    two_phase: bool = True
    bulk_fts: str = "auto"
    min_free_gb: int = 5
    #: Where the completions sidecar goes - `DATA_PATH`, normally.
    #:
    #: Passed rather than derived because the pipeline has no `Settings`: it is
    #: given a store and a vector store and nothing else, deliberately, so it
    #: can be driven from a test with two temporary directories. `None` falls
    #: back to the folder above the SQLite file, which is `DATA_PATH` for every
    #: layout this application creates.
    sidecar_dir: Optional[Path] = None
    #: Re-hash files whose mtime moved, rather than trusting mtime alone.
    verify_hash: bool = True
    #: Remove rows for files that no longer exist. Off for a partial run over a
    #: subset of roots, where "missing" only means "not in this walk".
    prune_missing: bool = True
    #: Retry files previously skipped as locked - the program holding them may
    #: well have closed since.
    retry_locked: bool = True
    #: Re-parse every skipped and failed file, even one whose date and size have
    #: not moved. **Off, because leaving it on was H1**: an unchanged file
    #: cannot produce a different outcome, and re-reading 100k known failures on
    #: every incremental pass costs hours and finds nothing.
    #:
    #: On is the answer when the *environment* has changed in a way no pass
    #: knows how to announce - LibreOffice installed after a run recorded
    #: thousands of `ERR_CONVERTER_MISSING`, a Python library added, a size
    #: ceiling raised. Cheaper and far more targeted than `--force`, which
    #: re-indexes the whole corpus including everything that succeeded.
    retry_skipped: bool = False
    #: Which pass this is. See `OCR_MODES` and `_ocr_gate`.
    #:
    #: **OCR is the schedule, not a feature.** At 3.6 seconds a page, 100,000
    #: scanned pages is 100 hours on its own - so a single pass that reads text
    #: and images together means nothing is searchable until everything is.
    #: `text` indexes everything readable without OCR and *queues* the images;
    #: `images` picks up exactly that queue. Search becomes useful after the
    #: first, in a day or two rather than a fortnight.
    ocr_mode: str = "both"
    #: Honour the Live/Archive mode on each root. Off for a run that must see
    #: everything whatever the modes say - `--recheck-archives` sets `recheck`
    #: instead, which walks the archives *and* refreshes their records.
    archives: bool = True
    #: Walk every archival root in full this once, and record a new pass.
    recheck_archives: bool = False
    #: Days an archive is trusted without evidence. From `ARCHIVE_RECHECK_DAYS`.
    recheck_days: int = 30
    #: Seconds between summary lines in the run log. See `SUMMARY_EVERY_S`.
    summary_every_s: float = SUMMARY_EVERY_S
    #: Free space this run would like to see before starting, in GB. Advisory:
    #: it produces a notice, never a refusal. `min_free_gb` is the floor that
    #: actually stops a run, and it stops it *when space runs out* rather than
    #: guessing beforehand.
    #:
    #: **It used to be neither.** `REQUIRED_FREE_GB` had a control, a default
    #: and a tooltip saying it was "checked before a run starts", and no code
    #: anywhere read it. At 100GB that was a harmless untruth; before a week-long
    #: run over 1.5TB, "you have 40GB free" is worth knowing at minute one.
    required_free_gb: int = 0
    #: Index every file found, whatever the change detector says.
    #:
    #: **The escape hatch that was missing.** Change detection decided a file
    #: was unchanged from its `files` row alone, and there was no way to
    #: overrule it - so a corpus whose rows said INDEXED while holding no chunks
    #: could never be rebuilt except by deleting the database. Seventeen files
    #: seen, seventeen unchanged, zero chunks, and the run reported success.
    force: bool = False

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
    #: Record this file by name and read nothing. See `FileStatus.NAME_ONLY`.
    name_only: bool = False
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
        # **The batch the config asks for is the batch the model gets.**
        #
        # `_embed_pending` gathers `config.embed_batch` chunks and hands them to
        # `embed_all`, which splits by the *embedder's* own `batch_size`. With
        # the two set differently the config's number was decoration: it decided
        # how often the store was written, never how large an ONNX call was.
        #
        # Aligned rather than asserted, because the caller who set
        # `PipelineConfig(embed_batch=...)` plainly meant the embedding batch.
        # A caller who deliberately wants a smaller ONNX batch than the gather
        # size can still set it afterwards.
        if getattr(embedder, "batch_size", None) != config.embed_batch:
            try:
                embedder.batch_size = config.embed_batch
            except Exception as exc:                # noqa: BLE001 - a fake, or frozen
                logger.bind(component="index.pipeline").debug(
                    "embedder batch size not aligned to {}: {}",
                    config.embed_batch, exc)
        # Injected in tests with a fake probe, so every pause and resume path is
        # exercised without needing a machine that is actually short of memory.
        self.governor = governor or ResourceGovernor(
            config.resolved_limits(),
            probe=SystemProbe(lambda: getattr(self.vectors, "uri", None)).read,
            on_state_change=self._on_throttle,
        )
        self._log = logger.bind(component="index.pipeline")
        #: Skips left alone this run, by code. **Counted so the fix for H1 does
        #: not become a silence of its own.** Before it, every run re-parsed
        #: these files and reported them in `skipped_by_code`, so the size of
        #: the problem was at least visible. Settling them without saying so
        #: would make 100k unreadable PDFs vanish from every summary, and
        #: somebody would reasonably conclude they had been fixed.
        #:
        #: Written by the producer thread only, in `_classify`, and read once
        #: at the end of the run.
        self._settled_skips: dict[str, int] = {}
        #: Every path this run has covered, lowercased. **One set, shared**
        #: between the walker, `_candidates` and `_produce` - see M17 in
        #: `_candidates`. Replaced at the start of each run.
        self._seen_paths: set[str] = set()
        # Repository roots the walk finds, as `root_path -> kind`. Written by
        # the producer thread inside `walk()`, read by the consumer when it
        # attributes a file. Safe because the only write is `setdefault` and
        # because of the ordering `walker.py` guarantees: a repository root is
        # detected in the same `os.walk` iteration that yields the files
        # sitting directly in it, and before them - so by the time any
        # candidate arrives here, its repository is already in the sink.
        self._repo_roots: dict[str, str] = {}
        #: root_path -> repos.id, so each root is inserted once per run.
        self._repo_ids: dict[str, int] = {}
        #: The sink's keys, longest first. Rebuilt only when the sink grows.
        self._repo_order: list[str] = []
        self._throttle: Optional[Verdict] = None
        #: What `_plan_roots` decided this run. Read again at the end, to record
        #: a pass for every archival root that was walked in full.
        self._plans: tuple[Any, ...] = ()
        #: Monotonic marks for the daily summary line. Set in `run`.
        self._run_started = 0.0
        #: Wall-clock start, and who started it. `_run_started` is monotonic -
        #: correct for measuring elapsed time and meaningless to another
        #: process, which needs a clock it can format as "since 14:02".
        self._run_started_wall = 0.0
        #: Named on the published record so a refusal can say who holds the
        #: lock. Set by the window to `run_lock.GUI`; the default suits the CLI
        #: and every test that constructs a pipeline directly.
        self.run_owner = COMMAND_LINE
        self._last_summary = 0.0
        self._stats_ref = IndexStats()
        #: §6a. Built here rather than in `run` so a pipeline constructed and
        #: never run still has one - several helpers touch it, and a `None`
        #: they would each have to check is a `None` one of them would forget.
        self._clock = StageClock()
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
        stats = IndexStats(ocr_mode=self.config.ocr_mode)
        self._stats_ref = stats          # workers announce the file they are on
        # A fresh clock per run: a Pipeline reused for a second run would
        # otherwise report the first one's stages added to the second's, and
        # the number nobody can act on is a total over two different corpora.
        self._clock = StageClock()
        started = time.perf_counter()
        self._run_started = self._last_summary = time.monotonic()
        self._run_started_wall = time.time()
        self._stop.clear()
        self._interrupted = False

        # Below-normal CPU and background I/O priority, before a single file is
        # read. The cheapest courtesy available and the most effective: the
        # scheduler simply prefers whatever the person is actually doing.
        if self.governor.apply_priority():
            self._log.debug("running at below-normal priority")

        self.vectors.ensure_table()
        # **The model loads here, before a single file is read.**
        #
        # It used to load lazily, on the first `embed()` call - which happens
        # inside `_embed_pending`, after a few hundred chunks are already
        # committed. `ERR_MODEL_LOAD` then killed the run having already
        # orphaned a batch, and since those files stay PENDING the next run
        # reached the same place and orphaned another. A fault that should cost
        # nothing at all instead cost a batch per attempt, for ever.
        #
        # `warm_up` has existed since Layer 4, so the first *search* would not
        # pay the ONNX load. Indexing simply never called it.
        #
        # Deliberately not caught: if the model cannot load there is no
        # meaning-based indexing to do, and finding that out before anything is
        # written is the whole point.
        self._warm_embedder()
        # **Anything left without a vector by a previous run is filled first.**
        # See `_drain_unembedded`.
        self._drain_unembedded(stats)
        # Before anything else: an archival root that is being skipped must not
        # have its own stores protected, its repositories seeded or its rows
        # pruned, because none of those should look at it at all.
        self._preflight_disk(stats)
        self._plan_roots(stats)
        # The images pass walks only the image types. Before the producer, or
        # it walks the whole corpus and throws almost all of it away.
        self._narrow_to_images()
        # Before the producer starts, so the sink is attached to the config the
        # walk is about to read and the enclosing roots are already in it.
        self._repo_roots.clear()
        self._repo_ids.clear()
        self._repo_order = []
        self._protect_own_stores()
        self._seed_repos()

        work: queue.PriorityQueue = queue.PriorityQueue(maxsize=self.config.queue_size)
        results: queue.Queue = queue.Queue(maxsize=self.config.queue_size)

        # The one shared set - see `_candidates`. Reset per run rather than
        # created here, because `_candidates` and the walker both write to it
        # and they only have `self` in common.
        self._seen_paths = set()
        seen_paths = self._seen_paths
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
        # **Never after an images-only pass.** That walk saw only the pictures,
        # so "missing" would mean "not an image" for every document in the
        # corpus - and while `exists()` would save them, it would do so at the
        # cost of one syscall per row for nothing. Same reasoning as a run
        # restricted to one root, which has always been excluded.
        if (self.config.prune_missing and not self._interrupted
                and self.config.ocr_mode != "images"):
            stats.deleted = self._prune_missing(seen_paths)

        self._record_repos()
        # Only after a run that finished. Recording a pass that stopped a third
        # of the way through would mark an archive as fully indexed when two
        # thirds of it has never been read, and nothing would look at it again.
        if not self._interrupted and stats.stopped_early is None:
            self._record_archive_pass(stats)

        stats.elapsed_s = time.perf_counter() - started
        # §6a: copied onto the stats last, so the summary, the run log and the
        # tuning footer all read the same numbers rather than three snapshots
        # taken at three different moments.
        stats.stages = self._clock.seconds()
        stats.worker_seconds = self._clock.worker_seconds()
        # §5c: what this run was configured with, recorded beside what it
        # measured. The pipeline knows these because it was handed them; a
        # reader afterwards would have to guess from settings that may since
        # have changed.
        stats.resolved = {
            "workers": self.config.worker_count(),
            "batch": self.config.embed_batch,
            "device": getattr(self.embedder, "device", ""),
            "threads": getattr(self.embedder, "threads", 0),
            "dedup": bool(self.config.dedup_chunks),
        }
        # **Said out loud, every run.** These files were skipped by an earlier
        # run and left alone by this one, which is the right thing to do and
        # also the thing nobody would otherwise know had happened. See
        # `_settled_skips`, and `retry_skipped` for the way to make a run look
        # at them again once the environment has changed.
        # **What the walk could not even look at.** See `WalkConfig.stat_failures`:
        # `except OSError: continue` used to make those files vanish with no
        # number anywhere, and on Windows a path over 260 characters is exactly
        # that case.
        unreachable = dict(getattr(self.config.walk, "stat_failures", {}) or {})
        if unreachable:
            stats.unreachable_by_reason = unreachable
            self._log.warning(
                "{} file(s) could not be read at all and have no row in the "
                "index: {}. A count over 260 characters means Windows long-path "
                "support is off; `app.cli doctor` reports the setting.",
                sum(unreachable.values()),
                ", ".join(f"{why} ({count:,})"
                          for why, count in sorted(unreachable.items(),
                                                   key=lambda kv: -kv[1])),
            )
        stats.settled_by_code = dict(self._settled_skips)
        if self._settled_skips:
            worst = sorted(self._settled_skips.items(), key=lambda kv: -kv[1])[:3]
            self._log.info(
                "left {} previously-skipped file(s) alone - unchanged since they "
                "were skipped, so re-reading them would find the same thing: {}. "
                "Use --retry-skipped after installing something that would change "
                "the answer.",
                sum(self._settled_skips.values()),
                ", ".join(f"{code} ({count:,})" for code, count in worst),
            )
        self._report_root_problems(stats)
        self._say_if_nothing_was_walked(stats)
        self.store.set_state("last_run", str(int(time.time())))
        self.store.set_state("last_run_stats", repr(stats.as_dict()))
        self.vectors.maybe_create_index()
        # **Always at the end of a run**, whatever the row threshold says. A run
        # that added 4,000 chunks would otherwise never compact at all, and a
        # nightly incremental index is exactly that shape - a small run, every
        # day, each one leaving fragments behind forever.
        self.vectors.maybe_compact(force=True)
        self._optimise_keyword_index(stats)
        self._write_completions()
        self._log.info("index run: {}", stats.as_dict())
        return stats

    def _report_root_problems(self, stats: IndexStats) -> None:
        r"""A folder that could not be walked is named. Never raises.

        **This is the one that hid a 30GB corpus.** `walker.walk` skipped a
        root that does not exist with a bare `continue`: no log line, no
        counter, no notice. A drive that had not mounted, a folder renamed
        since it was added, a path saved with a typo - any of them removed the
        entire corpus from the run, and the run then reported success.

        Reported even when the run indexed plenty, which is the case the
        empty-run notice cannot reach: three folders configured, one of them
        gone, thousands of files indexed from the other two, and the missing
        third is invisible in every number on the page.
        """
        try:
            problems = dict(
                getattr(self.config.walk, "root_problems", {}) or {})
            if not problems:
                return
            stats.root_problems = problems

            missing = [path for path, why in problems.items() if why == "not found"]
            listed = ", ".join(
                f"{path} ({why})" for path, why in sorted(problems.items()))
            notice = (
                f"{len(problems)} of the folders you asked Leasha to search "
                f"could not be read this run: {listed}."
            )
            if missing:
                notice += (
                    " Nothing in them is in the index. If that is a removable "
                    "or network drive, connect it and index again.")
            stats.notices.append(notice)
            self._log.warning("{}", notice)
        except Exception as exc:                    # noqa: BLE001 - a notice
            self._log.debug("could not describe the unusable roots: {}", exc)

    def _say_if_nothing_was_walked(self, stats: IndexStats) -> None:
        r"""A run that looked at no files at all must say why. Never raises.

        **This is the most confusing thing Leasha can do and it used to do it
        in silence.** On 2026-08-27 an index run was started from the window,
        took six minutes, reported success, and had `seen: 0` - it walked
        nothing whatsoever. The Indexing page showed zeroes and offered no
        reason, and the owner's reasonable conclusion was that his mail had
        not been indexed. Nothing in the run said the walk had found no files,
        because there was no such notice: `notices` was appended to in exactly
        one place in this file, for archival roots.

        The three ways it happens are genuinely different and want different
        answers, so they are named separately rather than folded into one
        "nothing to do":

        * **No folders are configured.** Since the privacy work, roots start
          empty on a fresh install - by design - so this is the expected state
          of a new machine and the fix is one trip to Settings.
        * **Every folder was skipped as archival.** Deliberate, already
          reported per-root, but worth repeating when the *total* is nothing.
        * **The folders were read and held nothing.** A drive that did not
          mount comes back as an empty folder rather than an error, which is
          the case worth naming out loud.

        `unreachable` is deliberately not one of them: that already has its own
        warning above, and a run can be both unreachable-heavy and non-empty.
        """
        try:
            if stats.seen or stats.indexed or stats.unchanged:
                return

            walked = [str(root) for root in (self.config.walk.roots or [])]
            skipped = len(stats.skipped_roots or [])

            if not walked and not skipped:
                notice = (
                    "No folders are set up to be searched, so this run had "
                    "nothing to look at. Add the folders you want indexed on "
                    "the Settings page.")
            elif not walked and skipped:
                notice = (
                    f"Nothing was indexed: all {skipped} folder(s) are marked "
                    "as archives and were left alone this time. They are "
                    "listed above with the date each was last read.")
            else:
                shown = ", ".join(walked[:3]) + ("..." if len(walked) > 3 else "")
                notice = (
                    f"Nothing was found to index in {shown}. The folder was "
                    "read and held no files Leasha can index - if that is a "
                    "removable or network drive, check it is connected.")

            stats.notices.append(notice)
            # WARNING, not INFO: a run that indexed nothing and said nothing is
            # the report this exists to prevent.
            self._log.warning("{}", notice)
        except Exception as exc:                    # noqa: BLE001 - a notice
            self._log.debug("could not describe an empty run: {}", exc)

    def _write_completions(self) -> None:
        """The shell's completion sidecar, refreshed at the end of the run.

        **Here rather than in the CLI**, because it must also be refreshed by a
        run started from the window and by one started by the scheduler - and
        because the values it holds are exactly what this run has just changed.

        Never raises: a sidecar is a convenience, and a run of several days
        must not end in an exception over a menu.
        """
        from app.search.completions import write_sidecar

        target = self.config.sidecar_dir
        if target is None:
            database = getattr(self.store, "db_path", None)
            if database is None:
                return
            # `<DATA_PATH>/fts/knowledge.db` -> `<DATA_PATH>`.
            target = Path(database).parent.parent
        write_sidecar(self.store, target)

    def _optimise_keyword_index(self, stats: IndexStats) -> None:
        r"""Merge the FTS5 segments, after a run that wrote enough to matter.

        **Never run before this existed.** FTS5 writes a segment per batch of
        inserts and queries touch all of them, so an index built over a
        week-long run accumulates thousands and keyword search gets slower in
        proportion - permanently, and with nothing on any screen to say why.

        Guarded on the chunk count rather than done every time: the merge
        rewrites the entire index, which is minutes at ten million chunks and
        pure waste after an incremental pass that added four.
        """
        wanted = str(self.config.bulk_fts or "auto").lower()
        # §6f, as far as it goes safely today. `on` merges whatever the run
        # wrote; `auto` merges only when the run was big enough for the merge
        # to earn its minutes; `off` leaves the segments alone.
        #
        # **What is deliberately not here is dropping the triggers.** The order
        # asks for that, and it is the half that can lose data: an interrupted
        # bulk run leaves the word index missing everything the run wrote, and
        # the dirty flag that makes it recoverable has to be written *before*
        # the triggers go. That is a schema change and a resume path, and
        # shipping the fast half without the safe half is how a corpus becomes
        # unsearchable with nothing to say why. The control already exists and
        # already changes behaviour; the trigger drop is the next item.
        if wanted == "off":
            self._log.debug("the word index was left unmerged, as asked")
            return
        if wanted != "on" and stats.chunks < FTS_OPTIMIZE_AFTER_CHUNKS:
            return
        optimise = getattr(self.store, "optimize_fts", None)
        if optimise is None:
            return
        started = time.perf_counter()
        if optimise():
            self._log.info(
                "merged the keyword index after {:,} new chunks ({:.1f}s)",
                stats.chunks, time.perf_counter() - started)

    # -- stage 1: walk ------------------------------------------------------

    def _produce(self, work: queue.PriorityQueue, stats: IndexStats, seen: set[str]) -> None:
        """Walk, decide what needs doing, and queue it. Runs in one thread.

        The unchanged decision happens *here*, before anything is queued, so an
        incremental pass over a settled corpus never wakes a worker at all.
        """
        from app.index.archives import files_under

        sequence = 0
        # Snapshotted: `walk.roots` is not written during a run, and asking for
        # it per file would be a list build a million times over.
        roots = list(self.config.walk.roots)
        try:
            for candidate in self._candidates():
                if self._stop.is_set():
                    break
                # `_candidates` and the walker have already recorded this path
                # in the same set - see M17. Kept as a no-op `add` rather than
                # removed, because `_produce` is also called with a private set
                # by tests, and a set that is only *sometimes* filled is the
                # kind of thing that makes a prune pass delete a live file.
                seen.add(str(candidate.path).lower())
                stats.seen += 1

                # Per-root file counts, so a skipped archive can say how many
                # files it holds. A string prefix test per file; no I/O.
                owner = files_under(candidate.path, roots)
                if owner is not None:
                    stats.root_counts[owner] = stats.root_counts.get(owner, 0) + 1

                # Wait here, not in the consumer. This thread holds nothing but
                # one candidate path, so pausing it starves the workers of new
                # work while everything already in flight keeps draining - which
                # is what actually brings memory down.
                verdict = self.governor.wait_while_throttled(should_stop=self._stop.is_set)
                stats.paused_seconds = self.governor.paused_seconds
                stats.pauses = self.governor.pauses
                stats.paused = self.governor.paused
                stats.pause_reason = self.governor.pause_reason
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
            # **`seen` only becomes a real total here.** Until the walk ends it
            # is "what has been found so far", and because the work queue is
            # bounded the walker can never run more than a queue-length ahead of
            # the workers - so `done / seen` sits near 1 from the first minute
            # whatever fraction of the corpus is left. See `progress_for`.
            stats.walk_complete = True
            # §6a. **Recorded, but not on the critical path**, so it is added
            # to the worker tally rather than the stage one: the walk runs on
            # its own thread alongside everything else, and counting its
            # seconds as a share of the run would push the total past 100%.
            #
            # Worth having all the same - a walk that takes two hours over a
            # network share is a fact about the corpus that no other number in
            # the report shows.
            self._clock.add_worker(
                "walk", time.monotonic() - self._run_started)
            for _ in range(self.config.worker_count()):
                work.put((10_000, sequence + 1, _STOP, None))

    def _preflight_disk(self, stats: IndexStats) -> None:
        """Say at minute one what the disk looks like. Never stops the run.

        **A notice, not a refusal.** `min_free_gb` is the floor that stops a
        run, and it does so when space actually runs out - which is correct,
        because everything indexed by then is kept and the run resumes after
        somebody frees space. This is the other half: before a run measured in
        days, a person is entitled to know that the drive has 40GB on it.

        Refusing here would be worse than useless: nobody knows what the index
        for a given corpus costs until it is built, so a refusal would be
        enforcing a guess.
        """
        wanted = int(self.config.required_free_gb or 0)
        if wanted <= 0:
            return
        target = getattr(self.vectors, "uri", None) or getattr(self.store, "db_path", None)
        if not target:
            return
        try:
            import shutil

            free_gb = shutil.disk_usage(str(Path(target).parent)).free / 1_073_741_824
        except Exception as exc:                    # noqa: BLE001 - a notice, not a run
            self._log.debug("free space not checked: {}", exc)
            return
        if free_gb >= wanted:
            return
        notice = (
            f"{free_gb:,.0f}GB free on the index drive, below the {wanted}GB "
            f"this is set to expect. Indexing will still run and will stop "
            f"cleanly at the {self.config.min_free_gb}GB floor if it runs out - "
            f"nothing indexed is lost - but on a corpus this size it is worth "
            f"freeing space before starting rather than at hour sixty."
        )
        stats.notices.append(notice)
        self._log.warning("{}", notice)

    # -- the two passes ------------------------------------------------------

    #: Skip codes that mean **"another pass will do this"**, not "this cannot be
    #: done". A row carrying one of these is a queue entry wearing a skip's
    #: clothes, so settling it would delete the queue.
    #:
    #: `ERR_OCR_HELD` is the one that caught this out. The two functions the
    #: review named - `_locked_candidates` and `_no_text_layer_candidates` -
    #: re-queue explicitly and can flag their candidates, so the first version
    #: of H1 relied on that flag alone. But a held *image* is picked up by the
    #: ordinary walk on the images pass, by extension, with no re-queue function
    #: involved and therefore no flag. Settling it turned OCR off entirely, and
    #: `test_ocr_passes` said so immediately.
    #:
    #: `ERR_FILE_LOCKED` is here as well as on its candidates: a lock is
    #: transient by definition, so it is never a settled answer regardless of
    #: which route the file arrives by.
    DEFERRED_SKIP_CODES = frozenset({"ERR_OCR_HELD", "ERR_FILE_LOCKED"})

    def _is_deferred(self, skip_code: Optional[str]) -> bool:
        """Is this skip a queue entry that the current pass should honour?

        `ERR_NO_TEXT_LAYER` is the case that needs the mode: it is a settled
        answer during a text pass - nothing there can read a scanned page - and
        it is precisely the work during any pass that can run OCR. The same code
        means opposite things depending on who is asking, which is why this is a
        method and not a constant.
        """
        code = str(skip_code or "")
        if code in self.DEFERRED_SKIP_CODES:
            return True
        return code == "ERR_NO_TEXT_LAYER" and self.config.ocr_mode != "text"

    def _ocr_gate(self, candidate: Candidate) -> Optional[AppError]:
        """`ERR_OCR_HELD` if this file belongs to the *other* pass, else None.

        **The skip is a queue, and it has to look like one.** A held file gets
        an ordinary `files` row with a skip code, exactly like a failure - which
        is what makes it findable later - so the code and its wording are the
        only thing separating "40,000 images waiting" from "40,000 broken
        files" on the skipped-files panel. Hence `ERR_OCR_HELD` rather than
        reusing `ERR_NO_TEXT_LAYER`.

        Asked once per file, in the extraction worker, after the change check
        has already decided the file is worth looking at.
        """
        if self.config.ocr_mode not in ("text", "images"):
            return None

        from app.extract.base import reads_by_ocr

        is_image = reads_by_ocr(candidate.path)
        if self.config.ocr_mode == "text" and is_image:
            return make_error(
                "ERR_OCR_HELD", "index.pipeline", path=str(candidate.path))
        # In `images` mode the walk is already narrowed to the image types, so
        # this is a belt-and-braces guard rather than the mechanism - see
        # `_narrow_to_images`. Nothing is written for a non-image, because
        # writing a skip row would mark files the *text* pass indexed perfectly
        # well as failures.
        #
        # **And a PDF from the ledger is not a non-image.** The images pass
        # takes its scanned PDFs from `ERR_NO_TEXT_LAYER` rows rather than from
        # the walk, because whether a PDF needs OCR cannot be known from its
        # name. Holding them here would refuse the very work this pass exists
        # to do.
        return None

    def _narrow_to_images(self) -> None:
        """Restrict the walk to what OCR reads, for the images pass.

        **Narrowing the walk rather than filtering the results is the whole
        saving.** The images pass over a 1.5TB corpus otherwise `stat`s every
        one of its millions of files to discard all but the pictures - which is
        the same fruitless walk archival roots exist to avoid, paid a second
        time.
        """
        if self.config.ocr_mode != "images":
            return
        from app.extract.ocr import OcrExtractor

        wanted = frozenset(OcrExtractor.extensions)
        current = self.config.walk.extensions
        # An explicit set from the caller is narrowed, never widened: a run
        # restricted to `.png` must not become a run over every image type.
        self.config.walk.extensions = (
            wanted if current is None else frozenset(current) & wanted
        )
        # **And no name-only rows on this pass.** Narrowing the extensions is
        # only half a narrowing while `name_only` is on: the walk still yields
        # every `.txt`, `.pdf` and `.docx` in the corpus - now as unreadable,
        # because the extension set no longer admits them - and the saving this
        # method exists for disappears.
        #
        # The worse half is what those rows would say. A `.txt` the text pass
        # indexed perfectly well would be rewritten as NAME_ONLY, its chunks
        # left behind it, and the index would end up holding rows that claim
        # nothing read them while their content sits in `chunks` - the lying
        # row this status was introduced to prevent, arriving through the door
        # nobody was watching. Naming a file is the first pass's job.
        self.config.walk.name_only = False

    # -- archival roots ------------------------------------------------------

    def _plan_roots(self, stats: IndexStats) -> None:
        r"""Narrow the walk to the roots this run should actually look at.

        **Here rather than in the two callers.** `app.cli index` and the window
        each build their own `WalkConfig`, and a saving this large implemented
        in one of them would apply to whichever way the person happened to
        start the run - which is the shape of bug that gets reported as "it is
        fast from the command line and slow from the app". The standing rule is
        that a feature added for one entry point is added for the others.

        Never raises: a failure to read the archive records means every root is
        walked, which is the slow answer and never the wrong one.
        """
        self._plans = ()
        if not self.config.archives:
            return
        try:
            from app.index.archives import (
                MODE_STATE_KEY,
                RECORD_STATE_KEY,
                load_modes,
                load_records,
                plan_roots,
            )

            plans = plan_roots(
                list(self.config.walk.roots),
                modes=load_modes(self.store.get_state(MODE_STATE_KEY, "") or ""),
                records=load_records(self.store.get_state(RECORD_STATE_KEY, "") or ""),
                recheck=self.config.recheck_archives,
                recheck_days=self.config.recheck_days,
            )
        except Exception as exc:                    # noqa: BLE001 - see the docstring
            self._log.warning(
                "archival roots could not be read, so every folder will be "
                "walked in full: {}", exc)
            return

        self._plans = plans
        skipped = [plan for plan in plans if not plan.walk]
        if not skipped:
            return

        self.config.walk.roots = [plan.root for plan in plans if plan.walk]
        stats.skipped_roots = [plan.as_dict() for plan in skipped]
        for plan in skipped:
            # INFO, not DEBUG. This is the one line that tells somebody their
            # archive was deliberately not looked at, and it carries the count
            # and the date so it cannot be mistaken for an empty folder.
            self._log.info("{}", plan.describe())

    def _archive_roots(self) -> list[str]:
        """Roots being skipped, for the prune guard. Normalised."""
        from app.index.archives import normalise

        return [normalise(row["root"]) for row in self._stats_ref.skipped_roots]

    def _record_archive_pass(self, stats: IndexStats) -> None:
        """Store what this run saw, for the archival roots it walked in full."""
        plans = getattr(self, "_plans", ())
        if not any(plan.walk and plan.mode == "archive" for plan in plans):
            return
        try:
            from app.index.archives import (
                RECORD_STATE_KEY,
                dump_records,
                load_records,
                record_pass,
            )

            records = load_records(self.store.get_state(RECORD_STATE_KEY, "") or "")
            self.store.set_state(
                RECORD_STATE_KEY,
                dump_records(record_pass(records, plans, stats.root_counts)),
            )
        except Exception as exc:                    # noqa: BLE001
            # Losing the record costs one more full walk next time. Failing the
            # run would cost the whole run.
            self._log.warning("the archive record was not saved: {}", exc)

    # -- repository attribution ---------------------------------------------

    def _protect_own_stores(self) -> None:
        """Never walk the index we are writing into.

        `app.cli index` derives the full set from Settings, but the window
        builds its own `WalkConfig` and cannot be reached from here. This adds
        the two paths the pipeline knows about first-hand, so an indexed root
        that happens to contain the SQLite index or the vector store is safe
        whoever started the run.

        Additive: whatever the caller already excluded is kept.
        """
        mine: set[str] = set(self.config.walk.exclude_paths)
        database = getattr(self.store, "db_path", None)
        if database:
            mine.add(str(Path(database).parent))
        vectors = getattr(self.vectors, "uri", None)
        if vectors:
            mine.add(str(vectors))
        self.config.walk.exclude_paths = frozenset(mine)

    def _seed_repos(self) -> None:
        """Repositories that *enclose* an indexed root, before the walk starts.

        An indexed root may sit below a repository root: `D:\\SearchProject\\app`
        has no `.git` beneath it and every file under it is still in a
        repository. A walk that only looks downwards attributes none of them.

        One `stat` per ancestor per root, once per run.

        **Roots the owner has disowned are never adopted.** `repos --forget`
        releases 1,179 files and detection would re-adopt the same folder on the
        very next walk, because finding a `.git` is the whole of how a
        repository is registered. The ignore list is where "I have looked at
        this and it is not a checkout" is recorded.
        """
        self.config.walk.repo_sink = self._repo_roots
        try:
            self._ignored_repos = {
                root.rstrip("\\/").lower()
                for root in self.store.ignored_repo_roots()
            }
        except Exception as exc:              # noqa: BLE001 - never fail a run
            self._log.debug("could not read the ignored repositories: {}", exc)
            self._ignored_repos = set()

        for root in self.config.walk.roots:
            found = enclosing_repo(Path(root))
            if found is None:
                continue
            kind = repo_kind_at(found) or "work"
            self._repo_roots.setdefault(str(found), kind)

    def _repo_id_for(self, path: Path) -> Optional[int]:
        """The id of the repository containing `path`, or None.

        **Longest matching prefix, not first match.** A submodule's files sit
        inside its parent's tree, so a first-match search over an unordered
        list attributes them to whichever root it happened to see first -
        correct or not, depending on dictionary order. Sorting by length
        descending makes the nested case right by construction rather than by
        luck.

        An in-memory string comparison per file. No I/O.
        """
        if not self._repo_roots:
            return None
        if len(self._repo_order) != len(self._repo_roots):
            self._repo_order = sorted(self._repo_roots, key=len, reverse=True)

        text = str(path).lower()
        for root in self._repo_order:
            prefix = root.lower().rstrip("\\/")
            if text == prefix or text.startswith(prefix + os.sep) or \
                    text.startswith(prefix + "/"):
                cached = self._repo_ids.get(root)
                if cached is None:
                    cached = self.store.upsert_repo(
                        root, kind=self._repo_roots[root])
                    self._repo_ids[root] = cached
                return cached
        return None

    def _record_repos(self) -> None:
        """Every root the walk found, whether or not any of its files indexed.

        A repository whose files are all excluded by type still exists, and
        `repos_list` uses a LEFT JOIN so it appears with a count of zero. If
        this only recorded the ones that were attributed, the Code tab would
        disagree with the walker for reasons nobody could see.
        """
        ignored = getattr(self, "_ignored_repos", set())
        for root, kind in list(self._repo_roots.items()):
            if str(root).rstrip("\\/").lower() in ignored:
                # Disowned deliberately. Dropped from the run's own map too, so
                # `_repo_id_for` cannot attribute a file to it either.
                self._repo_roots.pop(root, None)
                self._log.debug("ignoring disowned repository {}", root)
                continue
            try:
                self._repo_ids[root] = self.store.upsert_repo(root, kind=kind)
            except Exception as exc:      # detection may never fail a run
                self._log.warning("could not record repository {}: {}", root, exc)

    def _candidates(self) -> Iterator[Candidate]:
        # Snapshotted BEFORE the walk, deliberately. Read lazily afterwards, the
        # query would pick up files this very run had just marked locked and
        # queue them a second time - doubling the work, double-counting the
        # skips, and retrying a file whose lock is by definition still held.
        retry = list(self._locked_candidates()) if self.config.retry_locked else []
        # **The images pass reads its work back from the ledger, not from the
        # walk.** This is the open piece of wiring `WORKORDER-...-ocr-strategy`
        # §4 names: whether a PDF needs OCR is **not knowable from its
        # extension**, so `reads_by_ocr` is False for every PDF, the walk is
        # narrowed to `.png`/`.jpg`, and a scanned manual is never revisited -
        # however many times somebody runs `--only-ocr`.
        #
        # The text pass has already recorded exactly the right rows, as
        # `ERR_NO_TEXT_LAYER`. Reading them back costs one indexed query;
        # re-walking a terabyte to find them again costs the walk this pass
        # exists to avoid.
        scanned = (list(self._no_text_layer_candidates())
                   if self.config.ocr_mode == "images" else [])

        # **One set, shared with the walker and with `_produce`.** This used to
        # keep its own `walked`, the walker kept its own `seen`, and `_produce`
        # kept a third for the prune pass - three copies of every lowercased
        # path in the corpus, about a gigabyte each at five million files, all
        # answering the same question. `walk()` adds to it as it yields; the
        # prune pass reads it at the end.
        seen = self._seen_paths

        yield from walk(self.config.walk, seen)

        for candidate in (*retry, *scanned):
            key = str(candidate.path).lower()
            if key not in seen:
                seen.add(key)
                yield candidate

    def _no_text_layer_candidates(self) -> Iterator[Candidate]:
        r"""PDFs the text pass could not read, for the images pass to retry.

        **Whether a PDF needs OCR cannot be known from its name**, which is the
        whole reason this exists. `reads_by_ocr` asks the resolved extractor and
        answers False for every `.pdf` - correctly, because most PDFs have a
        text layer - so narrowing the images walk to image extensions skips
        every scanned manual in the corpus.

        The text pass already found them and said so: one `ERR_NO_TEXT_LAYER`
        row each. This reads those rows back, which is an indexed query over a
        few hundred rows rather than a second walk of 1.5TB.

        A file that has since been deleted or moved is skipped silently - the
        ordinary prune will deal with the row.
        """
        for record in self.store.iter_files(status=FileStatus.SKIPPED):
            if record.skip_code != "ERR_NO_TEXT_LAYER":
                continue
            path = Path(record.path)
            if path.suffix.lower() != ".pdf":
                # Belt and braces beside the separate code above: only a PDF is
                # work this pass can do. A deck is declined by the strategy, and
                # queueing one would be a decision made by accident.
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            # `retry=True`: these rows are settled - an unchanged `ERR_NO_TEXT_LAYER`
            # is exactly what `_classify` now declines to re-parse - and this pass
            # exists to read them anyway. The images pass is the one thing that
            # can do what the text pass could not.
            yield Candidate(path=path, size_bytes=stat.st_size,
                            mtime_ns=stat.st_mtime_ns, priority=0, retry=True)

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
                            priority=0,          # retried first: they are few and cheap
                            retry=True)          # settled row, deliberately reopened

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
        except Exception as exc:            # noqa: BLE001 - see the docstring
            self._log.warning(
                "could not read the row for {}, queuing it anyway: {}",
                candidate.path, exc,
            )
            return ""

        if not getattr(candidate, "readable", True):
            # **Decided on mtime and size alone, and never by reading.**
            #
            # `has_changed` is the wrong instrument for a file nothing opens.
            # Its recent-edit rule deliberately pays for a hash when a file was
            # touched in the last few minutes, and a NAME_ONLY row holds no hash
            # to compare against - so `fresh != None` is true every time. The
            # result was that a `.mp4` copied in this morning got its 4GB read
            # on the next pass, which is the exact cost this whole feature
            # promises not to incur.
            #
            # Content changes are irrelevant here: the content is not indexed.
            # The only thing a row can go stale about is its size and date.
            if record is not None and record.status == FileStatus.INDEXED:
                # **Never demote a row that was read.** This pass cannot read
                # the file; a previous one could. That happens whenever the
                # extension set narrows - the images-only OCR pass, a type
                # removed in Settings - and rewriting the row as NAME_ONLY
                # would leave it claiming nothing read it while its chunks sit
                # in `chunks` next to it. A pass that cannot open a file has
                # nothing to say about its contents.
                return UNCHANGED
            if (record is not None
                    and record.status == FileStatus.NAME_ONLY
                    and record.mtime_ns == candidate.mtime_ns
                    and record.size_bytes == candidate.size_bytes):
                return UNCHANGED
            return None                      # write the name row, read nothing

        try:
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

        if self.config.force:
            return digest                    # `--force`: index it whatever the row says

        # **`status == INDEXED` is trusted, and a row can lie.**
        #
        # A file recorded as INDEXED that produced no chunks is skipped for ever:
        # every later run reports it as unchanged, the totals look healthy, and
        # its content is not searchable. That is the same shape as the `.pst`
        # bug this method's docstring describes - "the run was a success by
        # every number it printed" - reappearing through a different door.
        #
        # Checking the chunk count here would cost a query per file on a 100GB
        # walk, which is why the row is trusted. `--force` is the answer
        # instead: the trust is cheap and overridable rather than expensive and
        # absolute.
        # **NAME_ONLY is deliberately not in that tuple**, and this is the only
        # place the distinction shows. Reaching here means the walk called this
        # file readable while its row says nothing read it - the size ceiling
        # was raised, or an extractor was added for its type. Treating the row
        # as settled would leave it name-only for ever, with nothing to tell
        # anyone why. The unreadable case never gets this far; it is answered
        # above without a read.
        if not changed and record is not None and record.status == FileStatus.INDEXED:
            return UNCHANGED

        # **A skip is settled while the file has not moved, and this was H1.**
        #
        # `_classify` returned `UNCHANGED` only for `INDEXED`, so a SKIPPED or
        # FAILED row with an identical date and size fell straight through: the
        # file was re-queued, fully re-parsed, failed again for the same reason,
        # and its row was rewritten. Every incremental pass. For ever. A corpus
        # with 100k scanned PDFs recorded as `ERR_NO_TEXT_LAYER` pays hours
        # every night to rediscover failures it already knows about.
        #
        # Nothing about the file has changed, so nothing about the outcome can.
        # What *can* change is the environment - a lock released, an OCR pass
        # that reads what the text pass could not, a converter finally
        # installed - and that is what `candidate.retry` and `config.retry_skipped`
        # are for. Neither is a guess about the skip code: the pass that
        # re-queues a file is the only thing that knows why, so it says.
        #
        # The existence of `_locked_candidates` is the evidence this
        # fall-through was never intended. A pass built to re-queue locked files
        # is pointless if every skipped file is re-queued anyway.
        # **Date and size, not `changed`.** `has_changed` compares the content
        # hash when `verify_hash` is on, and a skipped file has no content hash
        # - nothing ever read it. So `changed` is unconditionally True for every
        # skip, and gating on it would have left this fix doing nothing at all
        # while every test of the *mechanism* passed. The first version did
        # exactly that, and only a test that asserted the outcome caught it.
        #
        # mtime and size are what the NAME_ONLY branch above compares, for the
        # same reason and in the same situation.
        settled = (FileStatus.SKIPPED, FileStatus.FAILED)
        if (record is not None and record.status in settled
                and record.mtime_ns == candidate.mtime_ns
                and record.size_bytes == candidate.size_bytes
                and not self._is_deferred(record.skip_code)
                and not getattr(candidate, "retry", False)
                and not self.config.retry_skipped):
            self._settled_skips[record.skip_code or "unknown"] = (
                self._settled_skips.get(record.skip_code or "unknown", 0) + 1)
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
            if not getattr(candidate, "readable", True):
                # **Nothing is opened.** The row is its name, path, size and
                # date - one INSERT on top of a `stat` the walk already did.
                self._offer(results, _Extracted(
                    candidate=candidate, content_hash=None, name_only=True))
                work.task_done()
                continue
            self._stats_ref.current = candidate.path.name
            self._stats_ref.current_since = time.monotonic()
            self._stats_ref.current_item = 0
            try:
                # **Worker-seconds, kept apart from wall time on purpose.**
                # This is real and worth having - "extraction cost 40
                # worker-minutes" answers a question - but it is not a
                # percentage of anything, because N of these run at once.
                # The consumer's `waiting` is the wall-clock half.
                #
                # Timed one item at a time and **still lazy**: wrapping the
                # whole generator in a `list` would time it just as well and
                # buffer a 30GB archive's messages in memory, which is exactly
                # the M16 fix undone for a stopwatch.
                stream = self._extract_stream(candidate, digest)
                while True:
                    started = time.perf_counter()
                    try:
                        item = next(stream)
                    except StopIteration:
                        break
                    finally:
                        self._clock.add_worker(
                            "extract", time.perf_counter() - started)
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

        held = self._ocr_gate(candidate)
        if held is not None:
            yield _Extracted(candidate, digest, error=held)
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
                        # Adoptions §6a. `None` for everything that is not a
                        # spreadsheet, which is nearly every document.
                        "label": chunk.label,
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
        self._last_summary = last_checkpoint
        stats.sample(now=last_checkpoint)          # the window's first point
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
                # **`waiting` is the honest name for "extraction is the
                # bottleneck".** Timed around the blocking get rather than
                # around extraction itself, because extraction runs on N
                # threads and summing their seconds would give a percentage
                # over 100. What the consumer waits for is what would go
                # faster if more readers were added - which is the question
                # the tuning screen is actually asked. See `index/stages.py`.
                with self._clock.stage(WAITING):
                    item = results.get(timeout=0.25)
            except queue.Empty:
                # Nothing has finished, but a worker may be minutes into a large
                # archive. Say so, rather than leaving a blank screen that reads
                # as a crash.
                now = time.monotonic()
                if on_progress is not None and (now - last_checkpoint) >= self.config.checkpoint_seconds:
                    last_checkpoint = now
                    try:
                        on_progress(stats)
                    except Exception as exc:  # see the `due` branch
                        self._log.warning("progress reporting failed: {}", exc)
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

            if item.name_only:
                self._write_name_only(item)
                stats.name_only += 1
                extension = indexed_ext(item.candidate.path) or "(none)"
                stats.name_only_by_ext[extension] = (
                    stats.name_only_by_ext.get(extension, 0) + 1)
                continue

            if item.file_marker:
                # **Its messages are embedded before it is marked done.**
                #
                # The marker says "this archive was read at this size and time",
                # and the walker trusts it: a marked archive is not opened
                # again. But up to `embed_batch` of its own messages could still
                # be sitting in `pending_vectors` at this moment, with their
                # chunks committed and no vectors written. A crash, a stop, or
                # an embedding failure in that window left the archive
                # permanently marked complete with a hole in its vector
                # coverage - and nothing drains it: `iter_unembedded` is reached
                # only by a manual `app.cli reembed` that nobody knows to run.
                #
                # Flushing first costs one early batch per archive and closes
                # the window: the marker is written after the vectors exist.
                if pending_vectors:
                    self._embed_pending(pending_vectors)
                    pending_vectors = []
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
                with self._clock.stage("write"):
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
                # **Reporting must never cost the flush.** None of this was
                # guarded, and all of it can raise: `_checkpoint` writes to
                # SQLite, and `on_progress` is the caller's - the CLI's version
                # prints a *filename* to a Windows console, so one path outside
                # cp1252 was a `UnicodeEncodeError` that escaped `_consume`
                # before the final `_embed_pending`. The whole pending batch was
                # then lost, chunks committed and vectors never written, over a
                # character in a filename.
                #
                # Logged rather than swallowed - progress that has silently
                # stopped updating is its own confusing fault - but never
                # allowed to end the run.
                try:
                    self._checkpoint(item.candidate, stats)
                    stats.sample(now=now)
                    self._maybe_summarise(stats, now=now)
                    if on_progress is not None:
                        on_progress(stats)
                except Exception as exc:    # reporting, not work
                    self._log.warning("progress reporting failed: {}", exc)
                if not self._disk_ok(stats):
                    break

        # **The final flush, before anything else can go wrong.** Everything
        # above may have left up to `embed_batch` passages committed to SQLite
        # with no vector; this is the only thing that resolves them, so it runs
        # before the last progress call rather than after it.
        self._embed_pending(pending_vectors)
        if on_progress is not None:
            try:
                on_progress(stats)
            except Exception as exc:        # reporting, not work
                self._log.warning("final progress report failed: {}", exc)

    def _warm_embedder(self) -> None:
        """Load the embedding model now, so a failure costs nothing written.

        Tolerant of an embedder that has no `warm_up` - the pipeline is given
        doubles by half the test suite, and requiring the method would make
        every one of them declare a load it does not do.
        """
        warm = getattr(self.embedder, "warm_up", None)
        if callable(warm):
            warm()

    def _maybe_summarise(self, stats: IndexStats, *, now: float) -> None:
        r"""One line a day in the run log, on a run measured in days.

        **A week-long run produces millions of progress lines and nobody reads
        them.** Reviewing what happened afterwards - did it slow down, when did
        the scanned PDFs start, was it pausing all Tuesday - means finding a
        handful of facts in a file of that size, which nobody does. One line a
        day, carrying the counts, the throughput over the last window and the
        skips by cause, is a run somebody reviews in under a minute.

        Costs nothing on a short run: the interval never elapses.
        """
        interval = self.config.summary_every_s
        if interval <= 0 or (now - self._last_summary) < interval:
            return
        self._last_summary = now
        hours = (now - self._run_started) / 3600
        recent = stats.recent_files_per_minute
        self._log.info(
            "day {:.0f} of this run: {:,} document(s), {:,} file(s) seen, "
            "{:,.1f}GB read, {:,} skipped, {:.1f} hours elapsed, {} in the "
            "last {:.0f} minutes, {:,.0f} min paused. Skips by cause: {}",
            hours / 24 + 1, stats.indexed, stats.seen,
            stats.bytes_read / 1_073_741_824, stats.skipped, hours,
            f"{recent:,.0f} files/min" if recent is not None else "no measurement",
            RATE_WINDOW_S / 60, stats.paused_seconds / 60,
            dict(stats.skipped_by_code) or "none",
        )

    def _write_name_only(self, item: _Extracted) -> None:
        r"""A row for a file nothing can read. **Opens nothing.**

        `NAME_ONLY` rather than `INDEXED`, because a row claiming its contents
        were read while holding no chunks is the bug that made `--force`
        necessary. And rather than `SKIPPED`, because nothing went wrong: there
        is no reader for a `.mp4` and there was never going to be.

        `upsert_file` feeds `files_fts` for anything whose `source_kind` is
        `file`, so the name becomes searchable with no extra work.
        """
        candidate = item.candidate
        self.store.upsert_file(
            str(candidate.path),
            size_bytes=candidate.size_bytes,
            mtime_ns=candidate.mtime_ns,
            content_hash=None,
            # Nothing was read, so any hash on the row belongs to contents this
            # pass has not seen.
            clear_hash=True,
            status=FileStatus.NAME_ONLY,
            source_kind="file",
            parent_dir=str(candidate.path.parent),
            ext=indexed_ext(candidate.path),
            repo_id=self._repo_id_for(candidate.path),
        )

    def _drain_unembedded(self, stats: IndexStats) -> None:
        r"""Embed chunks a previous run committed and never vectorised.

        **The hole had no route out.** Chunks are written first and vectorised a
        batch later, so any interruption in that window - a crash, a stop, the
        disk floor, an embedding failure - leaves rows with text and no vector.
        The file is INDEXED and unchanged, so every later run correctly skips
        it, and `iter_unembedded` was reached only by `app.cli reembed`, which
        is a command nobody runs because nothing ever says it is needed.

        So coverage could only fall. The M6 flush above closes the window for
        archives; this repairs what earlier runs already lost, at the start of
        every run, where the model is loaded and nothing is queued behind it.

        Bounded and never fatal. On a healthy index the first query returns
        nothing and this costs one indexed lookup. If it fails, the run
        continues - a run that indexes new files is worth more than one that
        refuses to start because old ones are incomplete.
        """
        try:
            batches = self.store.iter_unembedded(batch_size=self.config.embed_batch)
        except Exception as exc:                 # noqa: BLE001 - a repair, not the job
            self._log.warning("could not check for unembedded chunks: {}", exc)
            return

        filled = 0
        try:
            for batch in batches:
                if self._stop.is_set():
                    break
                pending = [(chunk.id, chunk.file_id, chunk.text) for chunk in batch]
                self._embed_pending(pending)
                filled += len(pending)
        except Exception as exc:                 # noqa: BLE001
            self._log.warning(
                "could not finish filling in missing vectors: {}. Indexing "
                "continues; run `app.cli reembed` when the cause is fixed.", exc)

        if filled:
            stats.vectors_repaired = filled
            self._log.info(
                "filled in {} chunk(s) that an earlier run left without vectors - "
                "they were searchable by keyword but not by meaning", filled)

    def _write_marker(self, item: _Extracted) -> None:
        """Record a container as indexed without giving it any chunks."""
        candidate = item.candidate
        with self.store.batch():
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
        # **One transaction for the three writes, not three.**
        #
        # `upsert_file`, `replace_chunks` and `set_message` each committed
        # separately, so one mail message cost three commits here and six over
        # the whole loop. A commit is roughly fourteen times the cost of the
        # same statement inside an open transaction.
        #
        # The LanceDB delete is deliberately *outside* the block: it is slow
        # relative to a SQLite statement, and the write lock is held for the
        # whole batch. Putting it in would trade commit overhead for making
        # every other thread's writes wait on a vector store.
        with self.store.batch():
            file_id = self.store.upsert_file(
                item.row_key,
                size_bytes=candidate.size_bytes,
                mtime_ns=candidate.mtime_ns,
                content_hash=(
                    _text_digest(item.chunks) if item.source_kind != "file" else item.content_hash
                ),
                # `--fast` (`verify_hash=False`) reaches here with no digest.
                clear_hash=item.source_kind == "file" and item.content_hash is None,
                status=FileStatus.PENDING,
                source_kind=item.source_kind,
                # A message key is `<archive>#<EntryID>`, so its parent directory
                # must come from the archive rather than from splitting a path that
                # is not one. Without this, `path:` filters stop matching mail.
                parent_dir=str(candidate.path.parent),
                # **The name, for a file that has no extension.** `Dockerfile`
                # and `Makefile` are indexed and were unfilterable: `ext` was
                # `''`, `distinct_values` skips those rows, and `type:` matches
                # on that column - so they could not be narrowed to, offered in
                # the `/type` menu, or named in a query at all. See
                # `source_types.indexed_ext`.
                ext=indexed_ext(candidate.path),
                # NULL for anything outside a repository, and for mail, whose
                # `path` is an archive key rather than a location on disk.
                repo_id=(
                    self._repo_id_for(candidate.path)
                    if item.source_kind == "file" else None
                ),
            )

            chunk_ids = self.store.replace_chunks(file_id, item.chunks)

            if item.meta:
                self._store_message_meta(file_id, item.meta)

        # **The old vectors are NOT deleted here.** They used to be, and that
        # single line is the mechanism behind the embedding gap - 154 of 3,355
        # passages with a vector on the owner's index.
        #
        # The chunks above are committed now; their vectors are written by
        # `_embed_pending`, up to `embed_batch` chunks and one lazy model load
        # later. Deleting here opened that whole window with the file holding
        # *no* vectors, old or new, and every abort inside it - a model that
        # will not load, a window closed, the disk floor, an exception in a
        # progress callback - was pure, uncounted loss.
        #
        # **And it compounded**, which is what turned a batch-sized fault into a
        # corpus-sized one. A file whose flush never happened stays PENDING, so
        # the next run picks it up, reaches this line, and destroys the vectors
        # of everything it re-reaches *before* failing in the same place. Each
        # run left coverage lower than it found it. `--force` - the natural
        # thing to try on seeing the gap - made every file take this path.
        #
        # The delete now happens in `_embed_pending`, immediately before the
        # add, so the window is closed: at every instant a file has either its
        # old vectors or its new ones.
        #
        # The reasoning that put it here was about *lock* contention - the
        # LanceDB delete is slow and the SQLite write lock is held for the whole
        # batch - and that reasoning was right. It is preserved: the delete is
        # still outside any `store.batch()`, just later.

        for warning in item.warnings:
            self._log.warning("{} | {}", warning.message, warning.suggestion)
            # **Counted, not only logged.** A warning on a document that indexed
            # successfully never reached the skip ledger, so "how many decks are
            # mostly pictures" - the number the Office OCR decision turns on -
            # was answerable only by grepping a log file. `_warn_if_mostly_
            # pictures` has been collecting this evidence since it was written;
            # nothing was reading it.
            code = str(getattr(warning, "code", "") or "")
            if code:
                self._stats_ref.warned_by_code[code] = (
                    self._stats_ref.warned_by_code.get(code, 0) + 1)

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
        if not chunk_ids:
            # **The one case the flush cannot clean up after.** A file that used
            # to produce chunks and now produces none - text that chunked to
            # nothing, a document emptied in place - has just had its chunk rows
            # replaced with nothing, and it will never appear in `pending`, so
            # `_embed_pending` will never delete its vectors. Orphaned vectors
            # point at chunk ids that no longer exist: they cost the ANN index
            # its accuracy and can resurface content the file no longer holds.
            self.vectors.delete_by_file_ids([file_id])
            return []

        return [
            (chunk_id, file_id, chunk["text"])
            for chunk_id, chunk in zip(chunk_ids, item.chunks, strict=True)
        ]

    def _store_message_meta(self, file_id: int, meta: dict[str, Any]) -> None:
        # **`quoted_removed` is in this tuple, and leaving it out killed the
        # feature it belongs to.** Schema v12 added the column, extraction
        # measured the value, the preview was built to read it - and this
        # dictionary comprehension, which is the only thing that writes message
        # metadata, never listed the key. So the column was NULL for every
        # message ever indexed, and the mail preview could not have shown "480
        # characters of quoted thread removed" on any corpus, ever.
        #
        # A column added, populated at one end and never written at the other is
        # the quietest possible failure: nothing raises, the schema is correct,
        # the extractor is correct, and the feature simply does not exist.
        fields = {
            key: meta.get(key)
            for key in ("store_path", "entry_id", "conversation", "subject",
                        "sender", "recipients", "sent_at", "quoted_removed")
            if meta.get(key) is not None
        }
        if not fields:
            return
        fields["has_attach"] = int(meta.get("has_attach", 0) or 0)
        try:
            self.store.set_message(file_id, **fields)
        except Exception as exc:                # noqa: BLE001 - metadata is not worth a failed file
            self._log.warning("message metadata for file {} not stored: {}", file_id, exc)

    def _embed_texts(self, texts: list[str]) -> list:
        r"""Embed a batch, sending each **distinct** passage once. §6e.

        Signatures, disclaimers, letterheads and boilerplate repeat across
        thousands of documents, and every copy costs a full forward pass
        through the model for a vector the run already has.

        **This changes no result.** Identical text produces an identical
        vector, so the saving is arithmetic avoided rather than a trade-off
        taken - which is why it needed no quality gate, only a number to
        report. `chunks_deduped` is that number, and it is what tells the owner
        whether the feature earns its place on his corpus.

        Off restores the previous behaviour exactly, for anybody who suspects
        it and wants to compare.
        """
        if not self.config.dedup_chunks or len(texts) < 2:
            return list(self.embedder.embed_all(texts))

        # An ordinary dict, keyed by the text itself: hashing it again would
        # cost a second pass over every character to save nothing, since
        # Python already interns the hash on the string object.
        first_seen: dict[str, int] = {}
        unique: list[str] = []
        where: list[int] = []
        for text in texts:
            index = first_seen.get(text)
            if index is None:
                index = first_seen[text] = len(unique)
                unique.append(text)
            where.append(index)

        saved = len(texts) - len(unique)
        if not saved:
            return list(self.embedder.embed_all(texts))

        embedded = list(self.embedder.embed_all(unique))
        if len(embedded) != len(unique):
            # **The model disagreed about how many it was given.** Rather than
            # map the wrong vectors onto the wrong chunks - which would be
            # silent, permanent and invisible to every test - fall back to the
            # plain path and let its own count check catch it.
            self._log.warning(
                "the model returned {} vectors for {} passages; not reusing",
                len(embedded), len(unique))
            return list(self.embedder.embed_all(texts))

        self._stats_ref.chunks_deduped += saved
        return [embedded[index] for index in where]

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
        # §6a's two most decision-shaped numbers. A run where `embed` dominates
        # wants a bigger batch or the graphics card; one where `vectors` does
        # wants a faster drive. Neither is guessable from the outside, and both
        # are one `perf_counter` pair here.
        with self._clock.stage("embed"):
            vectors = self._embed_texts(texts)

        # **Deleted here, one instant before the add** - see `_write_one` for
        # why this is not up there any more. A re-index must not leave the old
        # vectors behind, but it must not remove them until the replacements are
        # ready either, and "ready" means embedded rather than merely intended.
        #
        # After `embed_all`, deliberately: embedding is where a run dies, and a
        # model that will not load must cost nothing at all rather than cost
        # every file in the batch its existing coverage.
        #
        # `delete_by_file_ids` returns immediately when the table is empty,
        # which is the whole of a first index, so this is free on the run that
        # does the most work.
        self.vectors.delete_by_file_ids(
            list(dict.fromkeys(fid for _c, fid, _t in pending)))

        with self._clock.stage("vectors"):
            written = self.vectors.add(
                chunk_ids=[cid for cid, _f, _t in pending],
                file_ids=[fid for _c, fid, _t in pending],
                vectors=vectors,
            )
        # **The return value was ignored, and that is how a file ends up
        # INDEXED with no vector.**
        #
        # `add` returns how many rows it wrote. Marking the chunks embedded and
        # the files INDEXED regardless means a write that produced nothing is
        # recorded as complete - and because the file is then INDEXED and
        # unchanged, every later run skips it. It is stuck permanently, and the
        # only symptom is that meaning-based search quietly covers less of the
        # corpus than it claims.
        #
        # Left PENDING instead, which is the state that gets retried: the
        # chunks are already written, so the next run re-embeds them and costs
        # nothing else. Loud, because a silent one is what produced 559 missing
        # vectors with the run reporting success.
        #
        # `None` means the store did not report - an older double, or a
        # stub. Not second-guessed: silently treating "no answer" as
        # "failed" would leave every file PENDING for ever.
        if written is not None and written < len(pending):
            self._log.error(
                "wrote {} vectors for {} passages - the rest stay PENDING and "
                "will be retried. Meaning-based search covers less of the "
                "corpus until then; `app.cli reembed` fixes it now.",
                written, len(pending),
            )
            self._stats_ref.embed_failures += 1
            self._stats_ref.vectors += int(written or 0)
            pending.clear()
            return
        self._stats_ref.vectors += (
            int(written) if written is not None else len(pending))
        # One transaction for the whole batch. This was a commit per chunk-set
        # plus a commit per file - dozens of them, for one logical step.
        with self.store.batch():
            self.store.mark_embedded(cid for cid, _fid, _t in pending)
            self.store.mark_indexed_many(
                dict.fromkeys(fid for _c, fid, _t in pending))
        pending.clear()

    def _record_skip(self, item: _Extracted) -> None:
        r"""A file something tried to read and could not. **Still findable.**

        `ext` and `parent_dir` are filled here for the same reason
        `_write_name_only` fills them: §1a's promise is that every file in an
        indexed folder is findable by its **name, its folder and its type**, and
        none of those three depend on whether the contents could be read. They
        were simply never passed, so a corrupt archive or a locked document
        answered to its name and then vanished from `/type zip` and from a
        search on the folder holding it - which reads as the index having lost
        the file rather than having failed to open it.
        """
        assert item.error is not None
        candidate = item.candidate
        with self.store.batch():
            file_id = self.store.upsert_file(
                str(candidate.path),
                size_bytes=candidate.size_bytes,
                mtime_ns=candidate.mtime_ns,
                content_hash=item.content_hash,
                clear_hash=item.content_hash is None,
                status=FileStatus.PENDING,
                parent_dir=str(candidate.path.parent),
                ext=indexed_ext(candidate.path),
            )
            self.store.mark_skipped(file_id, item.error)

    # -- guards and bookkeeping ---------------------------------------------

    def _checkpoint(self, candidate: Candidate, stats: IndexStats) -> None:
        r"""Progress for the UI. The `files` table is what actually resumes.

        **"For the UI" was aspirational for a long time.** These two keys were
        written on every checkpoint and read by nothing outside the test suite,
        so a run started from the command line was invisible to an open window:
        the bar sat at zero and Start stayed enabled while an index was plainly
        under way.

        `run_lock.publish` is the half that was missing. It writes a snapshot of
        the whole `IndexStats` under one key, which the window polls - see
        `IndexingView._watch_external`. One JSON blob rather than a spread of
        keys, so a reader in another process cannot catch a half-written set and
        draw a bar from two different instants.
        """
        # Two keys, one commit. `set_states` already existed for exactly this
        # and the checkpoint simply was not using it.
        self.store.set_states({
            "cursor:last_path": str(candidate.path),
            "cursor:indexed": str(stats.indexed),
        })
        publish(self.store, owner=self.run_owner,
                started_at=self._run_started_wall, stats=stats)

        # **A stop asked for by somebody else.** The Stop button in the window
        # has to work on this run even when the window did not start it, and the
        # two processes share nothing but the database. Polled here, between
        # files, so it is honoured the same way the in-process stop is: finish
        # what is open, keep everything already written.
        if stop_requested(self.store):
            self._log.info("stopping: another process asked this run to stop")
            self.request_stop()

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
        stats.paused = self.governor.paused
        stats.pause_reason = self.governor.pause_reason

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
        # **Rows under a skipped archive are left alone without being stat'd.**
        #
        # The `exists()` test below would keep them anyway - they are still on
        # disk - but it is one syscall per row, and on a 1.5TB archive that is
        # several million syscalls at the end of every incremental run, to
        # confirm that a folder nobody has touched in twelve years still
        # contains what it contained. That cost is precisely what marking a
        # root as an archive is meant to remove; skipping the walk and then
        # paying it here would have saved almost nothing.
        from app.index.archives import files_under

        archived = self._archive_roots()

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
            if str(record.path).lower() not in seen
            and (not archived or files_under(record.path, archived) is None)
            and not Path(record.path).exists()
        ]
        # **The archive's contents go with the archive**, which nothing did.
        # The loop above only ever looked at `source_kind="file"`, and no other
        # deletion path exists for `archive`, `pst_message` or `eml` rows - so
        # deleting a 30GB `.pst` removed the marker row and left two hundred
        # thousand messages searchable for ever, every one of them opening to
        # nothing. Collected before deleting, for the same reason as above.
        doomed.extend(self._doomed_inside_archives(seen, archived))
        return self._delete_in_batches(doomed)

    def _doomed_inside_archives(self, seen: set[str], archived: Any) -> list[int]:
        r"""Ids of the marker **and the contents** of an archive that has gone.

        **`source_kind="archive"` is not "the archive file".** It is every row
        that came out of one - the container's marker *and* each member - which
        is the thing the first version of this got wrong: it treated every such
        row as a container, found that `backup.zip/q3/plan.dwg` is not a path on
        disk, and deleted the members of perfectly healthy archives.
        `test_archive_reading` caught it immediately.

        A member's path is its container's path plus a separator, so the two are
        told apart by asking which paths are real files. Anything under a
        container that is still on disk is alive, whatever its own path says.

        **The parent folder must still exist**, and that guard is the difference
        between pruning and data loss. One missing `.pst` is 200,000 rows; a
        disconnected network drive or an unmounted volume makes *every* path
        under it stop existing at once. If the folder is there and the archive
        is not, it was deleted. If the folder has gone too, this run knows
        nothing and does nothing.
        """
        from app.index.archives import files_under

        rows = [(record.id, str(record.path))
                for record in self.store.iter_files(source_kind="archive")]
        if not rows:
            return []

        def prefixes(paths: Any) -> tuple:
            """Each path as the two forms a member of it could start with."""
            return tuple(
                str(path).rstrip("\\/") + sep
                for path in paths for sep in ("\\", "/")
            )

        # **Containers are decided first, members follow.** A member cannot be
        # judged on its own: its path is never a file on disk, and its parent
        # folder is the container - which is exactly the evidence the
        # offline-drive guard below is looking at. Ask about the container, then
        # let everything inside inherit the answer.
        every_prefix = prefixes(path for _id, path in rows)
        containers = [(file_id, path) for file_id, path in rows
                      if not path.startswith(every_prefix)]

        doomed_paths: list[str] = []
        gone: list[int] = []
        for file_id, path in containers:
            if Path(path).exists():
                continue                      # still here; its members are fine
            if path.lower() in seen:
                continue                      # this walk covered it; the walk decides
            if archived and files_under(path, archived) is not None:
                continue                      # inside a folder this run skipped
            if not Path(path).parent.exists():
                # The folder, the drive or the share is missing - not the
                # archive. Deleting on that evidence is how an offline network
                # drive costs somebody their mail index, 200,000 rows at a time.
                continue
            gone.append(file_id)
            doomed_paths.append(path)

        if doomed_paths:
            inside = prefixes(doomed_paths)
            gone.extend(file_id for file_id, path in rows
                        if path.startswith(inside))
        return gone

    #: Rows per delete. One `IN (...)` list of a million ids is a query nobody
    #: can plan; a few thousand is one statement and one Lance version.
    PRUNE_BATCH = 2_000

    def _delete_in_batches(self, doomed: list[int]) -> int:
        r"""Remove these files, their chunks and their vectors. **In batches.**

        This was `for file_id in doomed:` with a `delete_by_file_ids([file_id])`
        and a `delete_file(file_id)` inside it - one LanceDB **dataset version**
        and one SQLite write transaction per file. Deleting a folder of ten
        thousand files produced ten thousand Lance versions, which is precisely
        the fragmentation `COMPACT_EVERY_ROWS` exists to prevent, and ten
        thousand commits at the very end of a run.

        Batched, that is five Lance deletes and five transactions.

        The order within a batch is deliberate and matches `_embed_pending`:
        vectors first, then SQLite. A crash between them leaves vectors for rows
        that still exist - harmless, they are simply re-deleted next time - where
        the reverse leaves vectors whose file row has gone, which is the
        orphaned-vector state that has no route back.
        """
        if not doomed:
            return 0
        for start in range(0, len(doomed), self.PRUNE_BATCH):
            batch = doomed[start:start + self.PRUNE_BATCH]
            self.vectors.delete_by_file_ids(batch)
            with self.store.batch():
                for file_id in batch:
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
