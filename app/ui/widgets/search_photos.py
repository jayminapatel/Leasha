r"""The Search page's pictures, drawn the way the Photos page draws them.

Layer: L5

Review 2026-10-14, MS-6. The Search grid had a pipeline of its own: every photo
queued at once, a full decode each, 200 kept in memory, a generic file icon
while it waited. It now borrows the Photos page's: `ThumbLoader` (made once,
kept on disk, the newest-asked first, a blurred preview while the sharp one
comes) and `PhotoModel` with its fade-in delegate. Only what is painted is
asked for - a view asks its model for the cells it draws - plus one screen
ahead of the scroll (`ask_ahead`), and a grid or strip that is not on screen
asks for nothing.

**Its own loader, its own folder, its own pool.** A search row carries no file
size, so its thumbnails could never share the Photos page's names anyway; a
folder of their own (`<data>/thumbs/search`) also keeps the two loaders from
writing one blurred-preview pack over the other. The decodes run on the grid's
small pool (`DECODE_THREADS`), behind nothing and in front of nothing.

Used by the grid (`thumbnail_grid.py`) and the photo strip (`photo_strip.py`).
"""

from __future__ import annotations

from pathlib import Path, PurePath
from typing import Any, NamedTuple, Optional, Sequence

from PySide6.QtCore import QModelIndex, QPoint, QSize, Qt, QThreadPool, QTimer
from PySide6.QtWidgets import QAbstractItemView, QListView

from app.ui.thumbnail_loader import is_image_result
from app.ui.widgets.photo_browser import AHEAD, ROLE_ROW, PhotoModel
from app.ui.widgets.photo_thumbs import ThumbLoader

__all__ = ["Photo", "SearchPhotoModel", "shared_loader", "photo_view", "ask_ahead",
           "photos_of", "decode_pool", "DECODE_THREADS", "SEARCH_THUMBS"]

#: Decoders running at once for the Search page's pictures.
DECODE_THREADS = 2
#: The Search page's thumbnails, under the Photos page's own folder.
SEARCH_THUMBS = "search"

_POOL: list = []
_LOADERS: dict = {}


def decode_pool() -> QThreadPool:
    """The Search pictures' own pool, made once and kept by the application."""
    from PySide6.QtCore import QCoreApplication

    if not _POOL:
        pool = QThreadPool(QCoreApplication.instance())
        pool.setMaxThreadCount(DECODE_THREADS)
        _POOL.append(pool)
    return _POOL[0]


def _thumbs_dir(store: Any) -> Optional[Path]:
    """`<data>/thumbs/search`, beside the database - the Photos page's rule."""
    db_path = getattr(store, "db_path", None) if store is not None else None
    if not db_path:
        return None
    return Path(db_path).parent / "thumbs" / SEARCH_THUMBS


def shared_loader(store: Any) -> ThumbLoader:
    """One `ThumbLoader` per thumbnail folder, shared by the grid and the strip
    and kept by the application (the cache outlives any one result set)."""
    from PySide6.QtCore import QCoreApplication

    folder = _thumbs_dir(store)
    key = str(folder or "")
    loader = _LOADERS.get(key)
    try:
        if loader is not None:
            loader.objectName()                    # still alive?
            return loader
    except RuntimeError:
        pass
    loader = ThumbLoader(folder, QCoreApplication.instance(), pool=decode_pool())
    _LOADERS[key] = loader
    return loader


class Photo(NamedTuple):
    """A search row, as the Photos model reads one: `path`, `size_bytes`,
    `mtime_ns` - and the row itself, which is what every signal carries out."""

    path: str
    size_bytes: int
    mtime_ns: int
    row: Any


