r"""Getting around the window by keyboard: the palette, page keys, Esc, Ctrl+F, history.

Layer: L5

The UX review of 2026-10-14, the keys and places rows, in one place so the
window (`shell.py`) only binds them:

* **IA-1 / MS-11 - the command palette (Ctrl+K).** `palette_entries` gathers
  what the menu bar declares, every rail page in rail order, every Settings
  category and the Sync window; `presenter/actions.py` matches;
  `widgets/command_palette.py` draws.
* **IA-2 / MS-3 - Esc clears the page in front.** An open find bar closes first;
  then Search's box when Search is in front, otherwise the page's own box.
  A page in the background keeps what was typed in it.
* **IA-3 - Ctrl+1..Ctrl+9** open the rail page at that place, top to bottom, in
  the order the owner dragged them to (`rail.page_order()`). The Go menu lists
  every rail page in that same order (`fill_go_menu`).
* **IA-9 - Ctrl+F** finds in a preview when one has the focus; otherwise it puts
  the cursor in the page's own box; on a page with none, Search.
* **IA-10 - back and forward** (`presenter/page_history.py`): Alt+Left / Alt+Right
  and the mouse's back and forward buttons, between pages and the Photos naming
  page / the Settings category they were left on.
* **MS-4 - type, then arrow** on Files, Mail, Code and Photos (`widgets/arrow_keys.py`).
"""

from __future__ import annotations

from typing import Any, Optional

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import QApplication, QLineEdit, QWidget

from app.core.logging import logger
from app.ui.presenter import actions as palette_words
from app.ui.presenter.page_history import PageHistory

__all__ = ["NavigationController", "page_box", "PAGE_KEYS", "NAMING", "PALETTE_KEY"]

_log = logger.bind(component="ui.navigation")

#: Ctrl+1 .. Ctrl+9.
PAGE_KEYS = tuple(f"Ctrl+{n}" for n in range(1, 10))

#: The key that opens the palette.
PALETTE_KEY = "Ctrl+K"

#: The Photos page's sub-state while its naming page is in front.
NAMING = "naming"

#: The menus whose items are the palette's actions. Go's pages are listed as pages.
_PAGE_MENU = "Go"


def page_box(view: Any) -> Optional[QLineEdit]:
    """The page's own search or filter box, or None for a page that has none.

    By capability, not by name: Search, Files, Mail, Code and Photos call theirs
    `input`, Settings `filter_box`, and Contacts keeps its in the toolbar of a
    page built on its first show. Chat's message box is not a filter, and is
    never cleared or taken by these keys.
    """
    if view is None:
        return None
    for name in ("input", "filter_box"):
        box = getattr(view, name, None)
        if isinstance(box, QLineEdit):
            return box
    toolbar = getattr(getattr(view, "page", None), "toolbar", None)
    box = getattr(toolbar, "filter", None)
    return box if isinstance(box, QLineEdit) else None


