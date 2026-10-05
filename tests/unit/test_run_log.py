r"""The Indexing page's live log: its words, its times, and its scrollbar.

Layer: L5. Work order 0w §2b, with the §2e tests that ride on it.

**Watch the Indexing page and read, line by line and with times, what it is
doing right now** - the order's acceptance sentence. What these pin:

* every entry becomes a sentence with `HH:MM:SS` in front, in the presenter;
* every phase the pipeline can announce has a line, including reading;
* the widget appends only what is new, newest at the bottom, capped;
* a new run clears the old run's lines, and a tick with none adds nothing;
* at the bottom it follows the run; scrolled up it keeps the reader's line
  still, even as old lines fall off the top; back at the bottom it follows;
* the page paints it within the existing 0.25 s throttle, and a dropped tick
  loses no lines;
* `indexing_view.py` did not grow to get it.
"""

from __future__ import annotations

import re
import time
from types import SimpleNamespace

import pytest

from app.index import pipeline as pipeline_module
from app.index.activity import ActivityEntry, ActivityLog
from app.index.pipeline import IndexStats
from app.ui.presenter.activity import (
    LOG_LINES_SHOWN,
    READING_WORDS,
    activity_line,
    activity_lines,
    activity_text,
    clock_time,
    console_safe,
)
from app.ui.presenter.indexing import PHASE_WORDS

CLOCK = re.compile(r"^\d\d:\d\d:\d\d  ")


def _entry(kind: str, text: str = "", **extra) -> ActivityEntry:
    return ActivityEntry(seq=1, at=extra.pop("at", time.time()), kind=kind,
                         text=text, **extra)


# ---------------------------------------------------------------------------
# The presenter
# ---------------------------------------------------------------------------


def test_the_clock_is_local_hours_minutes_seconds() -> None:
    at = time.mktime((2026, 9, 27, 14, 2, 7, 0, 0, -1))
    assert clock_time(at) == "14:02:07"
    assert clock_time(None) == ""
    assert clock_time("nonsense") == ""


def test_every_line_starts_with_the_time() -> None:
    at = time.mktime((2026, 9, 27, 9, 5, 0, 0, 0, -1))
    line = activity_line(_entry("notice", "40GB free on the index drive.", at=at))
    assert line == "09:05:00  40GB free on the index drive."


def test_every_phase_has_a_line_and_reuses_the_detail_words() -> None:
    phases = [value for name, value in vars(pipeline_module).items()
              if name.startswith("PHASE_") and isinstance(value, str)]
    for phase in phases:
        words = activity_text(_entry("phase", phase))
        assert words and words != phase, f"no words for {phase!r}"
        if phase in PHASE_WORDS:
            assert words == PHASE_WORDS[phase], "the same words as the detail line"
    assert activity_text(_entry("phase", pipeline_module.PHASE_READING)) == READING_WORDS


def test_a_large_file_is_named_with_its_size() -> None:
    line = activity_text(_entry("large_file", "Mail 2019.pst",
                                size=3 * 1024 ** 3, detail="mail"))
    assert line == "Reading a large mail archive: Mail 2019.pst (3.0 GB)"
    assert activity_text(_entry("large_file", "talk.mp4", size=40 * 1024 ** 2,
                                detail="recording")).startswith(
        "Reading a large video or recording: talk.mp4")
    assert activity_text(_entry("large_file", "x.bin", detail="")) == (
        "Reading a large file: x.bin")


def test_pauses_say_whose_they_are_and_why() -> None:
    assert activity_text(_entry("pause", "Paused at your request. Nothing is lost.",
                                detail="manual")) == "Paused at your request."
    assert activity_text(_entry(
        "pause", "On battery. Indexing resumes on mains power.", detail="machine")) == (
        "Paused - On battery. Indexing resumes on mains power.")
    assert activity_text(_entry("pause", "", detail="machine")) == (
        "Paused to stay out of the way.")
    assert activity_text(_entry("resume")) == "Carrying on."


def test_the_end_of_a_run_says_how_it_ended() -> None:
    assert activity_text(_entry("finished")) == "Finished."
    assert activity_text(_entry("finished", "stopped")).startswith("Stopped.")
    assert activity_text(_entry("stopping")).startswith("Stopping after the current file.")


def test_warnings_and_notices_are_their_own_words() -> None:
    said = "Only part of 'C:\\Mail\\a.pst' could be read: 2 folders were damaged."
    assert activity_text(_entry("warning", said)) == said
    assert activity_text(_entry("notice", said)) == said


