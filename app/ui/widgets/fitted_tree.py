r"""A settings table whose columns fit what is in them, and rows that fit their controls.

Layer: L5

2026-10-08, the owner, of Settings > What's indexed: "the columns on this page
are not sizeable and should autofit by default.. also the buttons size is big
they are getting clipped". Both tables there - "Folders to index"
(`roots_box.py`) and "Mail archives" (`mail_archives_box.py`) - are a
`QTreeWidget` with a drop-down and icon buttons on every line, and both had
the same three faults:

* **Columns nobody could drag.** They were `Stretch` and `ResizeToContents`,
  which Qt sizes itself and the person cannot. Every column is now
  `Interactive`: fitted to its contents whenever the rows change, and draggable
  from then on.
* **The path column squeezed to nothing.** "Stretch" takes what is left after
  the others, and on the mail list a skipped archive's sentence left the
  Archive column 43px wide - "D:\..." on every line. The path column now takes
  the spare width, never drops below a floor, and elides in the middle, so both
  the drive and the file name stay readable (the full path is in the tooltip).
* **Controls cut off at the bottom.** A widget set on a row is given the row's
  text rectangle - the row *less* the sheet's item padding - so a 28px button
  was drawn in a 26px cell, and the drop-down likewise. The row delegate here
  places each widget itself, centred at its own height, and the row height is
  worked out from the tallest control on it, at whatever scale and font size.

**Why not `view_options.remember_widths`.** It saves dragged widths through a
View button and a store, and these two boxes have neither; the brief was
"fitted by default and draggable", and a refit on every load is what keeps a
new long path visible. The 40% share every result table caps a fitted column
at (`view_options.column_cap`) is reused for the one column here that can be a
whole sentence.

**Never connected to `sectionResized`.** That connection crashed the process
in 2026-08 (`view_options.remember_widths` has the history). A drag is noticed
here by comparing the widths with the ones this module last set, on a
zero-length timer from the event loop.
"""

from __future__ import annotations

from typing import Any, Iterable

from PySide6.QtCore import QEvent, QObject, QRect, Qt, QTimer
from PySide6.QtWidgets import (
    QComboBox,
    QHeaderView,
    QStyledItemDelegate,
    QTreeWidget,
)

__all__ = ["FittedColumns", "COMPACT_SHEET", "ROW_GAP", "PATH_FLOOR_PX", "share_widths"]

#: Room above and below the tallest control on a line, in pixels, both sides
#: together. Enough that two lines' drop-downs do not touch.
ROW_GAP = 6

#: The path column is never fitted narrower than this (or its heading, if
#: wider): below it, even elided in the middle, a path is only its two ends.
PATH_FLOOR_PX = 150

#: A column that gives up width before the path does goes no narrower than
#: this - a skipped archive's reason still starts readably.
SQUEEZE_FLOOR_PX = 100

#: Space either side of a drop-down in its cell, and the extra a column is
#: given past its widest control, so the control never touches the next one.
CELL_INSET = 4

#: The compact look of the controls on a table's lines, set on the table so
#: every control added later has it too. **Sizes only**: every colour still
#: comes from the window's sheet (`theme.py`), which this sits on top of. A
#: button keeps the system's look and icon (`buttons.icon_button`) at 24px
#: instead of 28; a drop-down loses two pixels of padding top and bottom. The
#: icon carries 3px of space on its right (`buttons.ICON_GAP`), hence the
#: uneven sides.
#:
#: On the table and not on each control: a control given its own sheet while
#: the table was hidden (the mail list is, until it has a line) kept the size
#: it was first measured at - 26px, not 24 - and the line was sized to that.
COMPACT_SHEET = (
    "QPushButton { padding: 3px 3px 3px 6px; min-height: 16px; }\n"
    "QComboBox { padding: 2px 6px; }\n")


