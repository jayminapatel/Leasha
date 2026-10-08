r"""How a row of the quick search box is drawn.

Layer: L5

Painted rather than built from widgets, as the Search list is
(`result_delegate.py`): seven rows of three labels each would be twenty-one
widgets rebuilt on every keystroke.

A row is a rounded tile: the kind's badge - the brand colour for documents,
mail and code, and its icon in white - then the title, and under it the type
and folder or who sent the message; the date sits at the right. The current
row has a soft accent ground and a focus ring, because the keyboard is how
this box is used and the ring is where the keyboard is.

**Nothing here touches the disk.** The badge icons are drawn once per theme
in `retint`, never inside `paint`.
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import QRect, QRectF, QSize, Qt
from PySide6.QtGui import QBrush, QColor, QFont, QFontMetrics, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QStyle, QStyledItemDelegate

__all__ = ["MiniRowDelegate", "ROLE_LINES", "BADGE_ICONS"]

#: Where an item keeps its `RowLines` (or, for a recent search, a dict).
ROLE_LINES = int(Qt.ItemDataRole.UserRole) + 7

#: The badge's icon per chip bucket, plus the two kinds of recent search.
BADGE_ICONS = {
    "files": "file-text", "mail": "mail", "photos": "image", "code": "code",
    "recent": "history", "saved": "bookmark",
}

#: The badge's fill per colour family: the brand's text-safe tints (`theme.py`),
#: the same in both themes, as on the Search list.
_FAMILY_TOKEN = {"doc": "kind_doc", "mail": "kind_mail", "code": "kind_code",
                 "other": "kind_other"}

_PAD_X = 8
_PAD_Y = 6
_GAP = 10
_RADIUS = 8.0


class MiniRowDelegate(QStyledItemDelegate):
    """Two lines and a badge per row; one line for a recent search."""

    def __init__(self, parent: Any = None) -> None:
        super().__init__(parent)
        self._colours: dict = {}
        self._icons: dict = {}
        self.retint()

    def retint(self, colours: Any = None) -> None:
        """Take the theme's colours and draw the badge icons for them. Off the paint path."""
        from app.ui.theme import theme_colours
        from app.ui.widgets.icons import icon

        self._colours = dict(colours or theme_colours())
        dim = self._colours.get("text_dim", "#888888")
        self._icons = {}
        for bucket, glyph in BADGE_ICONS.items():
            ink = dim if bucket in ("recent", "saved") else "#ffffff"
            self._icons[bucket] = icon(glyph, ink)

    # -- sizes -------------------------------------------------------------------

    def _fonts(self, base: QFont) -> tuple[QFont, QFont]:
        title = QFont(base)
        title.setPointSizeF(max(8.0, base.pointSizeF() * 13.0 / 12.0))
        title.setWeight(QFont.Weight.DemiBold)
        small = QFont(base)
        small.setPointSizeF(max(7.0, base.pointSizeF()))
        return title, small

    def sizeHint(self, option: Any, index: Any) -> QSize:          # noqa: N802 - Qt's name
        title, small = self._fonts(option.font)
        one = QFontMetrics(title).height()
        lines = index.data(ROLE_LINES)
        two_line = not isinstance(lines, dict)
        height = one + (QFontMetrics(small).height() + 2 if two_line else 0) + 2 * _PAD_Y + 4
        height = max(height, self._badge_side(option) + 2 * _PAD_Y)
        return QSize(max(200, option.rect.width()), height)

    def _badge_side(self, option: Any) -> int:
        return max(26, QFontMetrics(option.font).height() * 2 + 2)

    # -- painting ------------------------------------------------------------------

    def paint(self, painter: QPainter, option: Any, index: Any) -> None:
        lines = index.data(ROLE_LINES)
        if lines is None:
            super().paint(painter, option, index)
            return
        colours = self._colours
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        tile = QRectF(option.rect.adjusted(2, 1, -2, -1))
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
        path = QPainterPath()
        path.addRoundedRect(tile, _RADIUS, _RADIUS)
        if selected:
            painter.fillPath(path, QBrush(QColor(colours.get("accent_soft", "#e9e4fb"))))
            ring = QPen(QColor(colours.get("focus_ring", "#4a37b0")))
            ring.setWidthF(1.6)
            painter.setPen(ring)
            painter.drawRoundedRect(tile.adjusted(0.8, 0.8, -0.8, -0.8), _RADIUS, _RADIUS)
        elif hovered:
            painter.fillPath(path, QBrush(QColor(colours.get("surface_hover", "#e8eaed"))))

        recent = isinstance(lines, dict)
        bucket = lines.get("kind", "recent") if recent else lines.bucket
        side = self._badge_side(option)
        rect = option.rect
        badge = QRect(rect.left() + _PAD_X, rect.top() + (rect.height() - side) // 2, side, side)
        badge_path = QPainterPath()
        badge_path.addRoundedRect(QRectF(badge), 7.0, 7.0)
        if recent:
            painter.fillPath(badge_path, QBrush(QColor(colours.get("surface_alt", "#f0f1f3"))))
        else:
            token = _FAMILY_TOKEN.get(lines.family, "kind_other")
            painter.fillPath(badge_path, QBrush(QColor(colours.get(token, "#6c6685"))))
        glyph = self._icons.get(bucket)
        if glyph is not None and not glyph.isNull():
            inner = max(14, side * 9 // 16)
            glyph.paint(painter, badge.left() + (side - inner) // 2,
                        badge.top() + (side - inner) // 2, inner, inner)

        title_font, small_font = self._fonts(option.font)
        text_left = badge.right() + _GAP
        right = rect.right() - _PAD_X - 4
        when = "" if recent else lines.when
        if when:
            painter.setFont(small_font)
            when_width = QFontMetrics(small_font).horizontalAdvance(when)
            right_edge = right
            right = right - when_width - _GAP
        text_width = max(10, right - text_left)

        title = lines.get("text", "") if recent else lines.title
        subtitle = lines.get("note", "") if recent else lines.subtitle
        title_metrics = QFontMetrics(title_font)
        small_metrics = QFontMetrics(small_font)
        ink = QColor(colours.get("text", "#1b1d20"))
        dim = QColor(colours.get("text_dim", "#585e66"))
        faint = QColor(colours.get("text_faint", "#6b7178"))

        if recent and not subtitle:
            block = title_metrics.height()
        else:
            block = title_metrics.height() + 2 + small_metrics.height()
        top = rect.top() + (rect.height() - block) // 2

        painter.setFont(title_font)
        painter.setPen(QPen(ink))
        painter.drawText(QRect(text_left, top, text_width, title_metrics.height()),
                         int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                         title_metrics.elidedText(title, Qt.TextElideMode.ElideRight, text_width))
        if subtitle:
            painter.setFont(small_font)
            painter.setPen(QPen(dim))
            line_top = top + title_metrics.height() + 2
            painter.drawText(QRect(text_left, line_top, text_width, small_metrics.height()),
                             int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                             small_metrics.elidedText(subtitle, Qt.TextElideMode.ElideMiddle,
                                                      text_width))
        if when:
            painter.setFont(small_font)
            painter.setPen(QPen(faint))
            painter.drawText(QRect(right + _GAP, top, right_edge - right - _GAP,
                                   title_metrics.height()),
                             int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter), when)
        painter.restore()
