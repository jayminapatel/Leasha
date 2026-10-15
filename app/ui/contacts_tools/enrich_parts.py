"""The Enrich window's parts: the picture in a row, the picture on its own, the filter row and Details.

Layer: L5

The owner, 2026-10-15, on the Enrich contacts window: "the photo suggestions should show the photo and
double click should open source else how do i check ? ... this window has to be world class and
functional with our standards". The window itself (`enrich_dialog.py`) stays the shell that wires them;
the rules they show are pure (`enrich_rows.py`), and these are the few widgets that draw them:

* `thumbnail` - a suggested (or current) photo as a square picture in its row, scaled smoothly, so a
  photo is judged by looking at it, not by the words "(a photo)".
* `PictureWindow` - the same picture bigger, in the one dialog frame, for "Show the picture". The
  Photos page's viewer opens files from the index; a suggested photo is bytes no file holds yet.
* `FilterBar` - by kind, by source and by the contact's name, with the count shown.
* `DetailsArea` - the run's notes folded to one line, the whole text a click away, so the list has the
  room the wall of notes took.
"""

from __future__ import annotations

from typing import Any, Iterable, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPainter, QPixmap
from PySide6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QLineEdit, QSizePolicy, QToolButton,
    QVBoxLayout, QWidget,
)

from app.ui.contacts_tools import enrich_rows
from app.ui.contacts_tools._common import ink, named
from app.ui.theme import SPACE_S
from app.ui.widgets.buttons import give_icon
from app.ui.widgets.dialog_frame import buttons as dialog_buttons, frame
from app.ui.widgets.icons import icon, icon_size

__all__ = ["THUMB", "thumbnail", "PictureWindow", "FilterBar", "DetailsArea"]

#: The side of a photo in its row, in pixels: big enough to tell a face from a logo, small enough that
#: a contact's other rows stay on screen. A picture, not an icon, so not in `ICON_SIZES`.
THUMB = 48
#: The longest side of "Show the picture": a suggested photo is kept at most 256 pixels
#: (`base.normalise_image`), so this shows it whole without blowing it up into a blur.
PICTURE = 320


