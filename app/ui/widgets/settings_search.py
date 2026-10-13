r"""Search all settings: find a setting by its words, and be taken to it (UI phase B, 2026-10-13).

Layer: L6 (UI)

**The filter already found things; it did not take you anywhere.** Typing "memory" showed every
matching control in every category at once, which answers "is there such a setting" - and then
left somebody to clear the box and hunt for the same control again in its own category, where it
sits among its neighbours and its box's explanation. The owner's word for Settings was that it
felt like several applications; a search that finds a thing and cannot show you where it lives
is part of that.

So the same box now lists what it found - each setting's own label and the category it is in -
and Enter, a click, or Down then Enter goes there: the box clears, the category opens, an
"Advanced" fold around it opens, the page scrolls to it, it takes the focus, and the row is lit
for a moment so the eye lands on it.

**It searches what the filter searches**: every registered setting's label and tooltip words
(`settings_registry`), found on the page by the object name each control already carries. No
widget file needs to know search exists, and a setting added next month is findable without
anybody remembering to make it so.

**No words live here** - the list's lines are the registry's labels and the category names the
sidebar already shows. The one new sentence is in `settings_view.py`, with the rest of the page's.
"""

from __future__ import annotations

from typing import Any, Optional

from PySide6.QtCore import QEvent, QObject, Qt, QTimer
from PySide6.QtWidgets import QListWidget, QListWidgetItem, QWidget

from app.core import settings_registry as reg
from app.ui.later import later

__all__ = ["FOUND_PROPERTY", "FOUND_MS", "attach", "fill_found", "found_settings", "go_to_first",
           "go_to_item", "go_to_setting", "KeysToList"]

#: The dynamic property the theme lights a found row by (`theme.py`).
FOUND_PROPERTY = "settingsFound"

#: How long the row stays lit. Long enough to find with the eye, short enough not to look stuck.
FOUND_MS = 2500

#: The most lines the list shows before it scrolls - a long list under a search box pushes the
#: page it is meant to lead to off the screen.
_MOST_LINES = 8


def attach(view: Any, layout: Any, *, name: str, found: str, goto: str) -> None:
    """Build the list and its hint under `view.filter_box` in `layout`, and wire the keys.

    The words are `settings_view.py`'s, passed in, so they stay with the rest of the page's.
    """
    from PySide6.QtWidgets import QLabel

    view.filter_box.setAccessibleName(name)
    view.filter_found = QListWidget()
    view.filter_found.setObjectName("settingsFound")
    view.filter_found.setAccessibleName(found)
    view.filter_found.setVisible(False)
    view.filter_goto = QLabel(goto)
    view.filter_goto.setObjectName("settingsHint")
    view.filter_goto.setWordWrap(True)
    view.filter_goto.setVisible(False)
    layout.addWidget(view.filter_goto)
    layout.addWidget(view.filter_found)
    view.filter_box.returnPressed.connect(lambda: go_to_first(view))
    view.filter_found.itemClicked.connect(lambda item: go_to_item(view, item))
    view._keys_to_list = KeysToList(view)


def found_settings(view: Any, needle: str) -> list[tuple[str, str, str]]:
    """`(key, label, category)` for every setting on the page whose words hold `needle`,
    in registry order. Empty for an empty needle."""
    needle = (needle or "").strip().lower()
    if not needle:
        return []
    found = []
    for setting in reg.SETTINGS:
        if needle not in f"{setting.label} {setting.help}".lower():
            continue
        widget = view.findChild(QWidget, setting.key)
        if widget is None:
            continue
        category = view._category_of(widget)
        if category is not None:
            found.append((setting.key, setting.label, category))
    return found


