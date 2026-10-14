r"""The Photos grid's optional "Aspect" view: rows of photos at their own shape.

Layer: L5

MS-12 (UX review 2026-10-14). The square grid stays the default - the owner chose it.
"Aspect" is a fifth choice in the page's View menu, beside Details and the three square
sizes: every row one height, every photo as wide as its own shape needs, the row just
filling the width - the layout Apple Photos and Google Photos use.

**The same grid, laid out another way.** No second view: the grid's `QListView` flows
its tiles left to right and wraps (`ListMode`, `LeftToRight`, wrapping, sizes per tile),
and this delegate gives each tile the size `presenter.photo_rows.justify` worked out, so
Qt's own wrapping lands exactly on the rows the arithmetic chose. The selection, the
keys, the menu, the month pill and the read-ahead are the grid's, unchanged.

**The pictures are `ThumbLoader`'s, uncropped.** With the view chosen the loader keeps
each thumbnail whole beside its square (`ThumbLoader.keep_shape`) and says each photo's
shape (`aspect`); a photo not made yet is laid out at 4:3 and the rows settle, once,
when its shape is known - a burst of arrivals is one re-layout (`SETTLE_MS`), and the
photo at the top of the screen stays where it was.
"""

from __future__ import annotations

from collections import OrderedDict
from typing import Any, Optional

from PySide6.QtCore import QModelIndex, QPoint, QRect, QSize, Qt, QTimer
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QListView, QStyle,
                               QStyledItemDelegate, QStyleOptionViewItem)

from app.ui.presenter.photo_rows import ROW_HEIGHT, justify

__all__ = ["AspectDelegate", "enter", "leave", "SETTLE_MS", "INSET"]

#: How long a burst of newly known shapes waits before the rows are laid out again.
SETTLE_MS = 150
#: The picture is drawn this far inside its tile, so the selection shows round it.
INSET = 2
#: Pictures kept already cropped and scaled to their tile (a few screensful).
KEEP_COVERED = 256


