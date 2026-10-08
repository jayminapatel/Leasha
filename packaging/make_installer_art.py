r"""The Leasha pictures the Windows installer shows, made from the brand assets.

Layer: L0 (a build tool; nothing in the app imports it)

2026-10-08, the owner: "can there be leasha branded stuff instead of plain white
standard images". Inno Setup shows a tall picture down the left of its Welcome
and Finish pages (`WizardImageFile`) and a small one at the top right of every
other page (`WizardSmallImageFile`). Left unset, both are Inno's own.

Each is made at the sizes Inno picks between for the display's scaling - 100%,
125%, 150%, 175%, 200%, 250% - so the picture is never stretched. The tall one
is the brand indigo with the white lockup and a line saying what Leasha does;
the small one is the mark on white. Run it after the brand assets change:

    venv\Scripts\python.exe packaging\make_installer_art.py

It writes `packaging\art\`; `installer.iss` names the files it writes.
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "assets"
OUT = Path(__file__).resolve().parent / "art"

#: The brand kit's colours (`jeff-doc-leasha`, `brand.json`; also pinned in
#: tests/unit/test_brand_colours.py).
INDIGO = (0x15, 0x08, 0x4B)
BLUE = (0x0A, 0x79, 0xDB)
LIME = (0xA1, 0xB0, 0x02)
ORANGE = (0xFF, 0x99, 0x33)

#: Inno's own sizes for each scaling, in pixels.
SCALES = (100, 125, 150, 175, 200, 250)
WIZARD = (164, 314)
SMALL = (55, 55)

TAGLINE = ("Find anything", "on your computer,", "from a description.")


def _scaled(size: tuple[int, int], percent: int) -> tuple[int, int]:
    return round(size[0] * percent / 100), round(size[1] * percent / 100)


def _font(px: int) -> ImageFont.ImageFont:
    for name in ("segoeuisl.ttf", "segoeui.ttf", "arial.ttf"):
        try:
            return ImageFont.truetype(name, px)
        except OSError:
            continue
    return ImageFont.load_default()


def wizard_image(percent: int) -> Image.Image:
    """The tall picture: indigo, the white lockup near the top, the tagline
    below it, and the brand's three accents as a thin rule at the foot."""
    width, height = _scaled(WIZARD, percent)
    image = Image.new("RGB", (width, height), INDIGO)
    draw = ImageDraw.Draw(image)

    lockup = Image.open(ASSETS / "leasha-lockup-reversed.png").convert("RGBA")
    target = int(width * 0.78)
    lockup = lockup.resize((target, round(lockup.height * target / lockup.width)),
                           Image.LANCZOS)
    top = int(height * 0.12)
    image.paste(lockup, ((width - lockup.width) // 2, top), lockup)

    font = _font(max(10, round(height * 0.042)))
    y = top + lockup.height + int(height * 0.08)
    for line in TAGLINE:
        box = draw.textbbox((0, 0), line, font=font)
        draw.text(((width - (box[2] - box[0])) // 2, y), line, font=font,
                  fill=(0xCD, 0xCB, 0xD8))
        y += int((box[3] - box[1]) * 1.55)

    rule = max(2, round(height * 0.012))
    third = width // 3
    for index, colour in enumerate((BLUE, LIME, ORANGE)):
        right = width if index == 2 else (index + 1) * third
        draw.rectangle([index * third, height - rule, right, height], fill=colour)
    return image


def small_image(percent: int) -> Image.Image:
    """The mark, centred on white with a little room round it."""
    side = _scaled(SMALL, percent)[0]
    image = Image.new("RGB", (side, side), (255, 255, 255))
    mark = Image.open(ASSETS / "leasha-256.png").convert("RGBA")
    inner = int(side * 0.86)
    mark = mark.resize((inner, inner), Image.LANCZOS)
    offset = (side - inner) // 2
    image.paste(mark, (offset, offset), mark)
    return image


def wizard_files() -> list[str]:
    return [f"wizard-{p}.png" for p in SCALES]


def small_files() -> list[str]:
    return [f"small-{p}.png" for p in SCALES]


def main() -> int:
    OUT.mkdir(exist_ok=True)
    for percent in SCALES:
        wizard_image(percent).save(OUT / f"wizard-{percent}.png", optimize=True)
        small_image(percent).save(OUT / f"small-{percent}.png", optimize=True)
    print(f"wrote {2 * len(SCALES)} pictures to {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
