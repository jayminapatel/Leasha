"""How a contact is drawn: the cells of the List and the tiles of the Cards.

Layer: L5

Order 1k 6b, 6m, 6s, 6t. Both views are virtual - Qt asks for a cell or a tile only when it is
on screen - and everything here is painting from data already in memory: no query, no file
read (a picture is the `PhotoCache`'s, and until it arrives the circle shows initials). The
account colours and the tag colours are data (the person's own choices) and are drawn as given;
every other colour is a theme token, handed in by `retint`.
"""

from __future__ import annotations

from typing import Any, Optional

from PySide6.QtCore import QModelIndex, QPointF, QRect, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QStyle, QStyledItemDelegate, QStyleOptionViewItem

from app.ui.contacts.logic import COLUMNS, initials
from app.ui.contacts.model import RowRole
from app.ui.theme import theme_colours
from app.ui.widgets.icons import icon

__all__ = ["NameDelegate", "ChipsDelegate", "CardDelegate", "CARD_SIZE", "draw_avatar", "name_font", "NAME_POINTS"]

CARD_SIZE = QSize(304, 140)
GLYPH = 14


class _Base(QStyledItemDelegate):
    def __init__(self, photos: Any = None, parent: Optional[Any] = None) -> None:
        super().__init__(parent)
        self.photos = photos
        self.c: dict[str, str] = {}
        self._icons: dict[tuple[str, str], Any] = {}

    def retint(self, colours: dict[str, str]) -> None:
        self.c = dict(colours)
        self._icons.clear()

    def tok(self, name: str) -> QColor:
        # Before the first `retint` the theme's own value, not a literal grey.
        return QColor(self.c.get(name) or theme_colours()[name])

    def glyph(self, name: str, colour: str) -> Any:
        key = (name, colour)
        found = self._icons.get(key)
        if found is None:
            found = self._icons[key] = icon(name, colour)
        return found

    def paint_glyph(self, painter: QPainter, name: str, colour: str, x: int, y: int, side: int = GLYPH) -> None:
        self.glyph(name, colour).paint(painter, QRect(x, y, side, side))


#: The name on a card: the application's own font, bold, 12 points at the default text size
#: (2026-10-11, the owner: "too big ... same as the main config, bold and size 12").
NAME_POINTS = 12.0
_DEFAULT_BASE_POINTS = 9.0


def name_font(base: QFont) -> QFont:
    """The card name's font: `base`'s family, bold, 12 points - and scaled with the person's
    "Make text bigger" setting (a base larger than the default makes it larger in proportion)."""
    font = QFont(base)
    font.setBold(True)
    points = base.pointSizeF()
    font.setPointSizeF(NAME_POINTS * (points if points > 0 else _DEFAULT_BASE_POINTS) / _DEFAULT_BASE_POINTS)
    return font


def draw_avatar(painter: QPainter, rect: QRect, row: Any, photos: Any, c: dict[str, str]) -> None:
    """A circle: the contact's picture when it is here, else their initials."""
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    path = QPainterPath()
    path.addEllipse(QRectF(rect))
    pix = photos.pixmap(row.id) if (photos is not None and row.has_photo) else None
    if pix is not None:
        painter.setClipPath(path)
        side = max(rect.width(), rect.height())
        scaled = pix.scaled(side, side, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                            Qt.TransformationMode.SmoothTransformation)
        painter.drawPixmap(rect.x() - (scaled.width() - rect.width()) // 2,
                           rect.y() - (scaled.height() - rect.height()) // 2, scaled)
    else:
        painter.fillPath(path, QColor(c.get("accent_soft", "#dddddd")))
        font = QFont(painter.font())
        font.setBold(True)
        font.setPixelSize(max(9, int(rect.height() * 0.38)))
        painter.setFont(font)
        painter.setPen(QColor(c.get("accent_text", "#444444")))
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, initials(row.display_name))
    painter.restore()


