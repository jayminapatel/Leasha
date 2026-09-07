r"""Among equally good matches, the newer one first.

Layer: L4 — pure arithmetic over fused hits. No store, no clock beyond the one
passed in.

**This is a nudge, not a sort.** `/newest` already exists and abandons
relevance order deliberately, saying so on the page. This is the opposite
trade: relevance stays in charge, and age breaks near-ties. The reason people
want it is that a corpus of fifteen years contains eight versions of the same
letter, and the one written last week is almost always the one meant - but
"almost always" is not "always", which is why an older document that matches
much better must still win.

**The strength is a measured number, not a taste.** Weights were run against
the twenty built-in sentences through the real engine; see `WEIGHT` for the
table. The value written here before that run was twice too strong and would
have cost five points of recall@1 - which is the whole argument for the rule
that a ranking change without its measurement is not done.
"""

from __future__ import annotations

import time
from typing import Any, Optional, Sequence

__all__ = ["blend", "freshness", "WEIGHT", "HALF_LIFE_DAYS"]

#: How much a brand-new document may add to its own fused score, as a
#: fraction.
#:
#: **Measured, and the measurement overruled the guess.** Recall@1 over the
#: twenty built-in sentences, through the real engine (see the §2d note in
#: `WORKORDER-202626270157-search-experience.md`):
#:
#:     weight   recall@1        moved   lost   gained
#:     off      14/20  (70%)        -      -        -
#:     0.01     14/20  (70%)        0      0        0
#:     0.02     14/20  (70%)        0      0        0
#:     0.03     15/20  (75%)        1      0        1
#:     0.04     13/20  (65%)        3      2        1
#:     0.06     13/20  (65%)        3      2        1
#:     0.10      9/20  (45%)        7      6        1
#:     0.30      7/20  (35%)       11      8        1
#:
#: 0.06 was the value written here before the run, and it would have cost five
#: points. **The band that helps is narrow and it has a cliff at 0.04**: below
#: 0.03 nothing moves at all, above it good answers start being displaced by
#: newer documents on the same topic.
#:
#: The one sentence 0.03 fixes is *"what did I send to Priya"*, which used to
#: return an invoice template and now returns the mail actually sent to her -
#: precisely the case the feature exists for, where topic words tie and the
#: date decides.
#:
#: For scale: RRF at `k=60` scores rank 1 at 0.0164 and rank 2 at 0.0161, so
#: three percent is worth a few of those gaps at the top and fewer further
#: down, where the curve flattens. That is the intended reach - a few places,
#: never the whole page.
WEIGHT = 0.03

#: Days for freshness to halve. Six months, because that is roughly where
#: "recent" stops meaning anything in a working corpus: last week and last
#: month are both *now*, and 2019 and 2015 are both *ages ago*.
HALF_LIFE_DAYS = 180.0

_DAY_NS = 86_400 * 1_000_000_000


def freshness(mtime_ns: Any, *, now_ns: Optional[int] = None) -> float:
    """How new this is, from 1.0 (today) decaying towards 0.0. **Never
    raises**, and returns 0.0 for anything undatable.

    A missing or zero `mtime_ns` is treated as ancient rather than as new.
    Files whose date could not be read are a real population - archives,
    network shares, anything the walk could not stat - and floating them to
    the top of every search on the strength of a missing value would be the
    sentinel bug that hid every PST, repeated in the ranking.
    """
    try:
        stamp = int(mtime_ns or 0)
    except (TypeError, ValueError):
        return 0.0
    if stamp <= 0:
        return 0.0
    current = int(now_ns if now_ns is not None else time.time_ns())
    age_days = (current - stamp) / _DAY_NS
    if age_days <= 0:
        # Clock skew, or a file saved a moment ago. Newest is newest.
        return 1.0
    return float(0.5 ** (age_days / HALF_LIFE_DAYS))


def blend(hits: Sequence[dict], *, weight: Optional[float] = None,
          now_ns: Optional[int] = None,
          score_key: str = "rrf_score") -> list:
    r"""Re-order fused hits by score plus a mild age bonus.

    Returns a new list; the hits themselves are annotated with `recency` so
    the reason for an order is inspectable rather than mysterious - the same
    rule the scores column follows.

    **Stable for equal blended scores**, so a corpus with no dates at all
    comes back in exactly the order it went in. That is the case a switched-on
    behaviour must not disturb.

    **Reads `taken_at_ns` before `mtime_ns` - work order 0f §3a's third
    clause.** This is a display-facing nudge, not the change-detection
    `mtime_ns` also serves elsewhere: the "recency" this produces is shown
    back to the person, verbatim, in `app.ui.presenter.why_result`'s "Recent,
    so it came slightly ahead of equally good older ones" line, so a photo
    copied in 2019 but shot in 2006 must not be nudged up as if it were a
    2019 document, and must not be narrated to the person as recent when it
    is not.
    """
    # **Read at call time, not bound as a default.** A default argument is
    # evaluated once when the function is defined, so `weight=WEIGHT` would
    # freeze the constant at import - and the measurement that chose it would
    # have reported the same number for every weight it tried. It did, until
    # this line; see the §2d note in the search-experience order.
    weight = WEIGHT if weight is None else float(weight)
    if not hits or weight <= 0:
        return list(hits)
    stamp = int(now_ns if now_ns is not None else time.time_ns())

    ranked = []
    for position, hit in enumerate(hits):
        try:
            score = float(hit.get(score_key) or 0.0)
        except (TypeError, ValueError):
            score = 0.0
        new = freshness(hit.get("taken_at_ns") or hit.get("mtime_ns"), now_ns=stamp)
        hit["recency"] = new
        # **Multiplicative, so the bonus scales with how good the match was.**
        # An additive bonus is worth the same to the best hit and to the
        # fiftieth, which at the tail of an RRF curve is enough to lift
        # something barely relevant over something that answered the question.
        ranked.append((-(score * (1.0 + weight * new)), position, hit))

    ranked.sort(key=lambda row: (row[0], row[1]))
    return [row[2] for row in ranked]
