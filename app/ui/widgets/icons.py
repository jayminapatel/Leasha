r"""The one place an SVG icon is loaded.

Layer: L5

UI Redesign (202626160950 §1c). The shell had no icon set at all - every
control was text-labelled - and the redesign needs a dozen monochrome glyphs
for the rail and the Search toolbar. They are a subset of Lucide (ISC), shipped
as SVG under `assets/icons/` with the licence file beside them.

**One function, `icon(name, colour)`.** The SVGs are authored with
`stroke="currentColor"`, which Qt's renderer does not resolve; this substitutes
the palette colour into the bytes before rendering, so the same file is
legible in both themes - the same argument `command_icon.py` makes for
painting its glyphs. Rendered through `QSvgRenderer` at a size large enough to
stay sharp on a high-DPI display, then handed to Qt as a pixmap it may scale
down.

**Cached by (name, colour, size)**, because the rail is rebuilt when the theme
changes and the toolbar toggles repaint on every state change.

**Never raises for a missing file.** A glyph that cannot be found comes back
as an empty `QIcon`; the control still has its accessible name and tooltip,
so it is usable without the picture. A missing icon is a visible bug, not a
startup failure.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from PyQt6.QtCore import QByteArray, QRectF, Qt
from PyQt6.QtGui import QColor, QIcon, QPainter, QPixmap

from app.ui.tray import assets_dir

__all__ = ["icon", "icon_file", "clear_cache", "ICON_PX", "ICON_NAMES"]

#: Rendered at this size and left for Qt to scale down.
ICON_PX = 48

#: Every glyph this order ships, so a test can assert each file exists and a
#: caller asking for a name outside the set is caught at review rather than at
#: runtime.
ICON_NAMES = (
    # The rail and the Search toolbar (§1c as first written).
    "search", "folder", "mail", "code", "hard-drive", "chart-column",
    "settings", "pin", "chart-no-axes-column", "layout-grid", "panel-right",
    "ellipsis", "x", "chevron-down", "chevron-right", "move", "sparkles",
    "database",
    # §0.3, owner 2026-09-16 - "icons wherever possible": the inspector's
    # buttons, the menus, CategoryNav, the Indexing page's controls.
    "folder-open", "external-link", "file-text", "sliders-horizontal",
    "palette", "cpu", "clock", "play", "pause", "square", "scan-search",
    "refresh-cw", "trash-2", "circle-help", "keyboard", "eye", "image", "list", "bookmark",
    "info", "x-circle", "copy", "sun-moon",
    # The Chat tab (order 202626270611).
    "message-square",
    # The button system (widgets/buttons.py, owner 2026-09-27: every action
    # button carries an icon). Lucide 1.48.0, same licence and stroke.
    "folder-plus", "folder-minus", "list-checks", "list-x", "plus",
    "rotate-ccw", "rotate-cw", "save", "arrow-right-left", "eraser",
    "flask-conical", "gauge", "users", "brain", "stethoscope", "circle-check",
    "package", "history", "pencil", "send", "globe", "check", "zoom-in",
    "zoom-out", "move-horizontal", "printer", "chevron-up", "calendar",
    "split", "file-down", "git-branch",
)

_cache: dict[tuple[str, str, int], QIcon] = {}


def icon_file(name: str) -> Optional[Path]:
    """Where the SVG for `name` lives, or `None` if it is not shipped."""
    candidate = assets_dir() / "icons" / f"{name}.svg"
    return candidate if candidate.is_file() else None


def _svg_bytes(name: str, colour: str) -> Optional[bytes]:
    path = icon_file(name)
    if path is None:
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    return text.replace("currentColor", colour).encode("utf-8")


def _render(data: bytes, size: int) -> QPixmap:
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    try:
        from PyQt6.QtSvg import QSvgRenderer  # noqa: PLC0415 - optional module
    except ImportError:                               # pragma: no cover - venv
        QSvgRenderer = None                           # type: ignore[assignment]
    if QSvgRenderer is not None:
        renderer = QSvgRenderer(QByteArray(data))
        if renderer.isValid():
            painter = QPainter(pixmap)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            renderer.render(painter, QRectF(0, 0, size, size))
            painter.end()
            return pixmap
    # The SVG image plugin ships with PyQt6; this is the path when QtSvg's
    # renderer is unavailable rather than a second design.
    loaded = QPixmap()
    if loaded.loadFromData(data, "SVG"):
        return loaded.scaled(size, size, Qt.AspectRatioMode.KeepAspectRatio,
                             Qt.TransformationMode.SmoothTransformation)
    return pixmap


def icon(name: str, colour: str, *, size: int = ICON_PX) -> QIcon:
    """A `QIcon` for `name` drawn in `colour` (a `#rrggbb` palette token value).

    Empty when the file is missing - see the module docstring.
    """
    key = (name, colour, size)
    cached = _cache.get(key)
    if cached is not None:
        return cached
    data = _svg_bytes(name, colour)
    if data is None:
        result = QIcon()
    else:
        result = QIcon(_render(data, size))
    _cache[key] = result
    return result


def tinted(name: str, colour: QColor, *, size: int = ICON_PX) -> QIcon:
    """`icon()` for a `QColor` rather than a token string."""
    return icon(name, colour.name(), size=size)


def clear_cache() -> None:
    """For a theme change, and for tests."""
    _cache.clear()
