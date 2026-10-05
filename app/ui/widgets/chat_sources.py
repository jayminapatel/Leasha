"""The Sources pane: the documents an answer stands on, as the search cards.

Layer: L5 view

Work order 202626270611 3a and 4e-2/4e-3. It is the same results list search
uses, so the row upgrades made for search arrive here for free; what this adds
is the rule that **it only ever grows**. A source is appended when the answer
first points at it and never moved afterwards, so a card somebody is reaching
for is still there when their hand arrives.

Under the list a strip shows the exact passage behind the source that is
picked (or hovered from the prose). Up/Down from the message box walk the list.

**Under that, the preview** (2026-10-04, the owner: "if it shows files it should
have the ability to preview"). The same pane Search shows beside its list: one
click on a source here, or on a result row in an answer, shows the document
itself - a message as its card and text, a file as its pages. Double-click
still opens the file in its own program, as before. The pane needs the store
to show a message (`set_store`); without one it previews files only.
"""

from __future__ import annotations

from typing import Any, Optional

from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import QLabel, QSplitter, QVBoxLayout, QWidget

from app.ui.presenter.chat import is_web_receipt, passage_html, receipt_to_result
from app.ui.result_delegate import ROLE_PAYLOAD
from app.ui.results_view import ResultsView
from app.ui.widgets.preview import PreviewPane

__all__ = ["SourcesPane"]

#: **Reworded 2026-09-20 (owner: the sources pane must visibly be the local sources).**
#: The words were "Sources" and "Sources appear here as the answer uses them."
HEADING = "Local sources"
#: What the heading becomes while a web page stands among the sources - added to, not
#: reworded: with no web source the heading is exactly `HEADING`.
HEADING_WITH_WEB = "Local sources and the web"
NOTHING_YET = "Passages from your files appear here as the answer uses them."


class SourcesPane(QWidget):
    opened = Signal(object)            # a ResultRow
    revealed = Signal(object)
    #: An `AppError` from the preview (a file that would not render).
    error = Signal(object)

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

        # 2026-10-04: the preview, under the list. Wired as `attach_preview`
        # wires Search's: open and reveal take the list's own routes, the
        # store (for a message) arrives through `set_store`. Shown from the
        # start - a pane that has to be found is a pane nobody finds.
        self.preview = PreviewPane()
        self.preview.setAccessibleName("Preview of the chosen source or result")
        self.preview.open_requested.connect(self.opened.emit)
        self.preview.reveal_requested.connect(self.revealed.emit)
        self.preview.error.connect(self.error.emit)
        self.preview.terms_provider = lambda: ()
        self.results.selected.connect(self.preview.show_row)

        listing = QWidget()
        column = QVBoxLayout(listing)
        column.setContentsMargins(0, 0, 0, 0)
        for part in (self.heading, self.empty):
            column.addWidget(part)
        column.addWidget(self.results, stretch=1)
        column.addWidget(self.passage)

        self.split = QSplitter(Qt.Orientation.Vertical)
        self.split.addWidget(listing)
        self.split.addWidget(self.preview)
        self.split.setStretchFactor(0, 2)
        self.split.setStretchFactor(1, 3)
        self.split.setChildrenCollapsible(False)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 0, 0, 0)     # off the splitter's hairline
        layout.addWidget(self.split)

    # -- the preview ------------------------------------------------------------
    def set_store(self, store: Any) -> None:
        """What the preview reads a message from. The window owns the store."""
        self.preview.store = store

    def preview_row(self, row: Any) -> None:
        """Show `row` - a result picked in an answer - without touching the list."""
        self.preview.show_row(row)

    def shutdown(self) -> None:
        self.preview.shutdown()

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
        self.preview.clear()

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
