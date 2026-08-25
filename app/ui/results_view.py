"""The results list.

Layer: L5

Each row shows the path, where in the document the hit is, why it matched, and a
snippet centred on the match with the query terms picked out. All four of those
are computed in `presenter.py`; this module draws them.

**A result whose file has vanished is marked, not hidden.** A path that no longer
exists is a genuine finding - the index is stale for that file - and silently
dropping it would make the result count disagree with the list for reasons
nobody could see.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional, Sequence

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.ui.view_options import Density
from app.ui.widgets.file_menu import FileActions, show_for, viewport_point
from app.ui.presenter import ResultRow, to_rows

__all__ = ["ResultsView"]


class ResultsView(QWidget):
    """A list of search results with open, reveal, copy and add-to-document."""

    opened = pyqtSignal(object)          # ResultRow
    reveal_requested = pyqtSignal(object)
    reindex_requested = pyqtSignal(object)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._rows: list[ResultRow] = []
        #: 0 follows the system font. Results are the one pane people read
        #: rather than scan, and the right size for reading a paragraph of
        #: snippet is not the right size for a toolbar - which is why this is
        #: the pane's own setting and not an application-wide zoom.
        self._font_pt = 0
        self._density = Density.NORMAL

        self._summary = QLabel("")
        self._summary.setObjectName("resultsSummary")

        self._list = QListWidget()
        self._list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._list.setWordWrap(True)
        self._list.setUniformItemSizes(False)
        self._list.itemActivated.connect(self._on_activated)
        self._list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._list.customContextMenuRequested.connect(self._on_context_menu)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._summary)
        layout.addWidget(self._list, stretch=1)

    def set_view_preferences(self, prefs: Any) -> None:
        """Text size and row spacing for the results pane.

        Re-rendered rather than restyled: each row is a widget built at a fixed
        size, so `setSizeHint` has to be recomputed or the new font is drawn
        clipped inside the old row height.
        """
        self._font_pt = int(getattr(prefs, "font_pt", 0) or 0)
        self._density = str(getattr(prefs, "density", Density.NORMAL))
        if self._rows:
            self._redraw()

    # -- populating ---------------------------------------------------------

    def show_results(self, results: Sequence[Any], terms: Sequence[str], summary: str = "") -> None:
        self._rows = to_rows(results, terms)
        self._list.clear()
        self._summary.setText(summary)

        self._redraw()

    def _redraw(self) -> None:
        """Rebuild the visible rows at the current size and spacing."""
        self._list.clear()
        for row in self._rows:
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, row)
            widget = _ResultItem(row, font_pt=self._font_pt, density=self._density)
            item.setSizeHint(widget.sizeHint())
            self._list.addItem(item)
            self._list.setItemWidget(item, widget)

    def clear(self, message: str = "") -> None:
        self._rows = []
        self._list.clear()
        self._summary.setText(message)

    def selected_rows(self) -> list[ResultRow]:
        return [
            item.data(Qt.ItemDataRole.UserRole)
            for item in self._list.selectedItems()
        ]

    # -- interaction --------------------------------------------------------

    def _row_for(self, item: Optional[QListWidgetItem]) -> Optional[ResultRow]:
        return item.data(Qt.ItemDataRole.UserRole) if item is not None else None

    def _on_activated(self, item: QListWidgetItem) -> None:
        row = self._row_for(item)
        if row is not None:
            self.opened.emit(row)

    def _on_context_menu(self, point: Any) -> None:
        """One menu, shared with the filename browser - see widgets/file_menu.py.

        It used to be built here, and offered "Add to document", which emitted a
        signal nothing was connected to. Layer 7 was cancelled before it was
        built, and the menu item outlived it: a thing you could click that did
        nothing at all, silently.
        """
        # Viewport coordinates - see `viewport_point`. A list has only a frame
        # rather than a header, so the offset is a pixel or two and the bug was
        # subtler here: it mostly worked, and failed on the bottom row.
        row = self._row_for(self._list.itemAt(viewport_point(self._list, point)))
        if row is None:
            # No item under the cursor: empty space below the results, or the
            # Menu key. Use whatever is selected rather than doing nothing.
            selected = self.selected_rows()
            row = selected[0] if selected else None
        if row is None:
            return

        show_for(self._list, point, row.path, FileActions(
            open_file=lambda: self.opened.emit(row),
            reveal=lambda: self.reveal_requested.emit(row),
            reindex=lambda: self.reindex_requested.emit(row),
        ))


class _ResultItem(QWidget):
    """One row: path, location, why it matched, and a highlighted snippet."""

    def __init__(
        self,
        row: ResultRow,
        parent: Optional[QWidget] = None,
        *,
        font_pt: int = 0,
        density: str = Density.NORMAL,
    ) -> None:
        super().__init__(parent)
        missing = not Path(row.path).exists()

        # **The other half of "right click doesn't work".**
        #
        # Every row here is a real widget sitting on top of the list item, via
        # `setItemWidget`. The right-click therefore lands on *this* widget, not
        # on the QListWidget - and a widget with the default policy is entitled
        # to handle the event itself. `NoContextMenu` says explicitly that it
        # does not, so the event passes up to the list, where the handler that
        # builds the menu actually lives.
        #
        # Set on the labels too: the snippet is rich text, and a QLabel that
        # decides its text is selectable grows its own "Copy" menu, which would
        # shadow the file menu on the one part of the row people aim at most.
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)

        heading = QLabel(f"{row.rank}. {row.display_path}")
        heading.setObjectName("resultPath")
        heading.setToolTip(row.path)

        bits = [row.location, row.explain]
        if missing:
            bits.append("file missing")
        meta = QLabel("  ·  ".join(bit for bit in bits if bit))
        meta.setObjectName("resultMissing" if missing else "resultMeta")

        snippet = QLabel(row.snippet.marked())
        snippet.setObjectName("resultSnippet")
        snippet.setWordWrap(True)
        snippet.setTextFormat(Qt.TextFormat.RichText)

        for label in (heading, meta, snippet):
            label.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
            if font_pt:
                font = label.font()
                font.setPointSize(int(font_pt))
                label.setFont(font)

        # Compact halves the padding rather than shrinking the text: the point
        # of a compact list is more rows on screen, and text you cannot read is
        # not more information.
        compact = density == Density.COMPACT
        if compact:
            meta.setVisible(False)      # the least-read line, first to go

        layout = QVBoxLayout(self)
        margin = 3 if compact else 6
        layout.setContentsMargins(8, margin, 8, margin)
        layout.setSpacing(1 if compact else 2)
        layout.addWidget(heading)
        layout.addWidget(meta)
        layout.addWidget(snippet)
