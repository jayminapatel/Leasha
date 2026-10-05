"""The icon, and an optional tray presence that must not lose the index lock.

Layer: L5

**The tray is opt-in.** An application that vanishes from the taskbar when you
did not ask it to is alarming - you close a window, it disappears, and there is
no obvious way to get it back.

**The dangerous half is quitting.** A tray icon that leaves a process holding
the single-instance mutex produces `ERR_DB_LOCKED` on the *next* launch, with
nothing on screen to blame and minutes between cause and symptom. Quit from the
tray therefore goes through the window's ordinary close path rather than hiding.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from app.ui.tray import (
    ICON_FILE,
    TRAY_ICON_FILE,
    TrayPresence,
    assets_dir,
    icon_path,
    set_app_user_model_id,
)

ROOT = Path(__file__).resolve().parents[2]
TRAY = ROOT / "app" / "ui" / "tray.py"


class FakeWindow:
    """Records what the tray asked the window to do."""

    def __init__(self):
        self.closed = False
        self.shown = False
        self.searched = False

    def close(self):
        self.closed = True

    def showNormal(self):        # noqa: N802 - Qt's naming
        self.shown = True

    # 2026-09-27: restore goes through `window_state.bring_forward` now (it
    # keeps a maximised window maximised, which `showNormal` did not), so the
    # fake answers the calls a real window answers there. A hidden window
    # that is shown still counts as "shown" - the assertions are unchanged.
    def windowState(self):       # noqa: N802
        from PyQt6.QtCore import Qt

        return Qt.WindowState.WindowNoState

    def setWindowState(self, state):   # noqa: N802
        pass

    def isVisible(self):         # noqa: N802
        return self.shown

    def show(self):
        self.shown = True

    def raise_(self):
        pass

    def activateWindow(self):    # noqa: N802
        pass

    def _focus_search(self):
        self.searched = True


# ---------------------------------------------------------------------------
# The icons
# ---------------------------------------------------------------------------

def test_both_icon_files_exist():
    assert (assets_dir() / ICON_FILE).is_file()
    assert (assets_dir() / TRAY_ICON_FILE).is_file()


def test_the_tray_uses_a_different_file_from_the_window():
    """Below 48px the navy ellipse becomes an indistinct dark mass that swamps
    the three shapes. The tray variant is the blobs alone."""
    assert ICON_FILE != TRAY_ICON_FILE


def test_a_missing_icon_is_none_rather_than_a_guess():
    """A silently substituted default would hide an incomplete build."""
    assert icon_path("does-not-exist.ico") is None


def test_app_user_model_id_is_a_noop_off_windows():
    """Off Windows, `SetCurrentProcessExplicitAppUserModelID` does not exist -
    the call must return False rather than raise."""
    import sys
    from unittest.mock import patch

    with patch.object(sys, "platform", "linux"):
        assert set_app_user_model_id() is False


def test_app_user_model_id_failure_is_cosmetic_only():
    """A broken shell32 call must never be allowed to crash startup over a
    taskbar icon - same rule as `install_window_icon` just above it."""
    import sys
    from unittest.mock import MagicMock, patch

    fake_ctypes = MagicMock()
    fake_ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID.side_effect = (
        OSError("no shell32 here")
    )
    with patch.object(sys, "platform", "win32"), \
            patch.dict("sys.modules", {"ctypes": fake_ctypes}):
        assert set_app_user_model_id() is False


def test_app_user_model_id_calls_shell32_on_windows():
    """The real fix: give the process its own taskbar identity before any
    window exists, so Windows stops keying the icon on pythonw.exe."""
    import sys
    from unittest.mock import MagicMock, patch

    fake_ctypes = MagicMock()
    with patch.object(sys, "platform", "win32"), \
            patch.dict("sys.modules", {"ctypes": fake_ctypes}):
        assert set_app_user_model_id("Leasha.Test") is True
    fake_ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID.assert_called_once_with(
        "Leasha.Test")


def test_the_icon_is_looked_for_beside_a_frozen_executable_too():
    """A packaged build puts assets beside the exe, not beside app/. Getting
    this wrong means the icon works from source and vanishes when shipped -
    which is exactly when nobody is testing it."""
    source = TRAY.read_text(encoding="utf-8")
    assert "_MEIPASS" in source
    assert "sys.argv[0]" in source


# ---------------------------------------------------------------------------
# Opt-in
# ---------------------------------------------------------------------------

def test_both_preferences_start_off():
    tray = TrayPresence(FakeWindow())
    assert tray.minimise_to_tray is False
    assert tray.close_to_tray is False


def test_nothing_is_installed_until_asked_for():
    assert TrayPresence(FakeWindow()).installed is False


def test_hiding_without_a_tray_does_not_raise():
    """Called on the close path. An exception there would turn a preference
    nobody enabled into a window that cannot be closed."""
    TrayPresence(FakeWindow()).hide()


def test_setting_status_without_a_tray_does_not_raise():
    TrayPresence(FakeWindow()).set_status("indexing")


def test_notifying_without_a_tray_does_not_raise():
    TrayPresence(FakeWindow()).notify_hidden()


# ---------------------------------------------------------------------------
# Quit must be a real quit
# ---------------------------------------------------------------------------

def test_quit_closes_the_window_rather_than_hiding_it():
    """**The bug this prevents.** A process left holding the index lock fails
    the next launch with ERR_DB_LOCKED and no visible cause."""
    window = FakeWindow()
    tray = TrayPresence(window)
    tray.close_to_tray = True

    tray.quit()

    assert window.closed is True


def test_quit_clears_close_to_tray_first():
    """Otherwise `closeEvent` sees the flag, hides instead of closing, and the
    Quit menu item silently does nothing at all."""
    tray = TrayPresence(FakeWindow())
    tray.close_to_tray = True
    tray.quit()
    assert tray.close_to_tray is False


def test_the_close_path_checks_the_flag_before_tearing_anything_down():
    r"""Close-to-tray is a *hide*: nothing is shut, and the lock stays held
    deliberately. That check has to come first or the stores close underneath a
    window that is still alive.

    **Read as statements rather than as a string.** The first version of this
    searched the source text for the literal `"indexing_view.stop()"`, and broke
    the moment `closeEvent` started timing its shutdown stages and the call
    became `stage("indexing", self.indexing_view.stop)`. Nothing about the rule
    had changed - only the spelling - which is the failure mode of a test that
    reads prose instead of code, and the same one that made
    `test_narrowing_the_tree_runs_no_subprocess` fail on its own docstring.

    So: parse `closeEvent`, take its top-level statements in order, and require
    that the one mentioning the flag comes before any that shuts something down.
    That survives any rearrangement that keeps the rule and fails any that does
    not.
    """
    import ast

    shell = (ROOT / "app" / "ui" / "shell.py").read_text(encoding="utf-8")
    tree = ast.parse(shell)
    close_event = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "closeEvent"
    )

    #: Names that only appear where something is being dismantled.
    teardown = {"shutdown", "stop", "_drain_workers", "flush_pending"}

    def names_in(statement) -> set:
        """Every attribute and identifier the statement *uses*.

        Deliberately not the unparsed text: this function's own docstring
        contains the words "shutdown" and "stop", and so does a log line saying
        what it is about to do. A test that cannot tell a call from a sentence
        about a call is a test that fails for the wrong reason.
        """
        used = set()
        for node in ast.walk(statement):
            if isinstance(node, ast.Attribute):
                used.add(node.attr)
            elif isinstance(node, ast.Name):
                used.add(node.id)
        return used

    guard_at = teardown_at = None
    for position, statement in enumerate(close_event.body):
        used = names_in(statement)
        if guard_at is None and "close_to_tray" in used:
            guard_at = position
        if teardown_at is None and (used & teardown):
            teardown_at = position

    assert guard_at is not None, "closeEvent no longer checks close_to_tray at all"
    assert teardown_at is not None, "closeEvent tears nothing down; has it moved?"
    assert guard_at < teardown_at, (
        f"the tray check is statement {guard_at} and the first teardown is "
        f"{teardown_at}: a close-to-tray hide would shut the stores under a "
        f"window that is still alive")


def test_restore_brings_the_window_back():
    window = FakeWindow()
    TrayPresence(window).restore()
    assert window.shown is True


def test_search_from_the_tray_focuses_the_box():
    """Restoring to a window you then have to click into is half an action."""
    window = FakeWindow()
    TrayPresence(window).restore_and_search()
    assert window.shown and window.searched


def test_a_window_without_a_search_box_does_not_break_the_menu():
    class Bare(FakeWindow):
        _focus_search = None

    TrayPresence(Bare()).restore_and_search()


# ---------------------------------------------------------------------------
# No tray available
# ---------------------------------------------------------------------------

def test_installing_without_a_tray_reports_false_rather_than_raising():
    """Returns False on a bare X session, some Linux desktops, and Windows with
    the notification area disabled by policy."""
    assert TrayPresence(FakeWindow()).install() in (True, False)


def test_the_window_turns_the_preferences_off_when_there_is_no_tray():
    """**Never silently.** A switch that does nothing is worse than a switch
    that is not offered."""
    shell = (ROOT / "app" / "ui" / "shell.py").read_text(encoding="utf-8")
    assert "no system tray available" in shell


def test_the_tray_holds_no_store_or_engine():
    """It forwards intentions to the window, which keeps the single answer to
    "what does closing mean". A tray that closed things itself would be a second
    shutdown path to keep in step."""
    tree = ast.parse(TRAY.read_text(encoding="utf-8"))
    imported = {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert not any("storage" in name or "search" in name for name in imported)


# --- the live status line (2026-10-05) ---------------------------------------------

def test_the_status_line_says_what_indexing_is_doing_now():
    """The owner: the line "says indexed but the count does not seem right". It
    was the last run's new files only, and never moved during a run."""
    import datetime as dt

    from app.ui.presenter.tray_words import IDLE_WORDS, tray_status

    assert tray_status("idle") == IDLE_WORDS
    assert tray_status("idle", documents=152340) == "152,340 files in the index"
    assert tray_status("running", value=1240, total=15010) == "Indexing – 1,240 of 15,010"
    assert tray_status("running") == "Indexing – finding files…"
    assert tray_status("running", value=5, total=9, paused=True) == "Paused – 5 of 9"
    assert tray_status("finished", documents=152340,
                       finished_at=dt.datetime(2026, 10, 5, 3, 10)) == (
        "Up to date – 152,340 files · last run 03:10")
    assert tray_status("finished", stopped_early=True) == (
        "Stopped part-way – click to carry on")
    assert tray_status("failed").startswith("The last run stopped with a problem")


def test_the_window_paints_the_tray_from_the_pills_data():
    from types import SimpleNamespace

    from app.ui.shell import MainWindow

    shown = []
    window = SimpleNamespace(tray=SimpleNamespace(set_status=shown.append),
                             _last_document_count=12)
    window._paint_tray = lambda *args: MainWindow._paint_tray(window, *args)
    MainWindow._paint_tray(window, "running", 3, 40, 100, False, False, "")
    assert shown[-1] == "Indexing – 40 of 100"
    MainWindow._paint_tray(window, "finished", 3, 1, 1, False, False, "")
    assert shown[-1].startswith("Up to date – 12 files · last run ")
    window._last_document_count = 15
    MainWindow._totals_for_tray(window, 15)
    assert shown[-1].startswith("Up to date – 15 files")