def share_widths(natural: dict[int, int], heading: dict[int, int], room: int,
                 stretch: int, squeeze: Iterable[int] = ()) -> dict[int, int]:
    """Each column's width, given what it needs (`natural`), its heading's
    width and the room the table has. Pure, so it is tested without a screen.

    Controls and short columns get what they need, always. What is left goes
    to the path column (`stretch`) and the `squeeze` columns: all the spare to
    the path when there is enough for everything; when there is not, each gets
    its floor and the rest is shared by how much more each needs. Below the
    floors the table scrolls sideways - a heading is never cut and a control
    never pushed out of reach. `room` 0 (not laid out yet) gives `natural`.
    """
    widths = dict(natural)
    if room <= 0 or stretch not in widths:
        return widths
    flexible = [stretch] + [c for c in squeeze if c in widths and c != stretch]
    fixed = sum(w for c, w in widths.items() if c not in flexible)
    left = room - fixed
    wanted = sum(natural[c] for c in flexible)
    if left >= wanted:
        widths[stretch] = natural[stretch] + (left - wanted)
        return widths
    floor_px = {c: PATH_FLOOR_PX if c == stretch else SQUEEZE_FLOOR_PX for c in flexible}
    floors = {c: max(heading.get(c, 0), min(natural[c], floor_px[c])) for c in flexible}
    spare = left - sum(floors.values())
    above = {c: natural[c] - floors[c] for c in flexible}
    total_above = sum(above.values())
    for column in flexible:
        extra = 0
        if spare > 0 and total_above > 0:
            extra = spare * above[column] // total_above
        widths[column] = floors[column] + extra
    # What integer division left over goes to the path, so the row is exact.
    if spare > 0:
        widths[stretch] += left - sum(widths[c] for c in flexible)
    return widths


class _RowDelegate(QStyledItemDelegate):
    """Every cell at least `row_height` tall; the path column elided in the
    middle; each widget on a line placed at its own height, centred."""

    def __init__(self, parent: QObject, elide: Iterable[int]) -> None:
        super().__init__(parent)
        self.row_height = 0
        self._elide = set(elide)

    def initStyleOption(self, option: Any, index: Any) -> None:   # noqa: N802 - Qt's naming
        """The path column elides in the middle so both the drive and the file name show."""
        super().initStyleOption(option, index)
        if index.column() in self._elide:
            option.textElideMode = Qt.TextElideMode.ElideMiddle

    def sizeHint(self, option: Any, index: Any) -> Any:           # noqa: N802 - Qt's naming
        """Never shorter than the tallest control on any line (`row_height`)."""
        size = super().sizeHint(option, index)
        if self.row_height > size.height():
            size.setHeight(self.row_height)
        return size

    def updateEditorGeometry(self, editor: Any, option: Any, index: Any) -> None:  # noqa: N802
        """Place a row widget at its own height, centred in the cell - Qt's own
        version hands it the text rectangle and cuts the button's bottom off.
        """
        # Qt's own version gives the widget the *text* rectangle, the row less
        # the sheet's item padding: the bottom of every button was cut off.
        cell = QRect(option.rect)
        hint = editor.sizeHint()
        height = min(cell.height(), max(hint.height(), 1))
        top = cell.top() + (cell.height() - height) // 2
        if isinstance(editor, QComboBox):
            # A drop-down at its own width, not stretched across a dragged column.
            width = max(1, min(cell.width() - 2 * CELL_INSET, hint.width()))
            editor.setGeometry(cell.left() + CELL_INSET, top, width, height)
        else:
            # A cell from `buttons.put_on_row` (or a centred tick box) lays out
            # its own contents across the column.
            editor.setGeometry(cell.left(), top, cell.width(), height)


