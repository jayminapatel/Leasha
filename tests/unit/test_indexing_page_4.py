r"""The Indexing page, order 0x section 4: the button row in its own file, the
"what is happening now" sentence, a bar that glides, and a log with a filter
and Copy.

Layer: L5

Work order `WORKORDER-overhaul-and-mac-ready.md` §4a, §4b, §4c and §4e. Every
promise a person can click is pressed here with pytest-qt, as
`WORKORDER-CONVENTIONS.md` §5b asks - a wiring check that only proves a signal
is connected is not enough on its own.

What these pin:

* **4a** - the row is its own widget; `indexing_view.py` is under its 250-line
  guard *without* the guard being raised; Start, Pause, Resume and Stop still
  work when pressed, against a real `IndexWorker` in the view's own pool; the
  row's tab order runs left to right.
* **4b** - the "now" line shows a sentence while files are read, and is hidden
  whenever another line on the page already says what is happening (a phase,
  a pause, a stop) and after the run.
* **4c** - the bar slides between two values rather than jumping, never moves
  backwards unless the total changed, jumps straight to a new total, is a busy
  bar while the total is unknown, stops sliding when hidden or minimised, and
  runs no timer at all between ticks.
* **4e** - the log's filter hides everything but warnings and errors and brings
  it all back; Copy puts exactly the visible lines, with their times, on the
  clipboard; the new controls have accessible names and come first in Tab
  order on the Status shelf, where they appear.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("PyQt6")

from PyQt6.QtCore import Qt  # noqa: E402
from PyQt6.QtWidgets import QApplication, QWidget  # noqa: E402

from app.index import pipeline as pipeline_module  # noqa: E402
from app.index.activity import ActivityLog  # noqa: E402
from app.index.pipeline import IndexStats  # noqa: E402

pytestmark = pytest.mark.gui

UI = Path(__file__).resolve().parents[2] / "app" / "ui"


def _tab_after(widget):
    """The next widget Tab would land on: the focus chain, skipping the
    pieces (viewports, scroll bars, containers) that never take Tab."""
    current = widget.nextInFocusChain()
    while current is not widget and not (
            current.focusPolicy() & Qt.FocusPolicy.TabFocus):
        current = current.nextInFocusChain()
    return current


def _view(qtbot, *, show: bool = True):
    from app.ui.indexing_view import IndexingView

    view = IndexingView()
    qtbot.addWidget(view)
    if show:
        view.resize(900, 700)
        view.show()
        qtbot.waitExposed(view)
    return view


# ---------------------------------------------------------------------------
# 4a. The row in its own file, and the guard left where it was
# ---------------------------------------------------------------------------

def test_the_view_is_under_its_guard_and_the_guard_was_not_raised() -> None:
    """The fix is the carve-out, not a bigger number. Both halves asserted."""
    source = (UI / "indexing_view.py").read_text(encoding="utf-8").splitlines()
    code = [line for line in source if line.strip() and not line.strip().startswith("#")]
    assert len(code) < 250, f"indexing_view.py has {len(code)} code lines"

    guard = (UI.parents[1] / "tests" / "unit" / "test_presenter.py").read_text(
        encoding="utf-8")
    assert "assert len(code) < 250" in guard, "the guard's limit moved"


def test_the_row_is_one_widget_and_the_buttons_keep_their_names(qtbot) -> None:
    from app.ui.indexing_view import CATEGORY_STATUS
    from app.ui.widgets.indexing_controls import IndexControls

    view = _view(qtbot, show=False)
    assert isinstance(view.controls, IndexControls)
    for name in ("start_button", "scan_button", "stop_button", "pause_button",
                 "reset_button"):
        button = getattr(view, name)
        assert button is getattr(view.controls, name), name
        assert button.parentWidget() is view.controls, name
    assert view._nav.page(CATEGORY_STATUS).isAncestorOf(view.controls)


def test_the_row_tabs_left_to_right(qtbot) -> None:
    """Pause joined the row after Reset was made, so Qt's default tab order
    jumped Stop -> Reset -> Pause. It now follows the row as it reads."""
    view = _view(qtbot, show=False)
    row = [view.start_button, view.pause_button, view.stop_button,
           view.scan_button, view.reset_button]
    for first, second in zip(row, row[1:]):
        assert _tab_after(first) is second, (first.text(), second.text())


class _HeldPipeline:
    """A pipeline whose `run` goes until it is told to stop, ticking as it
    goes, and records every pause and resume. What the real buttons drive."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self._stop = threading.Event()
        self.store = None
        self.config = SimpleNamespace(limits=SimpleNamespace(low_priority=False))

    def pause(self) -> None:
        self.calls.append("pause")

    def resume(self) -> None:
        self.calls.append("resume")

    def request_stop(self) -> None:
        self.calls.append("stop")
        self._stop.set()

    def run(self, on_progress=None):
        stats = IndexStats(phase=pipeline_module.PHASE_READING,
                           walk_complete=True, seen=100)
        while not self._stop.wait(0.05):
            stats.indexed = min(99, stats.indexed + 1)
            if on_progress is not None:
                on_progress(stats)
        stats.stopped_early = "stopped"
        return stats