def _cover(cache: "OrderedDict", pixmap: Any, width: int, height: int, ratio: float) -> Any:
    """`pixmap` cropped to the shape `width` x `height` and scaled to fill it, made once and
    kept in `cache` by the pixmap's `cacheKey` and the size. Drawn 1:1 on any screen."""
    ratio = ratio if ratio > 0 else 1.0
    key = (int(pixmap.cacheKey()), int(width), int(height), round(ratio, 3))
    found = cache.get(key)
    if found is not None:
        cache.move_to_end(key)
        return found
    source_w, source_h = pixmap.width(), pixmap.height()
    if source_w <= 0 or source_h <= 0 or width <= 0 or height <= 0:
        return pixmap
    want = width / height
    if source_w / source_h > want:                      # wider than the tile: trim the sides
        crop_w = max(1, round(source_h * want))
        part = pixmap.copy((source_w - crop_w) // 2, 0, crop_w, source_h)
    else:                                               # taller: trim top and bottom
        crop_h = max(1, round(source_w / want))
        part = pixmap.copy(0, (source_h - crop_h) // 2, source_w, crop_h)
    found = part.scaled(max(1, round(width * ratio)), max(1, round(height * ratio)),
                        Qt.AspectRatioMode.IgnoreAspectRatio,
                        Qt.TransformationMode.SmoothTransformation)
    found.setDevicePixelRatio(ratio)
    cache[key] = found
    while len(cache) > KEEP_COVERED:
        cache.popitem(last=False)
    return found


class AspectDelegate(QStyledItemDelegate):
    """Sizes each tile from the justified rows and paints its picture at its own shape."""

    def __init__(self, browser: Any) -> None:
        super().__init__(browser.grid)
        self._browser = browser
        self._sizes: list[tuple[int, int]] = []
        self._key: Optional[tuple] = None
        self._covered: "OrderedDict" = OrderedDict()
        #: Bumped whenever a photo's shape becomes known; part of the layout's key.
        self.version = 0

    # -- the layout ------------------------------------------------------------------

    def gap(self) -> int:
        """The gap Qt's flow leaves between two tiles: `spacing` on each side of each tile
        (measured offscreen, 2026-10-14: tiles at x = 4, 231, ... for spacing 4)."""
        return 2 * self._browser.grid.spacing()

    def room(self) -> int:
        """The width a row may fill: the viewport, less `spacing` before the first tile and
        after the last, and a pixel so the last tile never tips onto the next row."""
        grid = self._browser.grid
        return max(1, grid.viewport().width() - 2 * grid.spacing() - 1)

    def sizes(self) -> list[tuple[int, int]]:
        """Every tile's `(width, height)`, worked out again only when the width, the rows
        or a shape changed."""
        model = self._browser.model
        key = (self.room(), model.rowCount(), self.version, id(model.rows_ref()))
        if key != self._key:
            thumbs = model.thumbs
            shape = getattr(thumbs, "aspect", None)
            aspects = [shape(str(row.path)) if shape else None for row in model.rows_ref()]
            self._sizes = justify(aspects, key[0], ROW_HEIGHT, self.gap())
            self._key = key
        return self._sizes

    def sizeHint(self, option: Any, index: QModelIndex) -> QSize:      # noqa: N802 - Qt's name
        sizes = self.sizes()
        number = index.row()
        if 0 <= number < len(sizes):
            return QSize(*sizes[number])
        return QSize(ROW_HEIGHT, ROW_HEIGHT)

    # -- the painting ----------------------------------------------------------------

    def paint(self, painter: Any, option: Any, index: QModelIndex) -> None:
        """UI thread, no I/O: the selection, then the picture (or its blurred preview, or
        grey) fitted to the tile, fading in as the square grid's do."""
        from app.ui.widgets.photo_browser import ROLE_ROW, scaled_preview

        row = index.data(ROLE_ROW)
        opt = QStyleOptionViewItem(option)
        widget = opt.widget
        style = widget.style() if widget is not None else QApplication.style()
        style.drawPrimitive(QStyle.PrimitiveElement.PE_PanelItemViewItem, opt, painter, widget)
        if row is None:
            return
        model = index.model()
        thumbs = model.thumbs
        path = str(row.path)
        where = QRect(opt.rect).adjusted(INSET, INSET, -INSET, -INSET)
        if where.width() <= 0 or where.height() <= 0:
            return
        shaped = getattr(thumbs, "shaped", None)
        picture = shaped(path) if shaped else None
        if picture is None:
            thumbs.request(path, row.size_bytes, row.mtime_ns)
            picture = thumbs.pixmap(path)               # the square, until the whole one comes
        try:
            ratio = painter.device().devicePixelRatioF()
        except (AttributeError, RuntimeError):
            ratio = 1.0
        alpha = model.fade_of(path)
        painter.save()
        if picture is None or alpha < 1.0:
            _sharp, soft = model.waiting_picture(row)
            if soft is not None:
                painter.drawPixmap(where.topLeft(), scaled_preview(
                    self._covered, soft, where.width(), where.height(), ratio))
            else:
                model.placeholder.paint(painter, where)
        if picture is not None:
            painter.setOpacity(alpha)
            painter.drawPixmap(where.topLeft(),
                               _cover(self._covered, picture, where.width(), where.height(), ratio))
        painter.restore()


def enter(browser: Any) -> None:
    """Lay the grid out in justified rows: flow left to right, a size per tile."""
    grid = browser.grid
    delegate = getattr(browser, "_aspect_delegate", None)
    if delegate is None:
        delegate = browser._aspect_delegate = AspectDelegate(browser)
        browser._aspect_settle = QTimer(browser)
        browser._aspect_settle.setSingleShot(True)
        browser._aspect_settle.setInterval(SETTLE_MS)
        browser._aspect_settle.timeout.connect(lambda: _settle(browser))
        browser.model.thumbs.ready.connect(lambda path: _shape_known(browser, path))
    if browser._fade_delegate is None:
        browser._fade_delegate = grid.itemDelegate()
    keep = getattr(browser.model.thumbs, "set_keep_shape", None)
    if keep is not None:
        keep(True)
    grid.setViewMode(QListView.ViewMode.ListMode)
    grid.setFlow(QListView.Flow.LeftToRight)
    grid.setWrapping(True)
    grid.setResizeMode(QListView.ResizeMode.Adjust)
    grid.setMovement(QListView.Movement.Static)
    grid.setUniformItemSizes(False)
    grid.setGridSize(QSize())
    grid.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
    grid.setItemDelegate(delegate)
    grid.setProperty("showNames", False)
    browser.model.names = False
    grid.doItemsLayout()


def leave(browser: Any) -> None:
    """Back to the square grid exactly as `PhotoBrowser.__init__` set it up."""
    grid = browser.grid
    if getattr(browser, "_fade_delegate", None) is None or grid.itemDelegate() is browser._fade_delegate:
        return
    keep = getattr(browser.model.thumbs, "set_keep_shape", None)
    if keep is not None:
        keep(False)
    grid.setItemDelegate(browser._fade_delegate)
    grid.setViewMode(QListView.ViewMode.IconMode)      # which also resets movement and flow
    grid.setResizeMode(QListView.ResizeMode.Adjust)
    grid.setMovement(QListView.Movement.Static)
    grid.setUniformItemSizes(True)
    grid.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)


def _shape_known(browser: Any, path: str) -> None:
    """A thumbnail landed while Aspect is shown: its shape may move the rows - soon, once."""
    if browser.mode != "aspect" or browser.model.number_of(path) is None:
        return
    if not browser._aspect_settle.isActive():
        browser._aspect_settle.start()


def _settle(browser: Any) -> None:
    """Lay the rows out again with the shapes now known, the top photo kept in place."""
    if browser.mode != "aspect":
        return
    grid = browser.grid
    top = grid.indexAt(QPoint(INSET + 4, INSET + 4))
    offset = grid.visualRect(top).top() if top.isValid() else 0
    browser._aspect_delegate.version += 1
    grid.doItemsLayout()
    if top.isValid():
        grid.scrollTo(top, QAbstractItemView.ScrollHint.PositionAtTop)
        bar = grid.verticalScrollBar()
        bar.setValue(bar.value() - offset)
