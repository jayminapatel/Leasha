r"""The Photos page's centre: one list of pictures, shown four ways.

Layer: L5

2026-10-05, the owner: "there should be option to have list view where meta
data can be viewed, so list, small thumbnail or normal thumbnail etc design a
system" and "make it like a professional photo management/viewer".

**One model, four views** - the arrangement every photo manager settles on:

- **Details** - a table: a small picture, then the name, date taken, people,
  place, what it shows, type, size and folder. Sorts by any column.
- **Small / Medium / Large** - a grid of thumbnails, 96, 160 and 256 pixels,
  the name under each in the two larger sizes.

All four read the same `PhotoModel` and share one selection, so switching view
keeps what was selected and where you were. Thumbnails come from `ThumbLoader`
(on disk once, newest-asked first); `data()` never touches a file - it asks,
and paints a placeholder until the picture arrives.

While a date-ordered grid scrolls, the month of the first row on screen shows
at its top-left - the "June 2023" a photo library scrubs by.
"""

from __future__ import annotations

from pathlib import PurePath
from typing import Any, Optional, Sequence

from PyQt6.QtCore import (QAbstractTableModel, QItemSelectionModel, QModelIndex, QPoint, QSize,
                          Qt, QTimer, pyqtSignal)
from PyQt6.QtGui import QColor, QIcon, QPixmap
from PyQt6.QtWidgets import (QAbstractItemView, QHeaderView, QLabel, QListView, QStackedWidget,
                             QTableView, QVBoxLayout, QWidget)

from app.ui.presenter.photos import COLUMNS, column_text, date_text, month_heading, people_text

__all__ = ["PhotoModel", "PhotoBrowser", "MODES", "ROLE_ROW"]

#: `(key, label, thumbnail edge)` - the View menu's choices, in its order.
MODES: tuple[tuple[str, str, int], ...] = (
    ("details", "Details", 40),
    ("small", "Small thumbnails", 96),
    ("medium", "Medium thumbnails", 160),
    ("large", "Large thumbnails", 256),
)
_EDGE = {key: edge for key, _label, edge in MODES}

ROLE_ROW = int(Qt.ItemDataRole.UserRole) + 1


