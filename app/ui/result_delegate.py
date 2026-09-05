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
from PyQt6.QtWidgets import QStyle, QStyledItemDelegate, QStyleOptionViewItem

from app.ui.presenter import ResultGroup, Terminator, group_subtitle, is_code_kind, why
from app.ui.theme import theme_colours
from app.ui.view_options import Density, Metrics, ViewPreferences

__all__ = ["ResultDelegate", "ROLE_PAYLOAD", "ROLE_EXPANDED"]

#: The row's `ResultGroup` or `ResultRow`.
ROLE_PAYLOAD = int(Qt.ItemDataRole.UserRole)
#: True when this group is currently showing its chunks.
ROLE_EXPANDED = int(Qt.ItemDataRole.UserRole) + 1

#: Side, in pixels, of the file-type icon painted where the `[PDF]` text tag
#: used to sit. Item 3a.
ICON_SIZE = 16

#: One `QIcon` per `kind`, ever. Item 3a: `QFileIconProvider` asks the shell
#: for a type icon per call, and doing that once per repaint of fifty rows of
#: the same kind is the cost this cache exists to remove - the same reasoning
#: as painting instead of building a widget per row, one layer down.
_ICON_CACHE: dict[str, Any] = {}
_ICON_PROVIDER: Any = None


def _icon_for(kind: str) -> Any:
    """A cached file-type `QIcon` for `kind`. Never raises: an icon that
    cannot be produced falls back to the provider's generic file icon rather
    than leaving the row with nothing painted at all.

    The lookup is by **suffix**, not by an existing file - `QFileIconProvider`
    asks the Windows shell for the icon a `.pdf` (or `.eml`, for mail) is
    *associated with*, which is the same icon Explorer shows for any file of
    that type and needs no file to exist on disk. `kind_tag` still supplies
    the word ("PDF", "MAIL") for the tooltip and accessible text - this only
    replaces what used to be a bracketed text tag in the painted row.
    """
    global _ICON_PROVIDER
    if kind not in _ICON_CACHE:
        try:
            if _ICON_PROVIDER is None:
                from PyQt6.QtWidgets import QFileIconProvider
                _ICON_PROVIDER = QFileIconProvider()
            from PyQt6.QtCore import QFileInfo
            # A message has no real file extension; "eml" is the nearest
            # real one and the shell knows it, so mail gets an actual
            # envelope-style icon rather than the generic-file fallback.
            suffix = "eml" if kind == "email" else (kind or "")
            icon = _ICON_PROVIDER.icon(QFileInfo(f"x.{suffix}" if suffix else "x"))
            if icon is None or icon.isNull():
                from PyQt6.QtWidgets import QFileIconProvider as _P
                icon = _ICON_PROVIDER.icon(_P.IconType.File)
            _ICON_CACHE[kind] = icon
        except Exception:                        # noqa: BLE001 - never blocks a paint
            from PyQt6.QtGui import QIcon
            _ICON_CACHE[kind] = QIcon()
    return _ICON_CACHE[kind]


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

        if isinstance(payload, Terminator):
            # Item 5c: one plain line, not the group/chunk geometry below.
            return QSize(width, QFontMetrics(meta_font).height() + 2 * metrics.pad_y)

        rows = [QFontMetrics(meta_font).height()]
        if isinstance(payload, ResultGroup):
            rows.insert(0, QFontMetrics(name_font).height())
        if self._shows_snippet(payload):
            text = _snippet_text(payload)
            snippet_font = _snippet_font(body_font, _payload_kind(payload))
            rows.append(_snippet_height(snippet_font, text, width - 2 * metrics.pad_x,
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
        if isinstance(payload, Terminator):
            self._paint_terminator(painter, payload, option)
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
        elif option.state & QStyle.StateFlag.State_MouseOver:
            # **Item 5a.** `surface_hover` already exists for the stylesheet's
            # own `QListView::item:hover` rule, and that rule can never reach
            # a row this delegate paints itself - the same reason selection
            # is filled here rather than left to the palette, a few lines up.
            painter.fillRect(option.rect, QColor(colours["surface_hover"]))
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
            painter.setFont(_snippet_font(body_font, _payload_kind(payload)))
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

        # **Item 3a: a real file icon where the `[PDF]` text tag used to
        # sit.** Recognised faster than read - the kind word itself moved to
        # the tooltip and accessible text (`kind_tag`, still used there),
        # which is where it stays useful to someone who cannot see the icon.
        text_left = left
        if group.kind:
            icon = _icon_for(group.kind)
            if not icon.isNull():
                size = min(ICON_SIZE, name_metrics.height())
                icon.paint(painter, left, y + (name_metrics.height() - size) // 2,
                          size, size)
                text_left = left + size + 6

        painter.setFont(name_font)
        painter.setPen(QPen(colour))
        name_width = width - date_width - (text_left - left)
        name = name_metrics.elidedText(
            group.name, Qt.TextElideMode.ElideMiddle, name_width)
        painter.drawText(QRect(text_left, y, name_width, name_metrics.height()),
                         int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                         name)
        y += name_metrics.height() + metrics.gap

        painter.setFont(meta_font)
        painter.setPen(QPen(faint))
        meta_metrics = QFontMetrics(meta_font)
        subtitle = group_subtitle(group, show_scores=self.prefs.show_scores, expanded=expanded)
        # **Item 4c**: two results sharing a display name get the segment
        # that tells them apart bolded, in the same slot the folder always
        # occupies - `group_subtitle` puts `folder` first, so the emphasis
        # range `_distinguish_twins` computed against `folder` alone still
        # lands correctly against the assembled subtitle. `(0, 0)` - no twin
        # in this result set - takes the exact path painted here before.
        start, end = getattr(group, "folder_emphasis", (0, 0))
        if end > start:
            bold_meta = QFont(meta_font)
            bold_meta.setBold(True)
            plain_pen = QPen(faint)
            _draw_run(painter, subtitle, 0, len(subtitle), [(start, end)],
                     left, y + meta_metrics.ascent(), left + width,
                     meta_font, bold_meta, plain_pen, plain_pen)
        else:
            painter.drawText(QRect(left, y, width, meta_metrics.height()),
                             int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                             meta_metrics.elidedText(subtitle, Qt.TextElideMode.ElideRight, width))
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

    def _paint_terminator(self, painter: QPainter, payload: Terminator, option: Any) -> None:
        """Item 5c: "That's all — 23 results.", centred and faint."""
        painter.save()
        _, meta_font, _ = self._fonts(option.font)
        painter.setFont(meta_font)
        painter.setPen(QPen(QColor(theme_colours()["text_faint"])))
        painter.drawText(option.rect, int(Qt.AlignmentFlag.AlignCenter), payload.text)
        painter.restore()

    def subtitle_rect(self, option: Any, payload: Any) -> Optional[QRect]:
        """Item 2a: where the "N matches ▸/▾" line paints, for a multi-match
        group - the chevron's own click target.

        `None` for anything that is not an expandable group, so a caller
        hit-testing a click falls through to the ordinary whole-row toggle
        (double-click/Enter, unchanged) rather than claiming the click.

        Mirrors the y-arithmetic `_paint_group` already does for the name
        line, rather than a second copy of it: both read `Metrics.for_density`
        and `_fonts` off the same `option.font`/`self.prefs`, so a click and a
        paint can never disagree about where this line sits.
        """
        if not isinstance(payload, ResultGroup) or payload.match_count <= 1:
            return None
        metrics = Metrics.for_density(self.prefs.density)
        name_font, meta_font, _ = self._fonts(option.font)
        top = option.rect.top() + metrics.pad_y + QFontMetrics(name_font).height() + metrics.gap
        left = option.rect.left() + metrics.pad_x
        width = option.rect.width() - 2 * metrics.pad_x
        return QRect(left, top, width, QFontMetrics(meta_font).height())

    def chevron_hit(self, view: Any, pos: Any) -> Optional[int]:
        """Item 2a: the `file_id` to toggle if `pos` (viewport coordinates)
        landed on a multi-match group's chevron line, else `None`.

        The click arithmetic lives here rather than in `results_view.py`:
        `subtitle_rect` already owns the one true copy of this geometry, and
        a second copy in the view would be exactly the drift that file's own
        line-count guard exists to catch.
        """
        index = view.indexAt(pos)
        if not index.isValid():
            return None
        payload = index.data(ROLE_PAYLOAD)
        option = QStyleOptionViewItem()
        view.initViewItemOption(option)
        option.rect = view.visualRect(index)
        rect = self.subtitle_rect(option, payload)
        return payload.file_id if rect is not None and rect.contains(pos) else None


# ---------------------------------------------------------------------------
# Geometry helpers. The *text* decisions live in `presenter.py`, Qt-free, so
# they can be checked without a display - see `group_subtitle` and `kind_tag`.
# ---------------------------------------------------------------------------

def _snippet_payload(payload: Any) -> Any:
    if isinstance(payload, ResultGroup):
        return payload.best.snippet if payload.best else None
    return getattr(payload, "snippet", None)


def _payload_kind(payload: Any) -> str:
    """`ResultGroup.kind`, or a chunk row's own `ext` when there is no group."""
    return getattr(payload, "kind", None) or getattr(payload, "ext", None) or ""


def _snippet_font(base: QFont, kind: str) -> QFont:
    """Item 3c: code rows paint in monospace, at the same size as `base`.

    No line number - see `presenter.is_code_kind`'s docstring for why.
    """
    if not is_code_kind(kind):
        return base
    from PyQt6.QtGui import QFontDatabase
    mono = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
    if base.pointSize() > 0:
        mono.setPointSize(base.pointSize())
    elif base.pixelSize() > 0:
        mono.setPixelSize(base.pixelSize())
    return mono


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
