r"""`*voice` and `inv?ice`, answered from the vocabulary FTS5 already keeps.

Layer: L4

From `docs/WORKORDER-202626081106-wildcards.md`. **The defect being fixed is
silence.** A trailing `*` has always been real FTS5 prefix matching, but the two
other things people type failed without saying so:

| Typed | Became | Result |
|---|---|---|
| `*voice` | `"voice"` | the star dropped, hits for "voice" - looks like it worked |
| `inv?ice` | `"inv" OR "ice"` | split into two unrelated terms |

`*voice` returning results for *voice* is worse than returning none, because
there is nothing on screen to suggest the question was not the one asked.

**`fts5vocab` costs nothing.** It is a virtual table over the terms FTS5 already
stores - no new index, no re-index, no extra disk. A wildcard becomes a `LIKE`
over the vocabulary and the matching terms become an ordinary `OR` query, so
everything downstream - ranking, fusion, filters, the `symbols` column - keeps
working untouched. Measured on this project's own index: 400k distinct terms,
77ms for a leading wildcard.

The alternative was a second FTS5 table with a `trigram` tokenizer, which gives
exact substring matching and no stemming problem. It is what makes filename
search work, and it is **2-5x the size of the text it indexes**. Right for
filenames, which are tiny; hundreds of gigabytes of wrong for 600GB of content.

**The vocabulary holds Porter stems, not words**, and that shapes everything
here. The stored form of *voice* is `voic`, so `LIKE '%voice'` matches nothing
at all. The fragments are therefore stemmed before they meet the vocabulary -
using FTS5's own tokenizer rather than a reimplementation of Porter, see
`stem_with` - and the consequence is that **suffix wildcards are approximate**:
`*voice` also reaches `invoicing`, because they share a stem. That is usually
what somebody wants and it is not what they typed, so `report` says so and the
window prints it.
"""

from __future__ import annotations

import re
import time
from typing import Any, Optional, Sequence

from app.core.logging import logger

__all__ = [
    "MAX_TERMS", "MIN_LITERAL_CHARS", "BUDGET_S", "Expansion",
    "needs_expansion", "like_pattern", "literal_length", "stem_with",
    "expand", "strip_wildcards",
]

log = logger.bind(component="search.wildcards")

#: Terms one wildcard may expand to.
#:
#: **A cap, and the reason is the shape of the failure without one.** `*a*`
#: uncapped is every term in the corpus in a single `OR` expression - which is
#: not a slow search, it is a way to hang the application from the search box.
MAX_TERMS = 200

#: Literal characters a pattern must have outside its wildcards.
#:
#: `*a*` is a vocabulary scan that returns everything and helps nobody. The same
#: reasoning already applies to one and two-character filename queries, and the
#: refusal is reported rather than silent.
MIN_LITERAL_CHARS = 2

#: Seconds the vocabulary lookup may take before it returns what it has.
#:
#: 77ms over 400k terms measured; a corpus at 600GB might hold two to five
#: million distinct terms, so roughly 400ms-1s. Acceptable for a query somebody
#: typed a `*` into on purpose, and paid only when a wildcard is present.
BUDGET_S = 3.0

#: `*` and `?`, the two the grammar accepts.
_WILDCARD = re.compile(r"[*?]")


class Expansion:
    """What one wildcard turned into, and everything worth saying about it.

    Not a `NamedTuple`: the whole point of this feature is the reporting, and a
    result that carries its own explanation cannot be logged without it.
    """

    __slots__ = ("pattern", "terms", "capped", "stemmed", "refused", "elapsed_s")

    def __init__(self, pattern: str, terms: Sequence[str] = (), *,
                 capped: bool = False, stemmed: bool = False,
                 refused: str = "", elapsed_s: float = 0.0) -> None:
        self.pattern = pattern
        self.terms = tuple(terms)
        #: More than `MAX_TERMS` matched and the rest were dropped.
        self.capped = capped
        #: The literal fragments were stemmed to meet the stored form, so the
        #: expansion is wider than what was typed.
        self.stemmed = stemmed
        #: Why nothing was attempted, when nothing was.
        self.refused = refused
        self.elapsed_s = elapsed_s

    @property
    def ok(self) -> bool:
        """Whether anything matched; a refused pattern is never ok."""
        return bool(self.terms)

    def message(self) -> str:
        r"""One line for the results pane. **The deliverable, not decoration.**

        An expansion that is capped or approximate is silence with extra steps,
        so each case gets its own sentence - and "no words in the index match
        that pattern" is deliberately different from "no documents matched",
        because the difference is the whole diagnosis.
        """
        if self.refused:
            return f"{self.pattern} — {self.refused}"
        if not self.terms:
            return (f"No words in the index match {self.pattern} — "
                    f"which is not the same as no documents matching.")
        shown = ", ".join(self.terms[:6])
        more = "…" if len(self.terms) > 6 else ""
        line = f"{self.pattern} matched {len(self.terms):,} term(s): {shown}{more}"
        if self.capped:
            line += (f" — more than {MAX_TERMS} matched, showing the "
                     f"{MAX_TERMS} most common")
        if self.stemmed:
            line += (" — also matches words sharing a stem, so it is wider "
                     "than what you typed")
        return line


