r"""Among equally good matches, the newer one first — and no further.

Layer: L4. The arithmetic needs nothing; the last test runs the twenty
built-in sentences through the real engine, because **the weight is a measured
number and a test is the only thing that keeps it measured.**

The measurement that chose it, recall@1 over those sentences:

    weight   recall@1        moved   lost   gained
    off      14/20  (70%)        -      -        -
    0.02     14/20  (70%)        0      0        0
    0.03     15/20  (75%)        1      0        1
    0.04     13/20  (65%)        3      2        1
    0.06     13/20  (65%)        3      2        1
    0.10      9/20  (45%)        7      6        1

0.06 was in the source before the run. It would have cost five points.
"""

from __future__ import annotations

import pathlib
import tempfile

import pytest

from app.search import recency
from app.search.recency import HALF_LIFE_DAYS, WEIGHT, blend, freshness

_DAY_NS = 86_400 * 1_000_000_000
_NOW = 1_700_000_000 * 1_000_000_000


def _aged(days: float) -> int:
    return int(_NOW - days * _DAY_NS)


# --------------------------------------------------------------------------
# Freshness
# --------------------------------------------------------------------------

def test_today_is_one_and_a_half_life_ago_is_a_half():
    assert freshness(_NOW, now_ns=_NOW) == pytest.approx(1.0)
    assert freshness(_aged(HALF_LIFE_DAYS), now_ns=_NOW) == pytest.approx(0.5)
    assert freshness(_aged(HALF_LIFE_DAYS * 2), now_ns=_NOW) == pytest.approx(0.25)


@pytest.mark.parametrize("value", [0, None, "", "not a date", -1])
def test_an_undatable_file_is_ancient_not_new(value):
    """**The sentinel bug that hid every PST, repeated in the ranking.**

    Files whose date could not be read are a real population - archives,
    network shares, anything the walk could not stat. Reading a missing value
    as "now" would float every one of them to the top of every search.
    """
    assert freshness(value, now_ns=_NOW) == 0.0


def test_a_file_from_the_future_is_simply_newest():
    """Clock skew and files saved a moment ago land here. Newest is newest;
    there is nothing to be gained by treating it as an error."""
    assert freshness(_aged(-5), now_ns=_NOW) == 1.0


# --------------------------------------------------------------------------
# The blend
# --------------------------------------------------------------------------

def _hit(chunk_id: int, score: float, days: float) -> dict:
    return {"chunk_id": chunk_id, "rrf_score": score, "mtime_ns": _aged(days)}


def test_a_near_tie_goes_to_the_newer_one():
    """The whole feature, in one assertion."""
    older, newer = _hit(1, 0.0164, 3000), _hit(2, 0.0163, 1)
    assert [h["chunk_id"] for h in blend([older, newer], now_ns=_NOW)] == [2, 1]


def test_a_much_better_match_still_wins():
    """**It never hides an older document, it only orders them** - the
    behaviour's own tooltip. A three percent nudge cannot cross a real gap in
    relevance, and if it could the feature would be a date sort wearing a
    disguise."""
    much_better_but_old = _hit(1, 0.0164, 4000)
    slightly_worse_new = _hit(2, 0.0100, 0)
    order = [h["chunk_id"] for h in blend([much_better_but_old,
                                           slightly_worse_new], now_ns=_NOW)]
    assert order == [1, 2]


def test_the_bonus_scales_with_how_good_the_match_was():
    """**Multiplicative, not additive.** An additive bonus is worth the same
    to the best hit and to the fiftieth, which at the tail of an RRF curve is
    enough to lift something barely relevant over something that answered the
    question."""
    strong, weak = _hit(1, 0.0164, 400), _hit(2, 0.0020, 0)
    blend([strong, weak], now_ns=_NOW)
    # The weak hit is newer, but three percent of 0.002 cannot reach 0.0164.
    assert [h["chunk_id"] for h in blend([strong, weak], now_ns=_NOW)] == [1, 2]


