r"""The first ten seconds: an empty box, and what it should say.

Layer: L5 presenter — Qt-free, so every word here is testable without a
window.

**Somebody opening this for the first time sees an empty box and nothing
else.** They do not know whether to type a filename, a sentence, or one of the
slash commands the other tabs advertise. The other three tabs already answer
that with a placeholder; the everyday tab, the one an eight-year-old is meant
to use, had none at all.

**And somebody opening it for the hundredth time wants what they typed
yesterday.** Searches are already logged - `searches` has held every query
since Layer 4, for a tuning feature nobody has built - so the most useful
thing an empty box can offer costs one indexed read of a table that already
exists.
"""

from __future__ import annotations

from typing import Any, Optional, Sequence

__all__ = ["RECENT_LIMIT", "recent", "rows_for", "greeting"]

#: **No placeholder is defined here, and that is a decision.**
#:
#: §2e asks for "a plain example sentence" in the search box. There is already
#: a placeholder there, argued in `widgets/search_bar.py`: it names `/` and
#: three real filters, and it is the only thing on screen that says the box
#: reaches mail and repositories at all. Replacing it would break this same
#: order's principle 4 - *existing labels and descriptions never change* - to
#: satisfy a later item of the same order. The principle wins; the note in the
#: order records it.

#: How many past searches an empty box offers.
#:
#: Six, because this is a list somebody glances at under their own cursor, not
#: a history page. Ten would cover the box; three would rarely contain the one
#: they want.
RECENT_LIMIT = 6

#: Queries never offered back. **A slash command is a mechanism, not a
#: memory** - somebody who typed `/newest` was steering, not asking, and
#: offering it back teaches the box's own syntax rather than their own work.
_SKIP_PREFIXES = ("/",)


#: How many logged rows to ask for, to end up with `RECENT_LIMIT` distinct
#: ones. Eight times over, because a refined query leaves several near-copies
#: in a row and asking for six would often return one afternoon's typing.
FETCH_MULTIPLE = 8


def recent(rows: Any, *, limit: int = RECENT_LIMIT,
           enabled: bool = True) -> tuple:
    r"""The last few distinct things this person searched for, newest first.

    **Takes rows, not a store.** `recent_searches` is a database read and this
    runs while a box is being focused; `test_no_store_call_outside_a_worker`
    caught the first version of this reading the store on the UI thread, and
    it was right to. The fetch lives in `workers.recent_searches_async`; the
    rules live here, where they can be tested with a list.

    **Never raises**: a history that cannot be read is a box with no dropdown
    rather than a window that fails to open.

    Deduped case-insensitively but returned as typed, because somebody
    refining a query leaves four near-identical rows in the log and a list of
    those is worse than no list - while showing them back in a case they did
    not use reads as a correction.
    """
    if not enabled or not rows:
        return ()

    seen: set = set()
    found: list = []
    for row in rows:
        text = str((row or {}).get("query") or "").strip()
        if not text or text.startswith(_SKIP_PREFIXES):
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        found.append(text)
        if len(found) >= limit:
            break
    return tuple(found)


def greeting(count: Optional[int]) -> str:
    r"""What sits under an empty box when there is no history to offer.

    **Three states, because "nothing indexed yet" and "nothing searched yet"
    are different problems** and telling somebody the wrong one sends them to
    the wrong screen. A new user with an empty index needs the Indexing tab; a
    new user with a full index needs an example, which the placeholder already
    gives them; and `None` means nobody has counted, so this says nothing at
    all rather than guessing.
    """
    if count is None:
        return ""
    if count <= 0:
        return ("Nothing has been indexed yet. Open the Indexing tab, add a "
                "folder, and press Start.")
    return f"{count:,} documents ready to search."


def rows_for(rows: Any, settings: Any = None) -> Sequence[str]:
    """`recent`, with the preference read off `settings`.

    The switch is honoured here rather than in the view, so a view has one
    call and no rule of its own - the same reason `SearchPolicy` exists.
    """
    enabled = True
    if settings is not None:
        enabled = bool(getattr(settings, "search_offer_recent", True))
    return recent(rows, enabled=enabled)
