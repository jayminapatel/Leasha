r"""The Photos page's viewer: one picture, as big as the screen allows.

Layer: L5

2026-10-05, the owner: "make it like a professional photo management/viewer".
Double-click a photo (or press Enter) and it fills a dark window: ← and → step
through the photos as the page has them ordered and narrowed, Esc closes (from
full screen it first leaves full screen), F or a double-click toggles full
screen. A line under the picture says what it is, when, where and who. The
picture is decoded on a worker at the screen's own size, upright; the photos
either side are decoded while this one is looked at, and the last few are kept,
so a step either way shows the next picture at once rather than "…".

Opening another photo while the viewer is open shows it in the same window
(`photo_page_parts.open_viewer`), so a double-click never stacks a second one.
"""

from __future__ import annotations

from collections import OrderedDict
from pathlib import PurePath
from typing import Any, Callable, Optional

from PySide6.QtCore import Qt, QThreadPool
from PySide6.QtGui import QKeyEvent, QPixmap
from PySide6.QtWidgets import QDialog, QLabel, QSizePolicy, QVBoxLayout, QWidget

from app.ui.presenter.photos import date_text, people_text
from app.ui.theme import theme_colours

__all__ = ["PhotoViewer", "caption"]

#: Pictures kept decoded: the one shown, the one either side asked for ahead,
#: and the two last stepped from.
KEEP = 5


def caption(row: Any, position: int, total: int) -> str:
    """The line under the picture: name, date, place, people, and "n of total"."""
    parts = [PurePath(row.path).name, date_text(row), row.place or "", people_text(row)]
    text = "  ·  ".join(p for p in parts if p)
    return f"{text}    ({position:,} of {total:,})" if total else text


class PhotoViewer(QDialog):
    """`step(delta)` is the page's: it moves the selection and returns the row."""

    def __init__(self, row: Any, step: Callable[[int], Any],
                 position: Callable[[], tuple[int, int]],
                 parent: Optional[QWidget] = None, *,
                 peek: Optional[Callable[[int], Any]] = None) -> None:
        """`peek(delta)` names the photo `delta` away without moving to it (the
        page's `PhotoBrowser.neighbour`), so the neighbours can be decoded ahead."""
        super().__init__(parent, Qt.WindowType.Window)
        self.setObjectName("photo_viewer")
        self.setWindowTitle("Photo")
        # The theme's photo backdrop: one near-black in both palettes, because a
        # photograph is judged against black whatever the window's theme is.
        colours = theme_colours()
        self.setStyleSheet(f"QDialog#photo_viewer {{ background: {colours['backdrop']}; }} "
                           f"QLabel {{ color: {colours['backdrop_text']}; }}")
        self._step, self._position, self._peek = step, position, peek
        self._pool = QThreadPool.globalInstance()
        self._decoded: "OrderedDict[str, Any]" = OrderedDict()
        self._asked: set[str] = set()
        self._row = row

        self.picture = QLabel("")
        self.picture.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.picture.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        self.picture.setToolTip("← and → for the photo before and after; Esc to close")
        self.line = QLabel("")
        self.line.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.line.setWordWrap(True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.addWidget(self.picture, 1)
        layout.addWidget(self.line)
        screen = self.screen().availableGeometry() if self.screen() else None
        self._edge = max(screen.width(), screen.height()) if screen else 1920
        if screen is not None:
            self.resize(int(screen.width() * 0.9), int(screen.height() * 0.9))
        self._show(row)

    # -- showing ------------------------------------------------------------------------

    def show_row(self, row: Any) -> None:
        """Show `row` in this window - the page's way in when one is already open."""
        self._show(row)

    def _show(self, row: Any) -> None:
        """Show `row`: caption now, the picture when its decode lands, and the
        photos either side asked for next, so the next arrow press finds them."""
        if row is None:
            return
        self._row = row
        position, total = self._position()
        self.line.setText(caption(row, position, total))
        self.setWindowTitle(PurePath(row.path).name)
        self._paint()
        self._decode(str(row.path))
        if self._peek is not None:
            for delta in (1, -1):
                near = self._peek(delta)
                if near is not None:
                    self._decode(str(near.path))

    def _paint(self) -> None:
        """Scale the decoded picture to the label, or show a dot while it is on its way."""
        image = self._decoded.get(str(self._row.path)) if self._row is not None else None
        if image is None:
            self.picture.setPixmap(QPixmap())
            self.picture.setText("…")
            return
        # Most recently shown last, so the picture on screen is never the one
        # the neighbours' decodes push out of `KEEP`.
        self._decoded.move_to_end(str(self._row.path))
        self.picture.setText("")
        self.picture.setPixmap(QPixmap.fromImage(image).scaled(
            self.picture.size(), Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation))

    def _decode(self, path: str) -> None:
        """Decode `path` at the screen's size on a worker, once; `_decoded_one` paints it."""
        from app.ui.later import when_done
        from app.ui.thumbnail_loader import decode_thumbnail
        from app.ui.workers import CallableWorker, run

        if path in self._decoded or path in self._asked:
            return
        self._asked.add(path)
        worker = CallableWorker(decode_thumbnail, path, edge=self._edge,
                                component="ui.photo_viewer")
        when_done(self, worker, finished=lambda image, p=path: self._decoded_one(p, image))
        run(self._pool, worker)

    def _decoded_one(self, path: str, image: Any) -> None:
        """UI thread: a picture landed. Kept (`KEEP` most recent) and painted if current."""
        self._asked.discard(path)
        if image is None or image.isNull():
            if self._row is not None and str(self._row.path) == path:
                self.picture.setText("Leasha could not draw this picture.")
            return
        self._decoded[path] = image
        while len(self._decoded) > KEEP:
            self._decoded.popitem(last=False)
        if self._row is not None and str(self._row.path) == path:
            self._paint()

    def go(self, delta: int) -> None:
        """Step to the next or previous photo as the page orders them."""
        row = self._step(delta)
        if row is not None:
            self._show(row)

    # -- Qt -------------------------------------------------------------------------------

    def resizeEvent(self, event: Any) -> None:  # noqa: N802 - Qt's name
        super().resizeEvent(event)
        self._paint()

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802 - Qt's name
        """Arrows step, F toggles full screen, Esc leaves full screen first."""
        key = event.key()
        if key in (Qt.Key.Key_Right, Qt.Key.Key_Down, Qt.Key.Key_Space, Qt.Key.Key_PageDown):
            self.go(1)
        elif key in (Qt.Key.Key_Left, Qt.Key.Key_Up, Qt.Key.Key_Backspace, Qt.Key.Key_PageUp):
            self.go(-1)
        elif key == Qt.Key.Key_F:
            self.showNormal() if self.isFullScreen() else self.showFullScreen()
        elif key == Qt.Key.Key_Escape and self.isFullScreen():
            self.showNormal()
        else:
            super().keyPressEvent(event)

    def mouseDoubleClickEvent(self, _event: Any) -> None:  # noqa: N802 - Qt's name
        self.showNormal() if self.isFullScreen() else self.showFullScreen()
