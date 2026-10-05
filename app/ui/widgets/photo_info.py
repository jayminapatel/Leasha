r"""The Photos page's right-hand panel: one picture, and everything known about it.

Layer: L5

2026-10-05, the owner: "make it like a professional photo management/viewer".
What a photo manager's info panel shows, in the order it is looked for: the
picture, when and where, who is in it, what it shows, Leasha's description, any
text read from it, then the camera and the file. Open and Show in folder at the
foot; "Name the people" when it has faces nobody has named.

Everything is read on one worker (`gather`): a 480-pixel picture, the
description and text from the index, and the camera's EXIF. A newer selection
wins - a slow HEIC never paints over the photo now selected.
"""

from __future__ import annotations

from pathlib import Path, PurePath
from typing import Any, Optional

from PySide6.QtCore import Qt, QThreadPool, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (QFormLayout, QFrame, QHBoxLayout, QLabel, QPushButton,
                             QScrollArea, QSizePolicy, QVBoxLayout, QWidget)

from app.ui.presenter.photos import date_text, people_text, size_text

__all__ = ["PhotoInfo", "gather", "CAMERA_TAGS"]

#: The EXIF tags worth a line, and what to call them.
CAMERA_TAGS: tuple[tuple[str, str], ...] = (
    ("Make", "Camera make"), ("Model", "Camera"), ("LensModel", "Lens"),
    ("FNumber", "Aperture"), ("ExposureTime", "Exposure"),
    ("ISOSpeedRatings", "ISO"), ("FocalLength", "Focal length"),
)
PREVIEW_EDGE = 480


def gather(store: Any, file_id: int, path: str) -> dict:
    """Everything the panel shows that needs a read. **Worker.** Never raises."""
    out: dict = {"image": None, "details": {}, "camera": [], "pixels": ""}
    try:
        from app.ui.thumbnail_loader import decode_thumbnail

        out["image"] = decode_thumbnail(path, edge=PREVIEW_EDGE)
    except Exception:                               # noqa: BLE001 - a blank picture
        pass
    try:
        out["details"] = store.photo_details(file_id)
    except Exception:                               # noqa: BLE001
        pass
    try:
        from PIL import Image

        from app.extract.exif import read_all_metadata
        from app.extract.heif import register_heif

        register_heif()
        with Image.open(path) as opened:
            out["pixels"] = f"{opened.width:,} × {opened.height:,}"
        tags = read_all_metadata(Path(path))
        for key, label in CAMERA_TAGS:
            value = str(tags.get(key, "")).strip().strip("\x00")
            if value:
                if key == "FNumber":
                    value = f"f/{float(value):g}" if _number(value) else value
                elif key == "FocalLength":
                    value = f"{float(value):g} mm" if _number(value) else value
                elif key == "ExposureTime" and _number(value) and 0 < float(value) < 1:
                    value = f"1/{round(1 / float(value))} s"
                out["camera"].append((label, value))
    except Exception:                               # noqa: BLE001
        pass
    return out


def _number(text: str) -> bool:
    try:
        float(text)
        return True
    except (TypeError, ValueError):
        return False


def _value(text: str = "") -> QLabel:
    label = QLabel(text)
    label.setWordWrap(True)
    # A long file name has nowhere to wrap; without this it widens the panel.
    label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
    label.setMinimumWidth(40)
    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return label


