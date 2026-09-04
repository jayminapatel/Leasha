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
from app.ui.theme import theme_colours
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
            rows.append(_snippet_height(body_font, text, width - 2 * metrics.pad_x,
                                        max_lines=_max_snippet_lines(self.prefs.density)))

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
        # **Painted from the theme's own tokens, not from the widget palette.**
        #
        # The palette is the operating system's: nothing in this application
        # ever sets one, because theming is done with a stylesheet. So on a
        # light-mode Windows with the theme forced to dark, `palette.text()`
        # returned near-black and it was painted onto `#1e1f22`. The results
        # were unreadable and every other widget looked correct, because every
        # other widget is styled by the sheet and only this one paints itself.
        #
        # Reading the same tokens the sheet is built from means the two cannot
        # drift apart again.
        colours = theme_colours()
        if option.state & QStyle.StateFlag.State_Selected:
            painter.fillRect(option.rect, QColor(colours["accent_soft"]))
            text_colour = QColor(colours["text"])
        else:
            text_colour = QColor(colours["text"])
        faint = QColor(colours["text_faint"])

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
                          QRect(left, y, width, option.rect.bottom() - y), text_colour,
                          max_lines=_max_snippet_lines(self.prefs.density))
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


def _max_snippet_lines(density: str) -> int:
    """Item 1a: two lines in comfortable density; compact keeps one.

    `Density` has no third "generous" value in `view_options.py` today - only
    `COMPACT` and `NORMAL` - so "comfortable" here means "not compact".
    """
    return 1 if density == Density.COMPACT else 2


def _wrap_ranges(metrics: QFontMetrics, text: str, width: int, max_lines: int) -> list[tuple[int, int]]:
    r"""Character ranges for up to `max_lines` word-wrapped lines of `text`.

    Greedy word wrap over the *original* string rather than a list of split
    words, so a highlight's `(start, end)` offsets stay valid against whichever
    line they land in without any re-slicing. The last line reserves room for
    an ellipsis when text remains beyond what `max_lines` can hold - `paint`
    draws the "…" itself once it knows a line was the truncated one.
    """
    if width <= 0 or not text or max_lines <= 0:
        return []
    ranges: list[tuple[int, int]] = []
    pos, n = 0, len(text)
    while pos < n and len(ranges) < max_lines:
        last_line = len(ranges) == max_lines - 1
        budget = width - (metrics.horizontalAdvance("…") if last_line else 0)
        end, last_break = pos, -1
        while end < n and metrics.horizontalAdvance(text[pos:end + 1]) <= budget:
            if text[end] == " ":
                last_break = end
            end += 1
        if end >= n:
            ranges.append((pos, n))
            break
        if last_line:
            ranges.append((pos, end))
            break
        if last_break > pos:
            ranges.append((pos, last_break))
            pos = last_break + 1
        else:
            # A single word wider than the row - forced to break mid-word
            # rather than looping forever with no progress.
            ranges.append((pos, max(end, pos + 1)))
            pos = max(end, pos + 1)
    return ranges


def _snippet_height(font: QFont, text: str, width: int, *, max_lines: int = 1) -> int:
    r"""How tall the snippet actually draws. Used by `sizeHint` only.

    **Wraps up to `max_lines`, because `_draw_snippet` wraps up to
    `max_lines`.** This used to reserve exactly one line while claiming to
    reserve two - `_draw_snippet` had always laid the snippet out on a single
    baseline, eliding at the right edge rather than wrapping - and every row
    whose snippet was longer than the pane carried an empty line under it: a
    gap that looks like a spacing bug, costing about a result per screenful.
    The two must agree, and the cheapest way to make them agree is for the
    hint to describe what the paint does rather than what it might have done.
    """
    if not text or width <= 0:
        return 0
    lines = max(1, len(_wrap_ranges(QFontMetrics(font), text, width, max_lines)))
    return QFontMetrics(font).height() * lines


def _draw_run(painter: QPainter, text: str, line_start: int, line_end: int,
             highlights: list, x: int, y: int, limit: int,
             base: QFont, bold: QFont, plain: QPen, matched: QPen) -> int:
    """One wrapped line's worth of runs, alternating plain/bold at `highlights`.

    Runs of bold rather than rich text: a `QTextDocument` per row would put
    back most of the cost this delegate exists to remove, and the highlight
    ranges are already computed by `presenter.build_snippet`.
    """
    cursor = line_start
    bounds = [(max(s, line_start), min(e, line_end))
             for s, e in highlights if e > line_start and s < line_end]
    for start, end in [*bounds, (line_end, line_end)]:
        for piece, font, pen in (
            (text[cursor:start], base, plain),
            (text[start:end], bold, matched),
        ):
            if not piece or x >= limit:
                continue
            painter.setFont(font)
            painter.setPen(pen)
            advance = QFontMetrics(font).horizontalAdvance(piece)
            if x + advance > limit:
                piece = QFontMetrics(font).elidedText(
                    piece, Qt.TextElideMode.ElideRight, limit - x)
                advance = QFontMetrics(font).horizontalAdvance(piece)
            painter.drawText(x, y, piece)
            x += advance
        cursor = end
    return x


def _draw_snippet(painter: QPainter, snippet: Any, rect: QRect, colour: QColor,
                  *, max_lines: int = 1) -> None:
    """Draw the passage with its matched terms in bold, wrapped to `max_lines`.

    **Colour as well as weight.** `#resultSnippet b` in the stylesheet was
    meant to do this and never could: the snippet is painted here, and no
    stylesheet rule reaches a QPainter. So a match was signalled by boldness
    alone - the one cue that is invisible to somebody scanning quickly, and
    the first thing lost at small text sizes.
    """
    if snippet is None or not getattr(snippet, "text", ""):
        return
    text = snippet.text
    highlights = sorted(getattr(snippet, "highlights", ()) or ())

    plain = QPen(colour)
    matched = QPen(QColor(theme_colours()["highlight"]))
    painter.setPen(plain)

    base = QFont(painter.font())
    bold = QFont(base)
    bold.setBold(True)
    metrics = QFontMetrics(base)

    ranges = _wrap_ranges(metrics, text, rect.width(), max_lines)
    truncated = bool(ranges) and ranges[-1][1] < len(text)
    for index, (start, end) in enumerate(ranges):
        y = rect.top() + index * metrics.height() + metrics.ascent()
        x = _draw_run(painter, text, start, end, highlights, rect.left(), y,
                     rect.right(), base, bold, plain, matched)
        if index == len(ranges) - 1 and truncated and x < rect.right():
            painter.setFont(base)
            painter.setPen(plain)
            painter.drawText(x, y, "…")
    painter.setFont(base)
    painter.setPen(plain)
