r"""The Photos page's "Aspect" grid: rows of photos at their own shape - no Qt, no I/O.

Layer: L5 (presenter)

MS-12 (UX review 2026-10-14). The square grid crops every picture to its centre square,
the owner's choice and still the default. "Aspect" is the other way a photo library lays
pictures out (Apple Photos, Google Photos): each row is one height, each picture as wide
as its own shape needs at that height, and the row stretched or shrunk a little so it
fills the width exactly. A panorama is a long tile, a portrait a narrow one - nothing cut.

`justify` does the packing. It is pure arithmetic over a list of aspect ratios (width
divided by height), so it runs the same in a test as on the page and costs a few
milliseconds for the owner's 15,000 photos.
"""

from __future__ import annotations

from typing import Optional, Sequence

__all__ = ["justify", "row_breaks", "clamp_aspect", "ROW_HEIGHT", "UNKNOWN_ASPECT",
           "MIN_ASPECT", "MAX_ASPECT", "LAST_ROW_STRETCH"]

#: The height a row aims for, in pixels - between the Medium and Large tiles.
ROW_HEIGHT = 180
#: The shape assumed until a picture's thumbnail says otherwise: a camera's 4:3.
UNKNOWN_ASPECT = 4 / 3
#: Shapes past these are drawn at the limit (and cropped to it): a 20:1 panorama
#: would otherwise be a strip a few pixels high, a 1:20 receipt a sliver.
MIN_ASPECT = 0.4
MAX_ASPECT = 4.0
#: The last row is filled to the width only when that makes it at most this much
#: taller than `ROW_HEIGHT`; a shorter last row stays at the height and ragged.
LAST_ROW_STRETCH = 1.25


def clamp_aspect(aspect: Optional[float]) -> float:
    """`aspect` held between `MIN_ASPECT` and `MAX_ASPECT`; `UNKNOWN_ASPECT` for none or nonsense."""
    try:
        value = float(aspect)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return UNKNOWN_ASPECT
    if not value > 0 or value != value or value == float("inf"):
        return UNKNOWN_ASPECT
    return max(MIN_ASPECT, min(MAX_ASPECT, value))


def row_breaks(aspects: Sequence[Optional[float]], width: int, height: int = ROW_HEIGHT,
               gap: int = 0) -> list[tuple[int, int]]:
    """`(first, last)` index of each row, top to bottom, greedy: a row takes pictures until,
    at `height`, they reach `width`. Every picture is in exactly one row."""
    width = max(1, int(width))
    height = max(1, int(height))
    rows: list[tuple[int, int]] = []
    start, run = 0, 0.0
    for number, aspect in enumerate(aspects):
        run += clamp_aspect(aspect) * height
        if run + gap * (number - start) >= width:
            rows.append((start, number))
            start, run = number + 1, 0.0
    if start < len(aspects):
        rows.append((start, len(aspects) - 1))
    return rows


def justify(aspects: Sequence[Optional[float]], width: int, height: int = ROW_HEIGHT,
            gap: int = 0) -> list[tuple[int, int]]:
    """`(width, height)` in whole pixels for each picture, in order.

    Each row's pictures, with `gap` pixels between neighbours, add up to exactly `width`
    (the last picture in a row takes the rounding), all at the row's one height. The last
    row - usually short - is drawn at `height` and left ragged, as a photo library leaves
    it, unless it is nearly full. A row is never narrower than a picture of one pixel.
    """
    width = max(1, int(width))
    height = max(1, int(height))
    gap = max(0, int(gap))
    shapes = [clamp_aspect(a) for a in aspects]
    sizes: list[tuple[int, int]] = []
    rows = row_breaks(shapes, width, height, gap)
    for row_number, (first, last) in enumerate(rows):
        members = shapes[first:last + 1]
        room = max(len(members), width - gap * (len(members) - 1))
        total = sum(members)
        fitted = room / total
        if row_number == len(rows) - 1 and fitted > height * LAST_ROW_STRETCH:
            fitted = float(height)                      # the short last row stays ragged
            widths = [max(1, round(a * fitted)) for a in members]
        else:
            widths = [max(1, round(a * fitted)) for a in members]
            widths[-1] = max(1, room - sum(widths[:-1]))
        row_height = max(1, round(fitted))
        sizes.extend((w, row_height) for w in widths)
    return sizes
