r"""Reporting designed for a run of days rather than a run of minutes.

Layer: L3 / L5

From `docs/WORKORDER-terabyte-scale.md` §5: *"A progress line designed for a run
of minutes is unreadable over days."*

The failure this guards is a particular one and it is not a crash. Every number
on the progress line stays plausible while a run degrades: the lifetime average
barely moves after seventy hours, so a run that has slowed to a crawl - or
stopped making progress entirely - reports the throughput it managed on the
first morning. Nothing looks wrong until somebody works out that the file count
has not changed since Tuesday.
"""

from __future__ import annotations

import math
import os
import time
from pathlib import Path

import pytest

from app.index.embedder import Embedder, l2_normalise
from app.index.pipeline import RATE_WINDOW_S, IndexStats, Pipeline, PipelineConfig
from app.index.walker import WalkConfig
from app.storage.sqlite_store import FileStatus, SqliteStore
from app.ui.presenter import progress_text

LONG_AGO = 3600


class NullVectors:
    def delete_by_file_ids(self, file_ids):
        pass

    def add(self, **kwargs):
        return len(kwargs.get("chunk_ids") or ())

    def __getattr__(self, name):
        return lambda *args, **kwargs: None


def _embedder(dim: int = 8) -> Embedder:
    def encode(texts):
        return [
            l2_normalise([math.sin(abs(hash(t)) % 100 + i) for i in range(dim)])
            for t in texts
        ]

    return Embedder(dim=dim, encoder=encode)


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    stamp = time.time() - LONG_AGO
    os.utime(path, (stamp, stamp))
    return path


# --- throughput over a window ----------------------------------------------

def test_no_measurement_is_not_a_rate_of_zero():
    """"Nothing measured yet" and "stopped" must not print the same."""
    stats = IndexStats()

    assert stats.recent_files_per_minute is None
    stats.sample(now=100.0)
    assert stats.recent_files_per_minute is None   # one point is not a rate


def test_the_rate_is_measured_over_the_window():
    stats = IndexStats()
    stats.sample(now=0.0)
    stats.indexed = 600
    stats.bytes_read = 60 * 1_048_576
    stats.sample(now=60.0)

    assert stats.recent_files_per_minute == pytest.approx(600)
    assert stats.recent_mb_per_minute == pytest.approx(60)


def test_a_run_that_has_stopped_moving_says_so_even_after_days():
    r"""**The whole point of the window.**

    Three days of solid progress and then an hour of nothing: the lifetime
    average still reads about 1,400 files an hour and the windowed rate reads
    zero. The second is the one that tells somebody to go and look.
    """
    stats = IndexStats()
    day = 86_400.0

    stats.sample(now=0.0)
    stats.indexed = 100_000                        # three good days
    stats.sample(now=3 * day)
    stats.elapsed_s = 3 * day
    for minute in range(1, 61):                    # then an hour of nothing
        stats.sample(now=3 * day + minute * 60)

    assert stats.files_per_minute > 20             # the lifetime average is fine
    assert stats.recent_files_per_minute == pytest.approx(0.0)


def test_samples_older_than_the_window_are_dropped():
    """Bounded by time, so a week-long run holds a quarter of an hour of
    samples rather than a week of them."""
    stats = IndexStats()
    for second in range(0, 4 * RATE_WINDOW_S, 60):
        stats.indexed += 10
        stats.sample(now=float(second))

    span = stats.recent[-1][0] - stats.recent[0][0]
    assert span <= RATE_WINDOW_S + 60
    assert len(stats.recent) < RATE_WINDOW_S       # nowhere near one per second


def test_the_window_keeps_the_point_before_the_cutoff():
    """Dropping it leaves the first tick after a trim comparing a point with
    itself, which reports a rate of zero on a run that is working perfectly."""
    stats = IndexStats()
    stats.sample(now=0.0)
    stats.indexed = 100
    stats.sample(now=RATE_WINDOW_S + 1)

    assert stats.recent_files_per_minute is not None
    assert stats.recent_files_per_minute > 0


# --- an ETA that may say it does not know ----------------------------------

def test_without_a_scan_the_eta_says_it_does_not_know():
    """*"With `scan` it can be real; without it, no number is better than a
    wrong one."*"""
    stats = IndexStats(indexed=400, seen=500, walk_complete=False)

    _headline, detail = progress_text(stats)

    assert "unknown" in detail
    assert "scan" in detail


def test_with_a_scan_total_the_eta_is_a_real_one():
    stats = IndexStats(indexed=1_000, seen=1_000)
    stats.sample(now=0.0)
    stats.indexed = 2_000
    stats.sample(now=60.0)

    _headline, detail = progress_text(stats, total_estimate=10_000)

    assert "unknown" not in detail
    assert "about" in detail                       # "about 4 hours", not a to-the-second claim


def test_a_scan_that_undercounted_does_not_make_the_eta_say_done():
    """**The bug this closes.** A scan of 680 files against a corpus that
    turned out to hold far more used to make `remaining` go negative the
    moment `done` passed 680, and `format_eta` reads `remaining <= 0` as
    "done" - so the panel announced the run had finished while the headline
    above it kept counting indexed files. `progress_for` already refuses to
    let the stale total cap the *bar*; the ETA must use that same corrected
    total rather than reading the original scan figure straight back out."""
    stats = IndexStats(indexed=1_000, seen=1_200, walk_complete=False)
    stats.sample(now=0.0)
    stats.indexed = 1_100
    stats.sample(now=60.0)

    _headline, detail = progress_text(stats, total_estimate=680)

    assert "done" not in detail
    assert "unknown" not in detail


