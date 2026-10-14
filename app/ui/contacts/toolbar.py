"""The bar above the list: new, the two views, the tools, history, bin, favourite.

Layer: L5

Order 1k 6m.3, 6k; labelled in UI phase B (2026-10-13). Each button is an icon and its words, with
its tooltip and accessible name - the same as the bars on the other pages - and the glyph is in
the theme's colour and follows a theme change. The words are the ones each button always had as
its accessible name. When the window is too narrow for every word, the bar gives up words in a
fixed order (`FIT_LEVELS`): first the two views and the tools, then the rest; New contact keeps
its words. The tooltips and names stay, so nothing is lost but width. The filter box is `/`. The
buttons decide nothing: they emit, and the page acts.
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QButtonGroup, QFrame, QHBoxLayout, QLabel, QLineEdit, QToolButton, QWidget

from app.ui.theme import theme_colours
from app.ui.contacts.parts import name_inner_buttons, tool_button
from app.ui.presenter.contact_commands import CONTACT_COMMANDS, contact_matching
from app.ui.widgets import menus
from app.ui.widgets.command_popup import attach_to
from app.ui.widgets.icons import icon

__all__ = ["ContactToolbar", "TOOLS", "MORE_TOOLS", "enrich_words", "labelled"]

#: (openers name, glyph, words, tooltip)
TOOLS = (
    ("import", "upload", "Import contacts", "Bring in contacts from a vCard or CSV file"),
    ("export", "download", "Export contacts", "Save contacts as vCard, CSV or Excel - the picked ones, or all shown"),
    ("print", "printer", "Print contacts", "Print labels, envelopes or an address book"),
    ("duplicates", "merge", "Find duplicates", "Find people who are in your book twice, and merge them"),
    ("sync", "refresh-cw", "Sync now", "Sync the accounts that keep contacts now"),
)

#: The "More tools" menu (6p, 7): (openers name, glyph, words, tooltip). The toolbar is full, so the
#: tools that need a person's look first sit behind one button. "Enrich" says what it will run on.
MORE_TOOLS = (
    ("cleanup", "eraser", "Clean up contacts…", "Tidy phone numbers and names, fill blank companies, "
     "and find contacts that are empty or have repeated values - you see every change first"),
    ("enrich", "wand-sparkles", "Enrich selection…", "Look for missing details in your own mail and photos "
     "(and the internet, if you switched that on) - nothing is kept until you accept it"),
    ("duplicates", "merge", "Find duplicates…", "Find people who are in your book twice, and merge them"),
    ("conflicts", "triangle-alert", "Changed in two places…", "Review the details changed in Leasha and at an "
     "account, and accept which value to keep - all at once or one by one"),
    ("select_all", "list-checks", "Select all contacts", "Pick every contact in the list (Ctrl+A), to act on them "
     "together"),
)


def labelled(button: QToolButton, on: bool = True) -> QToolButton:
    """Show a bar button's words beside its icon (`on`), or its icon alone. The words are its name."""
    if not button.text():
        button.setText(button.accessibleName())
    button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon if on
                              else Qt.ToolButtonStyle.ToolButtonIconOnly)
    return button


def enrich_words(picked: int) -> str:
    """The words of the Enrich entry for how many contacts are picked (7i)."""
    if picked == 1:
        return "Enrich this contact…"
    if picked > 1:
        return "Enrich selection…"
    return "Enrich the contacts shown…"


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
        self.more_button = QToolButton()
        self.more_button.setAutoRaise(True)
        self.more_button.setIconSize(self.new_button.iconSize())
        self.more_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        self.more_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.more_button.setAccessibleName("More tools")
        self.more_button.setText("More tools")
        self.more_button.setToolTip("More tools: clean up, enrich and find duplicates")
        self.more_button.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.more_button.setProperty("glyph", "ellipsis")
        self.more_button.setIcon(icon("ellipsis", theme_colours().get("text_dim", "#888888")))
        self.more_menu = menus.attach(self.more_button, [
            menus.Item(words, lambda n=name: self.tool_requested.emit(n), icon=glyph, tip=tip, key=name)
            for name, glyph, words, tip in MORE_TOOLS])
        self.more_actions: dict[str, object] = menus.lines(self.more_menu)
        for name, _glyph, _words, tip in MORE_TOOLS:
            self.more_actions[name].setStatusTip(tip)
        row.addWidget(self.more_button)
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
        for b in self.findChildren(QToolButton):
            if b.accessibleName():
                labelled(b)
        self.fit_level = 0
        self._fitting = False
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
        self._popup = attach_to(self.filter, catalogue=CONTACT_COMMANDS, matcher=contact_matching,
                                resolve=lambda n: next((c for c in CONTACT_COMMANDS if c.name == n.strip().lower().rstrip(":")), None))
        row.addWidget(self.count)
        row.addWidget(self.filter)

    def fit_levels(self) -> tuple[tuple[QToolButton, ...], ...]:
        """Which buttons show their icon alone at each level: none, then the views and the tools,
        then all but New contact."""
        first = (self.list_button, self.cards_button, *self.tools.values())
        rest = (self.more_button, self.history_button, self.bin_button, self.favourite_button)
        return ((), first, first + rest)

    def fit(self, width: int) -> int:
        """Words where they fit in `width`: the first level whose bar is no wider. Returns it."""
        if self._fitting:
            return self.fit_level
        self._fitting = True
        try:
            levels = self.fit_levels()
            chosen = len(levels) - 1
            for level, icons_only in enumerate(levels):
                for b in levels[-1]:
                    labelled(b, b not in icons_only)
                if self.layout().sizeHint().width() <= width:
                    chosen = level
                    break
            self.fit_level = chosen
            return chosen
        finally:
            self._fitting = False

    def resizeEvent(self, event: object) -> None:                 # noqa: N802 - Qt's naming
        super().resizeEvent(event)
        self.fit(self.width())

    @staticmethod
    def _bar() -> QFrame:
        line = QFrame()
        line.setFrameShape(QFrame.Shape.VLine)
        line.setFrameShadow(QFrame.Shadow.Plain)
        line.setFixedHeight(18)
        return line

    def set_picked(self, picked: int) -> None:
        """How many contacts are picked: the Enrich entry says what it will run on."""
        self.more_actions["enrich"].setText(enrich_words(picked))

    def retint(self, colours: dict[str, str]) -> None:
        ink = colours.get("text_dim", "#888888")
        self.more_button.setIcon(icon("ellipsis", ink))
        menus.retint(self.more_menu)

    def set_view(self, mode: str) -> None:
        (self.cards_button if mode == "cards" else self.list_button).setChecked(True)

    def focus_filter(self) -> None:
        self.filter.setFocus(Qt.FocusReason.ShortcutFocusReason)
        self.filter.selectAll()
