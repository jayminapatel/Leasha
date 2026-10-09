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

**Smooth, the way a photo library is** (2026-10-05, the owner: "can the ui be
slick world class and smooth like i have in google photos"):

- a picture that arrives **fades in** over `FADE_MS` instead of popping onto
  its grey tile; one already in memory shows at once, no fade;
- until it arrives, a tile seen before (in any session) shows its **blurred
  preview** (`ThumbLoader.tiny`) rather than grey - the "blur-up";
- the grid scrolls **by the pixel**, and a mouse wheel's notch **glides**
  over `GLIDE_MS` instead of jumping a row - a touchpad, already smooth,
  is left alone;
- the screenful **below** (and above) is asked for while you look at this
  one, so scrolling on finds most pictures already made.
"""

from __future__ import annotations

from pathlib import PurePath
from typing import Any, Optional, Sequence

import time

from PySide6.QtCore import (QAbstractTableModel, QEasingCurve, QEvent, QItemSelectionModel,
                          QModelIndex, QObject, QPoint, QSize, Qt, QTimer, QVariantAnimation,
                          Signal)
from PySide6.QtGui import QIcon, QPainter
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QHeaderView, QLabel, QListView,
                             QStackedWidget, QStyle, QStyledItemDelegate, QStyleOptionViewItem,
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

#: How long a picture takes to fade in once it has arrived.
FADE_MS = 160
#: How long one notch of a mouse wheel takes to glide, and how far it goes.
GLIDE_MS = 180
GLIDE_PX = 120
#: Screensful asked for beyond the one shown, below and above.
AHEAD = 1


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
        #: path -> when its picture arrived (monotonic seconds), while it fades in.
        self._arrived: dict[str, float] = {}
        self._fading = QTimer(self)
        self._fading.setInterval(16)
        self._fading.timeout.connect(self._fade_step)
        thumbs.ready.connect(self._thumb_ready)

    @staticmethod
    def _blank(edge: int) -> QIcon:
        """The grey placeholder tile at this size (`face_crops.blank_tile`, cached)."""
        from app.ui.widgets.face_crops import blank_tile

        return blank_tile(edge)

    @property
    def placeholder(self) -> QIcon:
        return self._placeholder

    def fade_of(self, path: str) -> float:
        """How far `path`'s picture has faded in: 0 just arrived, 1 done."""
        began = self._arrived.get(str(path))
        if began is None:
            return 1.0
        done = (time.monotonic() - began) * 1000 / FADE_MS
        return 1.0 if done >= 1 else max(0.0, done)

    def _fade_step(self) -> None:
        """Repaint every tile still fading in, then forget the finished ones -
        after their last repaint, so it lands at full strength.

        **One repaint per run of neighbouring tiles, not one per tile.** 2026-10-09:
        with a few hundred thumbnails arriving together, each tick sent a separate
        change for every one of them, and the window stalled for about half a second.
        """
        now = time.monotonic()
        numbers = []
        for path in list(self._arrived):
            number = self._index.get(path)
            if number is not None:
                numbers.append(number)
            if (now - self._arrived[path]) * 1000 >= FADE_MS:
                del self._arrived[path]
        for first, last in fade_runs(numbers):
            self.dataChanged.emit(self.index(first, 0), self.index(last, 0),
                                  [Qt.ItemDataRole.DecorationRole])
        if not self._arrived:
            self._fading.stop()

    def waiting_picture(self, row: Any) -> tuple[Optional[Any], Optional[Any]]:
        """`(thumbnail, blurred preview)` for a tile - either may be None."""
        thumbs = self._thumbs
        sharp = thumbs.pixmap(str(row.path))
        tiny = getattr(thumbs, "tiny", None)
        soft = tiny(str(row.path), row.size_bytes, row.mtime_ns) if tiny else None
        return sharp, soft

    def prefetch(self, first: int, last: int) -> None:
        """Ask for the thumbnails of rows `first..last` not yet made - the
        nearest asked for last, so it is made first (the loader is a stack)."""
        if not self._rows:
            return
        first, last = max(0, first), min(len(self._rows) - 1, last)
        for number in range(last, first - 1, -1):
            row = self._rows[number]
            if self._thumbs.pixmap(str(row.path)) is None:
                self._thumbs.request(str(row.path), row.size_bytes, row.mtime_ns)

    def set_rows(self, rows: Sequence[Any]) -> None:
        r"""2026-10-05, the owner: "the photos flash ... even when tagging".
        Coming back from naming, or back to the tab, read the library again
        and reset the whole grid, every tile repainting at once. When the
        same photos come back in the same order - names or places changed,
        the pictures did not - the rows are swapped under the view and only
        their words repaint. Anything else is a new list, and resets."""
        rows = list(rows)
        if rows and len(rows) == len(self._rows) and all(
                str(new.path) == str(old.path) for new, old in zip(rows, self._rows, strict=True)):
            self._rows = rows
            self.dataChanged.emit(self.index(0, 0),
                                  self.index(len(rows) - 1, len(COLUMNS) - 1))
            return
        self.beginResetModel()
        self._arrived.clear()
        self._rows = list(rows)
        self._index = {str(row.path): n for n, row in enumerate(self._rows)}
        self.endResetModel()
        self._thumbs.forget_waiting()

    def set_edge(self, edge: int) -> None:
        """A new thumbnail size: a new placeholder, and every first cell repaints."""
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
        """UI thread, no I/O: a picture not in memory is *asked for* and the
        placeholder returned; `ready` repaints the cell when it arrives.
        """
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
        """Sort the rows in place by the column's own value; the selection follows."""
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
        """A thumbnail landed: start its fade-in and repaint its cell."""
        number = self._index.get(path)
        if number is not None:
            self._arrived[path] = time.monotonic()
            if not self._fading.isActive():
                self._fading.start()
            cell = self.index(number, 0)
            self.dataChanged.emit(cell, cell, [Qt.ItemDataRole.DecorationRole])


def fade_runs(numbers) -> list[tuple[int, int]]:
    """`(first, last)` for each run of consecutive row numbers, in order.

    One repaint covers a run, so a tick costs one change per run of tiles that
    are fading together rather than one per tile. Repeats are ignored.
    """
    runs: list[list[int]] = []
    for number in sorted(set(numbers)):
        if runs and number == runs[-1][1] + 1:
            runs[-1][1] = number
        else:
            runs.append([number, number])
    return [(first, last) for first, last in runs]


class _FadeDelegate(QStyledItemDelegate):
    """Paints a tile as usual, except while its picture is on its way: then the
    tile is its blurred preview (or grey, never seen before), and a picture
    still fading in is drawn over that at the strength `PhotoModel.fade_of` gives."""

    def paint(self, painter: Any, option: Any, index: QModelIndex) -> None:
        """UI thread, no I/O: the blurred preview under a picture fading in."""
        model = index.model()
        row = index.data(ROLE_ROW)
        if row is None or index.column() != 0:
            super().paint(painter, option, index)
            return
        alpha = model.fade_of(str(row.path))
        sharp, soft = model.waiting_picture(row)
        if sharp is not None and alpha >= 1.0:
            super().paint(painter, option, index)
            return
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        picture = QIcon(opt.icon)
        opt.icon = model.placeholder
        widget = opt.widget
        style = widget.style() if widget is not None else QApplication.style()
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, opt, painter, widget)
        where = style.subElementRect(QStyle.SubElement.SE_ItemViewItemDecoration, opt, widget)
        painter.save()
        if soft is not None:
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            painter.drawPixmap(where, soft)
        if sharp is not None:
            painter.setOpacity(alpha)
            picture.paint(painter, where)
        painter.restore()


