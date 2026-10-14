"""The read view of one contact: what a person sees when they pick one, before they edit anything.

Layer: L5

UI phase B (2026-10-13; the owner: every screen should "look and feel like" one app, and the UI
review's "reading a contact is not the same as editing it"). Picking a contact used to open the
editor - fifteen boxes and drop-downs to read one phone number. Now a pick shows this: the photo,
the name and the work line, then each kind of detail as plain text you can select and copy, with
its label (work, home, mobile) and its icon, then notes, where it is kept, its groups and tags,
and - when the page lends them - the "From your index" and enrichment panels underneath. **Edit**
(also Enter, a double-click and F2 on the list) switches to the editor; Save and Cancel there come
back here.

Nothing is written from here. The words are the editor's (`logic.KIND_INFO`, the editor's section
titles), so a detail is called the same thing in both. Colours are theme tokens; the photo is
drawn by `round_photo`, the same circle as the editor's.
"""

from __future__ import annotations

from typing import Any, Iterable, Optional

from PySide6.QtCore import QRect, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPixmap
from PySide6.QtWidgets import QGridLayout, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from app.contacts.card import KINDS, Card, display_name
from app.ui.contacts.logic import KIND_INFO, initials, label_icon
from app.ui.theme import SPACE_M, SPACE_S, SPACE_XS, theme_colours
from app.ui.widgets.buttons import action_button
from app.ui.widgets.icons import icon
from app.ui.widgets.scroll import scrollable
from app.ui.widgets.paint_kit import points_for_pixels, scaled_font, set_weight

__all__ = ["ContactReader", "round_photo", "work_line", "PHOTO_SIDE"]

PHOTO_SIDE = 72


def work_line(card: Card) -> str:
    """ "Buyer · Purchasing · Acme Ltd": the job title, department and company that are filled in."""
    return " · ".join(v for v in (card.get("title"), card.get("department"), card.get("company")) if v)


def round_photo(photo: Any, name: str, side: int, colours: Optional[dict[str, str]] = None) -> QPixmap:
    """The contact's picture in a circle, or their initials on the theme's soft accent."""
    c = colours or theme_colours()
    canvas = QPixmap(side, side)
    canvas.fill(Qt.GlobalColor.transparent)
    painter = QPainter(canvas)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    path = QPainterPath()
    path.addEllipse(QRectF(0, 0, side, side))
    loaded = QPixmap()
    if photo is not None and loaded.loadFromData(photo.data):
        painter.setClipPath(path)
        scaled = loaded.scaled(side, side, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                               Qt.TransformationMode.SmoothTransformation)
        painter.drawPixmap((side - scaled.width()) // 2, (side - scaled.height()) // 2, scaled)
    else:
        painter.fillPath(path, QColor(c.get("accent_soft", "#dddddd")))
        font = set_weight(QFont(painter.font()), "bold")
        font.setPointSizeF(points_for_pixels(int(side * 0.38)))     # sized to the circle, in points
        painter.setFont(font)
        painter.setPen(QColor(c.get("accent_text", "#444444")))
        painter.drawText(QRect(0, 0, side, side), Qt.AlignmentFlag.AlignCenter, initials(name) if name else "?")
    painter.end()
    return canvas


def _selectable(text: str, *, dim: bool = False) -> QLabel:
    label = QLabel(text)
    label.setWordWrap(True)
    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse
                                  | Qt.TextInteractionFlag.TextSelectableByKeyboard)
    if dim:
        label.setObjectName("resultsSummary")
    return label


