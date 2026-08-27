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

__all__ = ["RECENT_LIMIT", "SAVED_LIMIT", "SAVED_HEADING", "RECENT_HEADING",
           "recent", "rows_for", "greeting", "saved_rows", "sections"]

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
        # **A row, or the string a worker already reduced one to.** The fetch
        # runs this once off the interface thread and hands back plain text;
        # the dropdown then hands that same list back in when it redraws, and
        # a second pass over it must be the identity rather than a crash.
        text = (row.strip() if isinstance(row, str)
                else str((row or {}).get("query") or "").strip())
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


# ---------------------------------------------------------------------------
# Adoptions §3 — saved searches, under the recent ones
# ---------------------------------------------------------------------------

#: How many saved searches an empty box offers.
#:
#: Larger than `RECENT_LIMIT`, and deliberately: a recent search is one of
#: hundreds and the list is a sample, while saved searches are a set somebody
#: **chose**, one at a time, on purpose. Truncating that set is throwing away
#: the only work the person did to make this feature exist. Eight covers
#: anybody's real list; past that the `/saved` menu is the right surface and
#: says so.
SAVED_LIMIT = 8

#: The two headings. **Words, not styling** - a section anybody has to infer
#: from a font weight is a section screen readers do not have at all, which is
#: the same rule the meaning-match marker follows.
RECENT_HEADING = "Recent"
SAVED_HEADING = "Saved"


def saved_rows(saved: Any, *, limit: int = SAVED_LIMIT,
               settings: Any = None) -> tuple:
    r"""`(label, token)` per saved search, most-run first. **Never raises.**

    The label is what is read - `invoices — type:pdf from:accounts` - and the
    token is what goes in the box: `saved:invoices`, not the query it stands
    for.

    **The reference, not the text**, and that is §3a's "a smart folder, not a
    snapshot" made real one level down. Inserting the stored query would put a
    frozen copy in the box: edit it, press Enter, and you have run something
    that is no longer the thing you saved, with nothing on screen saying so.
    The token stays a pointer, so the search that runs is always the one the
    name currently means.
    """
    try:
        from app.search.saved import ordered, summary

        rows = []
        for entry in ordered(saved, limit=max(0, int(limit))):
            note = summary(entry)
            rows.append(((f"{entry.name} — {note}" if note else entry.name),
                         entry.as_token()))
        return tuple(rows)
    except Exception:                            # noqa: BLE001 - see docstring
        return ()


def sections(rows: Any, saved: Any = None, settings: Any = None) -> tuple:
    r"""What an empty, focused search box offers: recent first, then saved.

    Returns `((heading, ((label, insert), …)), …)`, skipping any section with
    nothing in it - a heading over an empty list is a promise the box does not
    keep.

    **Recent above saved**, which is the order §3a asks for and is also the
    right one: recent is the answer to "carry on with what I was doing", which
    is what somebody who has just opened the box is nearly always doing.
    Saved is deliberate and rarer, and a rare thing at the top of a list is a
    rare thing in everybody's way.
    """
    out = []
    history = tuple((text, text) for text in rows_for(rows, settings))
    if history:
        out.append((RECENT_HEADING, history))
    stored = saved_rows(saved, settings=settings)
    if stored:
        out.append((SAVED_HEADING, stored))
    return tuple(out)
