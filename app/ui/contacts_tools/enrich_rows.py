"""How the Enrich window lays out its suggestions (order 1s section 7). Pure; no widgets.

Layer: L5

* 7a - grouped by contact, the contact with the surest suggestion first and, inside each, the
  surest first; "how sure" said in words ("Seen in 4 messages", "Once, 5 months ago").
* 7b, D4 - a row starts ticked at or above **Tick automatically from** and is not shown below
  **Hide suggestions below**. A row that would change a value the card already has (`update`,
  set by the signature reader, 3k) never starts ticked, however sure: it replaces something the
  person may have typed.
* 7d - what the run said besides suggestions, as one line: the age window's start, the sources
  unreachable or not asked again, what Any age would add, and how many were hidden.
* Where a suggestion came from, in a person's words, and where it can be seen for oneself
  (owner, 2026-10-15: "double click should open source else how do i check?"): `source_words`,
  `where_to_open`, `nothing_to_open`.
* The filter row over the list (owner, 2026-10-15): by kind, by source and by the contact's name
  (`matches`), and the count it shows (`shown_words`).

Pure so the rules can be tested without a window, and so the dialog module stays the shell.
"""

from __future__ import annotations

import re
import time
from typing import Any, Iterable, Optional

__all__ = ["confidence_of", "starts_ticked", "sure_words", "age_words", "layout", "notes_line", "since_words",
           "notes_bits", "notes_summary", "SOURCES", "source_words", "source_icon", "where_to_open",
           "nothing_to_open", "KIND_FILTERS", "ANY_SOURCE", "kind_tags", "matches", "shown_words",
           "suggestion_text", "is_photo"]

#: What a proposal that carries no confidence counts as: `Proposal`'s own default.
DEFAULT_CONFIDENCE = 0.5


def confidence_of(p: Any) -> float:
    try:
        return float(getattr(p, "confidence", DEFAULT_CONFIDENCE))
    except (TypeError, ValueError):
        return DEFAULT_CONFIDENCE


def starts_ticked(p: Any, tick_from: float) -> bool:
    """D4. Confidence decides what is pre-ticked, never what is applied; an update is never pre-ticked."""
    return not bool(getattr(p, "update", False)) and confidence_of(p) >= float(tick_from)


