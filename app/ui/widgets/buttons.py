r"""The one button system: every action button in the window, and its rules.

Layer: L5

**Why this exists** (owner, 2026-09-27): *"this buttons across the whole
screen look odd and out of place all buttons should be consistent throughout
the app and should have icons."* The screenshot was Settings › Storage &
maintenance: "Run doctor" and three more stretched across the whole page,
"Clear logs" beside them at its natural width, and not one icon between them.
Each page had built its buttons by hand, so each page had its own idea of
what a button is.

The rules, in plain English
---------------------------

1. **A button is as wide as its words and its icon, never wider.** It is
   never stretched across a page or a column. (In Qt terms: the horizontal
   size policy is `Fixed`, so a layout places it at its natural width, at the
   left of the space it was given.)
2. **Buttons that belong together sit in one row** (`button_row`), the
   standard `ROW_SPACING` apart, starting at the left. A dialog's own
   OK / Cancel row is the one exception: it stays where the operating
   system's convention puts it (the right), because that is where people look.
3. **One height and one padding for all of them.** Set in `theme.py`
   (`BUTTON`), not here, so the look lives with the rest of the look. The
   height follows the system font, so "Make text bigger" still grows every
   button the same amount.
4. **Every one carries a 16 px Lucide icon** in front of its words. The icon
   for each button is in `BUTTONS` below - one table, so the whole app's
   choices can be read (and changed) in one place.
5. **Three kinds, and only three:**

   * ``primary`` - the page's main action, filled with the accent colour.
     At most one per page or dialog ("Start indexing", "Send", "OK").
   * ``danger`` - it deletes or clears something ("Reset index…",
     "Clear logs", "Delete"). Red words and a red icon, so it is noticed
     before it is pressed; the confirmation it asks for is unchanged.
   * ``secondary`` - everything else. The quiet default.

6. **Colours come from theme tokens only**, in both themes: the primary
   icon takes `accent_on` (the ink for text on the accent), the danger one
   `danger`, the rest `text_dim`. When the theme changes, `retint_all`
   redraws every icon.

What is *not* in the system, on purpose: the rail, the Search toolbar's
icon toggles, filter chips, segmented controls, the chat's small flat
message actions and the operating system's own question boxes. Each of
those was designed separately (order 202626160950) and is a different
kind of control, not an action button. A flat `QPushButton`, or one marked
``setProperty("buttonSystem", "exempt")``, is left alone.

**Labels are never changed here.** A button is looked up by the words it
already has; nothing in this file rewrites one.

*Note, 2 October 2026:* rule 4 has one case where the words are not drawn. A
button on every line of a list - "Index now" on a folder's line, Rescan on a
drive's - shows its icon alone (`icon_button`): the words are in the column's
heading, the tooltip and the screen reader's name, and its icon and kind still
come from `BUTTONS`. `put_on_row` places it so it is neither stretched nor cut.
"""

from __future__ import annotations

from typing import Iterable, Optional

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QApplication, QDialogButtonBox, QHBoxLayout, QPushButton, QSizePolicy, QWidget,
)

from app.ui.widgets.icons import ICON_PX as ICON_RENDER_PX
from app.ui.widgets.icons import icon as themed_icon

__all__ = [
    "BUTTONS", "ROLES", "ICON_PX", "ROW_SPACING", "action_button", "style_button",
    "style_all", "retint_all", "button_row", "is_exempt", "clean_label", "lookup",
    "refresh_icon", "put_on_row", "icon_button",
]

#: The three kinds of button. See rule 5 in the module docstring.
ROLES = ("primary", "secondary", "danger")

#: Every action button's icon is drawn this size, in pixels.
ICON_PX = 16

#: The gap between two buttons in a row, in pixels. The same gap Qt's own
#: dialog button boxes leave, so a page row and a dialog row look alike.
ROW_SPACING = 8

