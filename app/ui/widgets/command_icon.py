r"""Icons for the `/` menu, painted rather than shipped.

Layer: L5

Asked for: *"i want the / command to show icons too similar to claude / command"*.

**A glyph painted into a pixmap, not an image file.** Three reasons, and the
third is the one that decides it:

*Nothing to ship.* No asset folder, no packaging step that can drop a file, no
second thing to keep in step with `COMMANDS`. A command that gains an icon
gains one character in `app/search/commands.py`.

*Nothing to redraw for dark mode.* The colour comes from the palette at paint
time, so the same glyph is legible on both. A shipped PNG is one bitmap with one
colour baked in - which is the exact failure `test_forced_theme_stays_readable`
exists to catch, made permanent.

*It is one line of Qt.* The alternative - a delegate drawing text in a first
column - means owning the popup's painting, and the popup belongs to
`QCompleter`.

**Cached by (character, colour, size).** The menu is rebuilt on every keystroke
after `/`, and painting eleven pixmaps per keystroke to throw them away is the
kind of cost that is invisible until somebody types quickly.

**Lucide first, since UI phase B (2026-10-13).** Every other picture in the
window is a Lucide SVG from `widgets/icons.py`, so a `/` menu of painted
characters was the one list that did not look like the rest. `PICTURES` names
a Lucide icon for every switch in every catalogue - Search, Code (index and
git switches), Mail, Files, Contacts, and the `/saved` action - and
`command_icon` draws it in the popup's own ink, through the same cache-by-colour
route as the rail. The painted character stays as the fallback (a heading row,
or a switch added to a catalogue before it has a row here), and stays in
`Command.icon` because the terminal (`app.cli`, `app/shell`) has no pictures.
`tests/unit/test_ui_style_rules.py` fails when a switch has no Lucide row.
"""

from __future__ import annotations

from typing import Any, Optional

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPixmap

__all__ = ["icon_for", "clear_cache", "ICON_PX", "PICTURES", "CONTACT_PICTURES",
           "VALUE_PICTURE", "OFFER_PICTURE", "picture_for", "command_icon"]

#: Drawn at this size and left for Qt to scale down to the row. Larger than any
#: row so the glyph is sharp on a high-DPI display, where a 16px pixmap drawn
#: into a 24px slot is visibly soft.
ICON_PX = 32

#: **Every `/` switch -> its Lucide icon** (from `icons.ICON_NAMES`). Keyed by
#: the switch's name, which is the same word in every box that offers it -
#: `/path` is a folder in Search and in Code alike.
PICTURES: dict[str, str] = {
    # What kind of thing, and its name.
    "type": "layout-grid", "extension": "sheet", "name": "file", "file": "file",
    "size": "gauge", "only": "image", "shows": "sparkles",
    # Mail.
    "from": "mail", "to": "send", "subject": "file-text", "has": "list-checks",
    "attachment": "link", "mailbox": "inbox", "mailfolder": "folder-open",
    "message": "message-square",
    # When.
    "after": "calendar-clock", "before": "calendar-clock", "date": "calendar",
    "between": "arrow-right-left", "since": "calendar-clock", "until": "calendar-clock",
    # Where.
    "path": "folder", "exclude-path": "folder-minus", "exclude-file": "ban",
    "on": "hard-drive", "place": "map-pin", "repo": "code",
    # Who.
    "who": "user", "author": "user-round", "committer": "pencil",
    # How it is ordered, and what state it is in.
    "sort": "list", "status": "loader-circle", "depth": "sliders-horizontal",
    # Git history.
    "branch": "git-branch", "all-branches": "git-branch", "remote-branches": "cloud",
    "history": "history", "commit": "clock", "range": "move-horizontal", "tag": "tag",
    "lifetime": "hourglass", "merges": "merge", "introduced": "plus",
    "removed": "trash", "changed": "refresh-cw", "lifecycle": "rotate-cw",
    "file-history": "history", "added-only": "plus", "removed-only": "circle-minus",
    "deleted-files": "trash-2",
    # How the words are matched, and what kind of code.
    "regex": "scan-search", "word": "search", "ignore-case": "keyboard",
    "class": "package", "interface": "layout-grid", "function": "code",
    "symbol": "at-sign", "endpoint": "globe", "config": "settings",
    # The one action.
    "saved": "bookmark",
}

