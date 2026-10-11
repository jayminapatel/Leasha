"""A contact as a QR code, drawn here with no network.

Layer: L7

Order 1k 6r. The code holds a compact vCard 3.0 - the same text `vcard.dumps`
writes, unfolded (a QR code has no line-length limit) and cut to what a phone
camera reads easily. `segno` (a pure-Python wheel) makes the module grid; nothing
is fetched, and the SVG carries no link, image or script.

**What goes in, in this order, stopping at the first thing that does not fit**
`MAX_BYTES`: the name and company, the first email and phone, the title, the first
web page, the first address, the other emails and phones, the birthday, the
nickname, then everything else. Notes, groups, photo and the star never go in: a
photo alone would make a code no phone could read. Priority means a card with a
hundred emails still scans, with the first few.
"""

from __future__ import annotations

import io

from app.contacts import vcard
from app.contacts.card import Card, Field

__all__ = ["MAX_BYTES", "vcard_qr_text", "vcard_qr_matrix", "vcard_qr_svg"]

#: The most bytes of vCard put in a code. Fixed (rule 11): about QR version 20 at
#: error level M, which a phone reads from a laptop screen; much larger codes are
#: dense enough to need a steady hand. Evidence that would change it: a real phone
#: failing to read a code at this size.
MAX_BYTES = 800

_NAME = ("prefix", "given", "additional", "family", "suffix")


def _items(card: Card) -> list:
    """Callables that add one thing to a reduced card, best first."""
    live = [f for f in card.fields if f.value.strip() or any(f.data.values())]
    of = lambda kind: [f for f in live if f.kind == kind]  # noqa: E731
    emails, phones, urls, addrs = of("email"), of("phone"), of("url"), of("address")
    birthdays = [f for f in of("date") if f.label.strip().lower() == "birthday"]
    used = {id(f) for f in [*emails[:1], *phones[:1], *urls[:1], *addrs[:1], *emails[1:], *phones[1:], *birthdays]}
    rest = [f for f in live if id(f) not in used]

    def scalar(*names):
        return lambda out: out.scalars.update({n: card.get(n) for n in names if card.get(n).strip()})

    def field(f: Field):
        return lambda out: out.fields.append(Field(f.kind, f.value, f.label, dict(f.data), f.primary))

    steps = [scalar(*_NAME, "company"), *map(field, emails[:1]), *map(field, phones[:1]), scalar("title", "department"),
             *map(field, urls[:1]), *map(field, addrs[:1]), *map(field, emails[1:]), *map(field, phones[1:]),
             *map(field, birthdays), scalar("nickname"), *map(field, rest)]
    return steps


def _text(card: Card) -> str:
    return vcard.dumps(card, version="3.0").replace("\r\n ", "")


def vcard_qr_text(card: Card, max_bytes: int = MAX_BYTES) -> str:
    """The vCard 3.0 text the code holds, at most `max_bytes` bytes of UTF-8."""
    out = Card()
    for add in _items(card):
        trial = out.copy()
        add(trial)
        if len(_text(trial).encode("utf-8")) > max_bytes:
            break
        out = trial
    has_content = any(card.get(n).strip() for n in (*_NAME, "company", "nickname")) or any(
        f.value.strip() or any(f.data.values()) for f in card.fields)
    if has_content and not out.scalars and not out.fields:
        raise ValueError("This contact's name and company are too long to fit in a QR code. Shorten them, or share "
                         "the contact as a vCard file instead.")
    if not out.scalars and not out.fields:
        raise ValueError("There is nothing to put in a QR code: this contact has no name, company, email "
                         "address or phone number.")
    return _text(out)


def _make(card: Card, error: str):
    import segno  # noqa: PLC0415 - only when a code is wanted
    data = vcard_qr_text(card).encode("utf-8")
    # Raw bytes, no ECI header: phone cameras read UTF-8 without one, and some stumble on it.
    return segno.make(data, mode="byte", error=error, micro=False, boost_error=False)


def vcard_qr_matrix(card: Card, error: str = "m") -> list[list[bool]]:
    """The module grid, True for a dark module, with no quiet zone."""
    return [[bool(v) for v in row] for row in _make(card, error).matrix]


def vcard_qr_svg(card: Card, scale: int = 4, border: int = 4, dark: str = "#000000", light: str = "#ffffff",
                 error: str = "m") -> str:
    """The code as inline SVG text. Always dark on light, whatever the app's theme:
    a code with its colours reversed is unreadable to many cameras."""
    buf = io.BytesIO()
    _make(card, error).save(buf, kind="svg", scale=scale, border=border, dark=dark, light=light, xmldecl=False,
                            nl=False)
    return buf.getvalue().decode("utf-8")