#: **Every action button in the app: its words -> (icon, kind).**
#:
#: Keyed by the button's label exactly as it is shown (any `&` shortcut
#: marker removed), so this table is also the list of every action button
#: there is. A new button with no entry here fails
#: `tests/unit/test_button_system.py`, which is the point: it gets an icon
#: and a kind on the day it is written, not at the next review.
BUTTONS: dict[str, tuple[str, str]] = {
    # -- Indexing ------------------------------------------------------------
    "Start indexing": ("play", "primary"),
    "Pause": ("pause", "secondary"),
    "Resume": ("play", "secondary"),
    "Stop": ("square", "secondary"),
    "Scan first": ("scan-search", "secondary"),
    "Reset index…": ("trash-2", "danger"),
    "Retry these": ("rotate-cw", "secondary"),
    "Return to automatic": ("rotate-ccw", "secondary"),
    "Re-detect": ("refresh-cw", "secondary"),
    "Benchmark now": ("gauge", "secondary"),
    "Browse…": ("folder-open", "secondary"),
    # -- Settings › What's indexed ---------------------------------------------
    "Add folder…": ("folder-plus", "secondary"),
    # 2026-10-03: one file as an entry in the list, beside the folders.
    "Add file…": ("file-plus", "secondary"),
    "Remove": ("folder-minus", "secondary"),
    "Rescan archived folders now": ("refresh-cw", "secondary"),
    # 2026-09-29: marks the selected folder to be read before the rest.
    "Index this folder first": ("pin", "secondary"),
    # 2026-10-02: on each line of the folder list - that folder, now.
    "Index now": ("play", "secondary"),
    "Rescan these folders now": ("refresh-cw", "secondary"),
    "Add all four": ("folder-plus", "secondary"),
    "Select all": ("list-checks", "secondary"),
    "Select none": ("list-x", "secondary"),
    "Add file type...": ("plus", "secondary"),
    "Reset to defaults": ("rotate-ccw", "secondary"),
    "Save file types": ("save", "primary"),
    "Convert a .pst to .eml files…": ("arrow-right-left", "secondary"),
    # 2026-10-07: on each line of Mail archives - that archive, read again;
    # or its messages taken out of the index first (it asks).
    "Read again": ("refresh-cw", "secondary"),
    "Clear and read again": ("eraser", "danger"),
    # -- Settings › Models & AI › AI programs (2026-10-04) ----------------------
    "Start": ("play", "secondary"),
    "Connect": ("plus", "secondary"),
    "Disconnect": ("x", "secondary"),
    "Copy address and key": ("copy", "secondary"),
    "Copy bridge command": ("copy", "secondary"),
    # -- Settings › Search -----------------------------------------------------
    "Clear search history": ("eraser", "danger"),
    "Reset search behaviour to defaults": ("rotate-ccw", "secondary"),
    # -- Settings › Models & AI ------------------------------------------------
    "Refresh list": ("refresh-cw", "secondary"),
    "Test": ("flask-conical", "secondary"),
    "Name the people in your photos…": ("users", "secondary"),
    # Beside every model drop-down (widgets/model_download.py, 2026-09-29).
    "Download": ("file-down", "secondary"),
    # 2026-10-08: Models Leasha uses (widgets/needed_models_box.py) - every
    # missing model, one after another.
    "Download all": ("file-down", "secondary"),
    # The Models box (widgets/model_manager.py, order 1c, 2026-09-30). They were
    # added without entries here, which test_button_system caught on 2026-09-30.
    # "Remove copies nothing uses" shows its size, so it is matched in PREFIXES.
    "Use recommended models": ("rotate-ccw", "secondary"),
    "Use this": ("check", "secondary"),
    "Update the list from Hugging Face": ("refresh-cw", "secondary"),
    # -- Settings › Storage & maintenance ------------------------------------
    "Move or change index location…": ("move", "secondary"),
    "Change the meaning model…": ("brain", "secondary"),
    "Open in its own window": ("external-link", "secondary"),
    "Copy": ("copy", "secondary"),
    "Run doctor": ("stethoscope", "secondary"),
    "Check that search works": ("circle-check", "secondary"),
    "Save a support bundle…": ("package", "secondary"),
    "Open the recordings folder": ("folder-open", "secondary"),
    "Clear logs": ("eraser", "danger"),
    "Open the logs folder": ("folder-open", "secondary"),
    # Every Settings page's own "put this page back" button (defaults.py).
    "Restore defaults": ("rotate-ccw", "secondary"),
    # -- Offline Media -------------------------------------------------------
    "Scan a drive…": ("hard-drive", "primary"),
    "Rescan": ("refresh-cw", "secondary"),
    "Delete": ("trash-2", "danger"),
    # -- Reports ---------------------------------------------------------------
    "Export…": ("file-down", "primary"),
    "Show": ("eye", "secondary"),
    # -- Code ------------------------------------------------------------------
    "Search history": ("history", "secondary"),
    "Git view": ("git-branch", "secondary"),
    # -- Chat ------------------------------------------------------------------
    "New chat": ("plus", "secondary"),
    "Rename": ("pencil", "secondary"),
    "Rename…": ("pencil", "secondary"),
    "Send": ("send", "primary"),
    "Web": ("globe", "secondary"),
    "Check again": ("refresh-cw", "secondary"),
    "Look again": ("refresh-cw", "secondary"),
    "Allow": ("check", "secondary"),
    "Skip": ("x", "secondary"),
    # -- The preview pane and the pinned window --------------------------------
    "Open": ("external-link", "primary"),
    # Order 0y section 4d: the original of a previewed message.
    "Open in Outlook": ("mail", "secondary"),
    "Show in folder": ("folder-open", "secondary"),
    "Pin in a window": ("bookmark", "secondary"),
    "Rotate": ("rotate-cw", "secondary"),
    "Zoom in": ("zoom-in", "secondary"),
    "Zoom out": ("zoom-out", "secondary"),
    "Fit width": ("move-horizontal", "secondary"),
    "Print": ("printer", "secondary"),
    "Open the real file": ("external-link", "primary"),
    "Show full layout": ("file-text", "secondary"),
    "Third-party notices": ("file-text", "secondary"),   # Help > About, 2026-10-04
    "Show simplified view": ("eye", "secondary"),
    "Describe": ("sparkles", "secondary"),
    "Open them all": ("external-link", "secondary"),
    "Copy the paths": ("copy", "secondary"),
    "Clear the list": ("eraser", "secondary"),
    # 2026-10-08: the quick search box's top bar (Ctrl+Shift+Space), icons only.
    "Preview": ("panel-right", "secondary"),
    "Show all results": ("external-link", "secondary"),
    # -- Find, notices, saved searches -----------------------------------------
    "Previous": ("chevron-up", "secondary"),
    "Next": ("chevron-down", "secondary"),
    "Close": ("x", "secondary"),
    "Dismiss": ("x", "secondary"),
    "Search": ("search", "primary"),
    # -- Photos ------------------------------------------------------------------
    "Yes": ("check", "primary"),
    "No": ("x", "secondary"),
    "Set roughly when a folder of scans is from…": ("calendar", "secondary"),
    "Remove selected from this pile": ("folder-minus", "secondary"),
    "Move selected to a new pile": ("split", "secondary"),
    # 2026-10-05: the Photos tab, and Accept all on its naming page.
    "Accept all": ("list-checks", "primary"),
    # 2026-10-04: each model's processor, measured (`device_box.py`).
    "Test this machine": ("flask-conical", "secondary"),
    "Back to photos": ("image", "secondary"),
    "Name people": ("users", "secondary"),
    "Write names into photos…": ("save", "secondary"),
    # -- Dialog button boxes -------------------------------------------------------
    "OK": ("check", "primary"),
    "Cancel": ("x", "secondary"),
    "Write them": ("check", "primary"),
    "Re-embed everything": ("refresh-cw", "primary"),
    "Export": ("file-down", "primary"),
    "Scan": ("hard-drive", "primary"),
    "Forget this source": ("trash-2", "danger"),
    "No — catalogue as new": ("plus", "secondary"),
}