class FittedColumns(QObject):
    """Fits a `QTreeWidget`'s columns and rows; owned by (and dies with) the tree.

    `stretch` is the path column: it takes the spare width and elides in the
    middle. `squeeze` are columns that give up width, down to
    `SQUEEZE_FLOOR_PX`, before the path goes below its floor; they are also
    capped at the share every result table caps a fitted column at.
    """

    def __init__(self, tree: QTreeWidget, *, stretch: int = 0,
                 squeeze: Iterable[int] = ()) -> None:
        super().__init__(tree)
        self._tree = tree
        self._stretch = stretch
        self._squeeze = tuple(squeeze)
        #: The widths this module last set. Anything else is a person's drag.
        self._fitted: dict[int, int] = {}
        self._force = False
        self._delegate = _RowDelegate(self, elide=(stretch,))
        tree.setItemDelegate(self._delegate)
        tree.setStyleSheet(COMPACT_SHEET)
        header = tree.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(0)
        self._timer.timeout.connect(self._run)

        model = tree.model()
        for signal in (model.rowsInserted, model.rowsRemoved, model.modelReset,
                       model.dataChanged):
            signal.connect(self._rows_changed)
        #: Kept so the filter can tell the two apart without asking the tree,
        #: which during teardown may already be gone.
        self._viewport = tree.viewport()
        tree.installEventFilter(self)
        self._viewport.installEventFilter(self)

    # -- when ------------------------------------------------------------------

    def schedule(self, *, force: bool = False) -> None:
        """Fit on the next pass of the event loop. `force` refits even over a
        width somebody dragged - for new rows, which may need more room."""
        from app.ui import qtsip

        if qtsip.isdeleted(self._tree) or qtsip.isdeleted(self._timer):
            return                                 # the table is being taken down
        self._force = self._force or force
        self._timer.start()

    def _rows_changed(self, *_args: Any) -> None:
        """Rows came or went: refit even over a dragged width - new rows may need room."""
        self.schedule(force=True)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802 - Qt's naming
        """A viewport resize, or a show/style/font change, asks for a fit on the
        next pass of the event loop.
        """
        kind = event.type()
        if watched is self._viewport:
            if kind == QEvent.Type.Resize:
                self.schedule()
        elif kind in (QEvent.Type.Show, QEvent.Type.StyleChange, QEvent.Type.FontChange):
            self.schedule()
        return False

    def _run(self) -> None:
        """The deferred fit. A dragged column is respected unless `force` was asked
        for; a tree already torn down is left alone.
        """
        force, self._force = self._force, False
        try:
            if force or not self.dragged():
                self.fit()
            else:
                self.fit_rows()
        except RuntimeError:                       # the tree's C++ side has gone
            return

    def dragged(self) -> bool:
        """Has somebody dragged a column since the last fit?"""
        header = self._tree.header()
        return any(header.sectionSize(column) != width
                   for column, width in self._fitted.items()
                   if column < header.count())

    # -- how -------------------------------------------------------------------

    def row_height(self) -> int:
        return self._delegate.row_height

    def fit_rows(self) -> int:
        """Every line as tall as its tallest control plus `ROW_GAP`. Returns
        the height."""
        tree = self._tree
        tallest = 0
        for item in self._items():
            for column in range(tree.columnCount()):
                widget = tree.itemWidget(item, column)
                if widget is not None:
                    tallest = max(tallest, widget.sizeHint().height())
        height = tallest + ROW_GAP if tallest else 0
        if height != self._delegate.row_height:
            self._delegate.row_height = height
            tree.doItemsLayout()
        return height

    def fit(self) -> dict[int, int]:
        """Fit every column to its heading and contents; the path column takes
        what is left. Returns `{column: width}` as set."""
        from app.ui.view_options import column_cap

        # Whatever was waiting is answered by this.
        self._timer.stop()
        self._force = False
        self.fit_rows()
        tree = self._tree
        header = tree.header()
        columns = [c for c in range(header.count()) if not tree.isColumnHidden(c)]
        if not columns:
            return {}
        heading = {c: header.sectionSizeHint(c) for c in columns}
        natural = {c: max(heading[c], tree.sizeHintForColumn(c), self._widgets_width(c))
                   for c in columns}
        room = max(0, tree.viewport().width())
        cap = column_cap(room)
        for column in self._squeeze:
            if column in natural and cap:
                natural[column] = max(heading[column], min(natural[column], cap))

        widths = share_widths(natural, heading, room, self._stretch, self._squeeze)
        for column, width in widths.items():
            header.resizeSection(column, width)
        self._fitted = {c: header.sectionSize(c) for c in widths}
        return dict(self._fitted)

    def _items(self) -> list:
        tree = self._tree
        return [tree.topLevelItem(i) for i in range(tree.topLevelItemCount())]

    def _widgets_width(self, column: int) -> int:
        """The widest row widget in `column`, with a drop-down's cell inset added."""
        widest = 0
        for item in self._items():
            widget = self._tree.itemWidget(item, column)
            if widget is not None:
                extra = 2 * CELL_INSET if isinstance(widget, QComboBox) else 0
                widest = max(widest, widget.sizeHint().width() + extra)
        return widest
