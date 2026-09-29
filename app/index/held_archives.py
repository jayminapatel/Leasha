r"""Mail archives whose attached pictures wait for the pictures pass.

Layer: L3

Order 0z lane C. The text-first pass holds loose pictures for OCR later
(`pipeline._ocr_gate`, `ERR_OCR_HELD`), and the pictures pass finds them again
by walking for picture extensions. A picture *attached to an email* has no file
of its own to walk to: it is inside a `.pst`, which the pictures pass never
walks. So when the text pass holds attachment pictures (`app.extract.reading`,
`IMAGES_HOLD`), the archive's path is written down here, and the pictures pass
queues exactly these archives (`Pipeline._held_archive_candidates`) and reads
only their pictures (`IMAGES_ONLY`).

**One `index_state` row, a JSON list of paths.** A person has tens of
archives, not thousands; a list that small needs no table of its own. Changes
are kept in memory during the run, under a lock (every extraction worker may
report), and written once at the end by `save`, on the thread that writes the
rest of the run's state - never from a worker.

The rules, per archive read:

* text pass, pictures held - remember it (even when the read was stopped:
  what was held is still waiting);
* any pass that read the archive to the end without holding anything - forget
  it (either there were no pictures, or they have just been read);
* a stopped read never forgets.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Optional

from app.core.logging import logger

__all__ = ["HELD_ARCHIVES_STATE_KEY", "HeldArchives"]

_log = logger.bind(component="index.held_archives")

#: The `index_state` key. Spelt once, here.
HELD_ARCHIVES_STATE_KEY = "pictures_held_in_archives"


def _key(path: Path) -> str:
    from app.core.osbridge.pathnames import path_key

    return path_key(path)


class HeldArchives:
    """The held-pictures list for one run. Thread-safe; `save` once at the end."""

    def __init__(self, store: Any) -> None:
        self._store = store
        self._lock = threading.Lock()
        self._paths: Optional[dict[str, str]] = None   # key -> path as written
        self._dirty = False

    def _load(self) -> dict[str, str]:
        if self._paths is None:
            paths: dict[str, str] = {}
            try:
                raw = self._store.get_state(HELD_ARCHIVES_STATE_KEY)
                for value in json.loads(raw) if raw else ():
                    paths[_key(Path(str(value)))] = str(value)
            except Exception as exc:             # noqa: BLE001 - an empty list is safe
                _log.debug("held-archive list unreadable, starting empty: {}", exc)
            self._paths = paths
        return self._paths

    def paths(self) -> list[Path]:
        """Every archive waiting for the pictures pass."""
        with self._lock:
            return [Path(value) for value in self._load().values()]

    def is_held(self, path: Path) -> bool:
        with self._lock:
            return _key(path) in self._load()

    def note(self, path: Path, *, held: int, finished: bool, pass_: str = "") -> None:
        """Record one read of `path`. See the module docstring for the rules."""
        key = _key(path)
        with self._lock:
            paths = self._load()
            if held > 0:
                if key not in paths:
                    paths[key] = str(path)
                    self._dirty = True
            elif finished and key in paths:
                del paths[key]
                self._dirty = True

    def save(self) -> None:
        """Write the list if it changed. Never raises."""
        with self._lock:
            if not self._dirty or self._paths is None:
                return
            try:
                self._store.set_state(HELD_ARCHIVES_STATE_KEY,
                                      json.dumps(sorted(self._paths.values())))
                self._dirty = False
            except Exception as exc:             # noqa: BLE001 - retried at the next save
                _log.warning("could not save the held-pictures archive list: {}", exc)
