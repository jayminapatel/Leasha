"""Small controls the Contacts page is made of: icon buttons and the token box.

Layer: L5

Order 1k 6k, 6m. Every icon-only control here has a tooltip and an accessible name (6k) and its
icon comes from `icon(name, colour)` in the theme's colour; `retint_tools` redraws them when the
theme changes. A toolbar of icons is `QToolButton`s, which the button system leaves to the
rail's way of working (it governs the action buttons with words, `QPushButton`).
"""

from __future__ import annotations

from typing import Any, Iterable, Optional

from PySide6.QtCore import QStringListModel, Qt, Signal
from PySide6.QtWidgets import (QCompleter, QHBoxLayout, QLabel, QLineEdit, QToolButton, QWidget)

from app.ui.theme import SPACE_XS, theme_colours
from app.ui.widgets.flow_layout import FlowLayout
from app.ui.widgets.icons import icon, icon_size

__all__ = ["name_inner_buttons", "tool_button", "retint_tools", "TokenEdit", "set_glyph"]


def set_glyph(button: QToolButton, glyph: str, colours: Optional[dict[str, str]] = None) -> None:
    c = colours or theme_colours()
    button.setProperty("glyph", glyph)
    ink = c.get("accent_text" if button.isChecked() else "text_dim", "#888888")
    button.setIcon(icon(glyph, ink))


def tool_button(glyph: str, words: str, tooltip: str = "", *, checkable: bool = False,
                parent: Optional[QWidget] = None) -> QToolButton:
    """An icon-only button: the words are its accessible name, the tooltip says what it does."""
    button = QToolButton(parent)
    button.setAutoRaise(True)
    button.setCheckable(checkable)
    button.setIconSize(icon_size("toolbar"))
    button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
    button.setAccessibleName(words)
    button.setToolTip(tooltip or words)
    button.setFocusPolicy(Qt.FocusPolicy.TabFocus)
    set_glyph(button, glyph)
    if checkable:
        button.toggled.connect(lambda _on, b=button: set_glyph(b, str(b.property("glyph"))))
    return button


def name_inner_buttons(edit: QLineEdit, words: str = "Clear") -> None:
    """Qt's own clear button inside a line edit has no name; a screen reader gets one here."""
    for b in edit.findChildren(QToolButton):
        if not b.accessibleName():
            b.setAccessibleName(words)
            b.setToolTip(words)


def retint_tools(root: QWidget, colours: dict[str, str]) -> None:
    for button in root.findChildren(QToolButton):
        glyph = button.property("glyph")
        if glyph:
            set_glyph(button, str(glyph), colours)


class TokenEdit(QWidget):
    """Groups or tags as removable chips with a box to add another (6d, 6s).

    `colours` maps a token to its colour for tags; groups pass none. Adding is Enter in the
    box (a completer offers the names that exist); `changed` fires with the full list.
    """

    changed = Signal(list)

    def __init__(self, words: str, glyph: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._words = words
        self._glyph = glyph
        self._items: list[str] = []
        self._colours: dict[str, str] = {}
        self._flow = FlowLayout(self, spacing=4)
        self.entry = QLineEdit()
        self.entry.setPlaceholderText(f"Add {words}…")
        self.entry.setAccessibleName(f"Add {words}")
        self.entry.setClearButtonEnabled(False)
        self.entry.setMinimumWidth(120)
        self.entry.returnPressed.connect(self._add_typed)
        self._names = QStringListModel(self)
        self._completer = QCompleter(self._names, self.entry)
        self._completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.entry.setCompleter(self._completer)
        self._rebuild()

    def set_known(self, names: Iterable[str], colours: Optional[dict[str, str]] = None) -> None:
        self._names.setStringList(sorted(set(names), key=str.casefold))
        self._colours = dict(colours or {})
        self._rebuild()

    def retint(self) -> None:
        self._rebuild()

    def items(self) -> list[str]:
        return list(self._items)

    def set_items(self, items: Iterable[str]) -> None:
        self._items = list(dict.fromkeys(items))
        self._rebuild()

    def add(self, name: str) -> None:
        name = name.strip()
        if name and name.casefold() not in {i.casefold() for i in self._items}:
            self._items.append(name)
            self._rebuild()
            self.changed.emit(self.items())

    def remove(self, name: str) -> None:
        if name in self._items:
            self._items.remove(name)
            self._rebuild()
            self.changed.emit(self.items())

    def _add_typed(self) -> None:
        self.add(self.entry.text())
        self.entry.clear()

    def _rebuild(self) -> None:
        while self._flow.count():
            item = self._flow.takeAt(0)
            widget = item.widget() if item is not None else None
            if widget is not None and widget is not self.entry:
                widget.deleteLater()
        for name in self._items:
            self._flow.addWidget(self._chip(name))
        self._flow.addWidget(self.entry)

    def _chip(self, name: str) -> QWidget:
        c = theme_colours()
        colour = self._colours.get(name, "")
        chip = QWidget()
        chip.setObjectName("contactToken")
        row = QHBoxLayout(chip)
        row.setContentsMargins(SPACE_XS, 1, 1, 1)
        row.setSpacing(SPACE_XS)
        glyph = QLabel()
        glyph.setPixmap(icon(self._glyph, colour or c.get("text_dim", "#888888")).pixmap(14, 14))
        label = QLabel(name)
        remove = tool_button("x", f"Remove {name}", f"Take {name} off this contact")
        remove.setIconSize(icon_size("inline"))
        remove.clicked.connect(lambda _c=False, n=name: self.remove(n))
        row.addWidget(glyph)
        row.addWidget(label)
        row.addWidget(remove)
        border = colour or c.get("border_strong", "#888888")
        chip.setStyleSheet(f"#contactToken {{ border: 1px solid {border}; border-radius: 9px; }}")
        return chip