class _Glide(QObject):
    """A mouse wheel's notch glides the view instead of jumping it. A touchpad
    (pixel deltas, already smooth) and Ctrl/Shift + wheel pass straight through."""

    def __init__(self, view: QAbstractItemView) -> None:
        super().__init__(view)
        self._bar = view.verticalScrollBar()
        self._target = self._bar.value()
        self._animation = QVariantAnimation(self)
        self._animation.setDuration(GLIDE_MS)
        self._animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._animation.valueChanged.connect(lambda value: self._bar.setValue(int(value)))
        view.viewport().installEventFilter(self)

    def eventFilter(self, watched: Any, event: Any) -> bool:  # noqa: N802 - Qt's name
        """A wheel notch on the viewport: glide the scroll bar instead of jumping."""
        if event.type() != QEvent.Type.Wheel:
            return False
        if not event.pixelDelta().isNull() or event.modifiers() != Qt.KeyboardModifier.NoModifier:
            return False
        notches = event.angleDelta().y() / 120
        if not notches:
            return False
        running = self._animation.state() == QVariantAnimation.State.Running
        start = self._target if running else self._bar.value()
        self._target = max(self._bar.minimum(),
                           min(self._bar.maximum(), int(start - notches * GLIDE_PX)))
        self._animation.stop()
        self._animation.setStartValue(self._bar.value())
        self._animation.setEndValue(self._target)
        self._animation.start()
        return True


