r"""The run's notices carry the time they happened. Work order 0w §2c.

Layer: L5, with the L3 half in `test_activity_log.py`.

The notices were plain strings with no time, so "40GB free on the index drive"
could have been said at minute one or at hour sixty. What these pin: the time
goes **in front of** the notice and the notice's own words are untouched - on
the page, in the log and in `app.cli index`'s summary - and a notice with no
recorded time is shown exactly as it always was.
"""

from __future__ import annotations

import re
import time
from types import SimpleNamespace

import pytest

from app.index.pipeline import IndexStats
from app.ui.presenter.activity import clock_time, console_safe, timed_notices

CLOCK = re.compile(r"^(\d\d:\d\d:\d\d)  (.*)$", re.S)

NOTICE = ("2 of the folders you asked Leasha to search could not be read this "
          "run: E:\\Mail (not found). Nothing in them is in the index.")


def test_the_time_goes_in_front_and_the_words_are_untouched() -> None:
    stats = IndexStats()
    stats.add_notice(NOTICE)
    (line,) = timed_notices(stats)
    match = CLOCK.match(line)
    assert match, line
    assert match.group(1) == clock_time(stats.notice_times[0])
    assert match.group(2) == NOTICE
    assert stats.notices == [NOTICE], "the stored notice is not changed"


def test_a_notice_with_no_time_is_shown_as_it_always_was() -> None:
    """A published record from another process, or an older stats object."""
    assert timed_notices(SimpleNamespace(notices=["plain"])) == ["plain"]
    assert timed_notices(SimpleNamespace(notices=["a", "b"], notice_times=[0.0])) == [
        f"{clock_time(0.0)}  a", "b"]
    assert timed_notices(SimpleNamespace()) == []


def test_each_notice_has_its_own_time() -> None:
    stats = IndexStats()
    stats.add_notice("first")
    stats.notice_times[0] = time.mktime((2026, 9, 27, 9, 0, 0, 0, 0, -1))
    stats.add_notice("second")
    stats.notice_times[1] = time.mktime((2026, 9, 27, 17, 30, 5, 0, 0, -1))
    assert timed_notices(stats) == ["09:00:00  first", "17:30:05  second"]


def test_the_page_shows_the_time_before_each_notice(qtbot) -> None:
    pytest.importorskip("PySide6")
    from app.ui.indexing_view import IndexingView

    view = IndexingView()
    qtbot.addWidget(view)
    live = IndexStats()
    live.add_notice(NOTICE)
    live.add_notice("Nothing was found to index in D:\\Empty.")
    view._on_progress(live.snapshot())

    lines = view.notices.text().split("\n")
    assert [CLOCK.match(line).group(2) for line in lines] == live.notices
    assert not view.notices.isHidden()

    # And the same notice in the log, with the same time.
    log_lines = view.run_log.view.toPlainText().split("\n")
    assert lines[0] in log_lines


def test_the_finished_panel_keeps_the_times(qtbot) -> None:
    pytest.importorskip("PySide6")
    from app.ui.indexing_view import IndexingView

    view = IndexingView()
    qtbot.addWidget(view)
    live = IndexStats()
    live.add_notice(NOTICE)
    view._on_finished(live.snapshot())
    assert CLOCK.match(view.notices.text()).group(2) == NOTICE


def test_the_command_line_summary_prints_the_time_too() -> None:
    import inspect

    from app.cli import index as cli_index

    source = inspect.getsource(cli_index.cmd_index)
    assert "timed_notices(stats)" in source


def test_the_console_form_keeps_what_the_console_can_show() -> None:
    assert console_safe("Note  D:\\Café – ready…", "cp1252") == "Note  D:\\Café - ready..."
    assert console_safe("Note  D:\\Café", "ascii") == "Note  D:\\Caf?"
    assert console_safe("x", "not-an-encoding") == "x"