#: A dialog button whose words change with what it is about ("Yes — same as
#: 'Holiday disk'") is matched by how its label starts.
PREFIXES: dict[str, tuple[str, str]] = {
    "Yes — same as": ("check", "primary"),
    # "Remove copies nothing uses (3.4 GB)": it deletes files, so it is danger.
    "Remove copies nothing uses": ("trash-2", "danger"),
    # "Force skip reader 2": one per busy reader, numbered (2026-10-05).
    "Force skip reader": ("skip-forward", "secondary"),
}


def clean_label(text: str) -> str:
    """A button's words without the `&` that marks its keyboard shortcut."""
    return text.replace("&&", "\0").replace("&", "").replace("\0", "&").strip()


def lookup(text: str) -> Optional[tuple[str, str]]:
    """The (icon, kind) the table gives these words, or `None`."""
    label = clean_label(text)
    found = BUTTONS.get(label)
    if found is not None:
        return found
    for start, pair in PREFIXES.items():
        if label.startswith(start):
            return pair
    return None


def is_exempt(button: QPushButton) -> bool:
    """True for the kinds of button this system leaves alone (see the module
    docstring): flat ones, and any marked `buttonSystem="exempt"`."""
    return bool(button.isFlat() or button.property("buttonSystem") == "exempt")


