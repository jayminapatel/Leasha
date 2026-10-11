"""What the contact PDFs share: fonts, photos or initials, and the layout record.

Layer: L7

Order 1k 6j, 6q. `app/ui/report_pdf.py` writes a report through Qt's
`QPdfWriter`; the contact PDFs are drawn on a page grid (cards, label sheets,
envelopes) that a text-document layout cannot place to the millimetre, and this
package imports no Qt (rule 8), so they are drawn with reportlab (a wheel, rule
12). Everything is positioned in millimetres from the page's **top-left**, and
every writer returns a `PdfLayout` saying where it put what, so a test checks a
known label sheet by arithmetic.
"""

from __future__ import annotations

import io
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from app.contacts.card import Card, display_name

__all__ = ["Placement", "PdfLayout", "NothingToPrint", "mm", "fonts", "fit_text", "draw_photo_or_initials",
           "initials", "first_value", "address_lines", "page_size_mm"]

MM = 72.0 / 25.4


def mm(value: float) -> float:
    return value * MM


class NothingToPrint(ValueError):
    """Raised when there is no page to write. The message is plain English and says what to do."""


@dataclass(frozen=True)
class Placement:
    """One thing placed on a page. `index` is the position of the card in the
    list given to the writer (-1 for a heading); `position` is a label's number
    on its sheet; `text` carries a heading's letter."""

    page: int
    index: int
    x_mm: float
    y_mm: float
    w_mm: float
    h_mm: float
    kind: str = "item"
    position: int = 0
    text: str = ""


@dataclass
class PdfLayout:
    path: str
    page_size_mm: tuple[float, float]
    pages: int
    placements: list[Placement] = field(default_factory=list)
    skipped: list[int] = field(default_factory=list)          # positions of cards left out, with no address
    warnings: list[str] = field(default_factory=list)


_PAGES = {"A4": (210.0, 297.0), "Letter": (215.9, 279.4)}


def page_size_mm(name: str) -> tuple[float, float]:
    try:
        return _PAGES[name]
    except KeyError:
        raise ValueError(f"Page size {name!r} is not known. Choose A4 or Letter.") from None


# --------------------------------------------------------------------------
# Fonts
# --------------------------------------------------------------------------

_FONT_CANDIDATES = [
    (r"C:\Windows\Fonts\segoeui.ttf", r"C:\Windows\Fonts\segoeuib.ttf"),
    (r"C:\Windows\Fonts\arial.ttf", r"C:\Windows\Fonts\arialbd.ttf"),
    ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    ("/Library/Fonts/Arial.ttf", "/Library/Fonts/Arial Bold.ttf"),
]
_fonts: Optional[tuple[str, str, bool]] = None


def fonts() -> tuple[str, str, bool]:
    """(regular, bold, unicode). A TrueType font from the system when there is
    one, so names in any script print; the built-in Helvetica otherwise (Latin-1
    only - `safe` below replaces what it cannot draw)."""
    global _fonts
    if _fonts is None:
        from reportlab.pdfbase import pdfmetrics  # noqa: PLC0415
        from reportlab.pdfbase.ttfonts import TTFont  # noqa: PLC0415
        _fonts = ("Helvetica", "Helvetica-Bold", False)
        for regular, bold in _FONT_CANDIDATES:
            if os.path.exists(regular) and os.path.exists(bold):
                try:
                    pdfmetrics.registerFont(TTFont("LeashaSans", regular))
                    pdfmetrics.registerFont(TTFont("LeashaSans-Bold", bold))
                except Exception:  # noqa: BLE001 - a damaged system font must not stop a print; try the next
                    continue
                _fonts = ("LeashaSans", "LeashaSans-Bold", True)
                break
    return _fonts


def safe(text: str) -> str:
    """`text` as the chosen font can draw it."""
    if fonts()[2]:
        return text
    return text.encode("cp1252", "replace").decode("cp1252")