def test_an_undercounted_scan_says_so_rather_than_changing_silently():
    """A total that grows with no explanation reads as a bug, even though the
    walker finding more than a stale count expected is the ordinary case on a
    corpus that has grown since the last scan."""
    stats = IndexStats(indexed=1_000, seen=1_200, walk_complete=False)
    stats.sample(now=0.0)
    stats.indexed = 1_100
    stats.sample(now=60.0)

    _headline, detail = progress_text(stats, total_estimate=680)

    assert "found more than the last count expected" in detail


def test_the_progress_line_says_which_rate_it_is_quoting():
    """A number with no window attached invites the reading that it is current
    when it is a four-day average."""
    stats = IndexStats()
    stats.sample(now=0.0)
    stats.indexed = 120
    stats.sample(now=60.0)

    _headline, detail = progress_text(stats, total_estimate=1_000)

    assert "last 15 min" in detail


# --- resuming, by actually killing one -------------------------------------

def test_a_run_stopped_part_way_resumes_where_it_stopped(tmp_path):
    r"""**§5 asks for this to be proved by killing a run, not by reading the
    code**, and the reason is the failure it would hide: a resumed run that
    starts from zero is indistinguishable from a slow one for the first several
    hours, and on a corpus this size nobody waits long enough to find out.

    Stopped after the first checkpoint, then restarted: the second run must
    treat what the first wrote as done, and finish the rest.
    """
    root = tmp_path / "docs"
    for index in range(60):
        _write(root / f"f{index:02d}.txt", f"Barnsley Dairy note {index}.")

    with SqliteStore(tmp_path / "index.db") as store:
        first = Pipeline(
            store, NullVectors(), _embedder(),
            PipelineConfig(walk=WalkConfig(roots=[root]), workers=1,
                           checkpoint_every=1, prune_missing=False),
        )
        first.run(on_progress=lambda _s: first.request_stop())

        done_after_first = sum(
            1 for record in store.iter_files(source_kind="file")
            if record.status == FileStatus.INDEXED
        )

        second = Pipeline(
            store, NullVectors(), _embedder(),
            PipelineConfig(walk=WalkConfig(roots=[root]), workers=1),
        )
        stats = second.run()

        total = sum(
            1 for record in store.iter_files(source_kind="file")
            if record.status == FileStatus.INDEXED
        )

    assert 0 < done_after_first < 60, "the first run was not actually interrupted"
    assert total == 60, "the resumed run did not finish the corpus"
    # The heart of it: the second run did not redo the first run's work.
    assert stats.unchanged == done_after_first
    assert stats.indexed == 60 - done_after_first


def test_the_cursor_survives_the_interruption(tmp_path):
    """The cursor is progress reporting rather than the resume mechanism - the
    `files` table is that - but a cursor stuck at zero after sixty hours is
    what somebody looks at to decide whether to kill the run again."""
    root = tmp_path / "docs"
    for index in range(40):
        _write(root / f"f{index:02d}.txt", f"note {index}")

    with SqliteStore(tmp_path / "index.db") as store:
        pipeline = Pipeline(
            store, NullVectors(), _embedder(),
            PipelineConfig(walk=WalkConfig(roots=[root]), workers=1,
                           checkpoint_every=1, prune_missing=False),
        )
        pipeline.run(on_progress=lambda _s: pipeline.request_stop())

        assert int(store.get_state("cursor:indexed", "0") or 0) > 0
        assert store.get_state("cursor:last_path", "")


# --- the daily line ---------------------------------------------------------

def test_a_long_run_writes_a_summary_line(tmp_path):
    """One line a day. A week-long run otherwise produces millions of progress
    lines and is reviewed by nobody."""
    root = tmp_path / "docs"
    for index in range(30):
        _write(root / f"f{index:02d}.txt", f"note {index}")

    from app.core.logging import logger

    written: list[str] = []
    sink = logger.add(lambda message: written.append(str(message)), level="INFO")
    try:
        with SqliteStore(tmp_path / "index.db") as store:
            Pipeline(
                store, NullVectors(), _embedder(),
                PipelineConfig(walk=WalkConfig(roots=[root]), workers=1,
                               checkpoint_every=1, prune_missing=False,
                               # A "day" of zero seconds, so the line this test
                               # is about is reachable without waiting one.
                               summary_every_s=0.0001),
            ).run()
    finally:
        logger.remove(sink)

    summaries = [line for line in written if "of this run" in line]
    assert summaries, "a long run wrote no summary line"
    assert "hours elapsed" in summaries[0]
    assert "Skips by cause" in summaries[0]


def test_a_short_run_writes_no_summary_line(tmp_path):
    """The interval never elapses, so this costs nothing on the runs that are
    over in a minute - which is nearly all of them."""
    root = tmp_path / "docs"
    _write(root / "one.txt", "note")

    from app.core.logging import logger

    written: list[str] = []
    sink = logger.add(lambda message: written.append(str(message)), level="INFO")
    try:
        with SqliteStore(tmp_path / "index.db") as store:
            Pipeline(
                store, NullVectors(), _embedder(),
                PipelineConfig(walk=WalkConfig(roots=[root]), workers=1),
            ).run()
    finally:
        logger.remove(sink)

    assert not [line for line in written if "of this run" in line]
