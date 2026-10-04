r"""HEIC/HEIF pictures readable by Pillow, wherever a picture is opened.

Layer: L2

**Found 2026-10-04 on the owner's photo library.** 3,741 of 15,011 pictures
are `.heic`, and in the index process every one of them failed every model:
"cannot identify image file" from OCR, Florence-2, EXIF, face detection and
CLIP - so a quarter of the library was never described, tagged, scanned for
faces or searchable by picture, and the CLIP lane misreported it as "the
embedding model failed to load". `pillow-heif` was installed; nothing in the
indexer registered it. Only the window's preview did
(`app.ui.preview_loader`), which is why the pictures looked fine there.

Called where pictures are opened - an index run's start and each picture
model's loader - rather than in `app/extract/__init__.py`, which was made
cheap to import on purpose (work order 0r item 2b). Registering twice is
harmless; this does it once per process. Never raises: without the library a
HEIC file is unreadable exactly as before, and nothing else changes.
"""

from __future__ import annotations

from app.core.logging import logger

_log = logger.bind(component="extract.heif")
_registered = False


def register_heif() -> bool:
    """Teach Pillow to open HEIC/HEIF. True when it can."""
    global _registered
    if _registered:
        return True
    try:
        import pillow_heif

        pillow_heif.register_heif_opener()
    except Exception as exc:                       # noqa: BLE001 - optional, as before
        _log.debug("HEIC pictures stay unreadable: {}", exc)
        return False
    _registered = True
    return True