def age_words(seconds: Any, now: Optional[float] = None) -> str:
    """How long ago, in the words a person uses for an age: days, then months, then years."""
    from app.ui.presenter.formatting import format_when  # noqa: PLC0415 - the Search tab's own words

    try:
        at = int(seconds or 0)
    except (TypeError, ValueError):
        return ""
    if at <= 0:
        return ""
    days = int(((now if now is not None else time.time()) - at) // 86400)
    if days < 30:
        return format_when(at * 1_000_000_000, now=now)
    if days < 365:
        months = max(1, days // 30)
        return f"{months} month{'s' if months != 1 else ''} ago"
    years = days // 365
    return f"{years} year{'s' if years != 1 else ''} ago"


#: The Settings words for how sure (`logic.ENRICH_CHOICES`, "Tick automatically from"), lowest first.
_SURE = ((0.95, "Very sure"), (0.85, "Sure"), (0.75, "Fairly sure"), (0.0, "Not sure"))


def sure_words(p: Any, now: Optional[float] = None) -> str:
    """7a: how sure, in words. Mail counts first (how many messages agree, or when the one was sent);
    a source that does not read mail says it as the Settings page does, "Sure (85%)"."""
    seen = int(getattr(p, "seen", 0) or 0)
    if seen >= 2:
        return f"Seen in {seen} messages"
    when = getattr(p, "when", None)
    if when:
        age = age_words(when, now)
        return f"Once, {age}" if age else "Once"
    conf = confidence_of(p)
    word = next(w for floor, w in _SURE if conf >= floor)
    return f"{word} ({round(conf * 100)}%)"


def layout(proposals: list, skip: Iterable[int], hide_below: float) -> tuple[list[tuple[Any, list[int]]], int]:
    """[(contact id, [proposal indexes])] surest contact first, surest row first; and how many were hidden.

    `skip` are indexes already accepted or turned down. Ties keep the order the sources gave."""
    gone = set(skip)
    groups: dict[Any, list[int]] = {}
    hidden = 0
    for i, p in enumerate(proposals):
        if i in gone:
            continue
        if confidence_of(p) < float(hide_below):
            hidden += 1
            continue
        groups.setdefault(getattr(p, "contact_id", None), []).append(i)
    for idxs in groups.values():
        idxs.sort(key=lambda i: -confidence_of(proposals[i]))
    ordered = sorted(groups.items(), key=lambda kv: -confidence_of(proposals[kv[1][0]]))
    return ordered, hidden


def since_words(since: Optional[int]) -> str:
    if not since:
        return ""
    return time.strftime("%d %B %Y", time.gmtime(int(since))).lstrip("0")


def notes_bits(since: Optional[int], notes: Iterable[str], hidden: int) -> list[str]:
    """7d's notes one by one: the window's start first, then what the sources said, then what was hidden."""
    bits = []
    if since:
        bits.append(f"Reading mail since {since_words(since)}.")
    bits += [" ".join(str(n).split()) for n in notes if str(n).strip()]
    if hidden:
        bits.append(f"{hidden} less sure suggestion{' is' if hidden == 1 else 's are'} hidden "
                    "(Settings > Accounts > Enrichment > Hide suggestions below).")
    return bits


def notes_line(since: Optional[int], notes: Iterable[str], hidden: int) -> str:
    """7d: the run's notes in one line - the window's start first, then what the sources said."""
    return " ".join(notes_bits(since, notes, hidden))


def notes_summary(bits: list[str]) -> str:
    """The one line shown while Details is closed (owner, 2026-10-15: the notes were a wall of text
    under the list, taking the list's room). The first note as it is, and how many more Details holds."""
    if not bits:
        return ""
    more = len(bits) - 1
    return bits[0] if not more else f"{bits[0]} And {more} more note{'s' if more != 1 else ''} in Details."


# --------------------------------------------------------------------------
# Where a suggestion came from (owner, 2026-10-15)
# --------------------------------------------------------------------------

#: A source's id -> (its words, its Lucide icon). The words are what a person calls the place; the id
#: stays what the receipt and the journal record.
SOURCES: dict[str, tuple[str, str]] = {
    "signature": ("Email signature", "mail"),
    "gravatar": ("Gravatar", "user-round"),
    "logo": ("Company website", "globe"),
    "wikidata": ("Wikidata", "book-open-text"),
    "face": ("Your photos", "image"),
    "vcard": ("Contact file", "contact"),
    "calendar": ("Calendar invitation", "calendar"),
    "linkedin": ("LinkedIn export", "id-card"),
    "document": ("Document", "file-text"),
    "company": ("Email domain", "at-sign"),
    "correction": ("Correction", "wand-sparkles"),
}
#: The icon of a source this table does not know yet.
OTHER_ICON = "link"


def source_words(source: Any) -> str:
    """ "Email signature" for `signature`. An unknown id is shown capitalised rather than hidden."""
    key = str(source or "").strip()
    if key in SOURCES:
        return SOURCES[key][0]
    return key[:1].upper() + key[1:] if key else "Unknown"


def source_icon(source: Any) -> str:
    return SOURCES.get(str(source or "").strip(), ("", OTHER_ICON))[1]


_URL = re.compile(r"https?://[^\s<>\"';,)]+", re.IGNORECASE)


def where_to_open(p: Any) -> str:
    """Where the suggestion can be seen for oneself: its `opens`, else the first web address in its
    receipt, else "" (nothing to open). A web address goes to the browser, a path to the one open route."""
    opens = str(getattr(p, "opens", "") or "").strip()
    if opens:
        return opens
    found = _URL.search(str(getattr(p, "receipt", "") or ""))
    return found.group(0).rstrip(".") if found else ""


def nothing_to_open(p: Any) -> str:
    """What a double-click says on the status line when there is nothing to open, and why."""
    source = str(getattr(p, "source", "") or "")
    if source == "company":
        why = "it was worked out from the email address."
    elif source == "correction":
        why = "it corrects a value the card already has."
    else:
        why = f"{source_words(source)} gave no page, file or message to show."
    return f"Nothing to open for this suggestion: {why}"


def is_photo(p: Any) -> bool:
    return str(getattr(p, "field_kind", "")) == "photo" or hasattr(getattr(p, "new", None), "mime")


def suggestion_text(p: Any) -> str:
    """The suggested value as text, for Copy the suggestion. A picture has none: ""."""
    return "" if is_photo(p) else str(getattr(p, "new", "") or "")


# --------------------------------------------------------------------------
# The filter row (owner, 2026-10-15)
# --------------------------------------------------------------------------

#: The kinds to filter by, in the order the drop-down shows them: (words, icon).
KIND_FILTERS: tuple[tuple[str, str], ...] = (
    ("All", "list"), ("Photos", "image"), ("Phones", "phone"), ("Emails", "mail"),
    ("Addresses", "map-pin"), ("Web", "globe"), ("Company & title", "briefcase"),
    ("Corrections", "wand-sparkles"),
)
#: The source drop-down's first line.
ANY_SOURCE = "Any source"

_KIND_OF = {"photo": "Photos", "phone": "Phones", "email": "Emails", "address": "Addresses", "url": "Web",
            "social": "Web", "company": "Company & title", "department": "Company & title",
            "title": "Company & title"}


def kind_tags(p: Any) -> set[str]:
    """The kinds a suggestion is found under: All, its field's, and Corrections for a correction."""
    tags = {"All"}
    kind = _KIND_OF.get("photo" if is_photo(p) else str(getattr(p, "field_kind", "")))
    if kind:
        tags.add(kind)
    if str(getattr(p, "source", "")) == "correction":
        tags.add("Corrections")
    return tags


def matches(p: Any, contact_name: str, *, kind: str = "All", source: str = ANY_SOURCE, text: str = "") -> bool:
    """Is this suggestion shown under the filter? `source` is the source's words; `text` is looked for
    in the contact's name, ignoring case."""
    if kind and kind not in kind_tags(p):
        return False
    if source and source != ANY_SOURCE and source_words(getattr(p, "source", "")) != source:
        return False
    wanted = " ".join(str(text or "").split()).casefold()
    return not wanted or wanted in str(contact_name or "").casefold()


def shown_words(shown: int, total: int, ticked: int) -> str:
    """The count beside the filter: "Showing 12 of 140 - 5 ticked"."""
    head = f"Showing all {total}" if shown == total else f"Showing {shown} of {total}"
    return f"{head} - {ticked} ticked"
