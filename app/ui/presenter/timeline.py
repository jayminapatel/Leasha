r"""What the timeline list is made of, and what each line says. No Qt.

Layer: L5 presenter - the decisions, so they are testable without a display.

The timeline is a list of *blocks*, and a block is one of three things:

- a **day** heading ("Saturday 13 June 2015"),
- a **row** - a document, a video or a message, drawn as text,
- a **band** - a run of up to `BAND_SIZE` photographs drawn as thumbnails.

Chronological order is kept exactly: a run of photographs is a band, the letter
written that afternoon is a row after it, and the evening's photographs are the
next band. That is what "photos as thumbnails, documents, mail and videos as
rows, interleaved chronologically" means when written down as a list.

**Why bands rather than a wrapped grid.** A grid whose height depends on the
window's width has to be laid out again on every resize, and a list of four
thousand of them cannot afford it. A band is one fixed height however wide the
window is, so the list scrolls by arithmetic and only what is on screen is ever
drawn - the reason `result_delegate.py` paints instead of building widgets.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Optional, Sequence

from app.reports.timeline_words import (
    badge_words, basis_tip, basis_words, day_heading, size_words,
)

__all__ = ["BAND_SIZE", "Block", "RowText", "blocks_for", "row_text", "photo_tip",
           "loading_sentence", "shown_sentence", "KIND_NOUNS"]

#: Photographs to a band. **Fixed**: it sets a cell's shape (a band is five
#: cells across at its widest), not something anybody would tune - a person
#: who wants bigger pictures asks for the preview, not for fewer per row.
BAND_SIZE = 5

KIND_NOUNS: dict[str, str] = {
    "photo": "Photo", "video": "Video", "mail": "Message",
    "document": "Document", "code": "Program code",
}


@dataclass(frozen=True)
class Block:
    """One line of the list. `folds` holds `app.search.folding.Fold` objects."""

    kind: str                    # "day" | "row" | "band"
    text: str = ""               # a day's heading
    folds: tuple = ()            # a row's one fold, or a band's photographs


@dataclass(frozen=True)
class RowText:
    """The words on a row, already chosen."""

    title: str
    detail: str
    when: str
    basis: str
    basis_tip: str
    badge: str
    folded: str


def blocks_for(items: Sequence[Any], previous_day: Optional[date] = None,
               band_size: int = BAND_SIZE) -> tuple[list, Optional[date]]:
    r"""The blocks for a page of folds, and the day the page ended on.

    `previous_day` is where the last page ended, so a page that carries on
    through the same day does not print its heading twice. A band never spans a
    day: a heading in the middle of one would be a lie about when the last
    photographs were taken.

    Each page starts a fresh band. A page seam can therefore leave a short band
    (two photographs where five would fit) - one visual gap in a scroll of
    hundreds, in exchange for never having to re-draw a band already on screen.
    """
    blocks: list[Block] = []
    day = previous_day
    band: list = []

    def flush() -> None:
        nonlocal band
        if band:
            blocks.append(Block("band", folds=tuple(band)))
            band = []

    for fold in items:
        entry = fold.head
        when = entry.when
        if when.date() != day:
            flush()
            day = when.date()
            blocks.append(Block("day", text=day_heading(when)))
        if entry.kind == "photo":
            band.append(fold)
            if len(band) >= band_size:
                flush()
        else:
            flush()
            blocks.append(Block("row", folds=(fold,)))
    flush()
    return blocks, day


def _folder_of(entry: Any) -> str:
    path = (entry.relative_path or entry.path or "").replace("\\", "/")
    folder = path.rsplit("/", 1)[0] if "/" in path else ""
    return folder


def row_text(fold: Any) -> RowText:
    """The words for a document, video or message row."""
    entry = fold.head
    parts = [KIND_NOUNS.get(entry.kind, "File")]
    if entry.kind == "mail" and entry.sender:
        parts.append(f"from {entry.sender}")
    if entry.size_bytes and entry.kind != "mail":
        parts.append(size_words(entry.size_bytes))
    if entry.place:
        parts.append(entry.place)
    folder = _folder_of(entry)
    if folder and entry.kind != "mail":
        parts.append(folder)
    return RowText(
        title=entry.name or "(no name)", detail="  -  ".join(parts),
        when=f"{entry.when:%H:%M}", basis=basis_words(entry.basis),
        basis_tip=basis_tip(entry.basis), badge=badge_words(entry),
        folded=fold.label() if fold.folded else "")


def photo_tip(fold: Any) -> str:
    """The tooltip on a thumbnail: what it is, when, by which rule, and where."""
    entry = fold.head
    lines = [entry.name, f"{entry.when:%d %B %Y, %H:%M} - {basis_words(entry.basis)}"]
    if entry.place:
        lines.append(entry.place)
    badge = badge_words(entry)
    if badge:
        lines.append(f"On {badge}")
    if fold.folded:
        lines.append(fold.label() + " - right-click to show them all")
    return "\n".join(lines)


def loading_sentence(period_text: str) -> str:
    return f"Looking through what Leasha has from {period_text}..."


def shown_sentence(shown: int, done: bool) -> str:
    """The line under the list: how much is on screen, and whether that is all."""
    if not shown:
        return ""
    noun = "item" if shown == 1 else "items"
    if done:
        return f"{shown:,} {noun} - that is everything from this period."
    return f"{shown:,} {noun} so far - scroll down for more."