class _NoLock:
    """Stands in for the machine-wide run lock, which another test process on
    the same box could be holding. The lock has its own tests."""

    def __init__(self, *_a, **_k) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_exc) -> None:
        return None


def test_start_pause_resume_stop_pressed_for_real(qtbot, monkeypatch) -> None:
    """The row moved; pressing its buttons must do exactly what it did.

    Start is wired by the window (`shell.py` connects `start_button.clicked`),
    so the test wires it the same way; everything after that is the page's own.
    """
    from app.core import run_lock
    from app.ui.indexing_view import PAUSE_LABEL, PAUSED_HEADLINE, RESUME_LABEL

    monkeypatch.setattr(run_lock, "IndexRunLock", _NoLock)
    view = _view(qtbot)
    pipeline = _HeldPipeline()
    view.start_button.clicked.connect(lambda _c=False: view.start(pipeline))
    left = Qt.MouseButton.LeftButton

    qtbot.mouseClick(view.start_button, left)
    assert view.is_running()
    assert not view.start_button.isEnabled()
    assert view.stop_button.isEnabled() and view.pause_button.isEnabled()
    # Ticks arrive through the real signal and move the bar and the "now" line.
    qtbot.waitUntil(lambda: view.bar.maximum() == 100, timeout=5000)
    qtbot.waitUntil(lambda: view.now_line.isVisible(), timeout=5000)

    qtbot.mouseClick(view.pause_button, left)
    assert pipeline.calls == ["pause"]
    assert view.pause_button.text() == RESUME_LABEL
    assert view.headline.text() == PAUSED_HEADLINE

    qtbot.mouseClick(view.pause_button, left)
    assert pipeline.calls == ["pause", "resume"]
    assert view.pause_button.text() == PAUSE_LABEL

    qtbot.mouseClick(view.stop_button, left)
    assert pipeline.calls[-1] == "stop"
    assert view.headline.text() == "Stopping after the current file…"
    assert not view.stop_button.isEnabled() and not view.pause_button.isEnabled()
    assert not view.now_line.isVisible(), "the headline already says it is stopping"

    qtbot.waitUntil(lambda: not view.is_running(), timeout=5000)
    assert view.start_button.isEnabled()
    assert not view.stop_button.isEnabled() and not view.pause_button.isEnabled()
    assert view.bar.value() == 0, "a stopped run is never drawn as a full bar"
    view.pool.waitForDone(5000)


def test_the_window_still_wires_start_to_the_moved_button(gui_mainwindow) -> None:
    """`shell.py` connects `indexing_view.start_button.clicked` by that name;
    after the move it must still be the button on screen."""
    _app, window, _store, _engine = gui_mainwindow
    page = window.indexing_view
    assert page.start_button is page.controls.start_button
    assert page.start_button.receivers(page.start_button.clicked) >= 1


# ---------------------------------------------------------------------------
# 4b. One sentence for what is happening now
# ---------------------------------------------------------------------------

def test_now_sentence_rules_without_qt() -> None:
    from app.ui.presenter.activity import READING_WORDS
    from app.ui.widgets.indexing_headline import now_sentence

    reading = SimpleNamespace(phase="reading", paused=False)
    assert now_sentence(reading) == READING_WORDS
    assert now_sentence(SimpleNamespace(phase="", paused=False)) == READING_WORDS
    # Each of these is already said by another line on the page.
    assert now_sentence(SimpleNamespace(phase="tidying", paused=False)) == ""
    assert now_sentence(SimpleNamespace(phase="reading", paused=True)) == ""
    assert now_sentence(SimpleNamespace(phase="reading", paused=False,
                                        paused_by_person=True)) == ""
    assert now_sentence(reading, stopping=True) == ""
    assert now_sentence(None) == ""


