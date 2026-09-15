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


#: The date tags this reads, **in the order they are preferred**. Work order
#: 0f §3a names `DateTimeOriginal` specifically, and the order is the item:
#:
#:   * `36867 DateTimeOriginal` - when the shutter fired. Cameras write it once
#:     and nothing rewrites it, which is the entire reason EXIF is trusted here
#:     over the file's own mtime.
#:   * `36868 DateTimeDigitized` - when a scan or an import was made. The right
#:     answer for a scanned print, whose shutter date nothing recorded.
#:   * `306 DateTime` - "last modified", and **the trap**. Photo software
#:     rewrites it whenever it saves the file, so a 2006 photograph opened and
#:     re-saved in 2019 carries a 2019 `DateTime` beside its untouched 2006
#:     `DateTimeOriginal`. Preferring it would reintroduce, inside EXIF, the
#:     very copy-date bug that reading EXIF exists to escape - so it is last,
#:     and used only when it is the only date the file has.
_DATE_TAGS: tuple[int, ...] = (36867, 36868, 306)


def read_datetime(path: Path) -> Optional[datetime.datetime]:
    r"""EXIF DateTimeOriginal from an image, or None. Never raises.

    Tried in `_DATE_TAGS` order: DateTimeOriginal (camera time), then
    DateTimeDigitized (scan time), then DateTime (last modified).

    **This used to walk `TAGS` instead, and got the order backwards.** The
    loop iterated `PIL.ExifTags.TAGS` - a dict keyed by tag *number* - and
    returned the first of the three it met, so tag 306 (`DateTime`) was
    always reached before 36867 (`DateTimeOriginal`). A photo carrying both
    resolved to its last-saved date while the docstring above it promised
    the shot date. Measured before the fix: a fixture with
    `DateTimeOriginal=2006:06:15` and `DateTime=2019:03:01` returned
    2019-03-01. The preference is now stated as data and iterated directly,
    so the order is the tuple above rather than an artefact of how Pillow
    happens to number its tags.
    """
    try:
        from PIL import Image

        with Image.open(path) as img:
            exif = img._getexif()
            if exif is None:
                return None

            for tag_id in _DATE_TAGS:
                dt_str = exif.get(tag_id)
                if not dt_str:
                    continue
                try:
                    # EXIF format: "YYYY:MM:DD HH:MM:SS"
                    return datetime.datetime.strptime(
                        str(dt_str).strip(), "%Y:%m:%d %H:%M:%S"
                    )
                except (ValueError, TypeError):
                    # A malformed value in the preferred tag must not hide a
                    # good one in the next: a camera that wrote "0000:00:00
                    # 00:00:00" into DateTimeOriginal is common enough that
                    # falling through matters.
                    continue

    except Exception as exc:  # noqa: BLE001
        log.debug("could not read EXIF from {}: {}: {}", path.name, type(exc).__name__, exc)

    return None


#: The EXIF tag holding the whole GPS IFD as a nested dict, and the four
#: sub-tags this needs out of it. Work order 0i section 4a.
_GPS_IFD_TAG = 34853
_GPS_LAT_REF, _GPS_LAT, _GPS_LON_REF, _GPS_LON = 1, 2, 3, 4


def _dms_to_degrees(dms) -> float:
    """`((d,1),(m,1),(s,100))`-shaped EXIF rationals to decimal degrees.

    Pillow exposes each of degrees/minutes/seconds as either a plain number
    or an `IFDRational` (itself numerator/denominator) depending on Pillow
    version and how the file was written - `float()` handles both without
    this module needing to know which.
    """
    degrees, minutes, seconds = (float(part) for part in dms)
    return degrees + minutes / 60.0 + seconds / 3600.0


def read_gps(path: Path) -> Optional[tuple[float, float]]:
    r"""A photo's EXIF GPS coordinates as `(latitude, longitude)`, or None.

    Work order 0i section 4a. Never raises - the same H4 contract as
    `read_datetime` beside it: a photo with no GPS block, a corrupt one, or
    one Pillow cannot parse costs this one photo its place, never the run.

    South and West are negative, per the ordinary decimal-degrees
    convention `reverse_geocoder` (and everything else) expects - EXIF
    itself stores the *reference* (`N`/`S`, `E`/`W`) and an unsigned
    magnitude separately, which is the one translation this function does.
    """
    try:
        from PIL import Image

        with Image.open(path) as img:
            exif = img._getexif()
            if exif is None:
                return None

            gps = exif.get(_GPS_IFD_TAG)
            if not gps:
                return None

            lat_ref = gps.get(_GPS_LAT_REF)
            lat = gps.get(_GPS_LAT)
            lon_ref = gps.get(_GPS_LON_REF)
            lon = gps.get(_GPS_LON)
            if not (lat_ref and lat and lon_ref and lon):
                return None

            latitude = _dms_to_degrees(lat)
            if str(lat_ref).upper().startswith("S"):
                latitude = -latitude
            longitude = _dms_to_degrees(lon)
            if str(lon_ref).upper().startswith("W"):
                longitude = -longitude

            # A (0, 0) reading is Null Island, not a real photo location -
            # the value a camera writes when it has a GPS block but no fix.
            if latitude == 0.0 and longitude == 0.0:
                return None

            return (latitude, longitude)

    except Exception as exc:  # noqa: BLE001
        log.debug("could not read GPS from {}: {}: {}",
                  path.name, type(exc).__name__, exc)

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