def draw_chips(painter: QPainter, rect: QRect, chips: list[tuple[str, str, str]], d: _Base) -> None:
    """Rounded chips left to right inside `rect`; what does not fit becomes "+n"."""
    fm = QFontMetrics(painter.font())
    h = min(rect.height() - 4, fm.height() + 6)
    y = rect.y() + (rect.height() - h) // 2
    x = rect.x()
    text_colour = d.tok("text")
    for i, (words, colour, glyph) in enumerate(chips):
        base = QColor(colour) if colour else d.tok("text_faint")
        gap = GLYPH + 4 if glyph else 0
        w = fm.horizontalAdvance(words) + 14 + gap
        left = len(chips) - i - 1
        if x + w > rect.right() - (fm.horizontalAdvance(f"+{left}") + 8 if left else 0) and i:
            painter.setPen(d.tok("text_faint"))
            painter.drawText(QRect(x, rect.y(), rect.right() - x, rect.height()),
                             Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, f"+{len(chips) - i}")
            return
        w = min(w, rect.right() - x)
        pill = QRectF(x, y, w, h)
        fill = QColor(base)
        fill.setAlpha(46)
        painter.setPen(QPen(base, 1))
        painter.setBrush(fill)
        painter.drawRoundedRect(pill, h / 2, h / 2)
        tx = x + 7
        if glyph:
            d.paint_glyph(painter, glyph, base.name(), tx, y + (h - GLYPH) // 2)
            tx += GLYPH + 4
        painter.setPen(text_colour)
        painter.drawText(QRect(tx, y, int(x + w - tx - 6), h), Qt.AlignmentFlag.AlignVCenter,
                         fm.elidedText(words, Qt.TextElideMode.ElideRight, int(x + w - tx - 6)))
        x += w + 4
        if x >= rect.right():
            return


class NameDelegate(_Base):
    """Column 0: the avatar, the name, and a star for a favourite."""

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        row = index.data(RowRole)
        self.initStyleOption(option, index)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        if row is None:
            return
        painter.save()
        if selected:
            painter.fillRect(option.rect, self.tok("accent_soft"))
        elif option.state & QStyle.StateFlag.State_MouseOver:
            painter.fillRect(option.rect, self.tok("surface_hover"))
        r = option.rect
        side = min(26, r.height() - 6)
        draw_avatar(painter, QRect(r.x() + 6, r.y() + (r.height() - side) // 2, side, side), row, self.photos, self.c)
        text_left = r.x() + 6 + side + 8
        star = 0
        if row.favourite:
            star = GLYPH + 6
            self.paint_glyph(painter, "star", self.c.get("warning", "#b06000"), r.right() - GLYPH - 4,
                             r.y() + (r.height() - GLYPH) // 2)
        painter.setPen(self.tok("text"))
        fm = option.fontMetrics
        painter.drawText(QRect(text_left, r.y(), r.right() - text_left - star, r.height()),
                         Qt.AlignmentFlag.AlignVCenter,
                         fm.elidedText(row.display_name or "(no name)", Qt.TextElideMode.ElideRight,
                                       r.right() - text_left - star))
        painter.restore()

    def sizeHint(self, option: QStyleOptionViewItem, index: QModelIndex) -> QSize:        # noqa: N802
        return QSize(200, 34)


class ChipsDelegate(_Base):
    """The Accounts, Groups and Tags cells: small coloured chips (6m.5, 6s)."""

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        row = index.data(RowRole)
        model = index.model()
        if row is None:
            return
        painter.save()
        if option.state & QStyle.StateFlag.State_Selected:
            painter.fillRect(option.rect, self.tok("accent_soft"))
        elif option.state & QStyle.StateFlag.State_MouseOver:
            painter.fillRect(option.rect, self.tok("surface_hover"))
        kind = COLUMNS[index.column()][0]
        font = QFont(option.font)
        font.setPixelSize(max(9, int(QFontMetrics(font).height() * 0.82)))
        painter.setFont(font)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        draw_chips(painter, option.rect.adjusted(6, 0, -4, 0), model.chips(row, kind), self)
        painter.restore()


class CardDelegate(_Base):
    """The Cards view: a business card with the account colours down its left edge."""

    def sizeHint(self, option: QStyleOptionViewItem, index: QModelIndex) -> QSize:        # noqa: N802
        return CARD_SIZE

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        row = index.data(RowRole)
        model = index.model()
        if row is None:
            return
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        card = QRectF(option.rect.adjusted(6, 6, -6, -6))
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hover = bool(option.state & QStyle.StateFlag.State_MouseOver)
        path = QPainterPath()
        path.addRoundedRect(card, 8, 8)
        painter.fillPath(path, self.tok("accent_soft" if selected else "surface_hover" if hover else "surface"))
        painter.setPen(QPen(self.tok("focus_ring" if selected else "border"), 2 if selected else 1))
        painter.drawPath(path)
        # The account colours down the left edge (6b): equal bands, top to bottom.
        stripes = [chip[1] for chip in model.chips(row, "accounts")]
        painter.save()
        painter.setClipPath(path)
        band = QRectF(card.x(), card.y(), 6, card.height())
        if stripes:
            each = card.height() / len(stripes)
            for i, colour in enumerate(stripes):
                painter.fillRect(QRectF(band.x(), band.y() + i * each, band.width(), each + 1), QColor(colour))
        else:
            painter.fillRect(band, self.tok("border_strong"))
        painter.restore()

        x0 = int(card.x()) + 18
        y0 = int(card.y()) + 12
        draw_avatar(painter, QRect(x0, y0, 52, 52), row, self.photos, self.c)
        tx = x0 + 52 + 12
        width = int(card.right()) - tx - 10
        base = QFont(option.font)
        big = name_font(base)
        painter.setFont(big)
        painter.setPen(self.tok("text"))
        fm_big = QFontMetrics(big)
        marks = (GLYPH + 4 if row.favourite else 0) + (GLYPH + 4 if row.conflict_at else 0)
        painter.drawText(QRect(tx, y0, width - marks, fm_big.height() + 2), Qt.AlignmentFlag.AlignVCenter,
                         fm_big.elidedText(row.display_name or "(no name)", Qt.TextElideMode.ElideRight,
                                           width - marks))
        mx = int(card.right()) - 10 - GLYPH
        if row.favourite:
            self.paint_glyph(painter, "star", self.c.get("warning", "#b06000"), mx, y0 + 2)
            mx -= GLYPH + 4
        if row.conflict_at:
            self.paint_glyph(painter, "triangle-alert", self.c.get("warning", "#b06000"), mx, y0 + 2)
        painter.setFont(base)
        fm = QFontMetrics(base)
        line = fm.height() + 3
        y = y0 + fm_big.height() + 4
        dim = self.c.get("text_dim", "#666666")
        for glyph_name, text in (("briefcase", row.title), ("building-2", row.company)):
            if not text:
                continue
            self.paint_glyph(painter, glyph_name, dim, tx, y + (line - GLYPH) // 2)
            painter.setPen(self.tok("text_dim"))
            painter.drawText(QRect(tx + GLYPH + 5, y, width - GLYPH - 5, line), Qt.AlignmentFlag.AlignVCenter,
                             fm.elidedText(text, Qt.TextElideMode.ElideRight, width - GLYPH - 5))
            y += line
        y = max(y, y0 + 56) + 4
        wide = int(card.right()) - x0 - 10
        for glyph_name, text in (("mail", row.email), ("phone", row.phone)):
            if not text:
                continue
            self.paint_glyph(painter, glyph_name, dim, x0, y + (line - GLYPH) // 2)
            painter.setPen(self.tok("text"))
            painter.drawText(QRect(x0 + GLYPH + 5, y, wide - GLYPH - 5, line), Qt.AlignmentFlag.AlignVCenter,
                             fm.elidedText(text, Qt.TextElideMode.ElideRight, wide - GLYPH - 5))
            y += line
        chips = model.chips(row, "tags")
        if chips:
            small = QFont(base)
            small.setPixelSize(max(9, int(fm.height() * 0.82)))
            painter.setFont(small)
            draw_chips(painter, QRect(x0, int(card.bottom()) - 24, wide, 20), chips, self)
        painter.restore()
