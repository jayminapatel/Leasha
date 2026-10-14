"""The repeatable fields of a contact: one row each, any number of each kind, in the order given.

Layer: L5

Order 1k 6d, 6m.2. A `FieldRow` is one email, phone, address, website, date, related person,
social handle, messaging name or free value: a grip to drag it by, the kind's icon, a label
combo (work, home, mobile or the person's own word), the value, a "main" toggle for email and
phone, and a remove button. A `FieldList` holds the rows of one kind under a heading with an Add
button; rows are reordered by dragging the grip or with Up/Down while the grip has the focus
(a drag is not the only way, 6k). Nothing here reads or writes the hub.
"""

from __future__ import annotations

from typing import Callable, Optional

from PySide6.QtCore import QMimeData, QPoint, Qt, Signal
from PySide6.QtGui import QDrag
from PySide6.QtWidgets import (QComboBox, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QToolButton,
                             QVBoxLayout, QWidget)

from app.contacts.card import Field
from app.ui.contacts.logic import KIND_INFO, LABELS, label_icon
from app.ui.contacts.parts import tool_button
from app.ui.theme import SPACE_XS, theme_colours
from app.ui.widgets.region_box import CountryBox
from app.ui.widgets.icons import icon

__all__ = ["FieldRow", "FieldList", "Grip", "ADDRESS_PARTS"]

ADDRESS_PARTS = (("street", "Street"), ("city", "Town or city"), ("region", "County or state"),
                 ("postcode", "Postcode"), ("country", "Country"))
MIME = "application/x-leasha-field-row"


class Grip(QToolButton):
    """The handle a row is dragged by; Up and Down move it when it has the focus."""

    move_requested = Signal(int)
    drag_started = Signal()

    def __init__(self, words: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setAutoRaise(True)
        self.setAccessibleName(words)
        self.setToolTip("Drag to reorder, or press Up or Down while this has the focus")
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self.setProperty("glyph", "grip-vertical")
        self.setIcon(icon("grip-vertical", theme_colours().get("text_faint", "#888888")))
        self._press: Optional[QPoint] = None

    def mousePressEvent(self, event) -> None:                       # noqa: N802
        self._press = event.position().toPoint()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:                        # noqa: N802
        if self._press is not None and (event.position().toPoint() - self._press).manhattanLength() > 6:
            self._press = None
            self.drag_started.emit()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:                     # noqa: N802
        self._press = None
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event) -> None:                         # noqa: N802
        if event.key() in (Qt.Key.Key_Up, Qt.Key.Key_Down):
            self.move_requested.emit(-1 if event.key() == Qt.Key.Key_Up else 1)
            event.accept()
            return
        super().keyPressEvent(event)


