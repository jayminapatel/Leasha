r"""Thumbnails for the Photos page: made once, kept on disk, loaded newest-asked first.

Layer: L5

2026-10-05. The search grid's `decode_thumbnail` decodes every photo at full
size each time it is shown - fine for the twenty pictures in a result list,
far too slow for a library of 15,000 scrolled through. Here:

- **On disk, once.** A 320-pixel JPEG per picture in `<data>/thumbs`, named by
  a hash of the path, size and modified time - so a changed photo gets a new
  one and nothing has to be invalidated. Scrolling back over the library reads
  a 20 KB file instead of decoding a 4 MB one.
- **Fast first decode.** A JPEG is decoded with Pillow's draft mode, at an
  eighth of its size when that is still big enough - most of the cost of a
  camera photo is in pixels a thumbnail never shows. Everything else (HEIC
  included) goes through `preview_loader.decode_image`, the decoder every
  preview uses, which also turns it upright.
- **What is on screen first.** Requests are a stack, not a queue: scroll past
  five hundred photos and the ones now visible are made next, not after the
  five hundred. Four at a time, on the global pool.

Lives in Leasha's own data folder, never beside the photos (rule 10).
"""

from __future__ import annotations

import hashlib
from collections import OrderedDict
from pathlib import Path
from typing import Any, Optional

from PyQt6.QtCore import QObject, Qt, QThreadPool, pyqtSignal
from PyQt6.QtGui import QPixmap

from app.core.logging import logger

__all__ = ["ThumbLoader", "photo_thumbnail", "CACHE_EDGE", "cache_name"]

_log = logger.bind(component="ui.photo_thumbs")

#: The edge kept on disk - big enough for the Large view, small enough to read
#: in a millisecond or two.
CACHE_EDGE = 320
#: Thumbnails made at once.
AT_ONCE = 4
#: Pixmaps kept in memory, most recently shown last.
KEEP = 1500
#: Requests waiting beyond this are dropped, oldest first - they scrolled past.
MAX_WAITING = 600


def cache_name(path: str, size: int, mtime_ns: int) -> str:
    key = f"{path}|{int(size)}|{int(mtime_ns)}".encode("utf-8", "replace")
    return hashlib.sha1(key).hexdigest() + ".jpg"


def _fast_jpeg(path: str, edge: int) -> Any:
    """A JPEG decoded small with draft mode, upright, as a `QImage`; None when
    this is not a JPEG Pillow can draft."""
    from PIL import Image, ImageOps

    with Image.open(path) as opened:
        if opened.format != "JPEG":
            return None
        opened.draft("RGB", (edge, edge))
        image = ImageOps.exif_transpose(opened.convert("RGB"))
    image.thumbnail((edge, edge))
    from PyQt6.QtGui import QImage

    data = image.tobytes("raw", "RGB")
    return QImage(data, image.width, image.height, image.width * 3,
                  QImage.Format.Format_RGB888).copy()


def photo_thumbnail(path: str, size: int, mtime_ns: int, cache_dir: Optional[Path],
                    edge: int = CACHE_EDGE) -> Any:
    """A `QImage` no bigger than `edge`, or None. **Worker thread only.**"""
    from PyQt6.QtGui import QImage

    target = Path(cache_dir) / cache_name(path, size, mtime_ns) if cache_dir else None
    if target is not None and target.exists():
        image = QImage(str(target))
        if not image.isNull():
            return image
    image = None
    if Path(path).suffix.lower() in (".jpg", ".jpeg", ".jpe"):
        try:
            image = _fast_jpeg(path, edge)
        except Exception as exc:                    # noqa: BLE001 - falls back below
            _log.debug("draft decode failed for {}: {}", path, exc)
    if image is None:
        from app.ui.thumbnail_loader import decode_thumbnail

        image = decode_thumbnail(path, edge=edge)
    if image is None or image.isNull():
        return None
    if image.width() > edge or image.height() > edge:
        image = image.scaled(edge, edge, Qt.AspectRatioMode.KeepAspectRatio,
                             Qt.TransformationMode.SmoothTransformation)
    if target is not None:
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            image.save(str(target), "JPG", 82)
        except Exception as exc:                    # noqa: BLE001 - a cache, not the photo
            _log.debug("could not keep a thumbnail for {}: {}", path, exc)
    return image


def _square(pixmap: QPixmap) -> QPixmap:
    """The picture centred on a clear square, so every cell in a grid is the
    same shape and every name sits on the same line - portrait or landscape."""
    from PyQt6.QtGui import QColor, QPainter

    edge = max(pixmap.width(), pixmap.height())
    if pixmap.width() == pixmap.height():
        return pixmap
    canvas = QPixmap(edge, edge)
    canvas.fill(QColor(0, 0, 0, 0))
    painter = QPainter(canvas)
    painter.drawPixmap((edge - pixmap.width()) // 2, (edge - pixmap.height()) // 2, pixmap)
    painter.end()
    return canvas


class ThumbLoader(QObject):
    """Ask for a photo's thumbnail; `ready(path)` says when it can be had."""

    ready = pyqtSignal(str)

    def __init__(self, cache_dir: Optional[Path], parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._cache_dir = Path(cache_dir) if cache_dir else None
        self._pixmaps: "OrderedDict[str, QPixmap]" = OrderedDict()
        self._waiting: "OrderedDict[str, tuple[int, int]]" = OrderedDict()
        self._running: set[str] = set()
        self._failed: set[str] = set()
        self._pool = QThreadPool.globalInstance()

    def pixmap(self, path: str) -> Optional[QPixmap]:
        found = self._pixmaps.get(path)
        if found is not None:
            self._pixmaps.move_to_end(path)
        return found

    def request(self, path: str, size: int, mtime_ns: int) -> None:
        """Make `path`'s thumbnail soon - before anything asked for earlier."""
        if path in self._pixmaps or path in self._running or path in self._failed:
            return
        self._waiting.pop(path, None)
        self._waiting[path] = (int(size), int(mtime_ns))
        while len(self._waiting) > MAX_WAITING:
            self._waiting.popitem(last=False)
        self._next()

    def forget_waiting(self) -> None:
        """A new list: whatever was asked for before no longer matters."""
        self._waiting.clear()

    def _next(self) -> None:
        from app.ui.later import when_done
        from app.ui.workers import CallableWorker, run

        while self._waiting and len(self._running) < AT_ONCE:
            path, (size, mtime) = self._waiting.popitem(last=True)
            self._running.add(path)
            worker = CallableWorker(photo_thumbnail, path, size, mtime, self._cache_dir,
                                    component="ui.photo_thumbs")
            when_done(self, worker, finished=lambda image, p=path: self._made(p, image),
                      failed=lambda _error, p=path: self._made(p, None))
            run(self._pool, worker)

    def _made(self, path: str, image: Any) -> None:
        self._running.discard(path)
        if image is None or image.isNull():
            self._failed.add(path)
        else:
            self._pixmaps[path] = _square(QPixmap.fromImage(image))
            while len(self._pixmaps) > KEEP:
                self._pixmaps.popitem(last=False)
            self.ready.emit(path)
        self._next()
