"""Shared set-up for the Contacts tool dialog tests (order 1k 6g-6r).

Layer: L5

A real `ContactHub` on a real `SqliteStore`, a real `AccountStore`, an `openers.Ctx`, and a way to wait for
the dialogs' workers (rule 5: every read and write runs off the UI thread, so a test waits for the answer
to arrive rather than calling a method and looking at once). No test in this family needs the Windows
window; each is offscreen Qt.
"""

from __future__ import annotations

import os

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import (  # noqa: E402
    QAbstractButton, QAbstractSpinBox, QComboBox, QLineEdit, QListWidget, QPlainTextEdit, QTableWidget, QTabWidget,
    QTreeWidget, QWidget,
)

from app.accounts.store import AccountStore  # noqa: E402
from app.contacts.card import Card, Field  # noqa: E402
from app.contacts.hub import ContactHub  # noqa: E402
from app.storage.sqlite_store import SqliteStore  # noqa: E402
from app.ui.contacts import openers  # noqa: E402
from app.ui.contacts_tools import _common  # noqa: E402

pytestmark = pytest.mark.gui


class World:
    def __init__(self, tmp_path):
        self.store = SqliteStore(tmp_path / "k.db").connect()
        self.hub = ContactHub(self.store)
        self.accounts = AccountStore(self.store)
        self.refreshed = 0
        self.ctx = openers.Ctx(hub=self.hub, accounts=self.accounts, refresh=self._refresh)

    def _refresh(self) -> None:
        self.refreshed += 1

    def add(self, given="", family="", *, email="", phone="", company="", title="", extra=None, **kw) -> int:
        card = Card()
        for key, value in (("given", given), ("family", family), ("company", company), ("title", title)):
            if value:
                card.scalars[key] = value
        if email:
            card.fields.append(Field("email", email, "work"))
        if phone:
            card.fields.append(Field("phone", phone, "mobile"))
        for f in extra or []:
            card.fields.append(f)
        return self.hub.create(card, **kw)

    def live(self) -> dict[int, Card]:
        return dict(self.hub.all_cards())

    def close(self) -> None:
        self.store.close()


@pytest.fixture
def world(tmp_path):
    w = World(tmp_path)
    yield w
    w.close()


def settle(qtbot, timeout: int = 15000) -> None:
    """Wait until every worker a dialog started has delivered its answer."""
    qtbot.waitUntil(_common.idle, timeout=timeout)
    qtbot.wait(20)
    qtbot.waitUntil(_common.idle, timeout=timeout)


def open_dialog(qtbot, cls, *args):
    dialog = cls(*args)
    qtbot.addWidget(dialog)
    settle(qtbot)
    return dialog


def unnamed_controls(root: QWidget) -> list[str]:
    """Every control in `root` that lacks an accessible name or a tooltip."""
    kinds = (QAbstractButton, QComboBox, QLineEdit, QListWidget, QPlainTextEdit, QTableWidget, QTabWidget,
             QTreeWidget, QAbstractSpinBox)
    bad = []
    for w in root.findChildren(QWidget):
        if not isinstance(w, kinds):
            continue
        if w.objectName().startswith("qt_"):                # Qt's own internals (scroll bars, tab bar)
            continue
        if not w.accessibleName().strip():
            bad.append(f"{type(w).__name__} {w.text() if hasattr(w, 'text') and callable(w.text) else ''} has no accessible name")
        if not w.toolTip().strip():
            bad.append(f"{type(w).__name__} {w.accessibleName()!r} has no tooltip")
    return bad