def fit_text(text: str, font: str, size: float, width_pt: float) -> str:
    """`text` cut with an ellipsis so it is no wider than `width_pt`."""
    from reportlab.pdfbase.pdfmetrics import stringWidth  # noqa: PLC0415
    text = safe(text)
    if stringWidth(text, font, size) <= width_pt:
        return text
    while text and stringWidth(text + "\u2026", font, size) > width_pt:
        text = text[:-1]
    return (text + "\u2026") if fonts()[2] else (text + "...")


# --------------------------------------------------------------------------
# Card facts
# --------------------------------------------------------------------------

def initials(card: Card) -> str:
    given, family = card.get("given").strip(), card.get("family").strip()
    if given and family:
        return (given[0] + family[0]).upper()
    name = display_name(card).strip()
    return name[:1].upper() if name else "?"


def first_value(card: Card, kind: str) -> str:
    """The main value of a kind: the one marked main, else the first."""
    values = [f for f in card.values(kind) if f.value.strip()]
    for f in values:
        if f.primary:
            return f.value.strip()
    return values[0].value.strip() if values else ""


def address_lines(f, compact: bool = False) -> list[str]:
    """An address field as printed lines. `compact` joins town, region and
    postcode on one line, for short labels."""
    d = f.data or {}
    if not d:
        return [ln.strip() for ln in f.value.replace(",", "\n").splitlines() if ln.strip()] if f.value else []
    lines = [ln.strip() for ln in d.get("street", "").splitlines() if ln.strip()]
    if compact:
        mid = " ".join(x for x in (d.get("city", ""), d.get("region", ""), d.get("postcode", "")) if x)
        if mid:
            lines.append(mid)
    else:
        lines += [d[k] for k in ("city",) if d.get(k)]
        tail = " ".join(x for x in (d.get("region", ""), d.get("postcode", "")) if x)
        if tail:
            lines.append(tail)
    if d.get("country"):
        lines.append(d["country"])
    return lines


# --------------------------------------------------------------------------
# Drawing
# --------------------------------------------------------------------------

def draw_photo_or_initials(c, card: Card, x_pt: float, y_pt: float, size_pt: float,
                           warnings: list[str], who: str = "") -> None:
    """A rounded photo, or a grey disc with initials, with its lower-left corner at (x, y)."""
    from reportlab.lib.utils import ImageReader  # noqa: PLC0415
    if card.photo is not None:
        try:
            img = ImageReader(io.BytesIO(card.photo.data))
            img.getSize()                                  # opens the image now, so a bad one fails here
        except Exception:  # noqa: BLE001 - bytes from an import can be anything; one bad photo must not stop a print (rule 3)
            warnings.append(f"The photo of {who or display_name(card) or 'a contact'} could not be read, "
                            "so initials are printed instead.")
        else:
            c.saveState()
            path = c.beginPath()
            path.roundRect(x_pt, y_pt, size_pt, size_pt, size_pt * 0.12)
            c.clipPath(path, stroke=0, fill=0)
            c.drawImage(img, x_pt, y_pt, size_pt, size_pt, preserveAspectRatio=True, anchor="c", mask="auto")
            c.restoreState()
            return
    regular, bold, _ = fonts()
    c.saveState()
    c.setFillGray(0.85)
    c.circle(x_pt + size_pt / 2, y_pt + size_pt / 2, size_pt / 2, stroke=0, fill=1)
    c.setFillGray(0.25)
    c.setFont(bold, size_pt * 0.38)
    c.drawCentredString(x_pt + size_pt / 2, y_pt + size_pt / 2 - size_pt * 0.13, safe(initials(card)))
    c.restoreState()


def new_canvas(path, size_mm: tuple[float, float], title: str = ""):
    from reportlab.pdfgen import canvas  # noqa: PLC0415
    c = canvas.Canvas(str(Path(path)), pagesize=(mm(size_mm[0]), mm(size_mm[1])), pageCompression=1)
    c.setTitle(safe(title) if title else "Contacts")
    c.setCreator("Leasha")
    return c
