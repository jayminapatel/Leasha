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
"""

from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import QHBoxLayout, QListWidget, QVBoxLayout, QWidget

__all__ = ["CategoryNav"]


class CategoryNav(QWidget):
    """Sidebar category list + one visible content page at a time."""

    #: The category name just selected (by a click, never by `show_category`
    #: with `persist=False` — that path is a restore, not a choice, and must
    #: not be indistinguishable from one on the signal a caller persists from).
    category_changed = pyqtSignal(str)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._pages: dict[str, QWidget] = {}
        self._order: list[str] = []

        self.sidebar = QListWidget()
        self.sidebar.setObjectName("categorySidebar")
        # A floor, not a fixed width - long category names still wrap rather
        # than push the content pane off-screen on a narrow window.
        self.sidebar.setMaximumWidth(190)
        self.sidebar.setMinimumWidth(120)
        self.sidebar.currentTextChanged.connect(self._on_row_selected)

        self._content = QWidget()
        self._content.setObjectName("categoryContent")
        self._content_layout = QVBoxLayout(self._content)
        self._content_layout.setContentsMargins(0, 0, 0, 0)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.sidebar)
        layout.addWidget(self._content, stretch=1)

    # -- building -------------------------------------------------------------

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
