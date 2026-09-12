r"""Among equally good matches, the one whose *name* already says it first.

Layer: L4. The arithmetic needs nothing; the last test runs the twenty
built-in sentences through the real engine, because **the weight is a
measured number and a test is the only thing that keeps it measured** - the
same rule `test_recency.py` enforces for the sibling half of this same
"Relevance" item (`docs/WORKORDER-202626082352-review-remediation.md` §6).

The measurement that chose it, recall@1 over those sentences - see `WEIGHT`'s
own docstring in `app/search/name_match.py` for the full sweep and why 0.15
rather than a higher weight that costs nothing further in this sample:

    weight   recall@1        moved   lost   gained
    off      14/20  (70%)        -      -        -
    0.03     14/20  (70%)        0      0        0
    0.05     15/20  (75%)        1      0        1
    0.15     16/20  (80%)        2      0        2
"""

from __future__ import annotations

import pathlib
import tempfile

import pytest

from app.search import name_match
from app.search.name_match import WEIGHT, blend, name_overlap

_DAY_NS = 86_400 * 1_000_000_000
_NOW = 1_700_000_000 * 1_000_000_000

# --------------------------------------------------------------------------
# name_overlap
# --------------------------------------------------------------------------

def test_every_term_found_is_a_full_overlap():
    assert name_overlap("C:/work/pump station drawings.pdf",
                        ["pump", "station"]) == pytest.approx(1.0)


def test_half_the_terms_found_is_a_half_overlap():
    assert name_overlap("C:/work/pump-station.pdf",
                        ["pump", "invoice"]) == pytest.approx(0.5)


def test_no_terms_found_is_zero():
    assert name_overlap("C:/work/report.pdf", ["invoice", "budget"]) == 0.0


def test_no_terms_at_all_is_zero():
    assert name_overlap("C:/work/report.pdf", []) == 0.0


def test_matching_is_case_insensitive():
    assert name_overlap("C:/work/PUMP-STATION.PDF", ["pump"]) == pytest.approx(1.0)


def test_the_folder_does_not_count_only_the_filename():
    """"invoice" inside `Invoices\\2024.pdf` is a match on the folder, not on
    this document - folding it in would reward every file living in a
    well-named directory whatever it is itself called."""
    assert name_overlap(r"C:\Invoices\2024.pdf", ["invoice"]) == 0.0


def test_blank_terms_are_never_counted_as_matched():
    assert name_overlap("C:/work/report.pdf", ["", "  "]) == 0.0


# --------------------------------------------------------------------------
# The blend
# --------------------------------------------------------------------------

def _hit(chunk_id: int, score: float, path: str) -> dict:
    return {"chunk_id": chunk_id, "rrf_score": score, "path": path}


def test_a_near_tie_goes_to_the_matching_filename():
    """The whole feature, in one assertion."""
    plain = _hit(1, 0.0164, "C:/work/quarterly-notes.docx")
    named = _hit(2, 0.0163, "C:/work/budget-review.docx")
    order = [h["chunk_id"] for h in blend([plain, named], ["budget", "review"])]
    assert order == [2, 1]


def test_a_much_better_match_still_wins():
    """**It never hides another document, it only orders them** - the
    behaviour's own tooltip. A five percent nudge cannot cross a real gap in
    relevance."""
    much_better_unnamed = _hit(1, 0.0164, "C:/work/report.docx")
    slightly_worse_named = _hit(2, 0.0100, "C:/work/budget-review.docx")
    order = [h["chunk_id"] for h in blend(
        [much_better_unnamed, slightly_worse_named], ["budget", "review"])]
    assert order == [1, 2]


def test_the_bonus_scales_with_how_good_the_match_was():
    """**Multiplicative, not additive** - the same reasoning as `recency.
    blend`: an additive bonus is worth the same to the best hit and to the
    fiftieth, which at the tail of an RRF curve is enough to lift something
    barely relevant over something that answered the question."""
    strong, weak = _hit(1, 0.0164, "C:/x/plain.txt"), _hit(2, 0.0020, "C:/x/budget-review.txt")
    order = [h["chunk_id"] for h in blend([strong, weak], ["budget", "review"])]
    assert order == [1, 2]


def test_a_corpus_with_no_matching_filenames_comes_back_untouched():
    hits = [_hit(n, 0.01, f"C:/x/file{n}.txt") for n in range(6)]
    order = [h["chunk_id"] for h in blend(hits, ["nonexistentword"])]
    assert order == [0, 1, 2, 3, 4, 5]


def test_no_terms_is_the_identity():
    hits = [_hit(1, 0.01, "C:/x/a.txt"), _hit(2, 0.02, "C:/x/b.txt")]
    assert blend(hits, []) == hits


