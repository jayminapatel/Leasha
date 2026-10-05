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
"""

from __future__ import annotations

from typing import Any, Optional

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPixmap

__all__ = ["icon_for", "clear_cache", "ICON_PX"]

#: Drawn at this size and left for Qt to scale down to the row. Larger than any
#: row so the glyph is sharp on a high-DPI display, where a 16px pixmap drawn
#: into a 24px slot is visibly soft.
ICON_PX = 32

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
