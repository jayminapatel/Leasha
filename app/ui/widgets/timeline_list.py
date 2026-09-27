r"""The timeline's list: day headings, rows, and bands of thumbnails.

Layer: L5 widget. What goes in it, and every sentence on it, is
`app/ui/presenter/timeline.py`; this only paints and forwards clicks.

**Painted, not built.** A month of 4,000 photographs is 800 bands; one widget
each would be thousands of live objects before the first was visible. A
`QStyledItemDelegate` paints the dozen on screen, exactly as
`result_delegate.py` does for search results and for the same reason.

**Every thumbnail is decoded on a worker, and only if it is still wanted.**
Painting only *asks* (`picture_for`); a `CallableWorker` decodes; the pixmap is made
back on this thread. A person dragging the scroll bar asks for hundreds of
pictures they never stop at, so a decode that starts more than a moment after
its cell was last painted is skipped rather than run - the queue never grows
behind the scroll. A photograph that will not decode (a truncated download, a
file on a drive that is in a drawer) keeps its placeholder: never an error,
never a stalled scroll.

**Nothing here touches the disk or the store.** `paint`, `sizeHint`, `data`
are the three names `test_ui_never_blocks.py` watches for a syscall in a
paint path.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from typing import Any, Optional

from PyQt6.QtCore import QEvent, QRect, QSize, Qt, QThreadPool, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPen, QPixmap, QStandardItem, QStandardItemModel
from PyQt6.QtWidgets import (
    QAbstractItemView, QListView, QStyle, QStyledItemDelegate, QToolTip,
)

from app.ui.later import later
from app.ui.presenter.timeline import BAND_SIZE, Block, photo_tip, row_text
from app.ui.theme import theme_colours
from app.ui.thumbnail_loader import decode_thumbnail

__all__ = ["TimelineList", "TimelineDelegate", "BAND_HEIGHT", "ROLE_BLOCK"]

ROLE_BLOCK = int(Qt.ItemDataRole.UserRole)

#: A band's height in pixels: a cell and its margin. Fixed on purpose - see the
#: module docstring - so the list never has to measure a photograph.
BAND_HEIGHT = 128
_PAD = 10
#: Longest edge a thumbnail is decoded to: a little over a cell, so it stays
#: sharp on a 125% or 150% screen without holding full-size pictures in memory.
_THUMB_EDGE = 176
#: How many decoded thumbnails are kept. ~60 KB each, so about 25 MB at most -
#: enough to scroll back over the last few screens without decoding again.
_CACHE_LIMIT = 400
#: A decode whose cell was last painted longer ago than this is not run.
_STALE_AFTER_S = 1.5


def _decode_if_still_wanted(path: str, seen: dict) -> Any:
    """Worker body. `None` also means "skipped", which the caller treats the
    same as a failed decode: keep the placeholder, ask again if it reappears."""
    if time.monotonic() - seen.get(path, 0.0) > _STALE_AFTER_S:
        return None
    return decode_thumbnail(path, edge=_THUMB_EDGE)


class TimelineDelegate(QStyledItemDelegate):
    """Paints a `Block`. Never reads a file: it *asks* the list for a picture."""

    def __init__(self, owner: "TimelineList") -> None:
        super().__init__(owner)
        self._owner = owner

    # -- geometry -------------------------------------------------------------

    def _fonts(self, base: QFont) -> tuple[QFont, QFont]:
        title, meta = QFont(base), QFont(base)
        title.setBold(True)
        size = base.pointSizeF() if base.pointSize() > 0 else -1
        if size > 0:
            meta.setPointSizeF(max(6.0, size - 1))
        return title, meta

    def sizeHint(self, option: Any, index: Any) -> QSize:          # noqa: N802
        block = index.data(ROLE_BLOCK)
        # Width 0: a list-mode row takes the viewport's width whatever it says,
        # and saying more is what put a horizontal scroll bar under the list.
        title, meta = self._fonts(option.font)
        if block is None:
            return QSize(0, 24)
        if block.kind == "day":
            return QSize(0, QFontMetrics(title).height() + 2 * _PAD)
        if block.kind == "band":
            return QSize(0, BAND_HEIGHT)
        return QSize(0, QFontMetrics(title).height() + QFontMetrics(meta).height() + 2 * _PAD)

    def cell_width(self, width: int) -> float:
        """One photograph's slot. **Always `BAND_SIZE` across**, even for a band
        of two, so the columns line up down the whole list."""
        return max(40.0, (width - 2 * _PAD) / BAND_SIZE)

    def cell_at(self, rect: QRect, x: int, count: int) -> Optional[int]:
        slot = int((x - rect.left() - _PAD) // self.cell_width(rect.width()))
        return slot if 0 <= slot < count else None

    # -- painting ---------------------------------------------------------------

    def paint(self, painter: QPainter, option: Any, index: Any) -> None:
        block = index.data(ROLE_BLOCK)
        if not isinstance(block, Block):
            return
        colours = theme_colours()
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        title_font, meta_font = self._fonts(option.font)
        if block.kind == "day":
            self._paint_day(painter, option.rect, block, colours, title_font)
        elif block.kind == "band":
            self._paint_band(painter, option, index, block, colours, meta_font)
        else:
            self._paint_row(painter, option, block, colours, title_font, meta_font)
        painter.restore()

    def _paint_day(self, painter, rect, block, colours, font) -> None:
        painter.setFont(font)
        painter.setPen(QColor(colours["text_dim"]))
        painter.drawText(rect.adjusted(_PAD, _PAD // 2, -_PAD, 0),
                         int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), block.text)
        painter.setPen(QPen(QColor(colours["divider"])))
        painter.drawLine(rect.left() + _PAD, rect.bottom(), rect.right() - _PAD, rect.bottom())

    def _paint_row(self, painter, option, block, colours, title_font, meta_font) -> None:
        text = row_text(block.folds[0])
        rect = option.rect.adjusted(4, 1, -4, -1)
        if option.state & QStyle.StateFlag.State_Selected:
            painter.setBrush(QColor(colours["accent_soft"]))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawRoundedRect(rect, 8, 8)
        elif option.state & QStyle.StateFlag.State_MouseOver:
            painter.setBrush(QColor(colours["surface_hover"]))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawRoundedRect(rect, 8, 8)
        left, right = rect.left() + _PAD, rect.right() - _PAD
        title_h = QFontMetrics(title_font).height()
        meta_h = QFontMetrics(meta_font).height()
        # Right-hand column: the time, and under it how the date was arrived at.
        painter.setFont(meta_font)
        column = max(QFontMetrics(meta_font).horizontalAdvance(text.basis),
                     QFontMetrics(meta_font).horizontalAdvance(text.when)) + 4
        painter.setPen(QColor(colours["text_dim"]))
        painter.drawText(QRect(right - column, rect.top() + _PAD, column, title_h),
                         int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter), text.when)
        painter.setPen(QColor(colours["text_faint"]))
        painter.drawText(QRect(right - column, rect.top() + _PAD + title_h, column, meta_h),
                         int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter), text.basis)
        room = right - column - 12 - left
        # The source badge, right after the name - "Old WD - not plugged in".
        badge_w = 0
        if text.badge:
            fm = QFontMetrics(meta_font)
            badge_w = min(fm.horizontalAdvance(text.badge) + 14, max(60, room // 2))
            self._pill(painter, QRect(left + room - badge_w, rect.top() + _PAD + 1, badge_w,
                                      title_h - 2), text.badge, colours, meta_font)
        painter.setFont(title_font)
        painter.setPen(QColor(colours["text"]))
        painter.drawText(
            QRect(left, rect.top() + _PAD, room - badge_w - (8 if badge_w else 0), title_h),
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            QFontMetrics(title_font).elidedText(text.title, Qt.TextElideMode.ElideMiddle,
                                                max(20, room - badge_w - 8)))
        painter.setFont(meta_font)
        painter.setPen(QColor(colours["text_faint"]))
        detail = text.detail + (f"  -  {text.folded}" if text.folded else "")
        painter.drawText(
            QRect(left, rect.top() + _PAD + title_h, room, meta_h),
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            QFontMetrics(meta_font).elidedText(detail, Qt.TextElideMode.ElideRight, room))

    def _pill(self, painter, rect, text, colours, font) -> None:
        painter.setBrush(QColor(colours["accent_soft"]))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRoundedRect(rect, rect.height() / 2, rect.height() / 2)
        painter.setFont(font)
        painter.setPen(QColor(colours["accent_text"]))
        painter.drawText(rect.adjusted(6, 0, -6, 0), int(Qt.AlignmentFlag.AlignCenter),
                         QFontMetrics(font).elidedText(text, Qt.TextElideMode.ElideRight, rect.width() - 12))

    def _paint_band(self, painter, option, index, block, colours, meta_font) -> None:
        rect = option.rect
        slot = self.cell_width(rect.width())
        chosen = self._owner.chosen_cell(index)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        for position, fold in enumerate(block.folds):
            cell = QRect(int(rect.left() + _PAD + position * slot), rect.top() + 4,
                         int(slot) - 6, rect.height() - 8)
            painter.setBrush(QColor(colours["surface_alt"]))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawRoundedRect(cell, 6, 6)
            entry = fold.head
            pixmap = self._owner.picture_for(entry)
            if pixmap is not None and not pixmap.isNull():
                fitted = pixmap.scaled(cell.size(), Qt.AspectRatioMode.KeepAspectRatio,
                                       Qt.TransformationMode.SmoothTransformation)
                painter.drawPixmap(cell.left() + (cell.width() - fitted.width()) // 2,
                                   cell.top() + (cell.height() - fitted.height()) // 2, fitted)
            else:
                # Not here (a drive in a drawer) or not decoded yet: say which.
                painter.setFont(meta_font)
                painter.setPen(QColor(colours["text_faint"]))
                label = entry.source_name if (entry.on_a_source and not entry.reachable) else ""
                painter.drawText(cell.adjusted(4, 4, -4, -4),
                                 int(Qt.AlignmentFlag.AlignCenter) | int(Qt.TextFlag.TextWordWrap),
                                 label or "...")
            if entry.on_a_source and not entry.reachable:
                # A photograph on a drive that is not plugged in still gets its
                # cell - but marked, so nobody double-clicks it expecting a picture.
                self._corner_tag(painter, cell, "not plugged in", colours, meta_font, top=True)
            if fold.folded:
                self._corner_tag(painter, cell, f"+{len(fold.older)}", colours, meta_font, top=False)
            if selected and chosen == position:
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.setPen(QPen(QColor(colours["accent"]), 3))
                painter.drawRoundedRect(cell.adjusted(1, 1, -1, -1), 6, 6)

    def _corner_tag(self, painter, cell, text, colours, font, *, top: bool) -> None:
        fm = QFontMetrics(font)
        width = min(cell.width() - 8, fm.horizontalAdvance(text) + 12)
        tag = QRect(cell.right() - width - 4, (cell.top() + 4) if top else (cell.bottom() - fm.height() - 6),
                    width, fm.height() + 2)
        self._pill(painter, tag, text, colours, font)

    # -- tooltips --------------------------------------------------------------

    def helpEvent(self, event: Any, view: Any, option: Any, index: Any) -> bool:   # noqa: N802
        block = index.data(ROLE_BLOCK) if index.isValid() else None
        if event.type() != QEvent.Type.ToolTip or not isinstance(block, Block):
            return False
        if block.kind == "band":
            slot = self.cell_at(option.rect, event.pos().x(), len(block.folds))
            text = photo_tip(block.folds[slot]) if slot is not None else ""
        elif block.kind == "row":
            row = row_text(block.folds[0])
            text = f"{row.basis}: {row.basis_tip}"
        else:
            text = ""
        if text:
            QToolTip.showText(event.globalPos(), text, view)
            return True
        QToolTip.hideText()
        return True


class TimelineList(QListView):
    """A virtualised list of `Block`s that asks for more when it runs out."""

    #: A row or a photograph was opened (Enter, double-click). Carries the entry.
    opened = pyqtSignal(object)
    #: The bottom is near: the view should ask its worker for the next page.
    near_end = pyqtSignal()
    #: One photograph or row was right-clicked; carries `(fold, global point)`.
    menu_requested = pyqtSignal(object, object)

    def __init__(self, parent: Any = None) -> None:
        super().__init__(parent)
        self.setAccessibleName("Timeline")
        self._model = QStandardItemModel(self)
        self.setModel(self._model)
        self.setItemDelegate(TimelineDelegate(self))
        self.setUniformItemSizes(False)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setMouseTracking(True)
        self.setFrameShape(QListView.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._on_context_menu)
        self.doubleClicked.connect(lambda _index: self._open_current())
        self.verticalScrollBar().valueChanged.connect(self._maybe_more)
        self._cell = 0
        self._pictures: "OrderedDict[str, Optional[QPixmap]]" = OrderedDict()
        self._asked: set = set()
        self._seen: dict = {}
        self._pool = QThreadPool.globalInstance()

    # -- content ------------------------------------------------------------

    def append_blocks(self, blocks: list) -> None:
        for block in blocks:
            item = QStandardItem()
            item.setData(block, ROLE_BLOCK)
            if block.kind == "day":
                item.setFlags(Qt.ItemFlag.NoItemFlags)
                spoken = block.text
            else:
                item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                first = block.folds[0].head
                spoken = (f"{len(block.folds)} photograph{'s' if len(block.folds) != 1 else ''}, from {first.name}" if block.kind == "band"
                          else f"{row_text(block.folds[0]).title}, {row_text(block.folds[0]).detail}")
            item.setData(spoken, int(Qt.ItemDataRole.AccessibleTextRole))
            self._model.appendRow(item)
        QTimer.singleShot(0, self._more_once_laid_out)

    def clear_blocks(self) -> None:
        self._model.clear()
        self._cell = 0

    def block_count(self) -> int:
        return self._model.rowCount()

    def block_at(self, row: int) -> Optional[Block]:
        return self._model.index(row, 0).data(ROLE_BLOCK) if 0 <= row < self._model.rowCount() else None

    # -- thumbnails ------------------------------------------------------------

    def picture_for(self, entry: Any) -> Optional[QPixmap]:
        """A decoded thumbnail, or None - and *asks* for one if it has none yet."""
        path = entry.real_path or (entry.path if not entry.on_a_source else "")
        if not path:
            return None
        self._seen[path] = time.monotonic()
        if path in self._pictures:
            self._pictures.move_to_end(path)
            return self._pictures[path]
        if path not in self._asked:
            self._asked.add(path)
            # `later`: a list the person has scrolled away from - or closed - must not
            # have a picture decoded into it afterwards.
            later(self, 0, lambda p=path: self._start_decode(p))
        return None

    def _start_decode(self, path: str) -> None:
        from app.ui.workers import CallableWorker, run

        worker = CallableWorker(_decode_if_still_wanted, path, self._seen, component="ui.timeline")
        worker.signals.finished.connect(lambda image, p=path: self._decoded(p, image))
        worker.signals.failed.connect(lambda _error, p=path: self._decoded(p, None))
        run(self._pool, worker)

    def _decoded(self, path: str, image: Any) -> None:
        self._asked.discard(path)
        if image is None:
            return                       # skipped or unreadable: the placeholder stays
        pixmap = QPixmap.fromImage(image)
        self._pictures[path] = None if pixmap.isNull() else pixmap
        while len(self._pictures) > _CACHE_LIMIT:
            self._pictures.popitem(last=False)
        self.viewport().update()

    # -- paging ---------------------------------------------------------------

    def _more_once_laid_out(self) -> None:
        """`_maybe_more` for rows just appended - but only once they are laid out.

        The zero-delay timer in `append_blocks` was meant to run after layout,
        and usually did. Qt's own delayed item layout is *also* a zero-delay
        timer, and which fires first is not promised. When ours won, the rows
        were not laid out yet, the scroll bar still had a maximum of 0, "at the
        bottom" was true, and a second page was fetched that nobody scrolled to.
        Found by `test_a_month_of_photographs_arrives_a_page_at_a_time_as_the_list_is_scrolled`,
        which failed about half the time on main whenever the timing moved.
        Flushing the pending layout first is the layout the list was about to
        do anyway, so it costs nothing extra.
        """
        self.executeDelayedItemsLayout()
        self._maybe_more()

    def _maybe_more(self, *_args: Any) -> None:
        bar = self.verticalScrollBar()
        if self._model.rowCount() and bar.value() >= bar.maximum() - 3 * BAND_HEIGHT:
            self.near_end.emit()

    # -- choosing ---------------------------------------------------------------

    def chosen_cell(self, index: Any) -> int:
        return self._cell

    def _fold_at(self, index: Any, x: Optional[int] = None) -> Any:
        block = index.data(ROLE_BLOCK) if index.isValid() else None
        if block is None or block.kind == "day":
            return None
        if block.kind == "row":
            return block.folds[0]
        slot = self.itemDelegate().cell_at(self.visualRect(index), x, len(block.folds)) \
            if x is not None else min(self._cell, len(block.folds) - 1)
        return block.folds[slot] if slot is not None else None

    def current_fold(self) -> Any:
        return self._fold_at(self.currentIndex())

    def mousePressEvent(self, event: Any) -> None:                # noqa: N802
        index = self.indexAt(event.position().toPoint())
        block = index.data(ROLE_BLOCK) if index.isValid() else None
        if block is not None and block.kind == "band":
            slot = self.itemDelegate().cell_at(self.visualRect(index), int(event.position().x()),
                                               len(block.folds))
            self._cell = slot if slot is not None else 0
        super().mousePressEvent(event)

    def keyPressEvent(self, event: Any) -> None:                  # noqa: N802
        block = self.currentIndex().data(ROLE_BLOCK) if self.currentIndex().isValid() else None
        key = event.key()
        if block is not None and block.kind == "band" and key in (Qt.Key.Key_Left, Qt.Key.Key_Right):
            step = 1 if key == Qt.Key.Key_Right else -1
            self._cell = max(0, min(len(block.folds) - 1, self._cell + step))
            self.viewport().update()
            return
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self._open_current()
            return
        super().keyPressEvent(event)

    def _open_current(self) -> None:
        fold = self.current_fold()
        if fold is not None:
            self.opened.emit(fold.head)

    def _on_context_menu(self, point: Any) -> None:
        index = self.indexAt(point)
        fold = self._fold_at(index, point.x())
        if fold is not None:
            self.setCurrentIndex(index)
            self.menu_requested.emit(fold, self.viewport().mapToGlobal(point))
