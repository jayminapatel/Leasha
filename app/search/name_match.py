r"""Among equally good matches, the one whose *name* already says it first.

Layer: L4 — pure arithmetic over fused hits. No store, no clock, no I/O.

**The filename half of one item.** `docs/WORKORDER-202626082352-review-
remediation.md` §6 asks for "a small recency decay + filename-match bonus";
`recency.py` is the first half, measured and shipped in the search-experience
order. This is the second: a document whose own name already contains most of
what was typed - "site survey.pdf" for "site survey" - is a stronger signal
than the same words merely appearing somewhere in thousands of words of body
text, and neither BM25 nor the vector score treats a filename specially -
both fold it in as just more text to tokenise or embed, worth no more than
any other sentence in the document.

**A nudge, not a filter.** Exactly `recency.blend`'s own contract: relevance
stays in charge, and a real gap in relevance cannot be closed by a filename
alone - a document that matches on every meaningful word still beats a
half-relevant one whose filename happens to repeat the query.

**The strength is a measured number, not a taste.** See `WEIGHT` for the
sweep that chose it, run the same way and against the same twenty built-in
sentences as `recency.WEIGHT` was - a ranking change without its measurement
is not done.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional, Sequence

__all__ = ["blend", "name_overlap", "WEIGHT"]

#: How much a filename that already contains the query may add to its own
#: fused score, as a fraction. Multiplicative, for the same reason `recency.
#: WEIGHT` is: an additive bonus is worth the same to the best hit and to the
#: fiftieth, which at the tail of an RRF curve is enough to lift something
#: barely relevant over something that actually answered the question.
#:
#: **Measured against the twenty built-in sentences, through the real
#: engine** (see `tests/unit/test_name_match.py::
#: test_the_shipped_weight_still_earns_its_place`):
#:
#:     weight   recall@1        moved   lost   gained
#:     off      14/20  (70%)        -      -        -
#:     0.03     14/20  (70%)        0      0        0
#:     0.05     15/20  (75%)        1      0        1
#:     0.08     15/20  (75%)        1      0        1
#:     0.10     15/20  (75%)        1      0        1
#:     0.15     16/20  (80%)        2      0        2
#:     0.20     16/20  (80%)        2      0        2
#:     0.30     16/20  (80%)        2      0        2
#:
#: **Unlike `recency.WEIGHT`, nothing here costs recall as the weight climbs**
#: - `0.5`, `1.0`, `2.0` and `5.0` were also swept and none move a twenty-first
#: sentence, gained or lost. 0.15 is the smallest weight that reaches the
#: plateau: "what Dave asked about the licence" and "what did I send to
#: Priya" both used to return a topically-similar mail before the one whose
#: filename already named the sender and the subject. Chosen at the plateau
#: rather than higher up it on the same principle as `recency.WEIGHT` -
#: nothing here justifies a stronger multiplier than the evidence asks for,
#: even where the ceiling has more room.
WEIGHT = 0.15


def name_overlap(path: str, terms: Sequence[str]) -> float:
    """Fraction of `terms` found in `path`'s filename, case-insensitively.

    **The filename only, never the folder.** "invoice" inside
    `Invoices\\2024.pdf` is a match on the folder, not on this document, and
    folding it in would reward every file living in a well-named directory
    whatever it is itself called - the same distinction `recency.py`'s own
    `taken_at_ns`-before-`mtime_ns` note draws between what is true of a file
    and what is true of where it happens to sit.

    Terms are matched as substrings, the same way `group_subtitle`'s match
    marker and the highlighter already treat a query term against text -
    stemming or tokenising a filename would need the same machinery search
    already runs over document text, for a signal this small.
    """
    if not terms:
        return 0.0
    name = Path(str(path)).name.lower()
    if not name:
        return 0.0
    wanted = [str(term).strip().lower() for term in terms]
    wanted = [term for term in wanted if term]
    if not wanted:
        return 0.0
    matched = sum(1 for term in wanted if term in name)
    return matched / len(wanted)


def blend(hits: Sequence[dict], terms: Sequence[str], *,
          weight: Optional[float] = None, score_key: str = "rrf_score") -> list:
    r"""Re-order fused hits by score plus a mild filename-match bonus.

    Same shape as `recency.blend` - see that function's own docstring for why
    the bonus is multiplicative and why a set of hits with no distinguishing
    filename comes back exactly as it went in. Annotates each hit with
    `name_match`, the overlap fraction, so the order is inspectable rather
    than mysterious - the same rule the scores column and `recency` follow.

    **Read at call time, not bound as a default** - `weight=WEIGHT` in the
    signature would freeze the constant at import, which is the exact bug
    `recency.blend`'s own docstring records finding in its first measurement
    sweep: every weight tried would report the number for the one written at
    definition time.
    """
    weight = WEIGHT if weight is None else float(weight)
    if not hits or weight <= 0 or not terms:
        return list(hits)

    ranked = []
    for position, hit in enumerate(hits):
        try:
            score = float(hit.get(score_key) or 0.0)
        except (TypeError, ValueError):
            score = 0.0
        overlap = name_overlap(hit.get("path", ""), terms)
        hit["name_match"] = overlap
        ranked.append((-(score * (1.0 + weight * overlap)), position, hit))

    ranked.sort(key=lambda row: (row[0], row[1]))
    return [row[2] for row in ranked]
