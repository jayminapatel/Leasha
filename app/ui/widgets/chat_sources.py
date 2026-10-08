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

**2026-10-08, the owner: the preview is a tab of its own, not a strip under the
list.** The list and the preview are two pages of one side panel, one showing at
a time (`show_page`); the strip of tabs that picks between them, and puts the
panel away, is `chat_side_panel.SideTabs`, laid out by the view. One click on a
source in the list asks for the Preview page (`preview_wanted`); the keyboard
walking the list from the message box does not, so the list stays in sight.
The passage strip sits under whichever page shows: it is the receipt for the
number somebody just pointed at, wanted on either.

A message is drawn with its subject, sender and sent date, never its entry id:
the turn carries the mail metadata (`ChatTurn.details`) and `set_details` hands
it to the list, which reads it exactly as the Search tab's list does.
"""

from __future__ import annotations

from typing import Any, Optional

from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

from app.ui.presenter.chat import (
    PREVIEW_TAB, SOURCES_TAB, is_web_receipt, passage_html, receipt_to_result,
)
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
    #: 2026-10-08: a source in the list was clicked - show it on the Preview page.
    preview_wanted = Signal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("chatSources")
        self._by_number: dict[int, Any] = {}
        self._order: list[int] = []
        self._hovering = False
        #: file_id -> mail metadata for the messages among the sources (2026-10-08).
        self._details: dict[int, Any] = {}
        self._page = SOURCES_TAB

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
        # A click is a person choosing a source; arrowing from the message box is
        # not (`walk`), so only the click asks for the Preview page.
        self.results._list.clicked.connect(lambda _index: self.preview_wanted.emit())

        self.list_page = QWidget()
        self.list_page.setObjectName("chatSourcesPage")
        column = QVBoxLayout(self.list_page)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(6)
        for part in (self.heading, self.empty):
            column.addWidget(part)
        column.addWidget(self.results, stretch=1)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 4, 8)     # off the splitter's hairline
        layout.setSpacing(8)
        layout.addWidget(self.list_page, stretch=1)
        layout.addWidget(self.preview, stretch=1)
        layout.addWidget(self.passage)
        self.show_page(SOURCES_TAB)

    # -- the two pages ------------------------------------------------------------
    def show_page(self, name: str) -> None:
        """Show the Sources list or the Preview, never both; "" shows neither (the
        panel is put away). The passage strip stays with whichever shows."""
        self._page = name if name in (SOURCES_TAB, PREVIEW_TAB) else ""
        self.list_page.setVisible(self._page == SOURCES_TAB)
        self.preview.setVisible(self._page == PREVIEW_TAB)

    def page(self) -> str:
        return self._page

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
        self._draw()
        return True

    def set_details(self, details: Optional[dict]) -> None:
        """Mail metadata for the messages among the sources (`ChatTurn.details`), so
        each is drawn as "sender - subject" with its sent date. Added to, never
        replaced: a streamed answer's details and its finished turn's agree."""
        fresh = {int(k): v for k, v in dict(details or {}).items() if v}
        if not fresh or all(self._details.get(k) == v for k, v in fresh.items()):
            return
        self._details.update(fresh)
        if self._order:
            self._draw()

    def _draw(self) -> None:
        """Every source, in the order it arrived - the list only ever grows at its
        end, so redrawing it whole moves nothing somebody is reaching for."""
        rows = [receipt_to_result(self._by_number[n], rank)
                for rank, n in enumerate(self._order, start=1)]
        self.results.show_results(rows, [], details=self._details, keep_scroll=True)

    def clear(self) -> None:
        self._by_number.clear()
        self._order.clear()
        self._details.clear()
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
