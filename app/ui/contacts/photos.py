"""Contact pictures for the list and the cards: fetched on a worker, kept small, never blocking.

Layer: L5

A painted row asks `pixmap(id)`; the answer is the picture if it is already here and `None`
otherwise (the delegate draws initials meanwhile), and the first ask queues one read on a pool
of its own. The read decodes and shrinks the picture on the worker (`QImage` is safe off the
UI thread); only the cheap `QPixmap.fromImage` happens on the window's. `loaded(id)` says a
picture arrived so the views repaint. Rule 5: nothing here queries on the UI thread.
"""

from __future__ import annotations

from collections import OrderedDict
from typing import Any, Optional

from PySide6.QtCore import QObject, QThreadPool, Signal
from PySide6.QtGui import QPixmap

from app.ui.contacts.loader import decode_photo
from app.ui.later import when_done
from app.ui.workers import CallableWorker, run

__all__ = ["PhotoCache"]

#: How many decoded pictures are kept. 64px squares: 600 of them is a few megabytes.
KEEP = 600


class PhotoCache(QObject):
    loaded = Signal(int)

    def __init__(self, hub: Any, side: int = 64, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._hub = hub
        self._side = side
        self._have: "OrderedDict[int, Optional[QPixmap]]" = OrderedDict()
        self._asked: set[int] = set()
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(2)

    @property
    def side(self) -> int:
        return self._side

    def pixmap(self, cid: int) -> Optional[QPixmap]:
        """The picture, or None (not here yet, or the contact has none)."""
        if cid in self._have:
            self._have.move_to_end(cid)
            return self._have[cid]
        if cid not in self._asked:
            self._asked.add(cid)
            worker = CallableWorker(decode_photo, self._hub, cid, self._side, component="ui.contacts.photo")
            when_done(self, worker, finished=lambda image, c=cid: self._arrived(c, image),
                      failed=lambda _e, c=cid: self._arrived(c, None))
            run(self._pool, worker)
        return None

    def _arrived(self, cid: int, image: Any) -> None:
        self._asked.discard(cid)
        self._have[cid] = QPixmap.fromImage(image) if image is not None else None
        while len(self._have) > KEEP:
            self._have.popitem(last=False)
        if self._have[cid] is not None:
            self.loaded.emit(cid)

    def forget(self, cid: Optional[int] = None) -> None:
        """After an edit changed a picture."""
        if cid is None:
            self._have.clear()
        else:
            self._have.pop(cid, None)
        self.loaded.emit(cid if cid is not None else -1)

    def wait(self, ms: int = 5000) -> bool:
        return self._pool.waitForDone(ms)
