r"""The one right-click menu builder: every list's menu, and its rules.

Layer: L5

**Why this exists** (UI phase B, the owner: every screen should feel like one
app). Each list built its own `QMenu` by hand, so each had its own idea of a
menu: some had icons and most did not, the action a double-click runs sat
anywhere in the list, nothing showed which key did the same thing, and two
ways of placing the popup were in use - one of them wrong (see
`global_point`). The same argument `buttons.py` made for buttons.

The rules, in plain English
---------------------------

1. **Every line carries a 16 px Lucide icon**, from `ICON_NAMES`. Where a
   button does the same thing, the line uses the button's icon (`BUTTONS`), so
   "Show in folder" is one picture wherever it is met. `MENU_ICONS` below is
   the table for words no button has; a line with no icon anywhere is refused
   when the menu is built, not found at review.
2. **What a double-click or Enter does is the first line, in bold**
   (`QMenu.setDefaultAction`), so the menu teaches the shortcut.
3. **A line shows its key** where one exists - Enter on the default, Ctrl+C on
   Copy path, Delete on the destructive line, F2 on rename. The keys are shown,
   not bound here: the list itself answers them (`list_keys.py`), and an open
   menu keeps Enter and Space for choosing its own highlighted line
   (`_KeysStayInTheMenu`).
4. **Lines are grouped**, with a separator between groups, always in this
   order: open or view; act on it; copy; remember; and anything destructive
   last, its icon drawn in the theme's `danger` colour. Qt cannot colour one
   line's words in a menu's stylesheet (`QMenu::item` has no per-action
   selector), so the red is the icon's and the place is last.
5. **Words are never changed here.** A line is given its words by the view
   that owns it; this file only orders, groups, decorates and places them.
"""

from __future__ import annotations

import weakref
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Sequence

from PySide6.QtCore import QEvent, QObject, QPoint, Qt
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import QAbstractScrollArea, QMenu, QWidget

from app.ui.qtsip import open_menu

__all__ = [
    "GROUPS", "MENU_ICONS", "MENU_PREFIXES", "Item", "build", "glyph_for",
    "global_point", "show", "weak_call", "BUILT_PROPERTY",
]

#: The groups, in the order they appear. See rule 4.
GROUPS = ("open", "act", "copy", "remember", "danger")

#: Set on every menu this builds, so the guard test can tell one of ours from a
#: hand-built `QMenu` (`tests/unit/test_menu_system.py`).
BUILT_PROPERTY = "menuBuilder"

#: **Menu words no button has -> their icon.** A word that is also a button's
#: (`buttons.BUTTONS`) is not repeated here: the button's icon is used, which
#: is rule 1, and the guard test fails if this table ever disagrees with it.
MENU_ICONS: dict[str, str] = {
    # The file menu (`file_menu.py`) - every list that shows a file.
    "View": "eye",
    "Search inside this file": "search",
    "Search inside this message": "search",
    "Search this repository": "search",
    "Index this file now": "play",
    "Pin": "pin",
    "More like this": "sparkles",
    "Why is this here?": "circle-help",
    "Why this result?": "circle-help",
    "See everything from this month": "calendar",
    "Find copies of this…": "copy-plus",
    "Copy path": "copy",
    "Copy file name": "copy",
    "Copy subject": "copy",
    "Copy sender": "copy",
    "Copy name": "copy",
    "Copy repository name": "copy",
    "Copy path and line": "copy",
    "Ignore this repository": "ban",
    "File is missing - re-index this folder": "refresh-cw",
    # Photos (`photo_page_parts.photo_menu`).
    "Name the people in it…": "users",
    "Write names into it…": "save",
    "Photos from the same day": "calendar",
    # Tidy.
    "Keep this one": "circle-check",
    "Compare this set": "split",
    # Offline Media.
    "Copy hardware ID": "copy",
    # File types.
    "Copy the fix command": "copy",
    "Built in - cannot be removed here": "ban",
    # Name people (`photo_tagger_page.py`).
    "Name this person…": "pencil",
    "Combine with another person…": "merge",
    "Manage the faces in this pile…": "users",
    "Forget this person…": "trash-2",
    "Look through these faces…": "users",
    # The rail's page buttons (`rail_order.py`).
    "Move up": "chevron-up",
    "Move down": "chevron-down",
    "Reset page order": "rotate-ccw",
    # The timeline strip over Search.
    "See everything from this period": "calendar",
    # A chat message (`chat_message_menu.py`).
    "Copy selection": "copy",
    "Copy as plain text": "file-text",
    "Regenerate": "refresh-cw",
    "Try again": "rotate-cw",
    "Edit": "pencil",
}

