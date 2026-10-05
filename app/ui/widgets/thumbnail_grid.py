r"""A grid of photo thumbnails, beside the results list. Work order 0h §3a.

Layer: L5

**Off by default; the list stays the default view.** The work order is
explicit - a thumbnail grid is a per-surface preference, off-able like every
behaviour this application has, the same way the preview pane and "show
scores" are off until somebody asks for them (`view_options.ViewPreferences`).
`enabled_checkbox` below follows `timeline_strip.enabled_checkbox`'s own
shape, with `default_on = False` where that one is `True` - this is a new
surface, not an established one, and the work order names the off default
explicitly rather than leaving it to guesswork.

**Every thumbnail is decoded on a worker.** `thumbnail_loader.decode_thumbnail`
does the actual work (M11 pattern: `QImage` off the interface thread, wrapped
into a `QPixmap` only once it is back); this widget's job is to hand one
`CallableWorker` per visible photo to the pool and paint whatever comes back,
never to touch a file itself. A cell that fails to decode keeps its
placeholder icon - H4: never a crash, never a blocked scroll.

**A `QListWidget` in icon mode, not a custom model.** `results_view.py`
earned its custom `QListView`/model/delegate because it paints five hundred
richly-formatted rows; a thumbnail grid holds far fewer items (only the
photos in one result set) and every cell is the same shape - an icon and a
filename - which is exactly what `QListWidget`'s own icon mode already does
correctly, without writing a second delegate to get there.
"""

from __future__ import annotations

from collections import OrderedDict
from pathlib import Path
from typing import Any, Optional, Sequence

from PySide6.QtCore import QSize, Qt, QThreadPool, Signal
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import QAbstractItemView, QCheckBox, QListView, QListWidget, QListWidgetItem, QVBoxLayout, QWidget

from app.ui.thumbnail_loader import decode_thumbnail, is_image_result
from app.ui.widgets.file_menu import FileActions, show_for, viewport_point

__all__ = ["ThumbnailGrid", "enabled_checkbox", "GRID_ENABLED_KEY"]

#: `index_state` key for the grid's own on/off switch. Namespaced `ui:`
#: exactly like every other per-surface preference in this file's family
#: (`ui:timeline_strip_enabled`, `ui:pinned_panel_enabled`).
GRID_ENABLED_KEY = "ui:thumbnail_grid_enabled"

#: The cell a thumbnail sits in - bigger than the decoded image
#: (`thumbnail_loader.THUMBNAIL_EDGE`) so the caption line and the grid's own
#: padding both fit without cropping the picture.
THUMB_CELL = 240

#: Qt's `UserRole` slot this widget stores the row payload in - the same
#: convention `result_delegate.ROLE_PAYLOAD` uses on the list view, so
#: anything inspecting "what row is this item" looks in the same place on
#: either view.
ROLE_ROW = int(Qt.ItemDataRole.UserRole)

#: Decoded thumbnails kept, the most recently shown last. A 220px `QImage` is
#: at most ~190 KB, so ~40 MB at worst - and a re-run, a tier swap or a
#: details redraw of the same photos is then no decode at all. Fixed: a grid
#: holds one result set's photos, and nobody would tune this.
CACHE_SIZE = 200
#: Decoders running at once. 2026-10-04, code review: the grid used the
#: global pool, so fifty photos queued ahead of the next search and of every
#: Open. A pool of its own keeps them behind nothing and in front of nothing.
DECODE_THREADS = 2

_CACHE: "OrderedDict[tuple, Any]" = OrderedDict()
_POOL: list = []


def decode_pool() -> QThreadPool:
    """The grids' own pool, made once and kept by the application - a pool
    owned by a grid would wait for its decodes when the grid is destroyed,
    on the interface thread."""
    from PySide6.QtCore import QCoreApplication

    if not _POOL:
        pool = QThreadPool(QCoreApplication.instance())
        pool.setMaxThreadCount(DECODE_THREADS)
        _POOL.append(pool)
    return _POOL[0]


def cache_key(row: Any) -> tuple:
    """`(path, mtime, size)` from what the row already carries - no stat. A
    file re-indexed after it changed has a new mtime, so a new key."""
    return (str(getattr(row, "path", "") or ""), int(getattr(row, "mtime_ns", 0) or 0),
            int(getattr(row, "size_bytes", 0) or 0))


def cached(key: tuple) -> Any:
    """The decoded `QImage` for `key`, or None. UI thread only."""
    image = _CACHE.get(key)
    if image is not None:
        _CACHE.move_to_end(key)
    return image