def test_a_corpus_with_no_dates_comes_back_untouched():
    """The case a switched-on behaviour must not disturb. Equal blended
    scores keep their original order, so nothing shuffles for no reason."""
    hits = [{"chunk_id": n, "rrf_score": 0.01, "mtime_ns": 0} for n in range(6)]
    assert [h["chunk_id"] for h in blend(hits, now_ns=_NOW)] == [0, 1, 2, 3, 4, 5]


def test_weight_zero_is_the_identity():
    hits = [_hit(1, 0.01, 3000), _hit(2, 0.009, 0)]
    assert [h["chunk_id"] for h in blend(hits, weight=0, now_ns=_NOW)] == [1, 2]


def test_the_weight_is_read_at_call_time_not_bound_as_a_default(monkeypatch):
    r"""**The bug that made the first measurement meaningless.**

    `weight=WEIGHT` in the signature is evaluated once, when the function is
    defined - so every weight the sweep tried reported the number for 0.06,
    and the table came back suspiciously identical on every row. It took a
    second look at the output to notice, which is the argument for pinning it
    here.
    """
    monkeypatch.setattr(recency, "WEIGHT", 0.0)
    hits = [_hit(1, 0.0164, 3000), _hit(2, 0.0163, 0)]
    assert [h["chunk_id"] for h in blend(hits, now_ns=_NOW)] == [1, 2]
    monkeypatch.setattr(recency, "WEIGHT", 0.5)
    assert [h["chunk_id"] for h in blend(hits, now_ns=_NOW)] == [2, 1]


def test_the_reason_for_the_order_is_left_on_the_hit():
    """`recency` is annotated so an order is inspectable rather than
    mysterious - the same rule the scores column follows."""
    hits = [_hit(1, 0.01, 0)]
    assert blend(hits, now_ns=_NOW)[0]["recency"] == pytest.approx(1.0)


# --------------------------------------------------------------------------
# The measurement, kept honest
# --------------------------------------------------------------------------

class _NoVectors:
    def search(self, *_args, **_kwargs):
        return []


class _NoModel:
    def embed(self, _text):
        raise RuntimeError("no embedding model in this test")

    def embed_all(self, _texts):
        raise RuntimeError("no embedding model in this test")


def test_the_shipped_weight_still_earns_its_place():
    r"""**Recall@1 over the twenty built-in sentences must not fall.**

    A ranking change without its measurement is not done - and a measurement
    taken once, at the moment of writing, decays into a claim. This runs it:
    70% with the blend off, 75% with it on at the shipped weight, and the
    sentence that improves is "what did I send to Priya", which used to return
    an invoice template and now returns the mail actually sent to her.

    Keyword and fusion only - the embedding model cannot load in CI - which is
    the same pipeline `evaluate --builtin` measures and the same one the
    weight was chosen against.
    """
    from app.search.engine import SearchEngine
    from app.search.policy import SEARCH, for_surface
    from app.storage.sqlite_store import SqliteStore
    from tests.fixtures.evaluation import QUESTIONS, load_into

    folder = pathlib.Path(tempfile.mkdtemp())
    store = SqliteStore(folder / "evaluate.db").connect()
    load_into(store)
    engine = SearchEngine(store, _NoVectors(), _NoModel())
    try:
        def recall(blend_on: bool) -> tuple:
            policy = for_surface(SEARCH).with_overrides(recency_blend=blend_on)
            hit = []
            for question in QUESTIONS:
                found = engine.search(question.sentence, policy=policy,
                                      use_cache=False)
                path = found.results[0].path if found.results else ""
                hit.append(question.expects in path)
            return sum(hit), len(hit)

        without, total = recall(False)
        with_blend, _ = recall(True)
    finally:
        engine.close()

    assert (without, total) == (14, 20), "the baseline moved - re-measure"
    assert with_blend >= without, (
        f"the recency blend now costs recall ({with_blend}/{total} against "
        f"{without}/{total}). Re-run the weight sweep before shipping it; "
        f"WEIGHT={WEIGHT} was chosen when 0.04 already lost two sentences.")
