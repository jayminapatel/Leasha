r"""Order 1l: the "Move duplicates to Deleted Items" button on each Mail archives line.

Layer: L5

It counts, asks with the number, and only a yes moves - the one action in
Leasha that changes a mail archive.
"""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytestmark = pytest.mark.gui


def _app():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def test_every_archive_line_has_the_button_and_it_names_its_archive():
    _app()
    from PySide6.QtWidgets import QPushButton

    from app.ui.widgets.mail_archives_box import COL_COPIES, MailArchivesBox

    box = MailArchivesBox()
    asked: list[str] = []
    box.copies_requested.connect(asked.append)
    box.set_archives([{"path": r"D:\OutlookArchive\2011.pst", "messages": 30},
                      {"path": r"D:\OutlookArchive\2018.pst", "messages": 20}], {}, "auto")
    assert box.tree.headerItem().text(COL_COPIES) == "Duplicates"
    for row in range(box.tree.topLevelItemCount()):
        holder = box.tree.itemWidget(box.tree.topLevelItem(row), COL_COPIES)
        button = holder if isinstance(holder, QPushButton) else holder.findChild(QPushButton)
        assert "Deleted Items" in button.toolTip() and "Nothing is deleted" in button.toolTip()
        button.click()
    assert asked == [r"D:\OutlookArchive\2011.pst", r"D:\OutlookArchive\2018.pst"]


class _Window:
    def __init__(self):
        self.said: list[str] = []

    def notify(self, text, *_a, **_k):
        self.said.append(text)

    def _show_error(self, error):
        raise AssertionError(error)


def _plan(count):
    from app.extract.outlook_cleanup import CleanupPlan, Copy

    copies = [Copy(f"E{i}", "Inbox", "Quotation", 1, "K", "Inbox") for i in range(count)]
    return CleanupPlan(archive=r"D:\OutlookArchive\2018.pst", copies=copies, examined=10)


def test_nothing_moves_without_a_yes(monkeypatch):
    _app()
    from app.extract import outlook_cleanup
    from app.ui.controllers.outlook_cleanup_controller import OutlookCleanupController

    window = _Window()
    controller = OutlookCleanupController.__new__(OutlookCleanupController)
    controller._w = window
    moves: list = []
    monkeypatch.setattr(outlook_cleanup, "move_copies", lambda plan, **_k: moves.append(plan) or (0, 0))
    controller.ask = lambda path, plan: False
    controller._confirm(r"D:\OutlookArchive\2018.pst", _plan(3))
    assert moves == []


def test_no_copies_says_so_and_asks_nothing():
    _app()
    from app.ui.controllers.outlook_cleanup_controller import OutlookCleanupController

    controller = OutlookCleanupController.__new__(OutlookCleanupController)
    controller._w = window = _Window()
    controller.ask = lambda *_a: pytest.fail("asked with nothing to move")
    controller._confirm(r"D:\OutlookArchive\2018.pst", _plan(0))
    assert window.said and "Nothing was moved" in window.said[0]


def test_the_question_carries_the_number_and_says_nothing_is_deleted():
    from app.ui.presenter import copies_confirmation, copies_moved_message

    title, body = copies_confirmation(r"D:\OutlookArchive\2011.pst", 1664, ["\"Plan\" in Inbox"])
    assert "1,664" in title and "2011.pst" in title
    assert "Nothing is deleted" in body and "restore" in body
    assert "2 had changed" in copies_moved_message("x.pst", 5, 2)
