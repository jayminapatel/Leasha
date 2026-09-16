r"""A small bonus when the file's own name carries the query.

Layer: L4. Work order 0 section 6, "Relevance": blend a small recency decay
plus filename-match bonus into the fused score; /newest should not be the
only way to prefer this decade. The recency half shipped separately, in
order 202626270157 section 2d (app.search.recency); this is the
filename-match half.

The arithmetic needs almost nothing; the last test runs the twenty built-in
sentences through the real engine, the same way test_recency.py's own
closing test does, because the weight is a measured number and a test is
the only thing that keeps it measured.
"""

from __future__ import annotations

import pathlib
import tempfile

import pytest

from app.search import filename_match
from app.search.filename_match import WEIGHT, basename, blend, matches

# --------------------------------------------------------------------------
# basename
# --------------------------------------------------------------------------


def test_basename_handles_both_separators():
    windows_path = 'D:\\Docs\\Engineering\\pump-station-drawings.pdf'
    assert basename(windows_path) == "pump-station-drawings.pdf"
    assert basename("D:/Docs/Engineering/pump-station-drawings.pdf") == (
        "pump-station-drawings.pdf")


def test_basename_of_a_bare_filename_is_itself():
    assert basename("readme.txt") == "readme.txt"


@pytest.mark.parametrize("value", ["", None])
def test_basename_never_raises_on_nothing(value):
    assert basename(value) == ""


# --------------------------------------------------------------------------
# matches
# --------------------------------------------------------------------------


def test_a_term_inside_the_filename_matches():
    assert matches("D:/Docs/pump-station-drawings.pdf", ["pump"])


def test_matching_is_whole_word_not_substring():
    assert not matches("D:/Docs/pipeline-notes.pdf", ["pi"])
    assert matches("D:/Docs/pi-report.pdf", ["pi"])


def test_matching_is_case_insensitive():
    assert matches("D:/Docs/INVOICE-2024.pdf", ["invoice"])


def test_a_term_only_in_the_folder_does_not_match():
    assert not matches("D:/Invoices/2024/march.pdf", ["invoice", "invoices"])


def test_no_terms_never_matches():
    assert not matches("D:/Docs/pump-station.pdf", [])


def test_matches_never_raises_on_a_missing_path():
    assert not matches("", ["pump"])
    assert not matches(None, ["pump"])


# --------------------------------------------------------------------------
# blend
# --------------------------------------------------------------------------


def _hit(chunk_id, score, path):
    return {"chunk_id": chunk_id, "rrf_score": score, "path": path}


def test_a_filename_match_can_lift_a_near_tie():
    named = _hit(1, 0.0164, "D:/Docs/pump-station-drawings.pdf")
    unnamed = _hit(2, 0.0163, "D:/Docs/commissioning-notes.pdf")
    order = [h["chunk_id"] for h in blend([named, unnamed], ["pump", "station"])]
    assert order == [1, 2]


def test_a_much_better_match_still_wins():
    much_better_unnamed = _hit(1, 0.0164, "D:/Docs/commissioning-notes.pdf")
    weak_but_named = _hit(2, 0.0020, "D:/Docs/pump-station.pdf")
    order = [h["chunk_id"] for h in
             blend([much_better_unnamed, weak_but_named], ["pump", "station"])]
    assert order == [1, 2]


def test_a_corpus_with_no_matches_comes_back_untouched():
    hits = [_hit(n, 0.01, "readme.txt") for n in range(6)]
    order = [h["chunk_id"] for h in blend(hits, ["pump"])]
    assert order == [0, 1, 2, 3, 4, 5]


def test_weight_zero_is_the_identity():
    r"""**"Identity" means the input order, not a re-sort by score.** Like
    `recency.blend`, a non-positive weight is a fast exit that returns the
    list exactly as given - it does not re-rank first."""
    unnamed = _hit(2, 0.011, "D:/Docs/other.pdf")
    named = _hit(1, 0.010, "D:/Docs/pump.pdf")
    order = [h["chunk_id"] for h in
             blend([unnamed, named], ["pump"], weight=0)]
    assert order == [2, 1]


def test_no_terms_is_the_identity():
    hits = [_hit(2, 0.011, "other.pdf"), _hit(1, 0.010, "D:/Docs/pump.pdf")]
    assert [h["chunk_id"] for h in blend(hits, [])] == [2, 1]