class PhotoBrowser(QWidget):
    """The grid and the table over one `PhotoModel`, with one selection."""

    #: The row the selection now points at (None when nothing is selected).
    current_changed = Signal(object)
    #: Double-click or Enter: open the full-screen viewer at this row.
    opened = Signal(object)
    #: Right-click: `(row, global point)`.
    menu_requested = Signal(object, object)
    selection_changed = Signal()

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
        self.grid.setItemDelegate(_FadeDelegate(self.grid))
        self.grid.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.grid.verticalScrollBar().setSingleStep(24)
        self._glide = _Glide(self.grid)

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
        self._ahead = QTimer(self)
        self._ahead.setSingleShot(True)
        self._ahead.setInterval(60)
        self._ahead.timeout.connect(self._ask_ahead)
        self.grid.verticalScrollBar().valueChanged.connect(lambda _v: self._ahead.start())

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._stack)
        self.set_mode("medium")

    # -- what is shown -----------------------------------------------------------------

    def set_rows(self, rows: Sequence[Any], *, dated: bool = True) -> None:
        """Show `rows`, keeping the current picture selected when it is still there."""
        current = self.current_row()
        scroll = self.active_view().verticalScrollBar().value()
        self._month_follows = dated
        self.model.set_rows(rows)
        if current is not None:
            self.select_path(str(current.path))
        elif scroll:
            # Nothing selected to scroll back to - stay where the person was.
            self.active_view().doItemsLayout()
            self.active_view().verticalScrollBar().setValue(scroll)

    def set_mode(self, mode: str) -> None:
        """Switch between Details and the three grid sizes, keeping the selection in view."""
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
        """Select and scroll to the row for `path`. False when it is not shown."""
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
        """Right-click: select the row under the pointer if it is not, then ask the page."""
        view = self.active_view()
        index = view.indexAt(point)
        row = self.model.row_at(index.row()) if index.isValid() else None
        if row is not None:
            if not self.grid.selectionModel().isSelected(index):
                self.select_path(str(row.path))
            self.menu_requested.emit(row, view.viewport().mapToGlobal(point))

    def _ask_ahead(self) -> None:
        """The screenful below and the one above, asked for while this one shows."""
        if self.mode == "details":
            return
        viewport = self.grid.viewport().rect()
        top = self.grid.indexAt(QPoint(8, 8))
        if not top.isValid():
            return
        # The last row read from its left edge: the right of a row is often the
        # empty margin past its last column, which reads as "no picture".
        # Stepping up past the gap between two rows, if the probe lands in one.
        bottom = QModelIndex()
        for y in range(viewport.height() - 8, max(0, viewport.height() - 8
                                                   - self.grid.gridSize().height()), -8):
            bottom = self.grid.indexAt(QPoint(8, y))
            if bottom.isValid():
                break
        columns = max(1, viewport.width() // max(1, self.grid.gridSize().width()))
        last = (bottom.row() + columns - 1 if bottom.isValid()
                else self.model.rowCount() - 1)
        span = max(1, last - top.row() + 1)
        self.model.prefetch(top.row() - span * AHEAD, top.row() - 1)
        self.model.prefetch(last + 1, last + span * AHEAD)

    def _scrolled(self, _value: int) -> None:
        """The grid moved: show the month of the first row on screen for a moment."""
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
