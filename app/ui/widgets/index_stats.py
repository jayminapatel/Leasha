"""What is in the index, as a panel that always says something.

Layer: L5

The Indexing tab carried one line - documents and chunks - and showed **nothing
at all** when the store read failed, because the handler was `except: return`.
Reported as "the index page is blank", and that is the worst possible answer to
"is my index working": a blank page is indistinguishable from an empty index, a
broken one, and a bug in the page itself.

So this panel has one rule: **every outcome produces rows, including failure.**
Unreadable store, empty index, healthy index, half-embedded index - each says
which it is, in the same shape.

It also answers "is the index where I configured it", which was otherwise a
question needing the command line. That question was asked, and the answer
turned out to matter.

The decisions are in `presenter.index_summary`, which takes plain mappings and
is tested without a database. This arranges labels.
"""

from __future__ import annotations

from typing import Optional, Sequence

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QGridLayout, QGroupBox, QLabel, QWidget

from app.ui.presenter import StatRow

__all__ = ["IndexStats"]


class IndexStats(QGroupBox):
    """A label grid, rebuilt from a list of `StatRow`."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__("This index", parent)
        self._grid = QGridLayout(self)
        self._grid.setColumnStretch(1, 1)
        self._grid.setVerticalSpacing(2)
        self.show_rows([])

    def show_rows(self, rows: Sequence[StatRow]) -> None:
        """Replace the contents. Cheap enough to call on every tab switch."""
        self._clear()
        if not rows:
            self._grid.addWidget(QLabel("Reading…"), 0, 0)
            return

        for index, row in enumerate(rows):
            label = QLabel(row.label)
            label.setObjectName("statLabel")

            value = QLabel(row.value)
            value.setObjectName("statWarn" if row.warn else "statValue")
            value.setWordWrap(True)
            # Selectable because these are the numbers somebody copies into a
            # bug report, and a value you cannot select is a value you retype
            # wrongly.
            value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)

            self._grid.addWidget(label, index * 2, 0, Qt.AlignmentFlag.AlignTop)
            self._grid.addWidget(value, index * 2, 1)

            if row.note:
                note = QLabel(row.note)
                note.setObjectName("statNote")
                note.setWordWrap(True)
                self._grid.addWidget(note, index * 2 + 1, 1)

    def _clear(self) -> None:
        """Take every widget out and schedule it for deletion.

        `deleteLater` rather than immediate destruction: this can be called from
        a signal handler while Qt still holds references to what is being
        removed, and deleting under Qt's feet is a crash rather than an error.
        """
        while self._grid.count():
            item = self._grid.takeAt(0)
            widget = item.widget() if item is not None else None
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
