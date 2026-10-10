r"""Videos and recordings as a background backlog, not part of the main index pass.

Layer: L3

Work order 202626270515: video and audio are "the most compute-hungry feature in
the stack - runs as enrichment-backlog job kinds (0511 section 2) with trickle
pacing". The measured cost is why. Transcription ran at about 12x real time on a
quiet machine and about 2x on a busy one; every picture taken from a film is then
read like a photograph at 1-7 seconds each. An hour of family video is minutes to
tens of minutes of a processor, so a 100GB archive of it is hours, and a run that
did that inline would leave a person watching a progress bar that had stopped
being about their documents.

**The shape**, and the reason it is this small:

  * A normal ("both") run **finds** every video and recording the switches admit
    and gives it a row that says `ERR_MEDIA_BACKLOG` - findable by name from that
    moment, the same as before this order - and reads nothing. Every ordinary
    document, mail and photo is indexed at full speed, so search is useful in the
    usual hours.
  * At the **tail of the same run**, `drain` reads what is queued: the ledger's
    `ERR_MEDIA_BACKLOG` rows, corpus-wide (so a recording left half-read by a run
    that was stopped last night is picked up without being walked again), paced by
    the run's own resource governor, stoppable between two spoken passages or two
    pictures, resumable through the transcript journal, reported as the
    `media_transcript` backlog kind.
  * The reading itself is **the pipeline, unchanged**: a private `Pipeline` in
    "images" mode whose only candidates come from the ledger. No second writer, no
    second embedding path, no copy of the CLIP or date logic - so what a backlog
    read writes is exactly what an inline read wrote, and the tests that pin
    that are the ones that already existed.

**A "text" run reads none of it** (`ERR_MEDIA_HELD`, unchanged: that is the
person's explicit "queue the images"), and an "images" run reads it inline: it *is*
the second pass. Only "both" - the default - defers, because only "both" used to
put a two-hour recording in the way of everything else.

**Not a setting** (non-negotiable 11): whether the film is read before or after
the spreadsheets is not a thing anybody has an opinion on that the two switches
do not already carry. The switches decide *whether*; this decides *when*, and
"last" has no downside.
"""

from __future__ import annotations

import dataclasses
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Iterator, Optional

from app.core.logging import logger
from app.core.osbridge.pathnames import path_key
from app.index.walker import Candidate

if TYPE_CHECKING:                                   # pragma: no cover
    from app.index.pipeline import IndexStats, Pipeline

__all__ = [
    "KIND_MEDIA_TRANSCRIPT",
    "QUEUED_CODE",
    "applies",
    "defers",
    "queued_paths",
    "queued_candidates",
    "drain",
]

log = logger.bind(component="index.media_backlog")

#: The backlog kind's name in `IndexStats.enrichment_counts`, beside
#: `unembedded_chunk` and `face_backfill`.
KIND_MEDIA_TRANSCRIPT = "media_transcript"

#: The skip code that is the queue. Also in `Pipeline.DEFERRED_SKIP_CODES`.
QUEUED_CODE = "ERR_MEDIA_BACKLOG"


def applies(config: Any) -> bool:
    """Does this run defer media to the tail? Only a "both" run with a switch on."""
    media = getattr(config, "media", None)
    if media is None or getattr(config, "ocr_mode", "both") != "both":
        return False
    return bool(getattr(media, "video_enabled", False)
                or getattr(media, "audio_enabled", False))


def defers(config: Any, path: Path) -> bool:
    """Should the main pass leave `path` for the tail? Pure: a name and two switches.

    Uses `media.disabled_extensions()` - the same answer the walker gave - so a
    `.mp3` is deferred only when the recordings switch is on, and a `.mp4` only
    when the videos one is.
    """
    if not applies(config):
        return False
    from app.extract import media

    suffix = path.suffix.lower()
    return suffix in (media.media_extensions() - media.disabled_extensions())