class ContactReader(QWidget):
    """One contact, to read. `edit_requested` is the Edit button; the page switches to the editor."""

    edit_requested = Signal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.cid: Optional[int] = None
        self._card = Card()
        self._kept: list[str] = []
        self._tags: list[str] = []
        self.setObjectName("contactReader")
        self.setAccessibleName("Contact")

        self.photo_label = QLabel()
        self.photo_label.setFixedSize(PHOTO_SIDE, PHOTO_SIDE)
        self.photo_label.setAccessibleName("Photo")
        self.heading = QLabel("")
        self.heading.setObjectName("contactName")
        self.heading.setWordWrap(True)
        self.heading.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        # The type scale's `title` step (it was x1.35, a step of its own; review 2026-10-14, DS-8).
        font = QFont(self.heading.font())
        font.setPointSizeF(max(font.pointSizeF(), 9.0))
        self.heading.setFont(scaled_font(font, "title", weight="bold"))
        self.work = _selectable("", dim=True)
        self.work.setAccessibleName("Job and company")
        self.star = QLabel()
        self.star.setAccessibleName("Favourite")
        self.star.setToolTip("Favourite")
        self.edit_button = action_button("Edit", "pencil", "primary")
        self.edit_button.setToolTip("Edit this contact (Enter or F2)")
        self.edit_button.clicked.connect(self.edit_requested.emit)
        names = QVBoxLayout()
        names.setSpacing(2)
        top = QHBoxLayout()
        top.setSpacing(6)
        top.addWidget(self.heading, 1)
        top.addWidget(self.star, 0, Qt.AlignmentFlag.AlignTop)
        names.addLayout(top)
        names.addWidget(self.work)
        names.addStretch(1)
        head = QHBoxLayout()
        head.setSpacing(SPACE_M)
        head.addWidget(self.photo_label, 0, Qt.AlignmentFlag.AlignTop)
        head.addLayout(names, 1)
        head.addWidget(self.edit_button, 0, Qt.AlignmentFlag.AlignTop)

        #: The details, rebuilt for each contact: a section per kind that has values.
        self.details = QWidget()
        self._details = QVBoxLayout(self.details)
        self._details.setContentsMargins(0, 0, 0, 0)
        self._details.setSpacing(SPACE_M)
        #: Where the page puts the panels it lends (`add_panels`).
        self._panels = QVBoxLayout()
        self._panels.setContentsMargins(0, 0, 0, 0)
        self._panels.setSpacing(10)

        body = QWidget()
        col = QVBoxLayout(body)
        col.setContentsMargins(12, 10, 12, 12)
        col.setSpacing(14)
        col.addLayout(head)
        col.addWidget(self.details)
        col.addLayout(self._panels)
        col.addStretch(1)
        self.scroll = scrollable(body, min_width=300)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self.scroll, 1)
        self.clear()

    # ------------------------------------------------------------------ filling
    def add_panels(self, *panels: QWidget) -> None:
        """Panels the page lends, shown under the details (the index and enrichment panels)."""
        for panel in panels:
            self._panels.addWidget(panel)

    def clear(self) -> None:
        self.cid = None
        self._card = Card()
        self._kept, self._tags = [], []
        self.heading.setText("")
        self.work.setText("")
        self.work.hide()
        self.star.hide()
        self.edit_button.setEnabled(False)
        self._rebuild()

    def show_contact(self, cid: int, card: Card, *, kept_in: Iterable[str] = (), tags: Iterable[str] = (),
                     editable: bool = True) -> None:
        """Show one contact. `kept_in` are the account names; `editable` is False in the bin."""
        self.cid = int(cid)
        self._card = card
        self._kept = list(kept_in)
        self._tags = list(tags)
        self.heading.setText(display_name(card))
        line = work_line(card)
        self.work.setText(line)
        self.work.setVisible(bool(line))
        self.star.setVisible(bool(card.favourite))
        self.edit_button.setEnabled(editable)
        self._rebuild()

    def set_kept(self, names: Iterable[str]) -> None:
        """After the editor changed where the contact is kept."""
        self._kept = list(names)
        self._rebuild()

    def retint(self, colours: dict[str, str]) -> None:
        self.star.setPixmap(icon("star", colours.get("warning", "#b06000")).pixmap(16, 16))
        self._rebuild(colours)

    # --------------------------------------------------------------- building
    def _rebuild(self, colours: Optional[dict[str, str]] = None) -> None:
        c = colours or theme_colours()
        self.photo_label.setPixmap(round_photo(self._card.photo, self.heading.text(), PHOTO_SIDE, c))
        if colours is None:
            self.star.setPixmap(icon("star", c.get("warning", "#b06000")).pixmap(16, 16))
        while self._details.count():
            item = self._details.takeAt(0)
            widget = item.widget() if item is not None else None
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
        if self.cid is None:
            return
        ink = c.get("text_dim", "#888888")
        for kind in KINDS:
            values = [f for f in self._card.values(kind) if (f.value or "").strip()]
            if not values:
                continue
            words, glyph, _example = KIND_INFO.get(kind, (kind.title(), "tag", ""))
            self._details.addWidget(self._section(
                glyph, words, [(label_icon(kind, f.label, glyph), f.value, f.label) for f in values], ink))
        notes = self._card.get("notes")
        if notes:
            self._details.addWidget(self._section("sticky-note", "Notes", [("", notes, "")], ink))
        if self._kept:
            self._details.addWidget(self._section("cloud", "Kept in", [("", ", ".join(self._kept), "")], ink))
        if self._card.groups:
            self._details.addWidget(self._section("users", "Groups", [("", ", ".join(self._card.groups), "")], ink))
        if self._tags:
            self._details.addWidget(self._section("tag", "Tags", [("", ", ".join(self._tags), "")], ink))

    @staticmethod
    def _section(glyph: str, words: str, lines: list[tuple[str, str, str]], ink: str) -> QWidget:
        """A titled block: the kind's icon and name, then one line per value with its label beside it."""
        box = QWidget()
        box.setObjectName("contactReadSection")
        box.setAccessibleName(words)
        col = QVBoxLayout(box)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(SPACE_XS)
        title_row = QHBoxLayout()
        title_row.setSpacing(6)
        pic = QLabel()
        pic.setPixmap(icon(glyph, ink).pixmap(16, 16))
        title = QLabel(words)
        title.setObjectName("contactSectionTitle")
        title_row.addWidget(pic)
        title_row.addWidget(title)
        title_row.addStretch(1)
        col.addLayout(title_row)
        grid = QGridLayout()
        grid.setContentsMargins(22, 0, 0, 0)
        grid.setHorizontalSpacing(SPACE_S)
        grid.setVerticalSpacing(3)
        grid.setColumnStretch(1, 1)
        for row, (line_glyph, value, label) in enumerate(lines):
            if line_glyph:
                mark = QLabel()
                mark.setPixmap(icon(line_glyph, ink).pixmap(14, 14))
                grid.addWidget(mark, row, 0, Qt.AlignmentFlag.AlignTop)
            grid.addWidget(_selectable(value), row, 1)
            if label:
                grid.addWidget(_selectable(label, dim=True), row, 2, Qt.AlignmentFlag.AlignTop)
        col.addLayout(grid)
        return box

    def section_titles(self) -> list[str]:
        """The sections shown, in order (for tests and for a screen reader's sake)."""
        return [self._details.itemAt(i).widget().accessibleName() for i in range(self._details.count())]

    def text(self) -> str:
        """Everything the read view says, as one string (tests read it)."""
        parts = [self.heading.text(), self.work.text()]
        parts += [lbl.text() for lbl in self.details.findChildren(QLabel) if lbl.text()]
        return "\n".join(p for p in parts if p)
