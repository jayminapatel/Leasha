r"""One decode per photo, shared by every picture model that reads it.

Layer: L2

2026-10-05, measured on the owner's photos: while a photo is read, faces,
picture search (CLIP) and the duplicate fingerprint (pHash) each opened and
decoded it again - 0.15 s a time for a HEIC, 0.08 s for a JPEG. They run one
after another on the same photo, so the last decoded picture is kept here and
the next model takes it instead of decoding again.

**Upright.** The picture is turned the way its EXIF orientation says, as
OpenCV's `IMREAD_COLOR` did for faces - and as CLIP never was: `fastembed`
opens a path without turning it, so a phone's portrait photo was embedded
lying on its side.

Two pictures are kept, keyed by path, size and modified time, so a changed
file is never answered from the cache. Thread-safe; never raises.

*2026-10-14, the conductor's face pool (order 5d):* with several photos looked
at side by side, each thread's photo must still be here when its next model
asks, so the run raises the count to the pool's size plus one
(`keep_at_least`) and puts it back at its end (`keep_default`).
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any, Optional

from app.core.logging import logger

__all__ = ["decoded", "upright", "clear", "KEEP", "small_copy", "keep_at_least", "keep_default",
           "kept_limit"]

log = logger.bind(component="extract.picture")

#: Pictures kept decoded. Two: the models of one photo run back to back, and a
#: full-size 12-megapixel picture is about 36 MB.
KEEP = 2

_lock = threading.Lock()
_kept: "OrderedDict[tuple, Any]" = OrderedDict()
#: How many are kept right now: `KEEP`, or more while photos are looked at
#: side by side (`keep_at_least`).
_limit = KEEP


def keep_at_least(count: int) -> int:
    """Keep at least `count` decoded pictures - one per thread reading photos
    side by side, plus one. Never fewer than `KEEP`. Returns the limit now."""
    global _limit
    with _lock:
        _limit = max(KEEP, int(count or KEEP))
        return _limit


def keep_default() -> None:
    """Back to `KEEP`, dropping the oldest beyond it."""
    global _limit
    with _lock:
        _limit = KEEP
        while len(_kept) > _limit:
            _kept.popitem(last=False)


def kept_limit() -> int:
    """How many decoded pictures are kept now."""
    return _limit


def _key(path: Path) -> Optional[tuple]:
    try:
        stat = path.stat()
    except OSError:
        return None
    return (str(path), stat.st_size, stat.st_mtime_ns)


#: EXIF orientation -> the `Image.Transpose` that undoes it (`upright`).
_ORIENTATION = {2: "FLIP_LEFT_RIGHT", 3: "ROTATE_180", 4: "FLIP_TOP_BOTTOM",
                5: "TRANSPOSE", 6: "ROTATE_270", 7: "TRANSVERSE", 8: "ROTATE_90"}


def upright(image: Any) -> Any:
    """`image` turned the way its EXIF says it was taken. Never raises.

    2026-10-26, the owner's run: "face detection failed on Maya_MedicalCard_2015.jpg:
    TypeError: can't concat str to bytes". `ImageOps.exif_transpose` writes the
    EXIF back out after turning the picture (`Exif.tobytes`), and Pillow cannot
    write a malformed tag it read - so a scanned card with odd EXIF lost its
    faces and its picture search, and showed no thumbnail. The turn itself needs
    only the orientation tag, so on that failure it is done here, by hand."""
    from PIL import Image, ImageOps

    try:
        return ImageOps.exif_transpose(image)
    except Exception:                               # noqa: BLE001 - see above
        pass
    try:
        orientation = int(image.getexif().get(0x0112, 1) or 1)
    except Exception:                               # noqa: BLE001 - unreadable EXIF: as it is
        return image
    method = _ORIENTATION.get(orientation)
    return image.transpose(getattr(Image.Transpose, method)) if method else image


def decoded(path: Any) -> Optional[Any]:
    """The photo as an upright RGB `PIL.Image`, decoded once. None if unreadable.

    The image returned is shared: callers read it and never change it in place
    (`copy()` first if they must)."""
    path = Path(path)
    key = _key(path)
    if key is not None:
        with _lock:
            found = _kept.get(key)
            if found is not None:
                _kept.move_to_end(key)
                return found
    try:
        from PIL import Image

        from app.extract.heif import register_heif

        register_heif()
        with Image.open(path) as opened:
            picture = upright(opened)
            picture = picture.convert("RGB")
            picture.load()
    except Exception as exc:                        # noqa: BLE001 - unreadable, as before
        log.debug("could not decode {}: {}: {}", path.name, type(exc).__name__, exc)
        return None
    if key is not None:
        with _lock:
            _kept[key] = picture
            while len(_kept) > _limit:
                _kept.popitem(last=False)
    return picture


def small_copy(picture: Any, shortest: int = 256) -> Any:
    """A copy whose shorter side is `shortest` (or the picture's own, if smaller).

    For a model that resizes to 224 anyway (CLIP): what it is given waiting in
    a batch costs a few hundred kilobytes, not tens of megabytes."""
    width, height = picture.size
    side = min(width, height)
    if side <= shortest:
        return picture.copy()
    scale = shortest / side
    from PIL import Image

    return picture.resize((max(1, round(width * scale)), max(1, round(height * scale))),
                          Image.Resampling.BICUBIC)


def clear() -> None:
    """Forget every kept decode (tests, and a run's end, so memory goes back)."""
    with _lock:
        _kept.clear()
