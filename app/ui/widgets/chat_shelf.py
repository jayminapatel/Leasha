"""The context shelf: exactly the documents the conversation may look at.

Layer: L5 view

Work order 202626270611 3c. Documents the chat touches accumulate here as
chips. Keep one (pin) and it is searched first and never falls off; take one
away and it is out of scope, and stays out until you put it back; drag a
result in to add it yourself. Nothing the assistant can see is hidden - what
is on this shelf is the whole of it.
"""

from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QScrollArea, QToolButton, QWidget,
)

from app.ui.presenter.chat import SHELF_EMPTY, Shelf

__all__ = ["ShelfBar"]


class _Chip(QFrame):
    def __init__(self, item, on_open, on_pin, on_remove) -> None:
        super().__init__()
        self.setObjectName("chatChip")
        self.path = item.path
        name = QToolButton()
        name.setText(item.name)
        name.setAutoRaise(True)
        name.setToolTip(f"Open {item.name}.\n{item.path}")
        name.clicked.connect(lambda _c=False: on_open(item.path))
        pin = QToolButton()
        pin.setText("Kept" if item.pinned else "Keep")
        pin.setCheckable(True)
        pin.setChecked(item.pinned)
        pin.setAutoRaise(True)
        pin.setToolTip(
            "Keep this document in scope: follow-up questions look here first "
            "and it never drops off the shelf. Press again to let it go.")
        pin.clicked.connect(lambda _c=False: on_pin(item.path))
        drop = QToolButton()
        drop.setText("Remove")
        drop.setAutoRaise(True)
        drop.setToolTip(
            "Take this document out of scope. Questions stop looking at it "
            "until you drag it back in. The file itself is not touched.")
        drop.clicked.connect(lambda _c=False: on_remove(item.path))
        row = QHBoxLayout(self)
        row.setContentsMargins(6, 0, 2, 0)
        row.setSpacing(2)
        for part in (name, pin, drop):
            row.addWidget(part)
        self.name_button, self.pin_button, self.remove_button = name, pin, drop


class ShelfBar(QWidget):
    """A single scrolling row of chips, and a drop target for results."""

    #: The person changed what is in scope (pin, remove, add by drag).
    changed = pyqtSignal()
    open_requested = pyqtSignal(str)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("chatShelf")
        self.shelf = Shelf()
        self.setAcceptDrops(True)
        self.setAccessibleName("Documents in scope")
        self.empty = QLabel(SHELF_EMPTY)
        self.empty.setObjectName("chatShelfEmpty")
        self._row = QHBoxLayout()
        self._row.setContentsMargins(0, 0, 0, 0)
        self._row.addWidget(self.empty)
        self._row.addStretch(1)
        inner = QWidget()
        inner.setLayout(self._row)
        self._scroll = QScrollArea()
        self._scroll.setWidget(inner)
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setFixedHeight(40)
        self._scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self._scroll)

    def set_shelf(self, shelf: Shelf) -> None:
        self.shelf = shelf
        self.refresh()

    def refresh(self) -> None:
        for index in reversed(range(self._row.count())):
            item = self._row.itemAt(index)
            widget = item.widget()
            if isinstance(widget, _Chip):
                self._row.removeWidget(widget)
                widget.deleteLater()
        for position, item in enumerate(self.shelf.items):
            self._row.insertWidget(position, _Chip(
                item, self.open_requested.emit, self._pin, self._remove))
        self.empty.setVisible(not self.shelf.items)

    def chips(self) -> list[_Chip]:
        return [self._row.itemAt(i).widget() for i in range(self._row.count())
                if isinstance(self._row.itemAt(i).widget(), _Chip)]

    def add_receipt(self, receipt) -> None:
        """A document the answer touched. Not a person's choice: no `changed`."""
        if self.shelf.add_receipt(receipt):
            self.refresh()

    def _pin(self, path: str) -> None:
        self.shelf.toggle_pin(path)
        self.refresh()
        self.changed.emit()

    def _remove(self, path: str) -> None:
        self.shelf.remove(path)
        self.refresh()
        self.changed.emit()

    # -- adding a result by dragging it in -----------------------------------
    def dragEnterEvent(self, event) -> None:                     # noqa: N802 - Qt
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dragMoveEvent(self, event) -> None:                      # noqa: N802 - Qt
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:                          # noqa: N802 - Qt
        added = False
        for url in event.mimeData().urls():
            if url.isLocalFile():
                added |= self.shelf.add(url.toLocalFile(), explicit=True)
        if added:
            self.refresh()
            self.changed.emit()
        event.acceptProposedAction()
