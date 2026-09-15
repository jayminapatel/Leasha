r"""Offline reverse geocoding: GPS coordinates to a place name, no network.

Layer: L2

Work order 0i (`202626270511`) section 4a. A photo's EXIF GPS coordinates
(`app.extract.exif.read_gps`) are two numbers nobody types into a search
box. `reverse_geocoder` turns them into a name somebody would actually
search for - "London", "Leeds" - from a dataset bundled inside the package
itself. **Zero network**, the item's own words: the whole point of finding
a photo by "the trip to Leeds" is that it works on a machine that has never
been and will never be online, exactly like every other search this
application does.

**`mode=1` (single-threaded), not the package default.** `reverse_geocoder.
search()` - the convenience function most examples use - defaults to
`mode=2`, which farms the k-d tree query out to a `multiprocessing` pool.
Measured directly: on Windows that means a fresh child *process* per call
(`multiprocessing`'s `spawn` start method has no `fork`), which re-imports
this process's entry module and re-parses the bundled dataset in the
child - seconds of cost, repeated, for one photo's coordinates. `RGeocoder
(mode=1)` runs the k-d tree query single-threaded in the calling process,
loaded once and reused exactly like `florence_tagger._load()` and `ocr.
_load_engine()` already load their own models once and reuse them.
"""

from __future__ import annotations

import threading
from typing import Any, Optional

from app.core.logging import logger

log = logger.bind(component="extract.places")

__all__ = ["reverse_geocode", "available"]

_geocoder: Any = None
_geocoder_lock = threading.Lock()
_geocoder_failed = False


def available() -> bool:
    """Is offline reverse geocoding usable here? Never raises, never loads
    the bundled dataset - the same contract as every other `available()` in
    this codebase (`app.extract.ocr.available`, `app.extract.florence_
    tagger.available`)."""
    try:
        import importlib.util

        return importlib.util.find_spec("reverse_geocoder") is not None
    except Exception:                              # noqa: BLE001
        return False


def _load() -> Optional[Any]:
    """The single-threaded geocoder, loaded once. `None` when it cannot be."""
    global _geocoder, _geocoder_failed

    if _geocoder is not None or _geocoder_failed:
        return _geocoder

    with _geocoder_lock:
        if _geocoder is not None or _geocoder_failed:
            return _geocoder
        try:
            import reverse_geocoder as rg

            _geocoder = rg.RGeocoder(mode=1, verbose=False)
            log.info("offline reverse geocoder loaded")
        except Exception as exc:                    # noqa: BLE001 - absence is normal
            _geocoder_failed = True
            log.warning("offline reverse geocoder did not load: {}: {}",
                        type(exc).__name__, exc)
            return None
    return _geocoder


def reverse_geocode(latitude: float, longitude: float) -> Optional[str]:
    r"""The nearest town's name for one coordinate pair, or None.

    Never raises - a photo whose GPS block is present but whose lookup
    fails for any reason simply has no place, the same H4 degradation
    `read_gps`/`read_datetime` already use.
    """
    geocoder = _load()
    if geocoder is None:
        return None

    try:
        results = geocoder.query([(float(latitude), float(longitude))])
        if not results:
            return None
        name = results[0].get("name")
        return str(name).strip() or None
    except Exception as exc:                        # noqa: BLE001 - H4
        log.debug("could not reverse-geocode ({}, {}): {}: {}",
                  latitude, longitude, type(exc).__name__, exc)
        return None