def test_an_unknown_kind_is_heard_not_dropped() -> None:
    assert activity_text(_entry("something_new", "a newer pipeline")) == "a newer pipeline"


def test_lines_keep_their_order() -> None:
    log = ActivityLog()
    for text in ("one", "two", "three"):
        log.record("notice", text)
    assert [line.split("  ", 1)[1] for line in activity_lines(log.entries())] == [
        "one", "two", "three"]
    assert activity_lines(None) == []


def test_console_lines_are_ascii() -> None:
    assert console_safe("Getting the search model ready…") == (
        "Getting the search model ready...")
    assert console_safe("a – b — “c” ‘d’") == "a - b - \"c\" 'd'"
    assert console_safe("Café.pst").isascii()


def test_the_presenter_imports_no_qt() -> None:
    import ast
    from pathlib import Path

    from app.ui.presenter import activity as module

    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    names = {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import)
             for alias in node.names}
    names |= {node.module or "" for node in ast.walk(tree)
              if isinstance(node, ast.ImportFrom)}
    assert not any(name.startswith("PyQt") for name in names)


# ---------------------------------------------------------------------------
# The widget
# ---------------------------------------------------------------------------


def _widget(qtbot):
    pytest.importorskip("PySide6")
    from app.ui.widgets.run_log import RunLog

    widget = RunLog()
    qtbot.addWidget(widget)
    widget.resize(500, 150)
    widget.show()
    return widget


def _lines(widget) -> list[str]:
    text = widget.view.toPlainText()
    return text.split("\n") if text else []


def test_it_is_hidden_until_there_is_something_to_say(qtbot) -> None:
    pytest.importorskip("PySide6")
    from app.ui.widgets.run_log import RunLog

    widget = RunLog()
    qtbot.addWidget(widget)
    widget.show_activity(ActivityLog())
    widget.show_activity(None)
    assert widget.isHidden() and not _lines(widget)
    log = ActivityLog()
    log.record("phase", "model")
    widget.show_activity(log)
    assert not widget.isHidden()
    assert len(_lines(widget)) == 1


def test_it_appends_only_whats_new_newest_at_the_bottom(qtbot) -> None:
    widget = _widget(qtbot)
    log = ActivityLog()
    log.record("phase", "model")
    widget.show_activity(log.copy())
    log.record("phase", "reading")
    log.record("large_file", "big.pst", size=30 * 1024 ** 2, detail="mail")

    # Append-only: a full rewrite would lose a reader's selection.
    widget.view.setPlainText = None                       # type: ignore[assignment]
    widget.show_activity(log.copy())
    widget.show_activity(log.copy())                      # nothing new: no-op

    lines = _lines(widget)
    assert len(lines) == 3
    assert all(CLOCK.match(line) for line in lines)
    assert lines[0].endswith(PHASE_WORDS["model"])
    assert lines[-1].split("  ", 1)[1].startswith("Reading a large mail archive: big.pst")


def test_a_new_run_starts_a_clean_log(qtbot) -> None:
    widget = _widget(qtbot)
    first = ActivityLog()
    first.record("notice", "first run")
    widget.show_activity(first)
    second = ActivityLog()
    second.record("notice", "second run")
    widget.show_activity(second)
    assert [line.split("  ", 1)[1] for line in _lines(widget)] == ["second run"]


def test_lines_are_capped(qtbot) -> None:
    widget = _widget(qtbot)
    log = ActivityLog(limit=LOG_LINES_SHOWN * 3)
    for index in range(LOG_LINES_SHOWN + 40):
        log.record("notice", f"line {index}")
        if index % 25 == 0:
            widget.show_activity(log.copy())
    widget.show_activity(log.copy())
    lines = _lines(widget)
    assert len(lines) == LOG_LINES_SHOWN
    assert lines[-1].endswith(f"line {LOG_LINES_SHOWN + 39}")


def _fill(widget, log: ActivityLog, count: int, start: int = 0) -> None:
    for index in range(start, start + count):
        log.record("notice", f"line {index}")
    widget.show_activity(log.copy())


def test_it_follows_the_run_at_the_bottom(qtbot) -> None:
    widget = _widget(qtbot)
    log = ActivityLog(limit=10_000)
    _fill(widget, log, 40)
    bar = widget.view.verticalScrollBar()
    assert bar.maximum() > 0, "the test needs a log taller than its box"
    assert bar.value() == bar.maximum()
    _fill(widget, log, 5, start=40)
    assert bar.value() == bar.maximum(), "new lines keep a watching reader at the end"


