"""The row of icons above the list: new, the two views, the tools, history, bin, favourite.

Layer: L5

Order 1k 6m.3, 6k. Icon-only buttons, each with its tooltip and accessible name (the glyph is in
the theme's colour and follows a theme change); the filter box is `/`. The buttons decide
nothing: they emit, and the page acts.
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QButtonGroup, QFrame, QHBoxLayout, QLabel, QLineEdit, QWidget

from app.ui.contacts.parts import name_inner_buttons, tool_button

__all__ = ["ContactToolbar", "TOOLS"]

#: (openers name, glyph, words, tooltip)
TOOLS = (
    ("import", "upload", "Import contacts", "Bring in contacts from a vCard or CSV file"),
    ("export", "download", "Export contacts", "Save contacts as vCard, CSV or Excel - the picked ones, or all shown"),
    ("print", "printer", "Print contacts", "Print labels, envelopes or an address book"),
    ("duplicates", "merge", "Find duplicates", "Find people who are in your book twice, and merge them"),
    ("sync", "refresh-cw", "Sync now", "Sync the accounts that keep contacts now"),
)


class ContactToolbar(QWidget):
    new_requested = Signal()
    view_changed = Signal(str)
    tool_requested = Signal(str)
    history_toggled = Signal(bool)
    bin_requested = Signal()
    favourite_requested = Signal()
    text_changed = Signal(str)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("contactToolbar")
        self.new_button = tool_button("user-plus", "New contact", "New contact (Ctrl+N)")
        self.new_button.clicked.connect(self.new_requested.emit)
        self.list_button = tool_button("list", "List view", "Show contacts as a table", checkable=True)
        self.cards_button = tool_button("layout-grid", "Cards view", "Show contacts as business cards",
                                        checkable=True)
        self._views = QButtonGroup(self)
        self._views.setExclusive(True)
        for b, name in ((self.list_button, "list"), (self.cards_button, "cards")):
            self._views.addButton(b)
            b.clicked.connect(lambda _c=False, n=name: self.view_changed.emit(n))
        self.list_button.setChecked(True)
        self.tools: dict[str, object] = {}
        row = QHBoxLayout(self)
        row.setContentsMargins(6, 4, 6, 4)
        row.setSpacing(2)
        row.addWidget(self.new_button)
        row.addWidget(self._bar())
        row.addWidget(self.list_button)
        row.addWidget(self.cards_button)
        row.addWidget(self._bar())
        for name, glyph, words, tip in TOOLS:
            b = tool_button(glyph, words, tip)
            b.clicked.connect(lambda _c=False, n=name: self.tool_requested.emit(n))
            self.tools[name] = b
            row.addWidget(b)
        row.addWidget(self._bar())
        self.history_button = tool_button("history", "History", "Show the changes made to the picked contact",
                                          checkable=True)
        self.history_button.toggled.connect(self.history_toggled.emit)
        self.bin_button = tool_button("trash-2", "Bin", "Show the bin: contacts deleted in the last 30 days")
        self.bin_button.clicked.connect(self.bin_requested.emit)
        self.favourite_button = tool_button("star", "Favourite", "Make the picked contacts favourites "
                                            "(or not, if they all are)")
        self.favourite_button.clicked.connect(self.favourite_requested.emit)
        for b in (self.history_button, self.bin_button, self.favourite_button):
            row.addWidget(b)
        row.addStretch(1)
        self.count = QLabel("")
        self.count.setObjectName("resultsSummary")
        self.count.setAccessibleName("How many contacts are shown")
        self.filter = QLineEdit()
        self.filter.setPlaceholderText("Find contacts - name, company, email or phone   ( / )")
        self.filter.setAccessibleName("Find contacts")
        self.filter.setClearButtonEnabled(True)
        self.filter.setMinimumWidth(240)
        self.filter.textChanged.connect(self.text_changed.emit)
        name_inner_buttons(self.filter, "Clear the search box")
        row.addWidget(self.count)
        row.addWidget(self.filter)

    @staticmethod
    def _bar() -> QFrame:
        line = QFrame()
        line.setFrameShape(QFrame.Shape.VLine)
        line.setFrameShadow(QFrame.Shadow.Plain)
        line.setFixedHeight(18)
        return line

    def set_view(self, mode: str) -> None:
        (self.cards_button if mode == "cards" else self.list_button).setChecked(True)

    def focus_filter(self) -> None:
        self.filter.setFocus(Qt.FocusReason.ShortcutFocusReason)
        self.filter.selectAll()