def test_the_now_line_follows_the_run_and_clears_after_it(qtbot) -> None:
    from app.ui.presenter import PHASE_WORDS

    view = _view(qtbot)
    assert not view.now_line.isVisible(), "nothing to say before a run"

    reading = IndexStats(phase=pipeline_module.PHASE_READING, walk_complete=True,
                         seen=10, indexed=4)
    view._on_progress(reading.snapshot())
    assert view.now_line.isVisible() and view.now_line.text()

    tidying = IndexStats(phase=pipeline_module.PHASE_TIDYING, walk_complete=True,
                         seen=10, indexed=10)
    view._on_progress(tidying.snapshot())
    assert view.detail.text() == PHASE_WORDS[pipeline_module.PHASE_TIDYING]
    assert not view.now_line.isVisible(), "the phase words are said once, below the bar"

    view._on_finished(tidying.snapshot())
    assert not view.now_line.isVisible()


def test_the_now_line_sits_under_the_counts(qtbot) -> None:
    view = _view(qtbot, show=False)
    layout = view.headline.parentWidget().layout()
    assert layout.indexOf(view.now_line) == layout.indexOf(view.headline) + 1


# ---------------------------------------------------------------------------
# 4c. The bar glides, never backwards, busy when the total is unknown
# ---------------------------------------------------------------------------

def _record(bar) -> list[int]:
    seen: list[int] = []
    bar.valueChanged.connect(seen.append)
    return seen


def test_the_bar_glides_between_two_values(qtbot) -> None:
    view = _view(qtbot)
    bar = view.bar
    bar.glide_to(10, 100)                  # a new total: straight there
    assert (bar.maximum(), bar.value()) == (100, 10)

    seen = _record(bar)
    bar.glide_to(60, 100)
    assert bar.gliding()
    assert bar.value() < 60, "a glide, not a jump"
    qtbot.waitUntil(lambda: bar.value() == 60 and not bar.gliding(), timeout=2000)
    assert len(seen) >= 3, f"it moved in steps, not one hop: {seen}"
    assert seen == sorted(seen), f"it went backwards on the way: {seen}"


def test_the_bar_never_goes_backwards_with_the_same_total(qtbot) -> None:
    view = _view(qtbot)
    bar = view.bar
    bar.glide_to(50, 100)
    bar.glide_to(80, 100)
    qtbot.waitUntil(lambda: not bar.gliding(), timeout=2000)
    seen = _record(bar)
    bar.glide_to(40, 100)                  # a stale tick, arriving late
    qtbot.wait(100)
    assert bar.value() == 80 and not seen


def test_a_new_total_jumps_rather_than_glides(qtbot) -> None:
    view = _view(qtbot)
    bar = view.bar
    bar.glide_to(80, 100)
    bar.glide_to(15, 200)
    assert (bar.maximum(), bar.value()) == (200, 15)
    assert not bar.gliding()


def test_an_unknown_total_is_a_busy_bar(qtbot) -> None:
    """0w's busy bar during a phase with no count, and during the walk."""
    view = _view(qtbot)
    walking = IndexStats(phase=pipeline_module.PHASE_READING, walk_complete=False,
                         seen=50, indexed=3)
    view._on_progress(walking.snapshot())
    assert (view.bar.minimum(), view.bar.maximum()) == (0, 0)
    assert not view.bar.gliding()


def test_the_busy_bar_actually_moves_under_the_theme(qtbot) -> None:
    """0w's fix for the "frozen full bar", kept: with the app's stylesheet on
    it, the busy bar's picture changes over time. Two grabs, 400 ms apart."""
    from app.ui.theme import stylesheet
    from app.ui.widgets.indexing_bar import GlidingBar

    bar = GlidingBar()
    qtbot.addWidget(bar)
    bar.setStyleSheet(stylesheet("light"))
    bar.resize(600, 26)
    bar.show()
    qtbot.waitExposed(bar)
    bar.glide_to(0, 0)
    qtbot.wait(100)
    first = bar.grab().toImage()
    qtbot.wait(400)
    assert bar.grab().toImage() != first, "the busy bar is a still picture"


