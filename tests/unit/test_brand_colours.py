"""The window's colours against the Leasha brand. 2026-10-04.

Layer: L5

The brand assessment (owner, 2026-10-04) measured the kind badges - a white
word on orange `#ff9933` was 2.13:1, on lime `#a1b000` 2.41:1, on blue
`#0778d9` 4.47:1 - all under the 4.5:1 the brand's own 1 October ruling
exists for. The brand supplies text-safe tints for exactly this; the badges
use them now, and the light accent is the brand's indigo rather than a navy
near it. The brand values are copied here from `brand.json`, so a change to
either side is seen.
"""

from __future__ import annotations

import pytest

from app.ui.theme import PALETTES, Theme

#: `brand.json`, jeff-doc-leasha, as of 1 October 2026.
BRAND = {
    "indigo": "#15084b",
    "blue": "#0a79db", "lime": "#a1b002", "orange": "#ff9933",
    "chrome": "#6c6685", "chrome_on_dark": "#cdcbd8",
    "blue_text": "#0866bd", "blue_text_on_dark": "#3d93e6",
    "lime_text": "#6b7600", "orange_text": "#a35200",
}


def _luminance(hex_colour: str) -> float:
    def channel(value: int) -> float:
        c = value / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (channel(int(hex_colour[i:i + 2], 16)) for i in (1, 3, 5))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a: str, b: str) -> float:
    la, lb = _luminance(a), _luminance(b)
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


KINDS = ("kind_doc", "kind_mail", "kind_code", "kind_other")


@pytest.mark.parametrize("theme", [Theme.LIGHT, Theme.DARK])
def test_the_badge_word_reads_on_every_kind_fill(theme):
    """A badge is a word on a fill; the word has to reach 4.5:1 on it."""
    from app.ui.result_delegate import BADGE_INK

    palette = PALETTES[theme]
    for kind in KINDS:
        ratio = contrast(BADGE_INK, palette[kind])
        assert ratio >= 4.5, f"{theme}: {BADGE_INK} on {kind} {palette[kind]} is {ratio:.2f}:1"


@pytest.mark.parametrize("theme", [Theme.LIGHT, Theme.DARK])
def test_the_badges_are_the_brands_text_safe_tints(theme):
    """The owner, 2026-10-04: the text-safe tints as the kind colours in both
    themes - the same label on either ground."""
    palette = PALETTES[theme]
    assert palette["kind_doc"] == BRAND["blue_text"]
    assert palette["kind_mail"] == BRAND["orange_text"]
    assert palette["kind_code"] == BRAND["lime_text"]
    assert palette["kind_other"] == BRAND["chrome"]


def test_the_splash_stripes_are_the_brands_colours_exactly():
    """Stripes are fills with no word on them, so they keep the full colours
    - and were two units off the logo's blue and lime until today."""
    from app.ui import splash

    assert splash.BRAND_NAVY.lower() == BRAND["indigo"]
    assert splash.BRAND_STRIPE_BLUE.lower() == BRAND["blue"]
    assert splash.BRAND_STRIPE_GREEN.lower() == BRAND["lime"]
    assert splash.BRAND_STRIPE_ORANGE.lower() == BRAND["orange"]


def test_the_icon_files_put_their_largest_picture_first():
    """`QPixmap(path)` loads an .ico's first picture, and the rail scales it
    to 28px. Rebuilt from the brand's symbol on 4 October with the smallest
    first, the rail's mark was a 16px picture blown up - a blur, seen in the
    regrabbed guide pictures."""
    import struct
    from pathlib import Path

    for name, first in (("leasha.ico", 256), ("leasha-tray.ico", 32)):
        data = Path("assets", name).read_bytes()
        count = struct.unpack("<H", data[4:6])[0]
        widths = [data[6 + 16 * i] or 256 for i in range(count)]
        assert widths == sorted(widths, reverse=True), f"{name}: {widths}"
        assert widths[0] == first


def test_the_light_accent_is_the_brand_indigo():
    light = PALETTES[Theme.LIGHT]
    assert light["accent"] == BRAND["indigo"]
    assert light["accent_text"] == BRAND["indigo"]
    assert light["accent_bar"] == BRAND["indigo"]
    assert contrast(light["accent_on"], light["accent"]) >= 4.5
    assert contrast(light["accent_text"], light["accent_soft"]) >= 4.5


def test_the_badge_painter_uses_the_themes_ink_not_a_literal():
    from pathlib import Path

    source = Path("app/ui/result_delegate.py").read_text(encoding="utf-8")
    start = source.index("def _paint_badge(")
    body = source[start:source.index("\ndef ", start + 1)]
    assert '"#ffffff"' not in body and "BADGE_INK" in body, (
        "the badge word's colour is a literal in the painter, where no test measures it")