def photos_of(rows: Sequence[Any]) -> list[Photo]:
    """The pictures among `rows`, in their order, as `Photo`s - once each: a
    picture found by its words and by its look is two rows and one picture."""
    out: list[Photo] = []
    seen: set[str] = set()
    for row in rows:
        path = str(getattr(row, "path", "") or "")
        if not path or path in seen or not is_image_result(getattr(row, "ext", "")):
            continue
        seen.add(path)
        out.append(Photo(path, int(getattr(row, "size_bytes", 0) or 0),
                         int(getattr(row, "mtime_ns", 0) or 0), row))
    return out


class SearchPhotoModel(PhotoModel):
    """`PhotoModel` over search rows: the name under each picture, the path in
    its tooltip; the picture, its fade and its blurred preview as Photos has them."""

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        photo = self.row_at(index.row()) if index.isValid() else None
        if photo is None:
            return None
        if role == Qt.ItemDataRole.DisplayRole:
            return PurePath(photo.path).name if (self.names and index.column() == 0) else None
        if role == Qt.ItemDataRole.ToolTipRole:
            return photo.path
        return super().data(index, role)

    def result_rows(self) -> list[Any]:
        """The search rows shown, in order - the lightbox's siblings."""
        return [photo.row for photo in self.rows()]


def photo_view(model: SearchPhotoModel, edge: int, *, names: bool = True,
               flow_left: bool = False) -> QListView:
    """An icon-mode `QListView` over `model`, painted by the Photos page's delegate."""
    from app.ui.theme import SPACE_XS
    from app.ui.widgets.photo_browser import _FadeDelegate

    view = QListView()
    view.setModel(model)
    view.setViewMode(QListView.ViewMode.IconMode)
    view.setResizeMode(QListView.ResizeMode.Adjust)
    view.setMovement(QListView.Movement.Static)
    view.setUniformItemSizes(True)
    view.setSpacing(SPACE_XS)
    view.setWordWrap(False)
    view.setTextElideMode(Qt.TextElideMode.ElideMiddle)
    view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    view.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
    view.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
    view.setItemDelegate(_FadeDelegate(view))
    model.set_edge(edge)
    model.names = names
    view.setIconSize(QSize(edge, edge))
    line = view.fontMetrics().height() + 10 if names else 8
    view.setGridSize(QSize(edge + 16, edge + line + 8))
    if flow_left:
        view.setFlow(QListView.Flow.LeftToRight)
        view.setWrapping(False)
    timer = QTimer(view)
    timer.setSingleShot(True)
    timer.setInterval(60)
    timer.timeout.connect(lambda: ask_ahead(view, model))
    for bar in (view.verticalScrollBar(), view.horizontalScrollBar()):
        bar.valueChanged.connect(lambda _v: timer.start())
    view._ahead_timer = timer
    return view


def ask_ahead(view: QListView, model: SearchPhotoModel) -> tuple[int, int]:
    """Ask for the screenful past the one shown (and the one before) - `AHEAD`
    screens each way, nearest first. Returns `(first, last)` row on screen, or
    `(-1, -1)` when nothing is (a hidden or empty view asks for nothing)."""
    if not view.isVisible() or model.rowCount() == 0:
        return -1, -1
    viewport = view.viewport().rect()
    top = view.indexAt(QPoint(8, 8))
    if not top.isValid():
        return -1, -1
    corner = QPoint(viewport.width() - 8, viewport.height() - 8)
    bottom = QModelIndex()
    step = max(8, view.gridSize().height() // 4)
    for y in range(corner.y(), 0, -step):
        for x in range(corner.x(), 0, -max(8, view.gridSize().width() // 4)):
            bottom = view.indexAt(QPoint(x, y))
            if bottom.isValid():
                break
        if bottom.isValid():
            break
    last = bottom.row() if bottom.isValid() else top.row()
    span = max(1, last - top.row() + 1)
    model.prefetch(top.row() - span * AHEAD, top.row() - 1)
    model.prefetch(last + 1, last + span * AHEAD)
    return top.row(), last


#: Re-exported for the grid and the strip: the role their rows sit in.
ROLE_PHOTO = ROLE_ROW
