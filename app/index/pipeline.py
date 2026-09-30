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

**§6b: a feeder thread carries the embedding and the vector write.** The
consumer still does everything up to the batch threshold itself - extraction
results, the SQLite write - but past it hands the batch to a dedicated thread
and moves straight on to gathering the next one, rather than blocking on the
model and the Lance write itself. Batch N+1's chunks are written while batch
N embeds: on a graphics card this is what keeps it fed, on a processor it
overlaps the model with disk I/O instead of paying for the two in sequence.
Two points still block on it deliberately, because §6b must not undo the M6
fix: an archive's completion marker (vectors before marker) and the very end
of a run (nothing may return claiming success before the last batch is
actually written). One feeder thread, one batch at a time - nothing here has
had to be safe against two concurrent writes into the vector store, and
still does not.
"""

from __future__ import annotations

import copy
import hashlib
import itertools
import os
import queue
import threading
import time
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

from app.core.errors import AppError, AppErrorException, make_error, to_app_error
from app.core.logging import logger
from app.extract import chunk_document, extract
from app.extract import progress as reader_progress
from app.extract import reading as reader_reading
from app.extract.base import extractor_for, reads_externally
from app.core.priority import lower_this_thread
from app.core.osbridge.pathnames import path_key
from app.core.run_lock import COMMAND_LINE, publish, stop_requested
from app.extract.source_types import indexed_ext
from app.index import backends
from app.index.activity import (
    KIND_ARCHIVE,
    KIND_ARCHIVE_COUNTS,
    KIND_FINISHED,
    KIND_LARGE_FILE,
    KIND_NOTICE,
    KIND_PAUSE,
    KIND_PHASE,
    KIND_RESUME,
    KIND_STOPPING,
    KIND_WARNING,
    ActivityLog,
    encode_counts,
    large_file_kind,
)
from app.index.clip_embedder import ClipImageEmbedder
from app.index.held_archives import HeldArchives
from app.index.image_book import PersistentImageBook
from app.index.embedder import CPU_INFER_BATCH
from app.index.embedder import EMBED_BATCH as _EMBED_BATCH
from app.index.embedder import Embedder
from app.index.interrupted import ARCHIVE_RESUME_PREFIX
from app.index import live_progress
from app.index.live_progress import (
    STAGE_CHUNKING,
    STAGE_EMBEDDING,
    STAGE_READING,
    STAGE_SAVING_RESUME,
    STAGE_WRITING,
    STAGES,  # noqa: F401 - re-exported: `pipeline.STAGES` is the one ordered list
    WorkerBoard,
)
from app.index.file_watch import (
    GRACE_S,
    LIMIT_STALL,
    ORPHANED,
    FileCancelled,
    FileTimedOut,
    FileWatch,
    Watchdog,
    limit_kind,
)
from app.index.phash import PhashComputer
from app.index.read_process import ReaderProcess, reads_in_process
from app.index.resources import (
    MANUAL_PAUSE_REASON,
    ResourceGovernor,
    ResourceLimits,
    SystemProbe,
    Verdict,
)
from app.index.read_order import ORDER_NEWEST, WorkList, normalise_order
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
from app.storage.filters import MAIL_KINDS
from app.storage.sqlite_store import FileStatus, SqliteStore
from app.storage.vector_store import ImageVectorStore, VectorStore

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

#: How late the interface's own timer must be running before the consumer
#: yields, and the longest single yield. 0.2s is where a keystroke starts to feel
#: delayed; 0.25s keeps one yield well under the point a run visibly crawls, and
#: the sleep repeats per item for as long as the lag lasts.
UI_LAG_YIELD_S = 0.2
UI_YIELD_MAX_S = 0.25

#: Rows per LanceDB append. From the spec; large enough to amortise the write,
#: small enough that a crash loses little.
VECTOR_BATCH = 1000

#: Work order 202626270509, item 1b. The `Document.meta` key an extractor
#: that opts into `supports_resume` (see `app/extract/base.py`) uses to
#: report its own furthest-processed position within one file, so `_consume`
#: can persist it as a resume cursor once that position is confirmed durable
#: - see `_note_resume_progress` and `_persist_resume_progress`.
RESUME_POSITION_META_KEY = "mbox_index"

#: `index_state` key prefix for a per-file resume cursor, keyed on that
#: file's own content hash so a changed file never resumes into stale bytes.
#: Also matched by `SqliteStore.clear_index()`'s reset delete.
RESUME_STATE_PREFIX = "resume:"

#: Work order `dates-live-log-and-interrupted-runs` 3b. The folder cursor an
#: archive extractor (`pst_libpff.read_archive`) stamps on each document, and
#: the message count before that folder. Spelt the same as there; not imported,
#: because importing the libpff reader here would load it for every run.
ARCHIVE_FOLDER_META_KEY = "pst_folder"
ARCHIVE_READ_META_KEY = "pst_read_before"

# `ARCHIVE_RESUME_PREFIX` (imported above) is the `index_state` key prefix for
# an archive's folder cursor. **Keyed on the archive's path, not a content
# hash**: an archive is read externally (`reads_externally`) and never hashed,
# so there is no digest to key on. The cursor records the archive's size and
# modified time instead and is used only while both still match - a changed
# archive starts from the top and overwrites it. Under `RESUME_STATE_PREFIX`, so
# a reset clears it too. Declared beside its reader, `app/index/interrupted.py`
# (3c: the Indexing page lists these), which must not import this module.

#: The most often an archive's folder cursor is written *during* a run, at a
#: folder boundary. **What a pulled plug costs**, at most: the folders finished
#: since the last write are re-parsed (their messages are skipped by text hash,
#: so nothing is embedded twice). Each write first waits for the embedding
#: thread to catch up - the cursor may only point past what is durable - so
#: writing at every boundary would give up the overlap between reading and
#: embedding on an archive of many small folders. Thirty seconds bounds the
#: loss to seconds of parsing and the cost to one short wait a half-minute.
#: A fixed constant (non-negotiable #11): nobody could choose it better than
#: this, and a slower machine only makes the same trade in the same place.
RESUME_PERSIST_S = 30.0

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

#: The stretches of a run, in the order `run` enters them. Announced on
#: `IndexStats.phase` with a progress tick as each one starts.
#:
#: **Only `PHASE_READING` has anything to count.** The rest - loading the
#: model, catching up on an earlier run, planning the roots, building the
#: vector index - can each take minutes on a big index and used to emit no
#: tick at all, so the bar sat still on whatever it last showed and the run
#: was reported as stalled. The words for each live in the presenter
#: (`PHASE_WORDS`); these are only the keys.
PHASE_MODEL = "model"
PHASE_WORD_INDEX_CHECK = "word_index_check"
PHASE_CATCH_UP = "catch_up"
PHASE_PLANNING = "planning"
#: 2026-09-29. `read_order` "newest": the walk and the cheap change check run
#: to the end before anything is read, and the list is sorted. `seen` climbs
#: while it lasts; `walk_complete` turns true when it ends, and the bar counts
#: against a real total from the first file read.
PHASE_SCANNING = "scanning"
PHASE_READING = "reading"
PHASE_MEDIA = "media"
PHASE_TIDYING = "tidying"
PHASE_VECTOR_INDEX = "vector_index"
PHASE_WORD_INDEX = "word_index"

#: Seconds between progress ticks while the run is waiting on a reader. Work
#: order 0x section 3d: "a heartbeat once a second".
#:
#: **Only the waiting branch uses it.** When documents are flowing, ticks come
#: from `checkpoint_every`/`checkpoint_seconds` as they always have, and
#: activity is obvious anyway. The quiet stretch - one reader deep inside a
#: 4GB archive, nothing finished yet - is exactly when the page needs a fresh
#: "message 4,512 of 18,300" and a fresh "last activity" every second, and it
#: used to get one every `CHECKPOINT_SECONDS`. A tick is one shallow copy of
#: the stats and one signal, so once a second is free. A constant, not a
#: setting (non-negotiable 11): nobody would tune it, and the page's own
#: repaint limit already stops faster ticks costing anything.
HEARTBEAT_SECONDS = 1.0


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
    #: 2026-09-20. **Which kind of pause this is.** "Paused" and "Paused - the
    #: computer is busy" are different sentences to the person reading them:
    #: one is waiting for them and one is waiting for the machine, and only
    #: the first has a button that ends it. `paused` alone could not tell them
    #: apart, so the flag is carried rather than guessed from the reason text -
    #: wording is meant to be free to change.
    paused_by_person: bool = False
    #: Of `paused_seconds`, the share the person asked for. Reported apart so
    #: "waited to stay out of the way" stays a true sentence.
    manual_paused_seconds: float = 0.0
    #: True once the walker has finished finding files, which is the moment
    #: `seen` stops being a running tally and becomes a total. Nothing can show
    #: an honest percentage before it.
    walk_complete: bool = False
    #: Which `PHASE_*` the run is in, or "" before the first. Only the one
    #: thread that runs `Pipeline.run` writes it.
    phase: str = ""
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
    #: Work order 0i section 2a: the unified enrichment backlog's per-kind
    #: counts for THIS run - "unembedded_chunk" is `vectors_repaired` under
    #: its generic name (kept as a duplicate rather than a replacement, so
    #: nothing reading `vectors_repaired` directly has to change). Other
    #: kinds appear here once a drain for them exists; a kind absent from
    #: this dict was not run this pass, not "ran and found nothing" - see
    #: `Pipeline._ENRICHMENT_DRAINS`.
    enrichment_counts: dict[str, int] = field(default_factory=dict)
    #: Files the walk could not `stat`, by reason. **Not skips**: a skip has
    #: a row explaining itself, and these have no row at all. Reported so
    #: that files invisible to the whole application are at least a number -
    #: see `walker._record_stat_failure`.
    unreachable_by_reason: dict[str, int] = field(default_factory=dict)
    #: Extension -> how many files were dropped for being over the size
    #: ceiling, outside a name-only run. **Not skips**, same reasoning as
    #: `unreachable_by_reason`: these have no row at all. See
    #: `WalkConfig.oversize_dropped`.
    oversize_dropped: dict[str, int] = field(default_factory=dict)
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
    #: Work order 0w §2c. When each of `notices` was said, as `time.time()`,
    #: index for index. **A parallel list rather than a change of type**, so
    #: every reader of `notices` - the CLI, the finished panel, the run
    #: record, a dozen tests - still gets the plain strings it always did.
    #: Filled by `add_notice`; a notice appended directly is stamped by
    #: `stamp_notices` at the next snapshot instead, so none goes untimed.
    notice_times: list[float] = field(default_factory=list)
    #: Work order 0w §2a. The run's own timestamped story - see
    #: `app/index/activity.py`. Not in `as_dict`: the run record is written
    #: into the store every run and has no use for a few hundred lines.
    activity: ActivityLog = field(default_factory=ActivityLog, repr=False,
                                  compare=False)
    #: Warning code -> how many documents carried it.
    #:
    #: Warnings live on documents that indexed *successfully*, so none of them
    #: reached `skipped_by_code` and the only record was a log line. That made
    #: "412 decks are mostly images" - the input to the Office OCR decision -
    #: a question nobody could answer without grepping.
    warned_by_code: dict[str, int] = field(default_factory=dict)
    #: Order 0z lane D. Pictures attached to mail that the junk-image filter
    #: left unread, per reason (`junk_images.REASONS`: decorative, repeated,
    #: few_words). Each was also one `Skipped` in its archive's counts line;
    #: this is the run's total, for the Indexing page.
    pictures_not_read: dict[str, int] = field(default_factory=dict)
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

    #: Work order 0x section 3c. **One line per reader**, as plain values:
    #: `{"1": {"file", "path", "started_at", "stage", "item", "inner"}, ...}`,
    #: where `inner` is the reader's frame stack from `app.extract.progress`
    #: (outermost first; each frame `{"kind", "name", "unit", "n", "total",
    #: "where", "stage", "detail"}`). Filled from the live `WorkerBoard` each
    #: time a snapshot is taken, so the per-message cost is zero and the
    #: result is JSON as it stands. `current`/`current_item` above are still
    #: written exactly as before, for everything that reads them.
    workers: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: Work order 0x section 3d. **When anything last moved**, as wall-clock
    #: `time.time()`; 0.0 before the first snapshot. Moved forward at snapshot
    #: time when the run's signature changed - see `live_progress` for why it
    #: is not stamped per message. The presenter turns it into "last activity
    #: 2 s ago" against its own clock.
    last_activity: float = 0.0
    #: What the consumer thread is doing when it is not simply taking the next
    #: document: `STAGE_WRITING` or `STAGE_SAVING_RESUME`, or "". The one
    #: thread that runs `_consume` writes it.
    stage: str = ""
    #: "Embedding, batch n of m" (0x section 3a). `embed_batch` is the batch
    #: the model is on now, counting from 1, and 0 when it is idle;
    #: `embed_batches` is how many batches have been handed to it this run,
    #: so m grows while the run reads - it is the honest total so far, not a
    #: forecast. Written by the consumer (m) and the feeder thread (n).
    embed_batch: int = 0
    embed_batches: int = 0

    def __post_init__(self) -> None:
        # The live board is an attribute, not a field: see `WorkerBoard`.
        # `None` on a snapshot (set in `snapshot`), so refreshing a snapshot
        # can never wipe the `workers` it was taken with.
        self.board: Optional[WorkerBoard] = WorkerBoard()

    def refresh_live(self, *, now: Optional[float] = None) -> None:
        """Copy the readers' live positions onto `workers`, and beat the heart.

        Called at every snapshot - about once a second while a reader is busy -
        on the run's own thread. Does nothing on a snapshot (no board). Never
        raises: this is reporting, and reporting must never cost the run.
        """
        board = self.board
        if board is None:
            return
        try:
            workers = board.workers()
            self.workers = workers
            counters = (
                self.seen, self.indexed, self.skipped, self.unchanged,
                self.unchanged_documents, self.chunks, self.vectors,
                self.deleted, self.name_only, self.phase, self.stage,
                self.embed_batch, self.embed_batches, self.walk_complete,
            )
            if board.moved(live_progress.signature(counters, workers)):
                self.last_activity = (now if now is not None
                                      else live_progress.now_wall())
        except Exception:                        # noqa: BLE001 - reporting only
            return

    def snapshot(self) -> "IndexStats":
        r"""A copy the interface can read without racing the run.

        **The live object was being handed across threads.** The walker, every
        extraction thread and the consumer all mutate these counters and
        dictionaries while the window iterates them to paint the skip summary;
        the failure is "dictionary changed size during iteration" inside a
        slot, which PyQt turns into an abort. Copying on the run's own thread
        makes that impossible, and costs one shallow copy per checkpoint.

        Each container is copied with a single C-level call, which holds the
        interpreter lock for its whole duration.
        """
        self.stamp_notices()
        self.refresh_live()
        clone = copy.copy(self)
        for spec in fields(self):
            value = getattr(clone, spec.name)
            if isinstance(value, (dict, list, set)):
                setattr(clone, spec.name, type(value)(value))
        # Its own lock, not the interpreter's: see `ActivityLog.copy`.
        clone.activity = self.activity.copy()
        # 0x 3c: one level deeper than the loop above, so no worker's dict or
        # frame list is shared with the live object; and no board, so the
        # snapshot is plain data through and through.
        clone.workers = live_progress.plain_copy(self.workers)
        clone.board = None
        return clone

    def add_notice(self, text: str) -> None:
        """Say a notice, and remember when. Work order 0w §2c.

        The text is stored exactly as given - the time goes beside it, never
        into it - and the same moment becomes the notice's line in the log.
        """
        with self.activity.lock:
            at = time.time()
            self.notices.append(text)
            self.notice_times.append(at)
            self.activity.record(KIND_NOTICE, text, at=at)

    def stamp_notices(self) -> None:
        """Time and log any notice that was appended without `add_notice`.

        **A safety net, not the way in.** `notices` is a public list and a
        caller that appends to it directly - an older path, or a change made
        without knowing about this one - would otherwise leave a notice with
        no time and no log line. One length comparison when there is nothing
        to do, which is every tick but the one after such an append.
        """
        if len(self.notice_times) >= len(self.notices):
            return
        with self.activity.lock:
            now = time.time()
            while len(self.notice_times) < len(self.notices):
                text = self.notices[len(self.notice_times)]
                self.notice_times.append(now)
                self.activity.record(KIND_NOTICE, str(text), at=now)

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
            "paused_by_person": self.paused_by_person,
            "paused_by_person_s": round(self.manual_paused_seconds, 1),
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
            "enrichment_counts": dict(self.enrichment_counts),
            "unreachable_by_reason": dict(self.unreachable_by_reason),
            "oversize_dropped": dict(self.oversize_dropped),
            "root_problems": dict(self.root_problems),
            "skipped_roots": list(self.skipped_roots),
            "notices": list(self.notices),
            "warned_by_code": dict(self.warned_by_code),
            "pictures_not_read": dict(self.pictures_not_read),
            "ocr_mode": self.ocr_mode,
            "stopped_early": self.stopped_early.code if self.stopped_early else None,
            # §6a. Omitted entirely when nothing was measured, so a run too
            # short to time prints nothing rather than a row of zeroes that
            # reads as "every stage took no time".
            **({"stages": dict(self.stages)} if self.stages else {}),
            **({"worker_seconds": dict(self.worker_seconds)}
               if self.worker_seconds else {}),
            **({"resolved": dict(self.resolved)} if self.resolved else {}),
            # 0x 3c/3d. Only while there is something to say, like the
            # three above: a finished run has no readers, and the run record
            # written into the store every run has no use for an empty map.
            **({"workers": live_progress.plain_copy(self.workers)}
               if self.workers else {}),
            **({"last_activity": self.last_activity}
               if self.last_activity else {}),
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
    #: Work order 0x §5b. Read files in a process per extraction thread
    #: (`app/index/read_process.py`), so readers stop taking turns on one
    #: interpreter lock. Only readers that are pure file parsing move; every
    #: other file is read on the thread as before. Off by default: the
    #: "Read files in separate processes" switch on the Indexing page.
    read_processes: bool = False
    #: Work order 0z lane B: the time limits (`app/index/file_watch.py`).
    #: Seconds a text or code file may take to read; other single documents
    #: get `file_watch.LONG_FACTOR` times this. 0 is no limit. The Indexing
    #: page's "Time limit per file" (`INDEX_FILE_TIME_LIMIT_S`).
    file_time_limit_s: int = 120
    #: Seconds a mailbox or archive may go with nothing new read before it is
    #: skipped. Never a limit on its total time. 0 is no limit.
    #: `INDEX_STALL_LIMIT_S`.
    stall_limit_s: int = 600
    #: Which pass this is. See `OCR_MODES` and `_ocr_gate`.
    #:
    #: **OCR is the schedule, not a feature.** At 3.6 seconds a page, 100,000
    #: scanned pages is 100 hours on its own - so a single pass that reads text
    #: and images together means nothing is searchable until everything is.
    #: `text` indexes everything readable without OCR and *queues* the images;
    #: `images` picks up exactly that queue. Search becomes useful after the
    #: first, in a day or two rather than a fortnight.
    ocr_mode: str = "both"
    #: Order 0z lane D: leave signature logos, social icons, tracking pixels
    #: and dividers attached to mail unread (`app/extract/junk_images.py`).
    #: From `INDEX_JUNK_IMAGE_FILTER`; on by default.
    junk_images: bool = True
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
    #: §6g. Extraction workers may grow past the static count above, one at a
    #: time, up to this many - never past it. `0` (the default) disables
    #: growth entirely and is exactly today's behaviour: the static count is
    #: the count, for every existing caller that has not set this.
    #:
    #: **The floor of a range, not the answer** - the static `workers` value
    #: stays the safe number to start at (and the number a machine with
    #: nothing better to offer falls back to); this is only ever a ceiling
    #: raising *toward*, never a replacement for it. Resolving this from the
    #: envelope is `resolve.py`'s job, the same as every other tunable here -
    #: `Pipeline` only ever spends what it is given.
    worker_ceiling: int = 0
    #: Work order 0i section 3b. OFF by default - see `app.core.
    #: settings_registry.CAPTION_TRICKLE_ENABLED` for the reasoning. Read by
    #: `_drain_caption_trickle`, which the pipeline has no `Settings` object
    #: to consult directly - the same reason `sidecar_dir` above is passed
    #: rather than derived.
    caption_trickle_enabled: bool = False
    #: Where and which model `_drain_caption_trickle` asks, when the switch
    #: above is on. Defaults match `app.llm.ollama.OllamaClient`'s own and
    #: `app.extract.vision_caption.DEFAULT_VISION_MODEL`.
    ollama_url: str = "http://127.0.0.1:11434"
    ollama_vision_model: str = "llava"
    #: `CHAT_ENGINE` (2026-09-29). With `onnx` the caption trickle does not run:
    #: Florence-2 already writes each photo-class image's AI description while
    #: it is indexed, and a second Florence pass would cost seconds a photo for
    #: the same words.
    chat_engine: str = "onnx"
    #: Work order 0j, the whole order. OFF by default - see `app.core.
    #: settings_registry.PEOPLE_RECOGNITION_ENABLED`. Read by `_write_one`
    #: (the images-pass face step, section 1a) and `_drain_face_backfill`
    #: (the enrichment-backlog kind for already-indexed photos, section
    #: 1a's own "backfill... runs as an enrichment-backlog job kind").
    people_recognition_enabled: bool = False
    #: Work order 202626270515 (video and audio): a `media.MediaConfig`, or None
    #: for "both layers off" - which is what every caller that has never heard
    #: of video means. Typed `Any` so this module need not import the extractor
    #: at load time. Handed to `media.configure` at the top of `run()`, because
    #: the walk decides which extensions exist from it.
    media: Optional[Any] = None
    #: Work order 202626130120 (0t) section 6: "" unless `resolve_for_run`
    #: found that this machine had a working DirectML provider last time and
    #: genuinely does not have one now. Carried through unchanged rather than
    #: recomputed here - `Pipeline` has no `Settings` and no store-independent
    #: way to know "last time" (see `resolve._gpu_regression`'s own docstring
    #: for why the cached profile cannot be trusted for this one question).
    gpu_regression_notice: str = ""
    #: 2026-09-20. A file whose presence holds the run, and whose removal
    #: resumes it - the command line's half of the pause button (see
    #: `app/cli/index.py`). `None`, so a run nobody asked to be pausable
    #: behaves exactly as it always did and stats nothing per file.
    pause_file: Optional[Path] = None
    #: 2026-09-29. The order files are read in - see `app/index/read_order.py`.
    #: `newest` scans the whole walk first, then reads the folders chosen
    #: first (`walk.priority_roots`), then everything else newest first, small
    #: before large within a month. `found` streams the walk straight into the
    #: queue, which is how every run behaved before. From `INDEX_ORDER`.
    read_order: str = "newest"
    #: Work order 0z F1 (`app/index/folder_watch.py`). **Where this run's files
    #: come from, when it is not a walk of `walk.roots`.** Called once with the
    #: run's `WalkConfig` and its shared `seen` set, and yields `Candidate`s -
    #: the handful of files the folder watch was told about. Everything after
    #: that is the ordinary run: the same change check, the same readers, the
    #: same writes. None (the default) is the walk every run has always done.
    candidate_source: Optional[Callable[[Any, set], Iterator[Candidate]]] = None
    #: Work order 0z F1. **A few files, not a run somebody started.** True
    #: leaves out everything in `run()` whose cost grows with the size of the
    #: whole index rather than with this run's files - catching up earlier
    #: runs' backlogs, the queued recordings, the forced compaction of the
    #: vector index, the completions file - and does not overwrite the "last
    #: run" record the Indexing page shows. The next ordinary run does all of
    #: them, as it always has. False (the default) changes nothing.
    light: bool = False
    #: Work order 0z F1. Folders the clean-up pass is limited to: only rows
    #: under one of them can be removed as "no longer on disk". Set for a run
    #: over part of the index that *did* look at the whole of those folders
    #: (the watch's "rescan this folder"), where an unlimited clean-up would
    #: `stat` every row of every other folder to learn nothing. None (the
    #: default) is the whole index, as before.
    prune_under: Optional[tuple] = None

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
    #: Work order `dates-live-log-and-interrupted-runs` 3b. The `index_state`
    #: key of this file's archive folder cursor (`ARCHIVE_RESUME_PREFIX`), or
    #: None for anything that has none. Set on every item from such a file,
    #: including its marker, which is what clears the cursor.
    resume_key: Optional[str] = None

    @property
    def row_key(self) -> str:
        r"""What `files.path` should hold for this document.

        **Volume-aware, and this is 1c's dedup/prune keying.** A file the
        walker found under a catalogued source's current mount point carries
        `candidate.volume_id`/`relative_path` (see `walker.Candidate`), and
        this builds the same letter-free synthetic string every time,
        whatever letter the drive happened to have *this* walk - so the same
        file seen as `E:\a.txt` then `F:\a.txt` is one row, not two, without
        `_prune_missing` or anything else having to know why.

        `self.key` still wins when an extractor has set one - an archive
        member's key already encodes its container's path. A `.pst` catalogued
        on an offline drive is a known gap this pass does not close: its
        messages would still key off the container's *walked* path rather
        than the volume-synthetic one. Recorded in the work order's dated
        note rather than silently accepted.
        """
        if self.key:
            return self.key
        return _candidate_row_key(self.candidate)


def _archive_resume_key(path: Path) -> str:
    """The `index_state` key of one archive's folder cursor (0w 3b).

    A hash of the lower-cased path rather than the path itself: a key is a
    short identifier, and Windows paths differ only in case.

    **Hashed from `path_key` (order 0x section 7b).** On Windows that is
    `str(path).lower()`, byte for byte, so every cursor already saved on the
    owner's machine is still found. In a case-sensitive Mac or Linux folder the
    case is kept, so `Mail.pst` and `mail.pst` - two archives there - cannot
    share one cursor and resume from each other's place.
    """
    digest = hashlib.blake2b(path_key(path).encode("utf-8"), digest_size=16)
    return f"{ARCHIVE_RESUME_PREFIX}{digest.hexdigest()}"


def _candidate_row_key(candidate: "Candidate") -> str:
    """The row key for a bare candidate that never became a `Document` -
    a name-only file or one recorded as skipped. Mirrors `Document.row_key`'s
    volume-aware branch (1c); kept as a free function because both call sites
    only have the candidate, never a `Document` to ask."""
    if candidate.volume_id is not None and candidate.relative_path is not None:
        from app.storage.sqlite_store import volume_synthetic_path

        return volume_synthetic_path(candidate.volume_id, candidate.relative_path)
    return str(candidate.path)


def _candidate_parent_dir(candidate: "Candidate") -> str:
    """`files.parent_dir` for a bare candidate - the real folder for an
    ordinary file, and the equivalent synthetic folder for a volume-backed
    one, so browsing by folder never has to parse `leasha-volume://...`
    through `Path()`, which does not understand that scheme."""
    if candidate.volume_id is not None and candidate.relative_path is not None:
        from app.storage.sqlite_store import volume_synthetic_path

        parent = str(Path(candidate.relative_path).parent)
        parent = "" if parent == "." else parent
        return volume_synthetic_path(candidate.volume_id, parent)
    return str(candidate.path.parent)


_STOP = object()

#: How far the consumer may run ahead of the feeder thread, in whole batches
#: waiting in the handoff queue. One: the feeder is always working on a batch
#: the moment it has one, so this is not a throughput knob - a bigger number
#: would only let more embedded-but-not-yet-confirmed batches queue up in
#: memory for no more overlap than one slot already gives, since the consumer
#: has nothing else to do until the next `embed_batch` chunks accumulate
#: anyway.
_FEEDER_QUEUE_SIZE = 1

#: §6g. How dominant `waiting` must be, as a share of the critical path,
#: before another extraction worker is worth starting. The same threshold
#: `stages.advice()` uses for "this run is extraction-bound" - one number,
#: not two, for the same conclusion.
_GROWTH_WAITING_SHARE = 0.5

#: 2026-09-20. How long a paused worker sleeps between looks. Short, because
#: this is what "Resume" costs before work restarts, and a paused run has
#: nothing else to spend. 50ms x four workers is 80 flag reads a second
#: against a machine doing nothing at all.
HOLD_POLL_S = 0.05

#: 2026-09-29. Least time between two asks of the resource governor while the
#: "newest" order is scanning (`_produce`).
#:
#: **Measured, not assumed.** One ask costs 1.76ms in the sandbox this was
#: built in (9,002 asks: 15.9s; the bare walk of the same 9,002 files: 0.37s),
#: because each one reads the disk's free space and the process table. Once a
#: file was harmless there - the walk overlapped the reading - but a scan runs
#: *before* the reading, so every millisecond of it is a millisecond before the
#: first file is searchable. A scan holds nothing in flight for a pause to
#: drain, so asking four times a second still honours a pause, a battery, a
#: full disk or Stop within a quarter of a second. A constant: nobody would
#: tune it, and the evidence that would change it is a probe that got cheaper.
#:
#: **2026-09-30: the same interval now holds while files are read**
#: (`_governor_allows`). "Once a file was harmless there" was true of the
#: sandbox and not of Windows: measured on the owner's laptop, one ask is
#: 26-30ms, nearly all of it psutil's walk of the process table for child
#: processes, and once a file that was the slowest step of the whole run.
SCAN_GOVERNOR_S = 0.25

#: 2026-09-20. Least time between two looks at `PipelineConfig.pause_file`.
#: Every waiter asks the governor whether the person has paused, and without
#: this each ask would be a `stat()` - so the answer is cached for half a
#: second, which is well under the time anybody notices.
PAUSE_FILE_POLL_S = 0.5

#: §6g. Seconds between growth attempts. Checked at the same checkpoints as
#: everything else in `_consume`, so this is a ceiling on how often a new
#: thread can start, not a schedule of its own - a corpus of many tiny files
#: checkpoints often, and nothing here should turn that into a thread storm.
_GROWTH_COOLDOWN_S = 10.0

#: Work order 0x item 5d. **Documents written back to back share one SQLite
#: transaction**, up to this many of them - see `_begin_write_group` for the
#: whole story. A ceiling, not a target: a group is committed as soon as the
#: consumer runs out of ready documents, so on a run where reading is the
#: bottleneck it is usually one or two documents. 256 is far past the point
#: where BEGIN/COMMIT stop showing in a profile (they are two statements per
#: group, so at 256 documents they are under 1% of the statements).
WRITE_GROUP_MAX_DOCS = 256

#: Work order 0x item 5d. The longest one shared transaction stays open, in
#: seconds. **This is how long another writer can be kept waiting**: the
#: embedding thread's "these passages have vectors now", a window setting
#: saved from the queued writer, or - once the indexer is its own process -
#: the window's own writes, which then wait on SQLite's lock. A tenth of a
#: second is well under the 0.25 s the lag monitor calls a stall, and long
#: enough that commits are no longer a measurable share of the run.
WRITE_GROUP_MAX_S = 0.1


def _kept_on_cut_off(watch: Any, item: Any) -> bool:
    """Is `item` a message its reader handed over as its archive was cut off?

    Such a message is kept (`Pipeline._kept_in_hand` says why). Only a
    document of a mailbox or archive: not an error, not the archive's closing
    marker - an archive that was cut off is not marked as read - and nothing
    from a thread that was already replaced, whose file is recorded.
    """
    return (getattr(watch, "kind", None) == LIMIT_STALL
            and not getattr(watch, "orphaned", False)
            and getattr(item, "error", None) is None
            and not getattr(item, "file_marker", False)
            and not getattr(item, "name_only", False)
            and bool(getattr(item, "chunks", None)))


def _is_transient_partial(warning: Any) -> bool:
    """Was this an `ERR_PST_PARTIAL` whose cause will pass on its own?

    The extractor says so in the warning's context (`transient`); nothing here
    decides that Outlook being busy is temporary and a damaged archive is not.
    """
    if str(getattr(warning, "code", "") or "") != "ERR_PST_PARTIAL":
        return False
    context = getattr(warning, "context", None) or {}
    return bool(context.get("transient"))


class Pipeline:
    """Walk, extract, embed and write - resumably, and without falling over."""

    def __init__(
        self,
        store: SqliteStore,
        vectors: VectorStore,
        embedder: Embedder,
        config: PipelineConfig,
        governor: Optional[ResourceGovernor] = None,
        *,
        image_embedder: Optional[ClipImageEmbedder] = None,
        image_vectors: Optional[ImageVectorStore] = None,
        phash_computer: Optional[PhashComputer] = None,
    ) -> None:
        self.store = store
        self.vectors = vectors
        self.embedder = embedder
        self.config = config
        # Work order 0h §1a/§1b/§1c. Both `None` by default, and every existing
        # caller stays exactly as it was: nothing above constructs one of
        # these yet, and H4 says a lane that is not configured must behave
        # like a lane that does not exist, not like a broken one. See
        # `_maybe_embed_image` and `_flush_pending_images`.
        self.image_embedder = image_embedder
        self.image_vectors = image_vectors
        # Work order 0h §2a. `None` by default, same reasoning, and
        # deliberately independent of the two above: a pHash is not derived
        # from the CLIP model at all (see `app/index/phash.py`), so a run
        # with a working `image_embedder` and no `phash_computer` still
        # writes CLIP vectors and simply gets no perceptual hashes, and a run
        # with the reverse still gets hashes with no vectors. Neither implies
        # the other; see `_maybe_compute_phash`.
        self.phash_computer = phash_computer
        #: CLIP vectors computed but not yet written - see `_flush_pending_images`.
        #: Reset per run in `run()`, same as `_seen_paths` and the feeder queue.
        self._pending_images: list[tuple[int, list[float], str, int]] = []
        #: Work order 202626270515. A video's per-picture CLIP vectors, waiting for
        #: `_flush_pending_images` to write them after the file's mean. Keyed by
        #: file id: `(seconds, vector)` pairs, extension, mtime.
        self._pending_frames: dict[int, tuple[list[tuple[float, Any]], str, int]] = {}
        #: Work order 0h §2a. `file_id -> pHash hex string`, computed but not
        #: yet written - a dict, not a list like `_pending_images`, because a
        #: pHash is one value per file and a later write for the same file id
        #: within a batch should simply replace the earlier one rather than
        #: queuing a second `UPDATE` for it. Reset per run in `run()`, same as
        #: `_pending_images`. See `_maybe_compute_phash`/`_flush_pending_phashes`.
        self._pending_phashes: dict[int, str] = {}
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
        #: When `config.pause_file` was last looked at, and what it said. See
        #: `PAUSE_FILE_POLL_S`; set before the governor, which reads them.
        self._pause_file_checked = 0.0
        self._pause_file_seen = False
        self.governor = governor or ResourceGovernor(
            config.resolved_limits(),
            probe=SystemProbe(lambda: getattr(self.vectors, "uri", None)).read,
            on_state_change=self._on_throttle,
            # The command line's pause, polled by the one wait path rather
            # than by each waiter. A governor handed in by a test or by the
            # window is left exactly as it was built - `pause()` below still
            # works on it, only the file does not.
            manual_check=self._pause_file_set,
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
        #: Work order 202626270509, item 1b. `str(candidate.path) ->
        #: (content_hash, furthest confirmed-durable resume position)` for a
        #: file currently being extracted by a `supports_resume` extractor.
        #: Written by the consumer thread only, in `_note_resume_progress`;
        #: flushed to `index_state` in `_persist_resume_progress` and
        #: cleared in `_write_marker` once the file itself is done and the
        #: cursor is no longer needed. See the two for why this only ever
        #: holds *confirmed-embedded* positions, never merely-chunked ones.
        self._resume_progress: dict[str, tuple[str, int]] = {}
        #: Work order `dates-live-log-and-interrupted-runs` 3b. The same idea
        #: for an archive read by folders: `path -> cursor dict` (see
        #: `_note_archive_progress`). Consumer thread only, like the above.
        self._archive_cursors: dict[str, dict[str, Any]] = {}
        #: Cursors a worker found and resumed from, handed to the consumer so
        #: the attachments it names are carried into the next write. Written by
        #: extraction workers, taken by the consumer; a plain dict's single
        #: assignment and `pop` are atomic, which is all this needs.
        self._archive_resumed: dict[str, dict[str, Any]] = {}
        self._last_resume_persist = 0.0
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
        #: Work order 0x item 5d. The shared transaction the consumer is
        #: writing documents into, or None when there is none open. See
        #: `_begin_write_group`. Only ever touched on the consumer's thread.
        self._write_group: Any = None
        self._write_group_opened = 0.0
        self._write_group_docs = 0
        #: Video and audio extensions, looked up once per run rather than
        #: once per document. See `_media_work_ahead`.
        self._media_exts: Optional[frozenset[str]] = None
        #: Work order 0w §2a. A log to record into instead of the run's own -
        #: set by `media_backlog.drain` on the pipeline that reads the queued
        #: recordings, so the page's log carries straight on through that
        #: tail rather than starting again empty.
        self.activity_into: Optional[ActivityLog] = None
        #: Warning codes already given a line in the log this run. Written by
        #: the consumer only. See `_note_warnings`.
        self._warned_codes: set[str] = set()
        #: Which pause the log last said had started - "manual", "machine" or
        #: "" - so each is said once however many threads notice it, and
        #: "carrying on" is said only after a pause that was said.
        self._pause_said = ""
        self._pause_said_lock = threading.Lock()
        #: What `_plan_roots` decided this run. Read again at the end, to record
        #: a pass for every archival root that was walked in full.
        self._plans: tuple[Any, ...] = ()
        #: Monotonic marks for the daily summary line. Set in `run`.
        self._run_started = 0.0
        #: The same moment on `time.perf_counter`, for the walk and sort
        #: timings: `monotonic` ticks every ~15ms on Windows, so a short scan
        #: measured on it read 0s and was left out of the report (2026-09-29).
        self._run_started_pc = 0.0
        #: Wall-clock start, and who started it. `_run_started` is monotonic -
        #: correct for measuring elapsed time and meaningless to another
        #: process, which needs a clock it can format as "since 14:02".
        self._run_started_wall = 0.0
        #: Named on the published record so a refusal can say who holds the
        #: lock. Set by the window to `run_lock.GUI`; the default suits the CLI
        #: and every test that constructs a pipeline directly.
        self.run_owner = COMMAND_LINE
        #: True when this run shares its process with the interface. Set by
        #: `IndexWorker`, which also lowers the thread that calls `run`. The
        #: CPU courtesy is then per thread - every thread this run starts lowers
        #: itself - instead of lowering the process, which would lower the
        #: window with it. See `app.core.priority`.
        self.thread_priority_only = False
        #: Seconds the interface has recently been late, or None. Set by the
        #: window to `LagMonitor.recent_lag_s`; `_yield_to_ui` sleeps on it.
        self.ui_lag: Optional[Callable[[], float]] = None
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
        #: Work order 0u 6e. True once a stop made `_embed_pending` abandon part
        #: of a batch. Its files stay PENDING and are redone on resume - which
        #: is right - but that means neither their archive's completion marker
        #: nor a resume position past them may be written this run.
        self._embed_abandoned = False
        #: §6f: FTS trigger SQL for restoration after bulk insert. Stored here
        #: so _optimise_keyword_index can restore them at the end of the run.
        self._suspended_fts_triggers: list[str] = []
        #: §6b. The handoff to the feeder thread, and what it has to say if a
        #: batch fails. Built here, like `_clock`, so a `Pipeline` constructed
        #: and never run still has both; `run()` replaces them with fresh ones
        #: so a second run on the same instance never sees the first run's
        #: leftover queue or error.
        self._feeder_queue: "queue.Queue[Any]" = queue.Queue(maxsize=_FEEDER_QUEUE_SIZE)
        #: Exceptions the feeder thread caught rather than let vanish. A list
        #: rather than one slot: `_raise_if_feeder_failed` always re-raises the
        #: *first* one, which is the one that actually explains what went
        #: wrong - everything after it is the feeder thread already unwinding.
        self._feeder_errors: list[BaseException] = []
        #: Set in `run()` once the thread exists, so `_put_on_feeder` and
        #: `_wait_for_feeder` can tell "still working" from "gone" - the
        #: difference between waiting and giving up.
        self._feeder_thread: Optional[threading.Thread] = None
        #: §6g. Extraction workers started past the static count, in the order
        #: they were started. Only the consumer thread ever appends to this,
        #: from `_maybe_grow_workers`, so no lock guards it.
        self._dynamic_workers: list[threading.Thread] = []
        #: 0z lane B: threads started in place of one left stuck in a reader.
        self._replacement_workers: list[threading.Thread] = []
        #: 0z lane B: the thread idents of those stuck threads.
        self._left_behind: set[int] = set()
        #: 0z lane B: the per-file time limits and Force skip, for the run
        #: in progress. None between runs.
        self._watchdog: Optional[Watchdog] = None
        #: §6g. Set for as long as the feeder thread is actually inside
        #: `_embed_pending` - not merely "has a batch queued" - so growth can
        #: tell "the model is chewing through a batch right now" from "a
        #: batch is waiting its turn". Only matters together with the device
        #: check in `_maybe_grow_workers`: on a graphics card nothing here
        #: competes with extraction for the same cores.
        self._embedding_now = threading.Event()
        #: §6g. `time.monotonic()` of the last growth, so `_GROWTH_COOLDOWN_S`
        #: has something to measure against.
        self._last_growth = 0.0
        #: §6g. How many `_STOP` markers `_consume` must see in `results`
        #: before extraction is really finished - the static worker count,
        #: plus one more for every dynamically-started worker since each of
        #: those enqueues its own matching marker. Set in `run()`.
        self._expected_stops = 0

    def _media_pace(self) -> bool:
        r"""The resource governor's pause, for a long recording. True = stop.

        Called by `app.extract.media` between pictures and between spoken
        passages - the only places inside one file where a two-hour job can
        stop and resume - so a video is paced by the same battery, CPU and
        memory ceilings as everything else instead of holding a worker for an
        hour regardless. Never raises: pacing is a courtesy, not a gate.
        """
        try:
            verdict = self.governor.wait_while_throttled(should_stop=self._stop.is_set)
        except Exception:                        # noqa: BLE001
            return self._stop.is_set()
        return self._stop.is_set() or getattr(verdict, "action", "") == "stop"

    def _on_throttle(self, found: Verdict) -> None:
        """Remember the last throttle so progress can say why it went quiet.

        A background job that slows down without saying so is indistinguishable
        from one that has hung, and the person watching will kill it.
        """
        self._throttle = found if found.action != "run" else None
        self._say_pause(found)

    def _record(self, kind: str, text: str = "", **extra: Any) -> None:
        """One entry in this run's log (`IndexStats.activity`). Never raises.

        Through `_stats_ref`, the one object every thread of the run already
        shares; `getattr`, because several tests build a bare pipeline with
        only the attributes the method under test needs.
        """
        activity = getattr(getattr(self, "_stats_ref", None), "activity", None)
        if activity is not None:
            activity.record(kind, text, **extra)

    def _say_pause(self, found: Verdict) -> None:
        """Work order 0w §2a: a pause, its reason, and the end of it, in the log.

        **Once each, however many threads ask.** The governor reports a change
        of state from whichever thread noticed it, and the person's own pause
        is also said by `pause()` the moment the button is pressed - so the
        last thing said is remembered, and repeating it is a no-op. A stop
        from the governor (the disk floor) is a warning: it ends the run, and
        its reason is the fix. Never raises.
        """
        try:
            activity = self._stats_ref.activity
            action = getattr(found, "action", "")
            if action == "stop":
                activity.record(KIND_WARNING, getattr(found, "reason", "") or "")
                return
            cause = "manual" if getattr(found, "cause", "") == "manual" else "machine"
            with self._pause_said_lock:
                if action == "pause":
                    if self._pause_said == cause:
                        return
                    self._pause_said = cause
                    activity.record(KIND_PAUSE, getattr(found, "reason", "") or "",
                                    detail=cause)
                elif action == "run" and self._pause_said:
                    self._pause_said = ""
                    activity.record(KIND_RESUME)
        except Exception as exc:                 # noqa: BLE001 - a log line
            self._log.debug("could not note a pause in the run log: {}", exc)

    def request_stop(self) -> None:
        """Ask the run to finish the file in flight and return cleanly.

        Used by the UI's pause button and by the disk guard. Not a kill: the
        point of stopping cleanly is that the cursor and every completed file
        survive, so resuming costs nothing.
        """
        # Said once, by the run that owns the log: the media tail's pipeline
        # is stopped through this too, straight after the outer one.
        if not self._interrupted and getattr(self, "activity_into", None) is None:
            self._record(KIND_STOPPING)
        self._interrupted = True
        self._stop.set()
        # **A stop beats a pause, and must leave nothing holding.** Every
        # waiter already checks the stop flag each poll, so this is not what
        # releases them - it is what stops a paused run from being re-held on
        # the way out, and what makes "pause, then close" end the process as
        # fast as "close" does.
        self._release_pause()

    # -- the person's pause -------------------------------------------------

    def _person_paused(self) -> bool:
        """Is the person holding this run? **Never raises, never guesses.**

        `getattr`, because a governor can be a stand-in: several tests hand in
        an object with only the methods they need, and every one of those runs
        must behave exactly as it did - "not paused" - rather than failing on
        a thread nobody is watching.
        """
        return bool(getattr(self.governor, "manually_paused", False))

    def pause(self) -> None:
        r"""Hold the run where it is, without ending it.

        **Distinct from `request_stop`, which settles and ends**: nothing is
        finalised, no marker is written, no cursor is closed off, and the run
        is still the same run when it starts moving again. Distinct as well
        from the governor's own pause, which is the machine's decision and
        keeps working independently underneath this one - resume while the
        computer is still busy and the run keeps waiting, with the machine's
        reason shown, because that is the true one.

        Held at three places, all of them boundaries that already exist: the
        walker (through `governor.wait_while_throttled`, the one wait path),
        each extraction worker between items and between documents, and the
        consumer between documents - which is the only thread that commits
        anything, so a pause can no more land mid-write than a stop can.
        """
        hold = getattr(self.governor, "pause_manually", None)
        if hold is None:
            return
        hold()
        # Said now, not when a waiter next asks the governor: after the walk
        # has finished nothing asks it, and the pause would go unrecorded.
        self._say_pause(Verdict("pause", MANUAL_PAUSE_REASON, cause="manual"))
        self._log.info("paused at the person's request")

    def resume(self) -> None:
        """Let go of the pause. The machine's own ceilings still apply."""
        self._release_pause()
        self._say_pause(Verdict("run"))
        self._log.info("resumed at the person's request")

    def _release_pause(self) -> None:
        let_go = getattr(self.governor, "resume", None)
        if let_go is not None:
            let_go()

    @property
    def paused_by_person(self) -> bool:
        """Is the person (or the command line's pause file) holding this run?"""
        return self._person_paused()

    def _pause_file_set(self) -> bool:
        """Does `config.pause_file` exist right now? Cached; never raises."""
        target = self.config.pause_file
        if target is None:
            return False
        now = time.monotonic()
        if now - self._pause_file_checked >= PAUSE_FILE_POLL_S:
            self._pause_file_checked = now
            try:
                self._pause_file_seen = Path(target).exists()
            except OSError:                      # noqa: PERF203 - a drive that vanished
                self._pause_file_seen = False
        return self._pause_file_seen

    def _hold_if_paused(self) -> bool:
        """Block while the person holds the run. True if the caller must stop.

        **The stop flag is checked every poll, and nothing is held while
        waiting that a close would need.** A worker sitting here owns one
        candidate and no lock; the consumer sitting here owns no half-written
        batch, because it only ever arrives between documents. So closing the
        window over a paused run ends the run and the process on exactly the
        path an unpaused one takes.
        """
        if not self._person_paused():
            return self._stop.is_set()
        while self._person_paused():
            if self._stop.is_set():
                return True
            time.sleep(HOLD_POLL_S)
        return self._stop.is_set()

    def _report_pause(
        self, stats: IndexStats,
        on_progress: Optional[Callable[[IndexStats], None]],
    ) -> None:
        """Copy the pause state onto the stats and tell whoever is watching."""
        self._copy_pause_state(stats)
        if on_progress is None:
            return
        try:
            on_progress(stats)
        except Exception as exc:                 # noqa: BLE001 - as elsewhere
            self._log.warning("progress reporting failed: {}", exc)

    def _announce_phase(
        self, stats: IndexStats,
        on_progress: Optional[Callable[[IndexStats], None]], phase: str,
    ) -> None:
        """Say which stretch of the run is starting. See `PHASE_MODEL`.

        Guarded like every other report: telling somebody what is happening
        must never be the reason it stops happening.
        """
        stats.phase = phase
        stats.activity.record(KIND_PHASE, phase)
        self._log.debug("phase: {}", phase)
        if on_progress is None:
            return
        try:
            on_progress(stats)
        except Exception as exc:                 # noqa: BLE001 - as elsewhere
            self._log.warning("progress reporting failed: {}", exc)

    def _copy_pause_state(self, stats: IndexStats) -> None:
        """The governor's live pause state, onto the stats the UI reads."""
        held = self._person_paused()
        stats.paused_seconds = self.governor.paused_seconds
        stats.pauses = self.governor.pauses
        stats.paused = self.governor.paused or held
        stats.paused_by_person = held
        stats.manual_paused_seconds = float(
            getattr(self.governor, "manual_paused_seconds", 0.0) or 0.0)
        if held:
            stats.pause_reason = MANUAL_PAUSE_REASON
        else:
            stats.pause_reason = self.governor.pause_reason

    # -- the run ------------------------------------------------------------

    def _background(self, target: Callable[..., Any]) -> Callable[..., Any]:
        """`target`, run by a thread that first lowers its own priority.

        Only when `thread_priority_only` is set, which is the window's run; for
        the command line the process itself was lowered and this hands back
        `target` untouched.
        """
        if not self.thread_priority_only:
            return target

        def lowered(*args: Any, **kwargs: Any) -> Any:
            lower_this_thread()          # the thread ends with the run: no restore
            return target(*args, **kwargs)

        return lowered

    def _yield_to_ui(self) -> None:
        r"""Give the window a beat when it is running late.

        **A control loop, not a hope.** `ui_lag` reports how late the interface's
        own timer has been recently. Over `UI_LAG_YIELD_S` this sleeps for about
        that long before the next item, which lets the consumer's queue back up
        and, through it, the extraction threads stop competing for the GIL
        until the window has caught up. Free when nothing is wrong: one call and
        one comparison. Never raises, and never sleeps past `UI_YIELD_MAX_S`, so
        it cannot be the reason a run crawls.
        """
        probe = self.ui_lag
        if probe is None:
            return
        try:
            lag = float(probe())
        except Exception:                        # noqa: BLE001 - advisory
            return
        if lag >= UI_LAG_YIELD_S:
            # 0x 5d: never sleep holding the write lock - the window may be
            # late precisely because it is waiting to save something.
            self._commit_write_group()
            time.sleep(min(lag, UI_YIELD_MAX_S))

    def run(
        self,
        *,
        on_progress: Optional[Callable[[IndexStats], None]] = None,
    ) -> IndexStats:
        stats = IndexStats(ocr_mode=self.config.ocr_mode)
        if self.activity_into is not None:
            stats.activity = self.activity_into
        self._stats_ref = stats          # workers announce the file they are on
        self._warned_codes = set()
        self._pause_said = ""
        # Work order 202626130120 (0t) section 6. First thing, before a
        # single file is read or the embedder loads - "before the run
        # starts" means before either of those, not after them. Persisted on
        # `stats.notices` (not printed and forgotten) so every progress tick
        # and the finished panel keep showing it for the run's whole length,
        # through the exact mechanism `_report_root_problems` already uses.
        self._report_gpu_regression(stats)
        # A fresh clock per run: a Pipeline reused for a second run would
        # otherwise report the first one's stages added to the second's, and
        # the number nobody can act on is a total over two different corpora.
        self._clock = StageClock()
        # §6b: likewise a fresh handoff per run, for the same reason - a
        # second run must not inherit a queue or an error from the first.
        self._feeder_queue = queue.Queue(maxsize=_FEEDER_QUEUE_SIZE)
        self._feeder_errors = []
        # §6g: same reason - a second run starts back at the static count.
        self._dynamic_workers = []
        self._replacement_workers = []
        self._left_behind = set()
        self._embedding_now.clear()
        self._last_growth = 0.0
        # Work order 202626270509, item 1b: same reason - a fresh run rebuilds
        # its view of in-progress resume positions from `index_state` (via
        # `_extract_stream`'s lookup) rather than trusting a prior run's
        # object state.
        self._resume_progress = {}
        self._archive_cursors = {}
        self._archive_resumed = {}
        started = time.perf_counter()
        self._run_started = self._last_summary = time.monotonic()
        self._run_started_pc = time.perf_counter()
        # The first mid-run cursor write waits a full interval, like the rest.
        self._last_resume_persist = self._run_started
        self._run_started_wall = time.time()
        self._stop.clear()
        self._interrupted = False
        self._embed_abandoned = False
        # A pause belongs to the run it was asked for. A second run on the
        # same `Pipeline` starts moving, and a person who wants it held asks
        # again - a run that sat still for a reason nobody can see is the
        # failure this whole feature exists to avoid. The command line's
        # pause file is re-read either way: it is a fact about now.
        self._release_pause()
        # Work order 202626270515. **Before anything walks**: the walker asks
        # `media.disabled_extensions()` which extensions exist, and
        # `_narrow_to_images` below adds the enabled ones to the images pass.
        # Always called, even with no media settings, so one run's switches
        # can never leak into the next.
        from app.extract import media as _media
        _media.configure(
            self.config.media,
            pacer=self._media_pace, should_stop=self._stop.is_set)
        self._suspended_fts_triggers = []
        # Work order 0h: a second run must not inherit the first run's
        # unflushed CLIP vectors, same reasoning as `_feeder_queue` above.
        self._pending_images = []
        self._pending_frames = {}
        # Order 0z lane C: the held-pictures list is read afresh each run.
        self.__dict__.pop("_held_archive_book", None)
        # Order 0z lane D: so is the junk-image book.
        self.__dict__.pop("_image_book_store", None)
        # A new run reads the machine before its first file (`_governor_allows`).
        self.__dict__.pop("_next_governor_ask", None)
        # Work order 0h §2a: same reasoning, for pending pHashes.
        self._pending_phashes = {}

        # Below-normal CPU and background I/O priority, before a single file is
        # read. The cheapest courtesy available and the most effective: the
        # scheduler simply prefers whatever the person is actually doing.
        if self.thread_priority_only:
            # The window is in this process: lower this run's threads, not the
            # process. `IndexWorker` has already lowered the one calling here.
            if self.governor.apply_io_priority():
                self._log.debug("running at background I/O priority")
        elif self.governor.apply_priority():
            self._log.debug("running at below-normal priority")

        self._announce_phase(stats, on_progress, PHASE_MODEL)
        self.vectors.ensure_table()
        if self.image_vectors is not None:
            self.image_vectors.ensure_table()
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
        # Check if FTS was marked dirty by an interrupted bulk run and rebuild
        # if needed. This must happen before extraction starts.
        self._announce_phase(stats, on_progress, PHASE_WORD_INDEX_CHECK)
        self.store.check_and_rebuild_fts_if_dirty()
        # **Anything left without a vector by a previous run is filled first.**
        # See `_drain_unembedded`. Work order 0i section 2a: now one of the
        # registered enrichment-backlog kinds - see `_run_enrichment_drains`.
        self._announce_phase(stats, on_progress, PHASE_CATCH_UP)
        # 0z F1: not for a few files from the folder watch - see `light`.
        light = bool(self.config.light)
        if not light:
            self._run_enrichment_drains(stats)
        # Before anything else: an archival root that is being skipped must not
        # have its own stores protected, its repositories seeded or its rows
        # pruned, because none of those should look at it at all.
        self._announce_phase(stats, on_progress, PHASE_PLANNING)
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
            target=self._background(self._produce), args=(work, stats, seen_paths),
            name="walker", daemon=True
        )
        workers = [
            threading.Thread(target=self._background(self._extract_worker),
                             args=(work, results), name=f"extract-{i}", daemon=True)
            for i in range(self.config.worker_count())
        ]
        # §6g: `_produce` sends exactly this many `_STOP` markers; every
        # worker `_maybe_grow_workers` starts later raises this by one, each
        # backed by its own marker. See `_offer_stop_token`.
        self._expected_stops = len(workers)
        # §6b: started alongside extraction, not inside `_consume`, for the
        # same reason the extraction workers are started here rather than in
        # `_produce` - thread lifecycle belongs at the one place that tears
        # every thread down again, in `finally` below.
        feeder = threading.Thread(target=self._background(self._feed_worker),
                                  name="feeder", daemon=True)
        self._feeder_thread = feeder

        # 0z lane B: before the workers, so each can register its file watch.
        self._watchdog = Watchdog(
            file_limit_s=self.config.file_time_limit_s,
            stall_limit_s=self.config.stall_limit_s,
            on_orphan=lambda watch: self._replace_worker(watch, work, results))
        self._watchdog.start()
        # 2026-09-29. Said before the producer starts, so it can never land
        # after the producer's own switch to reading - see `_read_in_order`.
        ordered = normalise_order(self.config.read_order) == ORDER_NEWEST
        if ordered:
            self._announce_phase(stats, on_progress, PHASE_SCANNING)

        producer.start()
        for worker in workers:
            worker.start()
        feeder.start()

        # Back to counting: `progress_for` draws a real bar from here on.
        # In the "newest first" order the walker says so itself, the moment
        # the sorted list is ready (`_read_in_order`).
        if not ordered:
            self._announce_phase(stats, on_progress, PHASE_READING)
        # 0x 5d: a second run on the same `Pipeline` starts with no shared
        # transaction open - see `_begin_write_group`.
        self._write_group = None
        self._write_group_docs = 0
        try:
            self._consume(results, workers, stats, on_progress, work=work)
        finally:
            # 0x 5d. **First, before anything else in teardown.** `_consume`
            # commits its shared transaction on every way out it takes on
            # purpose (the final `_feed_sync` does it). If one is still open
            # here, `_consume` raised part-way through a document, and that
            # half-written document must not be committed - so the whole
            # group is rolled back. See `_abandon_write_group`.
            self._abandon_write_group()
            # 0x 5d: and give back the page cache `_consume` asked for.
            self._restore_write_cache()
            self._stop.set()                    # unblock producer and workers
            # 0z lane B: nothing is timed out or replaced once the run is ending.
            watchdog, self._watchdog = self._watchdog, None
            if watchdog is not None:
                watchdog.stop()
            _drain(work)
            _drain(results)
            producer.join(timeout=5)
            # 0z lane B: a thread left stuck in native code is not waited for -
            # five seconds each, for a thread known not to be coming back.
            left_behind = set(self._left_behind)
            for worker in workers:
                if worker.ident not in left_behind:
                    worker.join(timeout=5)
            # §6g: whatever `_maybe_grow_workers` started, this run also ends -
            # the static workers above are not the only ones reading `work`.
            for worker in self._dynamic_workers:
                if worker.ident not in left_behind:
                    worker.join(timeout=5)
            # 0z lane B: replacements for threads left stuck in a reader. The
            # stuck threads themselves are not joined: they are daemons, held
            # in native code, and waiting for them is what they were left for.
            for worker in self._replacement_workers:
                if worker.ident not in left_behind:
                    worker.join(timeout=5)
            # **After** the extraction threads, never before: `_consume`'s own
            # final flush (`_feed_sync`) already waited for every batch it
            # handed off, whether it returned normally, was stopped, or
            # raised - so by the time we get here the feeder is idle or has
            # already ended itself having recorded the error. This only has
            # to ask it to stop.
            self._stop_feeder(feeder)
            # 0x 3c: every reader has ended and closed its slot, so this
            # empties `workers` - a finished run must not go on showing the
            # last files it was reading, whoever reads `stats` next.
            stats.refresh_live()

        # 0w 3c: an archive this run stopped inside says so in the run's log.
        # The Indexing page's summary says it too, from the cursor, for as long
        # as it stays true - this is the moment it became true.
        if self._interrupted:
            for cursor in list(self._archive_cursors.values()):
                stats.activity.record(
                    KIND_ARCHIVE, Path(cursor["path"]).name, detail="part_read")

        # Work order 202626270515: videos and recordings the run only found are
        # read now, after everything else - see `app/index/media_backlog.py`.
        if not light:
            self._drain_media_backlog(stats, on_progress)

        # Guarded on `_interrupted`, never on the event: an interrupted walk
        # did not see the whole corpus, so "missing" would mean "not reached
        # yet" and pruning would delete perfectly good rows.
        # **Never after an images-only pass.** That walk saw only the pictures,
        # so "missing" would mean "not an image" for every document in the
        # corpus - and while `exists()` would save them, it would do so at the
        # cost of one syscall per row for nothing. Same reasoning as a run
        # restricted to one root, which has always been excluded.
        self._announce_phase(stats, on_progress, PHASE_TIDYING)
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
        # §6g: only worth a line when it actually happened - a run where the
        # ceiling was never set, or was never reached, says nothing extra.
        if self._dynamic_workers:
            stats.resolved["workers_grown_to"] = (
                self.config.worker_count() + len(self._dynamic_workers))
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
            stats.activity.record(KIND_WARNING, (
                f"{sum(unreachable.values()):,} file(s) could not be looked at "
                "at all, so they are not in the index."), detail="unreachable")
            self._log.warning(
                "{} file(s) could not be read at all and have no row in the "
                "index: {}. A count over 260 characters means Windows long-path "
                "support is off; `app.cli doctor` reports the setting.",
                sum(unreachable.values()),
                ", ".join(f"{why} ({count:,})"
                          for why, count in sorted(unreachable.items(),
                                                   key=lambda kv: -kv[1])),
            )
        # **The other way a file goes missing with no row at all.** See
        # `WalkConfig.oversize_dropped`: `.pst`/`.ost` are exempt from the
        # size ceiling now, so this fires only for some other oversized type
        # that still hits it - but when it does, this is the only place that
        # says so.
        oversize_dropped = dict(
            getattr(self.config.walk, "oversize_dropped", {}) or {})
        if oversize_dropped:
            stats.oversize_dropped = oversize_dropped
            stats.activity.record(KIND_WARNING, (
                f"{sum(oversize_dropped.values()):,} file(s) were too large to "
                "open, so they are not in the index."), detail="oversize")
            self._log.warning(
                "{} file(s) were over the {:,} byte size ceiling and have no "
                "row in the index: {}. Formats read incrementally (like "
                "`.pst`/`.ost`) are exempt from this ceiling; anything else "
                "this large is dropped rather than opened.",
                sum(oversize_dropped.values()),
                self.config.walk.max_file_bytes,
                ", ".join(f"{ext} ({count:,})"
                          for ext, count in sorted(oversize_dropped.items(),
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
        if not light:
            # 0z F1: a few files from the folder watch are not "the last run"
            # - that record is what the Indexing page and the tuning footer
            # show, and a one-file update must not replace a night's numbers.
            self._say_if_nothing_was_walked(stats)
            self.store.set_state("last_run", str(int(time.time())))
            self.store.set_state("last_run_stats", repr(stats.as_dict()))
        # Order 0z lane C: archives whose attached pictures wait for the
        # pictures pass. Written here, on the run's own thread, never by a worker.
        self._held_archives().save()
        # Order 0z lane D: what this run learnt about pictures in mail.
        self._image_book().save()
        self._announce_phase(stats, on_progress, PHASE_VECTOR_INDEX)
        self.vectors.maybe_create_index()
        # **Always at the end of a run**, whatever the row threshold says. A run
        # that added 4,000 chunks would otherwise never compact at all, and a
        # nightly incremental index is exactly that shape - a small run, every
        # day, each one leaving fragments behind forever.
        # 0z F1: **except after a few files from the folder watch**, which may
        # come every few seconds - rewriting the table each time would cost
        # far more than the file did. Those leave the table to its own
        # threshold (`COMPACT_EVERY_ROWS`) and to the next ordinary run.
        self.vectors.maybe_compact(force=not light)
        # Work order 0h §1b, M8 pattern: the same end-of-run-only discipline,
        # applied to the image table. Nothing above this line ever calls
        # `maybe_create_index` on it either - see `_flush_pending_images`.
        if self.image_vectors is not None:
            self.image_vectors.maybe_create_index()
            self.image_vectors.maybe_compact(force=not light)
        self._announce_phase(stats, on_progress, PHASE_WORD_INDEX)
        self._optimise_keyword_index(stats)
        if not light:
            # 0z F1: the completions file is rebuilt from the whole index.
            self._write_completions()
        from app.extract.legacy_office import take_fallback_summary

        slower = take_fallback_summary()
        if slower:
            self._log.info("{}", slower)
        self._log.info("index run: {}", stats.as_dict())
        # The last line of the run's story. Not for a run reading into another
        # run's log (the media tail): that run is not finished when this is.
        if self.activity_into is None:
            stats.stamp_notices()
            stats.activity.record(
                KIND_FINISHED,
                "stopped" if (self._interrupted or stats.stopped_early) else "")
        return stats

    def _report_gpu_regression(self, stats: IndexStats) -> None:
        r"""Work order 202626130120 (0t) section 6, the one case that fires.

        `self.config.gpu_regression_notice` is computed once, off the UI
        thread, by `resolve.resolve_for_run` before this Pipeline was even
        built - see that module for the three-way had/has/probe-failed
        decision. This method only has to carry the answer onto the run
        somebody is actually watching, exactly like `_report_root_problems`
        beside it. Never raises: a notice is not worth a run.
        """
        try:
            notice = getattr(self.config, "gpu_regression_notice", "") or ""
            if not notice:
                return
            stats.add_notice(notice)
            self._log.warning("{}", notice)
        except Exception as exc:                    # noqa: BLE001 - a notice
            self._log.debug("could not report the lost graphics-card provider: {}", exc)

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
            stats.add_notice(notice)
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

            stats.add_notice(notice)
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

        §6f: if bulk mode is enabled, restores FTS triggers after a bulk insert.
        """
        wanted = str(self.config.bulk_fts or "auto").lower()
        # §6f. `on` merges whatever the run wrote; `auto` merges only when the
        # run was big enough for the merge to earn its minutes; `off` leaves the
        # segments alone.
        if wanted == "off":
            self._log.debug("the word index was left unmerged, as asked")
            return
        if wanted != "on" and stats.chunks < FTS_OPTIMIZE_AFTER_CHUNKS:
            return

        # Restore FTS triggers if they were suspended during bulk insert
        if self._suspended_fts_triggers:
            started = time.perf_counter()
            if self.store.restore_fts_triggers(self._suspended_fts_triggers):
                self._log.info(
                    "restored FTS content triggers after bulk insert ({:.1f}s)",
                    time.perf_counter() - started)
                # Clear the dirty flag now that triggers are restored
                self.store.set_state("fts_dirty", "")

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

        **Two orders** (`PipelineConfig.read_order`, see `read_order.py`).
        `found` streams each file into the queue as the walk finds it. `newest`
        walks to the end first, keeping only what needs reading, then sorts
        that list and queues it in order - `_read_in_order`.
        """
        from app.index.archives import files_under

        sequence = 0
        # Snapshotted: `walk.roots` is not written during a run, and asking for
        # it per file would be a list build a million times over.
        roots = list(self.config.walk.roots)
        ordered = normalise_order(self.config.read_order) == ORDER_NEWEST
        worklist = WorkList(self._spill_dir()) if ordered else None
        halted = False
        next_ask = 0.0
        try:
            for candidate in self._candidates():
                if self._stop.is_set():
                    halted = True
                    break
                # `_candidates` and the walker have already recorded this path
                # in the same set - see M17. Kept as a no-op `add` rather than
                # removed, because `_produce` is also called with a private set
                # by tests, and a set that is only *sometimes* filled is the
                # kind of thing that makes a prune pass delete a live file.
                seen.add(path_key(candidate.path))
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
                #
                # While scanning ("newest"), at most every `SCAN_GOVERNOR_S`:
                # nothing is in flight yet, and the scan is on the critical
                # path. The streaming order asks per file, as it always did.
                if worklist is None or time.monotonic() >= next_ask:
                    if not self._governor_allows(stats):
                        halted = True
                        break
                    next_ask = time.monotonic() + SCAN_GOVERNOR_S

                # **No hash during a scan.** A file never seen is hashed by the
                # change check, and a hash is a full read: the scan would read
                # the whole corpus before the first file could be searched. The
                # hash is taken when the file's turn comes - `_read_in_order`.
                decision = self._classify(candidate, hash_now=not ordered)
                if decision is UNCHANGED:
                    stats.unchanged += 1
                    continue

                if worklist is not None:
                    worklist.add(candidate, decision)
                    continue

                # (priority, sequence) keeps PriorityQueue from ever comparing
                # Candidates, which are not orderable, while preserving the
                # walker's deterministic order within a priority band.
                sequence += 1
                self._queue_work(work, (candidate.priority, sequence, candidate, decision))
            if worklist is not None and not halted and not self._stop.is_set():
                sequence = self._read_in_order(work, stats, worklist)
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
            if worklist is not None:
                worklist.close()
            # **`seen` only becomes a real total here.** Until the walk ends it
            # is "what has been found so far", and because the work queue is
            # bounded the walker can never run more than a queue-length ahead of
            # the workers - so `done / seen` sits near 1 from the first minute
            # whatever fraction of the corpus is left. See `progress_for`.
            # (In the "newest" order `_read_in_order` has already said so, at
            # the end of the scan, which is when it became true.)
            if not stats.walk_complete:
                stats.walk_complete = True
                # §6a. **Recorded, but not on the critical path**, so it is
                # added to the worker tally rather than the stage one: the walk
                # runs on its own thread alongside everything else, and
                # counting its seconds as a share of the run would push the
                # total past 100%.
                #
                # Worth having all the same - a walk that takes two hours over
                # a network share is a fact about the corpus that no other
                # number in the report shows.
                self._clock.add_worker(
                    "walk", time.perf_counter() - self._run_started_pc)
            for _ in range(self.config.worker_count()):
                work.put((10_000, sequence + 1, _STOP, None))

    def _read_in_order(self, work: queue.PriorityQueue, stats: IndexStats,
                       worklist: WorkList) -> int:
        """The scan is over: sort what it found and queue it. Returns the last
        sequence number used, for the stop markers after it.

        **Each file's hash is taken here, on its turn** - see `_produce` - by
        asking `_classify` again with hashing allowed. For a file never seen
        that is the hash the old single pass took; for one whose date moved it
        is the check that finds a `robocopy` restore unchanged, which is then
        counted as unchanged and not read. `verify_hash` off means the scan's
        answer was already final.
        """
        stats.walk_complete = True
        self._clock.add_worker("walk", time.perf_counter() - self._run_started_pc)
        sorting_from = time.perf_counter()
        entries = worklist.sorted()
        first = next(entries, None)             # the sort itself happens here
        self._clock.add_worker("sort", time.perf_counter() - sorting_from)
        self._log.info(
            "scan finished: {:,} file(s) found, {:,} to read, newest first{}",
            stats.seen, len(worklist), " (sorted on disk)" if worklist.spilled else "")
        stats.phase = PHASE_READING
        stats.activity.record(KIND_PHASE, PHASE_READING)
        sequence = 0
        if first is None:
            return sequence
        for candidate, decision in itertools.chain((first,), entries):
            if self._stop.is_set():
                break
            if not self._governor_allows(stats):
                break
            if self.config.verify_hash:
                decision = self._classify(candidate)
                if decision is UNCHANGED:
                    stats.unchanged += 1
                    continue
            sequence += 1
            if not self._queue_work(
                    work, (candidate.priority, sequence, candidate, decision)):
                break
        return sequence

    def _governor_allows(self, stats: IndexStats) -> bool:
        """Wait out a pause; False when the run must stop, having said why.

        **The machine is read at most once every `SCAN_GOVERNOR_S`, not once a
        file** (2026-09-30, order 0z E4 measured on Windows). One ask reads the
        process table, and on the owner's laptop that is 26-30ms (531
        processes) against 1.76ms in the Linux sandbox the per-file ask was
        judged in. Asked for every file, it held the whole run to about 35
        files a second whatever the number of readers, and made a rerun with
        nothing changed take 207s for 9,002 files in the "as found" order.

        The person's own pause is not a measurement and is still noticed on
        every file: `_person_paused` reads a flag, never the machine.
        """
        now = time.monotonic()
        if (now < self.__dict__.get("_next_governor_ask", 0.0)
                and not self._person_paused()):
            return True
        verdict = self.governor.wait_while_throttled(should_stop=self._stop.is_set)
        # Counted from the end of the wait: a pause that has just been waited
        # out is not followed at once by another look.
        self.__dict__["_next_governor_ask"] = time.monotonic() + SCAN_GOVERNOR_S
        self._copy_pause_state(stats)
        if verdict.action != "stop":
            return True
        # Say why. Breaking silently here would end the run reporting complete
        # success having indexed nothing - the exact failure shape that hid
        # every PST for two days.
        if not self._stop.is_set() and stats.stopped_early is None:
            stats.stopped_early = make_error(
                "ERR_DISK_SPACE", "index.pipeline",
                free_gb="low", drive=str(self.vectors.uri),
                details=verdict.reason,
            )
            self._log.error("{}", stats.stopped_early.render())
            self.request_stop()
        return False

    def _queue_work(self, work: queue.PriorityQueue, entry: tuple) -> bool:
        """Put one entry on the bounded queue, waiting for room. False if the
        run was stopped while waiting."""
        while not self._stop.is_set():
            try:
                work.put(entry, timeout=0.25)
                return True
            except queue.Full:
                continue                # bounded on purpose: this is backpressure
        return False

    def _spill_dir(self) -> Optional[Path]:
        """Where a large work list goes: beside the index, on the drive that
        already has room for it. None (the system's temporary folder) for a
        store with no file."""
        database = getattr(self.store, "db_path", None)
        return Path(database).parent if database else None

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
        stats.add_notice(notice)
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
    #:
    #: `ERR_CLOUD_ONLY` (202626270514 3b): a placeholder that is still a
    #: placeholder never reaches this check at all - the earlier
    #: `not candidate.readable` branch settles it on mtime/size alone, with
    #: no read. Reaching here with this skip code already on the row means
    #: the walk just classified the *same* candidate as `readable=True` -
    #: the file has hydrated, mtime and size held steady through the whole
    #: transition, and the only thing that changed is not on disk at all.
    #: Settling it here would leave a hydrated file claiming its content is
    #: still online-only, forever - the exact bug this constant was built to
    #: catch, reappearing for a skip code nobody had yet.
    #:
    #: `ERR_MEDIA_HELD` is the video and audio twin of `ERR_OCR_HELD` (held for
    #: the images pass), and `ERR_MEDIA_INTERRUPTED` is a recording whose
    #: transcription was stopped part-way: neither has settled anything.
    DEFERRED_SKIP_CODES = frozenset({
        "ERR_OCR_HELD", "ERR_FILE_LOCKED", "ERR_CLOUD_ONLY",
        "ERR_MEDIA_HELD", "ERR_MEDIA_INTERRUPTED", "ERR_MEDIA_BACKLOG",
    })

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
        # Work order 202626270515: a normal run finds videos and recordings and
        # reads them at the tail, as the `media_transcript` backlog kind
        # (`app/index/media_backlog.py`) - never inline in front of the documents.
        from app.index import media_backlog

        if media_backlog.defers(self.config, candidate.path):
            return make_error(
                "ERR_MEDIA_BACKLOG", "index.pipeline", path=str(candidate.path))

        if self.config.ocr_mode not in ("text", "images"):
            return None

        from app.extract.base import reads_by_ocr

        is_image = reads_by_ocr(candidate.path)
        if self.config.ocr_mode == "text" and is_image:
            return make_error(
                "ERR_OCR_HELD", "index.pipeline", path=str(candidate.path))
        # Work order 202626270515. **A video is the most expensive thing in the
        # corpus, so a text-only pass holds it exactly as it holds a picture**:
        # search is useful in hours rather than days, and the film waits for
        # the images pass. Only reached for extensions whose switch is on - the
        # walker does not offer the rest.
        if (self.config.ocr_mode == "text"
                and candidate.path.suffix.lower() in _media_extensions()):
            return make_error(
                "ERR_MEDIA_HELD", "index.pipeline", path=str(candidate.path))
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
        from app.extract import media as _media
        from app.extract.ocr import OcrExtractor

        # Pictures, and the video and audio whose switch is on: a text pass
        # holds those back (`ERR_MEDIA_HELD`), so this is where they are read.
        wanted = (frozenset(OcrExtractor.extensions)
                  | (_media.media_extensions() - _media.disabled_extensions()))
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

        # Letter case ignored on every system, as before (order 0x 7b, kept):
        # this decides only which repository a file is *attributed* to, never
        # whether it is indexed, so a case-sensitive disk cannot lose or
        # duplicate a file here.
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
        # Work order 0z F1: a run given its files (the folder watch) yields
        # exactly those. No walk, and none of the re-queues below - they read
        # the whole skip ledger, and belong to a run over the whole corpus.
        source = self.config.candidate_source
        if source is not None:
            yield from source(self.config.walk, self._seen_paths)
            return
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
        # Order 0z lane C: mail archives whose attached pictures the text pass
        # held. The narrowed walk never reaches a `.pst`, so the list does.
        if self.config.ocr_mode == "images":
            scanned.extend(self._held_archive_candidates())
        # Work order 0i section 2a: visibility into a requeue mechanism that
        # already existed before this item - additive counting only, no
        # change to what gets requeued or when. See the note on
        # `KIND_OCR_PENDING` for why this stays a count rather than being
        # restructured into a `_drain_*`-shaped function.
        self._stats_ref.enrichment_counts[self.KIND_OCR_PENDING] = len(scanned)

        # **One set, shared with the walker and with `_produce`.** This used to
        # keep its own `walked`, the walker kept its own `seen`, and `_produce`
        # kept a third for the prune pass - three copies of every lowercased
        # path in the corpus, about a gigabyte each at five million files, all
        # answering the same question. `walk()` adds to it as it yields; the
        # prune pass reads it at the end.
        seen = self._seen_paths

        yield from walk(self.config.walk, seen)

        for candidate in (*retry, *scanned):
            # `path_key`, the walker's own key (order 0x 7b) - see
            # `app/core/osbridge/pathnames.py`. `str(path).lower()` on Windows.
            key = path_key(candidate.path)
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

    def _held_archive_candidates(self) -> Iterator[Candidate]:
        """Archives with attachment pictures held for this pass (`held_archives`).

        `retry=True` because the archive's row is INDEXED and unchanged - the
        text pass read it - and `_classify` would otherwise send it home. A
        file since deleted or moved is skipped; the prune deals with its rows.
        """
        try:
            paths = self._held_archives().paths()
        except Exception as exc:                        # noqa: BLE001 - nothing queued
            self._log.debug("held-archive list unreadable: {}", exc)
            return
        for path in paths:
            try:
                stat = path.stat()
            except OSError:
                continue
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

    def _classify(self, candidate: Candidate, *, hash_now: bool = True) -> Optional[str]:
        """`UNCHANGED` to skip the file; otherwise its content hash, or None.

        `hash_now=False` answers from the row and `stat()` alone and never
        reads the file: the scan of the "newest" order (`_produce`), which asks
        again with hashing allowed when the file's turn comes. Everything it
        calls unchanged is unchanged on either answer; what it cannot settle
        without a hash it passes on.

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
            # **Volume-aware, same as `row_key`/`_candidate_row_key`.** A
            # volume-backed row is stored under its synthetic
            # `leasha-volume://...` key (1c), never `str(candidate.path)` -
            # looking it up by the real path here always missed, so every
            # file on a catalogued volume looked new on every single rescan
            # and was re-extracted every time. Found by
            # `test_a_moved_file_is_repaired_without_re_extraction`, whose
            # second pipeline run kept reporting `indexed=1` instead of the
            # `unchanged=1` a settled file should produce.
            record = self.store.get_file(_candidate_row_key(candidate))
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
                    and record.status == FileStatus.SKIPPED
                    and record.skip_code == "ERR_CLOUD_ONLY"
                    and not getattr(candidate, "is_cloud_placeholder", False)):
                # 2026-09-30: a row that says "stored online only" for a file
                # the walk no longer calls a placeholder is rewritten, not kept.
                # The rule below would settle it forever - which is how two
                # local `.exe` files, misread as cloud files (see cloudfs),
                # would have stayed skipped after the fix.
                return None
            if (record is not None
                    and record.status in (FileStatus.NAME_ONLY, FileStatus.SKIPPED)
                    and record.mtime_ns == candidate.mtime_ns
                    and record.size_bytes == candidate.size_bytes):
                # **`SKIPPED` joins `NAME_ONLY` here for 202626270514 3a.** A
                # cloud placeholder now settles as `ERR_CLOUD_ONLY` (see
                # `_extract_worker`), and without this it would fail the
                # exact H1 bug class the order register already fixed once:
                # re-queued, re-classified as a fresh skip and rewritten,
                # every incremental pass, forever - for every placeholder in
                # a synced library, not just the ones that changed.
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
                verify_hash=(self.config.verify_hash and hash_now
                             and not reads_externally(candidate.path)),
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
        # **Except a file deliberately put back** (order 0z lane C): the
        # pictures pass re-queues archives the text pass read in full but
        # whose attached pictures it held (`_held_archive_candidates`). Every
        # other re-queue reads SKIPPED rows only, so for them this changes
        # nothing.
        if (not changed and record is not None and record.status == FileStatus.INDEXED
                and not getattr(candidate, "retry", False)):
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
        """Run one extraction worker, and never let it end without saying so.

        `_consume` finishes only when it has seen one `_STOP` per worker, so a
        worker that ended abnormally - an exception outside the per-file guard,
        or a `BaseException` - left it polling `results` for ever with the run
        looking alive. A clean return needs no marker (the stop flag ends the
        consumer's loop); an abnormal one sends its own, and the reason is logged.
        """
        clean = False
        board = slot = watchdog = None
        try:
            # 0x 3c: this thread's own line on the page, and the list its
            # readers write their position into (`app.extract.progress.attach`).
            # Opened once per thread; see `live_progress.WorkerBoard`.
            #
            # **Inside the `try`, and asked for with `getattr`.** 2026-09-27:
            # this used to sit above the `try` and read `self._stats_ref`
            # directly. A pipeline whose `run()` had not set it (the bare one
            # `test_close_waits_for_index_run` builds without `__init__`) then
            # raised `AttributeError` here, before the `finally` below existed
            # for it - so the thread died without the `_STOP` marker this
            # method promises, and the test passed only because a dead thread
            # is also "not alive". Now a missing board means no line on the
            # page, exactly as a board of `None` always did.
            board = getattr(getattr(self, "_stats_ref", None), "board", None)
            slot = board.open_slot() if board is not None else None
            if slot is not None:
                reader_progress.attach(slot.frames)
            self._worker_slots().slot = slot
            # 0x 5b: this thread's reader process, started now so its start-up
            # overlaps the walk rather than delaying the first file.
            if getattr(getattr(self, "config", None), "read_processes", False):
                reader = ReaderProcess(
                    low_priority=bool(self.config.resolved_limits().low_priority))
                reader.start()
                self._worker_slots().reader = reader
            # 0z lane B: this thread's file watch - the time limits and Force
            # skip. Asked for with `getattr` like the board above: a pipeline a
            # test built without `run()` has no watchdog, and reads unwatched.
            watchdog = getattr(self, "_watchdog", None)
            if watchdog is not None:
                watch = FileWatch(slot=slot,
                                  reader=getattr(self._worker_slots(), "reader", None))
                self._worker_slots().watch = watch
                watchdog.add(watch)
            self._extract_worker_loop(work, results)
            clean = True
        except BaseException as exc:                    # noqa: BLE001 - reported, then re-raised
            log = getattr(self, "_log", None) or logger.bind(component="index.pipeline")
            log.error("an extraction worker ended unexpectedly: {}: {}",
                      type(exc).__name__, exc)
            raise
        finally:
            watch = getattr(self._worker_slots(), "watch", None)
            if watch is not None:
                self._worker_slots().watch = None
                if watchdog is not None:
                    watchdog.remove(watch)
            if slot is not None and board is not None:
                board.close_slot(slot)
                reader_progress.detach()
            self._worker_slots().slot = None
            reader = getattr(self._worker_slots(), "reader", None)
            if reader is not None:
                self._worker_slots().reader = None
                reader.close()
            if not clean:
                try:
                    results.put(_STOP, timeout=5)
                except Exception:                       # noqa: BLE001 - nobody left to tell
                    pass

    def _worker_slots(self) -> threading.local:
        """Each extraction thread's `WorkerSlot`, found without passing it about.

        Made on first use rather than in `__init__`, so a pipeline built by a
        test without running `__init__` (several are) still has one.
        """
        slots = self.__dict__.get("_worker_slot_local")
        if slots is None:
            slots = self.__dict__.setdefault("_worker_slot_local", threading.local())
        return slots

    def _extract_worker_loop(self, work: queue.PriorityQueue, results: queue.Queue) -> None:
        slot = getattr(self._worker_slots(), "slot", None)
        #: 0z lane B: None when the run has no watchdog (a bare test pipeline).
        watch = getattr(self._worker_slots(), "watch", None)
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
            # **Checked on every item, not only when the queue is empty.** The
            # queue is bounded and the walker keeps it full, so `Empty` above
            # never fires while there is work - and a stopped run kept
            # converting whatever was already queued, up to a queue-length of
            # files, for minutes after the window had closed. `_drain` empties
            # the queue too, but only once `_consume` returns, and `_consume`
            # can be inside an embedding batch for as long as that takes.
            if self._stop.is_set():
                work.task_done()
                return
            # **Noticed on every item, for the same reason the stop flag is.**
            # The queue is bounded and the walker keeps it full, so a worker
            # that only looked when the queue ran dry would never look at all -
            # and a paused run would keep reading a queue-length of files.
            if self._hold_if_paused():
                work.task_done()
                return
            if not getattr(candidate, "readable", True):
                # **Nothing is opened.** The row is its name, path, size and
                # date - one INSERT on top of a `stat` the walk already did.
                #
                # **202626270514 3a: a cloud placeholder is not the same kind
                # of "cannot read" as a `.zip` or a `.exe`.** Those have no
                # reader and no fix - `name_only=True` is the honest answer
                # forever. A placeholder has a perfectly good reader; its
                # content simply is not local right now, which is exactly
                # what `ERR_CLOUD_ONLY` (`SKIP_CONTINUE`) exists to say -
                # "3,412 files skipped: stored online only", with the fix
                # ("Always keep on this device", or the opt-in) named, per
                # non-negotiable 2. Checked from `candidate.attributes`
                # alone, so this still opens nothing.
                if getattr(candidate, "is_cloud_placeholder", False):
                    self._offer(results, _Extracted(
                        candidate=candidate, content_hash=None,
                        error=make_error(
                            "ERR_CLOUD_ONLY", "index.pipeline",
                            path=str(candidate.path),
                        ),
                    ))
                else:
                    self._offer(results, _Extracted(
                        candidate=candidate, content_hash=None, name_only=True))
                work.task_done()
                continue
            self._stats_ref.current = candidate.path.name
            self._stats_ref.current_since = time.monotonic()
            self._stats_ref.current_item = 0
            if slot is not None:
                slot.begin(candidate.path)       # 0x 3c: once per file
            if watch is not None:
                watch.begin(candidate, digest, limit_kind(candidate.path),
                            self._time_limit_factor(candidate))
            stream = None
            #: 0z lane B: set when the watchdog gave up on this thread and
            #: started another in its place - this one leaves after this file.
            replaced = False
            try:
                # 2026-09-30: before the first `watch.enter()`, so a reader
                # process's start-up is never this file's reader time.
                self._reader_ready(candidate)
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
                        # 0z lane B: the reader's time, bracketed for the
                        # watchdog. `enter` refuses a file already cancelled;
                        # `leave` raises `FileCancelled` if it was cancelled
                        # while the reader worked.
                        if watch is not None:
                            watch.enter()
                        try:
                            item = next(stream)
                        except StopIteration:
                            if watch is not None:
                                watch.leave(finished=True)
                            break
                        if watch is not None:
                            try:
                                watch.leave()
                            except FileCancelled:
                                # 2026-09-30: the reader handed this message
                                # over in the instant its archive was cut off.
                                # It was read; it is kept, like the rest.
                                if _kept_on_cut_off(watch, item):
                                    self._offer(results, item)
                                raise
                    finally:
                        self._clock.add_worker(
                            "extract", time.perf_counter() - started)
                    if self._stop.is_set():
                        break
                    # Between documents, so one 100MB archive does not ignore
                    # a pause for as long as it takes to read.
                    if self._hold_if_paused():
                        break
                    self._offer(results, item)
            except BaseException as exc:        # noqa: BLE001 - never let a worker die silently
                # `BaseException`, not `Exception`: one file that raises
                # `SystemExit` or the like must cost that file, not the worker.
                #
                # 0z lane B: first ask the watch whether this was a time limit
                # or a Force skip, which is recorded as `ERR_FILE_TIMEOUT`
                # instead of whatever the reader raised on the way out (a
                # reader process that was ended says "ended unexpectedly").
                # Retried once: an exception raised into this thread by the
                # watchdog can arrive at the first line of `settle` itself,
                # which then clears anything still pending.
                verdict = None
                if watch is not None:
                    try:
                        verdict = watch.settle()
                    except FileTimedOut:
                        verdict = watch.settle()
                if verdict is ORPHANED:
                    # Recorded by the watchdog, and another thread has taken
                    # this one's place: nothing to offer, and no more work.
                    replaced = True
                elif verdict is not None:
                    self._record(KIND_WARNING, verdict.message, detail="timed_out")
                    # Before the error, as every message before it was: what
                    # the reader had read and not yet handed on.
                    for kept in self._kept_in_hand(watch):
                        self._offer(results, kept)
                    self._offer(results, _Extracted(
                        candidate=candidate, content_hash=digest, error=verdict))
                else:
                    if isinstance(exc, (FileCancelled, FileTimedOut)):
                        # Cancelled with no verdict cannot happen; recorded as
                        # a plain failure rather than lost if it ever does.
                        exc = RuntimeError("the read was cancelled")
                    self._offer(results, _Extracted(
                        candidate=candidate, content_hash=digest,
                        error=to_app_error(exc, "index.pipeline",
                                           path=str(candidate.path)),
                    ))
            finally:
                # Closed here rather than left for the garbage collector: a
                # file abandoned part-way (Stop, Pause) must let go of what it
                # was reading - with 0x 5b, a reader process part-way through a
                # mailbox - before this thread takes its next file.
                if stream is not None:
                    try:
                        stream.close()
                    except Exception:           # noqa: BLE001 - already reported above
                        pass
                if watch is not None:
                    watch.end()
                self._stats_ref.current = ""
                self._stats_ref.current_item = 0
                if slot is not None:
                    slot.end()
                work.task_done()
            if replaced:
                self._log.info(
                    "a reader that was replaced after being stuck on {} has "
                    "come back; it ends here", candidate.path.name)
                return

    def _maybe_grow_workers(
        self, work: "queue.PriorityQueue[Any]", results: queue.Queue,
        workers: list[threading.Thread],
    ) -> None:
        r"""§6g: one more extraction worker, when idle capacity allows it.

        **The static count stays the floor of a range, never the answer
        raised past.** `worker_ceiling` is `0` for every caller that has not
        set it - `resolve.py`'s job, not this method's - so this is a no-op
        by default and changes nothing for an existing caller that never
        asked for it.

        Three conditions, all of them cheap: the run is actually
        extraction-bound (`waiting` dominant - the same number
        `stages.advice()` already uses for the same conclusion), there is
        room under the ceiling, and the model is not genuinely mid-batch on
        a processor right now (it would compete for the same cores; on a
        graphics card it would not, so that half does not apply there).
        A new thread just joins the same `work`/`results` queues everything
        else already uses, and is torn down in `run()`'s `finally` exactly
        like the static ones - the existing pause/backoff at the producer
        (`governor.wait_while_throttled`) already applies to however many
        extraction workers exist, so nothing new is needed for that half.

        **`_consume`'s own `_STOP` accounting has to grow with it, or a file
        goes missing.** `_produce` enqueues exactly one `_STOP` per *static*
        worker, and `_consume` used to stop consuming once it had seen that
        many. A dynamically-started worker never has a `_STOP` addressed to
        it, and if the static one happens to reach `results` first,
        `_consume` would return while the new worker was still mid-file -
        losing whatever it was about to hand over, silently. So starting a
        worker here always enqueues one more `_STOP` first, and only
        counts as growth (`_expected_stops` rises, the thread starts) once
        that succeeds - the two must not come apart under backpressure.
        """
        if self.config.worker_ceiling <= 0:
            return
        now = time.monotonic()
        if now - self._last_growth < _GROWTH_COOLDOWN_S:
            return
        total = len(workers) + len(self._dynamic_workers)
        if total >= self.config.worker_ceiling:
            return
        if self._clock.share(WAITING) < _GROWTH_WAITING_SHARE:
            return
        choice = getattr(self.embedder, "choice", None)
        is_gpu = bool(choice is not None
                      and getattr(choice, "device", "") == backends.GPU)
        if self._embedding_now.is_set() and not is_gpu:
            return

        if not self._offer_stop_token(work):
            # The work queue would not take one more entry in time - the
            # walker is well ahead of extraction, which is exactly the state
            # that argued for growth, so this is not surprising. Skipped
            # rather than forced: starting the thread without its matching
            # `_STOP` is the exact bug this method exists to avoid, so no
            # token means no worker, this round.
            self._log.debug(
                "held off adding an extraction worker - the work queue "
                "would not take its stop marker in time")
            return

        self._last_growth = now
        self._expected_stops += 1
        worker = threading.Thread(
            target=self._background(self._extract_worker), args=(work, results),
            name=f"extract-dynamic-{len(self._dynamic_workers) + 1}", daemon=True)
        self._dynamic_workers.append(worker)
        worker.start()
        self._log.info(
            "raised extraction workers to {} (ceiling {}) - most of the run "
            "has been spent waiting for files to be read",
            total + 1, self.config.worker_ceiling)

    def _replace_worker(self, watch: FileWatch, work: "queue.PriorityQueue[Any]",
                        results: queue.Queue) -> None:
        r"""0z lane B: a thread stuck in a reader is left behind and replaced.

        Called by the watchdog (`file_watch.Watchdog`, on its own thread) when
        a thread told to let go of a timed-out or force-skipped file did not
        within `file_watch.GRACE_S` - it is inside native code, where nothing
        in Python can reach it. The file is recorded here, from the watch, and
        a new thread takes the stuck one's place, so the run keeps its number
        of readers.

        **No new `_STOP` marker.** The stuck thread will never take the one
        `_produce` queued for it (if it ever comes back, it ends without
        taking work - see `_extract_worker_loop`), so its replacement takes
        that one, and `_consume`'s count of expected markers is unchanged.
        """
        candidate, error = watch.candidate, watch.cancel
        self._left_behind.add(watch.thread_id)
        if candidate is not None and error is not None:
            self._record(KIND_WARNING, error.message, detail="timed_out")
            # 2026-09-30: the stuck thread cannot hand over what its reader
            # had read and was holding back, so this thread does. The stuck
            # thread is not running Python, so nothing else touches these.
            for kept in self._kept_in_hand(watch):
                self._offer(results, kept)
            self._offer(results, _Extracted(
                candidate=candidate, content_hash=watch.digest, error=error))
        board = getattr(getattr(self, "_stats_ref", None), "board", None)
        if board is not None and watch.slot is not None:
            board.close_slot(watch.slot)
        self._log.warning(
            "a reader did not let go of {} within {:.0f}s of being told to (it is "
            "inside native code, which cannot be interrupted); it is left behind "
            "and another reader takes its place",
            getattr(getattr(candidate, "path", None), "name", candidate),
            GRACE_S)
        if self._stop.is_set():
            return
        worker = threading.Thread(
            target=self._background(self._extract_worker), args=(work, results),
            name=f"extract-replacement-{len(self._replacement_workers) + 1}",
            daemon=True)
        self._replacement_workers.append(worker)
        worker.start()

    def _reader_ready(self, candidate: Candidate) -> None:
        r"""Wait for this thread's reader process before `candidate`'s clock starts.

        2026-09-30. The file watchdog times a file by the seconds its thread
        spends waiting on the reader (`FileWatch.enter`/`leave`), and
        `ReaderProcess.read` used to wait for a child's start-up inside that.
        So a child slower to start than the limit - a loaded machine, a short
        limit - timed out the file it was started for; and because a child
        ended for a time-out is replaced by a fresh one that must start in its
        turn, every file after one stuck file timed out as well.

        The wait happens here instead, outside the clock, and only when there
        is something to wait for: a reader process that is not ready and a
        file that would be read in it. It is bounded by the reader's own
        `START_LIMIT_S` and gives up at once if the run is stopping.

        **A process that will not start costs no file.** It is reported once
        (`ERR_READER_PROCESS_START`) and this thread reads its files itself
        for the rest of the run, which is what it does with "Read files in
        separate processes" off. Trying again for every file would cost the
        start-up limit per file.
        """
        slots = self._worker_slots()
        reader = getattr(slots, "reader", None)
        if reader is None or reader.ready or not reads_in_process(candidate.path):
            return
        try:
            reader.wait_ready(cancelled=self._stop.is_set)
        except AppErrorException as exc:
            slots.reader = None
            watch = getattr(slots, "watch", None)
            if watch is not None:
                watch.reader = None
            reader.close()
            error = exc.error
            self._log.warning("{} | {}", error.message, error.suggestion)
            self._record(KIND_WARNING, error.message, detail=error.code)
            self._stats_ref.warned_by_code[error.code] = (
                self._stats_ref.warned_by_code.get(error.code, 0) + 1)

    def _kept_in_hand(self, watch: Optional[FileWatch]) -> list[_Extracted]:
        """What a cut-off mailbox or archive had read and not yet handed on.

        2026-09-30. A reader that attaches a closing warning to its last
        message holds the newest one back (`base.with_closing_warning`), so it
        is always one message ahead of the index. When a time limit or a Force
        skip ended the read, that message went with the reader: read, counted
        as Indexed, and never indexed. It is kept now - "the messages already
        read are kept" includes the last of them.

        **Mailboxes and archives only** (`LIMIT_STALL`). Any other file is one
        document, and one that ran out of time is recorded as timed out, not
        half-indexed. Never raises: keeping one more message must not cost the
        record of the cut-off itself.

        The cost is cutting that one message into passages with no limit on
        it - milliseconds for a message, and finite for any text.
        """
        take = getattr(watch, "in_hand", None)
        if take is None or getattr(watch, "kind", None) != LIMIT_STALL:
            return []
        try:
            return list(take())
        except Exception as exc:                        # noqa: BLE001 - see above
            self._log.warning("could not keep the last message read from {}: {}",
                              getattr(getattr(watch.candidate, "path", None),
                                      "name", watch.candidate), exc)
            return []

    def _time_limit_factor(self, candidate: Candidate) -> float:
        """Order 0z F3: how many times the usual time limit this file is given.

        1 for every run but a retry with a longer limit, which answers for
        the files it is retrying (`app/index/timed_out_retry.py`).
        """
        return 1.0

    def force_skip(self, slot_id: Any) -> bool:
        """The Indexing page's Force skip: skip reader `slot_id`'s current file.

        Safe from the UI thread: it only marks the file, and the watchdog acts
        on it within `file_watch.TICK_S`. False when no run is going or that
        reader has no file open.
        """
        watchdog = getattr(self, "_watchdog", None)
        if watchdog is None:
            return False
        return watchdog.request_skip(slot_id)

    def _offer_stop_token(self, work: "queue.PriorityQueue[Any]") -> bool:
        r"""Enqueue one more `_STOP` marker for a worker about to start.

        Priority `10_000` is the sentinel band `_produce` already uses -
        comfortably past any real candidate's priority, so this marker is
        only ever taken once every real file ahead of it in the queue has
        been. Bounded rather than a plain `put()`: the work queue is exactly
        as likely to be full as growth is likely to be warranted, since both
        follow from the same fact - the walker is outpacing extraction.
        """
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if self._stop.is_set():
                return False
            try:
                work.put((10_000, 0, _STOP, None), timeout=0.25)
                return True
            except queue.Full:
                continue
        return False

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
        """`_read_stream`, inside the pass's images rule (order 0z lane C).

        The rule (`app.extract.reading`) tells a container reader what to do
        with the pictures inside it: hold them on the text pass, read only them
        on the pictures pass. What the reader reports back - how many it held,
        and its per-item status words - is acted on here, once the file ends:
        the archive is remembered for the pictures pass (`held_archives`), and
        the log gets one line of counts. An abandoned read (a stop) still
        remembers held pictures but never forgets any, and logs nothing.
        """
        with reader_reading.reading(
                images=reader_reading.images_for_ocr_mode(self.config.ocr_mode),
                junk=(self._image_book()
                      if getattr(self.config, "junk_images", True) else False)) as policy:
            finished = False
            try:
                yield from self._read_stream(candidate, digest)
                finished = True
            finally:
                self._after_container(candidate, policy, finished)

    def _after_container(self, candidate: Candidate, policy: Any, finished: bool) -> None:
        """The held-pictures record and the counts line for one file. Never raises."""
        try:
            self._held_archives().note(
                candidate.path, held=policy.held, finished=finished,
                pass_=self.config.ocr_mode)
            if policy.not_read:
                # Order 0z lane D. Counted whether or not the read finished:
                # these pictures were left unread either way.
                with self._not_read_lock:
                    total = self._stats_ref.pictures_not_read
                    for reason, n in policy.not_read.items():
                        total[reason] = total.get(reason, 0) + int(n)
            # Order 0z C4, audit of 2026-09-30: an archive cut off by the
            # no-progress limit or a Force skip keeps the messages it read, so
            # it gets its counts line too, with the item it was cut off on as
            # the one `TimedOut`. A Stop or a Pause still logs nothing - that
            # read carries on next run.
            cut_off = not finished and self._cut_off_by_limit(candidate)
            if (finished or cut_off) and policy.counts:
                # "Skipped:decorative=24" beside "Skipped=30": the presenter
                # shows why, in brackets after the word (order 0z lane D).
                counts = dict(policy.counts)
                if cut_off:
                    word = reader_progress.STATUS_TIMED_OUT
                    counts[word] = counts.get(word, 0) + 1
                for reason, n in policy.not_read.items():
                    counts[f"{reader_progress.STATUS_SKIPPED}:{reason}"] = n
                self._record(KIND_ARCHIVE_COUNTS, candidate.path.name,
                             detail=encode_counts(counts))
        except Exception as exc:                        # noqa: BLE001 - bookkeeping only
            self._log.debug("could not note {}: {}", candidate.path.name, exc)

    def _cut_off_by_limit(self, candidate: Candidate) -> bool:
        """Was this thread's read of `candidate` ended by a time limit or a
        Force skip (`file_watch`), rather than by a Stop or a Pause?

        Asked from the reader's own thread while its stream closes, which is
        before the worker loop calls `watch.end()` - so the verdict is still on
        the watch. False for a pipeline with no watchdog.
        """
        watch = getattr(self._worker_slots(), "watch", None)
        if watch is None:
            return False
        with watch.lock:
            return watch.cancel is not None and watch.candidate is candidate

    def _image_book(self) -> PersistentImageBook:
        """This run's junk-image book, made on first use; loads nothing until a
        reader asks for its contents."""
        book = self.__dict__.get("_image_book_store")
        if book is None:
            book = self.__dict__.setdefault("_image_book_store",
                                            PersistentImageBook(self.store))
        return book

    @property
    def _not_read_lock(self) -> threading.Lock:
        lock = self.__dict__.get("_not_read_lock_obj")
        if lock is None:
            lock = self.__dict__.setdefault("_not_read_lock_obj", threading.Lock())
        return lock

    def _held_archives(self) -> HeldArchives:
        """This run's `HeldArchives`, made on first use (bare test pipelines have none)."""
        held = self.__dict__.get("_held_archive_book")
        if held is None:
            held = self.__dict__.setdefault("_held_archive_book", HeldArchives(self.store))
        return held

    def _is_held_archive(self, path: Path) -> bool:
        try:
            return self._held_archives().is_held(path)
        except Exception:                               # noqa: BLE001 - read it normally
            return False

    def _read_stream(
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

        # Work order 0w §2a. After the gate, not before it: a video the gate
        # queues for the tail is not being read now, and a line saying it was
        # would be the kind of thing that makes a log untrustworthy.
        large = large_file_kind(candidate.path, candidate.size_bytes)
        if large:
            self._record(KIND_LARGE_FILE, candidate.path.name,
                         size=candidate.size_bytes, detail=large)

        # Work order 202626270509, item 1b. Only asked of extractors that
        # opt in (`extractor_for(...).supports_resume`); the lookup and the
        # `int()` parse are both guarded, because a resume cursor is an
        # optimisation, not a correctness requirement - `extract()` reads the
        # whole file from message 0 exactly as it always did if this fails
        # or finds nothing, which is always still correct, only slower.
        resume_from = 0
        resume_extra: Optional[dict[str, Any]] = None
        resume_key: Optional[str] = None
        #: Order 0z lane C: the pictures pass coming back for an archive whose
        #: attachment pictures the text pass held. Only the pictures are read,
        #: so this read must not move the archive's resume cursor or write its
        #: "read to the end" marker - the text pass owns both.
        rereading_held = (self.config.ocr_mode == "images"
                          and self._is_held_archive(candidate.path))
        if rereading_held:
            pass
        elif digest is not None:
            extractor = extractor_for(candidate.path)
            if extractor is not None and getattr(extractor, "supports_resume", False):
                try:
                    stored = self.store.get_state(f"{RESUME_STATE_PREFIX}{digest}")
                    if stored:
                        resume_from = int(stored)
                except Exception as exc:                 # noqa: BLE001 - H4
                    self._log.debug(
                        "resume cursor unreadable for {}, starting from the top: {}",
                        candidate.path.name, exc,
                    )
                    resume_from = 0
        elif reads_externally(candidate.path):
            # Work order `dates-live-log-and-interrupted-runs` 3b: an archive,
            # which has no digest - see `ARCHIVE_RESUME_PREFIX`.
            extractor = extractor_for(candidate.path)
            if extractor is not None and getattr(extractor, "supports_resume", False):
                resume_key = _archive_resume_key(candidate.path)
                resume_from, resume_extra = self._load_archive_cursor(
                    candidate, resume_key)

        produced = 0
        seen_keys: set[str] = set()
        #: 0x 3a/3c. This thread's slot, or None outside a pipeline worker (a
        #: test calling this directly). Looked up once per file, not per message.
        slot = getattr(self._worker_slots(), "slot", None)
        #: Work order `pst-resilience` 4a. Set when the archive came up short
        #: for a reason that will pass (Outlook was busy) - see below.
        retry_next_pass = False
        #: How many documents the reader has handed over so far.
        received = 0

        def wrap(index: int, document: Any,
                 chunks: list[dict[str, Any]]) -> Optional[_Extracted]:
            """One document of this file as the consumer takes it, or None for
            one with no text. Shared by the loop below and by `in_hand`, so a
            document kept after a cut-off is keyed exactly as any other."""
            nonlocal produced, retry_next_pass
            if any(_is_transient_partial(w) for w in document.warnings):
                retry_next_pass = True

            if not chunks:
                # One empty message in an archive is ordinary and silent.
                # An empty *file* is a finding worth reporting - it is the
                # scanned PDF case, and the person needs to know why their
                # document is not searchable.
                return None

            key = document.key
            # **The real fix belongs here, not only in `row_key`.** An
            # extractor's `Document.key` already defaults to
            # `str(candidate.path)` for an ordinary single-document file,
            # so by the time `row_key`'s fallback ran, `key` was already
            # truthy and the volume-safe branch never fired - found by
            # `test_the_same_volume_walked_at_two_mount_points_is_one_row`
            # returning the real, letter-bearing path. Only substituted
            # when the extractor left the default in place: a multi-
            # document extractor's own `virtual_path` (an archive member,
            # a mail message) must never be overridden here.
            if (key == str(candidate.path)
                    and candidate.volume_id is not None
                    and candidate.relative_path is not None):
                key = _candidate_row_key(candidate)
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
            if slot is not None:
                slot.item = produced
            return _Extracted(
                candidate=candidate,
                content_hash=digest,
                key=key,
                chunks=chunks,
                meta=document.meta,
                source_kind=document.source_kind,
                warnings=document.warnings,
                first_of_file=(index == 0),
                resume_key=resume_key,
            )

        # 2026-09-30: what a cut-off read had read and not handed on. A reader
        # that holds a document back (`base.with_closing_warning`) names it on
        # this read's `Reading`; `_kept_in_hand` calls this when a time limit
        # or a Force skip ends the file, from this thread or - for a reader
        # stuck in native code - from the watchdog's.
        #
        # **Not the document whose text was being cut into passages** when the
        # cut-off came. That one is the item the read was cut off on: cutting
        # it again here would run with no limit on it, and it may be the very
        # thing that was taking too long.
        policy = reader_reading.current()

        def in_hand() -> list[_Extracted]:
            nonlocal received
            kept: list[_Extracted] = []
            for document in policy.take_in_hand():
                item = wrap(received, document, self._passages(document, None))
                received += 1
                if item is not None:
                    kept.append(item)
            return kept

        watch = getattr(self._worker_slots(), "watch", None)
        if watch is not None:
            watch.in_hand = in_hand
        try:
            for index, (document, chunks) in enumerate(
                self._read_documents(candidate, resume_from, resume_extra, slot)
            ):
                if watch is not None and watch.orphaned:
                    # A thread left behind in native code that has come back.
                    # Its file is recorded and what it held was kept by the
                    # thread that gave up on it; nothing more is wanted.
                    raise FileCancelled()
                received = index + 1
                item = wrap(index, document, chunks)
                if item is not None:
                    yield item
        except AppErrorException as exc:
            if rereading_held:
                # Order 0z lane C. The archive's own row belongs to the text
                # pass; a pictures-pass failure must not overwrite it. Kept on
                # the held list (`held` > 0) so the next pictures pass tries again.
                self._log.warning("could not read the held pictures in {}: {}",
                                  candidate.path.name, exc.error.code)
                reader_reading.current().held += 1
                return
            # A failure part-way through an archive costs the rest of that
            # archive, never the messages already handed over and written.
            yield _Extracted(candidate, digest, error=exc.error)
            return

        if rereading_held:
            # Only pictures were read: no marker (the text pass wrote it), and
            # "no text" here means only that the pictures held none.
            return

        if produced == 0:
            yield _Extracted(candidate, digest, error=make_error(
                "ERR_NO_TEXT_LAYER", "index.pipeline", path=str(candidate.path),
                details="Extracted successfully but produced no text."))
            return

        # The single-document default, volume-substituted the same way the
        # loop above substitutes it - otherwise a volume-backed plain file
        # would look like it had "become a container" the moment its key
        # stopped being the raw `str(candidate.path)`, and get a spurious
        # `source_kind="archive"` marker row alongside its real one.
        default_key = str(candidate.path)
        if candidate.volume_id is not None and candidate.relative_path is not None:
            default_key = _candidate_row_key(candidate)
        if retry_next_pass:
            # **No marker, so the walker reads this archive again next pass.**
            # Owner's decision, delegated and made 2026-09-20 (order
            # `pst-resilience` 4a): retry a partial read when the cause was
            # transient (Outlook busy on a folder), never for real damage -
            # which would re-read the whole archive every run to skip the same
            # messages. The messages already written are not re-embedded: each
            # is compared by its text hash (`_already_current`).
            self._log.info(
                "{} was only partly read (Outlook was busy); it will be read "
                "again on the next pass", candidate.path.name)
        elif seen_keys != {default_key}:
            # This file was a container. Close it with a row for the archive
            # itself so the next run can see it is unchanged and skip it whole.
            yield _Extracted(
                candidate=candidate, content_hash=digest,
                key=default_key, source_kind="archive",
                first_of_file=False, file_marker=True,
                resume_key=resume_key,
            )

    def _read_documents(
        self, candidate: Candidate, resume_from: int,
        resume_extra: Optional[dict[str, Any]], slot: Any,
    ) -> Iterator[tuple[Any, list[dict[str, Any]]]]:
        """Each document of one file with its passages, from wherever it is read.

        0x 5b: in this thread's reader process when there is one and the file's
        reader is on `read_process.PROCESS_READERS`; otherwise here, exactly as
        before. Either way the passages are the same dictionaries - the reader
        process runs the same `extract` and `chunk_document`.
        """
        reader = getattr(self._worker_slots(), "reader", None)
        if reader is not None and reads_in_process(candidate.path):
            yield from reader.read(
                candidate.path, resume_from=resume_from, resume_extra=resume_extra,
                frames=slot.frames if slot is not None else None)
            return
        for document in extract(candidate.path, resume_from=resume_from,
                                resume_extra=resume_extra):
            yield document, self._passages(document, slot)

    def _passages(self, document: Any, slot: Any) -> list[dict[str, Any]]:
        """One document's text cut into passages, as the writer takes them."""
        chunks: list[dict[str, Any]] = []
        # 0x 3a: "chunking" while the text is cut into passages. Two
        # plain stores per document; nothing is formatted here.
        if slot is not None:
            slot.stage = STAGE_CHUNKING
        for ordinal, chunk in enumerate(chunk_document(document)):
            chunks.append({
                "ordinal": ordinal, "text": chunk.text, "page": chunk.page,
                "char_start": chunk.char_start, "char_end": chunk.char_end,
                # Adoptions §6a. `None` for everything that is not a
                # spreadsheet, which is nearly every document.
                "label": chunk.label,
            })
        if slot is not None:
            slot.stage = STAGE_READING
        return chunks

    # -- stage 3: embed and write (one thread: this one) --------------------

    def _maybe_drop_fts_triggers(self, stats: IndexStats) -> None:
        """Drop FTS triggers if bulk mode is requested and the run is large enough.

        §6f: For large runs, dropping the chunk FTS triggers and rebuilding the
        index at the end is faster than updating row-by-row. The dirty flag is
        set before the triggers are dropped, so an interrupted run will rebuild
        FTS on resume.
        """
        wanted = str(self.config.bulk_fts or "auto").lower()
        if wanted == "off":
            return
        # Cannot predict the final chunk count before the run, so for "auto"
        # mode we drop triggers only after we see the first batch. For "on"
        # mode we drop them immediately.
        # Note: For now, we're conservative and only drop for "on" mode.
        # A more aggressive strategy would check pending work in auto mode.
        if wanted != "on":
            return
        self._suspended_fts_triggers = self.store.drop_fts_triggers()
        if self._suspended_fts_triggers:
            self._log.info(
                "dropped FTS content triggers for bulk insert mode"
            )

    def _consume(
        self,
        results: queue.Queue,
        workers: list[threading.Thread],
        stats: IndexStats,
        on_progress: Optional[Callable[[IndexStats], None]],
        work: Optional["queue.PriorityQueue[Any]"] = None,
    ) -> None:
        finished = 0
        since_checkpoint = 0
        last_checkpoint = time.monotonic()
        self._last_summary = last_checkpoint
        stats.sample(now=last_checkpoint)          # the window's first point
        pending_vectors: list[tuple[int, int, str]] = []
        # 0x 5d: this thread does the writing, so its connection gets a page
        # cache sized to the database - see `SqliteStore.size_write_cache` for
        # why that is what kept writes slowing down as the index grew. Asked
        # again at every checkpoint below, as the file grows; given back in
        # `run()`'s teardown.
        self._size_write_cache()
        # §6f: Drop FTS triggers if bulk mode is enabled, before processing
        # any work so row-by-row updates are avoided from the start.
        self._maybe_drop_fts_triggers(stats)

        # 2026-09-29. The walker moves the run from scanning to reading
        # (`_read_in_order`), but only this thread reports progress - so it
        # passes the change on, here, rather than two threads calling
        # `on_progress` at once.
        phase_told = stats.phase
        while finished < self._expected_stops:
            if stats.phase != phase_told:
                phase_told = stats.phase
                if on_progress is not None:
                    try:
                        on_progress(stats)
                    except Exception as exc:  # noqa: BLE001 - as elsewhere
                        self._log.warning("progress reporting failed: {}", exc)
            if self._stop.is_set():
                # Asked to stop - by the UI's pause button, or by the disk
                # guard. Everything already written stays written; the files
                # not reached are simply still PENDING, which is what a resumed
                # run looks for. Checked here, at the only point that commits
                # anything, so a stop can never land mid-write.
                break

            # The person's pause, at the same boundary and for the same
            # reason. **Reported on the way in and on the way out**, because
            # while this thread waits nothing else paints - and a pause that
            # does not say so is the frozen progress bar all over again.
            if self._person_paused():
                # 0x 5d: nothing may sit uncommitted, holding the write lock,
                # for however long the person keeps the run paused.
                self._commit_write_group()
                self._report_pause(stats, on_progress)
                held_from = time.monotonic()
                stopped = self._hold_if_paused()
                # **Timed here and nowhere else.** This thread waits out the
                # whole pause, so it is the one that can say how long it was;
                # the walker and the workers are held for the same seconds and
                # counting theirs as well would report one pause three times.
                note = getattr(self.governor, "note_manual_pause", None)
                if note is not None:
                    note(time.monotonic() - held_from)
                if stopped:
                    break
                self._report_pause(stats, on_progress)

            try:
                # **`waiting` is the honest name for "extraction is the
                # bottleneck".** Timed around the blocking get rather than
                # around extraction itself, because extraction runs on N
                # threads and summing their seconds would give a percentage
                # over 100. What the consumer waits for is what would go
                # faster if more readers were added - which is the question
                # the tuning screen is actually asked. See `index/stages.py`.
                #
                # 0x 5d: **a document that is ready is taken without waiting**,
                # and only when none is does the shared transaction commit
                # before the wait. So documents that arrive back to back are
                # written in one transaction (the case where writing is the
                # bottleneck, and the only one where commits cost anything),
                # and nothing written ever waits uncommitted - holding the
                # write lock - while the consumer waits for the readers.
                try:
                    item = results.get_nowait()
                except queue.Empty:
                    self._commit_write_group()
                    with self._clock.stage(WAITING):
                        item = results.get(timeout=0.25)
            except queue.Empty:
                # Nothing has finished, but a worker may be minutes into a large
                # archive. Say so, rather than leaving a blank screen that reads
                # as a crash.
                #
                # 0x 3d: at least once a second (`HEARTBEAT_SECONDS`), so the
                # page's "message 4,512 of 18,300" and "last activity" stay
                # fresh while one reader is deep inside a large archive.
                now = time.monotonic()
                beat = min(self.config.checkpoint_seconds, HEARTBEAT_SECONDS)
                if on_progress is not None and (now - last_checkpoint) >= beat:
                    last_checkpoint = now
                    try:
                        on_progress(stats)
                    except Exception as exc:  # see the `due` branch
                        self._log.warning("progress reporting failed: {}", exc)
                continue
            if item is _STOP:
                finished += 1
                continue

            # The window's own timer says it is late: let it catch up first.
            self._yield_to_ui()

            if item.first_of_file and item.error is None:
                # Attributed on arrival, not on write. Counting it only when the
                # first document is *written* means an archive whose first
                # message happens to be unchanged reports zero bytes read for
                # the whole file - which is how a 64-second run over 100MB came
                # back saying "0.0 MB".
                stats.bytes_read += item.candidate.size_bytes

            if item.name_only:
                self._begin_write_group()
                self._write_name_only(item)
                self._end_grouped_document()
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
                #
                # §6b: a **blocking** flush, always - even when `pending_vectors`
                # is empty here. The batch that would close this archive's
                # window may already have been handed to the feeder thread
                # asynchronously, a `_write_one` or two ago, and not yet be
                # written; only `_feed_sync`'s queue join knows that, not the
                # emptiness of this local list.
                # Work order 0h, M6 ordering: any CLIP vectors gathered for
                # this archive's own images flush before the marker too, for
                # exactly the reason the text vectors do just above.
                self._flush_pending_images()
                self._feed_sync(pending_vectors)
                if self._embed_abandoned:
                    # A stop abandoned part of a batch (0u 6e), so the archive
                    # may be missing vectors: no marker, and the next run reads
                    # it again. Its finished messages are skipped by hash.
                    continue
                self._write_marker(item)
                continue

            if item.error is None and self._already_current(item):
                # A changed archive still contains mostly unchanged mail. Every
                # message skipped here is an embedding not paid for - which on a
                # 30GB archive with one new email is the entire difference
                # between seconds and hours.
                stats.unchanged_documents += 1
                # Counted, not logged again: the message was logged on the run
                # that wrote it, and a warning on every unchanged message would
                # repeat on every incremental pass.
                self._note_warnings(item, log=False)
                if self._note_resume_progress(item):
                    self._persist_at_folder_boundary(pending_vectors)
                continue

            if item.error is not None:
                self._begin_write_group()
                self._record_skip(item)
                self._end_grouped_document()
                stats.skipped += 1
                stats.skipped_by_code[item.error.code] = (
                    stats.skipped_by_code.get(item.error.code, 0) + 1
                )
            else:
                stats.stage = STAGE_WRITING      # 0x 3a
                with self._clock.stage("write"):
                    self._begin_write_group()
                    pending_vectors.extend(self._write_one(item))
                    self._end_grouped_document(timed=False)
                stats.stage = ""
                stats.indexed += 1
                stats.chunks += len(item.chunks)

            # Work order 202626270509, item 1b. A skip or a write both settle
            # this message's fate for the run - recorded as a skip, or handed
            # to the feeder - so both are candidates to advance the resume
            # cursor once the feeder actually confirms them; see
            # `_note_resume_progress` and `_persist_resume_progress`.
            crossed_folder = self._note_resume_progress(item)

            if len(pending_vectors) >= self.config.embed_batch:
                # §6b: handed off, not embedded here - the consumer moves
                # straight on to the next batch while the feeder thread does
                # this one. See `_feed_async`, and the module docstring.
                self._feed_async(pending_vectors)

            if crossed_folder:
                # 0w 3b: an archive moved on to its next folder. See there.
                self._persist_at_folder_boundary(pending_vectors)

            if len(self._pending_images) >= self.config.embed_batch:
                # Work order 0h, H7 pattern: flushed in a batch on its own
                # threshold - reusing `embed_batch` rather than a new config
                # knob - never one Lance write per photo. Independent of the
                # text threshold above: a run over mostly-photo folders can
                # accumulate many images per text chunk, or none at all.
                self._flush_pending_images()

            since_checkpoint += 1
            now = time.monotonic()
            due = (
                since_checkpoint >= self.config.checkpoint_every
                or (now - last_checkpoint) >= self.config.checkpoint_seconds
            )
            if due:
                since_checkpoint = 0
                last_checkpoint = now
                # 0x 5d: the checkpoint tells another process (the window)
                # how far the run has got, and a reader there sees only what
                # is committed - so everything counted so far is committed
                # first, and the published count is true.
                self._commit_write_group()
                # 0x 5d: the file has grown since the cache was sized.
                self._size_write_cache()
                # **Reporting must never cost the flush.** None of this was
                # guarded, and all of it can raise: `_checkpoint` writes to
                # SQLite, and `on_progress` is the caller's - the CLI's version
                # prints a *filename* to a Windows console, so one path outside
                # cp1252 was a `UnicodeEncodeError` that escaped `_consume`
                # before the final `_embed_pending` (now `_feed_sync`, §6b -
                # the guarantee is unchanged). The whole pending batch was
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
                if work is not None:
                    try:
                        self._maybe_grow_workers(work, results, workers)
                    except Exception as exc:      # noqa: BLE001 - never costs the run
                        self._log.warning("could not add an extraction worker: {}", exc)

        # **The final flush, before anything else can go wrong.** Everything
        # above may have left up to `embed_batch` passages committed to SQLite
        # with no vector; this is the only thing that resolves them, so it runs
        # before the last progress call rather than after it.
        #
        # §6b: **blocking**, deliberately - `run()` must not report success
        # while the feeder thread is still writing the last batch, and an
        # embed failure here must still end the run the way it always has.
        # `_feed_sync` waits for the feeder and re-raises whatever it caught.
        self._flush_pending_images()
        self._feed_sync(pending_vectors)
        # Work order 202626270509, item 1b. Only reached once `_feed_sync`
        # above has returned *without raising* - so everything this run has
        # handed to the feeder, including whatever an interrupted run's last
        # batch was still carrying, is confirmed embedded and written before
        # any resume position derived from it is persisted. See
        # `_note_resume_progress` for why nothing is persisted any earlier
        # than this single point.
        try:
            # Not after an abandoned batch (0u 6e): a position was noted for
            # every message handed to the feeder, and some of those vectors were
            # never written - persisting them would skip those messages on
            # resume, which is silent loss rather than a slower resume.
            if not self._embed_abandoned:
                stats.stage = STAGE_SAVING_RESUME        # 0x 3a
                self._persist_resume_progress()
        except Exception as exc:            # noqa: BLE001 - H4: never the run
            self._log.warning("could not persist mbox resume progress: {}", exc)
        finally:
            stats.stage = ""
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

    # -- §6b: the feeder thread ----------------------------------------------

    def _feed_worker(self) -> None:
        r"""Embed and write, off the consumer's own thread.

        One batch at a time, in the order the consumer handed them over:
        `_embed_pending` is unchanged by this split and is not safe against
        two concurrent calls - it would mean two concurrent vector-store
        writes and two concurrent `mark_indexed_many` batches - so this loop
        is the thing that keeps it to one at a time, not `_embed_pending`
        itself.

        Never lets an exception past itself. It is remembered instead, and
        `_raise_if_feeder_failed` re-raises it on the thread a caller is
        actually watching (`_feed_sync`, or the next `_feed_async`) - which
        is the only way a batch that fails to embed still ends the run
        loudly, exactly as it did before this thread existed. The loop then
        stops rather than silently discarding every batch still behind the
        one that failed.

        **§6g reads `_embedding_now` from the consumer thread**, set for
        exactly the span of the call below - not from the moment a batch is
        queued, which could be a while before the model actually runs. A
        worker started while a CPU embed is genuinely mid-batch would
        compete with it for the same cores; one started while a batch is
        merely *waiting its turn* would not.

        **2026-09-08: the embedder now absorbs one graphics-driver failure
        before this path is reached.** `logs/runs/run-20260908-055844-
        window.log` at 06:29:49: a `887A0020` driver error escaped
        `Embedder.embed()` on one batch and the loud end above did exactly
        what it says, thirty minutes into a run that then sat dead for three
        and a half hours. `Embedder.embed()` now retries that batch once on
        the processor (`_retry_on_processor`) and only raises if the retry
        fails too - so what reaches here is genuine breakage, and nothing in
        this loop or in `_raise_if_feeder_failed` changed. The loud end is
        still right; it is just no longer the first thing a flaky driver
        meets.
        """
        # 0x 3a: "embedding, batch n of m". `done` is this thread's own
        # count; `embed_batches` (m) is counted by whoever hands batches over.
        done = 0
        while True:
            batch = self._feeder_queue.get()
            if batch is _STOP:
                self._feeder_queue.task_done()
                return
            self._embedding_now.set()
            self._stats_ref.embed_batch = done + 1
            try:
                self._embed_pending(batch)
            except BaseException as exc:              # noqa: BLE001 - reraised, not lost
                self._feeder_errors.append(exc)
                self._feeder_queue.task_done()
                return
            finally:
                self._embedding_now.clear()
                self._stats_ref.embed_batch = 0
            done += 1
            self._feeder_queue.task_done()

    def _raise_if_feeder_failed(self) -> None:
        """The feeder's exception, on the caller's own thread - or nothing.

        Always the *first* recorded exception: everything the feeder thread
        catches after it is the same thread already unwinding, not a second,
        independent fault worth reporting instead.
        """
        if self._feeder_errors:
            raise self._feeder_errors[0]

    def _put_on_feeder(self, batch: list[tuple[int, int, str]]) -> None:
        """Hand one batch to the feeder queue, without blocking for ever.

        A plain `put()` would hang if the feeder thread had already ended
        after an earlier error - nobody is left to call `get()`. Polling with
        a short timeout instead means a failed feeder is noticed and raised
        within a fraction of a second rather than freezing the run.

        **Also gives up if the thread has simply ended**, error or not. The
        only way that happens mid-run is the error path - `_STOP` is never
        sent until `run()`'s teardown, after the last `_feed_sync` - so this
        is belt and braces against exactly the deadlock a plain `put()` would
        risk: a batch sitting in the queue for ever with nobody left to call
        `get()` on it.
        """
        while True:
            self._raise_if_feeder_failed()
            if self._feeder_thread is not None and not self._feeder_thread.is_alive():
                return
            try:
                self._feeder_queue.put(batch, timeout=0.5)
                self._stats_ref.embed_batches += 1   # 0x 3a: the m in "n of m"
                return
            except queue.Full:
                continue

    def _feed_async(self, pending: list[tuple[int, int, str]]) -> None:
        r"""Hand a full batch to the feeder thread and move straight on.

        **The overlap §6b exists for.** `pending` is emptied here, in place,
        so the caller keeps the same list object and starts gathering the
        next batch immediately - while this one embeds and writes on the
        feeder thread instead of blocking the consumer.
        """
        # 0x 5d. **Committed before the hand-off, always.** The feeder thread
        # marks these passages embedded on its own connection, which cannot
        # see rows this thread has not committed - and it takes the same
        # write lock this thread holds while a group is open, so handing it
        # work first could leave each waiting on the other.
        self._commit_write_group()
        self._raise_if_feeder_failed()
        if not pending:
            return
        batch = list(pending)
        pending.clear()
        self._put_on_feeder(batch)

    def _feed_sync(self, pending: list[tuple[int, int, str]]) -> None:
        r"""Hand off whatever remains, then wait until it is actually written.

        **The two points that must not race the feeder thread.** Before an
        archive's completion marker - the M6 ordering, vectors before marker,
        does not survive an embed that is still in flight when the marker is
        written - and at the very end of a run, where nothing may report
        success before the last batch's vectors exist. Both call this whether
        or not `pending` itself is empty: a marker can follow a batch that was
        already handed off asynchronously and has not finished yet, and only
        the queue join - not the local list - knows that.
        """
        # 0x 5d: committed first, for the reasons `_feed_async` gives - and
        # because every caller of this goes on to write something that must
        # never be durable before the documents it describes: an archive's
        # completion marker, or a resume cursor.
        self._commit_write_group()
        self._raise_if_feeder_failed()
        if pending:
            batch = list(pending)
            pending.clear()
            self._put_on_feeder(batch)
        self._wait_for_feeder()
        self._raise_if_feeder_failed()

    def _wait_for_feeder(self) -> None:
        r"""Block until every batch handed to the feeder has actually finished.

        **Not a plain `Queue.join()`.** If the feeder thread has already died
        after recording an error, nothing will ever call `task_done()` for
        whatever else was still queued behind the batch that failed - a
        `Queue` has no way to know its only reader is gone - and a plain
        `join()` would then wait for ever for a thread that is not coming
        back. Polling instead means a dead feeder is noticed directly, rather
        than inferred from a hang.

        `unfinished_tasks` is read without the queue's own lock, which is
        exactly right for a poll: a value one increment stale for a moment
        only delays noticing "finished" by one more iteration of this loop,
        never produces a wrong answer.
        """
        while self._feeder_queue.unfinished_tasks > 0:
            if self._feeder_errors:
                return
            if self._feeder_thread is not None and not self._feeder_thread.is_alive():
                return
            time.sleep(0.01)

    def _stop_feeder(self, feeder: threading.Thread) -> None:
        r"""Let the feeder thread end.

        Called from `run()`'s `finally`, after `_consume` has already waited
        (via `_feed_sync`) for every batch it handed off - so by the time
        this runs the feeder is idle on an empty queue, or has already ended
        itself having recorded an error. Either way this only has to ask it
        to stop, and not wait for ever if something has gone wrong enough
        that it does not.
        """
        if feeder.is_alive():
            try:
                self._feeder_queue.put(_STOP, timeout=5)
            except queue.Full:
                pass
        feeder.join(timeout=30)

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
            _candidate_row_key(candidate),
            size_bytes=candidate.size_bytes,
            mtime_ns=candidate.mtime_ns,
            content_hash=None,
            # Nothing was read, so any hash on the row belongs to contents this
            # pass has not seen.
            clear_hash=True,
            status=FileStatus.NAME_ONLY,
            source_kind="file",
            volume_id=candidate.volume_id,
            relative_path=candidate.relative_path,
            parent_dir=_candidate_parent_dir(candidate),
            ext=indexed_ext(candidate.path),
            repo_id=self._repo_id_for(candidate.path),
        )

    #: Work order 0i section 2a: the unified enrichment backlog, one entry
    #: per job kind. `_run_enrichment_drains` below is the "one drain loop"
    #: the item asks for.
    #:
    #: **2026-09-15 - checked what "the three existing ad-hoc drains" this
    #: item's own text names actually are, rather than assumed.** Two are
    #: real: `unembedded_chunk` (`_drain_unembedded`, below - a proper drain
    #: with its own counted pass) and `ocr_pending`, which already existed
    #: but in a different shape - `_no_text_layer_candidates`/`_candidates`
    #: requeue held files by reading `skip_code` back off `files`, not by a
    #: separate counted drain step. It is NOT restructured into a
    #: `_drain_*`-shaped function this session: it is woven into the
    #: walker's candidate stream (`_candidates`), and existing tests
    #: (`test_ocr_passes.py`, `test_review_2026_08_26.py`) pin that exact
    #: integration - changing its shape for a naming consistency would risk
    #: well-tested retry behaviour for no functional gain. Instead it is
    #: made visible the same additive way `unembedded_chunk` is: a count
    #: recorded into `enrichment_counts["ocr_pending"]` from `_candidates`
    #: itself (see there), with no change to what gets requeued or when.
    #:
    #: The third, `image_tag` ("untagged images"), does not exist in any
    #: form - no function, no query, no test, confirmed by search. Declared
    #: here so the mechanism is genuinely extensible, but nothing drains it:
    #: what should count as "untagged" (Florence never attempted vs.
    #: attempted and found nothing) is not recorded anywhere today, and
    #: guessing at that distinction would be inventing product behaviour
    #: rather than migrating it. Left open - see the dated note on this item
    #: in the work order.
    KIND_UNEMBEDDED_CHUNK = "unembedded_chunk"
    KIND_OCR_PENDING = "ocr_pending"      # counted in _candidates, not drained here
    KIND_UNTAGGED_IMAGE = "image_tag"     # declared, not yet drained - see above
    #: Work order 0i section 3b - the fourth kind this order actually builds
    #: a real drain for. See `_drain_caption_trickle` below.
    KIND_CAPTION_TRICKLE = "caption_trickle"
    #: Work order 0j section 1a. Two kinds, not one: "has this photo been
    #: scanned for faces at all" and "does every detected face have a
    #: verdict yet" are different questions with different queues - see
    #: `_drain_face_backfill`/`_drain_face_cluster`.
    KIND_FACE_BACKFILL = "face_backfill"
    KIND_FACE_CLUSTER = "face_cluster"
    #: Work order 202626270515. Drained at the *tail* of a run, not at its start
    #: like the kinds above: it is the most expensive thing in the corpus and
    #: nothing is waiting behind it. See `_drain_media_backlog`.
    KIND_MEDIA_TRANSCRIPT = "media_transcript"

    def _drain_media_backlog(
        self, stats: IndexStats,
        on_progress: Optional[Callable[[IndexStats], None]] = None,
    ) -> None:
        """Read queued videos and recordings. Never fatal; see `media_backlog.drain`."""
        from app.index import media_backlog

        try:
            media_backlog.drain(self, stats, on_progress)
        except Exception as exc:                  # noqa: BLE001 - a repair, not the job
            self._log.warning("enrichment backlog kind {!r} failed: {}",
                              self.KIND_MEDIA_TRANSCRIPT, exc)

    def _run_enrichment_drains(self, stats: IndexStats) -> None:
        """Run every registered enrichment-backlog kind, once, at run start.

        One failing kind must not stop another - each is wrapped separately,
        matching `_drain_unembedded`'s own existing promise ("bounded and
        never fatal") rather than adding a new failure mode on top of it.
        """
        for kind, drain in (
            (self.KIND_UNEMBEDDED_CHUNK, self._drain_unembedded),
            (self.KIND_CAPTION_TRICKLE, self._drain_caption_trickle),
            (self.KIND_FACE_BACKFILL, self._drain_face_backfill),
            (self.KIND_FACE_CLUSTER, self._drain_face_cluster),
        ):
            try:
                drain(stats)
            except Exception as exc:              # noqa: BLE001 - a repair, not the job
                self._log.warning("enrichment backlog kind {!r} failed: {}", kind, exc)

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
                # Work order 0i section 2b: this repair respects the same
                # battery/CPU pacing an ordinary run does, at the same
                # granularity `_produce` already uses it at - one wait per
                # unit of work about to start, here a batch rather than a
                # single file. "Your index gets smarter while you sleep"
                # is the product story; a repair pass that ignored the
                # governor and ran the fan flat out would be the opposite
                # of that promise.
                verdict = self.governor.wait_while_throttled(
                    should_stop=self._stop.is_set)
                stats.paused_seconds = self.governor.paused_seconds
                stats.pauses = self.governor.pauses
                stats.paused = self.governor.paused
                stats.pause_reason = self.governor.pause_reason
                if verdict.action == "stop":
                    break
                pending = [(chunk.id, chunk.file_id, chunk.text) for chunk in batch]
                # **Counted before the call, not after.** `_embed_pending` ends
                # by clearing its own `pending` argument in place
                # (`pending.clear()`), for its normal caller (`_produce`),
                # which reuses one accumulator list across many calls - so
                # `len(pending)` read after the call always measured zero,
                # silently. `vectors_repaired` has reported 0 for every real
                # repair since this function was written; the repair itself
                # was never affected (`mark_embedded`/`mark_indexed_many` run
                # inside `_embed_pending` before the clear), only this count.
                # Found running this session, work order 0i section 2a - see
                # the dated note on the item.
                batch_size = len(pending)
                # 0x 3a: "embedding, batch n". No m: `iter_unembedded` is a
                # generator, and counting what it will yield would be a second
                # query for a number only the progress line wants.
                stats.embed_batch += 1
                self._embed_pending(pending)
                filled += batch_size
        except Exception as exc:                 # noqa: BLE001
            self._log.warning(
                "could not finish filling in missing vectors: {}. Indexing "
                "continues; run `app.cli reembed` when the cause is fixed.", exc)

        # Work order 0i section 2a: recorded even when zero, so the run
        # summary can tell "this kind ran and found nothing outstanding"
        # apart from "this kind did not run" - see `IndexStats.
        # enrichment_counts`. `vectors_repaired` and the log line below keep
        # their old `if filled:` gate unchanged - this is additive, not a
        # behaviour change to what already existed.
        stats.enrichment_counts["unembedded_chunk"] = filled
        stats.embed_batch = 0
        if filled:
            stats.vectors_repaired = filled
            self._log.info(
                "filled in {} chunk(s) that an earlier run left without vectors - "
                "they were searchable by keyword but not by meaning", filled)

    def _drain_caption_trickle(self, stats: IndexStats) -> None:
        r"""Corpus-wide "Describe" for every photo that does not have one yet.

        Work order 0i section 3b. OFF by default
        (`PipelineConfig.caption_trickle_enabled`) - see `app.core.
        settings_registry.CAPTION_TRICKLE_ENABLED` for why. When on, this is
        section 3a's on-demand Describe button run against the whole backlog
        instead of one photo: same model, same label
        (`app.extract.vision_caption.AI_CAPTION_LABEL`), same
        `store.add_caption_chunk`. A model that never answers (Ollama not
        running, or running without a vision-capable model installed) costs
        one `available()` check and nothing more - this never blocks a run
        the way a hung network call would.

        Bounded and never fatal, the same promise `_drain_unembedded` makes:
        a corpus with thousands of undescribed photos must not turn "index
        my new files" into "wait for every old photo to be described first".
        """
        if not self.config.caption_trickle_enabled or self.config.chat_engine == "onnx":
            stats.enrichment_counts[self.KIND_CAPTION_TRICKLE] = 0
            return

        from app.extract.ocr import OcrExtractor
        from app.extract.vision_caption import available, describe_image
        from app.llm.ollama import OllamaClient

        client = OllamaClient(
            url=self.config.ollama_url, model=self.config.ollama_vision_model)
        if not available(client):
            # Declared even when skipped, same reasoning as `_drain_unembedded`'s
            # own "ran and found nothing" vs "did not run" distinction - a
            # switch left on with no model pulled must be visible as zero,
            # not silently absent from the summary.
            stats.enrichment_counts[self.KIND_CAPTION_TRICKLE] = 0
            return

        described = 0
        try:
            for batch in self.store.iter_uncaptioned_images(
                    OcrExtractor.extensions, batch_size=8):
                if self._stop.is_set():
                    break
                # Same per-batch governor pacing `_drain_unembedded` already
                # uses (0i section 2b): a description costs real CPU/network
                # time per photo, so "your index gets smarter while you
                # sleep" must never make a laptop hot in a lap here either.
                verdict = self.governor.wait_while_throttled(
                    should_stop=self._stop.is_set)
                stats.paused_seconds = self.governor.paused_seconds
                stats.pauses = self.governor.pauses
                stats.paused = self.governor.paused
                stats.pause_reason = self.governor.pause_reason
                if verdict.action == "stop":
                    break
                for file_id, path in batch:
                    if self._stop.is_set():
                        break
                    result = describe_image(Path(path), client)
                    if result is not None:
                        self.store.add_caption_chunk(file_id, result.caption)
                        described += 1
        except Exception as exc:                  # noqa: BLE001 - a repair, not the job
            self._log.warning(
                "could not finish the caption trickle: {}. Indexing continues.", exc)

        stats.enrichment_counts[self.KIND_CAPTION_TRICKLE] = described
        if described:
            self._log.info(
                "described {} photo(s) that had no AI description yet", described)

    def _drain_face_backfill(self, stats: IndexStats) -> None:
        r"""Face-scan every already-indexed photo the switch was off for.

        Work order 0j section 1a's own "Backfill for already-indexed images
        runs as an enrichment-backlog job kind". Switch-gated exactly like
        `_maybe_detect_faces` - the same "off means the import never
        happens" shape, checked first, before `face_detect` is even named.
        """
        if not self.config.people_recognition_enabled:
            stats.enrichment_counts[self.KIND_FACE_BACKFILL] = 0
            return

        from app.extract.face_detect import available, detect_faces
        from app.extract.ocr import OcrExtractor

        if not available():
            stats.enrichment_counts[self.KIND_FACE_BACKFILL] = 0
            return

        scanned = 0
        try:
            for batch in self.store.iter_photos_without_face_scan(
                    OcrExtractor.extensions, batch_size=8):
                if self._stop.is_set():
                    break
                verdict = self.governor.wait_while_throttled(
                    should_stop=self._stop.is_set)
                stats.paused_seconds = self.governor.paused_seconds
                stats.pauses = self.governor.pauses
                stats.paused = self.governor.paused
                stats.pause_reason = self.governor.pause_reason
                if verdict.action == "stop":
                    break
                for file_id, path in batch:
                    if self._stop.is_set():
                        break
                    try:
                        detections = detect_faces(Path(path))
                    except Exception as exc:      # noqa: BLE001 - H4: one photo, not the run
                        self._log.debug("face backfill skipped {}: {}", path, exc)
                        continue
                    for detection in detections:
                        self.store.add_face(
                            file_id, detection.bbox, detection.embedding)
                    self.store.mark_face_scanned(file_id)
                    scanned += 1
        except Exception as exc:                  # noqa: BLE001 - a repair, not the job
            self._log.warning(
                "could not finish the face-detection backfill: {}. "
                "Indexing continues.", exc)

        stats.enrichment_counts[self.KIND_FACE_BACKFILL] = scanned
        if scanned:
            self._log.info(
                "face-scanned {} photo(s) indexed before Recognise people "
                "was switched on", scanned)

    def _drain_face_cluster(self, stats: IndexStats) -> None:
        r"""Give every unclustered face a verdict: assign, suggest, or a
        fresh pile. Work order 0j sections 1b and 2c.

        Switch-gated like its siblings above - a face row can only exist if
        the switch was on when it was detected, so this is defence in depth
        rather than the only thing standing between "off" and real work,
        but it is asked anyway: turning the switch off mid-session must stop
        *every* face-shaped thing this pipeline does, immediately, not just
        the ones that create new rows.
        """
        if not self.config.people_recognition_enabled:
            stats.enrichment_counts[self.KIND_FACE_CLUSTER] = 0
            return

        from app.index.face_clustering import centroid_of, cluster_batch

        resolved = 0
        try:
            raw_centroids = self.store.pile_centroids()
            centroids = {
                pile_id: centroid_of(embeddings)
                for pile_id, embeddings in raw_centroids.items()
            }
            for batch in self.store.iter_unclustered_faces(batch_size=32):
                if self._stop.is_set():
                    break
                verdict = self.governor.wait_while_throttled(
                    should_stop=self._stop.is_set)
                stats.paused_seconds = self.governor.paused_seconds
                stats.pauses = self.governor.pauses
                stats.paused = self.governor.paused
                stats.pause_reason = self.governor.pause_reason
                if verdict.action == "stop":
                    break

                plan = cluster_batch(
                    [(face.id, face.embedding) for face in batch], centroids)
                for face_id, pile_id, confidence in plan.assign:
                    self.store.assign_face(face_id, pile_id, confidence=confidence)
                    resolved += 1
                for face_id, pile_id, confidence in plan.suggest:
                    self.store.suggest_face(face_id, pile_id)
                    resolved += 1
                for group in plan.new_piles:
                    new_id = self.store.split_pile(group)
                    if new_id is not None:
                        resolved += len(group)
                        # A fresh pile joins the centroid pool immediately -
                        # the next batch in this same drain, and the next
                        # face in this one, can match against it too,
                        # rather than waiting for another whole run.
                        by_face = dict(
                            (f.id, f.embedding) for f in batch)
                        embeddings = [by_face[fid] for fid in group if fid in by_face]
                        if embeddings:
                            centroids[new_id] = centroid_of(embeddings)
                # `plan.unresolved` faces stay exactly as they were -
                # unassigned, unsuggested - for a later batch to give them
                # company. Nothing to do for them here.
        except Exception as exc:                  # noqa: BLE001 - a repair, not the job
            self._log.warning(
                "could not finish face clustering: {}. Indexing continues.", exc)

        stats.enrichment_counts[self.KIND_FACE_CLUSTER] = resolved
        if resolved:
            self._log.info("sorted {} face(s) into piles or suggestions", resolved)

    def _maybe_detect_faces(self, candidate: Candidate, file_id: int) -> None:
        r"""Section 1a's own images-pass face step. Switch-gated, always.

        **Off means off, all the way down** - the order's own test list
        asserts "no face code runs" when `people_recognition_enabled` is
        False, and this is the one place in the whole images pass that
        could import `app.extract.face_detect` without the caller having
        checked first. The guard is the first line, and `face_detect` is
        imported only after it passes - so switching the setting off costs
        this function nothing, not even the import.

        Detection only, never clustering - `_drain_face_cluster` (the
        enrichment backlog) is what turns a fresh embedding into an
        assignment, a suggestion, or a new pile, the same "written now,
        processed a batch later" shape `_embed_pending` already uses for
        vectors. Splitting them keeps this step as cheap and as bounded as
        `_maybe_compute_phash` beside it.
        """
        if not self.config.people_recognition_enabled:
            return

        from app.extract.base import reads_by_ocr
        from app.extract.face_detect import detect_faces

        path = candidate.path
        if not reads_by_ocr(path):
            return

        try:
            detections = detect_faces(path)
        except Exception as exc:                # noqa: BLE001 - H4: never costs the file
            self._log.warning(
                "no face scan for {}: {}. It stays searchable as usual - "
                "only people-search misses this photo.", path, exc)
            return

        for detection in detections:
            self.store.add_face(file_id, detection.bbox, detection.embedding)
        self.store.mark_face_scanned(file_id)

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
                # The container itself (the `.pst`/archive file) is exactly
                # as volume-safe as an ordinary file - it is `row_key` above
                # that already made the *key* letter-free. Its members are a
                # separate, harder question; see `row_key`'s docstring.
                volume_id=candidate.volume_id,
                relative_path=candidate.relative_path,
            )
            self.store.mark_indexed(file_id)
        # Work order 202626270509, item 1b. The file is done - a resume
        # cursor for it is not merely unneeded, it is a hazard: kept around
        # it would wrongly make a fresh re-index of the same unchanged bytes
        # (after a reset, say) skip messages a new index has never actually
        # seen. H4: never lets a cleanup failure cost the file being marked.
        self._resume_progress.pop(str(candidate.path), None)
        if item.content_hash:
            try:
                self.store.delete_state(f"{RESUME_STATE_PREFIX}{item.content_hash}")
            except Exception as exc:        # noqa: BLE001 - H4
                self._log.debug("could not clear resume cursor: {}", exc)
        # 0w 3b: and an archive's folder cursor, for the same reason. Popped
        # first, so the end of the run cannot write it back.
        self._archive_cursors.pop(str(candidate.path), None)
        if item.resume_key:
            try:
                self.store.delete_state(item.resume_key)
            except Exception as exc:        # noqa: BLE001 - H4
                self._log.debug("could not clear the archive's folder cursor: {}", exc)

    def _note_resume_progress(self, item: _Extracted) -> bool:
        """Remember the furthest position an extractor's `meta` has reported.

        In-memory only, and cheap: a dict update on the consumer's own
        thread, once per settled document. Nothing is written to SQLite here
        - see `_persist_resume_progress` for why that has to wait until a
        flush actually confirms the vectors exist, not merely that the
        chunks were handed to `_write_one`.

        True when an archive read by folders has just moved on to a later
        folder (0w 3b) - the moment a cursor written now would save the most
        re-reading. See `_persist_at_folder_boundary`.
        """
        if item.resume_key and ARCHIVE_FOLDER_META_KEY in item.meta:
            return self._note_archive_progress(item)
        position = item.meta.get(RESUME_POSITION_META_KEY)
        if position is None or item.content_hash is None:
            return False
        try:
            position = int(position)
        except (TypeError, ValueError):
            return False
        path_key = str(item.candidate.path)
        current = self._resume_progress.get(path_key)
        if current is None or current[0] != item.content_hash:
            self._resume_progress[path_key] = (item.content_hash, position)
        elif position > current[1]:
            self._resume_progress[path_key] = (item.content_hash, position)
        return False

    def _note_archive_progress(self, item: _Extracted) -> bool:
        r"""Work order `dates-live-log-and-interrupted-runs` 3b, for one document.

        **The cursor is the folder of the furthest settled document**, and a
        resumed read re-reads that folder whole. Documents reach this thread in
        the order the extractor yielded them (one worker per file, one queue),
        so once a document from folder *g* has settled, every document from
        every folder before *g* has settled too - which is exactly what "skip
        the folders before *g*" needs, once a flush has made them durable.
        What a resumed read finds again in folder *g* itself is already
        indexed and is skipped by its text hash (`_already_current`), so it
        costs a parse and never a duplicate.

        Carried with it: the message count before *g*, so the partial-read
        summary still covers the whole archive; and the content hash of every
        attachment that settled, so an attachment first seen in a skipped
        folder is still deduplicated as an uninterrupted read would. Seeded
        from the cursor this read resumed from, if any, so a run interrupted
        twice forgets nothing the first run knew.
        """
        try:
            folder = int(item.meta[ARCHIVE_FOLDER_META_KEY])
            read = int(item.meta.get(ARCHIVE_READ_META_KEY, 0) or 0)
        except (TypeError, ValueError):
            return False
        path_key = str(item.candidate.path)
        cursor = self._archive_cursors.get(path_key)
        crossed = False
        if cursor is None or cursor["key"] != item.resume_key:
            earlier = self._archive_resumed.pop(path_key, None) or {}
            cursor = {
                "key": item.resume_key,
                "path": str(item.candidate.path),
                "size": int(item.candidate.size_bytes),
                "mtime_ns": int(item.candidate.mtime_ns),
                "folder": folder,
                "read": read,
                "seen": set(earlier.get("seen") or ()),
            }
            self._archive_cursors[path_key] = cursor
        elif folder > cursor["folder"]:
            cursor["folder"], cursor["read"] = folder, read
            crossed = True
        if item.meta.get("attachment_of") and item.meta.get("content_hash"):
            cursor["seen"].add(str(item.meta["content_hash"]))
        return crossed

    def _load_archive_cursor(
        self, candidate: Candidate, key: str,
    ) -> tuple[int, Optional[dict[str, Any]]]:
        """`(resume_from, resume_extra)` for an archive, or `(0, None)`.

        Worker thread. **Used only if the archive is the size and age it was**
        when the cursor was written: folder numbers describe one archive's
        tree, and after Outlook or anybody else has written to it they may
        describe another. Anything unreadable is a read from the top, which is
        always correct and only slower - the cursor is an optimisation, never
        a correctness requirement (H4).
        """
        import json

        try:
            raw = self.store.get_state(key)
            if not raw:
                return 0, None
            cursor = json.loads(raw)
            if (int(cursor.get("size", -1)) != int(candidate.size_bytes)
                    or int(cursor.get("mtime_ns", -1)) != int(candidate.mtime_ns)):
                self._log.info(
                    "{} has changed since it was read part-way; reading it from "
                    "the start", candidate.path.name)
                return 0, None
            folder = int(cursor.get("folder", 0) or 0)
            extra = {"seen": [str(h) for h in cursor.get("seen") or ()],
                     "read": int(cursor.get("read", 0) or 0)}
        except Exception as exc:                     # noqa: BLE001 - H4
            self._log.debug(
                "folder cursor unreadable for {}, starting from the top: {}",
                candidate.path.name, exc)
            return 0, None
        if folder <= 0:
            return 0, None
        self._archive_resumed[str(candidate.path)] = extra
        self._log.info(
            "{} was read part-way before; carrying on at folder {}",
            candidate.path.name, folder)
        self._record(KIND_ARCHIVE, candidate.path.name, detail="resumed")
        return folder, extra

    def _persist_at_folder_boundary(self, pending: list[tuple[int, int, str]]) -> None:
        """Write the resume cursors now, if it has been a while. See
        `RESUME_PERSIST_S`: this is what makes a pulled plug cost seconds.

        **Durable first, then the cursor** - the same order the end of
        `_consume` keeps, for the same reason: a cursor pointing past a message
        whose vectors never landed would skip it on resume, which is loss.
        `_feed_sync` raises if the embedding thread failed, exactly as it does
        before an archive's marker.
        """
        now = time.monotonic()
        if now - self._last_resume_persist < RESUME_PERSIST_S:
            return
        self._last_resume_persist = now
        # 0x 3a: "saving the resume point". The wait for the embedder below
        # can take a few seconds, and this is what the page says meanwhile.
        self._stats_ref.stage = STAGE_SAVING_RESUME
        try:
            self._flush_pending_images()
            self._feed_sync(pending)
            if self._embed_abandoned:
                return
            try:
                self._persist_resume_progress()
            except Exception as exc:            # noqa: BLE001 - H4: never the run
                self._log.warning("could not persist an archive's folder cursor: {}", exc)
        finally:
            self._stats_ref.stage = ""

    def _persist_resume_progress(self) -> None:
        """Flush every tracked resume position to `index_state`, at once.

        **Only ever called right after a `_feed_sync` has returned without
        raising** - both call sites in `_consume` (the one this method is
        actually called from today) rely on that ordering. `_note_resume_progress`
        only ever records a position *before* embedding for it is confirmed;
        this is the one place that turns "seen, and handed to the feeder"
        into "durable enough to trust on the next run" - persisting any
        earlier would let a resumed run skip re-parsing a message whose
        vector never actually made it to LanceDB, which is silent data loss,
        not a slower resume.
        """
        import json

        values = {
            f"{RESUME_STATE_PREFIX}{content_hash}": str(position + 1)
            for content_hash, position in self._resume_progress.values()
        }
        # 0w 3b. The folder itself, not `+ 1`: the folder may not be finished,
        # and it is re-read whole - see `_note_archive_progress`.
        for cursor in self._archive_cursors.values():
            values[cursor["key"]] = json.dumps({
                "path": cursor["path"], "size": cursor["size"],
                "mtime_ns": cursor["mtime_ns"], "folder": cursor["folder"],
                "read": cursor["read"], "seen": sorted(cursor["seen"]),
            })
        if not values:
            return
        self.store.set_states(values)

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

    def _note_warnings(self, item: _Extracted, *, log: bool = True) -> None:
        """Log and count what an extractor warned about on one document.

        **Counted, not only logged.** A warning on a document that indexed
        successfully never reached the skip ledger, so "how many decks are
        mostly pictures" - the number the Office OCR decision turns on - was
        answerable only by grepping a log file. `_warn_if_mostly_pictures` has
        been collecting this evidence since it was written; nothing was
        reading it.

        Called from `_write_one` for a document that was written, and from
        `_consume` for one found already current - never both for the same
        document, so nothing is counted twice. Work order `pst-resilience` 3e:
        an archive's `ERR_PST_PARTIAL` rides on its *last* message, and on an
        incremental run that message is usually unchanged, so it took the
        `_already_current` skip and its warning was never counted.
        """
        for warning in item.warnings:
            code = str(getattr(warning, "code", "") or "")
            # An archive's own summary is logged whatever `log` says: it is one
            # line per archive, and the only place the log names what was missed.
            if log or code == "ERR_PST_PARTIAL":
                self._log.warning("{} | {}", warning.message, warning.suggestion)
            # Work order 0w §2a. **The first of each code, and every partial
            # archive.** A warning is counted per document and 400 decks can
            # carry the same one; four hundred identical lines would push
            # everything else out of the log. A partial archive is one line
            # per archive already, and each one names a different file.
            said = self.__dict__.setdefault("_warned_codes", set())
            if code == "ERR_PST_PARTIAL" or (code and code not in said):
                said.add(code)
                self._record(KIND_WARNING,
                             str(getattr(warning, "message", "") or code), detail=code)
            if code:
                self._stats_ref.warned_by_code[code] = (
                    self._stats_ref.warned_by_code.get(code, 0) + 1)

    # -- 0x 5d: many documents, one transaction -----------------------------

    def _begin_write_group(self) -> None:
        r"""Open the shared transaction the next documents are written into.

        Work order 0x item 5d. **Why this exists, measured.** With the model
        taken out of the picture (the fake embedder), the `write` stage was
        87-96% of `app.cli bench-pipeline`'s run - about 4 ms a document on
        the small corpus and 7 ms on the medium one (Linux sandbox, 4 CPUs,
        2026-09-27). The same writes on a thread of their own cost about 1 ms a
        document; with two threads doing pure-Python work beside them, 20-100
        ms (a stand-alone test script, same machine and day).

        **Most of that time is waiting for Python's interpreter lock, not
        SQLite working.** The whole run used one processor core out of four
        (process CPU time divided by wall time: 0.98), which is what a run
        limited by the interpreter lock looks like. Every SQLite statement
        lets go of the lock while SQLite works and must get it back
        afterwards, and a reader thread busy parsing mail only hands it back
        when the interpreter makes it (every 5 ms by default) - so the
        `write` stage mostly measures the readers' Python work, seen from the
        writer's side. Fewer statements per document means fewer of those
        waits. A document was about eleven statements; `BEGIN` and `COMMIT`
        were two of them, and the per-document bookkeeping
        (`index_generation`) two more.

        **So consecutive documents share one transaction**: one `BEGIN` and one
        `COMMIT` for up to `WRITE_GROUP_MAX_DOCS` documents or
        `WRITE_GROUP_MAX_S` seconds, and `SqliteStore` bumps the generation
        once per transaction instead of twice per document. Each document's
        own `store.batch()` in `_write_one` simply joins the open one (the
        store counts the nesting). Measured 2026-09-27, same sandbox, fake
        embedder, `--full-speed`, runs interleaved with the version before:
        medium corpus 118.6 s -> 110.6 s median over 3 runs each (-6.7%, the
        ranges do not overlap); small corpus 14.4 s -> 14.1 s over 5 each
        (-2.2%, inside the noise). Same documents, passages and vectors.

        **What stays exactly as it was:**

        * *Nothing is durable earlier than before, and nothing that depends on
          a document is durable before it.* The group is committed before
          every hand-off to the embedding thread (`_feed_async`,
          `_feed_sync`), so before any archive marker and any resume cursor;
          before each checkpoint; before a pause, a yield to the window, a
          wait for the readers, and any picture or video work.
        * *A crash loses no more than it did.* A document whose transaction
          had not committed was always redone by the next run; the most that
          can be redone now is one group (a tenth of a second of writing).
          Its resume position was never written, because cursors are only
          written after a `_feed_sync`, which commits first.
        * *Other writers wait no longer than a tenth of a second* - see
          `WRITE_GROUP_MAX_S`.

        Consumer thread only. A no-op when a group is already open.
        """
        if getattr(self, "_write_group", None) is not None:
            return
        group = self.store.batch()
        group.__enter__()
        self._write_group = group
        self._write_group_opened = time.monotonic()
        self._write_group_docs = 0

    def _end_grouped_document(self, *, timed: bool = True) -> None:
        """One document is written into the group; commit if the group is full.

        Full means `WRITE_GROUP_MAX_DOCS` documents or `WRITE_GROUP_MAX_S`
        seconds open, whichever comes first. `timed=False` when the caller is
        already inside the `write` stage's clock, so a commit is not counted
        twice.
        """
        if getattr(self, "_write_group", None) is None:
            return
        self._write_group_docs += 1
        if (self._write_group_docs >= WRITE_GROUP_MAX_DOCS
                or time.monotonic() - self._write_group_opened >= WRITE_GROUP_MAX_S):
            self._commit_write_group(timed=timed)

    def _commit_write_group(self, *, timed: bool = True) -> None:
        """Commit the shared transaction, if one is open. Safe to call any time.

        Counted as `write` time in the stage report, since committing is part
        of writing (it used to happen inside each document's own write) -
        unless `timed=False`, which a caller already inside that clock passes.

        `getattr`, because `_yield_to_ui` calls this and several tests drive
        that on a pipeline built without `__init__`
        (`test_ui_stays_responsive`); such a pipeline has no group, which is
        the answer `getattr` gives.
        """
        group = getattr(self, "_write_group", None)
        if group is None:
            return
        # Cleared first: if the commit raises, the group is over either way
        # (the store has already rolled it back), and nothing may try to
        # commit it a second time.
        self._write_group = None
        if not timed:
            group.__exit__(None, None, None)
            return
        with self._clock.stage("write"):
            group.__exit__(None, None, None)

    def _abandon_write_group(self) -> None:
        """Roll back a shared transaction left open by an error. Never raises.

        Called from `run()`'s teardown. A group still open there means
        `_consume` raised, possibly half-way through writing one document -
        and the store's nested `batch()` does not roll back just that
        document's part - so the whole group is rolled back. Every document in
        it is still unfinished as far as the database is concerned, and is
        read again next run: exactly what happened before 5d to a document
        whose own transaction failed. None of them had been handed to the
        embedding thread or had a resume position written (both commit the
        group first).
        """
        group = getattr(self, "_write_group", None)
        if group is None:
            return
        self._write_group = None
        failure = RuntimeError("index run ended with a write group still open")
        try:
            group.__exit__(RuntimeError, failure, None)
        except Exception as exc:                 # noqa: BLE001 - teardown, never mask
            # The store's own rollback re-raises the exception it was given;
            # that one is expected. Anything else is logged, because the run
            # is already ending on the error that brought us here.
            if exc is not failure:
                self._log.warning("could not roll back the last write group: {}", exc)

    def _size_write_cache(self) -> None:
        """`SqliteStore.size_write_cache`, tolerating a store double without it.

        Half the test suite hands the pipeline stand-in stores; requiring the
        method would make every one of them declare a cache it does not have
        (the same courtesy `_warm_embedder` extends to embedders).
        """
        sizer = getattr(self.store, "size_write_cache", None)
        if callable(sizer):
            sizer()

    def _restore_write_cache(self) -> None:
        """`SqliteStore.restore_write_cache`, with the same tolerance."""
        restore = getattr(self.store, "restore_write_cache", None)
        if callable(restore):
            restore()

    def _media_work_ahead(self, candidate: Candidate) -> bool:
        """Might `_write_one`'s picture/video steps do real work for this file?

        Asked with the same two tests those steps ask first - "is this read by
        OCR" (a picture) and "is this a video or audio file" - so a `False`
        here means every one of them returns at its first line. Plain
        documents and mail, which is nearly everything, answer `False`.
        """
        from app.extract.base import reads_by_ocr

        if getattr(self, "_media_exts", None) is None:
            self._media_exts = frozenset(_media_extensions())
        path = candidate.path
        return path.suffix.lower() in self._media_exts or reads_by_ocr(path)

    def _write_one(self, item: _Extracted) -> list[tuple[int, int, list[float]]]:
        """Chunks and vectors first, INDEXED last.

        A crash between them leaves a file that looks unfinished and is redone.
        The reverse would leave it marked done with no chunks - invisible to
        search, and never retried by anything.
        """
        candidate = item.candidate
        # Work order 0i section 4b: computed once, used by the upsert below.
        taken_at_ns, taken_at_is_hint = self._photo_taken_at(candidate)
        place = self._photo_place(candidate)
        # Work order 202626270515: a video's *recorded* date and place come from
        # its container (ffprobe), carried on the document's meta so the
        # subprocess is not run a second time. The same rule as a photograph's
        # EXIF date - it survives copies where the file's own date does not -
        # and the same precedence: only used when nothing above found one.
        if item.meta:
            if taken_at_ns is None and item.meta.get("media_created_ns"):
                taken_at_ns, taken_at_is_hint = int(item.meta["media_created_ns"]), False
            if place is None and item.meta.get("media_place"):
                place = str(item.meta["media_place"])
            if taken_at_ns is None and item.source_kind in MAIL_KINDS:
                taken_at_ns = _sent_at_ns(item.meta.get("sent_at"))
                taken_at_is_hint = False
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
                # Work order 202626270114 (0b) section 6d. PARTIAL, not
                # PENDING, when there is something to be partial about:
                # `replace_chunks` below gives this file real,
                # keyword-searchable content in the same transaction, so
                # "untouched" stops being true the moment this transaction
                # commits - only "not embedded yet" remains true, until
                # `_embed_pending` (below) or the M6 repair
                # (`_drain_unembedded`) promotes it to INDEXED. A file with
                # no chunks at all stays PENDING, exactly as before this
                # section existed.
                status=(FileStatus.PARTIAL if item.chunks else FileStatus.PENDING),
                source_kind=item.source_kind,
                # Only for a plain file. An archive member's identity is
                # `<container>#<key>`, not `(volume_id, relative_path)` - see
                # `row_key`'s docstring for why that is a known, flagged gap
                # rather than something silently pretended away here.
                volume_id=candidate.volume_id if item.source_kind == "file" else None,
                relative_path=candidate.relative_path if item.source_kind == "file" else None,
                # A message key is `<archive>#<EntryID>`, so its parent directory
                # must come from the archive rather than from splitting a path that
                # is not one. Without this, `path:` filters stop matching mail.
                # For a plain file, `_candidate_parent_dir` is volume-safe too.
                parent_dir=(_candidate_parent_dir(candidate) if item.source_kind == "file"
                           else str(candidate.path.parent)),
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
                # Work order 0f §3a. A photograph's own date beats the file's
                # mtime, which after twenty years of drive-to-drive copies is
                # the date of the last copy and nothing else. None for
                # everything that is not an image, and for an image with no
                # readable EXIF and no era hint - all of which mean "use
                # mtime_ns". Work order 0i section 4b added the era-hint half
                # and the is_hint flag beside it - see _photo_taken_at.
                taken_at_ns=taken_at_ns,
                taken_at_is_hint=taken_at_is_hint,
                place=place,
            )

            chunk_ids = self.store.replace_chunks(file_id, item.chunks)

            if item.meta:
                self._store_message_meta(file_id, item.meta)
                # Work order 0i section 1c. Written whenever this pass's
                # meta carries the key at all - including an empty list, so
                # a re-tag that now finds nothing correctly clears whatever
                # this file had before, rather than leaving stale tags
                # behind that Florence itself no longer stands behind.
                if "ai_tags" in item.meta:
                    self.store.set_file_tags(file_id, item.meta["ai_tags"] or [])

        # Work order 0h §1a/§1c: independent of whether OCR found any text in
        # this file - most photographs have none, and CLIP is exactly the
        # reason finding one by what it depicts must not depend on a caption.
        # Deliberately outside the `store.batch()` above: it is real CPU work
        # (measured, see the work order), and holding a SQLite transaction
        # open across it would block every other writer for no reason - the
        # same argument `_embed_pending`'s comment makes for the LanceDB
        # delete below.
        if self._media_work_ahead(candidate):
            # 0x 5d: the four steps below can be real work - a picture's
            # vector, its hash, its faces, a video's frames - and none of it
            # may run holding the write lock. This document's rows commit now,
            # with any written before it.
            self._commit_write_group(timed=False)   # inside the write clock already
        self._maybe_embed_image(candidate, file_id)
        # Work order 0h §2a. Independent of the CLIP call just above - see
        # `_maybe_compute_phash`'s docstring for why a pHash is computed and
        # gated on its own rather than folded into `_maybe_embed_image`.
        self._maybe_compute_phash(candidate, file_id)
        # Work order 0j section 1a. Independent of both calls above, same
        # reasoning `_maybe_compute_phash` already gives for its own
        # independence from `_maybe_embed_image`: face detection, CLIP and
        # pHash are three unrelated capabilities and a failure in one must
        # never cost either of the others.
        self._maybe_detect_faces(candidate, file_id)
        # Work order 202626270515. Last, and it always releases the video's
        # temporary pictures - see `_maybe_embed_video`.
        self._maybe_embed_video(candidate, file_id, item.meta)

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

        self._note_warnings(item)

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

    # -- work order 0h: the CLIP image-vector lane ---------------------------

    def _maybe_embed_image(self, candidate: Candidate, file_id: int) -> None:
        r"""One CLIP vector for a ladder-passed image, queued for a batched flush.

        **Independent of OCR text.** Most photographs carry none at all, and
        the whole point of this lane is finding one by what it depicts rather
        than by a caption it happens to have. Gated on `reads_by_ocr` - the
        same test `_ocr_gate` uses - so this only ever runs for the image
        types the OCR ladder itself handles, and only when `_write_one` was
        reached at all: an `ocr_mode="text"` run holds images as
        `ERR_OCR_HELD` and never gets here for them, which is exactly the
        pass discipline `_narrow_to_images` already relies on.

        **Computed synchronously, right here** - not gathered across many
        documents the way text chunks are. Text batches because a single
        short passage would waste the ONNX call on overhead; an image forward
        pass is already ~50-150ms of real work with nothing to amortise (see
        the work order's measurement), so deferring the *inference* would buy
        nothing. Only the LanceDB write is batched - see
        `_flush_pending_images` - which is where the H7 pattern actually
        applies.

        **H4 discipline.** A missing model, a corrupt image, an unreadable
        path: logged, counted, and returns quietly. The file stays fully
        indexed by name, folder, type and any OCR text it produced - the lane
        failing costs only this one photo's image-similarity coverage, never
        the file, never the run.
        """
        if self.image_embedder is None or self.image_vectors is None:
            return

        from app.extract.base import reads_by_ocr

        path = candidate.path
        if not reads_by_ocr(path):
            return

        try:
            with self._clock.stage("clip"):
                vector = self.image_embedder.embed([str(path)])[0]
        except Exception as exc:                # noqa: BLE001 - H4: never costs the file
            self._log.warning(
                "no CLIP vector for {}: {}. It stays searchable by name, "
                "folder, type and any OCR text - only image-similarity "
                "search misses it.", path, exc)
            code = str(getattr(getattr(exc, "error", None), "code", "") or "ERR_CLIP_EMBED")
            self._stats_ref.warned_by_code[code] = (
                self._stats_ref.warned_by_code.get(code, 0) + 1)
            return

        self._pending_images.append(
            (file_id, vector, indexed_ext(path) or "", int(candidate.mtime_ns)))

    def _maybe_embed_video(
        self, candidate: Candidate, file_id: int, meta: Optional[dict[str, Any]],
    ) -> None:
        r"""One CLIP vector for a video, from its keyframes. Work order 202626270515.

        **The existing image lane, unchanged, fed pictures a video produced.**
        `app.extract.media` took the scene-change frames and left them in a
        temporary folder named in `meta`; this embeds them with the same
        `image_embedder` a photograph uses and queues the result on the same
        `_pending_images` list, so `_flush_pending_images` writes it at the same
        M6-ordered checkpoints and nothing about the lane's storage changes.

        **One vector per video, the mean of its frames.** The image table's whole
        key is `file_id` (`ImageVectorStore.add_images`), so per-frame rows would
        need a new keyed table; a normalised mean answers "which video looks
        like this" and cannot answer "which minute", which is what the text
        anchors are for. Recorded as a known limit in the work order's note.

        **Always releases the folder**, success or failure, in a `finally` - a
        film's pictures are the biggest temporary thing this pipeline makes.
        Faces are deliberately not detected in frames: a face row stores a crop
        box into a *file*, and there is no picture file to crop from once the
        temporary folder is gone.

        H4: a missing model, a broken frame, an embedder that raises - logged,
        counted, and the video stays fully indexed by its container, its screen
        text and its speech.
        """
        if not meta or not meta.get("keyframes"):
            return
        from app.extract import media as _media

        try:
            kept = [(float(s), str(p)) for s, p in meta["keyframes"] if Path(p).is_file()]
            paths = [p for _s, p in kept]
            if not paths:
                return
            # Faces first and on their own: independent of the CLIP lane (a machine
            # can have one without the other) and switch-gated inside, exactly as a
            # photograph's are. `app/index/video_frames.py` says what it does not do.
            self._maybe_detect_faces_in_frames(file_id, paths)
            if self.image_embedder is None or self.image_vectors is None:
                return
            with self._clock.stage("clip"):
                vectors = self.image_embedder.embed(paths)
            if not len(vectors):
                return
            width = len(vectors[0])
            total = [0.0] * width
            for vector in vectors:
                for i in range(width):
                    total[i] += float(vector[i])
            norm = sum(v * v for v in total) ** 0.5 or 1.0
            mean = [v / norm for v in total]
            self._pending_images.append(
                (file_id, mean, indexed_ext(candidate.path) or "", int(candidate.mtime_ns)))
            # Each picture's own vector, keyed by second, so a search can say which
            # minute and not only which film. **Queued, not written here**: the mean
            # is written by `_flush_pending_images`, which deletes a file's old
            # vectors first, and the frames must go in after that - written now,
            # they would be deleted by the very flush that writes their mean.
            self._pending_frames[file_id] = (
                [(s, v) for (s, _p), v in zip(kept, vectors)],
                indexed_ext(candidate.path) or "", int(candidate.mtime_ns))
        except Exception as exc:                # noqa: BLE001 - H4: never costs the file
            self._log.warning(
                "no CLIP vector for the video {}: {}. It stays searchable by "
                "its details, the words on screen and what was said - only "
                "look-alike search misses it.", candidate.path, exc)
            code = str(getattr(getattr(exc, "error", None), "code", "") or "ERR_CLIP_EMBED")
            self._stats_ref.warned_by_code[code] = (
                self._stats_ref.warned_by_code.get(code, 0) + 1)
        finally:
            _media.release_keyframes(meta)

    def _maybe_detect_faces_in_frames(self, file_id: int, paths: list[str]) -> None:
        r"""Faces in a video's pictures. Switch-gated, first line, like a photograph's.

        Work order 202626270515. **Off means off, all the way down**: the guard is
        the first statement and `app.extract.face_detect` is named only inside
        `video_frames.detect_faces`, after it passes.
        """
        if not self.config.people_recognition_enabled:
            return
        try:
            from app.index import video_frames

            video_frames.detect_faces(
                self.store, file_id, paths, should_stop=self._stop.is_set)
        except Exception as exc:                # noqa: BLE001 - H4: never costs the file
            self._log.warning(
                "no face scan for the video's pictures: {}. It stays searchable as "
                "usual - only people-search misses this film.", exc)

    def _photo_place(self, candidate: Candidate) -> Optional[str]:
        r"""A photograph's place, from its EXIF GPS, offline. Or None.

        Work order 0i section 4a. Same H4 shape as `_photo_taken_at`: gated
        on `reads_by_ocr` so this costs one dictionary lookup for every
        file that is not an image, and any failure - no GPS block, a
        corrupt one, the geocoder package absent - costs this one photo its
        place and nothing else.
        """
        from app.extract.base import reads_by_ocr
        if not reads_by_ocr(candidate.path):
            return None

        try:
            from app.extract.exif import read_gps
            from app.extract.places import available, reverse_geocode

            if not available():
                return None
            coords = read_gps(candidate.path)
            if coords is None:
                return None
            return reverse_geocode(*coords)
        except Exception as exc:                # noqa: BLE001 - H4: a place, not the job
            self._log.debug(
                "no place for {}: {}: {}", candidate.path,
                type(exc).__name__, exc)
            return None

    def _photo_taken_at(self, candidate: Candidate) -> tuple[Optional[int], bool]:
        r"""A photograph's date in epoch nanoseconds, and whether it is a guess.

        Work order 0i section 4b. EXIF first (a fact); when there is none, a
        folder-year era hint (`app.extract.era_hints.guess_year`) for the
        pre-digital case EXIF cannot answer at all - a scanned print, whose
        only camera-adjacent metadata is whatever the scanner stamped on
        today. `mtime_ns` remains the caller's own fallback for neither: see
        `_date_clause` in `app/storage/filters.py`.

        The second element is `taken_at_is_hint` - see `FileRecord.
        taken_at_is_hint` for why a single date column cannot answer "how
        much should this be trusted" on its own, which is what work order
        0512's future batch-era override needs to find only the guesses.
        """
        exif_ns = self._photo_taken_at_ns(candidate)
        if exif_ns is not None:
            return exif_ns, False

        from app.extract.base import reads_by_ocr
        if not reads_by_ocr(candidate.path):
            return None, False

        try:
            from app.extract.era_hints import guess_year, year_to_epoch_ns

            year = guess_year(candidate.path)
            if year is None:
                return None, False
            return year_to_epoch_ns(year), True
        except Exception as exc:                # noqa: BLE001 - H4: a hint, not the job
            self._log.debug(
                "no era hint for {}: {}: {}", candidate.path,
                type(exc).__name__, exc)
            return None, False

    def _photo_taken_at_ns(self, candidate: Candidate) -> Optional[int]:
        r"""A photograph's EXIF shot date in epoch nanoseconds, or None.

        Work order 0f §3a. Written straight into the `files` row by the two
        call sites that create one for an image, rather than carried on the
        `Document` - **and that is the whole design decision here.**

        A `Document` is produced only when an extractor found text. Most
        photographs contain none: `extract()` raises `ERR_NO_TEXT_LAYER` and
        the row is written by `_record_skip`, which never sees a document at
        all. A shot date threaded through `Document.date` alone would
        therefore work for the minority of photos carrying a caption and for
        essentially none of a real twenty-year photo library - the exact
        corpus §3a exists to serve. Reading it here covers both write paths
        with one gate.

        `Document.date` is still declared and still set by the image
        extractors (it was a dead write before this order; see
        `app/extract/base.py`), because an extractor knowing the date of what
        it read is worth stating - it is simply not the mechanism the index
        depends on.

        Gated on `reads_by_ocr`, the same question `_maybe_compute_phash` and
        `_maybe_embed_image` ask, so it costs one dictionary lookup for every
        file that is not an image and opens nothing.

        **H4: one bad file never halts a 100GB run.** `read_datetime` never
        raises by contract, but this is defensive anyway - a photo with
        corrupt or absent EXIF falls back to `mtime_ns` quietly and the run
        carries on. Nothing here is fatal and nothing here is a skip: a
        missing shot date costs this one photo the difference between its
        shot date and its copy date, and costs the run nothing.
        """
        from app.extract.base import reads_by_ocr

        path = candidate.path
        if not reads_by_ocr(path):
            return None

        try:
            from app.extract.exif import read_datetime

            taken = read_datetime(path)
            if taken is None:
                return None
            return int(taken.timestamp() * 1_000_000_000)
        except Exception as exc:                # noqa: BLE001 - H4
            self._log.debug(
                "no EXIF date for {}: {}: {}. It is indexed as usual and dated "
                "by its file time.", path, type(exc).__name__, exc)
            return None

    def _maybe_compute_phash(self, candidate: Candidate, file_id: int) -> None:
        r"""One perceptual hash for a ladder-passed image, queued for a batched flush.

        Work order 0h §2a. **A parallel gate to `_maybe_embed_image`, not a
        step inside it.** Both are gated on `self.<thing> is None` and on
        `reads_by_ocr(path)` - the same two questions, asked independently -
        because the two capabilities genuinely are independent: a pHash is a
        DCT over the pixels (`app/index/phash.py`), nothing to do with CLIP's
        embedding model, so a CLIP failure must not cost the pHash and a
        pHash failure must not cost the CLIP vector. Folding this into
        `_maybe_embed_image` would make one `try/except` respond to two
        unrelated kinds of failure, which is exactly the shape that hides
        which one actually happened when the log is read a year later.

        **Computed synchronously, right here**, same reasoning as the CLIP
        call beside it: `imagehash.phash` is a few milliseconds of pure CPU
        with nothing to batch across images - only the SQLite write is
        gathered, by `_flush_pending_phashes`.

        **H4 discipline**, identical in shape to `_maybe_embed_image`: a
        missing Pillow, a corrupt image, an unreadable path is logged,
        counted, and returns quietly. The file stays fully indexed and fully
        CLIP-searchable regardless - only this one photo's duplicate and
        near-duplicate detection is missing until a later run re-touches it.
        """
        if self.phash_computer is None:
            return

        from app.extract.base import reads_by_ocr

        path = candidate.path
        if not reads_by_ocr(path):
            return

        try:
            with self._clock.stage("phash"):
                value = self.phash_computer.compute(path)
        except Exception as exc:                # noqa: BLE001 - H4: never costs the file
            self._log.warning(
                "no perceptual hash for {}: {}. It stays searchable and "
                "CLIP-findable as usual - only duplicate/near-duplicate "
                "detection misses this photo.", path, exc)
            code = str(getattr(getattr(exc, "error", None), "code", "") or "ERR_PHASH")
            self._stats_ref.warned_by_code[code] = (
                self._stats_ref.warned_by_code.get(code, 0) + 1)
            return

        self._pending_phashes[file_id] = value

    def _flush_pending_phashes(self) -> None:
        r"""Write accumulated pHashes in one batch - the H7 shape, over SQLite.

        Work order 0h §2a. Called from the top of `_flush_pending_images`, so
        it runs at exactly the same checkpoints - before every point that can
        mark a file INDEXED - without a second set of call sites to keep in
        step by hand. A no-op whenever nothing is pending, which is every
        run where `phash_computer` is not configured, so this costs nothing
        for a caller that has not opted in.

        **H4 discipline**, same shape as `_flush_pending_images`'s own store-
        level guard: a SQLite failure here (disk, a locked file) is logged
        and counted, never raised - those photos stay searchable and CLIP-
        findable exactly as before, only duplicate detection is missing.
        """
        if not self._pending_phashes:
            return
        batch = self._pending_phashes
        self._pending_phashes = {}
        try:
            self.store.set_phashes(batch)
        except Exception as exc:                # noqa: BLE001 - H4: never costs the run
            self._log.warning(
                "{} perceptual hash(es) could not be written: {}. Those "
                "photos stay searchable as usual; only duplicate detection "
                "is missing for them.", len(batch), exc)
            self._stats_ref.warned_by_code["ERR_PHASH_STORE"] = (
                self._stats_ref.warned_by_code.get("ERR_PHASH_STORE", 0) + 1)

    def _flush_pending_frames(self, file_ids: list[int]) -> None:
        """Write the per-picture vectors of the videos in this batch. Never raises.

        After the mean, and never costs it (H4): a video whose frame rows could
        not be written is still found by its mean, its details, the words on
        screen and what was said - only "which minute looked like this" misses it.
        """
        from app.index import video_frames

        for file_id in dict.fromkeys(file_ids):
            entry = self._pending_frames.pop(file_id, None)
            if entry is None:
                continue
            moments, ext, mtime_ns = entry
            try:
                video_frames.store_frames(
                    self.image_vectors, file_id, [s for s, _v in moments],
                    [v for _s, v in moments], ext=ext, mtime_ns=mtime_ns)
            except Exception as exc:            # noqa: BLE001 - H4
                self._log.warning(
                    "no per-picture vectors for video file {}: {}. Search still "
                    "finds the film; it cannot say which minute looked like the words.",
                    file_id, exc)
                self._stats_ref.warned_by_code["ERR_CLIP_STORE"] = (
                    self._stats_ref.warned_by_code.get("ERR_CLIP_STORE", 0) + 1)

    def _flush_pending_images(self) -> None:
        r"""Write accumulated CLIP vectors in one batch - the H7 shape.

        **Also flushes pending pHashes, first.** Work order 0h §2a's pHashes
        are computed independently of the CLIP vectors (see
        `_maybe_compute_phash`) but need the identical M6 ordering - written
        before a file can read as INDEXED - and reuse this method's existing
        call sites rather than adding a second set that could drift out of
        step with the first. See `_flush_pending_phashes` for that half;
        everything below is the CLIP-vector half, unchanged.

        **Never one delete-and-add per file.** `_maybe_embed_image` computes
        each vector immediately, but the LanceDB write is gathered here and
        flushed at the same checkpoints `pending_vectors` is (see `_consume`
        and `_write_marker`'s call sites) - one `delete_by_file_ids` and one
        `add_images` for the whole accumulated batch, never per photo. A
        10,000-image folder deleting and re-adding one row at a time is
        exactly the pathology H7 removed from the text table; nothing here
        reintroduces it for the image table.

        **M6 ordering.** Called immediately *before* every point that can
        mark a file INDEXED (`_write_marker`, and both `_feed_sync` /
        `_feed_async` call sites in `_consume`) - and, unlike the text
        vectors, never deferred to the feeder thread. So there is no async
        window at all for a crash to land in: by the time a batch's files can
        read as complete, their image vectors are already written or the
        failure below has already been counted.

        **H4 discipline**, same as `_maybe_embed_image`: a store-level failure
        (disk, a locked file, LanceDB itself) is logged and counted, never
        raised - those photos stay searchable by every route except CLIP
        similarity, and nothing here can fail the run those images belong to.
        """
        if self._pending_images or self._pending_phashes:
            # 0x 5d: a LanceDB write is slow next to a SQLite statement, so
            # the consumer's shared transaction is committed rather than held
            # open across it - the same rule `store.batch()` states.
            self._commit_write_group()
        self._flush_pending_phashes()

        if self.image_vectors is None or not self._pending_images:
            return

        batch = self._pending_images
        self._pending_images = []
        file_ids = [row[0] for row in batch]
        try:
            # Delete-before-add, same reasoning as `_embed_pending`: a
            # re-index must not leave a stale vector behind, and must not
            # remove the old one until the new one is ready. `delete_by_
            # file_ids` is free when the table holds nothing yet, which is
            # every first index - see `VectorStore.delete_by_file_ids`.
            self.image_vectors.delete_by_file_ids(list(dict.fromkeys(file_ids)))
            written = self.image_vectors.add_images(
                file_ids,
                [row[1] for row in batch],
                exts=[row[2] for row in batch],
                mtimes_ns=[row[3] for row in batch],
            )
            self._flush_pending_frames(file_ids)
        except Exception as exc:                # noqa: BLE001 - H4: never costs the run
            self._log.warning(
                "{} image vector(s) could not be written: {}. Those photos "
                "stay searchable by name, folder, type and any OCR text.",
                len(batch), exc)
            self._stats_ref.warned_by_code["ERR_CLIP_STORE"] = (
                self._stats_ref.warned_by_code.get("ERR_CLIP_STORE", 0) + 1)
            return

        if written is not None and written < len(batch):
            # Same reasoning as `_embed_pending`'s identical check: a partial
            # write reported as a full success is how coverage quietly falls
            # over many runs. Counted rather than silently accepted.
            self._log.warning(
                "wrote {} image vector(s) for {} photo(s) - the rest are "
                "missing from image-similarity search until the next run.",
                written, len(batch))
            self._stats_ref.warned_by_code["ERR_CLIP_STORE"] = (
                self._stats_ref.warned_by_code.get("ERR_CLIP_STORE", 0) + 1)

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
            return self._embed_sliced(texts)

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
            return self._embed_sliced(texts)

        embedded = self._embed_sliced(unique)
        if len(embedded) != len(unique) and self._stop.is_set():
            # A stop abandoned the rest of `unique`. `unique` is in first-seen
            # order, so the texts that are finished are exactly the leading run
            # whose slot was embedded.
            finished = len(embedded)
            out = []
            for slot in where:
                if slot >= finished:
                    break
                out.append(embedded[slot])
            return out
        if len(embedded) != len(unique):
            # **The model disagreed about how many it was given.** Rather than
            # map the wrong vectors onto the wrong chunks - which would be
            # silent, permanent and invisible to every test - fall back to the
            # plain path and let its own count check catch it.
            self._log.warning(
                "the model returned {} vectors for {} passages; not reusing",
                len(embedded), len(unique))
            return self._embed_sliced(texts)

        self._stats_ref.chunks_deduped += saved
        return [embedded[index] for index in where]

    def _embed_slice(self) -> int:
        """Passages embedded between two looks at the stop flag. 0 = no slicing.

        One model call on a processor (`CPU_INFER_BATCH`), so a stop costs at
        most one call - about six seconds - rather than the whole batch of up to
        `embed_batch`. The graphics card is left whole: it is the case that
        wants a large batch (see `Embedder._call_options`).
        """
        choice = getattr(self.embedder, "choice", None)
        if choice is not None and getattr(choice, "is_gpu", False):
            return 0
        return CPU_INFER_BATCH

    def _embed_sliced(self, texts: list[str]) -> list:
        """`embed_all`, looking at the stop flag before each slice.

        Returns the vectors of the slices that finished - shorter than `texts`
        only when a stop arrived. Work order 0u 6e.
        """
        size = self._embed_slice() or max(len(texts), 1)
        out: list = []
        for start in range(0, len(texts), size):
            # **The first slice always runs**, unless an earlier batch was already
            # abandoned. A stop keeps "everything gathered so far" up to one model
            # call - the run's final flush happens with the flag set, and treating
            # that as "abandon everything" would throw away the few files a stopped
            # run had just finished (`test_stopping_does_not_lose_completed_work`).
            # Once a batch has been cut, the rest are dropped without a call, so a
            # stop still costs one call in flight plus at most this one.
            if self._stop.is_set() and (start > 0 or self._embed_abandoned):
                break
            out.extend(self.embedder.embed_all(texts[start:start + size]))
        return out

    @staticmethod
    def _whole_file_prefix(pending: list[tuple[int, int, str]], done: int) -> int:
        """How many leading passages of `pending` belong to files finished in
        full when only the first `done` have vectors.

        A file is only marked INDEXED when *all* its passages have vectors, so a
        file cut in half by a stop is abandoned whole - it is redone on resume.
        """
        if done >= len(pending):
            return len(pending)
        cut = done
        boundary = pending[done][1]
        while cut > 0 and pending[cut - 1][1] == boundary:
            cut -= 1
        return cut

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

        if len(vectors) < len(pending) and self._stop.is_set():
            # **A stop arrived mid-batch (0u 6e).** Write what is finished, for
            # whole files only; leave the rest. Their chunks are already
            # committed and their files are still PENDING - the state a crash in
            # this window has always left, which the next run redoes. What must
            # not happen is a marker or a resume position that claims them, so
            # `_embed_abandoned` holds both back.
            keep = self._whole_file_prefix(pending, len(vectors))
            self._embed_abandoned = True
            self._log.info(
                "stopped mid-batch: {} of {} passages embedded and written, {} "
                "left for the next run", keep, len(pending), len(pending) - keep)
            if keep == 0:
                pending.clear()
                return
            del pending[keep:]
            vectors = vectors[:keep]

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
        # Work order 0i section 4b: computed once, used by the upsert below.
        taken_at_ns, taken_at_is_hint = self._photo_taken_at(candidate)
        place = self._photo_place(candidate)
        with self.store.batch():
            file_id = self.store.upsert_file(
                _candidate_row_key(candidate),
                size_bytes=candidate.size_bytes,
                mtime_ns=candidate.mtime_ns,
                content_hash=item.content_hash,
                clear_hash=item.content_hash is None,
                status=FileStatus.PENDING,
                parent_dir=_candidate_parent_dir(candidate),
                ext=indexed_ext(candidate.path),
                # Work order 0f §3a. **The path most photographs actually
                # take**, and the reason the shot date is read here rather
                # than carried on a `Document`: an ordinary photograph has no
                # text in it, so it never produces one and arrives here as
                # `ERR_NO_TEXT_LAYER`. It is still a photograph, it is still
                # findable by name and by CLIP, and it is still from the year
                # it was taken. Work order 0i section 4b: this is also the
                # exact path a scanned print with no EXIF takes, which is
                # what the era hint (see _photo_taken_at) exists for.
                taken_at_ns=taken_at_ns,
                taken_at_is_hint=taken_at_is_hint,
                place=place,
                volume_id=candidate.volume_id,
                relative_path=candidate.relative_path,
            )
            self.store.mark_skipped(file_id, item.error)

        # Work order 0h: **"OCR found nothing" is not the same as "OCR
        # failed".** A blank scan and an ordinary, uncaptioned photograph both
        # read as `ERR_NO_TEXT_LAYER` - and the second is the majority of any
        # real photo corpus. Without this, "every ladder-passed image gets a
        # vector" would be true only for the minority of photos that also
        # happen to contain text, which defeats the item: CLIP does not need
        # OCR to have found anything, and a photograph found by what it
        # depicts rather than by a caption is the acceptance sentence at the
        # top of this order. Every other skip code here (a locked, corrupt or
        # unreadable file) does not get this treatment - CLIP is not
        # confidently more able to open a file OCR could not.
        if item.error is not None and item.error.code == "ERR_NO_TEXT_LAYER":
            self._maybe_embed_image(candidate, file_id)
            # Work order 0h §2a: same reasoning as the CLIP call just above -
            # a photo OCR found no text in is still a photo worth hashing.
            self._maybe_compute_phash(candidate, file_id)

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
                started_at=self._run_started_wall, stats=stats,
                roots=self.config.walk.roots)

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
        self._copy_pause_state(stats)

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
        # 0z F1: the folders this clean-up is limited to, or None for all.
        scope = self._prune_scope()

        # **Offline Media, 1d: offline is not deleted.** `record.path` for a
        # volume-backed row is the synthetic `leasha-volume://...` string
        # (1c), which is never a real filesystem path and always fails
        # `.exists()` - so without this, unplugging a catalogued drive and
        # running an unrelated index over a normal folder would prune every
        # row that drive had ever contributed, the moment `_prune_missing`
        # reached them. Resolved once per run, not once per row: a machine
        # with a dozen catalogued sources must not pay a Windows volume
        # enumeration per candidate.
        from app.index.offline_media import connected_volumes

        online = connected_volumes(self.store)

        def _volume_row_is_missing(record: Any) -> bool:
            if record.volume_id is None:
                return not Path(record.path).exists()
            root = online.get(record.volume_id)
            if root is None:
                # The source is not currently connected. Not seen, not
                # confirmed gone either - exactly the H8 precedent, now
                # formalised for every catalogued row, not only archives.
                return False
            if not record.relative_path:
                return False
            return not (root / record.relative_path).exists()

        # Narrowed in SQL, and only the ids to delete are held. An archive of
        # 200,000 emails is 200,000 rows: materialising every one of them as a
        # FileRecord just to discard the mail is minutes and hundreds of
        # megabytes, at the very end of a run, for nothing.
        #
        # Collected before deleting rather than deleted while iterating: a
        # cursor being read while its table is written underneath it is exactly
        # the kind of thing that works until it does not.
        #
        # **Looked up with `path_key`, the same function that filled `seen`**
        # (order 0x 7b). If this side lower-cased while the walker kept the
        # case of a case-sensitive folder, every file there with a capital
        # letter would look unseen - and be deleted if its row's spelling no
        # longer existed. On Windows both sides are `.lower()`, as before.
        doomed = [
            record.id
            for record in self.store.iter_files(source_kind="file")
            if path_key(record.path) not in seen
            and (not archived or files_under(record.path, archived) is None)
            and (scope is None or files_under(record.path, scope) is not None)
            and _volume_row_is_missing(record)
        ]
        # **The archive's contents go with the archive**, which nothing did.
        # The loop above only ever looked at `source_kind="file"`, and no other
        # deletion path exists for `archive`, `pst_message` or `eml` rows - so
        # deleting a 30GB `.pst` removed the marker row and left two hundred
        # thousand messages searchable for ever, every one of them opening to
        # nothing. Collected before deleting, for the same reason as above.
        doomed.extend(self._doomed_inside_archives(seen, archived))
        return self._delete_in_batches(doomed)

    def _prune_scope(self) -> Optional[list]:
        """`config.prune_under` as a list, or None for the whole index (0z F1)."""
        scope = getattr(self.config, "prune_under", None)
        return list(scope) if scope else None

    def forget_files(self, file_ids: Any) -> int:
        """Remove these files from the index: rows, passages and vectors.

        Work order 0z F1: the folder watch's way to drop a file it was told
        has gone, without a clean-up pass over the whole index. The same
        batched delete the clean-up pass uses, in the same order. Returns how
        many were removed. **The index only** - nothing on disk is touched.
        """
        return self._delete_in_batches([int(file_id) for file_id in file_ids])

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
        scope = self._prune_scope()         # 0z F1: see `_prune_missing`
        for file_id, path in containers:
            if scope is not None and files_under(path, scope) is None:
                continue                      # outside the folders being cleaned
            if Path(path).exists():
                continue                      # still here; its members are fine
            if path_key(path) in seen:
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


def _media_extensions() -> frozenset[str]:
    """Every video and audio extension, imported late so this module does not
    depend on the extractor at load time. See `Pipeline._ocr_gate`."""
    from app.extract.media import media_extensions

    return media_extensions()


def _sent_at_ns(seconds: Any) -> Optional[int]:
    r"""A message's sent date as `files.taken_at_ns`, or None. Never raises.

    **Why a message's date goes in the photo column.** `taken_at_ns` means
    "the date this is *from*" (see `app/storage/filters.py::_date_clause`), and
    `after:`/`before:` read it before `mtime_ns`. A message read out of a
    `.pst` has no file of its own, so its row carries the *archive's*
    `mtime_ns` - the day the mail client last touched the archive - and ten
    thousand letters from 2009-2019 all filtered as "last week". The owner's
    "mail from 2017" found nothing for exactly that reason. The sent date is
    the fact a person means by "from 2017", so it is written where the filter
    already looks; `mtime_ns` stays the archive's real mtime, which change
    detection depends on (the reason `_v18_photo_taken_at` gave the shot date
    a column of its own applies unchanged).

    Nothing reads this column as "is a photograph": the timeline's camera
    branch and the Files tab restrict to `source_kind = 'file'`, and mail has
    its own timeline branch on `messages.sent_at`. Pinned by
    `tests/unit/test_mail_sent_date_filter.py`.
    """
    from app.storage.filters import sent_at_ns

    return sent_at_ns(seconds)


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
