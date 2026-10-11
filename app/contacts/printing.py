"""Printing: address labels, envelopes and an address book.

Layer: L7

Order 1k 6q. Pure functions that write a PDF with reportlab (see `_pdf` for why
this is not `report_pdf.py`'s Qt path) and return a `PdfLayout`: where every label
or entry went, in millimetres from the page's top-left corner. The Contacts page
shows that layout as its preview before anything is printed, and the tests check
a known sheet by arithmetic rather than by pixels.

**Label sheets.** Sizes are the manufacturers' published templates (Avery L-series
for A4, Avery 5160-family for US Letter), written down from those templates and
checked here only for being self-consistent (every label inside its page, none
overlapping). *UNVERIFIED against a physical sheet*: print one test page on plain
paper and hold it against a real sheet to the light before wasting labels.
`start_position` is the first label to use, for a part-used sheet.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from typing import Iterable, Optional, Union

from app.contacts import _pdf as P
from app.contacts.card import Card, Field, display_name
from app.contacts._pdf import NothingToPrint, PdfLayout, Placement

__all__ = ["LabelSheet", "LABEL_SHEETS", "ENVELOPE_SIZES", "position_rect", "choose_address", "label_lines",
           "labels_pdf", "envelope_pdf", "address_book_pdf", "NothingToPrint"]


@dataclass(frozen=True)
class LabelSheet:
    code: str
    name: str
    paper: str                    # "A4" | "Letter"
    page_w_mm: float
    page_h_mm: float
    cols: int
    rows: int
    label_w_mm: float
    label_h_mm: float
    left_mm: float                # page edge to the first label's left edge
    top_mm: float                 # page edge to the first label's top edge
    pitch_x_mm: float             # left edge to the next label's left edge
    pitch_y_mm: float

    @property
    def per_page(self) -> int:
        return self.cols * self.rows


_A4 = (210.0, 297.0)
_LT = (215.9, 279.4)


def _sheet(code, name, paper, cols, rows, w, h, left, top, px, py) -> LabelSheet:
    page = _A4 if paper == "A4" else _LT
    return LabelSheet(code, name, paper, page[0], page[1], cols, rows, w, h, left, top, px, py)


LABEL_SHEETS: dict[str, LabelSheet] = {s.code: s for s in [
    _sheet("L7163", "Avery L7163 - 14 per A4 sheet (99.1 x 38.1 mm)", "A4", 2, 7, 99.1, 38.1, 4.7, 15.1, 101.6, 38.1),
    _sheet("L7160", "Avery L7160 - 21 per A4 sheet (63.5 x 38.1 mm)", "A4", 3, 7, 63.5, 38.1, 7.2, 15.1, 66.0, 38.1),
    _sheet("L7161", "Avery L7161 - 18 per A4 sheet (63.5 x 46.6 mm)", "A4", 3, 6, 63.5, 46.6, 7.2, 8.8, 66.0, 46.6),
    _sheet("L7162", "Avery L7162 - 16 per A4 sheet (99.1 x 33.9 mm)", "A4", 2, 8, 99.1, 33.9, 4.7, 12.9, 101.6, 33.9),
    _sheet("L7159", "Avery L7159 - 24 per A4 sheet (63.5 x 33.9 mm)", "A4", 3, 8, 63.5, 33.9, 7.2, 12.9, 66.0, 33.9),
    _sheet("L7164", "Avery L7164 - 12 per A4 sheet (63.5 x 72 mm)", "A4", 3, 4, 63.5, 72.0, 7.2, 4.5, 66.0, 72.0),
    _sheet("L7165", "Avery L7165 - 8 per A4 sheet (99.1 x 67.7 mm)", "A4", 2, 4, 99.1, 67.7, 4.7, 13.1, 101.6, 67.7),
    _sheet("L7173", "Avery L7173 - 10 per A4 sheet (99.1 x 57 mm)", "A4", 2, 5, 99.1, 57.0, 4.7, 6.0, 101.6, 57.0),
    _sheet("5160", "Avery 5160 - 30 per Letter sheet (2-5/8 x 1 in)", "Letter", 3, 10, 66.675, 25.4, 4.7625, 12.7,
           69.85, 25.4),
    _sheet("5161", "Avery 5161 - 20 per Letter sheet (4 x 1 in)", "Letter", 2, 10, 101.6, 25.4, 3.97, 12.7,
           106.36, 25.4),
    _sheet("5162", "Avery 5162 - 14 per Letter sheet (4 x 1-1/3 in)", "Letter", 2, 7, 101.6, 33.87, 3.97, 21.17,
           106.36, 33.87),
    _sheet("5163", "Avery 5163 - 10 per Letter sheet (4 x 2 in)", "Letter", 2, 5, 101.6, 50.8, 3.97, 12.7,
           106.36, 50.8),
    _sheet("5164", "Avery 5164 - 6 per Letter sheet (4 x 3-1/3 in)", "Letter", 2, 3, 101.6, 84.67, 3.97, 12.7,
           106.36, 84.67),
]}

#: Envelope sizes in mm (landscape), name -> (width, height).
ENVELOPE_SIZES = {"DL": (220.0, 110.0), "C5": (229.0, 162.0), "C4": (324.0, 229.0), "No10": (241.3, 104.8)}

_NOTHING = ("None of these contacts has {what}. Pick a different address type, or add an address to the contacts "
            "first.")


def position_rect(sheet: LabelSheet, position: int) -> tuple[float, float, float, float]:
    """(x, y, w, h) in mm of label number `position` (1 = top-left, left to right, then down)."""
    if not 1 <= position <= sheet.per_page:
        raise ValueError(f"Label {position} is not on this sheet: it has {sheet.per_page} labels.")
    row, col = divmod(position - 1, sheet.cols)
    return (sheet.left_mm + col * sheet.pitch_x_mm, sheet.top_mm + row * sheet.pitch_y_mm,
            sheet.label_w_mm, sheet.label_h_mm)


# --------------------------------------------------------------------------
# Choosing what to print
# --------------------------------------------------------------------------

def choose_address(card: Card, label: Optional[str] = "") -> Optional[Field]:
    """The address to print: the one labelled `label` (any letter case), or when
    `label` is empty the main address, else the first. None when there is none."""
    addresses = [f for f in card.values("address") if f.value.strip() or any(f.data.values())]
    want = (label or "").strip().lower()
    if want:
        for f in addresses:
            if want == f.label.strip().lower() or want in f.label.lower().split():
                return f
        return None
    for f in addresses:
        if f.primary:
            return f
    return addresses[0] if addresses else None


def _name_line(card: Card) -> str:
    return display_name(card) or card.get("company")


def label_lines(card: Card, address_label: Optional[str] = "", height_mm: float = 38.1) -> list[str]:
    """The text of one address label: name, company when it is a work address, then
    the address. Short labels (25.4 mm and under) put town, region and postcode on one line."""
    f = choose_address(card, address_label)
    lines = [_name_line(card)]
    if f is None:
        return lines
    company = card.get("company")
    if company and company != lines[0] and "work" in f.label.lower():
        lines.append(company)
    lines += P.address_lines(f, compact=height_mm <= 30)
    return [ln for ln in lines if ln]


def _fit_block(c, lines: list[str], width_pt: float, height_pt: float, start: float, floor: float = 6.0):
    """Largest font size (down to `floor`) at which the lines fit the box."""
    from reportlab.pdfbase.pdfmetrics import stringWidth  # noqa: PLC0415
    regular, bold, _ = P.fonts()
    size = start
    while size > floor:
        need_h = len(lines) * size * 1.2
        widest = max(stringWidth(P.safe(t), bold if i == 0 else regular, size) for i, t in enumerate(lines))
        if need_h <= height_pt and widest <= width_pt:
            return size
        size -= 0.5
    return floor


def _draw_block(c, lines: list[str], x_mm: float, y_mm: float, w_mm: float, h_mm: float, page_h_mm: float,
                start: float = 11.0) -> None:
    regular, bold, _ = P.fonts()
    size = _fit_block(c, lines, P.mm(w_mm), P.mm(h_mm), start)
    y = y_mm
    for i, text in enumerate(lines):
        font = bold if i == 0 else regular
        c.setFont(font, size)
        baseline = page_h_mm - y - size * 1.2 / P.MM * 0.8
        c.drawString(P.mm(x_mm), P.mm(baseline), P.fit_text(text, font, size, P.mm(w_mm)))
        y += size * 1.2 / P.MM


# --------------------------------------------------------------------------
# Labels
# --------------------------------------------------------------------------

def labels_pdf(cards: Iterable[Card], sheet: Union[str, LabelSheet], address_label: Optional[str], path,
               start_position: int = 1) -> PdfLayout:
    """Address labels on `sheet` (a code from `LABEL_SHEETS` or a `LabelSheet`), one per
    contact that has the chosen address. Contacts without it are left out and their
    positions listed in `layout.skipped` so the page can say who. `start_position`
    skips the labels already used on the first sheet."""
    cards = list(cards)
    if isinstance(sheet, str):
        if sheet not in LABEL_SHEETS:
            raise ValueError(f"The label sheet {sheet!r} is not known. Choose one of: {', '.join(LABEL_SHEETS)}.")
        sheet = LABEL_SHEETS[sheet]
    if not 1 <= start_position <= sheet.per_page:
        raise ValueError(f"Start position {start_position} is not on this sheet: it has {sheet.per_page} labels.")
    printable = [(i, card) for i, card in enumerate(cards) if choose_address(card, address_label) is not None]
    skipped = [i for i in range(len(cards)) if i not in {k for k, _ in printable}]
    if not printable:
        what = f"a {address_label} address" if address_label else "an address"
        raise NothingToPrint(_NOTHING.format(what=what))
    size = (sheet.page_w_mm, sheet.page_h_mm)
    first_slot = start_position - 1
    pages = (first_slot + len(printable) + sheet.per_page - 1) // sheet.per_page
    lay = PdfLayout(str(path), size, pages, skipped=skipped)
    c = P.new_canvas(path, size, "Address labels")
    pad = 3.0
    for k, (i, card) in enumerate(printable):
        page, slot = divmod(first_slot + k, sheet.per_page)
        if slot == 0 and k:
            c.showPage()
        x, y, w, h = position_rect(sheet, slot + 1)
        _draw_block(c, label_lines(card, address_label, h), x + pad, y + pad, w - 2 * pad, h - 2 * pad, size[1])
        lay.placements.append(Placement(page + 1, i, x, y, w, h, "label", slot + 1))
    c.save()
    return lay


# --------------------------------------------------------------------------
# Envelopes
# --------------------------------------------------------------------------

def envelope_pdf(sender_card: Optional[Card], recipient_cards: Iterable[Card], size: str, path) -> PdfLayout:
    """One envelope per recipient that has an address (the main one, else the first),
    the sender's return address top-left, the recipient's in the lower middle. The page
    is the envelope itself, landscape, so it prints from a feeder set to that size."""
    if size not in ENVELOPE_SIZES:
        raise ValueError(f"The envelope size {size!r} is not known. Choose one of: {', '.join(ENVELOPE_SIZES)}.")
    recipients = list(recipient_cards)
    W, H = ENVELOPE_SIZES[size]
    printable = [(i, card) for i, card in enumerate(recipients) if choose_address(card, "") is not None]
    skipped = [i for i in range(len(recipients)) if i not in {k for k, _ in printable}]
    if not printable:
        raise NothingToPrint(_NOTHING.format(what="an address"))
    sender_lines = label_lines(sender_card, "", 99.0) if sender_card is not None else []
    if sender_card is not None and choose_address(sender_card, "") is None:
        sender_lines = []                                        # a name alone is not a return address
    lay = PdfLayout(str(path), (W, H), len(printable), skipped=skipped)
    c = P.new_canvas(path, (W, H), "Envelopes")
    s_x, s_y, s_w = 10.0, 10.0, min(80.0, W * 0.4)
    s_h = max(len(sender_lines), 1) * 4.6
    r_x, r_y = W * 0.45, H * 0.45
    r_w, r_h = min(W * 0.5, 110.0), min(H * 0.4, 42.0)
    start = {"DL": 12.0, "C5": 13.0, "C4": 14.0, "No10": 12.0}[size]
    for n, (i, card) in enumerate(printable, 1):
        if n > 1:
            c.showPage()
        if sender_lines:
            _draw_block(c, sender_lines, s_x, s_y, s_w, s_h, H, start=9.0)
            lay.placements.append(Placement(n, -1, s_x, s_y, s_w, s_h, "sender"))
        _draw_block(c, label_lines(card, "", 99.0), r_x, r_y, r_w, r_h, H, start=start)
        lay.placements.append(Placement(n, i, r_x, r_y, r_w, r_h, "recipient"))
    c.save()
    return lay


# --------------------------------------------------------------------------
# Address book
# --------------------------------------------------------------------------

def _fold(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "")
    return "".join(ch for ch in text if not unicodedata.combining(ch)).casefold().strip()


def _sort_key(card: Card) -> tuple:
    primary = card.get("family") or card.get("company") or display_name(card)
    key = _fold(primary)
    letter = key[:1].upper() if key[:1].isalpha() and key[:1].isascii() else "#"
    return (letter == "#", letter, key, _fold(card.get("given")))


def _book_name(card: Card) -> str:
    family, given = card.get("family"), card.get("given")
    if family and given:
        return f"{family}, {given}"
    return display_name(card) or "No name"


def _book_lines(card: Card) -> list[str]:
    lines: list[str] = []
    sub = ", ".join(x for x in (card.get("title"), card.get("company")) if x)
    if sub:
        lines.append(sub)
    for kind in ("phone", "email"):
        for f in card.values(kind):
            if f.value.strip():
                lines.append(f"{f.label.title() + ': ' if f.label else ''}{f.value.strip()}")
    for f in card.values("address"):
        text = ", ".join(P.address_lines(f, compact=True)) or f.value
        if text.strip():
            lines.append(f"{f.label.title() + ': ' if f.label else ''}{text}")
    return lines[:14]


def address_book_pdf(cards: Iterable[Card], path, with_photos: bool = True) -> PdfLayout:
    """The whole book, A to Z, in two columns on A4 with a letter heading before each
    group (accents fold to their letter; anything else goes under "#", last). A
    heading is never left at the foot of a column."""
    cards = list(cards)
    if not cards:
        raise NothingToPrint("There are no contacts to print. Choose some contacts, or clear the filter, and try again.")
    regular, bold, _ = P.fonts()
    W, H = 210.0, 297.0
    margin, gap = 15.0, 10.0
    col_w = (W - 2 * margin - gap) / 2
    bottom = H - 15.0
    line_h, head_h, pad = 3.9, 10.0, 3.0
    photo_mm = 12.0
    order = sorted(range(len(cards)), key=lambda i: _sort_key(cards[i]))

    blocks: list[tuple] = []                      # ("heading", letter) | ("entry", index, lines, height)
    last = None
    for i in order:
        letter = _sort_key(cards[i])[1]
        if letter != last:
            blocks.append(("heading", letter))
            last = letter
        lines = _book_lines(cards[i])
        height = max(line_h * (1 + len(lines)) + pad, photo_mm + pad if with_photos else 0)
        blocks.append(("entry", i, lines, height))

    lay = PdfLayout(str(path), (W, H), 1)
    c = P.new_canvas(path, (W, H), "Address book")
    page, col, y = 1, 0, margin + 12.0
    top_first = margin + 12.0

    def title_page() -> None:
        c.setFont(bold, 14)
        c.drawString(P.mm(margin), P.mm(H - margin - 5), "Address book")
        c.setFont(regular, 8)
        c.drawRightString(P.mm(W - margin), P.mm(H - margin - 5), f"{len(cards)} contacts")

    title_page()

    def advance() -> None:
        nonlocal page, col, y
        if col == 0:
            col, y = 1, (top_first if page == 1 else margin)
        else:
            c.showPage()
            page, col, y = page + 1, 0, margin

    k = 0
    while k < len(blocks):
        b = blocks[k]
        need = head_h + blocks[k + 1][3] if b[0] == "heading" else b[3]          # a heading travels with its first entry
        if y + need > bottom and y > (top_first if page == 1 else margin) + 0.01:
            advance()
            continue
        x = margin + col * (col_w + gap)
        if b[0] == "heading":
            c.setFont(bold, 13)
            c.drawString(P.mm(x), P.mm(H - y - 6.5), b[1])
            c.setStrokeGray(0.6)
            c.setLineWidth(0.5)
            c.line(P.mm(x), P.mm(H - y - 8.0), P.mm(x + col_w), P.mm(H - y - 8.0))
            lay.placements.append(Placement(page, -1, x, y, col_w, head_h, "heading", text=b[1]))
            y += head_h
        else:
            _i, lines, height = b[1], b[2], b[3]
            tx = x + (photo_mm + 3 if with_photos else 0)
            if with_photos:
                P.draw_photo_or_initials(c, cards[_i], P.mm(x), P.mm(H - y - photo_mm - 0.5), P.mm(photo_mm), lay.warnings)
            c.setFont(bold, 9.5)
            c.drawString(P.mm(tx), P.mm(H - y - 3.3), P.fit_text(_book_name(cards[_i]), bold, 9.5, P.mm(col_w - (tx - x))))
            c.setFont(regular, 8)
            for n, text in enumerate(lines, 1):
                c.drawString(P.mm(tx), P.mm(H - y - 3.3 - n * line_h), P.fit_text(text, regular, 8, P.mm(col_w - (tx - x))))
            lay.placements.append(Placement(page, _i, x, y, col_w, height, "entry"))
            y += height
        k += 1
    c.save()
    lay.pages = page
    return lay
