r"""A maximised window comes back maximised (owner report, 2026-09-27).

Every path that brought Leasha to the front - the tray icon, a second launch,
a `leasha://` link, "show me all of it" from the mini search - used
`showNormal()`, which un-maximises as well as un-minimises. These pin the
replacement, `window_state.bring_forward`, and that none of those paths goes
back to `showNormal()`.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QMainWindow  # noqa: E402

from app.ui.window_state import bring_forward  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]


def _maximised(qtbot) -> QMainWindow:
    window = QMainWindow()
    qtbot.addWidget(window)
    window.showMaximized()
    qtbot.waitUntil(lambda: window.isVisible())
    assert window.windowState() & Qt.WindowState.WindowMaximized
    return window


def test_a_window_hidden_to_the_tray_comes_back_maximised(qtbot) -> None:
    window = _maximised(qtbot)
    window.hide()
    bring_forward(window)
    assert window.isVisible()
    assert window.windowState() & Qt.WindowState.WindowMaximized


def test_a_minimised_window_comes_back_maximised_not_small(qtbot) -> None:
    window = _maximised(qtbot)
    window.setWindowState(window.windowState() | Qt.WindowState.WindowMinimized)
    bring_forward(window)
    state = window.windowState()
    assert not state & Qt.WindowState.WindowMinimized
    assert state & Qt.WindowState.WindowMaximized


def test_the_old_way_really_did_lose_it(qtbot) -> None:
    """Proves the two tests above test something: `showNormal()` - what every
    path used to call - drops the maximised state."""
    window = _maximised(qtbot)
    window.hide()
    window.showNormal()
    assert not window.windowState() & Qt.WindowState.WindowMaximized


def test_the_tray_restore_keeps_it_maximised(qtbot) -> None:
    from app.ui.tray import TrayPresence as TrayController

    window = _maximised(qtbot)
    window.hide()
    tray = TrayController.__new__(TrayController)     # just the restore path
    tray._window = window
    tray.restore()
    assert window.windowState() & Qt.WindowState.WindowMaximized


@pytest.mark.parametrize("relative", ["app/ui/tray.py", "app/ui/shell.py"])
def test_no_bring_to_front_path_calls_show_normal(relative: str) -> None:
    source = (ROOT / relative).read_text(encoding="utf-8")
    code = [line for line in source.splitlines()
            if "showNormal()" in line and not line.strip().startswith("#")]
    assert code == [], f"{relative} un-maximises with showNormal(): {code}"
