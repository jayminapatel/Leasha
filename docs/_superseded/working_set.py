r"""The panel results are gathered into. Workspace §3c.

Layer: L5 — thin, like every widget here. What is in the set and what order it
is in is `ui/pinned.py`; this draws a list and four buttons.

**Cleared explicitly, never by a new search.** That is the whole difference
between a working set and a results list, and it is a property of *not*
wiring something rather than of writing something — so a test asserts it.
"""

from __future__ import annotations

from typing import Any, Optional

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import (
    QGroupBox, QHBoxLayout, QLabel, QListWidget, QPushButton, QVBoxLayout,
    QWidget,
)

from app.ui import pinned

__all__ = ["WorkingSet"]


class WorkingSet(QGroupBox):
    """Documents gathered across several searches, and what to do with them."""

    #: `{key: value}` to store — one `index_state` row. §3c: it survives.
    remember = pyqtSignal(dict)
    #: Open every one of these paths.
    open_all = pyqtSignal(list)
    #: The set changed, so a results list can retick its buttons.
    changed = pyqtSignal(tuple)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__("Gathered documents", parent)
        self._pins: tuple = ()

        self.list = QListWidget()
        self.list.setAccessibleName("Gathered documents")
        self.list.setSelectionMode(
            QListWidget.SelectionMode.ExtendedSelection)
        # §3b again: what is gathered can be dragged out as a group, which is
        # the point of gathering it.
        self.list.setDragEnabled(True)

        self.summary = QLabel("")
        self.summary.setObjectName("resultMeta")

        self.open_button = self._button(
            "Open them all", "Opens every document in this list.",
            self._open_all)
        self.copy_button = self._button(
            "Copy the paths", "Copies every path here to the clipboard, one "
            "per line.", self._copy)
        self.remove_button = self._button(
            "Remove", "Takes the selected documents out of this list. The "
            "files themselves are not touched.", self._remove)
        self.clear_button = self._button(
            "Clear the list", "Empties this list. Nothing else changes, and "
            "no file is touched.", self.clear)

        buttons = QHBoxLayout()
        for button in (self.open_button, self.copy_button,
                       self.remove_button, self.clear_button):
            buttons.addWidget(button)
        buttons.addStretch(1)

        layout = QVBoxLayout(self)
        layout.addWidget(self.summary)
        layout.addWidget(self.list, stretch=1)
        layout.addLayout(buttons)
        self._redraw()

    def _button(self, label: str, tip: str, on_click: Any) -> QPushButton:
        button = QPushButton(label)
        button.setToolTip(tip)
        button.clicked.connect(lambda _checked=False: on_click())
        return button

    # -- what is in it --------------------------------------------------------

    @property
    def pins(self) -> tuple:
        return self._pins

    @property
    def paths(self) -> tuple:
        return pinned.paths(self._pins)

    def restore(self, state: Any) -> None:
        """Put back what was gathered last time. Never raises."""
        try:
            self._pins = pinned.decode((state or {}).get(pinned.PINNED_KEY))
        except Exception:                        # noqa: BLE001 - a list
            self._pins = ()
        self._redraw(remember=False)

    def pin(self, row: Any) -> None:
        """Gather one result. Already-there is not an error — see `pinned`."""
        self._pins = pinned.add(self._pins, row)
        self._redraw()

    def clear(self) -> None:
        """**Explicitly, and only explicitly.** See the module docstring."""
        self._pins = ()
        self._redraw()

    def _remove(self) -> None:
        for item in self.list.selectedItems():
            row = self.list.row(item)
            if 0 <= row < len(self._pins):
                self._pins = pinned.remove(self._pins, self._pins[row].path)
        self._redraw()

    def _redraw(self, *, remember: bool = True) -> None:
        self.list.clear()
        for pin in self._pins:
            self.list.addItem(pin.label)
            entry = self.list.item(self.list.count() - 1)
            if entry is not None:
                entry.setToolTip(pin.path)

        self.summary.setText(pinned.summary(self._pins))
        for button in (self.open_button, self.copy_button,
                       self.remove_button, self.clear_button):
            button.setEnabled(bool(self._pins))
        self.changed.emit(self._pins)
        if remember:
            self.remember.emit({pinned.PINNED_KEY: pinned.encode(self._pins)})

    # -- acting together ------------------------------------------------------

    def _open_all(self) -> None:
        found = list(self.paths)
        if found:
            self.open_all.emit(found)

    def _copy(self) -> None:
        r"""Every path, one per line. **Never raises.**

        One per line because that is what pastes usefully into a message, a
        spreadsheet column and a command line alike - and because a path may
        contain any separator anybody would otherwise reach for.
        """
        try:
            from PyQt6.QtWidgets import QApplication

            clipboard = QApplication.clipboard()
            if clipboard is not None:
                clipboard.setText("\n".join(self.paths))
        except Exception:                        # noqa: BLE001 - a copy
            return
