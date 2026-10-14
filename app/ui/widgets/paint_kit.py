r"""What every painted row shares: its hover and selection, its focus ring, its badge, its fonts.

Layer: L5 widget

**Why this exists** (review 2026-10-14, DS-2 and DS-11). Each delegate drew its own row
state: the Search list a rounded tint, the quick search box a tint and a 1.6px ring, the
contacts cards a 2px edge, the contacts table a square fill, the timeline a rounded tint
of its own - three selection looks - and **none of them drew keyboard focus**, so somebody
moving with the arrow keys lost their place on every list that paints itself. The badge,
the font sums and the "token or the theme's own" lookup were copied between three files,
and had already drifted (one badge 7px round, the other 8).

Everything here is painting from values in hand: no file, no store, no icon rendering.

* `paint_row_state(painter, rect, option, colours)` - the rounded hover or selection fill,
  one row radius (`theme.RADIUS["radius_control"]`), and a `theme.FOCUS_WIDTH` ring in
  `focus_ring` when the row is the view's current one and the view has the keyboard
  (Qt says so with `State_HasFocus`). Selection is checked before hover, so a selected
  row under the pointer still reads as selected.
* `paint_badge(painter, rect, fill, ...)` - the rounded square with a word or an icon in it.
* `scaled_font(base, step)` - `base` at a step of `theme.SCALE` (or a plain factor), in
  points, whatever `base` was sized in; `set_weight(font, name)` - a `theme.WEIGHT` step.
* `tok(colours, name)` - a token from the palette a painter was handed, else the theme's.
"""

from __future__ import annotations

from typing import Any, Optional, Union

from PySide6.QtCore import QRect, QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QStyle

from app.ui import theme

__all__ = ["tok", "row_radius", "badge_radius", "has_focus", "paint_row_state", "paint_focus_ring",
           "paint_badge", "scaled_font", "set_weight", "points_for_pixels", "rounded"]

#: One point is this many logical pixels at 96 DPI, the size Qt lays out in.
_PX_PER_PT = 96.0 / 72.0


def tok(colours: Optional[dict], name: str) -> QColor:
    """`name` from `colours` when it is there, else the current theme's - never a literal."""
    value = (colours or {}).get(name) or theme.theme_colours()[name]
    return QColor(value)


def row_radius() -> float:
    """The corner every painted row and card takes: the sheet's `radius_control`."""
    return theme.radius_px("radius_control")


def badge_radius() -> float:
    """A kind badge's corner: the same step as a row's, so the two never drift again."""
    return theme.radius_px("radius_control")


def rounded(rect: Union[QRect, QRectF], radius: float) -> QPainterPath:
    """A rounded-rectangle path."""
    path = QPainterPath()
    path.addRoundedRect(QRectF(rect), radius, radius)
    return path


def has_focus(option: Any) -> bool:
    """True when Qt painted this cell as the current one of a view that has the keyboard."""
    return bool(option.state & QStyle.StateFlag.State_HasFocus)


def paint_focus_ring(painter: QPainter, rect: Union[QRect, QRectF], colours: Optional[dict],
                     radius: Optional[float] = None) -> None:
    """A `FOCUS_WIDTH` ring in `focus_ring`, drawn inside `rect` so it is never clipped."""
    width = float(theme.FOCUS_WIDTH)
    corner = row_radius() if radius is None else radius
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    pen = QPen(tok(colours, "focus_ring"))
    pen.setWidthF(width)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    half = width / 2.0
    inner = QRectF(rect).adjusted(half, half, -half, -half)
    painter.drawRoundedRect(inner, max(0.0, corner - half), max(0.0, corner - half))
    painter.restore()


