r"""Painting a result row, instead of building three widgets for it.

Layer: L5

`QListWidget` with `setItemWidget` gave every row three live `QLabel`s. Five
hundred results is fifteen hundred widgets, each with its own layout, palette
and event handling, all constructed before the first one is visible. A
`QStyledItemDelegate` paints only the rows on screen - a dozen or so - and the
cost stops scaling with the result count.

**The layout is defined once, here, and used twice**: `size_for` measures it and
`paint` draws it, from the same numbers. When those two disagree the symptom is
text clipped at the bottom of every row, which is easy to produce accidentally
and tedious to chase, so there is exactly one source for the geometry.

The snippet's highlight ranges come from `presenter.Snippet` and are drawn as
runs of bold rather than as rich text: `QTextDocument` per row would put back
most of the cost this file exists to remove.
"""

from __future__ import annotations

from typing import Any, Optional

from PyQt6.QtCore import QRect, QSize, Qt
from PyQt6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPen
from PyQt6.QtWidgets import QStyle, QStyledItemDelegate

from app.ui.presenter import ResultGroup, group_subtitle, kind_tag, why
from app.ui.view_options import Density, Metrics, ViewPreferences

__all__ = ["ResultDelegate", "ROLE_PAYLOAD", "ROLE_EXPANDED"]

#: The row's `ResultGroup` or `ResultRow`.
ROLE_PAYLOAD = int(Qt.ItemDataRole.UserRole)
#: True when this group is currently showing its chunks.
ROLE_EXPANDED = int(Qt.ItemDataRole.UserRole) + 1


class ResultDelegate(QStyledItemDelegate):
    """Draws a result group, or one chunk of an expanded group."""

    def __init__(self, parent: Optional[Any] = None) -> None:
        super().__init__(parent)
        self.prefs = ViewPreferences()

    # -- geometry ----------------------------------------------------------

    def _fonts(self, base: QFont) -> tuple[QFont, QFont, QFont]:
        """`(name, meta, body)`. Derived from whatever the view is using.

        Sizes are relative, so the text-size preference moves all three together
        and the hierarchy survives at every size.
        """
        metrics = Metrics.for_density(self.prefs.density)
        # A widget styled with `font-size: 13px` reports `pointSize() == -1`,
        # so the pixel size has to be honoured when there is no point size -
        # this is the same trap that produced a `setPointSize(-1)` warning.
        point = base.pointSize()
        pixel = base.pixelSize()

        def sized(delta: int, bold: bool = False) -> QFont:
            font = QFont(base)
            if point > 0:
                font.setPointSize(max(6, point + delta))
            elif pixel > 0:
                font.setPixelSize(max(8, pixel + delta))
            font.setBold(bold)
            return font

        return sized(metrics.name_bump, True), sized(-metrics.meta_drop), sized(0)

    def sizeHint(self, option: Any, index: Any) -> QSize:   # noqa: N802 - Qt's naming
        payload = index.data(ROLE_PAYLOAD)
        name_font, meta_font, body_font = self._fonts(option.font)
        metrics = Metrics.for_density(self.prefs.density)
        width = max(120, option.rect.width())

        rows = [QFontMetrics(meta_font).height()]
        if isinstance(payload, ResultGroup):
            rows.insert(0, QFontMetrics(name_font).height())
        if self._shows_snippet(payload):
            text = _snippet_text(payload)
            rows.append(_wrapped_height(body_font, text, width - 2 * metrics.pad_x))

        height = sum(rows) + metrics.gap * (len(rows) - 1) + 2 * metrics.pad_y
        return QSize(width, height)

    def _shows_snippet(self, payload: Any) -> bool:
        """Compact drops the snippet from *group* rows only.

        A chunk row is nothing but its passage - dropping the snippet there
        would leave an empty line, so compact makes chunk rows tighter instead.
        """
        if isinstance(payload, ResultGroup):
            return self.prefs.density != Density.COMPACT
        return True

    # -- painting ----------------------------------------------------------

    def paint(self, painter: QPainter, option: Any, index: Any) -> None:
        payload = index.data(ROLE_PAYLOAD)
        if payload is None:
            super().paint(painter, option, index)
            return

        painter.save()
        if option.state & QStyle.StateFlag.State_Selected:
            painter.fillRect(option.rect, option.palette.highlight())
            text_colour = option.palette.highlightedText().color()
        else:
            text_colour = option.palette.text().color()
        faint = QColor(text_colour)
        faint.setAlpha(150)

        metrics = Metrics.for_density(self.prefs.density)
        name_font, meta_font, body_font = self._fonts(option.font)
        left = option.rect.left() + metrics.pad_x
        width = option.rect.width() - 2 * metrics.pad_x
        y = option.rect.top() + metrics.pad_y

        if isinstance(payload, ResultGroup):
            y = self._paint_group(painter, payload, left, y, width,
                                  name_font, meta_font, text_colour, faint, metrics,
                                  expanded=bool(index.data(ROLE_EXPANDED)))
        else:
            left += metrics.indent
            width -= metrics.indent
            y = self._paint_chunk(painter, payload, left, y, width,
                                  meta_font, faint, metrics)

        if self._shows_snippet(payload):
            painter.setFont(body_font)
            _draw_snippet(painter, _snippet_payload(payload),
                          QRect(left, y, width, option.rect.bottom() - y), text_colour)
        painter.restore()

    def _paint_group(self, painter, group, left, y, width,
                     name_font, meta_font, colour, faint, metrics,
                     expanded: bool = False) -> int:
        painter.setFont(name_font)
        name_metrics = QFontMetrics(name_font)
        painter.setFont(meta_font)
        date_width = QFontMetrics(meta_font).horizontalAdvance(group.when) + 8

        # Date first, right-aligned, so the name is elided against the space
        # actually left rather than overlapping it.
        painter.setPen(QPen(faint))
        painter.drawText(QRect(left + width - date_width, y, date_width,
                               name_metrics.height()),
                         int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter),
                         group.when)

        painter.setFont(name_font)
        painter.setPen(QPen(colour))
        tag = f"[{kind_tag(group.kind)}]  " if group.kind else ""
        name = name_metrics.elidedText(
            tag + group.name, Qt.TextElideMode.ElideMiddle, width - date_width)
        painter.drawText(QRect(left, y, width - date_width, name_metrics.height()),
                         int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                         name)
        y += name_metrics.height() + metrics.gap

        painter.setFont(meta_font)
        painter.setPen(QPen(faint))
        meta_metrics = QFontMetrics(meta_font)
        painter.drawText(QRect(left, y, width, meta_metrics.height()),
                         int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                         meta_metrics.elidedText(
                             group_subtitle(group, show_scores=self.prefs.show_scores,
                                            expanded=expanded),
                             Qt.TextElideMode.ElideRight, width))
        return y + meta_metrics.height() + metrics.gap

    def _paint_chunk(self, painter, row, left, y, width, meta_font, faint, metrics) -> int:
        painter.setFont(meta_font)
        painter.setPen(QPen(faint))
        meta_metrics = QFontMetrics(meta_font)
        bits = [row.location]
        if self.prefs.show_scores:
            bits.append(why(row))
        painter.drawText(QRect(left, y, width, meta_metrics.height()),
                         int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                         meta_metrics.elidedText("  ·  ".join(b for b in bits if b),
                                                 Qt.TextElideMode.ElideRight, width))
        return y + meta_metrics.height() + metrics.gap


