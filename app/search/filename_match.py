r"""A small bonus when the file's own name carries the query.

Layer: L4 - pure arithmetic over fused hits. No store, no clock, no I/O.

**The gap this closes.** `names:` already exists as an explicit filter - typed
on purpose, narrows hard. This is the implicit case: nobody typed `name:`, but
a plain word in the sentence happens to be sitting right there in the filename,
which is exactly the kind of thing an 8-year-old benchmark expects to just
work. `drawings of the pump station` should not need to beat
`pump-station-drawings.pdf` on content alone when the title already gave the
answer away.

**Folded into `rrf_score` itself, deliberately - unlike `recency.blend` and
`definitions.boost`, which only reorder and never persist.** Two independent
reorders that both read the untouched `rrf_score` do not blend at all: the one
that runs last simply throws the other away, which is exactly the bug
`engine.py`'s §4c note records having happened once already with
`definitions.boost` placed ahead of `recency.blend`. Writing the boosted value
back means whatever runs afterwards - `recency.blend` among them - composes on
top of it instead of silently discarding it. `definitions.boost` still reads
`rrf_score` last and still wins outright when a query is a bare symbol; a
document whose name already carries the query only makes that a stronger,
not a different, answer.

**A fixed constant, not a per-surface policy field - non-negotiable 11's own
escape hatch.** `recency_blend` earned a settings toggle because "prefer
recent files" is a real preference some people do not share. Nobody indexes
files named after what they are *not* looking for; there is no plausible
"please ignore the filename" preference to expose, so a control here would be
the sixty-first knob the rule exists to prevent. `definitions.boost` set the
precedent in this same file family.

**The weight is measured, the same way `recency.WEIGHT` was** - recall@1 over
the twenty built-in sentences, keyword-and-fusion only (`tests/fixtures/
evaluation.py`, the same corpus and the same restriction `recency`'s own test
uses, because the embedding model does not load in CI). `recency_blend` stayed
on at its own shipped default throughout, so this is the bonus's marginal
effect on top of recency, which is how the two actually run together:

    weight   recall@1        moved   lost   gained
    off       15/20 (75%)        -      -        -
    0.01      15/20 (75%)        0      0        0
    0.02      15/20 (75%)        1      0        0
    0.03      16/20 (80%)        2      0        1
    0.05      16/20 (80%)        2      0        1

0.03 is the threshold where "the pump station" starts returning
`pump-station-drawings.pdf` ahead of a report that only mentions pumps more
often. **Unlike `recency`, no cliff turned up on this corpus** - the sweep was
run all the way to 10.0 with the same 16/20 and the same two rows moved, one of
them a harmless reshuffle among two already-wrong answers. That is read as a
property of a 21-document fixture with only one filename genuinely worth
matching, not as licence to pick a large number: production names are noisier,
a substring match is a much weaker claim than a topic match, and the whole
point of "small" is to stay a tie-breaker rather than start deciding searches
on its own. 0.05 is kept, one step above the measured threshold rather than at
it, and in the same order of magnitude as `recency.WEIGHT`.
"""

from __future__ import annotations

from typing import Optional, Sequence

__all__ = ["basename", "matches", "blend", "WEIGHT"]

#: How much a filename match may add to `rrf_score`, as a fraction. See the
#: module docstring for the sweep that chose it.
WEIGHT = 0.05


def basename(path: str) -> str:
    """The last path component, whichever separator it uses.

    Not `Path(path).name`: these paths are written on Windows and may be read
    back anywhere, and `PurePosixPath` would treat a whole `D:\a\b.pdf` as
    one filename. Splitting on both separators is the portable answer -
    `app.storage.sqlite_store._basename` makes the same call for the same
    reason, and is not imported from here to avoid a storage-into-search
    dependency for four lines of stdlib.
    """
    text = str(path or "")
    return text.replace("\\", "/").rstrip("/").rpartition("/")[2] or text


def _contains_word(haystack: str, needle: str) -> bool:
    """Whole-word containment, case-insensitive. Not `in`: "PI" inside
    "PIPELINE" says nothing about the two being the same word."""
    if not needle:
        return False
    haystack = haystack.lower()
    needle = needle.lower()
    start = haystack.find(needle)
    while start != -1:
        before_ok = start == 0 or not haystack[start - 1].isalnum()
        after = start + len(needle)
        after_ok = after == len(haystack) or not haystack[after].isalnum()
        if before_ok and after_ok:
            return True
        start = haystack.find(needle, start + 1)
    return False


def matches(path: str, terms: Sequence[str]) -> bool:
    """Whether any bare term the person typed appears, whole-word, in this
    file's own name. **Never raises.**"""
    name = basename(path)
    if not name:
        return False
    for term in terms:
        term = str(term or "").strip()
        if term and _contains_word(name, term):
            return True
    return False


def blend(hits: Sequence[dict], terms: Sequence[str], *,
          weight: Optional[float] = None,
          score_key: str = "rrf_score") -> list:
    r"""Re-order fused hits, lifting the ones whose filename carries a term.

    Returns a new list. Each hit is annotated with `filename_match` so the
    reason for an order is inspectable rather than mysterious - the same rule
    `recency` and `declares` follow - and, unlike those two, the boosted score
    is written back to `score_key` so a step that runs afterwards composes with
    this one instead of overwriting it. See the module docstring for why that
    matters here specifically.

    **Stable for equal blended scores**, so a corpus with no filename matches
    at all comes back in exactly the order it went in.
    """
    weight = WEIGHT if weight is None else float(weight)
    if not hits or not terms or weight <= 0:
        return list(hits)

    ranked = []
    for position, hit in enumerate(hits):
        try:
            score = float(hit.get(score_key) or 0.0)
        except (TypeError, ValueError):
            score = 0.0
        found = matches(str(hit.get("path") or ""), terms)
        hit["filename_match"] = found
        new_score = score * (1.0 + weight) if found else score
        hit[score_key] = new_score
        ranked.append((-new_score, position, hit))

    ranked.sort(key=lambda row: (row[0], row[1]))
    return [row[2] for row in ranked]