def _colours() -> dict[str, str]:
    from app.ui.theme import theme_colours      # noqa: PLC0415 - avoids a cycle
    return theme_colours()


def _ink(role: str, colours: dict[str, str]) -> str:
    """Which theme token draws the icon for a kind of button."""
    if role == "primary":
        return colours.get("accent_on", "#ffffff")
    if role == "danger":
        return colours.get("danger", "#a32020")
    return colours.get("text_dim", "#888888")


#: Extra room between the icon and the words, in pixels. Qt leaves only about
#: four, which at 16px reads as the icon touching the first letter; there is
#: no stylesheet setting for it, so the icon's picture carries a transparent
#: strip this wide on its right-hand side instead.
ICON_GAP = 3

_spaced_cache: dict[tuple[str, str, str], QIcon] = {}


def _spaced_pixmap(glyph: str, colour: str) -> Optional[QPixmap]:
    plain = themed_icon(glyph, colour)
    if plain.isNull():
        return None
    side = ICON_RENDER_PX
    gap = round(side * ICON_GAP / ICON_PX)
    canvas = QPixmap(side + gap, side)
    canvas.fill(Qt.GlobalColor.transparent)
    painter = QPainter(canvas)
    painter.drawPixmap(0, 0, plain.pixmap(side, side))
    painter.end()
    return canvas


def _spaced_icon(glyph: str, colour: str, faint: str) -> QIcon:
    """The glyph with `ICON_GAP` of empty space after it. Cached.

    **The greyed-out picture is drawn too**, in `faint`. Qt would otherwise
    make one by fading the normal picture - and a primary button's icon is
    white in the light theme, so on a disabled button's pale ground it
    faded to nothing ("Save file types" before anything has changed).
    """
    key = (glyph, colour, faint)
    found = _spaced_cache.get(key)
    if found is not None:
        return found
    result = QIcon()
    normal = _spaced_pixmap(glyph, colour)
    if normal is not None:
        result.addPixmap(normal, QIcon.Mode.Normal)
        greyed = _spaced_pixmap(glyph, faint)
        if greyed is not None:
            result.addPixmap(greyed, QIcon.Mode.Disabled)
    _spaced_cache[key] = result
    return result


