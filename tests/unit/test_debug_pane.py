r"""The window with no console, and the pane that replaces it.

Layer: L0 and L5

**The console was doing real work, which is why it could not just be deleted.**
`leasha.cmd` launched the window with `python.exe`, the console-subsystem
binary, so Windows attached a terminal to every session - an empty black
rectangle behind the application that nobody asked for and nobody could close
without killing Leasha with it. `pythonw.exe` removes it.

What goes with it is the running commentary. Under pythonw `sys.stderr` is
`None`, and `logger.add(None)` is not a no-op - it is a failure that takes the
whole logging setup with it, so an application launched that way would have had
no logging at all. That is the first test here, and it is the one that would
have turned "no console" into "no diagnostics on the machine that needs them".

The file log always had everything. But *"open the logs folder and find today's
file"* is not what anybody does while wondering whether the application has
hung, which is exactly when the question gets asked - hence a pane in Settings.
"""

from __future__ import annotations

import pytest


# ---------------------------------------------------------------------------
# The ring, which needs no Qt
# ---------------------------------------------------------------------------

def test_logging_survives_having_no_stderr(tmp_path, monkeypatch):
    r"""**pythonw gives a process no stderr at all.**

    `logger.add(sys.stderr, ...)` with `sys.stderr` as `None` raises, and it
    raises inside `setup_logging` - so the window would have started with no
    file log either, on the exact configuration that has no console to fall
    back to.
    """
    import sys

    from app.core.logging import setup_logging

    monkeypatch.setattr(sys, "stderr", None)

    setup_logging(tmp_path / "logs", force=True)      # must not raise

    from app.core.logging import logger

    logger.bind(component="test").info("still logging")


def test_the_ring_keeps_what_was_logged(tmp_path):
    from app.core.logging import logger, recent_lines, setup_logging

    setup_logging(tmp_path / "logs", force=True)
    logger.bind(component="test").info("a distinctive line about pumps")

    assert any("distinctive line about pumps" in line
               for line in recent_lines(50))


def test_the_ring_is_bounded(tmp_path):
    """It is in memory for the life of the window. It cannot grow forever."""
    from app.core.logging import RECENT_LIMIT, logger, recent_lines, setup_logging

    setup_logging(tmp_path / "logs", force=True)
    for index in range(RECENT_LIMIT + 200):
        logger.bind(component="test").info("line {}", index)

    assert len(recent_lines(RECENT_LIMIT * 2)) <= RECENT_LIMIT


def test_recent_lines_returns_the_newest_last(tmp_path):
    """Oldest first, so appending to a text view reads in the right order."""
    from app.core.logging import logger, recent_lines, setup_logging

    setup_logging(tmp_path / "logs", force=True)
    logger.bind(component="test").info("first-marker")
    logger.bind(component="test").info("second-marker")

    lines = recent_lines(50)
    first = max(i for i, line in enumerate(lines) if "first-marker" in line)
    second = max(i for i, line in enumerate(lines) if "second-marker" in line)

    assert first < second


def test_a_sink_that_cannot_store_never_takes_logging_down(tmp_path):
    """Losing the log is how a diagnosable problem becomes undiagnosable."""
    from app.core import logging as logging_module

    logging_module._remember(object())            # not a string; must not raise


# ---------------------------------------------------------------------------
# The pane
# ---------------------------------------------------------------------------

def _qt():
    from PyQt6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def test_the_pane_shows_the_recent_lines(tmp_path):
    pytest.importorskip("PyQt6")
    from app.core.logging import logger, setup_logging
    from app.ui.widgets.debug_pane import DebugPane

    _qt()
    setup_logging(tmp_path / "logs", force=True)
    logger.bind(component="test").info("visible-in-the-pane")

    pane = DebugPane()
    pane.refresh()

    assert "visible-in-the-pane" in pane.view.toPlainText()


def test_an_unchanged_log_does_not_redraw(tmp_path):
    r"""This runs once a second for as long as Settings is open.

    A `setPlainText` on every tick drops the selection of anybody mid-copy and
    fights the scrollbar of anybody reading.
    """
    pytest.importorskip("PyQt6")
    from app.core.logging import logger, setup_logging
    from app.ui.widgets.debug_pane import DebugPane

    _qt()
    setup_logging(tmp_path / "logs", force=True)
    logger.bind(component="test").info("one line")

    pane = DebugPane()
    pane.refresh()

    calls: list[str] = []
    pane.view.setPlainText = lambda text: calls.append(text)   # type: ignore[method-assign]
    pane.refresh()

    assert calls == [], "redrew a pane whose content had not changed"


def test_the_timer_only_runs_while_the_pane_is_visible(tmp_path):
    """Zero cost on every tab except this one."""
    pytest.importorskip("PyQt6")
    from app.ui.widgets.debug_pane import DebugPane

    _qt()
    pane = DebugPane()

    assert not pane._timer.isActive()
    pane.show()
    assert pane._timer.isActive()
    pane.hide()
    assert not pane._timer.isActive()


# ---------------------------------------------------------------------------
# The launcher
# ---------------------------------------------------------------------------

def test_the_launcher_opens_the_window_without_a_console(project_root):
    r"""`python.exe` is the console binary; `pythonw.exe` is the windowed one.

    Also checks the fallback. An installation missing pythonw must still open -
    with a console - rather than not open at all.
    """
    script = (project_root / "leasha.cmd").read_text(encoding="utf-8")
    gui = script.split(":gui", 1)[1]

    assert "pythonw.exe -m app.main" in gui
    assert "python.exe -m app.main" in gui, "no fallback if pythonw is missing"
    assert "start " in gui, "the .cmd must detach rather than hold its own console"


def test_the_command_line_still_gets_a_console(project_root):
    """`leasha stats` prints. It must not be launched windowed."""
    script = (project_root / "leasha.cmd").read_text(encoding="utf-8")
    cli = script.split(":gui", 1)[0]

    assert "python.exe -m app.cli" in cli
    assert "pythonw" not in cli