def test_weight_zero_is_the_identity():
    plain = _hit(1, 0.01, "C:/x/plain.txt")
    named = _hit(2, 0.0099, "C:/x/budget.txt")
    order = [h["chunk_id"] for h in blend([plain, named], ["budget"], weight=0)]
    assert order == [1, 2]


def test_the_weight_is_read_at_call_time_not_bound_as_a_default(monkeypatch):
    r"""**The bug `recency.blend`'s own docstring records finding.**
    `weight=WEIGHT` in the signature is evaluated once, when the function is
    defined - so a sweep that mutated the module constant between calls would
    report the same number every time."""
    hits = [_hit(1, 0.0164, "C:/x/plain.txt"), _hit(2, 0.0163, "C:/x/budget.txt")]
    monkeypatch.setattr(name_match, "WEIGHT", 0.0)
    assert [h["chunk_id"] for h in blend(hits, ["budget"])] == [1, 2]
    monkeypatch.setattr(name_match, "WEIGHT", 0.5)
    assert [h["chunk_id"] for h in blend(hits, ["budget"])] == [2, 1]


def test_the_reason_for_the_order_is_left_on_the_hit():
    """`name_match` is annotated so an order is inspectable rather than
    mysterious - the same rule `recency`'s own `recency` field follows."""
    hits = [_hit(1, 0.01, "C:/x/budget-review.txt")]
    assert blend(hits, ["budget", "review"])[0]["name_match"] == pytest.approx(1.0)


# --------------------------------------------------------------------------
# Both nudges together, without one discarding the other
# --------------------------------------------------------------------------

def test_recency_and_filename_match_compound_rather_than_one_winning():
    r"""**The bug this regression-tests.** Chaining `recency.blend` then
    `name_match.blend` had the second silently discard the first: neither
    writes its multiplied score back to `rrf_score`, so the second always
    re-sorts the whole list from the same untouched original score - and two
    identically-worded documents differ in raw score only by their RRF rank
    (`1/61` versus `1/62`), which is enough to win that re-sort even after
    the first blend had correctly reordered them. Found live: a query for
    two documents with identical text, one newer, kept coming back with the
    older one first the moment a filename bonus was wired in beside recency,
    on a pair whose filenames did not even mention the query.

    `engine.SearchEngine` runs `_blend_recency_and_filename` instead of the
    two calls in sequence whenever both behaviours are on - this is that
    function, tested directly.
    """
    from app.search.engine import _blend_recency_and_filename

    old_unnamed = {"chunk_id": 1, "rrf_score": 0.0164, "mtime_ns": _NOW - 3000 * _DAY_NS,
                  "path": "C:/work/old-notes.txt"}
    new_unnamed = {"chunk_id": 2, "rrf_score": 0.0161, "mtime_ns": _NOW,
                  "path": "C:/work/new-notes.txt"}
    order = [h["chunk_id"] for h in _blend_recency_and_filename(
        [old_unnamed, new_unnamed], ["lava", "flows"], now_ns=_NOW)]
    assert order == [2, 1], "recency must still win when no filename matches either way"

    # Now give the *older* one a matching filename too - both nudges pulling
    # the same direction should make it win even more clearly than recency
    # alone would, not have the filename bonus quietly vanish.
    old_named = {"chunk_id": 1, "rrf_score": 0.0164, "mtime_ns": _NOW - 3000 * _DAY_NS,
                "path": "C:/work/lava-flows-notes.txt"}
    order = [h["chunk_id"] for h in _blend_recency_and_filename(
        [old_named, new_unnamed], ["lava", "flows"], now_ns=_NOW)]
    assert order == [1, 2], "a real filename match must still be able to win"


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

    A ranking change without its measurement is not done - see `test_recency.
    test_the_shipped_weight_still_earns_its_place` for the sibling half of
    this same item. Keyword and fusion only - the embedding model cannot
    load in CI - which is the same pipeline `evaluate --builtin` measures and
    the same one the weight was chosen against.
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
            # `recency_blend` isolated off: it is the sibling half of this
            # same "Relevance" item and defaults on, and this measures the
            # filename bonus alone against the baseline recency's own test
            # was chosen against - see `test_recency.py` for the mirror.
            policy = for_surface(SEARCH).with_overrides(
                filename_match_blend=blend_on, recency_blend=False)
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
        f"the filename-match blend now costs recall ({with_blend}/{total} "
        f"against {without}/{total}). Re-run the weight sweep before "
        f"shipping it; WEIGHT={WEIGHT}.")
    assert with_blend == 16, (
        f"recall moved to {with_blend}/{total} at the shipped weight - the "
        f"sweep in `name_match.WEIGHT`'s own docstring is now stale, "
        f"whichever direction it moved.")
