r"""The results list.

Layer: L5

**One row per document, not one per matching chunk.** A long PDF matching in
five places took five of the top ten rows, so ten documents became three.
`presenter.group_results` does the grouping; this shows it.

**Rows are painted, not built.** This used `QListWidget` with `setItemWidget` -
three live `QLabel`s per row, fifteen hundred widgets for five hundred results,
all constructed before the first one is visible. It is now a `QListView` over a
plain model with `ResultDelegate` painting only what is on screen, so the cost
stops scaling with the result count.

A row reads the way a browser result reads, because that convention is right and
people already know it: **name first**, location small and grey underneath, date
to the right. The previous layout led with a path elided in the *middle* -
`D:\Archive\2019\Projects\...` - which hides the distinguishing part of a long
archive path and asks somebody to scan the one thing they cannot recognise.

**`explain` and the score moved, they were not deleted.** Being able to ask "why
is this here" is where trust comes from. But it was the second thing the eye
landed on, on every row, forever - so it lives in the tooltip and the right-click
menu, and returns inline for anyone who turns it on.

**A result whose file has vanished is marked, not hidden.** A stale index entry
is a genuine finding, and dropping it silently would make the count disagree with
the list for reasons nobody could see.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional, Sequence

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QStandardItem, QStandardItemModel
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QLabel,
    QListView,
    QVBoxLayout,
    QWidget,
)

from app.ui.presenter import (
    KIND_LABELS, ResultGroup, ResultRow, group_results, result_tooltip, to_rows, why,
)
from app.ui.result_delegate import ROLE_EXPANDED, ROLE_PAYLOAD, ResultDelegate
from app.ui.view_options import ViewPreferences
from app.ui.widgets.file_menu import FileActions, show_for, viewport_point

__all__ = ["ResultsView", "KIND_LABELS"]

class ResultsView(QWidget):
    """A painted list of search results, grouped by document."""

    opened = pyqtSignal(object)          # ResultRow
    reveal_requested = pyqtSignal(object)
    reindex_requested = pyqtSignal(object)
    #: The row under the cursor changed. The preview pane listens; nothing else
    #: does, and nothing here knows the preview exists.
    selected = pyqtSignal(object)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._rows: list[ResultRow] = []
        self._details: dict[int, Any] = {}
        self._prefs = ViewPreferences()
        #: file_ids currently showing their chunks. Deliberately not persisted:
        #: it is about the query on screen, and restoring it against a different
        #: result set would expand arbitrary rows.
        self._expanded: set[int] = set()

        self._summary = QLabel("")
        self._summary.setObjectName("resultsSummary")

        self._model = QStandardItemModel(self)
        self._delegate = ResultDelegate(self)

        self._list = QListView()
        self._list.setModel(self._model)
        self._list.setItemDelegate(self._delegate)
        self._list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._list.setUniformItemSizes(False)
        self._list.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self._list.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._list.activated.connect(self._on_activated)
        self._list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._list.customContextMenuRequested.connect(self._on_context_menu)
        self._list.selectionModel().currentChanged.connect(
            lambda current, _prev: self.selected.emit(self._row_for(
                current.data(ROLE_PAYLOAD) if current.isValid() else None)))

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._summary)
        layout.addWidget(self._list, stretch=1)

    def set_view_preferences(self, prefs: Any) -> None:
        """Text size, spacing, grouping and whether scores show inline."""
        self._prefs = prefs if isinstance(prefs, ViewPreferences) else ViewPreferences()
        self._delegate.prefs = self._prefs
        if self._rows:
            self._rebuild()

    # -- populating ---------------------------------------------------------

    def show_results(
        self,
        results: Sequence[Any],
        terms: Sequence[str],
        summary: str = "",
        details: Optional[dict[int, Any]] = None,
    ) -> None:
        """`details` maps file_id to mail metadata - see `store.messages_for`."""
        self._rows = to_rows(results, terms)
        self._details = dict(details or {})
        self._expanded.clear()
        self._summary.setText(summary)
        self._rebuild()

    def _rebuild(self) -> None:
        """Refill the model. Rows are data now, so this is cheap."""
        # **Scroll position is kept.** Toggling a preference or expanding a row
        # otherwise jumps the list to the top, losing the place of somebody who
        # had scrolled to the eighth result to read it.
        bar = self._list.verticalScrollBar()
        position = bar.value() if bar is not None else 0

        self._model.clear()
        if self._prefs.group_by_document:
            for group in group_results(self._rows, details=self._details):
                expanded = group.file_id in self._expanded
                self._append(group, expanded=expanded)
                if expanded and group.match_count > 1:
                    for row in group.rows:
                        self._append(row)
        else:
            for row in self._rows:
                self._append(row)

        if bar is not None:
            bar.setValue(min(position, bar.maximum()))

    def _append(self, payload: Any, *, expanded: bool = False) -> None:
        item = QStandardItem()
        item.setEditable(False)
        item.setData(payload, ROLE_PAYLOAD)
        item.setData(expanded, ROLE_EXPANDED)
        item.setData(
            result_tooltip(payload, missing=_missing(getattr(payload, 'path', ''))),
            int(Qt.ItemDataRole.ToolTipRole))
        self._model.appendRow(item)

    def clear(self, message: str = "") -> None:
        self._rows = []
        self._expanded.clear()
        self._model.clear()
        self._summary.setText(message)

    def selected_rows(self) -> list[ResultRow]:
        rows = [
            self._row_for(index.data(ROLE_PAYLOAD))
            for index in self._list.selectedIndexes()
        ]
        return [row for row in rows if row is not None]

    # -- interaction --------------------------------------------------------

    def _row_for(self, payload: Any) -> Optional[ResultRow]:
        """A group stands in for its own best chunk when acted on."""
        if isinstance(payload, ResultRow):
            return payload
        return getattr(payload, "best", None)

    def _on_activated(self, index: Any) -> None:
        payload = index.data(ROLE_PAYLOAD)
        # Enter or double-click on a multi-match group expands it rather than
        # opening: the matches are the reason the row says "3 matches", and
        # opening the first silently discards the other two.
        if isinstance(payload, ResultGroup) and payload.match_count > 1:
            self._toggle(payload.file_id)
            return
        row = self._row_for(payload)
        if row is not None:
            self.opened.emit(row)

    def _toggle(self, file_id: int) -> None:
        self._expanded.symmetric_difference_update({file_id})
        self._rebuild()

    def _on_context_menu(self, point: Any) -> None:
        """One menu, shared with the filename browser - see widgets/file_menu.py."""
        # Viewport coordinates: the signal gives a point relative to the widget
        # and `indexAt` wants one relative to the viewport. Getting this wrong is
        # why right-click looked broken - it acted a row low and found nothing
        # at all on the last row.
        index = self._list.indexAt(viewport_point(self._list, point))
        row = self._row_for(index.data(ROLE_PAYLOAD) if index.isValid() else None)
        if row is None:
            selected = self.selected_rows()
            row = selected[0] if selected else None
        if row is None:
            return

        # **"Why this result?" keeps the explanation reachable.** It came off
        # every row to stop it competing with the name; it must not become
        # unavailable, because being able to ask is where trust comes from.
        show_for(self._list, point, row.path, FileActions(
            open_file=lambda: self.opened.emit(row),
            reveal=lambda: self.reveal_requested.emit(row),
            reindex=lambda: self.reindex_requested.emit(row),
            copy=[("Why this result?", why(row))],
        ))


def _missing(path: str) -> bool:
    """Never raises. A disconnected drive is "cannot open it", not a crash."""
    try:
        return bool(path) and not Path(path).exists()
    except OSError:
        return False
