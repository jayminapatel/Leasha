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
already handles the general case (`QImage(path)`), the HEIC/HEIF case (via
`pillow_heif`), and - as of work order 0f §3b, landed the same day as this
file was first written - EXIF orientation too, via its own internal
`_apply_orientation` call. This module used to duplicate that correction
here (a second `read_orientation` call and its own rotate/flip transform,
written before 0f §3b existed); once both landed, a portrait photo was
rotated twice, which for this project's own 90°/270° cases cancels back
out to sideways - fixed by deleting the duplicate and trusting
`decode_image` for the whole of orientation now.

**Never raises.** An unreadable photo is an ordinary state - a truncated
download, a `.jpg` that is really a renamed HTML error page, a file on a
drive that went to sleep - and the grid's answer is a placeholder tile, never
a traceback for a thumbnail somebody scrolled past.
"""

from __future__ import annotations

from typing import Any, Optional

from app.core.logging import logger

__all__ = ["IMAGE_RESULT_EXTS", "is_image_result", "decode_thumbnail",
           "decode_face_crop", "THUMBNAIL_EDGE", "use_store", "picture_bytes"]

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

def _scaled_to_edge(image: Any, edge: int) -> Any:
    """Longest side no bigger than `edge`, aspect kept. Never upscales."""
    from PySide6.QtCore import Qt

    width, height = image.width(), image.height()
    if width <= 0 or height <= 0:
        return image
    longest = max(width, height)
    if longest <= edge:
        return image
    if width >= height:
        return image.scaledToWidth(edge, Qt.TransformationMode.SmoothTransformation)
    return image.scaledToHeight(edge, Qt.TransformationMode.SmoothTransformation)


# ---------------------------------------------------------------------------
# A picture that has no file: a mail attachment, a zip member (2026-10-07)
# ---------------------------------------------------------------------------
#
# The owner: "the pictures from mail in there dont show on list or the preview
# either". A picture that arrived attached to a message is indexed under a
# key, not a place on disk - `pst://2017/2429924/attachments/Manish.jpg` - and
# every decode here handed that key to `QImage(path)`, which answered with
# nothing: a blank tile, a blank preview and a blank face, 1,188 times over.
# Its bytes can be read (`attachment_open.bytes_of`, what the Search preview
# does); that needs the message's row, and so the store.

#: The largest attachment read into memory for a picture. The preview pane's
#: own ceiling is for documents; a photo larger than this is not a thumbnail.
PICTURE_BYTES_LIMIT = 64 * 1024 * 1024

_store_ref: Any = None


def use_store(store: Any) -> None:
    """Tell this module which store names a message's archive. UI thread, once.

    Held weakly: a decoder must not be what keeps a closed index open."""
    import weakref

    global _store_ref
    try:
        _store_ref = weakref.ref(store) if store is not None else None
    except TypeError:                             # a test's plain stand-in
        _store_ref = (lambda: store)


def picture_bytes(path: str) -> Optional[bytes]:
    """The bytes of a picture that is an attachment or a zip member, else None.
    **Worker thread only.** None too when they cannot be read - no store yet,
    the archive held open by Outlook, the message gone - and the reason is
    logged, because "blank" with no word anywhere is how this went unseen."""
    from app.ui.attachment_open import bytes_of, opens_from_a_copy

    if not opens_from_a_copy(path):
        return None
    store = _store_ref() if _store_ref is not None else None
    try:
        message = None
        if store is not None:
            from app.ui.preview_loader import _attachment_parent

            parent = _attachment_parent(store, path)
            message = parent[0] if parent else None
        return bytes_of(path, message, search=False, max_bytes=PICTURE_BYTES_LIMIT)
    except Exception as exc:                      # noqa: BLE001 - a blank tile, said why
        _log.debug("could not read the picture {}: {}", path, exc)
        return None


def _decoded(path: str) -> Optional[Any]:
    """`decode_image` for a file; for a picture with no file, its bytes decoded."""
    from app.ui.attachment_open import opens_from_a_copy
    from app.ui.preview_loader import decode_image, decode_image_data

    if opens_from_a_copy(path):
        data = picture_bytes(path)
        return decode_image_data(data) if data else None
    return decode_image(path)


def decode_thumbnail(path: str, *, edge: int = THUMBNAIL_EDGE) -> Optional[Any]:
    """A small, upright `QImage` for one photo, or `None`. **Worker thread only.**

    Reads pixels via `preview_loader.decode_image` (the same decoder every
    other preview in this application uses, HEIC/HEIF included) and scales
    down. `None` on any failure along the way - a damaged photo is a
    placeholder tile in the grid, not a reason to stop scrolling.

    **Orientation is not re-applied here.** It used to be - this function's
    own `_oriented()` reading `read_orientation` a second time - written
    before `decode_image` gained the identical correction internally (see
    that function's own docstring in `preview_loader.py`). With both in
    place a portrait photo was rotated twice, which for the 90°/270° cases
    this project's own cameras produce cancels back out to sideways - the
    exact bug orientation support exists to fix, reintroduced by fixing it
    in two places that stopped agreeing once both landed. `decode_image` is
    the one place that owns this now.
    """
    try:
        image = _decoded(path)
        if image is None or image.isNull():
            return None

        return _scaled_to_edge(image, edge)
    except Exception as exc:                      # noqa: BLE001 - see docstring
        _log.debug("could not decode a thumbnail for {}: {}", path, exc)
        return None


def decode_face_crop(
    path: str, bbox: "tuple[float, float, float, float]", *,
    edge: int = THUMBNAIL_EDGE, margin: float = 0.35,
) -> Optional[Any]:
    r"""A small, upright `QImage` cropped to one face. **Worker thread only.**

    Work order 0j section 2a's "sample crops" - the Photo Tagger grid shows
    a face, not the whole photo it came from, the same way Google Photos'
    own People grid does. `bbox` is `(x, y, w, h)` in pixels, exactly as
    `app.extract.face_detect.FaceDetection.bbox` and `faces.bbox_*` store it.

    `margin` pads the box by this fraction of its own size on every side
    before cropping - a face detector's box is tight to eyes/nose/mouth, and
    a tight crop reads as a mugshot rather than a recognisable photo of a
    person, which matters for a feature an eight-year-old is meant to use
    unassisted (section 2a's own "designed for an 8-year-old").

    Same H4 contract as `decode_thumbnail`: `None` on any failure - a
    corrupted photo or an out-of-range box (the photo was replaced since
    the face was detected) is a placeholder tile, never a crash.
    """
    try:
        from PySide6.QtCore import QRect

        image = _decoded(path)
        if image is None or image.isNull():
            return None

        x, y, w, h = bbox
        pad_x, pad_y = w * margin, h * margin
        left = max(0, int(x - pad_x))
        top = max(0, int(y - pad_y))
        right = min(image.width(), int(x + w + pad_x))
        bottom = min(image.height(), int(y + h + pad_y))
        if right <= left or bottom <= top:
            return _scaled_to_edge(image, edge)      # a bad box - the whole photo beats nothing
        cropped = image.copy(QRect(left, top, right - left, bottom - top))
        if cropped.isNull():
            return None
        return _scaled_to_edge(cropped, edge)
    except Exception as exc:                      # noqa: BLE001 - see docstring
        _log.debug("could not decode a face crop for {}: {}", path, exc)
        return None
