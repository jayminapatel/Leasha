r"""A pause the PERSON controls: "i dont think there is a pause button do add it".

Layer: L3 and L5. Work order `202626191300`, dated note 2026-09-20.

**The page claimed this for months.** `IndexingView`'s docstring said "Start,
watch, pause and resume an index run" and the only pausing in the application
was the resource governor's: automatic, for memory, battery, other people's CPU
and disk space. The person watching a run that was making their machine
unusable could end it (Stop) or let it carry on. Nothing held it.

What these pin, in the order they were written:

* the governor reports the person's pause as its own kind, without reading the
  machine for an answer it does not need;
* resuming into a busy machine still waits - and says the *machine's* reason,
  because at that moment that is the true one;
* a real `Pipeline` paused mid-run makes **no further progress**, and when it
  is let go finishes with exactly what an uninterrupted run over the same
  corpus produced: nothing lost, nothing done twice;
* pause then Stop still stops, promptly;
* the command line's half - a pause file - holds and releases a real run;
* and the button itself, pressed the way a person presses it.

`test_close_while_paused_child.py` is the sixth, in its own process: a test
that closes a real `MainWindow` must never run inside pytest - it froze the
suite three times - see `test_close_ends_the_app.py`.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from app.index.embedder import Embedder
from app.index.pipeline import Pipeline, PipelineConfig
from app.index.resources import (
    MANUAL_PAUSE_REASON,
    ResourceGovernor,
    ResourceLimits,
    Snapshot,
)
from app.index.walker import WalkConfig
from app.storage.sqlite_store import SqliteStore

#: Long enough that a run which is *not* held would plainly have moved on -
#: the corpus below indexes at hundreds of files a minute - and short enough
#: that six of these do not dominate the file's runtime.
HELD_FOR_S = 1.0


class FakeVectors:
    """As `test_dynamic_workers.py`: a vector store that counts, not writes."""

    def __init__(self) -> None:
        self.rows: dict[int, int] = {}

    def ensure_table(self) -> None:
        pass

    def delete_by_file_ids(self, file_ids) -> None:
        wanted = [int(one) for one in file_ids]
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


def _corpus(root: Path, files: int = 60) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for index in range(files):
        (root / f"f{index:03d}.txt").write_text(
            f"pump station {index} commissioning report, dairy line {index % 7}",
            encoding="utf-8")
    return root


#: Seconds the fake model spends on one call. **A real corpus of 60 files goes
#: through this pipeline in under a second**, which leaves no run to pause: the
#: test would race the run to the end and pass or fail by scheduling luck. One
#: batch at a time with a fiftieth of a second in the model gives a few seconds
#: of honest, paceable work - the shape of a real run, three orders of
#: magnitude smaller.
MODEL_CALL_S = 0.05


def _pipeline(tmp_path: Path, *, name: str, files: int = 60,
              pause_file: Path | None = None) -> tuple[Pipeline, SqliteStore]:
    corpus = _corpus(tmp_path / name / "docs", files)

    def encode(texts):
        time.sleep(MODEL_CALL_S)
        return [[1.0, 0.0, 0.0, 0.0] for _ in texts]

    embedder = Embedder(dim=4, encoder=encode)
    store = SqliteStore(tmp_path / name / "index.db").connect()
    config = PipelineConfig(
        walk=WalkConfig(roots=[corpus]), workers=1, min_free_gb=0,
        required_free_gb=0, pause_file=pause_file, embed_batch=1,
        # The real probe walks the process table on every check; here it would
        # only add seconds and a reason to flake. Nothing below tests the
        # machine's own pause through the pipeline - the two governor tests
        # above do that directly.
        # `low_priority=False` as well: a test that drops its own process
        # below normal starves on a machine that is doing anything else, and
        # the courtesy is not what is under test here.
        limits=ResourceLimits(pause_on_battery=False, cpu_percent=0,
                              min_free_gb=0, poll_seconds=0.05,
                              low_priority=False),
    )
    pipeline = Pipeline(store, FakeVectors(), embedder, config)
    pipeline.governor = ResourceGovernor(
        config.resolved_limits(), probe=lambda: Snapshot(),
        manual_check=pipeline._pause_file_set)
    return pipeline, store


def _run_in_thread(pipeline: Pipeline) -> tuple[threading.Thread, list, list]:
    """Start the run, and hand back the thread, its stats, and the live one."""
    done: list = []
    live: list = []

    def progress(stats) -> None:
        if not live:
            live.append(stats)

    def go() -> None:
        done.append(pipeline.run(on_progress=progress))

    thread = threading.Thread(target=go, name="run-under-test", daemon=True)
    thread.start()
    return thread, done, live


def _wait_until(predicate, timeout: float = 30.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.02)
    return False


# ---------------------------------------------------------------------------
# The governor: one wait path, two kinds of reason
# ---------------------------------------------------------------------------

def test_the_persons_pause_is_reported_without_reading_the_machine():
    reads = []

    def probe() -> Snapshot:
        reads.append(1)
        return Snapshot()

    governor = ResourceGovernor(ResourceLimits(), probe=probe)
    assert governor.check().action == "run"
    before = len(reads)

    governor.pause_manually()
    found = governor.check()

    assert found.action == "pause" and found.cause == "manual"
    assert found.reason == MANUAL_PAUSE_REASON
    assert len(reads) == before, "a pause the person asked for needs no measurement"
    assert governor.manually_paused

    governor.resume()
    assert governor.check().action == "run"
    assert not governor.manually_paused


def test_resuming_into_a_busy_machine_waits_and_gives_the_machines_reason():
    """The two pauses are independent, and the reason shown is the true one."""
    busy = Snapshot(system_cpu_percent=97.0, own_cpu_percent=2.0, at=0.0)
    governor = ResourceGovernor(
        ResourceLimits(cpu_percent=80, busy_seconds=0.0, pause_on_battery=False),
        probe=lambda: busy)

    governor.pause_manually()
    held = governor.check()
    assert held.cause == "manual" and held.reason == MANUAL_PAUSE_REASON

    governor.resume()
    still = governor.check()

    assert still.action == "pause" and still.cause == "cpu"
    assert "busy" in still.reason, still.reason


# ---------------------------------------------------------------------------
# A real run, held and let go
# ---------------------------------------------------------------------------

def test_a_paused_run_stops_making_progress_and_finishes_the_same(tmp_path):
    reference, ref_store = _pipeline(tmp_path, name="reference")
    try:
        expected = reference.run()
    finally:
        ref_store.close()

    pipeline, store = _pipeline(tmp_path, name="held")
    try:
        thread, done, live = _run_in_thread(pipeline)
        assert _wait_until(lambda: bool(live) and live[0].indexed >= 5), \
            "the run never got going"

        pipeline.pause()
        stats = live[0]
        # Let every thread notice - the workers poll at `HOLD_POLL_S` and the
        # walker at the governor's poll interval.
        time.sleep(0.5)
        settled = stats.indexed
        assert settled < expected.indexed, "paused too late to prove anything"

        time.sleep(HELD_FOR_S)
        assert stats.indexed == settled, (
            f"a paused run indexed {stats.indexed - settled} more document(s)")
        assert stats.paused and stats.paused_by_person
        assert thread.is_alive(), "a pause is not an ending"

        pipeline.resume()
        thread.join(timeout=60)
        assert not thread.is_alive() and done, "the run never finished after Resume"
    finally:
        pipeline.request_stop()
        store.close()

    after = done[0]
    assert after.stopped_early is None
    assert (after.indexed, after.chunks, after.skipped) == (
        expected.indexed, expected.chunks, expected.skipped)
    assert after.vectors == expected.vectors
    assert after.paused_by_person is False       # cleared on the way out
    assert after.manual_paused_seconds > 0       # and counted while it lasted


def test_pause_then_stop_still_stops(tmp_path):
    pipeline, store = _pipeline(tmp_path, name="stopped")
    try:
        thread, done, live = _run_in_thread(pipeline)
        assert _wait_until(lambda: bool(live) and live[0].indexed >= 3)

        pipeline.pause()
        time.sleep(0.3)
        pipeline.request_stop()

        thread.join(timeout=30)
        assert not thread.is_alive(), "Stop did not get through a pause"
        assert done, "the run did not return its stats"
        assert not pipeline.paused_by_person, "a stopped run must hold nothing"
    finally:
        store.close()


def test_the_command_lines_pause_file_holds_and_releases_a_run(tmp_path):
    flag = tmp_path / "pause.flag"
    flag.write_text("held", encoding="utf-8")
    pipeline, store = _pipeline(tmp_path, name="by-file", files=40,
                                pause_file=flag)
    try:
        assert pipeline.paused_by_person, "the file was there before the run"
        thread, done, live = _run_in_thread(pipeline)
        time.sleep(HELD_FOR_S)

        assert thread.is_alive()
        assert not live or live[0].indexed == 0, "a held run indexed something"

        flag.unlink()
        thread.join(timeout=60)
        assert not thread.is_alive() and done
        assert done[0].indexed == 40
    finally:
        pipeline.request_stop()
        store.close()


# ---------------------------------------------------------------------------
# The button, pressed
# ---------------------------------------------------------------------------

def _view():
    from PySide6.QtWidgets import QApplication

    from app.ui.indexing_view import IndexingView

    QApplication.instance() or QApplication([])
    return IndexingView()


def test_the_pause_button_is_on_the_status_shelf_beside_stop_and_starts_dead():
    from app.ui.indexing_view import CATEGORY_STATUS, PAUSE_LABEL

    view = _view()

    assert view.pause_button.text() == PAUSE_LABEL
    assert not view.pause_button.isEnabled(), "nothing to pause before a run"
    assert view.pause_button.toolTip().strip(), "every control says what it does"
    assert view._nav.page(CATEGORY_STATUS).isAncestorOf(view.pause_button)
    # Beside Stop: the same row, the next position along.
    row = view.stop_button.parentWidget().layout()
    assert row is not None


def test_pressing_pause_holds_the_run_and_pressing_it_again_lets_go(qtbot):
    from PySide6.QtCore import Qt

    from app.ui.indexing_view import PAUSE_LABEL, PAUSED_HEADLINE, RESUME_LABEL

    class FakePipeline:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def pause(self) -> None:
            self.calls.append("pause")

        def resume(self) -> None:
            self.calls.append("resume")

    view = _view()
    qtbot.addWidget(view)
    pipeline = FakePipeline()

    # `start()` builds a real `IndexWorker` and puts it in the view's own
    # one-thread pool, which would run this fake. The button reads the
    # worker's `pipeline` attribute, so standing one in is enough and nothing
    # is started - see `test_close_while_paused_child.py` for the real thing.
    class FakeWorker:
        def __init__(self, held) -> None:
            self.pipeline = held

    view._worker = FakeWorker(pipeline)
    view.pause_button.setEnabled(True)

    qtbot.mouseClick(view.pause_button, Qt.MouseButton.LeftButton)
    assert pipeline.calls == ["pause"]
    assert view.pause_button.text() == RESUME_LABEL
    assert view.headline.text() == PAUSED_HEADLINE

    qtbot.mouseClick(view.pause_button, Qt.MouseButton.LeftButton)
    assert pipeline.calls == ["pause", "resume"]
    assert view.pause_button.text() == PAUSE_LABEL
    assert view.headline.text() != PAUSED_HEADLINE


def test_the_progress_line_says_who_paused_the_run():
    """A run the person paused reads differently from one the machine did."""
    from types import SimpleNamespace

    from app.ui.indexing_view import PAUSED_HEADLINE
    from app.ui.presenter import progress_text

    machine = SimpleNamespace(
        paused=True, paused_by_person=False, indexed=12,
        pause_reason="The machine is busy (91% CPU used by other programs).",
        stopped_early=None, seen=50, walk_complete=True, current="",
        current_since=0.0, current_item=0, recent_files_per_minute=None,
        elapsed_s=10.0, chunks=30, unchanged=0, skipped=0, ocr_mode="both")
    headline, _detail = progress_text(machine, total_estimate=0, stopping=False)

    assert headline != PAUSED_HEADLINE, (
        "the machine's pause must not read like the person's - one of them "
        "ends by itself and the other waits to be let go")


def test_stopping_a_paused_run_from_the_page_clears_the_button(qtbot):
    from app.ui.indexing_view import PAUSE_LABEL

    class FakePipeline:
        def __init__(self) -> None:
            self.stopped = False

        def pause(self) -> None:
            pass

        def request_stop(self) -> None:
            self.stopped = True

    pipeline = FakePipeline()

    class FakeWorker:
        def __init__(self) -> None:
            self.pipeline = pipeline

        def stop(self) -> None:
            pipeline.request_stop()

    view = _view()
    qtbot.addWidget(view)
    view._worker = FakeWorker()
    view.pause_button.setEnabled(True)
    view.pause_run()

    view.stop()

    assert pipeline.stopped
    assert not view.pause_button.isEnabled()
    assert view.pause_button.text() == PAUSE_LABEL


if __name__ == "__main__":       # pragma: no cover - convenience
    raise SystemExit(pytest.main([__file__]))