def remember(key: tuple, image: Any) -> None:
    """Keep a decoded thumbnail, dropping the least recently shown. UI thread."""
    _CACHE[key] = image
    _CACHE.move_to_end(key)
    while len(_CACHE) > CACHE_SIZE:
        _CACHE.popitem(last=False)


def enabled_checkbox(store: Any, *, on_toggle: Any) -> QCheckBox:
    """The "Thumbnail grid" on/off switch. Off by default - work order §3a.

    Same shape as `timeline_strip.enabled_checkbox` and
    `pinned_panel.enabled_checkbox` - read once from `index_state`, never
    raising against a locked or missing database - but `default_on = False`:
    this is a new, opt-in surface, and the list stays the default the work
    order names explicitly.
    """
    box = QCheckBox("Thumbnail grid")
    box.setToolTip(
        "Show image-heavy results as thumbnails instead of a list. Off by "
        "default; your other results still show as a list either way."
    )
    default_on = False
    try:
        raw = store.get_state(GRID_ENABLED_KEY, None) if store is not None else None
    except Exception:                            # noqa: BLE001 - a preference
        raw = None
    box.setChecked(default_on if raw is None else raw not in ("off", "0", "false"))
    box.toggled.connect(on_toggle)
    return box


class ThumbnailGrid(QWidget):
    """Photos from the current result set, as thumbnails."""

    #: A thumbnail was opened (double-click / Enter). Carries the row and the
    #: full, ordered list of photo rows currently in the grid - the lightbox
    #: (`PreviewWindow`, work order §3b) uses the second argument for
    #: next/previous, so the grid is the one place that already has the
    #: ordering and does not need to reconstruct it.
    opened = Signal(object, list)
    #: The menu's "Open": the file itself, as Open does on every other page
    #: (2026-10-04). Double-click and Enter stay the lightbox (`opened`), and
    #: the menu offers that as "View".
    file_requested = Signal(object)
    reveal_requested = Signal(object)
    pin_requested = Signal(object)
    #: "More like this" from a photo's right-click menu - work order §2d.
    similar_requested = Signal(object)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._rows: list[Any] = []
        #: Bumped every time `show_rows` replaces the set, so a slow decode
        #: for a photo that has since scrolled out of an *earlier* result set
        #: never paints over a cell that now belongs to something else - the
        #: same generation-stamp discipline `render_page`'s callers already
        #: use, applied here because this widget can fire dozens of workers
        #: for one result set rather than one.
        self._generation = 0
        self._pool = decode_pool()
        #: 2026-10-04, code review: the decodes not yet started, taken back
        #: (`QThreadPool.tryTake`) when the set changes or the grid hides; the
        #: photos the last `show_rows` drew, so an identical repaint of the
        #: list redraws nothing; and whether decoding waits to be shown.
        self._queued: list[Any] = []
        self._shown_keys: tuple = ()
        self._waiting = False

        self._list = QListWidget(self)
        self._list.setViewMode(QListView.ViewMode.IconMode)
        self._list.setIconSize(QSize(
            THUMB_CELL - 20, THUMB_CELL - 20))
        self._list.setGridSize(QSize(THUMB_CELL, THUMB_CELL))
        self._list.setResizeMode(QListView.ResizeMode.Adjust)
        self._list.setMovement(QListView.Movement.Static)
        self._list.setUniformItemSizes(True)
        self._list.setSpacing(6)
        self._list.setWordWrap(True)
        self._list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._list.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._list.itemActivated.connect(self._on_activated)
        self._list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._list.customContextMenuRequested.connect(self._on_context_menu)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._list)

    # -- populating -----------------------------------------------------------

    def show_rows(self, rows: Sequence[Any]) -> None:
        """The current result set. Only the photos in it are shown.

        Called from `ResultsView.rows_changed`, which already fires on every
        search and every federated append - the grid needs no search-specific
        wiring of its own, it just filters what the list already has.

        2026-10-04, code review: `rows_changed` fires on every repaint of the
        list, and each one started a decode per photo - no cache, no cancel,
        and the grid usually not on screen. Now the same photos are not
        redrawn, a decoded one comes from the cache, and the rest are decoded
        only while the grid is visible (`showEvent`).
        """
        photos = [row for row in rows if is_image_result(getattr(row, "ext", ""))]
        keys = tuple(cache_key(row) for row in photos)
        if keys and keys == self._shown_keys and self._list.count() == len(photos):
            self._rows = photos                  # the newest row objects, same photos
            for index, row in enumerate(photos):
                self._list.item(index).setData(ROLE_ROW, row)
            return
        self._cancel_queued()
        self._generation += 1
        generation = self._generation
        self._rows = photos
        self._shown_keys = keys
        self._list.clear()

        placeholder = self.style().standardIcon(self.style().StandardPixmap.SP_FileIcon)
        for row in self._rows:
            item = QListWidgetItem(placeholder, Path(str(getattr(row, "path", "") or "")).name)
            item.setData(ROLE_ROW, row)
            item.setToolTip(str(getattr(row, "path", "") or ""))
            self._list.addItem(item)

        self._load_thumbnails(generation)

    def clear(self) -> None:
        self._cancel_queued()
        self._generation += 1
        self._rows = []
        self._shown_keys = ()
        self._list.clear()

    def image_rows(self) -> list[Any]:
        """The photos currently shown, in grid order - the lightbox's sibling list."""
        return list(self._rows)

    # -- decoding, on workers ---------------------------------------------------

    def _load_thumbnails(self, generation: int) -> None:
        """One `CallableWorker` per photo. Never decodes here, on the UI thread.

        2026-10-04, code review: only for a photo not in the cache, on the
        grid's own pool, and not while the grid is hidden - `showEvent`
        starts what waited."""
        from app.ui.workers import CallableWorker, run

        self._waiting = not self.isVisible()
        for index, row in enumerate(self._rows):
            path = str(getattr(row, "path", "") or "")
            if not path:
                continue
            key = cache_key(row)
            image = cached(key)
            if image is not None:
                self._thumbnail_ready(index, image, generation)
                continue
            if self._waiting:
                continue
            worker = CallableWorker(decode_thumbnail, path, component="ui.thumbnail_grid")
            worker.signals.finished.connect(
                lambda image, i=index, g=generation, k=key: self._thumbnail_ready(i, image, g, k))
            worker.signals.failed.connect(
                lambda _error, i=index, g=generation: self._thumbnail_ready(i, None, g))
            worker.signals.done.connect(lambda w=worker: self._forget(w))
            self._queued.append(worker)
            run(self._pool, worker)

    def _forget(self, worker: Any) -> None:
        if worker in self._queued:
            self._queued.remove(worker)

    def _cancel_queued(self) -> None:
        """Take back every decode that has not started. One already running
        finishes, and its generation stamp drops it."""
        from app.ui.workers import release

        for worker in list(self._queued):
            try:
                if self._pool.tryTake(worker):
                    release(worker)              # it will never say it is done
            except RuntimeError:                 # already finished and deleted
                pass
        self._queued.clear()

    def showEvent(self, event: Any) -> None:     # noqa: N802 - Qt's naming
        super().showEvent(event)
        if self._waiting and self._rows:
            self._load_thumbnails(self._generation)

    def hideEvent(self, event: Any) -> None:     # noqa: N802 - Qt's naming
        super().hideEvent(event)
        if self._queued:
            # Off screen: what has not started waits for the next show.
            self._cancel_queued()
            self._waiting = True

    def _thumbnail_ready(self, index: int, image: Any, generation: int,
                         key: Optional[tuple] = None) -> None:
        """UI thread. Wraps the worker's `QImage`; never decodes anything itself."""
        if generation != self._generation:
            return                               # a later result set won
        if image is not None and key is not None:
            remember(key, image)
        if index < 0 or index >= self._list.count():
            return
        item = self._list.item(index)
        if item is None:
            return
        if image is None:
            return                               # H4: keep the placeholder icon
        pixmap = QPixmap.fromImage(image)
        if pixmap.isNull():
            return
        item.setIcon(QIcon(pixmap))

    # -- interaction --------------------------------------------------------

    def _row_at(self, item: Optional[QListWidgetItem]) -> Optional[Any]:
        return item.data(ROLE_ROW) if item is not None else None

    def _on_activated(self, item: QListWidgetItem) -> None:
        row = self._row_at(item)
        if row is not None:
            self.opened.emit(row, self.image_rows())

    def _on_context_menu(self, point: Any) -> None:
        item = self._list.itemAt(viewport_point(self._list, point))
        row = self._row_at(item)
        if row is None:
            selected = self._list.selectedItems()
            row = self._row_at(selected[0]) if selected else None
        if row is None:
            return

        show_for(self._list, point, str(getattr(row, "path", "") or ""), FileActions(
            open_file=lambda: self.file_requested.emit(row),
            view=lambda: self.opened.emit(row, self.image_rows()),
            reveal=lambda: self.reveal_requested.emit(row), row=row,
            pin=lambda: self.pin_requested.emit(row),
            similar=lambda: self.similar_requested.emit(row),
        ))
