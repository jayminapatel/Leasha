r"""What the Search page says while it works, and when it finds nothing. Qt-free.

Layer: L5 (presenter half)

Review 2026-10-14, the third friction point: "Search does not say when it is
working, or when it found nothing." Three decisions, each tested here with
plain values; `widgets/search_flight.py` only carries them out.

* **MS-1, the busy line.** Once rows are on screen a refinement showed nothing
  for 0.4-3.3 s - the skeleton only fills an empty list. A thin moving bar now
  appears `BUSY_DELAY_MS` after a search goes out and goes when *that*
  search's finished answer lands, or it fails. An early (not yet reranked)
  answer does not end it: the order is still coming. A stale answer never
  ends it either - the newer search is still out.
* **MS-7, nothing found over kept rows.** The page keeps what is on screen when
  a search momentarily finds nothing (mid-word, or while the quick tier runs),
  but the status line said "0 results" over those rows. Now the kept rows are
  dimmed and the line says so (`kept_words`); once the full tier has answered
  with nothing, the list empties and says that instead. The line never
  contradicts the rows.
* **MS-5, the grid offer.** When most of a result set is pictures, the notice
  bar offers the thumbnail grid (`grid_offer`).
* **MS-8, stale work.** A search superseded before it started is taken back
  from the pool (`search_flight` says why searches keep the global pool).
"""

from __future__ import annotations

from typing import Any, Iterable

__all__ = [
    "BUSY_DELAY_MS", "KEEP", "EMPTY", "busy_ends", "after_nothing",
    "kept_words", "GRID_OFFER_SHARE", "GRID_OFFER_MIN", "mostly_photos", "grid_offer",
    "GRID_HREF", "NOTICE_GRID_OFFER", "DIM_OPACITY",
]

#: How long a search is out before the busy line shows. Short enough that a
#: slow refinement is seen to be working; long enough that the warm keyword
#: tier (about 150 ms on the owner's index) usually answers before it appears.
BUSY_DELAY_MS = 120

#: How see-through kept rows are while they are not this search's answer.
DIM_OPACITY = 0.45

KEEP = "keep"
EMPTY = "empty"


def busy_ends(generation: int, latest: int, *, early: bool) -> bool:
    """Does this answer end the busy line? Only the latest search's finished one."""
    return not early and int(generation) >= int(latest)


def after_nothing(*, shown_anything: bool, early: bool, interim: bool) -> str:
    """What an answer with no rows does to the list: `KEEP` the rows on screen
    (dimmed, and said), or `EMPTY` it.

    Kept only while a better answer is still coming - an early answer, or the
    quick tier - and only when there is something to keep. The full tier's
    finished "nothing" is the answer, and the list says so.
    """
    if not shown_anything:
        return EMPTY
    return KEEP if (early or interim) else EMPTY


#: Long enough to recognise the query, short enough for one line.
_QUERY_CHARS = 60


def kept_words(query: str) -> str:
    """The status line while kept rows are dimmed - new words, 2026-10-14."""
    text = " ".join(str(query or "").split())
    if len(text) > _QUERY_CHARS:
        text = text[: _QUERY_CHARS - 1].rstrip() + "…"
    return f"No matches for “{text}” - showing the previous results"


#: Share of a result set that must be pictures before the grid is offered, and
#: the fewest pictures worth a grid at all.
GRID_OFFER_SHARE = 0.6
GRID_OFFER_MIN = 4

#: The link the notice bar hands back when the offer is taken.
GRID_HREF = "grid:on"
NOTICE_GRID_OFFER = "NOTICE_GRID_OFFER"


def mostly_photos(flags: Iterable[bool]) -> bool:
    """Are most rows pictures? `flags` is one bool per row: is it an image."""
    values = [bool(flag) for flag in flags]
    photos = sum(values)
    return photos >= GRID_OFFER_MIN and photos >= GRID_OFFER_SHARE * len(values)


def grid_offer(flags: Iterable[bool], *, grid_on: bool) -> list[Any]:
    """The notice offering the thumbnail grid, as a one-item list, or `[]`."""
    if grid_on or not mostly_photos(flags):
        return []
    from app.ui.presenter.search import _Hint

    return [_Hint(NOTICE_GRID_OFFER,
                  f'Most of these are pictures. <a href="{GRID_HREF}">Show them as a grid</a>?')]