def _paint_icon(button: QPushButton, colours: dict[str, str]) -> None:
    glyph = button.property("buttonIcon")
    if glyph:
        colour = _ink(str(button.property("buttonRole")), colours)
        faint = colours.get("text_faint", "#888888")
        # Already drawn in exactly these colours: nothing to do. The window
        # asks for a redraw each time a late page arrives, not only when the
        # theme changes, and setting an icon makes Qt lay the button out again.
        painted = f"{glyph}|{colour}|{faint}"
        if button.property("buttonPainted") == painted:
            return
        button.setIcon(_spaced_icon(str(glyph), colour, faint))
        button.setIconSize(QSize(ICON_PX + ICON_GAP, ICON_PX))
        button.setProperty("buttonPainted", painted)


def style_button(button: QPushButton, glyph: Optional[str] = None,
                 role: Optional[str] = None) -> QPushButton:
    """Give an existing button the system's look. Returns the same button.

    `glyph` and `role` default to what `BUTTONS` says for the button's words;
    a button whose words are not in the table (a folder name, say) must pass
    them. Safe to call twice.
    """
    found = lookup(button.text())
    if glyph is None and found is not None:
        glyph = found[0]
    if role is None:
        role = found[1] if found is not None else "secondary"
    if role not in ROLES:
        raise ValueError(f"unknown button kind {role!r}; one of {ROLES}")
    button.setProperty("buttonRole", role)
    button.setProperty("buttonIcon", glyph or "")
    # Rule 1: natural width, never stretched.
    button.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
    _paint_icon(button, _colours())
    # The stylesheet chooses its rules by `buttonRole`, which Qt only reads
    # when it styles a widget - so a button already on screen is asked to
    # style itself again.
    style = button.style()
    if style is not None:
        style.unpolish(button)
        style.polish(button)
    return button


def refresh_icon(button: QPushButton) -> None:
    """After a button's words change ("Pause" -> "Resume"), show the icon the
    table gives its new words. Words the table does not know keep the icon
    the button already had."""
    found = lookup(button.text())
    if found is not None and button.property("buttonRole"):
        button.setProperty("buttonIcon", found[0])
        _paint_icon(button, _colours())


def action_button(text: str, glyph: Optional[str] = None, role: Optional[str] = None,
                  parent: Optional[QWidget] = None) -> QPushButton:
    """A new `QPushButton` that follows the system. For new code."""
    return style_button(QPushButton(text, parent), glyph, role)


def icon_button(words: str, *, tooltip: str, name: Optional[str] = None,
                parent: Optional[QWidget] = None) -> QPushButton:
    """A system button that shows its icon and no words. 2026-10-02.

    **For a line of a list only** (the owner, of "Index now" on each folder's
    line: "make sure the button has an icon only"). The same button on every
    line of a table is the same word down a whole column; the icon says it
    once per line and the column's heading says it once in words.

    Rule 4 still holds in every way but the visible one: `words` must be in
    `BUTTONS`, which is where the icon and the kind come from, and they are
    what a screen reader is given (`name`, or `words` itself). **`tooltip` is
    not optional**: with no words on it, the tooltip is the only place a
    person reads what the icon does. The sheet centres the icon (`iconOnly`,
    `theme.py`).
    """
    found = lookup(words)
    if found is None:
        raise ValueError(f"{words!r} is not in the button table")
    if not str(tooltip or "").strip():
        raise ValueError(f"an icon-only button needs a tooltip ({words!r})")
    button = QPushButton(parent)
    button.setToolTip(tooltip)
    button.setAccessibleName(name or words)
    button.setProperty("iconOnly", True)
    return style_button(button, *found)


def button_row(*buttons: QWidget, align: str = "left") -> QHBoxLayout:
    """Rule 2: related buttons in one row, `ROW_SPACING` apart.

    `align="left"` (pages) puts the space after them; `"right"` puts it before
    them, for the rare page row that closes something, like a dialog's.
    """
    row = QHBoxLayout()
    row.setContentsMargins(0, 0, 0, 0)
    row.setSpacing(ROW_SPACING)
    if align == "right":
        row.addStretch(1)
    for button in buttons:
        row.addWidget(button)
    if align != "right":
        row.addStretch(1)
    return row