class PhotoModel(QAbstractTableModel):
    """The pictures shown, as rows; the columns are `presenter.photos.COLUMNS`."""

    def __init__(self, thumbs: Any, parent: Optional[Any] = None) -> None:
        super().__init__(parent)
        self._rows: list[Any] = []
        self._index: dict[str, int] = {}
        self._thumbs = thumbs
        self._edge = _EDGE["medium"]
        #: False in the Small grid, where a name under each picture is noise.
        self.names = True
        self._placeholder = self._blank(self._edge)
        thumbs.ready.connect(self._thumb_ready)

    @staticmethod
    def _blank(edge: int) -> QIcon:
        pixmap = QPixmap(edge, edge)
        pixmap.fill(QColor(128, 128, 128, 40))
        return QIcon(pixmap)

    def set_rows(self, rows: Sequence[Any]) -> None:
        self.beginResetModel()
        self._rows = list(rows)
        self._index = {str(row.path): n for n, row in enumerate(self._rows)}
        self.endResetModel()
        self._thumbs.forget_waiting()

    def set_edge(self, edge: int) -> None:
        if edge != self._edge:
            self._edge = edge
            self._placeholder = self._blank(edge)
            if self._rows:
                self.dataChanged.emit(self.index(0, 0), self.index(len(self._rows) - 1, 0))

    def row_at(self, number: int) -> Optional[Any]:
        return self._rows[number] if 0 <= number < len(self._rows) else None

    def rows(self) -> list[Any]:
        return list(self._rows)

    def number_of(self, path: str) -> Optional[int]:
        return self._index.get(str(path))

    # -- Qt's side ------------------------------------------------------------------

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(COLUMNS)

    def headerData(self, section: int, orientation: Qt.Orientation,  # noqa: N802
                   role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return COLUMNS[section][1] if 0 <= section < len(COLUMNS) else None
        return None

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        row = self.row_at(index.row()) if index.isValid() else None
        if row is None:
            return None
        key = COLUMNS[index.column()][0]
        if role == Qt.ItemDataRole.DisplayRole:
            if key == "name" and not self.names:
                return None
            return column_text(row, key)
        if role == ROLE_ROW:
            return row
        if role == Qt.ItemDataRole.DecorationRole and index.column() == 0:
            pixmap = self._thumbs.pixmap(str(row.path))
            if pixmap is None:
                self._thumbs.request(str(row.path), row.size_bytes, row.mtime_ns)
                return self._placeholder
            return QIcon(pixmap)
        if role == Qt.ItemDataRole.ToolTipRole and index.column() == 0:
            lines = [PurePath(row.path).name, date_text(row)]
            if row.people or row.faces:
                lines.append(people_text(row))
            if row.place:
                lines.append(row.place)
            return "\n".join(line for line in lines if line)
        return None

    def sort(self, column: int, order: Qt.SortOrder = Qt.SortOrder.AscendingOrder) -> None:
        key = COLUMNS[column][0] if 0 <= column < len(COLUMNS) else "date"
        reverse = order == Qt.SortOrder.DescendingOrder

        def value(row: Any) -> Any:
            if key == "date":
                return row.when_ns
            if key == "size":
                return row.size_bytes
            return column_text(row, key).casefold()

        self.layoutAboutToBeChanged.emit()
        self._rows.sort(key=value, reverse=reverse)
        self._index = {str(row.path): n for n, row in enumerate(self._rows)}
        self.layoutChanged.emit()

    def _thumb_ready(self, path: str) -> None:
        number = self._index.get(path)
        if number is not None:
            cell = self.index(number, 0)
            self.dataChanged.emit(cell, cell, [Qt.ItemDataRole.DecorationRole])


class PhotoBrowser(QWidget):
    """The grid and the table over one `PhotoModel`, with one selection."""

    #: The row the selection now points at (None when nothing is selected).
    current_changed = pyqtSignal(object)
    #: Double-click or Enter: open the full-screen viewer at this row.
    opened = pyqtSignal(object)
    #: Right-click: `(row, global point)`.
    menu_requested = pyqtSignal(object, object)
    selection_changed = pyqtSignal()

    def __init__(self, thumbs: Any, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.model = PhotoModel(thumbs, self)
        self.mode = "medium"
        self._month_follows = True

        self.grid = QListView()
        self.grid.setObjectName("photo_grid")
        self.grid.setViewMode(QListView.ViewMode.IconMode)
        self.grid.setResizeMode(QListView.ResizeMode.Adjust)
        self.grid.setMovement(QListView.Movement.Static)
        self.grid.setUniformItemSizes(True)
        self.grid.setLayoutMode(QListView.LayoutMode.Batched)
        self.grid.setBatchSize(400)
        self.grid.setSpacing(4)
        self.grid.setWordWrap(False)
        self.grid.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.grid.setToolTip("Your pictures - double-click one to see it full size")

        self.table = QTableView()
        self.table.setObjectName("photo_table")
        self.table.setToolTip("Your pictures - click a heading to sort by it")
        self.table.setSortingEnabled(True)
        self.table.setShowGrid(False)
        self.table.setAlternatingRowColors(True)
        self.table.setWordWrap(False)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)

        for view in (self.grid, self.table):
            view.setModel(self.model)
            view.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
            view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            view.customContextMenuRequested.connect(self._menu)
            view.doubleClicked.connect(self._open_index)
            view.activated.connect(self._open_index)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        # One selection for both, so a switch of view keeps it.
        self.table.setSelectionModel(self.grid.selectionModel())
        self.grid.selectionModel().currentChanged.connect(self._current)
        self.grid.selectionModel().selectionChanged.connect(
            lambda *_a: self.selection_changed.emit())

        self._stack = QStackedWidget()
        self._stack.addWidget(self.grid)
        self._stack.addWidget(self.table)

        self.month = QLabel(self.grid.viewport())
        self.month.setObjectName("photo_month")
        self.month.setStyleSheet(
            "QLabel#photo_month { background: rgba(0,0,0,150); color: white; "
            "padding: 3px 9px; border-radius: 9px; font-weight: 600; }")
        self.month.move(10, 8)
        self.month.hide()
        self._month_timer = QTimer(self)
        self._month_timer.setSingleShot(True)
        self._month_timer.setInterval(1400)
        self._month_timer.timeout.connect(self.month.hide)
        self.grid.verticalScrollBar().valueChanged.connect(self._scrolled)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._stack)
        self.set_mode("medium")

    # -- what is shown -----------------------------------------------------------------

    def set_rows(self, rows: Sequence[Any], *, dated: bool = True) -> None:
        """Show `rows`, keeping the current picture selected when it is still there."""
        current = self.current_row()
        self._month_follows = dated
        self.model.set_rows(rows)
        if current is not None:
            self.select_path(str(current.path))

    def set_mode(self, mode: str) -> None:
        if mode not in _EDGE:
            mode = "medium"
        self.mode = mode
        edge = _EDGE[mode]
        self.model.set_edge(edge)
        if mode == "details":
            self.model.names = True
            self.table.setIconSize(QSize(edge, edge))
            self.table.verticalHeader().setDefaultSectionSize(edge + 6)
            self._stack.setCurrentWidget(self.table)
            if not getattr(self, "_widths_set", False):
                self._widths_set = True
                for column, width in enumerate((260, 150, 180, 130, 160, 60, 80)):
                    self.table.setColumnWidth(column, width)
        else:
            self.grid.setIconSize(QSize(edge, edge))
            words = mode != "small"
            self.model.names = words
            # Room under every picture for one line of name, portrait or not.
            line = self.grid.fontMetrics().height() + 10 if words else 8
            self.grid.setGridSize(QSize(edge + 16, edge + line + 8))
            self.grid.setModelColumn(0)
            self.grid.setProperty("showNames", words)
            self._stack.setCurrentWidget(self.grid)
        current = self.grid.selectionModel().currentIndex()
        view = self.active_view()
        if current.isValid():
            view.scrollTo(current, QAbstractItemView.ScrollHint.PositionAtCenter)

    def active_view(self) -> QAbstractItemView:
        return self.table if self.mode == "details" else self.grid

    # -- reading the selection ---------------------------------------------------------

    def current_row(self) -> Optional[Any]:
        index = self.grid.selectionModel().currentIndex()
        return self.model.row_at(index.row()) if index.isValid() else None

    def selected_rows(self) -> list[Any]:
        numbers = sorted({i.row() for i in self.grid.selectionModel().selectedIndexes()})
        return [self.model.row_at(n) for n in numbers if self.model.row_at(n) is not None]

    def select_path(self, path: str) -> bool:
        number = self.model.number_of(path)
        if number is None:
            return False
        index = self.model.index(number, 0)
        self.grid.selectionModel().setCurrentIndex(
            index, QItemSelectionModel.SelectionFlag.ClearAndSelect
            | QItemSelectionModel.SelectionFlag.Rows)
        self.active_view().scrollTo(index)
        return True

    def step(self, delta: int) -> Optional[Any]:
        """Move the current picture by `delta` rows, for the viewer's arrows."""
        index = self.grid.selectionModel().currentIndex()
        number = (index.row() if index.isValid() else -1) + delta
        row = self.model.row_at(number)
        if row is not None:
            self.select_path(str(row.path))
        return row

    # -- signals out ---------------------------------------------------------------------

    def _current(self, current: QModelIndex, _previous: QModelIndex) -> None:
        self.current_changed.emit(self.model.row_at(current.row()) if current.isValid() else None)

    def _open_index(self, index: QModelIndex) -> None:
        row = self.model.row_at(index.row())
        if row is not None:
            self.opened.emit(row)

    def _menu(self, point: Any) -> None:
        view = self.active_view()
        index = view.indexAt(point)
        row = self.model.row_at(index.row()) if index.isValid() else None
        if row is not None:
            if not self.grid.selectionModel().isSelected(index):
                self.select_path(str(row.path))
            self.menu_requested.emit(row, view.viewport().mapToGlobal(point))

    def _scrolled(self, _value: int) -> None:
        if not self._month_follows or self.mode == "details":
            return
        index = self.grid.indexAt(QPoint(24, 24))
        if not index.isValid():
            index = self.grid.indexAt(self.grid.viewport().rect().center())
        row = self.model.row_at(index.row()) if index.isValid() else None
        if row is None:
            return
        self.month.setText(month_heading(row.when_ns))
        self.month.adjustSize()
        self.month.show()
        self.month.raise_()
        self._month_timer.start()