def fill_found(view: Any, needle: str) -> int:
    """Fill `view.filter_found` with what `needle` finds; show it, and its hint, only when it
    found something. Returns how many."""
    listing: QListWidget = view.filter_found
    hits = found_settings(view, needle)
    listing.clear()
    for key, label, category in hits:
        item = QListWidgetItem(f"{label}  ·  {category}")
        item.setData(Qt.ItemDataRole.UserRole, key)
        item.setToolTip(getattr(reg.by_key(key), "help", "") or label)
        listing.addItem(item)
    if hits:
        listing.setCurrentRow(0)
        rows = min(len(hits), _MOST_LINES)
        listing.setFixedHeight(rows * max(listing.sizeHintForRow(0), 18) + 2 * listing.frameWidth() + 4)
    listing.setVisible(bool(hits))
    view.filter_goto.setVisible(bool(hits))
    return len(hits)


def go_to_setting(view: Any, key: str) -> bool:
    """Take the page to the control for `key`: clear the search, open its category and any
    "Advanced" fold around it, scroll it into view, focus it and light its row. False when the
    page has no such control."""
    widget = view.findChild(QWidget, key)
    if widget is None:
        return False
    category = view._category_of(widget)
    if category is None:
        return False
    view.filter_box.clear()                 # the filter's own clear puts every control back
    for fold in getattr(view, "_folds", ()):
        if fold.body.isAncestorOf(widget) and not fold.is_open:
            fold.set_open(True)             # shown, not remembered: it was not a click
    view._nav.show_category(category)
    label = view._form_label_for(widget)
    _light(widget, label)
    widget.setFocus(Qt.FocusReason.OtherFocusReason)
    _scroll_to(view, widget)
    # Once more after the layout has settled: a page just shown has no geometry yet.
    later(widget, 0, lambda: _scroll_to(view, widget))   # not if the page has gone
    return True


def go_to_item(view: Any, item: Optional[QListWidgetItem]) -> None:
    """A line of the list chosen: go to its setting **on the next turn of the event loop**.

    Going there clears the search, which empties this very list - and emptying a list from
    inside its own click or key handler deletes the item Qt is still delivering to.
    """
    if item is None:
        return
    key = str(item.data(Qt.ItemDataRole.UserRole))
    # Not if Settings has gone, or was left in the moment between (the view outlives a page switch).
    later(view, 0, lambda: go_to_setting(view, key) if view.isVisible() else None)


def go_to_first(view: Any) -> bool:
    """Enter in the search box: go to the first thing it found. False when it found nothing."""
    if view.filter_found.count() == 0:
        return False
    return go_to_setting(view, str(view.filter_found.item(0).data(Qt.ItemDataRole.UserRole)))


def _scroll_to(view: Any, widget: QWidget) -> None:
    scroll = getattr(view._nav, "scroll", None)
    try:
        if scroll is not None and widget.isVisible():
            scroll.ensureWidgetVisible(widget, 0, 48)
    except RuntimeError:                    # the page was torn down in between
        return


def _light(widget: QWidget, label: Optional[QWidget]) -> None:
    """Light the row for `FOUND_MS`, through the theme's `[settingsFound="true"]` rule."""
    parts = [part for part in (widget, label) if part is not None]
    for part in parts:
        _set_found(part, True)

    def dim() -> None:
        for part in parts:
            try:
                _set_found(part, False)
            except RuntimeError:            # gone already
                pass

    QTimer.singleShot(FOUND_MS, dim)


def _set_found(part: QWidget, on: bool) -> None:
    part.setProperty(FOUND_PROPERTY, bool(on))
    style = part.style()
    style.unpolish(part)
    style.polish(part)
    part.update()


class KeysToList(QObject):
    """Down in the search box moves into the list of what it found, so the keyboard reaches
    every hit, not only the first; Enter there goes to it and Escape goes back to the box."""

    def __init__(self, view: Any) -> None:
        super().__init__(view)
        self._view = view
        view.filter_box.installEventFilter(self)
        view.filter_found.installEventFilter(self)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802 - Qt's name
        if event.type() != QEvent.Type.KeyPress:
            return False
        view, key = self._view, event.key()
        if watched is view.filter_box and key == Qt.Key.Key_Down and view.filter_found.isVisible():
            view.filter_found.setFocus()
            return True
        if watched is view.filter_found:
            if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                go_to_item(view, view.filter_found.currentItem())
                return True
            if key == Qt.Key.Key_Escape:
                view.filter_box.setFocus()
                return True
        return False
