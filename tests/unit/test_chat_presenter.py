r"""L8b §3: chat rendering decisions, kept Qt-free.

Layer: L5

Pure functions over `VerifiedSentence` - no widget, no display, matching
`test_the_presenter_still_does_not_import_qt`'s own discipline for the
search half of this module.
"""

from __future__ import annotations

from app.chat.verify import VerifiedSentence
from app.ui.chat_presenter import render_citations, sources_for


def _cited(text: str, *markers: int) -> VerifiedSentence:
    return VerifiedSentence(text=text, markers=tuple(markers), supported=True, similarity=0.9)


# ---------------------------------------------------------------------------
# render_citations
# ---------------------------------------------------------------------------

def test_a_single_citation_gets_one_superscript():
    html = render_citations([_cited("The deposit was 500 pounds.", 1)])
    assert html == "The deposit was 500 pounds.<sup>1</sup>"


def test_no_citations_is_an_empty_string():
    assert render_citations([]) == ""


def test_two_sentences_citing_the_same_chunk_share_one_number():
    html = render_citations([
        _cited("First claim.", 4),
        _cited("Second claim.", 4),
    ])
    assert html == "First claim.<sup>1</sup> Second claim.<sup>1</sup>"


def test_numbering_follows_first_mention_not_the_raw_marker():
    r"""**The property that matters.** The first sentence cites chunk 3 -
    that becomes source "1", the first (and only, so far) thing actually
    cited - not "3", which would leak an internal index a person has no way
    to make sense of."""
    html = render_citations([
        _cited("First claim.", 3),
        _cited("Second claim.", 1),
    ])
    assert html == "First claim.<sup>1</sup> Second claim.<sup>2</sup>"


def test_a_sentence_citing_two_chunks_shows_both_superscripts_sorted():
    html = render_citations([_cited("A claim.", 5, 2)])
    assert html == "A claim.<sup>1</sup><sup>2</sup>"


def test_sentence_text_is_html_escaped():
    html = render_citations([_cited("A <script> & more.", 1)])
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert "&amp;" in html


# ---------------------------------------------------------------------------
# sources_for - must agree with render_citations exactly
# ---------------------------------------------------------------------------

def test_sources_are_numbered_the_same_way_as_the_prose():
    # Named by their 1-based marker so `passages[marker - 1]` is obvious at
    # the call site rather than something to recompute by hand.
    passages = ["marker 1 text", "marker 2 text", "marker 3 text", "marker 4 text"]
    citations = [_cited("First claim.", 3), _cited("Second claim.", 1)]
    assert sources_for(citations, passages) == [
        (1, "marker 3 text"),
        (2, "marker 1 text"),
    ]


def test_a_chunk_cited_twice_appears_once_in_sources():
    passages = ["marker 1 text", "marker 2 text", "marker 3 text"]
    citations = [_cited("First.", 2), _cited("Second.", 2)]
    assert sources_for(citations, passages) == [(1, "marker 2 text")]


def test_no_citations_gives_no_sources():
    assert sources_for([], ["a", "b"]) == []


def test_an_out_of_range_marker_is_skipped_defensively():
    """Should not happen given `answer.py`'s own filtering, but a rendering
    function must not crash on a bad index reaching it some other way."""
    citations = [_cited("A claim.", 99)]
    assert sources_for(citations, ["only one passage"]) == []


def test_the_two_functions_agree_on_a_realistic_multi_sentence_answer():
    citations = [
        _cited("The tenancy started in 2019.", 2),
        _cited("The deposit was 500 pounds.", 2),
        _cited("The report was filed in June.", 1),
    ]
    passages = ["the safety report", "the tenancy agreement"]
    html = render_citations(citations)
    sources = sources_for(citations, passages)

    assert html == (
        "The tenancy started in 2019.<sup>1</sup> "
        "The deposit was 500 pounds.<sup>1</sup> "
        "The report was filed in June.<sup>2</sup>"
    )
    assert sources == [(1, "the tenancy agreement"), (2, "the safety report")]
