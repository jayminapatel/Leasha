"""A sidebar list of categories beside one visible content page at a time.

Layer: L6 (UI)

The settled structure for both Settings and Indexing (pages-reorg order,
§0.4): a sidebar category list rather than top sub-tabs, because it scales
past five categories without crowding a tab bar — the VS Code pattern the
owner named. Built once here so the two pages share one mechanism rather
than each growing its own.

**Deliberately not a `QStackedWidget`.** A stack's size hint is the maximum
over every page it holds, because Qt has to leave room to switch to the
tallest one without the window jumping — so the smallest category would be
forced as tall as the largest, which is exactly the kind of layout bug this
order exists to remove, not reintroduce. A hidden `QWidget` contributes
nothing to its layout's size hint at all, so plain visibility toggling
inside one `QVBoxLayout` is what keeps each category exactly as tall as
itself, and no taller.

That same visibility mechanism is what lets more than one category be shown
at once — see `show_all_for_filter`, which Settings' filter box (§1b) uses
so that "categories auto-expanding to show hits" is a real state rather than
a metaphor: every category with a match becomes visible together, and the
rest hide, in the same `QVBoxLayout` a single category normally has to
itself.

**The sidebar never scrolls with the page (owner, 2026-10-08).** *"the left
section headers scroll with the right ... the left sections should not scroll
with the right sections"*. Settings and Indexing scrolled differently: `shell.py`
wraps the whole Settings page in one scroll area, so the sidebar sat *inside*
it, moved off the top as the page scrolled, and a wheel over the list (which
has nothing of its own to scroll) passed up to that area and scrolled the page;
Indexing's shelves each carry their own scroll area, beside the sidebar, so
there it stayed put. Now the rule is this widget's, not each caller's: the
content side is one `QScrollArea` (`self.scroll`) and the sidebar is its
sibling, never its descendant - so the list is always full height, always in
view, and a wheel over it can only ever reach whatever holds the whole nav,
never the content. `scroll=False` is for a caller whose pages each scroll
themselves (Indexing - see `widgets/indexing_layout.assemble_pages`), so a
page is never inside two scroll areas.
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtWidgets import QHBoxLayout, QListWidget, QScrollArea, QVBoxLayout, QWidget

__all__ = ["CategoryNav"]


class CategoryNav(QWidget):
    """Sidebar category list + one visible content page at a time."""

    #: The category name just selected (by a click, never by `show_category`
    #: with `persist=False` — that path is a restore, not a choice, and must
    #: not be indistinguishable from one on the signal a caller persists from).
    category_changed = Signal(str)

    def __init__(self, parent: Optional[QWidget] = None, *, scroll: bool = True) -> None:
        super().__init__(parent)
        self._pages: dict[str, QWidget] = {}
        self._order: list[str] = []

        self.sidebar = QListWidget()
        self.sidebar.setObjectName("categorySidebar")
        # A floor, not a fixed width - long category names still wrap rather
        # than push the content pane off-screen on a narrow window.
        self.sidebar.setMaximumWidth(190)
        self.sidebar.setMinimumWidth(120)
        # See `_fit_sidebar`: the floor and ceiling above are only where the
        # sidebar starts; once it is on screen it is sized to its own words.
        # A sideways scrollbar under five short names says "something is cut
        # off" and nothing is, so it is never offered.
        self.sidebar.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.sidebar.currentTextChanged.connect(self._on_row_selected)

        self._content = QWidget()
        self._content.setObjectName("categoryContent")
        self._content_layout = QVBoxLayout(self._content)
        self._content_layout.setContentsMargins(0, 0, 0, 0)

        # The one scroll area on the content side, or None when every page
        # brings its own (see the module docstring). `scrollable` is the same
        # wrap each Indexing shelf gets and Settings used to get whole: it
        # scrolls vertically, never sideways, and its width floor is what
        # makes the content track the viewport's width - without an explicit
        # floor the area sizes it to its widest drop-down (1,853 pixels on
        # Search, measured) and cuts off the right-hand side.
        self.scroll: Optional[QScrollArea] = None
        if scroll:
            from app.ui.widgets.scroll import scrollable

            self.scroll = scrollable(self._content)
            self.scroll.setObjectName("categoryScroll")

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        # The list is as tall as the nav, and the nav as tall as the window
        # gives it: the content's own height stops at the scroll area.
        layout.addWidget(self.sidebar)
        layout.addWidget(self.scroll if self.scroll is not None else self._content, stretch=1)

    # -- building -------------------------------------------------------------

    #: §0.3 (owner: icons wherever possible). Category name -> Lucide glyph,
    #: for both pages this widget serves. A name not listed gets no icon and
    #: nothing else changes; the label is still the label.
    ICONS = {
        "What's indexed": "folder", "Search": "search", "Models & AI": "cpu",
        "Appearance": "palette", "Storage & maintenance": "hard-drive",
        "Status": "chart-column", "What gets read": "eye", "Schedule": "clock",
        "Tuning": "sliders-horizontal",
    }

    def retint(self, colours: dict) -> None:
        """Re-render the category icons for a palette (called by the window)."""
        from PySide6.QtCore import QSize

        from app.ui.widgets.icons import icon
        self.sidebar.setIconSize(QSize(16, 16))
        for row in range(self.sidebar.count()):
            item = self.sidebar.item(row)
            name = self.ICONS.get(item.text())
            if name:
                item.setIcon(icon(name, colours.get("text_dim", "#888888")))
        self._fit_sidebar()

    #: The widest the sidebar may grow to fit its names. Past this a name is
    #: allowed to be cut rather than take the page's room.
    SIDEBAR_CEILING = 260

    def _fit_sidebar(self) -> None:
        """Make the sidebar exactly wide enough for its longest name.

        **Why.** It had a floor of 120 and a ceiling of 190 pixels, and the
        page beside it takes every pixel it can. On Settings' Search category,
        whose page is wide, the sidebar was pushed down to its 120 floor and
        read "What's index" and "Storage & m"; on Appearance, at 190, "Storage
        & maintenance" was a few pixels too wide and a sideways scrollbar
        appeared under the list (both grabbed 2026-09-27, order 0x section 9).
        At 125% display scaling on Windows the names are wider again.

        So the list is asked how wide its widest row is - icon, padding and
        the stylesheet's own item border included, because `sizeHintForColumn`
        goes through the same style that paints the row - and held at that
        width, both floor and ceiling, so it neither squeezes nor sprawls.
        Called on show and whenever the font, the style or the icons change;
        it touches nothing but two numbers on one widget.
        """
        if self.sidebar.count() == 0:
            return
        self.sidebar.ensurePolished()
        frame = 2 * self.sidebar.frameWidth()
        # A little slack, because a label measured to the exact pixel is the
        # one a rounding difference on another machine cuts by a letter.
        needed = self.sidebar.sizeHintForColumn(0) + frame + 6
        width = max(120, min(needed, self.SIDEBAR_CEILING))
        if width != self.sidebar.minimumWidth() or width != self.sidebar.maximumWidth():
            self.sidebar.setFixedWidth(width)

    def showEvent(self, event) -> None:                       # noqa: N802 - Qt's name
        super().showEvent(event)
        self._fit_sidebar()

    def changeEvent(self, event) -> None:                     # noqa: N802 - Qt's name
        super().changeEvent(event)
        if event.type() in (QEvent.Type.FontChange, QEvent.Type.StyleChange):
            self._fit_sidebar()

    def add_category(self, name: str, page: QWidget) -> None:
        """Register one category's content widget, in display order.

        `page` starts hidden; the first category added is shown by default,
        matching "first run opens the first category" — a caller that later
        restores a remembered category just calls `show_category` again.
        """
        page.setVisible(False)
        self._pages[name] = page
        self._order.append(name)
        self._content_layout.addWidget(page)
        self.sidebar.addItem(name)
        if len(self._order) == 1:
            # Selected, but silently: a caller restoring a remembered
            # category (persist=False) has not had the chance to run yet,
            # and a real `category_changed` here would let a listener that
            # persists selections (Settings' §1c) overwrite the remembered
            # value with "first category" before construction even finishes.
            self.sidebar.blockSignals(True)
            self.sidebar.setCurrentRow(0)
            self.sidebar.blockSignals(False)
            self._show_only(name)

    # -- navigation -------------------------------------------------------------

    def show_category(self, name: str, *, persist: bool = True) -> None:
        """Show one category, hide the rest.

        `persist=False` is a restore (from remembered state): it moves the
        sidebar's selection and the visible page without emitting
        `category_changed`, so a caller applying a remembered value does not
        immediately re-save the value it just read.
        """
        if name not in self._pages:
            return
        if persist:
            self.sidebar.setCurrentRow(self._order.index(name))
        else:
            self.sidebar.blockSignals(True)
            self.sidebar.setCurrentRow(self._order.index(name))
            self.sidebar.blockSignals(False)
            self._show_only(name)

    def current_category(self) -> str:
        item = self.sidebar.currentItem()
        if item is not None:
            return item.text()
        return self._order[0] if self._order else ""

    def category_names(self) -> list[str]:
        return list(self._order)

    def page(self, name: str) -> Optional[QWidget]:
        return self._pages.get(name)

    def set_sidebar_enabled(self, enabled: bool) -> None:
        """Filtering (Settings' §1b) overrides which categories are visible;
        while it is active the sidebar's own selection is meaningless, so it
        is disabled rather than left clickable against a state it does not
        control."""
        self.sidebar.setEnabled(enabled)

    def show_all_for_filter(self, visible_names: set) -> None:
        """Filtering mode: show every category with a hit, hide the rest."""
        for key, page in self._pages.items():
            page.setVisible(key in visible_names)
        self._to_top()

    def restore_single_view(self) -> None:
        """Filter cleared: back to exactly the sidebar's current category."""
        self._show_only(self.current_category())

    # -- internals -------------------------------------------------------------

    def _on_row_selected(self, name: str) -> None:
        if not name:
            return
        self._show_only(name)
        self.category_changed.emit(name)

    def _show_only(self, name: str) -> None:
        for key, page in self._pages.items():
            page.setVisible(key == name)
        self._to_top()

    def _to_top(self) -> None:
        """A newly shown category opens at its top, not at wherever the last
        one had been scrolled to - the scroll area is shared by all of them."""
        if self.scroll is not None:
            self.scroll.verticalScrollBar().setValue(0)