#: The Contacts box's switches where its word means something else: `/name` is
#: a person's name there, not a file's. Every other Contacts switch is new.
CONTACT_PICTURES: dict[str, str] = {
    "name": "user", "company": "building-2", "title": "briefcase", "email": "at-sign",
    "phone": "phone", "tag": "tag", "group": "users", "has": "list-checks",
    "quiet": "clock", "favourite": "star",
}

#: Beside a value of the chosen switch, and beside a search this person ran or
#: saved - the Lucide spellings of `command_popup.VALUE_ICON` and `OFFER_ICON`.
VALUE_PICTURE = "chevron-right"
OFFER_PICTURE = "rotate-ccw"


def picture_for(command: Any) -> str:
    """The Lucide name for a `Command`, or "" when it has none (painted instead)."""
    name = str(getattr(command, "name", "") or "")
    try:
        from app.ui.presenter.contact_commands import CONTACT_COMMANDS  # noqa: PLC0415

        if any(command is c for c in CONTACT_COMMANDS):
            return CONTACT_PICTURES.get(name, "")
    except Exception:                # noqa: BLE001 - a picture, never a failure
        pass
    return PICTURES.get(name, "")


def command_icon(picture: str, glyph: str, colour: Any) -> QIcon:
    """The Lucide `picture` in `colour`, or the painted `glyph` when there is no
    Lucide picture (or its file is missing). Never raises - see `icon_for`."""
    try:
        from app.ui.widgets.icons import ICON_NAMES, icon  # noqa: PLC0415

        if picture and picture in ICON_NAMES:
            ink = QColor(colour) if not isinstance(colour, QColor) else colour
            found = icon(picture, ink.name())
            if not found.isNull():
                return found
    except Exception:                # noqa: BLE001
        pass
    return icon_for(glyph, colour)

_cache: dict[tuple[str, str, int], QIcon] = {}


def clear_cache() -> None:
    """Drop every painted icon. Called when the theme changes - the cache is
    keyed by colour, so this is belt and braces rather than a correctness fix."""
    _cache.clear()


def icon_for(glyph: str, colour: Any, *, size: int = ICON_PX) -> QIcon:
    """One character, drawn in `colour`, as an icon.

    `colour` is anything `QColor` accepts - normally
    `palette().color(QPalette.ColorRole.Text)`, so the icon is exactly as dark
    as the words beside it.

    Never raises. An icon that cannot be painted is an icon-less row, never a
    menu that fails to open on the keystroke whose whole purpose is discovery.
    """
    try:
        text = (glyph or "").strip()[:2]
        if not text:
            return QIcon()

        ink = QColor(colour) if not isinstance(colour, QColor) else colour
        key = (text, ink.name(QColor.NameFormat.HexArgb), int(size))
        found = _cache.get(key)
        if found is not None:
            return found

        pixmap = QPixmap(int(size), int(size))
        # Transparent, so the row's own background - including the highlight
        # under the selected row - shows through. A filled square would draw a
        # light tile onto a dark menu.
        pixmap.fill(Qt.GlobalColor.transparent)

        painter = QPainter(pixmap)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
            painter.setPen(ink)
            font = QFont()
            # Proportional to the pixmap rather than a point size: the glyph has
            # to fill the icon whatever the person's text size is set to, and a
            # fixed 10pt in a 32px box is a speck.
            font.setPixelSize(max(8, int(size * 0.72)))
            painter.setFont(font)
            painter.drawText(QRectF(0, 0, size, size),
                             int(Qt.AlignmentFlag.AlignCenter), text)
        finally:
            # **Always ended.** A live QPainter on a QPixmap that is then
            # returned and cached leaves the pixmap locked, and the symptom is
            # a later paint silently doing nothing.
            painter.end()

        icon = QIcon(pixmap)
        _cache[key] = icon
        return icon
    except Exception:                # noqa: BLE001 - see the docstring
        return QIcon()


def text_colour(widget: Optional[Any]) -> QColor:
    """The colour the words in this widget are drawn in.

    Asked of the widget rather than of the application, because a popup can
    carry its own palette, and asked at paint time rather than at startup
    because the theme changes while the window is open.
    """
    from PySide6.QtGui import QPalette
    from PySide6.QtWidgets import QApplication

    try:
        palette = widget.palette() if widget is not None else QApplication.palette()
        return palette.color(QPalette.ColorRole.Text)
    except Exception:                # noqa: BLE001
        return QColor(Qt.GlobalColor.black)