def test_the_weight_is_read_at_call_time_not_bound_as_a_default(monkeypatch):
    unnamed = _hit(2, 0.011, "other.pdf")
    named = _hit(1, 0.010, "D:/Docs/pump.pdf")
    monkeypatch.setattr(filename_match, "WEIGHT", 0.0)
    assert [h["chunk_id"] for h in blend([unnamed, named], ["pump"])] == [2, 1]
    monkeypatch.setattr(filename_match, "WEIGHT", 1.0)
    assert [h["chunk_id"] for h in blend([unnamed, named], ["pump"])] == [1, 2]


def test_the_reason_for_the_order_is_left_on_the_hit():
    named = _hit(1, 0.01, "D:/Docs/pump.pdf")
    unnamed = _hit(2, 0.01, "other.pdf")
    result = blend([named, unnamed], ["pump"])
    by_id = {h["chunk_id"]: h for h in result}
    assert by_id[1]["filename_match"] is True
    assert by_id[2]["filename_match"] is False


def test_the_boosted_score_is_persisted_so_later_steps_compose():
    hit = _hit(1, 0.010, "D:/Docs/pump-station.pdf")
    result = blend([hit], ["pump"])[0]
    assert result["rrf_score"] == pytest.approx(0.010 * (1.0 + WEIGHT))


def test_an_unmatched_hits_score_is_left_alone():
    hit = _hit(1, 0.010, "other.pdf")
    result = blend([hit], ["pump"])[0]
    assert result["rrf_score"] == pytest.approx(0.010)


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
    """Recall@1 over the twenty built-in sentences must not fall.

    recency_blend stays at its own shipped default (on) throughout, because
    that is how the two actually run in production - this measures the
    filename bonus's marginal contribution on top of it: 15/20 with recency
    alone, 16/20 with this bonus added. The sentence that moves is
    "drawings of the pump station", which now returns
    pump-station-drawings.pdf rather than the commissioning report that
    only mentions pumps more often.

    Keyword and fusion only - the embedding model cannot load in CI - which
    is the same pipeline evaluate --builtin measures and the same one the
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
    policy = for_surface(SEARCH)
    try:
        def recall(weight):
            filename_match.WEIGHT = weight
            hit = []
            for question in QUESTIONS:
                found = engine.search(question.sentence, policy=policy,
                                      use_cache=False)
                path = found.results[0].path if found.results else ""
                hit.append(question.expects in path)
            return sum(hit), len(hit)

        try:
            without, total = recall(0.0)
            with_bonus, _ = recall(WEIGHT)
        finally:
            filename_match.WEIGHT = WEIGHT
    finally:
        engine.close()

    assert (without, total) == (15, 20), "the baseline moved - re-measure"
    assert with_bonus >= without, (
        "the filename bonus now costs recall (%d/%d against %d/%d). "
        "Re-run the weight sweep before shipping it."
        % (with_bonus, total, without, total))


def test_an_explicit_sort_is_not_disturbed_by_a_filename_match():
    """Only when relevance is still in charge - recency.blend's own rule at
    its call site in engine.py, and this bonus is skipped by the same
    "if not parsed.sort" guard, for the same reason: nudging within an order
    that has already abandoned relevance for a date sort is arithmetic with
    no effect. A query that names a file directly but asks for /oldest must
    come back in plain date order.

    Two words, deliberately - "pump station" rather than "pump" alone, so
    this is not also exercising definitions.looks_like_symbol, which only
    ever fires on a single bare word and has its own, separate, claim to
    running last.
    """
    from app.search.commands import expand_slashes
    from app.search.engine import SearchEngine
    from app.storage.sqlite_store import SqliteStore
    from tests.fixtures.evaluation import load_into

    folder = pathlib.Path(tempfile.mkdtemp())
    store = SqliteStore(folder / "evaluate.db").connect()
    load_into(store)
    engine = SearchEngine(store, _NoVectors(), _NoModel())
    try:
        found = engine.search(expand_slashes("pump station /oldest"),
                              use_cache=False)
        dates = [r.mtime_ns for r in found.results if r.mtime_ns]
        assert dates == sorted(dates), (
            "an explicit /oldest sort must stay a pure date order")
    finally:
        engine.close()