class NavigationController(QObject):
    """The window's keyboard ways around. A child of the window."""

    def __init__(self, window: Any) -> None:
        super().__init__(window)
        self._w = window
        self.history = PageHistory()
        self._moving = False
        self.palette: Any = None
        #: Go menu's page items, by page title, and its other items, in order.
        self.go_pages: dict = {}
        self.go_extras: list = []
        #: The Go menu itself, kept from when the menu bar was built.
        self.go_menu: Any = None

    # -- wiring --------------------------------------------------------------------

    def bind(self) -> None:
        """The keys, on the window; the history follows the rail. UI thread, once."""
        w = self._w

        def bind(sequence: str, slot: Any) -> None:
            action = QAction(w)
            action.setShortcut(QKeySequence(sequence))
            action.triggered.connect(lambda _checked=False: slot())
            w.addAction(action)

        bind(PALETTE_KEY, self.open_palette)
        for number, sequence in enumerate(PAGE_KEYS, start=1):
            bind(sequence, lambda n=number: self.go_to_position(n))
        bind("Alt+Left", self.back)
        bind("Alt+Right", self.forward)
        w.rail.currentChanged.connect(self._page_changed)
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)
        self.reset()

    def wire_arrows(self) -> None:
        """MS-4: type, then arrow, on the pages that have a box over a list."""
        from app.ui.widgets.arrow_keys import forward_arrows

        w = self._w
        for view, listing in (
                (getattr(w, "files_view", None), lambda v: v.results),
                (getattr(w, "mail_view", None), lambda v: v.results),
                (getattr(w, "code_view", None), lambda v: v.results.table),
                (getattr(w, "photos_view", None), lambda v: v.browser.active_view)):
            box = page_box(view)
            if box is None or getattr(box, "_arrow_keys", None) is not None:
                continue
            try:
                forward_arrows(box, listing(view))
            except AttributeError as exc:           # a page built differently: no arrows there
                _log.debug("no arrow keys for {}: {}", type(view).__name__, exc)

    # -- pages ---------------------------------------------------------------------

    def title_of(self, view: Any) -> str:
        index = self._w._tab_index.get(view) if view is not None else None
        return self._w.rail.tabText(index) if index is not None else ""

    def view_of(self, title: str) -> Any:
        for view, index in list(self._w._tab_index.items()):
            if self._w.rail.tabText(index) == title:
                return view
        return None

    def index_of(self, title: str) -> int:
        rail = self._w.rail
        for index in range(rail.count()):
            if rail.tabText(index) == title:
                return index
        return -1

    def go_to_position(self, number: int) -> bool:
        """Ctrl+<number>: the rail page at that place, top to bottom. False past the end."""
        order = self._w.rail.page_order()
        if not 1 <= number <= len(order):
            return False
        return self.go_to(order[number - 1])

    def go_to(self, title: str) -> bool:
        """The page called `title`, by the Go menu's item when it has one (Mail lands in
        its box, People in photos on the naming page), else by the rail."""
        action = self.go_pages.get(title)
        if action is not None:
            action.trigger()
            return True
        index = self.index_of(title)
        if index < 0:
            return False
        self._w.rail.setCurrentIndex(index)
        return True

    # -- history -------------------------------------------------------------------

    def sub_state(self, view: Any) -> str:
        """The one thing a page is put back to: Photos' naming page, Settings' category."""
        w = self._w
        if view is not None and view is getattr(w, "photos_view", None):
            centre = getattr(view, "centre", None)
            naming = getattr(view, "_naming", None)
            return NAMING if centre is not None and naming is not None \
                and centre.currentIndex() == 1 else ""
        if view is not None and view is getattr(w, "settings_view", None):
            nav = getattr(view, "_nav", None)
            return nav.current_category() if nav is not None else ""
        return ""

    def reset(self) -> None:
        """Start the history again at the page in front (after the remembered page is restored)."""
        self.history = PageHistory(self.history.cap)
        current = self._w._current_view()
        self.history.visit(self.title_of(current) or self._w.rail.tabText(
            self._w.rail.currentIndex()), self.sub_state(current))

    def _page_changed(self, index: int) -> None:
        if self._moving:
            return
        left = self.history.current
        if left is not None:
            self.history.leave(self.sub_state(self.view_of(left[0])))
        view = self._w._current_view()
        self.history.visit(self._w.rail.tabText(index), self.sub_state(view))

    def remember_sub_state(self) -> None:
        """A page changed what it shows without changing page (Photos to its naming page)."""
        current = self.history.current
        if current is not None:
            self.history.leave(self.sub_state(self.view_of(current[0])))

    def back(self) -> bool:
        return self._step(-1)

    def forward(self) -> bool:
        return self._step(1)

    def _step(self, delta: int) -> bool:
        current = self.history.current
        if current is not None:
            self.history.leave(self.sub_state(self.view_of(current[0])))
        place = self.history.back() if delta < 0 else self.history.forward()
        if place is None:
            return False
        self._restore(place)
        return True

    def _restore(self, place: tuple) -> None:
        title, sub = place
        index = self.index_of(title)
        if index < 0:
            return
        self._moving = True
        try:
            self._w.rail.setCurrentIndex(index)
            view = self.view_of(title)
            if view is not None and view is getattr(self._w, "photos_view", None):
                if sub == NAMING:
                    view.show_naming()
                elif self.sub_state(view) == NAMING:
                    view.show_photos()
            elif view is not None and view is getattr(self._w, "settings_view", None) and sub:
                nav = getattr(view, "_nav", None)
                if nav is not None and sub in nav.category_names():
                    nav.show_category(sub)
        finally:
            self._moving = False

    def eventFilter(self, watched: Any, event: Any) -> bool:      # noqa: N802 - Qt's naming
        """The mouse's back and forward buttons, anywhere in the window."""
        if event.type() != QEvent.Type.MouseButtonPress or not isinstance(watched, QWidget):
            return False
        button = event.button()
        if button not in (Qt.MouseButton.BackButton, Qt.MouseButton.ForwardButton):
            return False
        try:
            if watched.window() is not self._w:
                return False
        except RuntimeError:
            return False
        if button == Qt.MouseButton.BackButton:
            self.back()
        else:
            self.forward()
        return True

    # -- Esc and Ctrl+F ------------------------------------------------------------

    def clear_current(self) -> None:
        """Esc. An open find bar closes first; then the box of the page in front."""
        w = self._w
        for pane in w._preview_panes():
            find = getattr(pane, "find", None)
            if find is not None and find.isVisible():
                find.dismissed.emit()
                return
        view = w._current_view()
        if view is w.search_view:
            w.search_view.input.clear()
            return
        box = page_box(view)
        if box is not None:
            box.clear()

    def find_here(self) -> None:
        """Ctrl+F. A preview's find; else the page's own box; else Search."""
        from app.ui.widgets.find_bar import focused_find

        w = self._w
        bar = focused_find(QApplication.focusWidget())
        if bar is not None:
            bar.focus()
            return
        view = w._current_view()
        box = page_box(view)
        if view is not w.search_view and box is not None and box.isVisible():
            focus = getattr(view, "focus", None)
            if callable(focus):
                focus()
            else:
                box.setFocus(Qt.FocusReason.ShortcutFocusReason)
                box.selectAll()
            return
        w._focus_search()

    # -- the Go menu ---------------------------------------------------------------

    def fill_go_menu_now(self) -> None:
        """Go's `aboutToShow`, on the menu kept when it was built (`go_menu`).

        Never found again through `QAction.menu()`: in this PySide6 that returns a
        wrapper whose C++ menu reads as deleted, and taking it spoils the good
        wrapper of the same menu too (measured offscreen, 2026-10-14)."""
        if self.go_menu is not None:
            self.fill_go_menu(self.go_menu)

    def fill_go_menu(self, menu: Any) -> None:
        """Go, as it opens: every rail page in the rail's order, then the rest."""
        order = self._w.rail.page_order()
        menu.clear()                    # the actions are the window's: removed, not deleted
        titles = [t for t in order if t in self.go_pages]
        titles += [t for t in self.go_pages if t not in titles]
        for title in titles:
            menu.addAction(self.go_pages[title])
        if self.go_extras:
            menu.addSeparator()
            for action in self.go_extras:
                menu.addAction(action)

    # -- the palette ---------------------------------------------------------------

    def palette_entries(self) -> list:
        """Every page, setting and action the palette offers, freshly read."""
        w, rail = self._w, self._w.rail
        icons = getattr(rail, "_icons", {})
        pages = []
        titles = list(rail.page_order())
        for index in range(rail.count()):
            title = rail.tabText(index)
            if title and title not in titles:
                titles.append(title)
        for title in titles:
            action = self.go_pages.get(title)
            index = self.index_of(title)
            pages.append(palette_words.page_entry(
                title, lambda t=title: self.go_to(t),
                icon=getattr(action, "icon_name", "") or icons.get(index, ""),
                keys=action.shortcut().toString() if action is not None else "",
                tip=action.statusTip() if action is not None else ""))
        settings = []
        view = getattr(w, "settings_view", None)
        nav = getattr(view, "_nav", None)
        if nav is not None:
            for name in nav.category_names():
                settings.append(palette_words.setting_entry(
                    name, lambda n=name: self._show_setting(n),
                    icon=getattr(nav, "ICONS", {}).get(name, "settings")))
        verbs = []
        page_actions = set(map(id, self.go_pages.values()))
        for action in getattr(w, "_menu_actions", ()):
            if id(action) in page_actions or getattr(action, "palette_skip", False):
                continue
            verbs.append(palette_words.action_entry(
                action.text(), action.trigger, icon=getattr(action, "icon_name", ""),
                keys=action.shortcut().toString(), tip=action.statusTip()))
        contacts = getattr(w, "contacts_view", None)
        if contacts is not None and hasattr(contacts, "ensure_built"):
            verbs.append(palette_words.action_entry(
                "Sync now", self.open_sync, icon="refresh-cw",
                tip="Sync the accounts that keep contacts now",
                words=("accounts", "contacts", "mail", "fetch")))
        # The Sync window without starting a sync - Settings > Accounts' "Open sync status".
        runtime = getattr(w, "account_runtime", None)
        if callable(getattr(runtime, "open_sync", None)):
            verbs.append(palette_words.action_entry(
                "Open sync status", lambda: runtime.open_sync(), icon="gauge",
                tip="Open the Sync window: what every account's contacts and mail are doing now, "
                    "with Stop, Allow and Retry",
                words=("accounts", "contacts", "mail", "sync", "progress")))
        return palette_words.build(pages, settings, verbs)

    def _show_setting(self, name: str) -> None:
        view = getattr(self._w, "settings_view", None)
        if view is None:
            return
        self._w._show(view)
        nav = getattr(view, "_nav", None)
        if nav is not None:
            nav.show_category(name)

    def open_sync(self) -> None:
        """The Sync window - the same door as the Contacts toolbar's Sync now."""
        contacts = getattr(self._w, "contacts_view", None)
        if contacts is None:
            return
        contacts.ensure_built().open_tool("sync")

    def search_for(self, query: str) -> None:
        """The palette's "Search for" line: the words, run on the Search page."""
        w = self._w
        w._show(w.search_view)
        w.search_view.input.setText(str(query or ""))
        w.search_view.search_now()

    def open_palette(self, text: str = "") -> Any:
        """Ctrl+K: the palette, over the window. Built on first use."""
        if self.palette is None:
            from app.ui.widgets.command_palette import CommandPalette

            self.palette = CommandPalette(self.palette_entries, search=self.search_for)
        if self.palette.isVisible():
            self.palette.close()
            return self.palette
        self.palette.open_over(self._w, text)
        return self.palette