#: Words that carry a name or a number ("Remove .xyz", "Show all 4 separately")
#: are matched by how they start, as `buttons.PREFIXES` does for buttons.
MENU_PREFIXES: dict[str, str] = {
    "Edit .": "pencil",
    "Remove .": "trash-2",
    "Show all ": "list",
    "Reset ": "rotate-ccw",
}


@dataclass
class Item:
    """One line of a menu. Only `words` and `run` are needed; the rest has a default.

    `run` is called with no arguments. `sub` makes the line a submenu instead
    (and `run` is ignored). `tint` is a colour for this line's icon alone - a
    contact tag's own colour; otherwise the theme decides.
    """

    words: str
    run: Optional[Callable[[], Any]] = None
    group: str = "act"
    icon: str = ""
    shortcut: str = ""
    default: bool = False
    enabled: bool = True
    tip: str = ""
    checked: Optional[bool] = None
    tint: str = ""
    sub: Sequence["Item"] = field(default_factory=tuple)


class _KeysStayInTheMenu(QObject):
    """On every menu built here: a shown key is a label, never a binding.

    While a menu is open it has the keyboard, and a line's shortcut would fire
    from there - a Space or an Enter meant to choose the highlighted line would
    run "Preview" or "Open" instead. Accepting every ShortcutOverride hands each
    key to the menu's own handling (arrows, Enter, Space, Esc, letters).

    A filter rather than an `event()` override on a `QMenu` subclass: a menu is
    deleted by Qt inside its own `event()` (its deferred deletion), and a Python
    override still on the stack then returns into a menu that is gone - a crash
    seen in tests/unit/test_side_lists.py while this was being built.
    """

    def eventFilter(self, watched: Any, event: Any) -> bool:      # noqa: N802 - Qt's naming
        if event.type() == QEvent.Type.ShortcutOverride:
            event.accept()
            return True
        return False


def _new_menu(*args: Any) -> QMenu:
    """A `QMenu` with `_KeysStayInTheMenu` on it."""
    menu = QMenu(*args)
    menu._keys = _KeysStayInTheMenu(menu)
    menu.installEventFilter(menu._keys)
    return menu


def glyph_for(words: str, icon: str = "") -> str:
    """The icon a line gets: its own, this table's, a prefix's, or its button's. "" if none."""
    if icon:
        return icon
    if words in MENU_ICONS:
        return MENU_ICONS[words]
    for start, glyph in MENU_PREFIXES.items():
        if words.startswith(start):
            return glyph
    from app.ui.widgets.buttons import lookup

    found = lookup(words)
    return found[0] if found is not None else ""


def _colours() -> dict[str, str]:
    from app.ui.theme import theme_colours      # noqa: PLC0415 - avoids a cycle

    return theme_colours()


def _icon_for(item: Item, colours: dict[str, str]) -> Any:
    from app.ui.widgets.icons import ICON_NAMES, icon

    glyph = glyph_for(item.words, item.icon)
    if not glyph or glyph not in ICON_NAMES:
        # Rule 1 is enforced where a menu is made, so a line without a picture
        # cannot reach the screen - the same bargain `buttons.icon_button` makes.
        raise ValueError(f"menu line {item.words!r} has no icon in ICON_NAMES ({glyph!r})")
    if item.tint:
        colour = item.tint
    elif item.group == "danger":
        colour = colours.get("danger", "#a32020")
    else:
        colour = colours.get("text_dim", "#888888")
    return icon(glyph, colour)


def _ordered(items: Sequence[Item]) -> list[list[Item]]:
    """The lines in their groups, in `GROUPS` order, the default line first of all."""
    unknown = [item.group for item in items if item.group not in GROUPS]
    if unknown:
        raise ValueError(f"unknown menu group {unknown[0]!r}; one of {GROUPS}")
    groups = [[item for item in items if item.group == name] for name in GROUPS]
    for lines in groups:
        for item in lines:
            if item.default:
                # Rule 2: first and bold. Its own group goes first with it, so the
                # separator still falls between groups and never inside one.
                lines.remove(item)
                lines.insert(0, item)
                groups.remove(lines)
                groups.insert(0, lines)
                return [lines for lines in groups if lines]
    return [lines for lines in groups if lines]


def _runs(items: Sequence[Item]) -> list[list[Item]]:
    """The lines in the order given, a new block wherever the group changes."""
    blocks: list[list[Item]] = []
    for item in items:
        if blocks and blocks[-1][0].group == item.group:
            blocks[-1].append(item)
        else:
            blocks.append([item])
    return blocks