def queued_paths(store: Any, *, limit: Optional[int] = None) -> list[Path]:
    """Files waiting in the ledger whose switch is (still) on, oldest first."""
    from app.extract import media

    wanted = media.media_extensions() - media.disabled_extensions()
    return [Path(p) for p in store.paths_with_skip_code(QUEUED_CODE, limit=limit)
            if Path(p).suffix.lower() in wanted]


def queued_candidates(store: Any, *, limit: Optional[int] = None) -> Iterator[Candidate]:
    """The queue as walker candidates. A vanished file is skipped in silence: the
    ordinary prune deals with its row, exactly as for `_locked_candidates`."""
    for path in queued_paths(store, limit=limit):
        try:
            stat = path.stat()
        except OSError:
            continue
        # `retry=True`: the row is a settled skip to `_classify` otherwise, and
        # this is the one pass that exists to read it anyway.
        yield Candidate(path=path, size_bytes=stat.st_size, mtime_ns=stat.st_mtime_ns,
                        priority=0, retry=True)


def _backlog_pipeline_class() -> type:
    """The `Pipeline` subclass for the tail, built on demand: `pipeline`
    imports this module, so importing `Pipeline` at the top would be a cycle."""
    from app.index.pipeline import Pipeline

    class BacklogPipeline(Pipeline):
        """`Pipeline` with its candidates taken from the ledger, not from a walk."""

        def __init__(self, *args: Any, queued: list[Candidate], **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            self._queued = queued

        def _candidates(self) -> Iterator[Candidate]:
            seen = self._seen_paths
            for candidate in self._queued:
                # The same key the walker and the clean-up pass use (order 0x
                # 7b): `str(path).lower()` on Windows, byte for byte.
                key = path_key(candidate.path)
                if key not in seen:
                    seen.add(key)
                    yield candidate

        def _run_enrichment_drains(self, stats: Any) -> None:
            # The outer run has already done these; a second pass over the same
            # store one hour later would only be the same query returning nothing.
            return None

        def _announce_phase(self, stats: Any, on_progress: Any, phase: str) -> None:
            # The outer run has already said "reading videos and recordings";
            # this run's own model load and tidying are part of that, and
            # announcing them again would read as the whole run starting over.
            return None

        # 2026-10-10, order 1h item 1c. The outer run's text-in-pictures helper
        # is still installed while this runs (the outer closes it after its own
        # picture text). Installing one here first *cleared* the outer's hook
        # (`_install_ocr_helper` closes before it opens), started a second helper
        # process, and its close left the hook empty - so the outer's run-end OCR
        # was read inside the index process, the fault the helper exists to keep
        # out. The keyframes use the outer's helper; the outer owns it.
        def _install_ocr_helper(self) -> None:
            return None

        def _close_ocr_helper(self) -> None:
            return None

        # The outer run describes photos and reads picture text straight after
        # this returns; doing both here as well loaded Florence and OCR twice.
        def _drain_photo_tags(self, stats: Any, on_progress: Any = None) -> None:
            return None

        def _drain_picture_text(self, stats: Any, on_progress: Any = None) -> None:
            return None

    return BacklogPipeline


class _LinkedStop(threading.Event):
    """An event that is also set whenever `outer` was asked to stop."""

    def __init__(self, outer: Any) -> None:
        super().__init__()
        self._outer = outer

    def is_set(self) -> bool:
        return super().is_set() or bool(getattr(self._outer, "_interrupted", False))


def drain(
    pipeline: "Pipeline",
    stats: "IndexStats",
    on_progress: Optional[Callable[[Any], None]] = None,
) -> None:
    """Read what is queued, after everything else. Bounded, paced, never fatal.

    Called once, at the tail of `Pipeline.run`. A failure here is logged and
    costs the backlog its turn, never the run that already finished its real
    work - the promise every drain in this pipeline makes.
    """
    stats.enrichment_counts[KIND_MEDIA_TRANSCRIPT] = 0
    config = pipeline.config
    if not applies(config):
        return
    # The person stopped the run, or the disk guard did: leave the queue for the
    # next one. `_stop` itself is always set by now (`run` sets it to unblock its
    # own threads), which is why `_interrupted` is the signal.
    if getattr(pipeline, "_interrupted", False) or stats.stopped_early is not None:
        return

    try:
        queued = list(queued_candidates(pipeline.store))
    except Exception as exc:                        # noqa: BLE001 - a repair, not the job
        log.warning("could not read the media backlog: {}", exc)
        return
    if not queued:
        return

    log.info("reading {} video/recording file(s) in the background", len(queued))
    from app.index.pipeline import PHASE_MEDIA

    pipeline._announce_phase(stats, on_progress, PHASE_MEDIA)
    sub_config = dataclasses.replace(
        config,
        ocr_mode="images",
        archives=False,
        prune_missing=False,
        retry_locked=False,
        walk=dataclasses.replace(config.walk, roots=[], extensions=None),
    )
    sub = _backlog_pipeline_class()(
        pipeline.store, pipeline.vectors, pipeline.embedder, sub_config,
        pipeline.governor,
        image_embedder=pipeline.image_embedder,
        image_vectors=pipeline.image_vectors,
        phash_computer=pipeline.phash_computer,
        queued=queued,
    )
    # Work order 0w §2a: the tail is part of this run's story, told in the
    # same log, so the page's log carries on rather than starting again.
    sub.activity_into = stats.activity

    # The person's Stop reaches the outer pipeline only. Forward it, or a two-hour
    # recording would ignore the button it was promised would work. **Two ways,
    # because each covers the other's gap**: the linked flag answers instantly to
    # everything that asks `is_set()` (which is how a transcription checks
    # between two passages), and the watcher turns the sub-run's own stop into a
    # proper `request_stop` for the parts that wait on the event itself.
    sub._stop = _LinkedStop(pipeline)
    finished = threading.Event()

    def watch() -> None:
        while not finished.wait(0.25):
            if getattr(pipeline, "_interrupted", False):
                sub.request_stop()
                return

    watcher = threading.Thread(target=watch, name="media-backlog-stop", daemon=True)
    watcher.start()
    try:
        done = sub.run(on_progress=on_progress)
    except Exception as exc:                        # noqa: BLE001 - never costs the run
        log.warning("the media backlog stopped: {}: {}", type(exc).__name__, exc)
        return
    finally:
        finished.set()
        watcher.join(timeout=2)

    _merge(stats, done, before=len(queued), remaining=len(queued_paths(pipeline.store)))
    stats.enrichment_counts[KIND_MEDIA_TRANSCRIPT] = done.indexed


def _merge(stats: "IndexStats", done: "IndexStats", *, before: int, remaining: int) -> None:
    """Fold the tail's counts into the run's, so one run reads as one run.

    The main pass counted each queued file as skipped (that is how a queue is
    written); the tail has since read most of them. What is still queued stays in
    `skipped_by_code`; what was read becomes `indexed`, and anything the tail
    found genuinely wrong (a damaged recording) is reported under its own code.
    """
    read = max(0, before - remaining)
    held = stats.skipped_by_code.get(QUEUED_CODE, 0)
    if held:
        left = max(0, held - read)
        if left:
            stats.skipped_by_code[QUEUED_CODE] = left
        else:
            stats.skipped_by_code.pop(QUEUED_CODE, None)
    stats.skipped = max(0, stats.skipped - min(held, read))
    stats.indexed += done.indexed
    stats.chunks += done.chunks
    stats.bytes_read += done.bytes_read
    stats.vectors += done.vectors
    stats.embed_failures += done.embed_failures
    stats.paused_seconds += done.paused_seconds
    for code, count in done.skipped_by_code.items():
        if code == QUEUED_CODE:
            continue
        stats.skipped_by_code[code] = stats.skipped_by_code.get(code, 0) + count
        stats.skipped += count
    for code, count in done.warned_by_code.items():
        stats.warned_by_code[code] = stats.warned_by_code.get(code, 0) + count
    if done.indexed:
        stats.add_notice(
            f"{done.indexed} video/recording file(s) were read in the background "
            f"after everything else.")
    if remaining:
        stats.add_notice(
            f"{remaining} video/recording file(s) are still waiting - run indexing "
            f"again and they carry on from where they stopped.")
