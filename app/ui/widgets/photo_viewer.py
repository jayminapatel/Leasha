r"""The Photos page's viewer: one picture, as big as the screen allows.

Layer: L5

2026-10-05, the owner: "make it like a professional photo management/viewer".
Double-click a photo (or press Enter) and it fills a dark window: ← and → step
through the photos as the page has them ordered and narrowed, Esc or a click
outside closes, F toggles full screen. A line under the picture says what it
is, when, where and who. The picture is decoded on a worker at the screen's own
size, upright, and the last few are kept so stepping back is immediate.
"""

from __future__ import annotations

from collections import OrderedDict
from pathlib import PurePath
from typing import Any, Callable, Optional

from PySide6.QtCore import Qt, QThreadPool
from PySide6.QtGui import QKeyEvent, QPixmap
from PySide6.QtWidgets import QDialog, QLabel, QSizePolicy, QVBoxLayout, QWidget

from app.ui.presenter.photos import date_text, people_text

__all__ = ["PhotoViewer", "caption"]

#: Pictures kept decoded: the one shown and its neighbours.
KEEP = 5


def caption(row: Any, position: int, total: int) -> str:
    parts = [PurePath(row.path).name, date_text(row), row.place or "", people_text(row)]
    text = "  ·  ".join(p for p in parts if p)
    return f"{text}    ({position:,} of {total:,})" if total else text


class PhotoViewer(QDialog):
    """`step(delta)` is the page's: it moves the selection and returns the row."""

    def __init__(self, row: Any, step: Callable[[int], Any],
                 position: Callable[[], tuple[int, int]],
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent, Qt.WindowType.Window)
        self.setObjectName("photo_viewer")
        self.setWindowTitle("Photo")
        self.setStyleSheet("QDialog#photo_viewer { background: #111; } "
                           "QLabel { color: #ddd; }")
        self._step, self._position = step, position
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

    def _show(self, row: Any) -> None:
        if row is None:
            return
        self._row = row
        position, total = self._position()
        self.line.setText(caption(row, position, total))
        self.setWindowTitle(PurePath(row.path).name)
        self._paint()
        self._decode(str(row.path))

    def _paint(self) -> None:
        image = self._decoded.get(str(self._row.path)) if self._row is not None else None
        if image is None:
            self.picture.setPixmap(QPixmap())
            self.picture.setText("…")
            return
        self.picture.setText("")
        self.picture.setPixmap(QPixmap.fromImage(image).scaled(
            self.picture.size(), Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation))

    def _decode(self, path: str) -> None:
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
        row = self._step(delta)
        if row is not None:
            self._show(row)

    # -- Qt -------------------------------------------------------------------------------

    def resizeEvent(self, event: Any) -> None:  # noqa: N802 - Qt's name
        super().resizeEvent(event)
        self._paint()

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802 - Qt's name
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