def _add(menu: QMenu, item: Item, colours: dict[str, str]) -> QAction:
    picture = _icon_for(item, colours)
    if item.sub:
        submenu = _new_menu(item.words, menu)
        submenu.setIcon(picture)
        submenu.setProperty(BUILT_PROPERTY, True)
        _fill(submenu, item.sub, colours, grouped=False)
        action = menu.addMenu(submenu)
    else:
        action = QAction(picture, item.words, menu)
        run = item.run
        if run is not None:
            action.triggered.connect(lambda _checked=False, go=run: go())
        menu.addAction(action)
    action.setEnabled(bool(item.enabled))
    if item.tip:
        action.setToolTip(item.tip)
    if item.checked is not None:
        action.setCheckable(True)
        action.setChecked(bool(item.checked))
    if item.shortcut:
        action.setShortcut(QKeySequence(item.shortcut))
        action.setShortcutVisibleInContextMenu(True)
    return action


def _fill(menu: QMenu, items: Sequence[Item], colours: dict[str, str], *, grouped: bool) -> None:
    menu.setToolTipsVisible(True)
    blocks = _ordered(items) if grouped else _runs(items)
    for number, lines in enumerate(blocks):
        if number:
            menu.addSeparator()
        for item in lines:
            action = _add(menu, item, colours)
            if item.default and menu.defaultAction() is None:
                menu.setDefaultAction(action)
                # The style draws the default line bold; the sheet's own font
                # can win over that, so the line's font says it too.
                font = action.font()
                font.setBold(True)
                action.setFont(font)


def build(parent: QWidget, items: Sequence[Item], *, grouped: bool = True) -> QMenu:
    """A menu of `items`, by the rules in the module docstring.

    `grouped=False` keeps the lines exactly in the order given, with a separator
    wherever the group changes - for a menu whose page decides its own order: the
    Contacts sidebar's lines (`side_list.py`), and a chat message's, where Copy
    comes before Regenerate as it does under the message.
    """
    # A menu is built per click and its lines close over the list's rows, so the
    # one this list showed last goes when the next is made - a long session keeps
    # one menu a list, not every menu it ever showed. Not on close
    # (`WA_DeleteOnClose`): a menu deleted as it closes crashed when its list was
    # torn down before the deletion ran (seen in tests/unit/test_side_lists.py).
    for old in parent.findChildren(QMenu, options=Qt.FindChildOption.FindDirectChildrenOnly):
        if old.property(BUILT_PROPERTY) and not old.isVisible():
            old.deleteLater()
    menu = _new_menu(parent)
    menu.setProperty(BUILT_PROPERTY, True)
    _fill(menu, list(items), _colours(), grouped=grouped)
    return menu


def global_point(widget: Any, point: QPoint) -> QPoint:
    r"""Where a `customContextMenuRequested` point is on the screen. **The one way.**

    Qt's own documentation of the signal: the point "is the position of the
    context menu event that the widget receives. Normally this is in widget
    coordinates. The exception to this rule is QAbstractScrollArea and its
    subclasses that map the context menu event to coordinates of the
    viewport()". Checked on PySide6 6.11 (2026-10-13): a right-click sent to a
    table's viewport at (20, 30) arrives as (20, 30), with the viewport 26 px
    below the table's top edge behind the header.

    So for every list, table, tree and grid the point is already in viewport
    coordinates: it goes to `indexAt`/`itemAt`/`rowAt` as it is, and onto the
    screen through `viewport().mapToGlobal`. Mapping it through the widget, as
    two conventions did, put the popup a header's height above the pointer and
    made the row lookup one row high. For any other widget - a rail button, a
    chip - the point is in the widget's own coordinates.
    """
    if isinstance(widget, QAbstractScrollArea):
        viewport = widget.viewport()
        if viewport is not None:
            return viewport.mapToGlobal(point)
    return widget.mapToGlobal(point)


def show(menu: QMenu, widget: Any, point: QPoint, *, wait: bool = True,
         keep: Any = None) -> Any:
    """Put `menu` up at `point` (a `customContextMenuRequested` point on `widget`).

    `wait=True` blocks until a line is chosen and returns it (`open_menu`, which
    a test can stand in for); `wait=False` pops it up and returns at once, for
    the lists whose tests read the menu while it is open. `keep` is the object
    that holds the open menu as `last_menu`, for those tests; the menu is its
    child, so the two go together.
    """
    where = global_point(widget, point)
    if keep is not None:
        keep.last_menu = menu
    if wait:
        return open_menu(menu, where)
    menu.popup(where)
    return None


def weak_call(function: Callable[..., Any]) -> Callable[..., Any]:
    """`function` as a callback that does not keep a bound method's object alive.

    A menu line or a key handler held by a child of a view must not pin the
    view (`view_options.weak_slot`, `tests/unit/test_views_are_freed.py`). A
    bound method is held through `WeakMethod`; anything else is held as given,
    and must not close over the view itself.
    """
    if hasattr(function, "__self__") and hasattr(function, "__func__"):
        ref = weakref.WeakMethod(function)

        def call(*args: Any) -> Any:
            method = ref()
            return method(*args) if method is not None else None

        return call
    return function