class PhotoInfo(QWidget):
    """Shows one `PhotoRow`. Signals ask the page to act."""

    open_requested = Signal(object)
    reveal_requested = Signal(object)
    name_requested = Signal(object)
    view_requested = Signal(object)

    def __init__(self, store: Any, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("photo_info")
        self._store = store
        self._pool = QThreadPool.globalInstance()
        self._row: Any = None
        self._generation = 0

        self.picture = QLabel("")
        self.picture.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.picture.setMinimumHeight(200)
        self.picture.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.picture.setToolTip("Double-click to see it full size")
        self.picture.mouseDoubleClickEvent = self._double_clicked  # type: ignore[method-assign]
        self.title = _value()
        self.title.setStyleSheet("font-weight: 600; font-size: 11pt;")

        self.form = QFormLayout()
        self.form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        self.fields: dict[str, QLabel] = {}
        for key, label in (("date", "Taken"), ("place", "Place"), ("people", "People"),
                           ("shows", "Shows"), ("description", "Description"),
                           ("text", "Text in it"), ("pixels", "Size"), ("file", "File"),
                           ("folder", "Folder")):
            self.fields[key] = _value()
            self.form.addRow(label, self.fields[key])
        self.camera = QFormLayout()
        self.camera.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self.open_button = QPushButton("Open")
        self.open_button.setToolTip("Open the photo in its usual program")
        self.open_button.clicked.connect(self._open)
        self.reveal_button = QPushButton("Show in folder")
        self.reveal_button.setToolTip("Show the photo in File Explorer")
        self.reveal_button.clicked.connect(self._reveal)
        self.name_button = QPushButton("Name people")
        self.name_button.setToolTip("Name the faces Leasha found in your photos")
        self.name_button.clicked.connect(self._name)
        buttons = QHBoxLayout()
        for button in (self.open_button, self.reveal_button):
            buttons.addWidget(button)
        buttons.addStretch(1)

        body = QWidget()
        inner = QVBoxLayout(body)
        inner.addWidget(self.picture)
        inner.addWidget(self.title)
        inner.addLayout(self.form)
        self.rule = QFrame()
        self.rule.setFrameShape(QFrame.Shape.HLine)
        inner.addWidget(self.rule)
        inner.addLayout(self.camera)
        inner.addWidget(self.name_button, 0, Qt.AlignmentFlag.AlignLeft)
        inner.addStretch(1)
        self.empty = QLabel("Select a photo to see everything Leasha knows about it.")
        self.empty.setWordWrap(True)
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        inner.addWidget(self.empty)

        scroll = QScrollArea()
        scroll.setObjectName("photo_info_scroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(body)
        self.scroll = scroll

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 0, 0, 0)
        layout.addWidget(scroll, 1)
        layout.addLayout(buttons)
        self.setMinimumWidth(260)
        self.show_row(None)

    def show_row(self, row: Any) -> None:
        from app.ui.later import when_done
        from app.ui.workers import CallableWorker, run

        self._row = row
        self._generation += 1
        has = row is not None
        for widget in (self.picture, self.title, self.open_button, self.reveal_button,
                       self.rule):
            widget.setVisible(has)
        self.empty.setVisible(not has)
        self.name_button.setVisible(has and row.faces > len(row.people))
        self._clear_camera()
        if not has:
            for field in self.fields.values():
                field.setText("")
            self._show_fields()
            return
        path = PurePath(row.path)
        self.title.setText(path.name)
        self.fields["date"].setText(date_text(row))
        self.fields["place"].setText(row.place or "")
        self.fields["people"].setText(people_text(row))
        self.fields["shows"].setText(", ".join(row.tags))
        self.fields["file"].setText(f"{str(row.ext).upper()} · {size_text(row.size_bytes)}")
        self.fields["folder"].setText(str(path.parent))
        for key in ("description", "text", "pixels"):
            self.fields[key].setText("")
        self.picture.setPixmap(QPixmap())
        self.picture.setText("…")
        self._show_fields()
        generation = self._generation
        worker = CallableWorker(gather, self._store, row.file_id, str(row.path),
                                component="ui.photo_info")
        when_done(self, worker, finished=lambda facts, g=generation: self._facts(facts, g))
        run(self._pool, worker)

    def _facts(self, facts: Any, generation: int) -> None:
        if generation != self._generation or not isinstance(facts, dict):
            return
        image = facts.get("image")
        if image is not None and not image.isNull():
            self.picture.setText("")
            self.picture.setPixmap(QPixmap.fromImage(image).scaled(
                max(120, self.scroll.viewport().width() - 24), 360,
                Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
        else:
            self.picture.setText("Leasha could not draw this picture.")
        details = facts.get("details") or {}
        self.fields["description"].setText(details.get("AI description", ""))
        self.fields["text"].setText(details.get("Text read from the image", "")[:1200])
        self.fields["pixels"].setText(facts.get("pixels", ""))
        self._clear_camera()
        for label, value in facts.get("camera", []):
            self.camera.addRow(label, _value(value))
        self._show_fields()

    def _show_fields(self) -> None:
        """A field with nothing in it takes no room."""
        for row in range(self.form.rowCount()):
            field = self.form.itemAt(row, QFormLayout.ItemRole.FieldRole)
            label = self.form.itemAt(row, QFormLayout.ItemRole.LabelRole)
            if field is None or label is None:
                continue
            shown = bool(field.widget().text())
            field.widget().setVisible(shown)
            label.widget().setVisible(shown)

    def _clear_camera(self) -> None:
        while self.camera.rowCount():
            self.camera.removeRow(0)

    def _open(self) -> None:
        if self._row is not None:
            self.open_requested.emit(self._row)

    def _reveal(self) -> None:
        if self._row is not None:
            self.reveal_requested.emit(self._row)

    def _name(self) -> None:
        if self._row is not None:
            self.name_requested.emit(self._row)

    def _double_clicked(self, _event: Any) -> None:
        if self._row is not None:
            self.view_requested.emit(self._row)
