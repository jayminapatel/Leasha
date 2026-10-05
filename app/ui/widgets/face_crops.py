r"""Face crops for the naming page: decoded once, kept on disk and in memory.

Layer: L5

2026-10-05, the owner: "the photos flash folder then the picture etc even when
tagging". Every name given re-read the piles, and every pile's face was cut
again from its full-size photo - a 4 MB decode per face, per tag, with a folder
icon showing until it landed. Now:

- **In memory.** A face shown once is a `QPixmap` here for the rest of the
  session, so a pile that is still there after a tag keeps its picture with no
  wait at all. The UI thread only - a `QPixmap` lives there.
- **On disk.** A small square JPEG per face in `<data>/thumbs/faces`, named by a
  hash of the path, the box and the photo's modified time - so a changed photo
  gets a new crop and nothing has to be invalidated. The next session reads a
  few KB instead of decoding the photo.
- **Square.** Every crop is cut to a square, so every tile on the page is the
  same size.
- **Round where it is a person** (Option B item 4): the people grid and the
  "Is this ...?" chips show each face in a circle, as a photo library's
  People page does (`round_pixmap`). "Manage the faces" keeps squares - there
  the face is a thing being selected, and a square shows the selection.

Lives in Leasha's own data folder, never beside the photos (rule 10).
"""

from __future__ import annotations

import hashlib
import os
from collections import OrderedDict
from pathlib import Path
from typing import Any, Optional

from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPixmap

from app.core.logging import logger

__all__ = ["face_key", "cached_face_crop", "remembered", "remember", "blank_tile",
           "square_image", "round_pixmap", "KEEP"]

_log = logger.bind(component="ui.face_crops")

#: Face pixmaps kept in memory, most recently shown last.
KEEP = 2000

_PIXMAPS: "OrderedDict[str, QPixmap]" = OrderedDict()


def face_key(path: str, bbox: Any) -> str:
    """One face, as a key - the same photo and box is the same crop."""
    box = ",".join(str(round(float(v), 1)) for v in tuple(bbox)[:4])
    return f"{path}|{box}"


def square_image(image: Any) -> Any:
    """The centre square of a `QImage` - every tile the same shape."""
    if image is None or image.isNull() or image.width() == image.height():
        return image
    edge = min(image.width(), image.height())
    return image.copy((image.width() - edge) // 2, (image.height() - edge) // 2, edge, edge)


def cached_face_crop(path: str, bbox: Any, cache_dir: Optional[Path]) -> Any:
    """A square face `QImage`, from disk when it was made before. **Worker thread only.**"""
    from PySide6.QtGui import QImage

    from app.ui.thumbnail_loader import decode_face_crop

    target = None
    if cache_dir is not None:
        try:
            mtime = os.stat(path).st_mtime_ns
        except OSError:
            mtime = 0
        name = hashlib.sha1(f"{face_key(path, bbox)}|{mtime}".encode("utf-8", "replace"))
        target = Path(cache_dir) / (name.hexdigest() + ".jpg")
        if target.exists():
            image = QImage(str(target))
            if not image.isNull():
                return image
    image = square_image(decode_face_crop(path, bbox))
    if image is None or image.isNull():
        return None
    if target is not None:
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            image.save(str(target), "JPG", 85)
        except Exception as exc:                    # noqa: BLE001 - a cache, not the photo
            _log.debug("could not keep a face crop for {}: {}", path, exc)
    return image


def remembered(key: str) -> Optional[QPixmap]:
    """The face already shown this session, or None. UI thread."""
    found = _PIXMAPS.get(key)
    if found is not None:
        _PIXMAPS.move_to_end(key)
    return found


def remember(key: str, image: Any) -> Optional[QPixmap]:
    """Keep a decoded face; the pixmap to show, or None when there is nothing. UI thread."""
    if image is None:
        return None
    pixmap = QPixmap.fromImage(image)
    if pixmap.isNull():
        return None
    _PIXMAPS[key] = pixmap
    _PIXMAPS.move_to_end(key)
    while len(_PIXMAPS) > KEEP:
        _PIXMAPS.popitem(last=False)
    return pixmap


_BLANKS: dict[tuple[int, bool], QIcon] = {}
_ROUND: "OrderedDict[int, QPixmap]" = OrderedDict()


def blank_tile(edge: int, *, round_: bool = False) -> QIcon:
    """A soft grey tile - rounded square, or a circle - shown until its picture arrives."""
    found = _BLANKS.get((edge, round_))
    if found is None:
        pixmap = QPixmap(edge, edge)
        pixmap.fill(QColor(0, 0, 0, 0))
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        shape = QPainterPath()
        if round_:
            shape.addEllipse(0, 0, edge, edge)
        else:
            shape.addRoundedRect(0, 0, edge, edge, edge * 0.08, edge * 0.08)
        painter.fillPath(shape, QColor(128, 128, 128, 48))
        painter.end()
        found = _BLANKS[(edge, round_)] = QIcon(pixmap)
    return found


def round_pixmap(pixmap: QPixmap) -> QPixmap:
    """The face in a circle, clear outside it. Made once per pixmap. UI thread."""
    key = pixmap.cacheKey()
    found = _ROUND.get(key)
    if found is not None:
        return found
    edge = min(pixmap.width(), pixmap.height())
    out = QPixmap(edge, edge)
    out.fill(QColor(0, 0, 0, 0))
    painter = QPainter(out)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    circle = QPainterPath()
    circle.addEllipse(0, 0, edge, edge)
    painter.setClipPath(circle)
    painter.drawPixmap(0, 0, pixmap, (pixmap.width() - edge) // 2,
                       (pixmap.height() - edge) // 2, edge, edge)
    painter.end()
    _ROUND[key] = out
    while len(_ROUND) > KEEP:
        _ROUND.popitem(last=False)
    return out
