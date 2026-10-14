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

from PySide6.QtCore import QRect, QSize, Qt
from PySide6.QtGui import QFont, QFontMetrics, QPainter, QPen
from PySide6.QtWidgets import QStyle, QStyledItemDelegate

from app.ui.widgets.paint_kit import paint_badge, paint_row_state, scaled_font, tok

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


class MiniRowDelegate(QStyledItemDelegate):
    """Two lines and a badge per row; one line for a recent search."""

    def __init__(self, parent: Any = None) -> None:
        super().__init__(parent)
        self._colours: dict = {}
        self._icons: dict = {}
        self.retint()

    def retint(self, colours: Any = None) -> None:
        """Take the theme's colours and draw the badge icons for them. Off the paint path."""
        from app.ui.result_delegate import BADGE_INK
        from app.ui.theme import theme_colours
        from app.ui.widgets.icons import icon

        self._colours = dict(colours or theme_colours())
        dim = self._colours.get("text_dim", "#888888")
        self._icons = {}
        for bucket, glyph in BADGE_ICONS.items():
            # On a kind fill, the results list's own badge ink - one value, measured
            # on every fill by test_brand_colours, rather than a second copy of it.
            ink = dim if bucket in ("recent", "saved") else BADGE_INK
            self._icons[bucket] = icon(glyph, ink)

    # -- sizes -------------------------------------------------------------------

    def _fonts(self, base: QFont) -> tuple[QFont, QFont]:
        """The title font (the scale's `large` step, semibold) and the body font for the rest.

        The title was `base` times 13/12, a step of its own; it is the nearest step of
        `theme.SCALE` now, `large` (15/13), so it is the size the search box's own text is
        (review 2026-10-14, DS-8)."""
        title = scaled_font(base, "large", minimum=8.0, weight="semibold")
        small = scaled_font(base, "body", minimum=7.0)
        return title, small

    def sizeHint(self, option: Any, index: Any) -> QSize:          # noqa: N802 - Qt's name
        """Two lines for a result, one for a recent search, never shorter than the badge."""
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
        """Paint one row from `ROLE_LINES`. No I/O and no icon rendering: the badge
        icons were drawn in `retint`.
        """
        lines = index.data(ROLE_LINES)
        if lines is None:
            super().paint(painter, option, index)
            return
        colours = self._colours
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        # The keyboard lives in the box above, not in this list, so the chosen row
        # carries the ring whether or not the list has focus - drawn by the one
        # helper every painted list uses (review 2026-10-14, DS-2).
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        paint_row_state(painter, option.rect, option, colours, ring=selected)

        recent = isinstance(lines, dict)
        bucket = lines.get("kind", "recent") if recent else lines.bucket
        side = self._badge_side(option)
        rect = option.rect
        badge = QRect(rect.left() + _PAD_X, rect.top() + (rect.height() - side) // 2, side, side)
        fill = tok(colours, "surface_alt" if recent else _FAMILY_TOKEN.get(lines.family, "kind_other"))
        paint_badge(painter, badge, fill, glyph=self._icons.get(bucket),
                    glyph_side=max(14, side * 9 // 16))

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
        ink = tok(colours, "text")
        dim = tok(colours, "text_dim")
        faint = tok(colours, "text_faint")

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
