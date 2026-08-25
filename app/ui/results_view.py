"""The results list.

Layer: L5

**One row per document, not one per matching chunk.** Results are chunk-level,
and nothing grouped them - so a long PDF matching in five places took five of the
top ten rows and the person saw three documents where they should have seen ten.
`presenter.group_results` does the grouping; this draws it.

A row reads the way a browser result reads, because that convention is right and
people already know it: **name first**, location small and grey underneath, date
to the right. The previous layout led with a path elided in the middle -
`D:\\Archive\\2019\\Projects\\...` - which hides the distinguishing part of a long
archive path and asks somebody to scan the one thing they cannot recognise.

**`explain` and the score moved, they were not deleted.** Being able to ask "why
is this here" is where trust comes from. But it was the second thing the eye
landed on, on every row, for the life of the application - so it lives in the
tooltip and the right-click menu, and comes back inline for anyone who turns it
on.

**A result whose file has vanished is marked, not hidden.** A path that no longer
exists is a genuine finding - the index is stale for that file - and silently
dropping it would make the count disagree with the list for reasons nobody could
see.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional, Sequence

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.ui.presenter import ResultRow, group_results, to_rows
from app.ui.view_options import Density, ViewPreferences, apply_font
from app.ui.widgets.file_menu import FileActions, show_for, viewport_point

__all__ = ["ResultsView", "KIND_LABELS"]

#: A short tag per kind, rather than an icon font or bundled SVGs.
#:
#: Text survives dark mode, high-DPI and a missing font file, all of which an
#: icon set has to be got right for - and none of which is worth spending on
#: before anybody has said the tags are insufficient.
KIND_LABELS = {
    "email": "MAIL",
    "pdf": "PDF",
    "docx": "DOC", "doc": "DOC", "odt": "DOC", "rtf": "DOC",
    "xlsx": "XLS", "xls": "XLS", "ods": "XLS", "csv": "CSV",
    "pptx": "PPT", "ppt": "PPT", "odp": "PPT",
    "txt": "TXT", "md": "TXT", "log": "TXT",
}


class ResultsView(QWidget):
    """A list of search results, grouped by document."""

    opened = pyqtSignal(object)          # ResultRow
    reveal_requested = pyqtSignal(object)
    reindex_requested = pyqtSignal(object)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._rows: list[ResultRow] = []
        self._details: dict[int, Any] = {}
        self._prefs = ViewPreferences()
        #: file_ids whose chunks are currently shown. Expansion is deliberately
        #: not persisted: it is about the query on screen, and restoring it
        #: against a different result set would expand arbitrary rows.
        self._expanded: set[int] = set()

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
        """Text size, spacing, grouping and whether scores show inline."""
        self._prefs = prefs if isinstance(prefs, ViewPreferences) else ViewPreferences()
        if self._rows:
            self._redraw()

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
        self._redraw()

    def _redraw(self) -> None:
        """Rebuild the list at the current size, spacing and grouping.

        Rebuilt rather than restyled: each row is a widget sized at construction,
        so `setSizeHint` has to be recomputed or new text is drawn clipped inside
        the old row height.
        """
        # **Scroll position is kept.** Toggling a preference or expanding a row
        # otherwise jumps the list back to the top, losing the place of somebody
        # who had scrolled to the eighth result to look at it.
        bar = self._list.verticalScrollBar()
        position = bar.value() if bar is not None else 0

        self._list.clear()
        if self._prefs.group_by_document:
            for group in group_results(self._rows, details=self._details):
                self._add(_GroupItem(group, prefs=self._prefs,
                                     expanded=group.file_id in self._expanded))
                if group.file_id in self._expanded and group.match_count > 1:
                    for row in group.rows:
                        self._add(_ChunkItem(row, prefs=self._prefs), payload=row)
        else:
            for row in self._rows:
                self._add(_ChunkItem(row, prefs=self._prefs, flat=True), payload=row)

        if bar is not None:
            bar.setValue(min(position, bar.maximum()))

    def _add(self, widget: QWidget, payload: Any = None) -> None:
        item = QListWidgetItem()
        item.setData(Qt.ItemDataRole.UserRole, payload if payload is not None else widget.payload)
        item.setSizeHint(widget.sizeHint())
        self._list.addItem(item)
        self._list.setItemWidget(item, widget)

    def clear(self, message: str = "") -> None:
        self._rows = []
        self._expanded.clear()
        self._list.clear()
        self._summary.setText(message)

    def selected_rows(self) -> list[ResultRow]:
        return [
            row for row in
            (item.data(Qt.ItemDataRole.UserRole) for item in self._list.selectedItems())
            if isinstance(row, ResultRow)
        ]

    # -- interaction --------------------------------------------------------

    def _payload_for(self, item: Optional[QListWidgetItem]) -> Any:
        return item.data(Qt.ItemDataRole.UserRole) if item is not None else None

    def _row_for(self, payload: Any) -> Optional[ResultRow]:
        """A group stands in for its own best chunk when acted on."""
        if isinstance(payload, ResultRow):
            return payload
        rows = getattr(payload, "rows", None)
        return rows[0] if rows else None

    def _on_activated(self, item: QListWidgetItem) -> None:
        payload = self._payload_for(item)
        # Enter or double-click on a multi-match group expands it rather than
        # opening: the matches are the reason the row says "3 matches", and
        # opening the first one silently discards the other two.
        if getattr(payload, "match_count", 1) > 1:
            self._toggle(payload.file_id)
            return
        row = self._row_for(payload)
        if row is not None:
            self.opened.emit(row)

    def _toggle(self, file_id: int) -> None:
        if file_id in self._expanded:
            self._expanded.discard(file_id)
        else:
            self._expanded.add(file_id)
        self._redraw()

    def _on_context_menu(self, point: Any) -> None:
        """One menu, shared with the filename browser - see widgets/file_menu.py."""
        # Viewport coordinates - see `viewport_point`. A list has only a frame
        # rather than a header, so the offset is small and the bug was subtle:
        # it mostly worked, and failed on the bottom row.
        payload = self._payload_for(self._list.itemAt(viewport_point(self._list, point)))
        row = self._row_for(payload)
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
            copy=[("Why this result?", _why(row))],
        ))


def _why(row: ResultRow) -> str:
    bits = [row.explain, f"score {row.score:.2f}"]
    if row.location:
        bits.insert(0, row.location)
    return "  ·  ".join(bit for bit in bits if bit)


class _GroupItem(QWidget):
    """One document: kind, name, date, breadcrumb, match count, best snippet."""

    def __init__(self, group: Any, *, prefs: ViewPreferences, expanded: bool) -> None:
        super().__init__()
        self.payload = group
        best = group.best
        missing = _missing(group.path)

        # See `_ChunkItem`: a widget over a list item swallows the right-click
        # unless it declines the event explicitly.
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)

        kind = QLabel(KIND_LABELS.get(group.kind, (group.kind or "?").upper()[:4]))
        kind.setObjectName("resultKind")
        name = QLabel(group.name)
        name.setObjectName("resultName")
        name.setToolTip(f"{group.path}\n\n{_why(best)}" if best else group.path)
        when = QLabel(group.when)
        when.setObjectName("resultMeta")
        when.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        heading = QHBoxLayout()
        heading.setContentsMargins(0, 0, 0, 0)
        heading.addWidget(kind)
        heading.addWidget(name, stretch=1)
        heading.addWidget(when)

        subtitle = [group.folder]
        if missing:
            subtitle.append("file missing")
        if group.match_label:
            subtitle.append(f"{group.match_label} {'▾' if expanded else '▸'}")
        if prefs.show_scores and best is not None:
            subtitle.append(_why(best))
        meta = QLabel("  ·  ".join(bit for bit in subtitle if bit))
        meta.setObjectName("resultMissing" if missing else "resultMeta")

        snippet = QLabel(best.snippet.marked() if best else "")
        snippet.setObjectName("resultSnippet")
        snippet.setWordWrap(True)
        snippet.setTextFormat(Qt.TextFormat.RichText)

        compact = prefs.density == Density.COMPACT
        for label in (kind, name, when, meta, snippet):
            label.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
            apply_font(label, prefs.font_pt)

        margin = 3 if compact else 6
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, margin, 8, margin)
        layout.setSpacing(1 if compact else 2)
        layout.addLayout(heading)
        layout.addWidget(meta)
        # Compact drops the snippet, not the name: more rows on screen is the
        # point, and the snippet is the tallest part of a row by far.
        if not compact:
            layout.addWidget(snippet)


class _ChunkItem(QWidget):
    """One matching passage, indented under its document."""

    def __init__(self, row: ResultRow, *, prefs: ViewPreferences, flat: bool = False) -> None:
        super().__init__()
        self.payload = row

        # **A widget over a list item swallows the right-click.** It lands here,
        # not on the QListWidget, and a widget with the default policy is
        # entitled to handle it. `NoContextMenu` passes it up to the list, where
        # the handler that builds the menu actually lives.
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)

        bits = [row.location]
        if flat:
            bits.append(row.display_path)
        if prefs.show_scores:
            bits.append(_why(row))
        meta = QLabel("  ·  ".join(bit for bit in bits if bit))
        meta.setObjectName("resultMeta")
        meta.setToolTip(f"{row.path}\n\n{_why(row)}")

        snippet = QLabel(row.snippet.marked())
        snippet.setObjectName("resultSnippet")
        snippet.setWordWrap(True)
        snippet.setTextFormat(Qt.TextFormat.RichText)

        compact = prefs.density == Density.COMPACT
        for label in (meta, snippet):
            label.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
            apply_font(label, prefs.font_pt)

        margin = 2 if compact else 4
        layout = QVBoxLayout(self)
        # Indented when nested, so an expanded group reads as one thing rather
        # than as several unrelated results that happen to be adjacent.
        layout.setContentsMargins(8 if flat else 34, margin, 8, margin)
        layout.setSpacing(1)
        if meta.text():
            layout.addWidget(meta)
        layout.addWidget(snippet)


def _missing(path: str) -> bool:
    """Never raises. A disconnected drive is "cannot open it", not a crash."""
    try:
        return bool(path) and not Path(path).exists()
    except OSError:
        return False
