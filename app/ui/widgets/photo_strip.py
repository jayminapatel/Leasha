r"""A row of the pictures in a mixed result set, above the list. Review 2026-10-14, MS-5.

Layer: L5

The Search list draws no images, and the grid is all-or-nothing - a global
switch that shows the pictures only. A query whose answer is a few documents
and a few photos showed the photos as file names. Now the pictures in the
result set also sit in one strip of thumbnails above the list: "Photos" and a
count, then the pictures, scrolled sideways. A click or Enter opens one in the
lightbox with its siblings, Space is quick look; with no pictures in the set,
or with the grid itself in front, the strip is not there at all.

Drawn the Photos page's way (`search_photos.py`), from the same loader as the
grid, so a picture made for one is there for the other.
"""

from __future__ import annotations

import time
from typing import Any, Optional, Sequence

from PySide6.QtCore import QModelIndex, Qt, Signal
from PySide6.QtWidgets import QAbstractItemView, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from app.ui.theme import SPACE_XS
from app.ui.widgets import list_keys
from app.ui.widgets.search_photos import (
    ROLE_PHOTO, SearchPhotoModel, ask_ahead, photo_view, photos_of, shared_loader,
)

__all__ = ["PhotoStrip", "STRIP_EDGE", "strip_heading"]

#: The strip's thumbnails: a glance, not a gallery - the grid is one switch away.
STRIP_EDGE = 72
#: A second open of the same picture within this is the same click (a double-click).
TWICE_S = 0.6


def strip_heading(count: int) -> str:
    """The strip's heading - new words, 2026-10-14."""
    return "Photos · 1 picture" if count == 1 else f"Photos · {count} pictures"


class PhotoStrip(QWidget):
    """The pictures of the current result set, in one sideways row."""

    #: A picture opened: the row, and every picture in the strip (the lightbox's siblings).
    opened = Signal(object, list)

    def __init__(self, parent: Optional[QWidget] = None, *, store: Any = None) -> None:
        super().__init__(parent)
        self.setObjectName("photoStrip")
        self.model = SearchPhotoModel(shared_loader(store), self)
        self.heading = QLabel("")
        self.heading.setObjectName("resultsSummary")
        self._list = photo_view(self.model, STRIP_EDGE, names=False, flow_left=True)
        self._list.setObjectName("photoStripList")
        self._list.setToolTip("Pictures among these results - click one to see it large")
        self._list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._list.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._list.setFixedHeight(self._list.gridSize().height()
                                  + self._list.horizontalScrollBar().sizeHint().height() + SPACE_XS)
        # A click opens, as a thumbnail anywhere in the window does on a double-click:
        # a strip this small is a row of buttons, not a list somebody selects in.
        self._list.clicked.connect(self._open_index)
        list_keys.install(self._list, default=self._open_index, preview=list_keys.PAGE_PREVIEW)
        self._wanted = False
        self._allowed = True
        self._last_open: tuple = ("", 0.0)

        head = QHBoxLayout()
        head.setContentsMargins(0, 0, 0, 0)
        head.addWidget(self.heading)
        head.addStretch(1)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, SPACE_XS)
        layout.setSpacing(SPACE_XS)
        layout.addLayout(head)
        layout.addWidget(self._list)
        self.hide()

    def show_rows(self, rows: Sequence[Any]) -> None:
        """The current result set; the strip shows its pictures, or goes."""
        photos = photos_of(rows)
        self.model.set_rows(photos)
        self.heading.setText(strip_heading(len(photos)))
        self._wanted = bool(photos)
        self._sync()
        if photos:
            ask_ahead(self._list, self.model)

    def set_allowed(self, allowed: bool) -> None:
        """False while the grid is in front: the pictures are already the page."""
        self._allowed = bool(allowed)
        self._sync()

    def _sync(self) -> None:
        self.setVisible(self._wanted and self._allowed)

    def image_rows(self) -> list[Any]:
        return self.model.result_rows()

    def current_row(self) -> Any:
        index = self._list.currentIndex()
        return getattr(index.data(ROLE_PHOTO), "row", None) if index.isValid() else None

    def _open_index(self, index: QModelIndex) -> None:
        """A click, a double-click or Enter: the lightbox. A double-click is a
        click and then an activation, so the second within `TWICE_S` of the first,
        on the same picture, is the same open."""
        photo = index.data(ROLE_PHOTO) if index.isValid() else None
        if photo is None:
            return
        now = time.monotonic()
        if self._last_open[0] == photo.path and now - self._last_open[1] < TWICE_S:
            return
        self._last_open = (photo.path, now)
        self.opened.emit(photo.row, self.image_rows())