def needs_expansion(term: str) -> bool:
    r"""Does this term need the vocabulary, or can FTS5 answer it directly?

    **A trailing `*` is not this feature.** `invoic*` is real FTS5 prefix
    matching, it has always worked, and routing it through here would make the
    commonest wildcard slower and approximate for no gain. Only a `*` that is
    not at the end, or any `?`, needs the vocabulary.

    A bare `*` or `?` is not a pattern and stays exactly as inert as it is
    today - nothing, rather than everything.
    """
    text = str(term or "")
    if not _WILDCARD.search(text):
        return False
    if not text.strip("*?"):
        return False                             # `*`, `??`, `***`
    if text.count("*") == 1 and text.endswith("*") and "?" not in text:
        return False                             # ordinary prefix search
    return True


def literal_length(term: str) -> int:
    """Characters that are not wildcards. The floor is applied to this."""
    return len(_WILDCARD.sub("", str(term or "")))


def like_pattern(term: str) -> str:
    r"""A term as a SQL `LIKE` pattern, with an explicit `ESCAPE '\'`.

    `*` becomes `%` and `?` becomes `_`. **A literal `%` or `_` the person typed
    is escaped first**, or a search for `100%` becomes a search for every term
    in the corpus - which is exactly the class of silent wrongness this module
    exists to remove.
    """
    out: list[str] = []
    for character in str(term or ""):
        if character in ("%", "_", "\\"):
            out.append("\\" + character)
        elif character == "*":
            out.append("%")
        elif character == "?":
            out.append("_")
        else:
            out.append(character)
    return "".join(out)


def stem_with(stemmer: Any, term: str) -> tuple[str, bool]:
    r"""The pattern with its literal fragments stemmed. `(pattern, changed)`.

    **Asked of FTS5 rather than reimplemented.** A second Porter implementation
    in Python would agree with SQLite's until the day it did not, and the day it
    did not would present as a wildcard that quietly matches nothing. `stemmer`
    is a callable taking a word and returning what FTS5 stores for it; the store
    supplies one backed by a scratch table using the same tokenizer.

    Only the fragments are stemmed, never the wildcards, and a fragment shorter
    than three characters is left alone - Porter does nothing useful with `ab`
    and stemming it can only lose an anchor.
    """
    text = str(term or "")
    pieces = re.split(r"([*?])", text)
    changed = False
    out: list[str] = []
    for piece in pieces:
        if piece in ("*", "?") or len(piece) < 3:
            out.append(piece)
            continue
        try:
            stemmed = stemmer(piece)
        except Exception:                        # noqa: BLE001 - a fragment
            out.append(piece)
            continue
        if stemmed and stemmed != piece.lower():
            changed = True
            out.append(stemmed)
        else:
            out.append(piece)
    return "".join(out), changed


def strip_wildcards(text: str) -> str:
    r"""The query as a sentence, for the embedder.

    **`*voice` is not a sentence.** The vector half embeds what was typed, and a
    dense model handed a star produces a vector for a star. The wildcards come
    out and the words stay, which is the closest thing to what the person meant.
    """
    return _WILDCARD.sub("", str(text or ""))


def expand(store: Any, term: str, *, limit: int = MAX_TERMS,
           budget_s: float = BUDGET_S,
           cache: Optional[dict] = None) -> Expansion:
    r"""One wildcard term, as the real terms it matches. **Never raises.**

    Ordered by document frequency, so if the cap bites it is the commonest real
    words that survive rather than an arbitrary alphabetical slice.

    A pattern matching nothing returns nothing - **never a fallback to the
    literal text**. Quietly searching for something else is the defect this
    module exists to remove, and reproducing it here would be worse than leaving
    the feature unbuilt.
    """
    pattern = str(term or "")
    if cache is not None and pattern in cache:
        return cache[pattern]

    def remember(found: "Expansion") -> "Expansion":
        if cache is not None:
            cache[pattern] = found
        return found

    if literal_length(pattern) < MIN_LITERAL_CHARS:
        return remember(Expansion(pattern, refused=(
            f"too little to go on. Give it at least {MIN_LITERAL_CHARS} "
            f"characters outside the wildcards.")))

    started = time.perf_counter()
    problems: list[str] = []
    try:
        stemmer = getattr(store, "fts_stem", None)
        wanted, stemmed = (stem_with(stemmer, pattern) if stemmer is not None
                           else (pattern, False))
        # **The budget goes down to the query.** It used to be checked here,
        # after the scan had already returned, which made it a report rather
        # than a limit: the one pattern it exists to contain - `*a*` over a
        # corpus with millions of distinct terms - was exactly the one that
        # ran to completion regardless.
        found = store.vocabulary_terms(
            like_pattern(wanted), limit=limit + 1,
            budget_s=budget_s, problems=problems)
    except Exception as exc:                     # noqa: BLE001 - one query
        log.warning("wildcard expansion failed for {}: {}", pattern, exc)
        return remember(Expansion(pattern, refused="could not be expanded"))

    elapsed = time.perf_counter() - started
    if problems:
        # Cut short, so `found` is empty rather than partial. Said out loud,
        # with the one thing that makes the next attempt work.
        return remember(Expansion(pattern, refused=(
            f"took longer than {budget_s:g}s to look up. Add another letter "
            f"or two outside the wildcard."), elapsed_s=elapsed))
    capped = len(found) > limit
    return remember(Expansion(
        pattern, tuple(found[:limit]), capped=capped, stemmed=stemmed,
        elapsed_s=elapsed))
