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

**A blurred preview first** (2026-10-05, Option B item 3). Every thumbnail
made also leaves a 24-pixel copy, kept for the whole library in one file,
`<thumbs>/tiny.pack`, read once when the tab opens. A tile whose thumbnail is
not in memory yet is painted from that copy, soft, and the sharp picture
fades in over it - a photo library's "blur-up", with no change to the index.

Lives in Leasha's own data folder, never beside the photos (rule 10).
"""

from __future__ import annotations

import hashlib
import os
import struct
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
#: The blurred preview's edge, and the file every one of them is kept in.
TINY_EDGE = 24
TINY_PACK = "tiny.pack"
#: New previews made before the pack is written again.
TINY_SAVE_EVERY = 200
_PACK_MAGIC = b"LTP1"


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


def _thumb_and_tiny(path: str, size: int, mtime_ns: int, cache_dir: Optional[Path]) -> Any:
    """`(thumbnail QImage, tiny JPEG bytes)` - the thumbnail as `photo_thumbnail`
    makes it, and its blurred preview encoded here, off the window's thread."""
    from PyQt6.QtCore import QBuffer, QByteArray, QIODevice

    image = photo_thumbnail(path, size, mtime_ns, cache_dir)
    if image is None or image.isNull():
        return image, b""
    edge = min(image.width(), image.height())
    square = image.copy((image.width() - edge) // 2, (image.height() - edge) // 2, edge, edge)
    tiny = square.scaled(TINY_EDGE, TINY_EDGE, Qt.AspectRatioMode.IgnoreAspectRatio,
                         Qt.TransformationMode.SmoothTransformation)
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    tiny.save(buffer, "JPG", 70)
    buffer.close()
    return image, bytes(data)


def read_tiny_pack(path: Path) -> dict[str, bytes]:
    """Every blurred preview kept, by `cache_name`. Empty when there is no pack
    or it is damaged - a cache, never a reason to fail. **Worker thread only.**"""
    out: dict[str, bytes] = {}
    try:
        data = Path(path).read_bytes()
    except OSError:
        return out
    if not data.startswith(_PACK_MAGIC):
        return out
    at = len(_PACK_MAGIC)
    while at + 44 <= len(data):
        name = data[at:at + 40].decode("ascii", "replace")
        (length,) = struct.unpack("<I", data[at + 40:at + 44])
        at += 44
        if at + length > len(data):
            break                                   # cut short - keep what is whole
        out[name + ".jpg"] = data[at:at + length]
        at += length
    return out


def write_tiny_pack(path: Path, tiny: dict[str, bytes]) -> None:
    """Write the pack whole, to a temporary file then swapped in, so a crash
    mid-write leaves the last good pack. **Worker thread only.**"""
    parts = [_PACK_MAGIC]
    for name, data in tiny.items():
        stem = name[:-4] if name.endswith(".jpg") else name
        if len(stem) != 40 or not data:
            continue
        parts.append(stem.encode("ascii") + struct.pack("<I", len(data)) + data)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".tmp")
    temporary.write_bytes(b"".join(parts))
    os.replace(temporary, target)


def _square(pixmap: QPixmap) -> QPixmap:
    """The centre square of the picture, so every cell in a grid is the same
    shape and every name sits on the same line - portrait or landscape.

    2026-10-05, the owner: "can all photos be same size in the view too?".
    This first fitted the whole picture on a clear square, so a landscape
    photo was a thin strip and a portrait one a narrow column. Now each tile
    is filled edge to edge, as a photo library's grid is; the whole picture
    is one double-click away in the viewer, and the Details list and the
    information panel are unchanged.

    Every square is then brought to `CACHE_EDGE`: a wide panorama's short side
    is far smaller than that, and a view never enlarges an icon past its own
    pixels - it showed as a small tile among full ones."""
    edge = min(pixmap.width(), pixmap.height())
    if pixmap.width() != pixmap.height():
        pixmap = pixmap.copy((pixmap.width() - edge) // 2, (pixmap.height() - edge) // 2,
                             edge, edge)
    if edge != CACHE_EDGE:
        pixmap = pixmap.scaled(CACHE_EDGE, CACHE_EDGE, Qt.AspectRatioMode.IgnoreAspectRatio,
                               Qt.TransformationMode.SmoothTransformation)
    return pixmap


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
        #: Blurred previews: encoded, by `cache_name`; and decoded, as shown.
        self._tiny: dict[str, bytes] = {}
        self._tiny_shown: "OrderedDict[str, QPixmap]" = OrderedDict()
        self._tiny_new = 0
        self._read_pack()

    # -- the blurred previews ------------------------------------------------------

    def _pack_path(self) -> Optional[Path]:
        return self._cache_dir / TINY_PACK if self._cache_dir else None

    def _read_pack(self) -> None:
        from app.ui.later import when_done
        from app.ui.workers import CallableWorker, run

        pack = self._pack_path()
        if pack is None:
            return
        worker = CallableWorker(read_tiny_pack, pack, component="ui.photo_thumbs")
        when_done(self, worker, finished=self._pack_read)
        run(self._pool, worker)

    def _pack_read(self, tiny: Any) -> None:
        for name, data in dict(tiny or {}).items():
            self._tiny.setdefault(name, data)        # one made meanwhile is newer

    def tiny(self, path: str, size: int, mtime_ns: int) -> Optional[QPixmap]:
        """The blurred preview for this photo, or None when none was ever made."""
        name = cache_name(path, size, mtime_ns)
        found = self._tiny_shown.get(name)
        if found is not None:
            self._tiny_shown.move_to_end(name)
            return found
        data = self._tiny.get(name)
        if not data:
            return None
        pixmap = QPixmap()
        if not pixmap.loadFromData(data, "JPG"):
            return None
        self._tiny_shown[name] = pixmap
        while len(self._tiny_shown) > KEEP:
            self._tiny_shown.popitem(last=False)
        return pixmap

    def save_tiny(self) -> None:
        """Write the pack on a worker - when enough are new, and as the page closes."""
        from app.ui.workers import CallableWorker, run

        pack = self._pack_path()
        if pack is None or not self._tiny_new:
            return
        self._tiny_new = 0
        worker = CallableWorker(write_tiny_pack, pack, dict(self._tiny),
                                component="ui.photo_thumbs")
        run(self._pool, worker)

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
            name = cache_name(path, size, mtime)
            worker = CallableWorker(_thumb_and_tiny, path, size, mtime, self._cache_dir,
                                    component="ui.photo_thumbs")
            when_done(self, worker,
                      finished=lambda made, p=path, n=name: self._made(p, made, n),
                      failed=lambda _error, p=path: self._made(p, None))
            run(self._pool, worker)

    def _made(self, path: str, made: Any, name: str = "") -> None:
        self._running.discard(path)
        image, tiny = made if isinstance(made, tuple) else (made, b"")
        if tiny and name and name not in self._tiny:
            self._tiny[name] = tiny
            self._tiny_new += 1
            if self._tiny_new >= TINY_SAVE_EVERY:
                self.save_tiny()
        if image is None or image.isNull():
            self._failed.add(path)
        else:
            self._pixmaps[path] = _square(QPixmap.fromImage(image))
            while len(self._pixmaps) > KEEP:
                self._pixmaps.popitem(last=False)
            self.ready.emit(path)
        self._next()