def test_a_direct_set_stops_a_glide(qtbot) -> None:
    """The end of a run, a failure and a command-line run all set the bar
    exactly; a slide still under way must not overwrite them a moment later."""
    view = _view(qtbot)
    bar = view.bar
    bar.glide_to(0, 100)
    bar.glide_to(90, 100)
    assert bar.gliding()
    bar.setRange(0, 1)
    bar.setValue(0)
    assert not bar.gliding()
    qtbot.wait(350)
    assert (bar.maximum(), bar.value()) == (1, 0)


def test_the_glide_stops_when_the_page_is_hidden(qtbot) -> None:
    view = _view(qtbot)
    bar = view.bar
    bar.glide_to(0, 100)
    bar.glide_to(70, 100)
    assert bar.gliding()
    view.hide()
    assert not bar.gliding() and bar.value() == 70, "finished where it was going"

    bar.glide_to(90, 100)                  # while hidden: no timer at all
    assert not bar.gliding() and bar.value() == 90


def test_the_glide_stops_when_the_window_is_minimised(qtbot, monkeypatch) -> None:
    """Asserted through `isMinimized`, because the offscreen platform does not
    minimise a real window; the hide event a real minimise sends is covered
    by the test above. (UNCONFIRMED on Windows and macOS - see the report.)"""
    view = _view(qtbot)
    bar = view.bar
    bar.glide_to(0, 100)
    bar.glide_to(70, 100)
    monkeypatch.setattr(QWidget, "isMinimized", lambda _self: True)
    qtbot.waitUntil(lambda: not bar.gliding(), timeout=1000)
    assert bar.value() == 70
    bar.glide_to(95, 100)
    assert not bar.gliding() and bar.value() == 95


def test_the_glide_costs_next_to_nothing(qtbot) -> None:
    """A simulated run: a tick every 0.25 s (the paint throttle's rate) for
    three seconds, each moving the bar. Asserted on the work, not the clock:

    * steps per tick stay within one glide's worth (`GLIDE_S / FRAME_MS`, +2
      for timer slack) - the throttle's rate is not multiplied;
    * after the last tick, the timer stops and **no step runs while idle**;
    * the time spent in the steps is a small fraction of the run.

    The 30 s version, with paint timings, is in the 0x §4 report.
    """
    from app.ui.widgets.indexing_bar import FRAME_MS, GLIDE_S

    view = _view(qtbot)
    bar = view.bar
    bar.glide_to(0, 10_000)
    spent = [0.0]
    original = bar._step

    def timed() -> None:
        began = time.perf_counter()
        original()
        spent[0] += time.perf_counter() - began

    bar._timer.timeout.disconnect()
    bar._timer.timeout.connect(timed)

    ticks = 12
    began = time.perf_counter()
    for index in range(1, ticks + 1):
        bar.glide_to(index * 300, 10_000)
        qtbot.wait(250)
    qtbot.waitUntil(lambda: not bar.gliding(), timeout=2000)
    elapsed = time.perf_counter() - began

    per_glide = GLIDE_S * 1000 / FRAME_MS
    assert bar.steps <= ticks * (per_glide + 2), bar.steps
    assert spent[0] < 0.05 * elapsed, f"{spent[0]:.4f}s of {elapsed:.2f}s in steps"

    idle_from = bar.steps
    qtbot.wait(300)
    assert bar.steps == idle_from, "a timer ran with nothing to move"


# ---------------------------------------------------------------------------
# 4e. The log's filter and Copy
# ---------------------------------------------------------------------------

def _log_widget(qtbot):
    from app.ui.widgets.run_log import RunLog

    widget = RunLog()
    qtbot.addWidget(widget)
    widget.resize(600, 180)
    widget.show()
    return widget


def _texts(widget) -> list[str]:
    text = widget.view.toPlainText()
    return [line.split("  ", 1)[1] for line in text.split("\n")] if text else []


def _story() -> ActivityLog:
    log = ActivityLog()
    log.record("phase", "model")
    log.record("warning", "Could not read D:\\old")
    log.record("phase", "reading")
    log.record("notice", "40GB free on the index drive")
    return log


