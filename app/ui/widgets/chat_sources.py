"""The Sources pane: the documents an answer stands on, as the search cards.

Layer: L5 view

Work order 202626270611 3a and 4e-2/4e-3. It is the same results list search
uses, so the row upgrades made for search arrive here for free; what this adds
is the rule that **it only ever grows**. A source is appended when the answer
first points at it and never moved afterwards, so a card somebody is reaching
for is still there when their hand arrives.

Under the list a strip shows the exact passage behind the source that is
picked (or hovered from the prose). Up/Down from the message box walk the list.
"""

from __future__ import annotations

from typing import Any, Optional

from PyQt6.QtCore import QEvent, Qt, pyqtSignal
from PyQt6.QtGui import QKeyEvent
from PyQt6.QtWidgets import QLabel, QVBoxLayout, QWidget

from app.ui.presenter.chat import is_web_receipt, passage_html, receipt_to_result
from app.ui.result_delegate import ROLE_PAYLOAD
from app.ui.results_view import ResultsView

__all__ = ["SourcesPane"]

#: **Reworded 2026-09-20 (owner: the sources pane must visibly be the local sources).**
#: The words were "Sources" and "Sources appear here as the answer uses them."
HEADING = "Local sources"
#: What the heading becomes while a web page stands among the sources - added to, not
#: reworded: with no web source the heading is exactly `HEADING`.
HEADING_WITH_WEB = "Local sources and the web"
NOTHING_YET = "Passages from your files appear here as the answer uses them."


class SourcesPane(QWidget):
    opened = pyqtSignal(object)            # a ResultRow
    revealed = pyqtSignal(object)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("chatSources")
        self._by_number: dict[int, Any] = {}
        self._order: list[int] = []
        self._hovering = False

        self.heading = QLabel(HEADING)
        self.heading.setObjectName("chatSourcesHeading")
        self.empty = QLabel(NOTHING_YET)
        self.empty.setObjectName("chatSourcesEmpty")
        self.empty.setWordWrap(True)
        self.results = ResultsView()
        self.results.setAccessibleName("Sources for this answer")
        self.results.opened.connect(self.opened.emit)
        self.results.reveal_requested.connect(self.revealed.emit)
        self.results.selected.connect(self._selected)
        self.passage = QLabel("")
        self.passage.setObjectName("chatPassage")
        self.passage.setWordWrap(True)
        self.passage.setTextFormat(Qt.TextFormat.RichText)
        self.passage.setAccessibleName("The passage behind the chosen source")
        self.passage.setVisible(False)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 0, 0, 0)     # off the splitter's hairline
        for part in (self.heading, self.empty):
            layout.addWidget(part)
        layout.addWidget(self.results, stretch=1)
        layout.addWidget(self.passage)

    # -- filling: append only -------------------------------------------------
    def numbers(self) -> list[int]:
        return list(self._order)

    def add(self, number: int, receipt: Any) -> bool:
        """Append the source with this reader-facing number. Never reorders."""
        if number in self._by_number:
            return False
        self._by_number[number] = receipt
        self._order.append(number)
        self.empty.setVisible(False)
        if is_web_receipt(receipt):
            self.heading.setText(HEADING_WITH_WEB)
        self.results.append_results([receipt_to_result(receipt, len(self._order))], [])
        return True

    def clear(self) -> None:
        self._by_number.clear()
        self._order.clear()
        self.results.clear()
        self.heading.setText(HEADING)
        self.passage.setVisible(False)
        self.empty.setVisible(True)

    def receipt(self, number: int) -> Any:
        return self._by_number.get(number)

    # -- the passage strip -----------------------------------------------------
    def show_passage(self, number: int) -> None:
        receipt = self._by_number.get(number)
        self.passage.setVisible(receipt is not None)
        if receipt is not None:
            self.passage.setText(passage_html(number, receipt))

    def hover_number(self, number: int) -> None:
        """The pointer is over a raised number in the prose (0 = it left)."""
        self._hovering = bool(number)
        if number:
            self.show_passage(number)
        else:
            self._selected(self.results.current_row())

    def _selected(self, row: Any) -> None:
        if self._hovering:
            return
        if row is None:
            self.passage.setVisible(False)
            return
        for number in self._order:
            other = self._by_number[number]
            if getattr(other, "path", None) == getattr(row, "path", ""):
                self.show_passage(number)
                return
        self.passage.setVisible(False)

    # -- choosing a source ------------------------------------------------------
    def select_number(self, number: int) -> bool:
        """Pick the card for source `number` and show its passage."""
        receipt = self._by_number.get(number)
        if receipt is None:
            return False
        model = self.results._model
        for row in range(model.rowCount()):
            payload = model.index(row, 0).data(ROLE_PAYLOAD)
            if getattr(payload, "path", None) == getattr(receipt, "path", ""):
                self.results._list.setCurrentIndex(model.index(row, 0))
                break
        self.show_passage(number)
        return True

    def walk(self, step: int) -> None:
        """Up/Down from the message box: the list moves, focus does not."""
        key = Qt.Key.Key_Down if step > 0 else Qt.Key.Key_Up
        event = QKeyEvent(QEvent.Type.KeyPress, key, Qt.KeyboardModifier.NoModifier)
        self.results.forward_key(event)

    def open_selected(self) -> bool:
        return self.results.open_current()

    def deselect(self) -> None:
        chosen = self.results._list.selectionModel()
        chosen.clearSelection()
        chosen.clearCurrentIndex()
        self.passage.setVisible(False)
