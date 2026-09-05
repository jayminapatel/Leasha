r"""Decode one photo to a small, orientation-correct `QImage`. **Worker thread only.**

Layer: L5. Work order 0h §3a.

**The M11 pattern, applied to a thumbnail.** `render_page.py` and
`preview_loader.decode_image` already established the rule this module follows:
`QImage` decodes off the interface thread; `QPixmap` cannot be built there at
all on some platforms and Qt refuses on the rest. A thumbnail grid decodes one
photo per cell, and doing that inline while a list of five hundred results
scrolls past is exactly the freeze non-negotiable #5 exists to prevent - so
every function here is meant to be handed to `app.ui.workers.CallableWorker`,
never called from a paint method or a slot that runs on the UI thread.

**`decode_image` is reused, not reimplemented.** `preview_loader.decode_image`
already handles the general case (`QImage(path)`) and the HEIC/HEIF case (via
`pillow_heif`) - the two-line MIME-sniffing free-for-all every image previewer
in this application already needs. This module adds exactly one thing on top:
EXIF orientation, which nothing in the codebase applied before work order 0h
(`app.extract.exif.read_orientation` existed and was called from nowhere -
`grep -rn "read_orientation" app/` before this file was written returned only
its own definition).

**Never raises.** An unreadable photo is an ordinary state - a truncated
download, a `.jpg` that is really a renamed HTML error page, a file on a
drive that went to sleep - and the grid's answer is a placeholder tile, never
a traceback for a thumbnail somebody scrolled past.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from app.core.logging import logger

__all__ = ["IMAGE_RESULT_EXTS", "is_image_result", "decode_thumbnail",
           "THUMBNAIL_EDGE"]

_log = logger.bind(component="ui.thumbnail")

#: **Kept equal to `OcrExtractor.extensions` in `app/extract/ocr.py`** - the
#: same rule `preview_loader._IMAGE_SUFFIXES` already states for the same
#: reason: this is the indexer's own definition of "an image", and a
#: thumbnail grid using a different list would show a grid that disagrees
#: with what CLIP actually indexed. Dot-stripped and lowercase, because that
#: is the normalised form `SearchResult.ext`/`ResultRow.ext` already carry
#: (`_to_result` in `app/search/engine.py` strips the leading dot).
def _image_extensions() -> frozenset:
    try:
        from app.extract.ocr import OcrExtractor

        return frozenset(ext.lstrip(".").lower() for ext in OcrExtractor.extensions)
    except Exception:                            # noqa: BLE001 - see docstring
        # A hardcoded fallback, identical to the extractor's own list at the
        # time this was written - so a broken import degrades to "the set as
        # it was", never to "no thumbnails at all".
        return frozenset({
            "png", "jpg", "jpeg", "tif", "tiff", "bmp", "webp", "gif",
            "heic", "heif", "svg",
        })


IMAGE_RESULT_EXTS = _image_extensions()

#: SVG is in `OcrExtractor.extensions` (OCR can read text baked into one as a
#: raster) but is not a photo - CLIP never embeds it (work order 0h §1a is
#: the vision tower over ladder-passed raster images) and Qt's raster decoder
#: cannot thumbnail it the way this module thumbnails everything else.
#: Excluded here, specifically, so the grid does not show a placeholder tile
#: for every diagram in a results set - it simply does not offer the grid
#: view for filetypes CLIP never saw.
_NOT_A_PHOTO = frozenset({"svg"})


def is_image_result(ext: str) -> bool:
    """Is a `SearchResult`/`ResultRow` with this extension a photo?

    Ext is expected already normalised - lower-case, no leading dot - which is
    the form both `SearchResult.ext` and `ResultRow.ext` already carry.
    Nothing else on either dataclass marks a row as coming from the CLIP
    image lane (see `_result_chunk_id`'s docstring in `app/search/engine.py`
    for why not); the extension is the only signal a UI consumer has, and it
    is the same signal the ladder itself used to decide whether to embed the
    file in the first place.
    """
    cleaned = str(ext or "").strip().lower().lstrip(".")
    return bool(cleaned) and cleaned in IMAGE_RESULT_EXTS and cleaned not in _NOT_A_PHOTO


#: The longest edge a thumbnail is decoded to. Large enough to look sharp at
#: the grid's own cell size (see `THUMB_CELL` in `widgets/thumbnail_grid.py`)
#: on a high-DPI screen, small enough that a hundred cells is not a hundred
#: full-resolution decodes sitting in memory at once.
THUMBNAIL_EDGE = 220

#: EXIF orientation -> (clockwise rotation degrees, mirror horizontally
#: first). The four codes a camera or phone actually produces (1, 3, 6, 8)
#: are exact; 2/4/5/7 are the rarer flipped variants a scanner or a mirrored
#: capture can produce and are composed the same way every other
#: implementation of this table does - flip, then rotate.
_ORIENTATION_TRANSFORM: dict[int, tuple[int, bool]] = {
    1: (0, False),
    2: (0, True),
    3: (180, False),
    4: (180, True),
    5: (90, True),
    6: (90, False),
    7: (270, True),
    8: (270, False),
}


def _oriented(image: Any, orientation: int) -> Any:
    """Rotate/flip a decoded `QImage` so it displays upright. Never raises."""
    from PyQt6.QtGui import QTransform

    angle, mirror = _ORIENTATION_TRANSFORM.get(int(orientation or 1), (0, False))
    if not angle and not mirror:
        return image
    transform = QTransform()
    if mirror:
        transform.scale(-1, 1)
    if angle:
        transform.rotate(angle)
    return image.transformed(transform)


def _scaled_to_edge(image: Any, edge: int) -> Any:
    """Longest side no bigger than `edge`, aspect kept. Never upscales."""
    from PyQt6.QtCore import Qt

    width, height = image.width(), image.height()
    if width <= 0 or height <= 0:
        return image
    longest = max(width, height)
    if longest <= edge:
        return image
    if width >= height:
        return image.scaledToWidth(edge, Qt.TransformationMode.SmoothTransformation)
    return image.scaledToHeight(edge, Qt.TransformationMode.SmoothTransformation)


def decode_thumbnail(path: str, *, edge: int = THUMBNAIL_EDGE) -> Optional[Any]:
    """A small, upright `QImage` for one photo, or `None`. **Worker thread only.**

    Reads pixels via `preview_loader.decode_image` (the same decoder every
    other preview in this application uses, HEIC/HEIF included), reads the
    orientation via `app.extract.exif.read_orientation`, rotates/flips to
    match, then scales down. `None` on any failure along the way - a damaged
    photo is a placeholder tile in the grid, not a reason to stop scrolling.
    """
    try:
        from app.ui.preview_loader import decode_image

        image = decode_image(path)
        if image is None or image.isNull():
            return None

        try:
            from app.extract.exif import read_orientation

            orientation = read_orientation(Path(path))
        except Exception as exc:                  # noqa: BLE001 - see docstring
            _log.debug("no orientation for {}: {}", path, exc)
            orientation = 1

        image = _oriented(image, orientation)
        return _scaled_to_edge(image, edge)
    except Exception as exc:                      # noqa: BLE001 - see docstring
        _log.debug("could not decode a thumbnail for {}: {}", path, exc)
        return None
