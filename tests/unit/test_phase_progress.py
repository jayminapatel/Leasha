r"""The stretches of a run with nothing to count, and the bar that sat still.

Layer: L3 and L5. Bug 2b.

**The owner thought the run had stalled, and from the page there was no way to
tell it had not.** Progress ticks came only from the consumer loop, so
everything before it - loading the model, catching up on an earlier run,
planning the roots - and everything after it - pruning, building the vector
index, merging the word index - reported nothing at all. On a large index each
of those is minutes, and a bar that does not move for minutes is a hang as far
as anyone watching can see.

The pipeline now announces each stretch as it starts (`IndexStats.phase`); the
presenter turns the ones with no total into a busy bar and a sentence; the view
paints them without being told anything new. What these pin:

* every phase the pipeline can announce has words, except the one that counts;
* a phase with nothing to count is indeterminate whatever the numbers say, and
  counting resumes when reading does;
* the paint throttle never drops the tick that announces a phase;
* a real run over a tiny corpus announces them, in order;
* and the page itself goes busy and comes back.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from app.index import pipeline as pipeline_module
from app.index.embedder import Embedder
from app.index.pipeline import IndexStats, Pipeline, PipelineConfig
from app.index.resources import ResourceLimits
from app.index.walker import WalkConfig
from app.storage.sqlite_store import SqliteStore
from app.ui.presenter import PHASE_WORDS, phase_words, progress_for, progress_text

#: The order `Pipeline.run` enters its phases on a run with no media backlog.
EXPECTED_ORDER = [
    pipeline_module.PHASE_MODEL,
    pipeline_module.PHASE_WORD_INDEX_CHECK,
    pipeline_module.PHASE_CATCH_UP,
    pipeline_module.PHASE_PLANNING,
    pipeline_module.PHASE_READING,
    pipeline_module.PHASE_TIDYING,
    pipeline_module.PHASE_VECTOR_INDEX,
    pipeline_module.PHASE_WORD_INDEX,
]


def _all_phases() -> list[str]:
    return [value for name, value in vars(pipeline_module).items()
            if name.startswith("PHASE_") and isinstance(value, str)]


# ---------------------------------------------------------------------------
# The presenter: which phases are busy, and what they say
# ---------------------------------------------------------------------------

def test_every_phase_but_reading_has_words() -> None:
    """A phase the pipeline announces with no words here would fall through
    to the counting bar - which is the frozen bar this fixes."""
    phases = _all_phases()
    assert pipeline_module.PHASE_MEDIA in phases, "the phase list went missing"
    for phase in phases:
        if phase == pipeline_module.PHASE_READING:
            assert phase not in PHASE_WORDS, "reading has a real count to show"
        else:
            assert phase in PHASE_WORDS, f"no words for the {phase!r} phase"
    assert set(PHASE_WORDS) <= set(phases), "words for a phase nobody announces"


def test_the_words_are_short_and_say_they_are_still_going() -> None:
    for phase, words in PHASE_WORDS.items():
        assert words.endswith("…"), phase
        assert len(words) <= 60, f"{phase!r} is too long for the detail line"


def test_a_busy_phase_is_indeterminate_whatever_the_counts_say() -> None:
    """The walk is over and the counts are real, but none of them are moving -
    a full bar at the end of a run reads as finished, not as building."""
    stats = SimpleNamespace(phase=pipeline_module.PHASE_VECTOR_INDEX,
                            walk_complete=True, seen=100, indexed=100)
    assert progress_for(stats) == (0, 0)
    assert progress_for(stats, total_estimate=500) == (0, 0)


def test_reading_counts_as_it_always_did() -> None:
    stats = SimpleNamespace(phase=pipeline_module.PHASE_READING,
                            walk_complete=True, seen=100, indexed=40)
    assert progress_for(stats) == (40, 100)
    assert phase_words(stats) == ""


def test_no_phase_at_all_is_unchanged() -> None:
    """A published record from another process carries no phase."""
    stats = SimpleNamespace(walk_complete=True, seen=10, indexed=10)
    assert progress_for(stats) == (10, 10)


def test_the_detail_line_says_what_is_happening() -> None:
    stats = IndexStats(phase=pipeline_module.PHASE_MODEL, indexed=3, seen=7)
    headline, detail = progress_text(stats)
    assert detail == PHASE_WORDS[pipeline_module.PHASE_MODEL]
    assert "3 documents" in headline, "the counts are still true and stay shown"


def test_stopping_and_pausing_still_win() -> None:
    stats = IndexStats(phase=pipeline_module.PHASE_TIDYING)
    assert progress_text(stats, stopping=True)[0].startswith("Stopping")
    stats.paused, stats.pause_reason = True, "The machine is busy."
    assert progress_text(stats)[0].startswith("Paused")


def test_the_command_line_shows_the_phase_too() -> None:
    """Non-negotiable 8: the command line has its own progress line, built
    separately, and was left on an unchanging line for exactly as long."""
    import inspect

    from app.cli import index as cli_index

    source = inspect.getsource(cli_index.cmd_index)
    assert "phase_words(stats)" in source


# ---------------------------------------------------------------------------
# The throttle
# ---------------------------------------------------------------------------

def test_a_phase_change_is_never_throttled() -> None:
    """Announced with one tick, often milliseconds after the last - and it may
    be the only tick for minutes. Dropping it is the frozen bar again."""
    from app.ui.widgets.indexing_layout import paint_due

    view = SimpleNamespace(_last_paint=0.0, _last_paused=False, _stopping=False)
    assert paint_due(view, IndexStats(phase="reading"), 100.0, 0.25)
    assert not paint_due(view, IndexStats(phase="reading"), 100.01, 0.25)
    assert paint_due(view, IndexStats(phase="tidying"), 100.02, 0.25)
    assert not paint_due(view, IndexStats(phase="tidying"), 100.03, 0.25)


# ---------------------------------------------------------------------------
# A real run announces them, in order
# ---------------------------------------------------------------------------

class _Vectors:
    """A vector store that counts, not writes. As `test_pause_button.py`."""

    def __init__(self) -> None:
        self.rows: dict[int, int] = {}

    def ensure_table(self) -> None:
        pass

    def delete_by_file_ids(self, file_ids) -> None:
        wanted = {int(one) for one in file_ids}
        for cid, fid in list(self.rows.items()):
            if fid in wanted:
                del self.rows[cid]

    def add(self, *, chunk_ids, file_ids, vectors) -> int:
        for cid, fid in zip(chunk_ids, file_ids, strict=True):
            self.rows[int(cid)] = int(fid)
        return len(list(chunk_ids))

    def maybe_compact(self, **_k) -> bool:
        return False

    def maybe_create_index(self, **_k) -> bool:
        return False

    def count(self) -> int:
        return len(self.rows)


def test_a_real_run_announces_every_phase_in_order(tmp_path: Path) -> None:
    corpus = tmp_path / "docs"
    corpus.mkdir()
    for index in range(5):
        (corpus / f"f{index}.txt").write_text(
            f"pump station {index} commissioning report", encoding="utf-8")

    embedder = Embedder(dim=4, encoder=lambda texts: [[1.0, 0.0, 0.0, 0.0] for _ in texts])
    seen: list[str] = []

    def progress(stats) -> None:
        # Recorded at the call, because the object carries on changing.
        if not seen or seen[-1] != stats.phase:
            seen.append(stats.phase)

    with SqliteStore(tmp_path / "index.db") as store:
        pipeline = Pipeline(store, _Vectors(), embedder, PipelineConfig(
            walk=WalkConfig(roots=[corpus]), workers=1, min_free_gb=0,
            required_free_gb=0,
            limits=ResourceLimits(pause_on_battery=False, cpu_percent=0,
                                  min_free_gb=0, low_priority=False)))
        stats = pipeline.run(on_progress=progress)

    assert stats.indexed == 5
    assert seen == EXPECTED_ORDER


def test_a_failing_progress_callback_never_costs_the_run(tmp_path: Path) -> None:
    corpus = tmp_path / "docs"
    corpus.mkdir()
    (corpus / "a.txt").write_text("pump station report", encoding="utf-8")

    def broken(_stats) -> None:
        raise UnicodeEncodeError("cp1252", "x", 0, 1, "a console")

    embedder = Embedder(dim=4, encoder=lambda texts: [[1.0, 0.0, 0.0, 0.0] for _ in texts])
    with SqliteStore(tmp_path / "index.db") as store:
        stats = Pipeline(store, _Vectors(), embedder, PipelineConfig(
            walk=WalkConfig(roots=[corpus]), workers=1, min_free_gb=0,
            required_free_gb=0,
            limits=ResourceLimits(pause_on_battery=False, cpu_percent=0,
                                  min_free_gb=0, low_priority=False),
        )).run(on_progress=broken)
    assert stats.indexed == 1


# ---------------------------------------------------------------------------
# The page
# ---------------------------------------------------------------------------

def test_the_page_bar_goes_busy_in_a_phase_and_comes_back(qtbot) -> None:
    pytest.importorskip("PyQt6")
    from app.ui.indexing_view import IndexingView

    view = IndexingView()
    qtbot.addWidget(view)

    reading = IndexStats(phase=pipeline_module.PHASE_READING,
                         walk_complete=True, seen=10, indexed=4)
    view._on_progress(reading.snapshot())
    assert (view.bar.maximum(), view.bar.value()) == (10, 4)

    # Milliseconds later, as it is in a real run: the throttle must let it by.
    building = IndexStats(phase=pipeline_module.PHASE_VECTOR_INDEX,
                          walk_complete=True, seen=10, indexed=10)
    view._on_progress(building.snapshot())
    assert view.bar.maximum() == 0, "a phase with nothing to count is a busy bar"
    assert view.detail.text() == PHASE_WORDS[pipeline_module.PHASE_VECTOR_INDEX]

    reading = IndexStats(phase=pipeline_module.PHASE_READING,
                         walk_complete=True, seen=20, indexed=15)
    view._on_progress(reading.snapshot())
    assert (view.bar.maximum(), view.bar.value()) == (20, 15)
    assert view.detail.text() not in PHASE_WORDS.values()
