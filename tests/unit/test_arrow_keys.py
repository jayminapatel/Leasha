"""Type, then arrow: a page's box hands its arrow keys to the page's list (MS-4, 2026-10-14).

Layer: L5.
"""

from __future__ import annotations

import os

import pytest

pytest.importorskip("PySide6.QtWidgets", exc_type=ImportError)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt                                       # noqa: E402
from PySide6.QtWidgets import QApplication, QLineEdit, QListView, QListWidget, QWidget, QVBoxLayout  # noqa: E402

from app.ui.widgets.arrow_keys import forward_arrows                # noqa: E402


def _page(rows=5, view=None):
    host = QWidget()
    box = QLineEdit()
    listing = view or QListWidget()
    if isinstance(listing, QListWidget):
        listing.addItems([f"row {n}" for n in range(rows)])
    layout = QVBoxLayout(host)
    layout.addWidget(box)
    layout.addWidget(listing)
    opened = []
    listing.activated.connect(lambda index: opened.append(index.row()))
    keys = forward_arrows(box, listing)
    host.show()
    box.setFocus()
    return host, box, listing, opened, keys


def test_down_from_the_box_chooses_the_first_row_then_moves(qapp, qtbot):
    host, box, listing, _opened, _keys = _page()
    qtbot.keyClick(box, Qt.Key.Key_Down)
    assert listing.currentRow() == 0
    qtbot.keyClick(box, Qt.Key.Key_Down)
    qtbot.keyClick(box, Qt.Key.Key_Down)
    assert listing.currentRow() == 2
    qtbot.keyClick(box, Qt.Key.Key_Up)
    assert listing.currentRow() == 1
    assert not listing.hasFocus(), "the cursor stays in the box"
    host.close()


def test_enter_opens_the_row_only_after_an_arrow_moved_to_it(qapp, qtbot):
    host, box, listing, opened, _keys = _page()
    qtbot.keyClicks(box, "rep")
    qtbot.keyClick(box, Qt.Key.Key_Return)
    assert opened == [], "Enter after typing is the box's own (Code searches on it)"
    qtbot.keyClick(box, Qt.Key.Key_Down)
    qtbot.keyClick(box, Qt.Key.Key_Down)
    qtbot.keyClick(box, Qt.Key.Key_Return)
    assert opened == [1]
    qtbot.keyClicks(box, "x")
    qtbot.keyClick(box, Qt.Key.Key_Return)
    assert opened == [1], "typing again gives Enter back to the box"
    host.close()


def test_page_keys_move_by_a_page_and_an_empty_list_keeps_its_keys(qapp, qtbot):
    host, box, listing, _opened, keys = _page(rows=40)
    listing.resize(200, 120)
    qtbot.keyClick(box, Qt.Key.Key_Down)
    qtbot.keyClick(box, Qt.Key.Key_PageDown)
    assert listing.currentRow() > 1
    host.close()
    empty_host, empty_box, _l, _o, empty_keys = _page(rows=0)
    assert not empty_keys.move(Qt.Key.Key_Down)
    assert not empty_keys.armed
    empty_host.close()


def test_the_list_may_be_whichever_is_in_front(qapp, qtbot):
    host = QWidget()
    box = QLineEdit()
    first, second = QListWidget(), QListWidget()
    first.addItems(["a", "b"])
    second.addItems(["c", "d", "e"])
    front = {"view": first}
    keys = forward_arrows(box, lambda: front["view"])
    QVBoxLayout(host).addWidget(box)
    host.show()
    qtbot.keyClick(box, Qt.Key.Key_Down)
    assert first.currentRow() == 0
    front["view"] = second
    qtbot.keyClick(box, Qt.Key.Key_Down)
    assert second.currentRow() == 0 and keys.view() is second
    host.close()


def test_a_grid_moves_by_its_own_rule(qapp, qtbot):
    from PySide6.QtCore import QSize
    from PySide6.QtGui import QStandardItem, QStandardItemModel

    grid = QListView()
    grid.setViewMode(QListView.ViewMode.IconMode)
    grid.setGridSize(QSize(60, 60))
    grid.setFixedWidth(150)
    model = QStandardItemModel()
    for n in range(12):
        model.appendRow(QStandardItem(f"p{n}"))
    grid.setModel(model)
    host, box, _listing, _opened, _keys = _page(view=grid)
    qtbot.keyClick(box, Qt.Key.Key_Down)
    assert grid.currentIndex().row() == 0
    qtbot.keyClick(box, Qt.Key.Key_Down)
    assert grid.currentIndex().row() >= 1
    host.close()


def test_modified_keys_and_other_keys_stay_the_boxs(qapp, qtbot):
    host, box, listing, _opened, keys = _page()
    qtbot.keyClick(box, Qt.Key.Key_Down, Qt.KeyboardModifier.ShiftModifier)
    assert listing.currentRow() == -1 and not keys.armed
    qtbot.keyClicks(box, "ab")
    assert box.text() == "ab"
    host.close()
