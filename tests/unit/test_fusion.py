"""Layer 4: reciprocal rank fusion.

A pure function, so it can be pinned down completely: the arithmetic, the ordering,
the tie-breaking, and the one property that justifies choosing RRF at all - that
agreement between two retrievers beats a single retriever's top hit.
"""

from __future__ import annotations

import pytest

from app.search.fusion import RRF_K, fuse_hits, rrf


def test_k_matches_the_spec() -> None:
    assert RRF_K == 60


def test_score_is_the_documented_formula() -> None:
    (key, score), = rrf([["a"]])
    assert key == "a"
    assert score == pytest.approx(1 / (RRF_K + 1))


def test_scores_from_both_lists_are_summed() -> None:
    result = dict(rrf([["a", "b"], ["b", "a"]]))
    expected = 1 / (RRF_K + 1) + 1 / (RRF_K + 2)
    assert result["a"] == pytest.approx(expected)
    assert result["b"] == pytest.approx(expected)


def test_agreement_beats_a_single_top_hit() -> None:
    """The reason hybrid search works. If this fails, RRF is not earning its place.

    'agreed' is 8th in both lists and never first anywhere; 'loud' is 1st in one
    list and absent from the other. Agreement must still win.
    """
    keyword = [f"k{i}" for i in range(7)] + ["agreed"]
    vector = ["loud"] + [f"v{i}" for i in range(6)] + ["agreed"]
    ranked = [key for key, _ in rrf([keyword, vector])]
    assert ranked[0] == "agreed"
    assert ranked.index("agreed") < ranked.index("loud")


def test_ordering_is_best_first() -> None:
    ranked = [key for key, _ in rrf([["a", "b", "c"], ["a", "c"]])]
    assert ranked == ["a", "c", "b"], "c is in both lists, b in only one"


def test_mirrored_ranks_tie_and_break_stably() -> None:
    """b at ranks 2,3 and c at ranks 3,2 score identically - that is correct RRF.

    The tie is real, so the guarantee is only that it resolves the same way every
    time: by best rank, then by list order.
    """
    scores = dict(rrf([["a", "b", "c"], ["a", "c", "b"]]))
    assert scores["b"] == pytest.approx(scores["c"])
    assert [key for key, _ in rrf([["a", "b", "c"], ["a", "c", "b"]])] == ["a", "b", "c"]


def test_ties_break_deterministically_by_best_rank() -> None:
    """Equal scores must not order by dict insertion luck, or recall@10 drifts."""
    first = rrf([["x"], ["y"]])
    second = rrf([["x"], ["y"]])
    assert first == second
    assert [key for key, _ in first] == ["x", "y"]


def test_duplicates_within_one_ranking_do_not_inflate() -> None:
    assert dict(rrf([["a", "a", "a"]]))["a"] == pytest.approx(1 / (RRF_K + 1))


def test_weights_shift_the_balance() -> None:
    ranked = [key for key, _ in rrf([["kw"], ["vec"]], weights=[3.0, 1.0])]
    assert ranked == ["kw", "vec"]


def test_zero_weight_excludes_a_retriever() -> None:
    assert [key for key, _ in rrf([["kw"], ["vec"]], weights=[1.0, 0.0])] == ["kw"]


def test_limit_truncates() -> None:
    assert len(rrf([["a", "b", "c", "d"]], limit=2)) == 2


def test_empty_inputs() -> None:
    assert rrf([]) == []
    assert rrf([[], []]) == []


def test_one_empty_ranking_is_not_fatal() -> None:
    """Vector search returning nothing must leave keyword results intact."""
    assert [key for key, _ in rrf([["a", "b"], []])] == ["a", "b"]


def test_invalid_k_is_rejected_loudly() -> None:
    with pytest.raises(ValueError):
        rrf([["a"]], k=0)


def test_mismatched_weights_are_rejected_loudly() -> None:
    with pytest.raises(ValueError):
        rrf([["a"], ["b"]], weights=[1.0])


# --- fuse_hits --------------------------------------------------------------

def test_fuse_hits_marks_which_retrievers_agreed() -> None:
    keyword = [{"chunk_id": 1, "path": "a.pdf"}, {"chunk_id": 2, "path": "b.pdf"}]
    vector = [{"chunk_id": 2, "path": "b.pdf"}, {"chunk_id": 3, "path": "c.pdf"}]
    merged = fuse_hits([keyword, vector])
    by_id = {row["chunk_id"]: row for row in merged}
    assert by_id[2]["sources"] == (0, 1)
    assert by_id[1]["sources"] == (0,)
    assert by_id[2]["source_ranks"] == {0: 2, 1: 1}
    assert merged[0]["chunk_id"] == 2, "the agreed row should rank first"


def test_fuse_hits_fills_gaps_between_retrievers() -> None:
    """BM25 rows carry a snippet, ANN rows carry a distance. Keep both."""
    merged = fuse_hits([
        [{"chunk_id": 1, "snippet": "...survey..."}],
        [{"chunk_id": 1, "distance": 0.12}],
    ])
    assert merged[0]["snippet"] == "...survey..."
    assert merged[0]["distance"] == 0.12


def test_fuse_hits_does_not_mutate_its_input() -> None:
    keyword = [{"chunk_id": 1}]
    fuse_hits([keyword])
    assert keyword == [{"chunk_id": 1}]


def test_fuse_hits_skips_rows_missing_the_id() -> None:
    """Fusing malformed rows under None would merge them all into one result."""
    merged = fuse_hits([[{"chunk_id": 1}, {"path": "orphan.pdf"}]])
    assert len(merged) == 1
    assert merged[0]["chunk_id"] == 1


def test_fuse_hits_honours_a_custom_id_key() -> None:
    merged = fuse_hits([[{"file_id": 7}]], id_key="file_id")
    assert merged[0]["file_id"] == 7


def test_fuse_hits_scores_match_rrf() -> None:
    assert fuse_hits([[{"chunk_id": 1}]])[0]["rrf_score"] == pytest.approx(1 / (RRF_K + 1))