def test_log_filter_rules_without_qt() -> None:
    from app.ui.presenter.log_filter import (
        LOG_FILTER_ALL, LOG_FILTER_WARNINGS, is_warning, shown_under,
    )

    for kind in ("warning", "notice", "error"):
        assert is_warning(SimpleNamespace(kind=kind)), kind
    for kind in ("phase", "large_file", "pause", "resume", "finished", "archive"):
        assert not is_warning(SimpleNamespace(kind=kind)), kind
    entry = SimpleNamespace(kind="phase")
    assert shown_under(entry, LOG_FILTER_ALL)
    assert not shown_under(entry, LOG_FILTER_WARNINGS)
    assert shown_under(entry, "a typo"), "an unknown choice hides nothing"


def test_choosing_warnings_hides_the_rest_and_all_brings_it_back(qtbot) -> None:
    from app.ui.presenter import PHASE_WORDS
    from app.ui.presenter.activity import READING_WORDS

    widget = _log_widget(qtbot)
    log = _story()
    widget.show_activity(log.copy())
    everything = _texts(widget)
    assert len(everything) == 4

    widget.filter.setCurrentIndex(1)                     # Warnings and errors
    assert _texts(widget) == ["Could not read D:\\old", "40GB free on the index drive"]

    # New lines while filtered: only the warnings arrive...
    log.record("phase", "tidying")
    log.record("warning", "A file would not open")
    widget.show_activity(log.copy())
    assert _texts(widget)[-1] == "A file would not open"
    assert len(_texts(widget)) == 3

    # ...but nothing was thrown away: "All" shows every line, in order.
    widget.filter.setCurrentIndex(0)
    assert _texts(widget) == [
        PHASE_WORDS["model"], "Could not read D:\\old", READING_WORDS,
        "40GB free on the index drive", PHASE_WORDS["tidying"],
        "A file would not open"]


def test_the_log_stays_on_screen_when_the_filter_hides_every_line(qtbot) -> None:
    """Otherwise the filter would vanish with the box and could never be set
    back."""
    widget = _log_widget(qtbot)
    widget.filter.setCurrentIndex(1)
    log = ActivityLog()
    log.record("phase", "model")
    widget.show_activity(log)
    assert not widget.isHidden()
    assert widget.view.toPlainText() == ""
    assert widget.view.placeholderText() == "No warnings or errors in this run."


def test_copy_puts_the_visible_lines_with_their_times_on_the_clipboard(qtbot) -> None:
    from app.ui.presenter.activity import activity_line

    widget = _log_widget(qtbot)
    log = _story()
    widget.show_activity(log.copy())
    widget.filter.setCurrentIndex(1)

    QApplication.clipboard().setText("")
    qtbot.mouseClick(widget.copy_button, Qt.MouseButton.LeftButton)
    copied = QApplication.clipboard().text()

    warnings = [entry for entry in log.entries() if entry.kind in ("warning", "notice")]
    assert copied == "\n".join(activity_line(entry) for entry in warnings)
    assert all(line[:8].count(":") == 2 for line in copied.split("\n")), "each has its time"


def test_the_new_log_controls_are_labelled_for_a_screen_reader(qtbot) -> None:
    widget = _log_widget(qtbot)
    assert widget.filter.accessibleName().strip()
    assert widget.copy_button.accessibleName().strip()
    assert widget.filter.toolTip().strip() and widget.copy_button.toolTip().strip()
    assert [widget.filter.itemText(i) for i in range(widget.filter.count())] == [
        "All", "Warnings and errors"]


def test_tab_reaches_the_log_controls_before_the_buttons_below_them(qtbot) -> None:
    view = _view(qtbot, show=False)
    # The row as it reads since 3ddb128: Start, Pause, Stop, Scan, Reset.
    chain = [view.run_log.filter, view.run_log.copy_button, view.run_log.view,
             view.start_button, view.pause_button, view.stop_button,
             view.scan_button, view.reset_button]
    for first, second in zip(chain, chain[1:]):
        assert _tab_after(first) is second, (type(first).__name__,
                                             type(second).__name__)


if __name__ == "__main__":       # pragma: no cover - convenience
    raise SystemExit(pytest.main([__file__]))
