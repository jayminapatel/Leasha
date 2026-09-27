r"""Order 0x section 9 - the UI review, one scenario per improvement.

Every surface of the real window was grabbed to PNG offscreen on 2026-09-27,
light and dark, at 1100x760 and at 760x560, and looked at. Each improvement
that came out of that review is pinned here by a scenario that drives the real
`MainWindow` (`tests/unit/conftest.py`'s `gui_mainwindow`) with real key
presses and clicks, per `docs/WORKORDER-CONVENTIONS.md` section 5b - not by
calling a slot or asserting that one function mentions another.

Each test says what the picture showed before the fix, so whoever reads a
failure knows what a person would be looking at.
"""

from __future__ import annotations

import os

import pytest

pytest.importorskip("PyQt6")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import Qt  # noqa: E402

from tests.unit.conftest import gui_pump  # noqa: E402

pytestmark = pytest.mark.gui

KEY = Qt.Key


@pytest.fixture(scope="module", autouse=True)
def _leave_no_window_showing(gui_mainwindow):
    """These scenarios `show()` the window (sizes and focus need a real one)
    and never close it - closing a `MainWindow` mid-process is what crashes.
    A window left showing takes the clicks `QTest` aims at the next module's
    hidden one, so it is hidden again afterwards."""
    yield
    gui_mainwindow[1].hide()


def _front(app, window, qtbot, width: int, height: int) -> None:
    """Show the window at a size, and let the layouts settle."""
    window.resize(width, height)
    window.show()
    qtbot.waitExposed(window)
    window.activateWindow()
    gui_pump(app, 20)


def _rail_labels_fit(window) -> list[str]:
    """Each rail button whose label is showing but is squeezed below the height
    that label needs. Empty means nothing is cut off."""
    squeezed = []
    for button in window.rail._buttons.values():
        showing_label = button.toolButtonStyle() == Qt.ToolButtonStyle.ToolButtonTextUnderIcon
        if showing_label and button.height() < button.sizeHint().height():
            squeezed.append(f"{button.text()}: {button.height()} < {button.sizeHint().height()}")
    return squeezed


def test_a_short_window_shows_rail_icons_rather_than_labels_cut_in_half(gui_mainwindow, qtbot):
    r"""**Before:** at 560 pixels tall every rail label was cut in half -
    "Searcn", "Files" with its lower half missing - because the rail measured
    its own needs while its buttons were still hidden, decided it needed 114
    pixels, and never switched to icons. Grab: `before-760x560/*/search-home.png`.

    Now: short, it shows icons alone (the words stay in each tooltip and
    accessible name); tall again, the labels come back. The arrow keys still
    walk the rail in both forms."""
    app, window, *_ = gui_mainwindow
    rail = window.rail

    _front(app, window, qtbot, 760, 560)
    # The real window is on screen before Mail, Code, Chat, Indexing and
    # Settings are added - each `insertTab` re-lays the rail out while it is
    # showing. This fixture builds every page before it shows the window, so
    # re-lay it out once here, the same call a late `insertTab` makes, from the
    # state the real window is in at that moment: labels showing, because when
    # it was first measured it had only four pages and they fitted.
    rail._set_compact(False)
    rail._relayout()
    gui_pump(app, 20)
    assert _rail_labels_fit(window) == [], "a label is showing but cut off"
    assert all(b.toolButtonStyle() == Qt.ToolButtonStyle.ToolButtonIconOnly
               for b in rail._buttons.values()), "560 tall is too short for labels"
    assert all(b.accessibleName() or b.text() for b in rail._buttons.values())

    # The rail still works from the keyboard while it is icons only.
    rail.column.setFocus()
    before = rail.currentIndex()
    qtbot.keyClick(rail.column, KEY.Key_Down)
    gui_pump(app, 4)
    assert rail.currentIndex() != before
    qtbot.keyClick(rail.column, KEY.Key_Up)
    gui_pump(app, 4)
    assert rail.currentIndex() == before

    _front(app, window, qtbot, 1100, 760)
    assert _rail_labels_fit(window) == []
    assert all(b.toolButtonStyle() == Qt.ToolButtonStyle.ToolButtonTextUnderIcon
               for b in rail._buttons.values()), "760 tall has room for every label"
