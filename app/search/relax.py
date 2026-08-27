r"""Nothing matched. What to let go of, and what to say about it.

Layer: L4 — pure. Takes a `ParsedQuery`, returns queries to try instead. No
store, no engine, no Qt.

**The order's premise did not survive contact with the code, and the
replacement is better.** §2b says "drop the rarest term and re-run". Measured
against a real index: plain words are already joined with `OR`
(`AND_TERM_LIMIT = 1`, chosen against twenty real sentences), so

    "volcano zzzqqq"   ->  '"volcano" OR "zzzqqq"'   ->  1 result

Zero results from several plain words therefore means **every one of them
matched nothing**, and there is no rarest term to drop — dropping any of them
leaves words that already failed. The item as written would have been a
re-run that cannot change the answer, and the label a lie about what happened.

What actually empties a page that had something to find is a **narrowing
instruction**, and there are exactly three of them:

    "volcano flavoured bread"   phrase   ->  every word, in order, adjacent
    volcano AND bread           and      ->  the person typed AND themselves
    volcano type:pdf            filter   ->  the word matched; the filter cut it

Each is dropped in turn, widest effect first, and each is said out loud. That
is the item's actual intent — *nothing matched all your words, here is what
matches with one instruction relaxed* — and the label is still what makes it
legal, because silent rewriting is what loses trust.

**All-words-unknown is left alone**, and deliberately: that query has nothing
to relax towards, and it is already answered one layer up by the spelling
correction (§2a) and the unmatched-terms notice. Manufacturing a "relaxed"
re-run there would report work that changed nothing.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:                                    # pragma: no cover
    from app.search.query import ParsedQuery

__all__ = ["Relaxation", "candidates", "ATTEMPTS", "PHRASE", "AND", "FILTER"]

#: The kinds, most-likely-to-help first.
PHRASE = "phrase"
AND = "and"
FILTER = "filter"

#: **How many re-runs an empty page may cost.**
#:
#: Two, because this is spent only where the alternative is nothing at all:
#: an empty result set means the retrievers had little to score, so a second
#: pass is cheap in exactly the case that reaches it. It is still bounded -
#: three narrowing instructions in one query is a power user's search, and a
#: power user reading "no results" already knows which of their own operators
#: to remove.
ATTEMPTS = 2

#: The filters worth dropping, with the words for saying which. Ordered as the
#: sentence would be, and **`ext` first because it is the one people set by
#: accident** - a kind-of-file chip stays on from the last search far more
#: often than a date range does.
_FILTERS: tuple[tuple[str, str], ...] = (
    ("ext", "the file-type filter"),
    ("names", "the filename filter"),
    ("paths", "the folder filter"),
    ("repos", "the repository filter"),
    ("senders", "the sender filter"),
    ("recipients", "the recipient filter"),
    ("subjects", "the subject filter"),
    ("sizes", "the size filter"),
    ("has_attachment", "the attachment filter"),
)

#: Dropped together with the date pair, since "after 2023" and "before 2024"
#: are one instruction as far as anybody reading a label is concerned.
_DATES = ("after", "before")


@dataclass(frozen=True)
class Relaxation:
    """One thing let go of, the query without it, and how to say so."""

    #: The query to run instead. Always a copy - `ParsedQuery` is frozen, and
    #: relaxation must never alter what the person actually typed.
    query: "ParsedQuery"
    kind: str
    #: The instruction that was dropped, named the way the person would name
    #: it: the phrase itself, `AND`, "the file-type filter".
    dropped: str

    def sentence(self) -> str:
        """The label on the page. **Says what was not found first**, because
        that is the question the person is holding: they typed something, got
        nothing, and need to know the results below are an answer to a
        slightly different question.

        Plain in every register - unlike most notices this one has no
        technical form worth keeping, since "relaxed the conjunction" tells
        nobody anything the plain sentence does not.
        """
        if self.kind == PHRASE:
            return (f"Nothing contains the exact phrase {self.dropped} - "
                    f"these match its words instead.")
        if self.kind == AND:
            return ("Nothing matched all your words at once - these match "
                    "some of them.")
        return (f"Nothing matched with {self.dropped} applied - "
                f"these ignore it.")


def _without_phrases(parsed: "ParsedQuery") -> Optional[Relaxation]:
    r"""A quoted phrase becomes its words.

    **The commonest way an ordinary person empties their own results page.**
    Quotes are widely believed to mean "search properly", so they get typed
    around whole remembered sentences - and a phrase must match every word,
    adjacent and in order, against text that was chunked and stemmed. One
    forgotten word and the page is empty.

    The words are appended to whatever terms the query already had, deduped
    and in order, so `"annual report" budget` relaxes to
    `annual report budget` rather than losing the word that was working.
    """
    if not parsed.phrases:
        return None
    words: list[str] = list(parsed.terms)
    for phrase in parsed.phrases:
        for word in str(phrase).split():
            cleaned = word.strip().lower()
            if cleaned and cleaned not in words:
                words.append(cleaned)
    if not words:
        return None
    named = ", ".join(f'"{phrase}"' for phrase in parsed.phrases)
    return Relaxation(
        query=replace(parsed, phrases=(), terms=tuple(words)),
        kind=PHRASE, dropped=named,
    )


def _without_and(parsed: "ParsedQuery") -> Optional[Relaxation]:
    """An explicit `AND` becomes the default `OR`.

    **Only when the person typed it.** The default is already OR, so there is
    nothing to relax in an ordinary query - and this is the one relaxation
    that contradicts a direct instruction, which is precisely why it is
    labelled and why it is never reached unless the direct instruction found
    nothing.
    """
    if not parsed.explicit_and or len(parsed.terms) < 2:
        return None
    return Relaxation(query=replace(parsed, explicit_and=False),
                      kind=AND, dropped="AND")


def _without_filters(parsed: "ParsedQuery"):
    """Each filter dropped in turn, one per attempt.

    **One at a time, not all at once.** Dropping every filter answers a
    question nobody asked - somebody who typed `type:pdf from:sarah` and sees
    results from everybody in every format has lost the thread of their own
    search. Dropping one names one thing, and the label stays a sentence
    rather than a list.

    Only filters that were actually set are offered, so the attempt budget is
    never spent re-running an identical query.
    """
    for field_name, description in _FILTERS:
        value = getattr(parsed, field_name, None)
        # **`has:no-attachment` is a filter that was set, and it is `False`.**
        # Three states are the point of that field - asked for some, asked for
        # none, did not say - so "unset" is `None` here and nothing else.
        if field_name == "has_attachment":
            if value is None:
                continue
            cleared: object = None
        else:
            if not value:
                continue
            cleared = ()
        yield Relaxation(query=replace(parsed, **{field_name: cleared}),
                         kind=FILTER, dropped=description)
    if parsed.after or parsed.before:
        yield Relaxation(query=replace(parsed, after=None, before=None),
                         kind=FILTER, dropped="the date range")


def candidates(parsed: "ParsedQuery", *,
               limit: int = ATTEMPTS) -> tuple[Relaxation, ...]:
    r"""What to try instead of a query that found nothing, best first.

    Empty when there is nothing to relax — no phrase, no explicit `AND`, no
    filter — which is the ordinary all-words-unknown case and correctly gets
    the spelling suggestion and the unmatched-terms notice instead.

    **Order is by how much of the person's intent survives.** Dropping the
    quotes keeps every word they typed; dropping `AND` keeps every word and
    only widens how they combine; dropping a filter discards something they
    stated outright, so it goes last.
    """
    found: list[Relaxation] = []
    for candidate in (_without_phrases(parsed), _without_and(parsed)):
        if candidate is not None:
            found.append(candidate)
    found.extend(_without_filters(parsed))
    return tuple(found[:max(0, int(limit))])
