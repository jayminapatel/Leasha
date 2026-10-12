"""Select all / none on every list, and a picture for each label of a phone or address.

Layer: L5

The owner (2026-10-12): "in all the lists - merge, changed in two places and the rest - there should be an option
to select all", and "different icons for different types of addresses and phone numbers".
"""

from __future__ import annotations

import os

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt                                    # noqa: E402
from PySide6.QtWidgets import (                                  # noqa: E402
    QListWidget, QListWidgetItem, QTableWidget, QTableWidgetItem, QTreeWidget, QTreeWidgetItem,
)

from app.ui.contacts.logic import label_icon                     # noqa: E402
from app.ui.widgets.icons import ICON_NAMES                      # noqa: E402
from app.ui.widgets.select_all import SelectBar, set_all         # noqa: E402


def test_list_ticks_and_clears_every_row(qtbot):
    lst = QListWidget()
    qtbot.addWidget(lst)
    for t in "abc":
        it = QListWidgetItem(t)
        it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
        it.setCheckState(Qt.CheckState.Unchecked)
        lst.addItem(it)
    assert set_all(lst, True) == 3
    assert all(lst.item(i).checkState() == Qt.CheckState.Checked for i in range(3))
    set_all(lst, False)
    assert all(lst.item(i).checkState() == Qt.CheckState.Unchecked for i in range(3))


def test_tree_ticks_the_children_and_the_bar_reports_once(qtbot):
    tree = QTreeWidget()
    qtbot.addWidget(tree)
    top = QTreeWidgetItem(["Ann"])
    top.setFlags(top.flags() | Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsAutoTristate)
    tree.addTopLevelItem(top)
    for t in ("x", "y"):
        c = QTreeWidgetItem([t])
        c.setFlags(c.flags() | Qt.ItemFlag.ItemIsUserCheckable)
        c.setCheckState(0, Qt.CheckState.Unchecked)
        top.addChild(c)
    bar = SelectBar(tree, "suggestions")
    qtbot.addWidget(bar)
    seen = []
    bar.changed.connect(seen.append)
    bar.all_button.click()
    assert seen == [2] and top.child(0).checkState(0) == Qt.CheckState.Checked
    bar.none_button.click()
    assert top.child(1).checkState(0) == Qt.CheckState.Unchecked


def test_table_without_ticks_selects_the_rows(qtbot):
    table = QTableWidget(3, 1)
    qtbot.addWidget(table)
    table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
    table.setSelectionMode(QTableWidget.SelectionMode.ExtendedSelection)
    for r in range(3):
        table.setItem(r, 0, QTableWidgetItem(str(r)))
    set_all(table, True)
    assert len(table.selectionModel().selectedRows()) == 3
    set_all(table, False)
    assert not table.selectionModel().selectedRows()


def test_every_label_has_its_own_picture():
    assert label_icon("phone", "Mobile") == "smartphone"
    assert label_icon("phone", "home") == "house" and label_icon("phone", "fax") == "printer"
    assert label_icon("phone", "work") == "briefcase" and label_icon("phone", "other") == "phone"
    assert label_icon("address", "home") == "house" and label_icon("address", "work") == "building-2"
    assert label_icon("address", "other") == "map-pin"
    assert label_icon("date", "birthday") == "cake"
    assert {"house", "smartphone"} <= set(ICON_NAMES)
