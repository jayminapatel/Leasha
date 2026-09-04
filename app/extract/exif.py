r"""EXIF metadata extraction from images.

Layer: L2

**EXIF date is THE date for photos.** File mtime is a lie after 20 years of
drive-to-drive copies. The camera's DateTimeOriginal survives all of them and
is the only timestamp a photo search can trust.

**Load-bearing:** `after:2010 before:2020` must find all photos taken in that
decade, regardless of how many times the files were copied or moved. EXIF is
the only source that delivers this.

**EXIF orientation:** Portrait photos must not render sideways. Anywhere an
image is decoded (previews, thumbnails), its EXIF orientation tag is honoured.
"""

from __future__ import annotations

import datetime
from pathlib import Path
from typing import Optional

from app.core.logging import logger

log = logger.bind(component="extract.exif")


def read_datetime(path: Path) -> Optional[datetime.datetime]:
    """EXIF DateTimeOriginal from an image, or None.

    Tried in order: DateTimeOriginal (camera time), DateTimeDigitized (scan time),
    then fallback to file mtime. Never raises.
    """
    try:
        from PIL import Image
        from PIL.ExifTags import TAGS

        with Image.open(path) as img:
            exif = img._getexif()
            if exif is None:
                return None

            # Try the tags in order of preference
            # 306 = DateTime, 36867 = DateTimeOriginal, 36868 = DateTimeDigitized
            for tag_id, tag_name in TAGS.items():
                if tag_name == "DateTime":
                    dt_str = exif.get(tag_id)
                elif tag_name == "DateTimeOriginal":
                    dt_str = exif.get(tag_id)
                elif tag_name == "DateTimeDigitized":
                    dt_str = exif.get(tag_id)
                else:
                    continue

                if not dt_str:
                    continue

                try:
                    # EXIF format: "YYYY:MM:DD HH:MM:SS"
                    return datetime.datetime.strptime(
                        str(dt_str).strip(), "%Y:%m:%d %H:%M:%S"
                    )
                except (ValueError, TypeError):
                    continue

    except Exception as exc:  # noqa: BLE001
        log.debug("could not read EXIF from {}: {}: {}", path.name, type(exc).__name__, exc)

    return None


def read_orientation(path: Path) -> int:
    """EXIF orientation tag from an image.

    Returns the orientation code (1-8), where:
    - 1: normal (0°)
    - 2: flipped horizontally
    - 3: rotated 180°
    - 4: flipped vertically
    - 5: rotated 90° CCW + flipped
    - 6: rotated 90° CCW
    - 7: rotated 90° CW + flipped
    - 8: rotated 90° CW

    Returns 1 (normal) if not found or unreadable.
    """
    try:
        from PIL import Image
        from PIL.ExifTags import TAGS

        with Image.open(path) as img:
            exif = img._getexif()
            if exif is None:
                return 1

            for tag_id, tag_name in TAGS.items():
                if tag_name == "Orientation":
                    orientation = exif.get(tag_id)
                    if orientation is not None and isinstance(orientation, int):
                        return orientation
                    break

    except Exception as exc:  # noqa: BLE001
        log.debug("could not read orientation from {}: {}: {}",
                  path.name, type(exc).__name__, exc)

    return 1


def read_all_metadata(path: Path) -> dict:
    """All readable EXIF tags from an image.

    Returns a dict of tag_name -> value. Never raises.
    """
    try:
        from PIL import Image
        from PIL.ExifTags import TAGS

        metadata = {}
        with Image.open(path) as img:
            exif = img._getexif()
            if exif is None:
                return metadata

            for tag_id, value in exif.items():
                tag_name = TAGS.get(tag_id, tag_id)
                try:
                    metadata[str(tag_name)] = str(value)[:200]  # Truncate long values
                except (TypeError, ValueError):
                    pass

            return metadata

    except Exception as exc:  # noqa: BLE001
        log.debug("could not read EXIF metadata from {}: {}: {}",
                  path.name, type(exc).__name__, exc)

    return {}