def test_it_keeps_the_readers_place_when_they_scroll_up(qtbot) -> None:
    widget = _widget(qtbot)
    log = ActivityLog(limit=10_000)
    _fill(widget, log, 40)
    bar = widget.view.verticalScrollBar()
    bar.setValue(10)
    reading = widget.view.firstVisibleBlock().text()

    _fill(widget, log, 5, start=40)
    assert bar.value() == 10
    assert widget.view.firstVisibleBlock().text() == reading


def test_the_readers_line_stays_put_as_old_lines_fall_off_the_top(qtbot) -> None:
    widget = _widget(qtbot)
    log = ActivityLog(limit=10_000)
    _fill(widget, log, LOG_LINES_SHOWN)                  # full to the cap
    bar = widget.view.verticalScrollBar()
    bar.setValue(100)
    reading = widget.view.firstVisibleBlock().text()

    _fill(widget, log, 30, start=LOG_LINES_SHOWN)         # thirty fall off the top
    assert len(_lines(widget)) == LOG_LINES_SHOWN
    assert bar.value() == 70
    assert widget.view.firstVisibleBlock().text() == reading


def test_it_follows_again_once_the_reader_is_back_at_the_bottom(qtbot) -> None:
    widget = _widget(qtbot)
    log = ActivityLog(limit=10_000)
    _fill(widget, log, 40)
    bar = widget.view.verticalScrollBar()
    bar.setValue(0)
    _fill(widget, log, 3, start=40)
    assert bar.value() == 0

    bar.setValue(bar.maximum())
    _fill(widget, log, 3, start=43)
    assert bar.value() == bar.maximum()
    assert widget.view.toPlainText().endswith("line 45")


def test_a_broken_log_never_raises(qtbot) -> None:
    widget = _widget(qtbot)
    widget.show_activity(SimpleNamespace(run=1, since=lambda _seq: 1 / 0))


# ---------------------------------------------------------------------------
# The page
# ---------------------------------------------------------------------------


def _page(qtbot, monkeypatch):
    pytest.importorskip("PySide6")
    from app.ui import indexing_view
    from app.ui.indexing_view import IndexingView

    now = [1_000.0]
    monkeypatch.setattr(indexing_view, "time",
                        SimpleNamespace(monotonic=lambda: now[0]))
    view = IndexingView()
    qtbot.addWidget(view)
    return view, now


def test_the_page_has_the_log_on_its_status_shelf(qtbot, monkeypatch) -> None:
    view, _now = _page(qtbot, monkeypatch)
    from app.ui.widgets.run_log import RunLog

    assert isinstance(view.run_log, RunLog)
    shelf = view.notices.parentWidget()
    assert view.run_log.parentWidget() is shelf, "beside the notices, on Status"


def test_repaints_stay_within_the_throttle_and_drop_no_lines(qtbot, monkeypatch) -> None:
    """Forty ticks in a second, each with a new line: at most a paint per
    quarter-second reaches the log, and every line is there at the end."""
    view, now = _page(qtbot, monkeypatch)
    appends: list[str] = []
    original = view.run_log.view.appendPlainText

    def counting(text: str) -> None:
        appends.append(text)
        original(text)

    view.run_log.view.appendPlainText = counting           # type: ignore[method-assign]

    live = IndexStats(phase=pipeline_module.PHASE_READING, walk_complete=True,
                      seen=100)
    start = now[0]
    for index in range(40):
        now[0] = start + index * 0.025
        live.activity.record("warning", f"warning {index}")
        live.indexed = index
        view._on_progress(live.snapshot())

    elapsed = now[0] - start
    assert len(appends) <= int(elapsed / 0.25) + 1
    assert len(appends) >= 2, "the log moved with the run, not only at the end"

    # The finished handler always paints the last state - the dropped ticks'
    # lines arrive with it rather than being lost.
    view._on_finished(live.snapshot())
    texts = [line.split("  ", 1)[1] for line in _lines(view.run_log)]
    assert texts == [f"warning {index}" for index in range(40)]


def test_the_view_did_not_grow() -> None:
    """`indexing_view.py` is over its 250-line guard; §2b may not add to it."""
    from pathlib import Path

    source = Path(__file__).resolve().parents[2] / "app" / "ui" / "indexing_view.py"
    counted = [line for line in source.read_text(encoding="utf-8").splitlines()
               if line.strip() and not line.strip().startswith("#")]
    assert len(counted) <= 299, f"indexing_view.py grew to {len(counted)} counted lines"