class FieldRow(QWidget):
    changed = Signal()
    remove_requested = Signal(object)
    main_toggled = Signal(object, bool)

    def __init__(self, kind: str, field: Optional[Field] = None, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.kind = kind
        self._orig = field
        self._primary = bool(field.primary) if field else False
        self._touched = False
        heading, _glyph, hint = KIND_INFO[kind]
        one = heading.rstrip("s").lower() if kind in ("related", "date") else heading.lower()
        self.grip = Grip(f"Move this {one}")
        self.grip.move_requested.connect(lambda d: self.parent_list() and self.parent_list().move_row(self, d))
        self.grip.drag_started.connect(lambda: self.parent_list() and self.parent_list().start_drag(self))
        self.glyph = QLabel()
        self.glyph.setFixedWidth(18)
        self.label = QComboBox()
        self.label.setEditable(True)
        self.label.addItems(list(LABELS[kind]))
        self.label.setMinimumWidth(96)
        self.label.setMaximumWidth(130)
        self.label.setAccessibleName(f"Label of this {one}")
        self.label.setToolTip("Work, home, mobile - or type your own word")
        self.label.setCurrentText(field.label if field and field.label else "")
        if not (field and field.label):
            self.label.setCurrentText(LABELS[kind][0])
        self.label.currentTextChanged.connect(self._edited)
        self.label.currentTextChanged.connect(lambda _t: self._paint_glyph())
        # A word, not just an icon: "Default" filled in when it is this one, "Set default" when it is not.
        self.main = tool_button("badge-check", f"Main {one}", f"Make this the default {one}", checkable=True)
        self.main.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.main.setProperty("defaultMarker", True)
        self.main.setChecked(self._primary)
        self._paint_default()
        self.main.setVisible(kind in ("email", "phone"))
        self.main.toggled.connect(self._main)
        self.remove = tool_button("x", f"Remove this {one}", f"Remove this {one} from the contact")
        self.remove.clicked.connect(lambda: self.remove_requested.emit(self))
        self.parts: dict[str, QWidget] = {}
        self.value = QLineEdit()
        self.value.setPlaceholderText(hint)
        self.value.setAccessibleName(f"{heading} value")
        self.value.setToolTip(f"The {one}")
        self.value.textChanged.connect(self._edited)
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(SPACE_XS)
        row.addWidget(self.grip)
        row.addWidget(self.glyph)
        row.addWidget(self.label)
        if kind == "address":
            # An address is a block, not a line: the label and the buttons on one line, and under it
            # street across the width, then town and postcode, then county and country - each box the
            # width it needs instead of five boxes squeezed into whatever the label left over.
            row.addStretch(1)
        else:
            row.addWidget(self.value, 1)
        row.addWidget(self.main)
        row.addWidget(self.remove)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(SPACE_XS)
        outer.addLayout(row)
        if kind == "address":
            grid = QGridLayout()
            grid.setContentsMargins(46, 0, 0, 0)         # under the label, clear of the grip and the icon
            grid.setHorizontalSpacing(SPACE_XS)
            grid.setVerticalSpacing(SPACE_XS)
            where = {"street": (0, 0, 1, 2), "city": (1, 0, 1, 1), "postcode": (1, 1, 1, 1),
                     "region": (2, 0, 1, 1), "country": (2, 1, 1, 1)}
            for key, words in ADDRESS_PARTS:
                edit = CountryBox() if key == "country" else QLineEdit()
                edit.setPlaceholderText(words)
                edit.setAccessibleName(f"Address {words.lower()}")
                edit.setToolTip(words)
                edit.textChanged.connect(self._part_edited)
                self.parts[key] = edit
                grid.addWidget(edit, *where[key])
            grid.setColumnStretch(0, 1)
            grid.setColumnStretch(1, 1)
            outer.addLayout(grid)
        if field is not None:
            self._load(field)
        self._paint_glyph()

    def parent_list(self) -> Optional["FieldList"]:
        w = self.parentWidget()
        while w is not None and not isinstance(w, FieldList):
            w = w.parentWidget()
        return w

    def _load(self, f: Field) -> None:
        for w in (self.value, *self.parts.values(), self.label):
            w.blockSignals(True)
        if self.kind == "address":
            if f.data:
                for key, edit in self.parts.items():
                    edit.setText(f.data.get(key, ""))
            else:
                self.parts["street"].setText(f.value)
        else:
            self.value.setText(f.value)
        for w in (self.value, *self.parts.values(), self.label):
            w.blockSignals(False)

    def _paint_glyph(self) -> None:
        name = label_icon(self.kind, self.label.currentText())
        self.glyph.setPixmap(icon(name, theme_colours().get("text_dim", "#888888")).pixmap(16, 16))

    def retint(self) -> None:
        self._paint_glyph()
        self.grip.setIcon(icon("grip-vertical", theme_colours().get("text_faint", "#888888")))

    def _edited(self, *_a: object) -> None:
        if self.kind == "date":
            self._paint_glyph()
        self.changed.emit()

    def _part_edited(self, *_a: object) -> None:
        self._touched = True
        self.changed.emit()

    def _paint_default(self) -> None:
        on = self.main.isChecked()
        colours = theme_colours()
        self.main.setIcon(icon("badge-check", colours.get("accent_on", "#ffffff") if on else colours.get("text_dim", "#888888")))
        self.main.setText("Default" if on else "Set default")
        self.main.setToolTip(f"This is the default {self.kind}. Pick another row's button to change it." if on
                             else f"Make this the default {self.kind}")

    def _main(self, on: bool) -> None:
        self._primary = on
        self._paint_default()
        self.main_toggled.emit(self, on)
        self.changed.emit()

    def set_primary(self, on: bool) -> None:
        self._primary = on
        self.main.blockSignals(True)
        self.main.setChecked(on)
        self.main.blockSignals(False)
        self._paint_default()

    def is_blank(self) -> bool:
        if self.kind == "address":
            return not any(e.text().strip() for e in self.parts.values()) and \
                not (self._orig is not None and not self._orig.data and self._orig.value.strip()
                     and not self._touched)
        return not self.value.text().strip()

    def to_field(self) -> Field:
        label = self.label.currentText().strip()
        if self.kind == "address":
            if self._orig is not None and not self._touched:
                return Field("address", self._orig.value, label, dict(self._orig.data), self._primary)
            data = {k: e.text().strip() for k, e in self.parts.items() if e.text().strip()}
            return Field("address", ", ".join(data.values()), label, data, self._primary)
        return Field(self.kind, self.value.text().strip(), label, dict(self._orig.data) if self._orig else {},
                     self._primary)

    def set_source_tip(self, text: str) -> None:
        for w in (self.value, *self.parts.values()):
            if text:
                base = w.toolTip().split("\n")[0]
                w.setToolTip(f"{base}\n{text}")
        self.setToolTip(text)

    def focus_value(self) -> None:
        (next(iter(self.parts.values())) if self.parts else self.value).setFocus()


class FieldList(QWidget):
    """Every row of one kind, with its heading and Add button."""

    changed = Signal()

    def __init__(self, kind: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.kind = kind
        self.rows: list[FieldRow] = []
        self._dragging: Optional[FieldRow] = None
        heading, glyph, _hint = KIND_INFO[kind]
        self.title = QLabel(heading)
        self.title.setObjectName("contactSectionTitle")
        self.icon_label = QLabel()
        self.icon_label.setPixmap(icon(glyph, theme_colours().get("text_dim", "#888888")).pixmap(16, 16))
        self._glyph = glyph
        self.add_button = tool_button("plus", f"Add {heading.lower()}", f"Add another {heading.lower()}")
        self.add_button.clicked.connect(lambda: self.add_row(None, focus=True))
        head = QHBoxLayout()
        head.setContentsMargins(0, 0, 0, 0)
        head.addWidget(self.icon_label)
        head.addWidget(self.title)
        head.addStretch(1)
        head.addWidget(self.add_button)
        self._rows_box = QVBoxLayout()
        self._rows_box.setContentsMargins(0, 0, 0, 0)
        self._rows_box.setSpacing(SPACE_XS)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(SPACE_XS)
        outer.addLayout(head)
        outer.addLayout(self._rows_box)
        self.setAcceptDrops(True)

    # ----------------------------------------------------------------- rows
    def set_fields(self, fields: list[Field], tip_for: Optional[Callable[[Field], str]] = None) -> None:
        for r in self.rows:
            r.setParent(None)
            r.deleteLater()
        self.rows = []
        for f in fields:
            row = self.add_row(f, emit=False)
            if tip_for is not None:
                row.set_source_tip(tip_for(f))

    def add_row(self, field: Optional[Field] = None, *, focus: bool = False, emit: bool = True) -> FieldRow:
        row = FieldRow(self.kind, field)
        row.changed.connect(self.changed.emit)
        row.remove_requested.connect(self._remove)
        row.main_toggled.connect(self._only_main)
        self.rows.append(row)
        self._rows_box.addWidget(row)
        if focus:
            row.focus_value()
        if emit:
            self.changed.emit()
        return row

    def _remove(self, row: FieldRow) -> None:
        if row in self.rows:
            self.rows.remove(row)
            row.setParent(None)
            row.deleteLater()
            self.changed.emit()

    def _only_main(self, row: FieldRow, on: bool) -> None:
        if on:
            for other in self.rows:
                if other is not row:
                    other.set_primary(False)

    def fields(self) -> list[Field]:
        return [r.to_field() for r in self.rows if not r.is_blank()]

    def move_row(self, row: FieldRow, delta: int) -> None:
        i = self.rows.index(row)
        j = max(0, min(len(self.rows) - 1, i + delta))
        if i == j:
            return
        self.rows.insert(j, self.rows.pop(i))
        self._relayout()
        row.grip.setFocus()
        self.changed.emit()

    def _relayout(self) -> None:
        for r in self.rows:
            self._rows_box.removeWidget(r)
        for r in self.rows:
            self._rows_box.addWidget(r)

    # ----------------------------------------------------------------- drag
    def start_drag(self, row: FieldRow) -> None:
        self._dragging = row
        drag = QDrag(row.grip)
        mime = QMimeData()
        mime.setData(MIME, str(self.rows.index(row)).encode("ascii"))
        drag.setMimeData(mime)
        drag.setPixmap(row.grab())
        drag.exec(Qt.DropAction.MoveAction)
        self._dragging = None

    def dragEnterEvent(self, event) -> None:                         # noqa: N802
        if self._dragging is not None and event.mimeData().hasFormat(MIME):
            event.acceptProposedAction()

    def dragMoveEvent(self, event) -> None:                          # noqa: N802
        if self._dragging is not None and event.mimeData().hasFormat(MIME):
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:                              # noqa: N802
        row = self._dragging
        if row is None:
            return
        y = event.position().toPoint().y()
        target = sum(1 for r in self.rows if r is not row and r.geometry().center().y() < y)
        self.rows.remove(row)
        self.rows.insert(target, row)
        self._relayout()
        event.acceptProposedAction()
        self.changed.emit()

    def retint(self) -> None:
        self.icon_label.setPixmap(icon(self._glyph, theme_colours().get("text_dim", "#888888")).pixmap(16, 16))
        for r in self.rows:
            r.retint()
