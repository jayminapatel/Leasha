r"""A word that matches nothing, and the real word it was probably meant to be.

Layer: L4 — the arithmetic is pure; the vocabulary arrives through a callable.

**The single highest-value item in the search-experience order, and the reason
is not subtle.** A child types "volcanoe", gets nothing, and concludes her
essay is gone. She does not conclude that the search box is fussy about
spelling - she has no model in which that is a thing that happens - so the
outcome of one transposed letter is a lost piece of homework and a tool she
stops trusting.

**Only for a word the index has never seen.** A word that matches something is
never corrected, however unusual: `recieve` may be a genuine misspelling *in
the corpus*, and finding the document somebody actually wrote is the whole job.
This fires exclusively where the alternative is zero results, which is what
makes it safe to do at all.

**Bounded exactly as wildcards are**, and for the same reason: this runs on a
keystroke. The vocabulary is fetched by prefix through the same budgeted call
`wildcards` uses, capped by document frequency, and the distance itself gives
up as soon as it exceeds what would be accepted.

**It reads the corpus, not a dictionary.** The candidate has to be a word the
person's own documents contain - so "leasha" corrects to "leasha" if that is
what they write, and no English word list can second-guess a surname, a project
code or a Welsh place name. That is also why it needs no data file.

*Known limit, stated rather than discovered:* candidates are found by prefix,
so a typo in the **first two letters** finds nothing. `vlocano` is not
corrected. Fixing that means scanning far more of the vocabulary on every
keystroke, and the trade is not obviously worth it - the common typo is a
transposition or a doubled letter later in the word.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional, Sequence

__all__ = [
    "Suggestion",
    "edit_distance",
    "best_match",
    "suggest",
    "MIN_LENGTH",
    "MAX_CANDIDATES",
]

#: Shorter than this is never corrected.
#:
#: **Because "cat" and "cut" are different words, not a typo.** At three
#: letters almost every string is one edit from several real words, so a
#: correction there is a coin toss presented as help - and the person who
#: typed a short word usually knows what it was.
MIN_LENGTH = 4

#: Words at least this long may be corrected by two edits rather than one.
#: Below it, two edits is a third of the word.
LONG_WORD = 7

#: How many vocabulary terms are considered. Ordered by document frequency, so
#: the cap keeps the words the corpus actually uses rather than an alphabetical
#: slice - the same reasoning as the wildcard cap.
MAX_CANDIDATES = 200

#: Letters of prefix used to fetch candidates. Three is the balance: enough to
#: keep the scan small, few enough that the usual typo - later in the word -
#: still shares it.
PREFIX = 3


@dataclass(frozen=True)
class Suggestion:
    """One correction, and enough to explain it."""

    typed: str
    #: The real term from the corpus.
    suggestion: str
    distance: int

    def sentence(self) -> str:
        """What the results header says. **Plain, and past tense for `auto`.**

        "also looked for 'volcanoes'" tells somebody what happened and implies
        it was reasonable. "Did you mean…" asks a question the results have
        already answered, which reads as hesitancy about results that are
        actually on screen.
        """
        return f"also looked for '{self.suggestion}'"

    def question(self) -> str:
        """What the chip says when the policy is `suggest` - a real question,
        because nothing has been done yet."""
        return f"Did you mean '{self.suggestion}'?"


def edit_distance(left: str, right: str, ceiling: int = 2) -> int:
    r"""Levenshtein distance, **giving up once it passes `ceiling`**.

    Returns `ceiling + 1` for anything further apart, which is all the caller
    needs to know. The early exit is what makes this affordable against two
    hundred candidates on every keystroke: a full matrix for two long strings
    is thousands of comparisons, and the answer is almost always "too far".

    A length difference greater than the ceiling is decided without looking at
    the letters at all.
    """
    left, right = str(left), str(right)
    if left == right:
        return 0
    if abs(len(left) - len(right)) > ceiling:
        return ceiling + 1
    if not left or not right:
        return min(len(left) or len(right), ceiling + 1)

    previous = list(range(len(right) + 1))
    for i, a in enumerate(left, start=1):
        current = [i]
        best = i
        for j, b in enumerate(right, start=1):
            cost = 0 if a == b else 1
            value = min(previous[j] + 1,        # deletion
                        current[j - 1] + 1,     # insertion
                        previous[j - 1] + cost)  # substitution
            current.append(value)
            best = min(best, value)
        if best > ceiling:
            # **Every path through this row is already too far.** Nothing
            # below can bring it back, because the distance never decreases.
            return ceiling + 1
        previous = current
    return min(previous[-1], ceiling + 1)


def _ceiling_for(word: str) -> int:
    """One edit for an ordinary word, two for a long one."""
    return 2 if len(word) >= LONG_WORD else 1


def best_match(word: str, candidates: Sequence[str]) -> Optional[Suggestion]:
    r"""The closest real word, or None when none is close enough.

    **Candidates arrive in document-frequency order and that order is kept for
    ties.** Two words at the same distance are not equally good: the one the
    corpus uses more is far likelier to be what was meant, and preferring it
    costs nothing because the caller already fetched them that way.
    """
    typed = str(word or "").strip().lower()
    if len(typed) < MIN_LENGTH:
        return None

    ceiling = _ceiling_for(typed)
    best: Optional[Suggestion] = None
    for candidate in candidates:
        other = str(candidate or "").strip().lower()
        if not other or other == typed:
            # Identical means the word *is* in the vocabulary, and this should
            # never have been called. Refusing rather than "correcting" it to
            # itself keeps that mistake visible upstream.
            return None
        distance = edit_distance(typed, other, ceiling)
        if distance > ceiling:
            continue
        if best is None or distance < best.distance:
            best = Suggestion(typed, other, distance)
            if distance == 1:
                # **Nothing can beat one edit, and frequency order means this
                # is the commonest word at that distance.** Stopping here is
                # not an optimisation, it is the answer.
                break
    return best


def suggest(word: str, lookup: Callable[[str], Sequence[str]],
            *, limit: int = MAX_CANDIDATES) -> Optional[Suggestion]:
    """A correction for one unmatched word. **Never raises.**

    `lookup` takes a `LIKE` pattern and returns terms, commonest first - the
    store's `vocabulary_terms`. Injected rather than imported so every rule
    above is testable with a list, on a machine with no index.
    """
    typed = str(word or "").strip().lower()
    if len(typed) < MIN_LENGTH:
        return None

    try:
        # Two passes: the word's own prefix, then one letter shorter. The
        # second catches a typo *at* the third letter, which the first cannot
        # see - "volcaneo" against "volcano" shares only two.
        seen: list = []
        for size in (PREFIX, PREFIX - 1):
            if size < 2:
                break
            found = lookup(typed[:size] + "%") or []
            seen = list(found)[:limit]
            match = best_match(typed, seen)
            if match is not None:
                return match
    except Exception:                            # noqa: BLE001 - a suggestion
        # A vocabulary that cannot be read is a search without a spelling
        # suggestion, never a search that fails. This runs on a keystroke.
        return None
    return None


def from_store(store: Any, word: str, *, budget_s: float = 0.05,
               limit: int = MAX_CANDIDATES) -> Optional[Suggestion]:
    """`suggest` against a real index, with the wildcard budget applied.

    50ms, because this happens while somebody is typing and the whole
    keystroke has about 150 to spend. A vocabulary scan that overran would
    turn a helpful correction into a stutter, which is a worse trade than not
    correcting at all.
    """
    terms = getattr(store, "vocabulary_terms", None)
    if terms is None:
        return None

    def lookup(pattern: str) -> Sequence[str]:
        return terms(pattern, limit=limit, budget_s=budget_s)

    return suggest(word, lookup, limit=limit)