# ---------------------------------------------------------------------------
# Geometry helpers. The *text* decisions live in `presenter.py`, Qt-free, so
# they can be checked without a display - see `group_subtitle` and `kind_tag`.
# ---------------------------------------------------------------------------

def _snippet_payload(payload: Any) -> Any:
    if isinstance(payload, ResultGroup):
        return payload.best.snippet if payload.best else None
    return getattr(payload, "snippet", None)


def _snippet_text(payload: Any) -> str:
    snippet = _snippet_payload(payload)
    return getattr(snippet, "text", "") if snippet else ""


def _wrapped_height(font: QFont, text: str, width: int) -> int:
    """How tall the snippet will be once wrapped. Used by `sizeHint` only."""
    if not text or width <= 0:
        return 0
    metrics = QFontMetrics(font)
    rect = metrics.boundingRect(QRect(0, 0, width, 10_000),
                                int(Qt.TextFlag.TextWordWrap), text)
    # Two lines is enough to judge relevance and keeps ten results on a screen.
    return min(rect.height(), metrics.height() * 2)


def _draw_snippet(painter: QPainter, snippet: Any, rect: QRect, colour: QColor) -> None:
    """Draw the passage with its matched terms in bold.

    Runs of bold rather than rich text: a `QTextDocument` per row would put back
    most of the cost this delegate exists to remove, and the highlight ranges
    are already computed by `presenter.build_snippet`.
    """
    if snippet is None or not getattr(snippet, "text", ""):
        return
    painter.setPen(QPen(colour))
    text = snippet.text
    highlights = sorted(getattr(snippet, "highlights", ()) or ())

    base = QFont(painter.font())
    bold = QFont(base)
    bold.setBold(True)
    metrics = QFontMetrics(base)

    x, y = rect.left(), rect.top() + metrics.ascent()
    limit = rect.right()
    cursor = 0
    for start, end in [*highlights, (len(text), len(text))]:
        for piece, font in ((text[cursor:start], base), (text[start:end], bold)):
            if not piece or x >= limit:
                continue
            painter.setFont(font)
            advance = QFontMetrics(font).horizontalAdvance(piece)
            if x + advance > limit:
                piece = QFontMetrics(font).elidedText(
                    piece, Qt.TextElideMode.ElideRight, limit - x)
                advance = QFontMetrics(font).horizontalAdvance(piece)
            painter.drawText(x, y, piece)
            x += advance
        cursor = end
    painter.setFont(base)