#: What the sheet pads every row of a list by (`QTreeWidget::item`,
#: `padding: 3px 4px` in `theme.py`): a widget set on a row is given the row's
#: rectangle *less* this, so a row has to be this much bigger than its button.
ROW_PAD_Y = 6
ROW_PAD_X = 8


def put_on_row(tree: QWidget, item: object, column: int, button: QPushButton) -> QWidget:
    """Put one system button on one row of a `QTreeWidget`, at its own size.

    2026-10-02, for "Index now" on a folder's line and Rescan on a drive's.
    Two things a plain `setItemWidget(item, column, button)` gets wrong:

    * the button is stretched to the column, which rule 1 forbids - so it
      sits in a cell that leaves it its natural width;
    * the row is sized to the button and the button is then given the row
      *less the row's padding*, so it is drawn two or three pixels short and
      its bottom edge is cut off (measured: a 28px button in a 26px cell).
      The row is told its height here, padding included.

    Returns the cell, which is what `tree.itemWidget(item, column)` gives back.
    """
    cell = QWidget()
    # The theme paints every plain QWidget the window colour; this one sits on
    # a row, and was a grey block round the button (seen 2026-10-04 in the
    # Offline page). Named so the theme can make it transparent.
    cell.setObjectName("rowCell")
    layout = QHBoxLayout(cell)
    layout.setContentsMargins(4, 0, 4, 0)
    layout.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
    layout.addWidget(button)
    button.ensurePolished()
    item.setSizeHint(column, QSize(                       # type: ignore[attr-defined]
        cell.sizeHint().width() + ROW_PAD_X,
        button.sizeHint().height() + ROW_PAD_Y))
    tree.setItemWidget(item, column, cell)                # type: ignore[attr-defined]
    return cell


def _dialog_default(button: QPushButton) -> Optional[tuple[str, str]]:
    """A dialog-box button the table does not name, by what it does."""
    parent = button.parentWidget()
    while parent is not None and not isinstance(parent, QDialogButtonBox):
        parent = parent.parentWidget()
    if parent is None:
        return None
    role = parent.buttonRole(button)
    if role in (QDialogButtonBox.ButtonRole.AcceptRole, QDialogButtonBox.ButtonRole.YesRole):
        return ("check", "primary")
    if role == QDialogButtonBox.ButtonRole.DestructiveRole:
        return ("trash-2", "danger")
    return ("x", "secondary")


def style_all(root: QWidget, *, only_new: bool = False) -> int:
    """Style every action button inside `root`. Returns how many it styled.

    Buttons the table does not name, and that are not in a dialog's button
    box, are left exactly as they were - `test_button_system.py` is what
    finds them. `only_new` skips any button already styled, for a page that
    is swept again after it grew.
    """
    count = 0
    for button in root.findChildren(QPushButton):
        if is_exempt(button):
            continue
        if only_new and button.property("buttonRole"):
            continue
        found = lookup(button.text()) or _dialog_default(button)
        if found is None:
            continue
        style_button(button, *found)
        count += 1
    return count


_last_retint: tuple = ()


def retint_all(colours: dict[str, str], roots: Optional[Iterable[QWidget]] = None) -> None:
    """Redraw every system button's icon in the theme's colours.

    Icons are pictures and never see the stylesheet, so a theme change has to
    reach them by hand - the same reason the rail has its own `retint`.
    Every open window by default, pop-outs and dialogs included.
    """
    global _last_retint
    if roots is None:
        # Every button is drawn in the current colours when it is styled, so
        # a walk of every window is only needed when the colours have changed
        # since the last one - not each time the window adds a page.
        key = tuple(sorted(colours.items()))
        if key == _last_retint:
            return
        _last_retint = key
        app = QApplication.instance()
        roots = list(app.topLevelWidgets()) if app is not None else []
    for root in roots:
        for button in root.findChildren(QPushButton):
            if button.property("buttonIcon"):
                _paint_icon(button, colours)