def paint_row_state(painter: QPainter, rect: QRect, option: Any, colours: Optional[dict], *,
                    radius: Optional[float] = None, inset: tuple[int, int] = (2, 1),
                    ring: Optional[bool] = None) -> bool:
    """Paint a row's hover or selection fill and, when it has the keyboard, its focus ring.

    `inset` is how far the tile sits in from the row's edges (x, y): 2 and 1 for a list
    row, 0 for a table cell, which must meet its neighbours. `radius` defaults to the one
    row radius; 0 is a square cell. `ring` left as None follows Qt (`State_HasFocus`); a
    list whose keyboard lives elsewhere - the quick search box's, which is driven from its
    text box - passes `ring=selected` so its chosen row is ringed the same way.
    Returns whether the row is selected, for a caller that also thickens its words.
    """
    selected = bool(option.state & QStyle.StateFlag.State_Selected)
    hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
    corner = row_radius() if radius is None else float(radius)
    dx, dy = inset
    tile = QRectF(rect).adjusted(dx, dy, -dx, -dy)
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    if selected:
        fill: Optional[QColor] = tok(colours, "accent_soft")
    elif hovered:
        fill = tok(colours, "surface_hover")
    else:
        fill = None
    if fill is not None:
        if corner > 0:
            painter.fillPath(rounded(tile, corner), QBrush(fill))
        else:
            painter.fillRect(tile, fill)
    painter.restore()
    ringed = has_focus(option) if ring is None else bool(ring)
    if ringed:
        paint_focus_ring(painter, tile, colours, corner)
    return selected


def paint_badge(painter: QPainter, rect: QRect, fill: QColor, *, word: str = "",
                ink: Optional[QColor] = None, glyph: Any = None, glyph_side: int = 0,
                font: Optional[QFont] = None, radius: Optional[float] = None) -> None:
    """The kind badge: a rounded square of `fill` with `word` in `ink`, or `glyph` (a QIcon)
    centred at `glyph_side`. The word's font defaults to the painter's, bold, sized to the badge."""
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.fillPath(rounded(rect, badge_radius() if radius is None else radius), QBrush(fill))
    if glyph is not None and not glyph.isNull():
        side = glyph_side or max(14, rect.height() * 9 // 16)
        glyph.paint(painter, rect.left() + (rect.width() - side) // 2,
                    rect.top() + (rect.height() - side) // 2, side, side)
    if word:
        if font is None:
            font = QFont(painter.font())
            set_weight(font, "bold")
            font.setPointSizeF(max(6.0, min(9.0, rect.height() / 4.0)))
        painter.setFont(font)
        painter.setPen(QPen(ink if ink is not None else tok(None, "accent_on")))
        painter.drawText(rect, int(Qt.AlignmentFlag.AlignCenter), word)
    painter.restore()


def points_for_pixels(px: float) -> float:
    """A pixel height as points at Qt's 96 logical DPI, for a font sized to drawn geometry
    (the initials in an avatar circle). Points, because a pixel-sized font ignores the
    text-size setting and the house rule says points."""
    return max(1.0, float(px) / _PX_PER_PT)


def _base_points(base: QFont) -> float:
    """`base`'s size in points: its own, or its pixel size turned into points, or the default."""
    points = base.pointSizeF()
    if points > 0:
        return points
    pixels = base.pixelSize()
    if pixels > 0:
        return points_for_pixels(pixels)
    return theme.DEFAULT_POINT_SIZE


def scaled_font(base: QFont, step: Union[str, float] = "body", *, minimum: float = 6.0,
                weight: Optional[str] = None) -> QFont:
    """`base` at a step of `theme.SCALE` (`"small"`, `"large"`, `"title"`...) or at a plain
    factor, in points - a pixel-sized `base` is converted, never copied as pixels."""
    factor = theme.SCALE[step] if isinstance(step, str) else float(step)
    font = QFont(base)
    font.setPointSizeF(max(minimum, _base_points(base) * factor))
    if weight is not None:
        set_weight(font, weight)
    return font


def set_weight(font: QFont, name: str) -> QFont:
    """Give `font` a `theme.WEIGHT` step by name. Changes it in place and returns it."""
    font.setWeight(QFont.Weight(theme.weight(name)))
    return font