def thumbnail(photo: Any, side: int = THUMB, ratio: float = 1.0) -> QPixmap:
    """`photo` (a `Photo`, or anything with `.data`) as a square `side` pixels across, the whole picture
    scaled smoothly into it (a wide logo is not cropped to its middle letters); an empty pixmap when the
    bytes are not a picture. `ratio` is the screen's device pixel ratio, so it is sharp on a dense screen."""
    loaded = QPixmap()
    data = getattr(photo, "data", None)
    if not data or not loaded.loadFromData(bytes(data)):
        return QPixmap()
    px = max(1, round(side * ratio))
    scaled = loaded.scaled(px, px, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
    square = QPixmap(px, px)
    square.fill(Qt.GlobalColor.transparent)
    painter = QPainter(square)
    painter.drawPixmap((px - scaled.width()) // 2, (px - scaled.height()) // 2, scaled)
    painter.end()
    square.setDevicePixelRatio(ratio)
    return square


class PictureWindow(QDialog):
    """A suggested photo on its own, bigger, with where it came from under it. Esc or Close."""

    def __init__(self, photo: Any, caption: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        layout = frame(self, "Suggested picture")
        self.setObjectName("enrichPicture")
        self.picture = QLabel()
        self.picture.setAlignment(Qt.AlignmentFlag.AlignCenter)
        loaded = QPixmap()
        if loaded.loadFromData(bytes(getattr(photo, "data", b"") or b"")):
            if max(loaded.width(), loaded.height()) > PICTURE:
                loaded = loaded.scaled(PICTURE, PICTURE, Qt.AspectRatioMode.KeepAspectRatio,
                                       Qt.TransformationMode.SmoothTransformation)
            self.picture.setPixmap(loaded)
        else:
            self.picture.setText("Leasha could not draw this picture.")
        named(self.picture, "Suggested picture", "The picture this suggestion would put on the contact")
        self.caption = QLabel(caption)
        self.caption.setWordWrap(True)
        self.caption.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.caption.setObjectName("resultsSummary")
        named(self.caption, "Where the picture came from", caption or "Where the picture came from")
        layout.addWidget(self.picture, 1)
        layout.addWidget(self.caption)
        layout.addWidget(dialog_buttons(self, standard=QDialogButtonBox.StandardButton.Close))


def _combo(name: str, tip: str) -> QComboBox:
    box = QComboBox()
    box.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
    box.setIconSize(icon_size("list"))
    box.setAccessibleName(name)
    box.setToolTip(tip)
    return box


class FilterBar(QWidget):
    """Kind, source and name, and how many rows are shown. `changed` when any of the three moves."""

    changed = Signal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(SPACE_S)
        self.kind = _combo("Show which kind", "Show only one kind of suggestion: photos, phone numbers, "
                                              "emails, addresses, web addresses, company and job title, or "
                                              "corrections of a misread value")
        for words, glyph in enrich_rows.KIND_FILTERS:
            self.kind.addItem(icon(glyph, ink()), words)
        self.source = _combo("Show which source", "Show only the suggestions from one place")
        self.set_sources([])
        self.name = QLineEdit()
        self.name.setClearButtonEnabled(True)
        self.name.setPlaceholderText("Filter by contact name")
        named(self.name, "Filter by contact name", "Show only the contacts whose name has these letters")
        for clear in self.name.findChildren(QToolButton):     # the box's own clear button, named as every control is
            named(clear, "Clear the name filter", "Empty the box and show every contact again")
        self.count = QLabel("")
        self.count.setObjectName("resultsSummary")
        named(self.count, "How many are shown",
              "Accept selected keeps only the ticked suggestions you can see: a ticked row the filter "
              "hides is left as it is")
        for widget in (self.kind, self.source):
            row.addWidget(widget)
        row.addWidget(self.name, 1)
        row.addWidget(self.count)
        self.kind.currentIndexChanged.connect(lambda _i: self.changed.emit())
        self.source.currentIndexChanged.connect(lambda _i: self.changed.emit())
        self.name.textChanged.connect(lambda _t: self.changed.emit())

    def set_sources(self, sources: Iterable[str]) -> None:
        """The sources this run found, each once, in their words; the choice is kept when still there."""
        was = self.source.currentText()
        self.source.blockSignals(True)
        self.source.clear()
        self.source.addItem(icon("filter", ink()), enrich_rows.ANY_SOURCE)
        for source in dict.fromkeys(sources):
            self.source.addItem(icon(enrich_rows.source_icon(source), ink()), enrich_rows.source_words(source))
        found = self.source.findText(was)
        self.source.setCurrentIndex(max(0, found))
        self.source.blockSignals(False)

    def wanted(self) -> dict:
        """The filter as `enrich_rows.matches` takes it."""
        return {"kind": self.kind.currentText(), "source": self.source.currentText(), "text": self.name.text()}

    def is_narrowed(self) -> bool:
        w = self.wanted()
        return w["kind"] != "All" or w["source"] != enrich_rows.ANY_SOURCE or bool(w["text"].strip())


class DetailsArea(QWidget):
    """One line of the run's notes, and Details to read them all. `notes_line` keeps their words."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.toggle = QToolButton()
        self.toggle.setText("Details")
        self.toggle.setCheckable(True)
        self.toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        give_icon(self.toggle, "chevron-right", size="list")
        self.toggle.setAccessibleName("Show or hide the details")
        self.toggle.setToolTip("Show everything the look-up said: the age window, sources that could not be "
                               "reached, and how many less sure suggestions are hidden")
        self.summary = QLabel("")
        self.summary.setObjectName("resultsSummary")
        self.summary.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        named(self.summary, "What the look-up said, in short", "The first of the look-up's notes")
        self.notes_line = QLabel("")
        self.notes_line.setWordWrap(True)
        self.notes_line.setObjectName("resultsSummary")
        self.notes_line.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        named(self.notes_line, "What the look-up said", "The age window, and what the sources said besides suggestions")
        self.notes_line.hide()
        head = QHBoxLayout()
        head.setContentsMargins(0, 0, 0, 0)
        head.setSpacing(SPACE_S)
        head.addWidget(self.toggle)
        head.addWidget(self.summary, 1)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(SPACE_S)
        layout.addLayout(head)
        layout.addWidget(self.notes_line)
        self.toggle.toggled.connect(self._opened)
        self.hide()

    def set_notes(self, bits: list[str]) -> None:
        line = " ".join(bits)
        self.notes_line.setText(line)
        self.notes_line.setToolTip(line or "The age window, and what the sources said besides suggestions")
        self.summary.setText(enrich_rows.notes_summary(bits))
        self.summary.setToolTip(line or "The first of the look-up's notes")
        self.setVisible(bool(line))

    def _opened(self, on: bool) -> None:
        give_icon(self.toggle, "chevron-down" if on else "chevron-right", size="list")
        self.notes_line.setVisible(on)
        self.summary.setVisible(not on)
