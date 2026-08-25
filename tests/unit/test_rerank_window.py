"""Cutting a passage to the part worth scoring.

Layer: L4

Reranking was **8.3 seconds of a 9-second search** - 93% of it, against a spec
budget of 300ms warm. Three things multiply into that: how many candidates are
scored, how long each passage is, and which model reads them.

This file is about the second. The reranker was handed whole chunks - a mean of
1,070 characters and a maximum of 2,734 - to reach a verdict the sentence around
the match already supports. A cross-encoder's cost is at best linear in passage
length, and attention within the sequence is quadratic, so the tail of a long
chunk is not free: it is most of the bill.

The correctness question is the only one that matters here: **the window must
contain the match.** A faster reranker that scores the wrong part of the passage
is worse than a slow one, and it would fail silently - the ordering would just
quietly get worse.
"""

from __future__ import annotations

import pytest

from app.search.window import RERANK_WINDOW_CHARS, rerank_window, windows_for

FILLER = "the team circulated some routine paperwork before the meeting. "


def passage(match: str, *, before: int = 40, after: int = 40) -> str:
    return FILLER * before + match + " " + FILLER * after


# ---------------------------------------------------------------------------
# It must contain the match
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("before", [0, 5, 40, 200])
def test_the_window_contains_the_match_wherever_it_sits(before):
    """**The one thing that cannot be got wrong.** A window that misses the
    match makes the reranker score the wrong text, and the only symptom is
    ordering that quietly gets worse."""
    text = passage("the petrorabigh licence renewal", before=before)
    window = rerank_window(text, ["petrorabigh"])
    assert "petrorabigh" in window


def test_a_match_at_the_very_end_is_still_captured():
    """The hardest case: clamping the start naively pushes the window past it."""
    text = FILLER * 100 + "petrorabigh"
    assert "petrorabigh" in rerank_window(text, ["petrorabigh"])


def test_a_match_at_the_very_start_is_still_captured():
    text = "petrorabigh " + FILLER * 100
    assert "petrorabigh" in rerank_window(text, ["petrorabigh"])


def test_the_densest_cluster_wins():
    """A passage where the terms appear together is more relevant than one
    where they are scattered, and that cluster is what the model should read."""
    text = ("alpha " + FILLER * 30 + "alpha beta gamma together here "
            + FILLER * 30 + "gamma")
    window = rerank_window(text, ["alpha", "beta", "gamma"], width=300)
    assert "together here" in window


def test_context_before_the_match_is_kept():
    """"the licence expires" without saying which licence is a fragment, and a
    reranker scoring fragments is scoring noise."""
    text = FILLER * 40 + "MARKER the licence expires in March " + FILLER * 40
    window = rerank_window(text, ["licence"], width=400)
    assert window.index("licence") > 30, "the match should not be at the very edge"


# ---------------------------------------------------------------------------
# Size
# ---------------------------------------------------------------------------

def test_the_window_respects_its_width():
    text = passage("a match", before=200)
    assert len(rerank_window(text, ["match"], width=400)) <= 400


def test_a_short_passage_is_returned_whole():
    """Cutting a 200-character chunk saves nothing and can only lose meaning."""
    text = "a short passage about the licence renewal"
    assert rerank_window(text, ["licence"]) == text


def test_whitespace_is_flattened():
    """A chunk full of \\r\\n\\r\\n from an email tokenises worse than the same
    words with single spaces, and costs more for the same meaning."""
    assert rerank_window("a\r\n\r\n  b\tc", ["b"]) == "a b c"


def test_no_word_is_split_at_either_end():
    """A truncated token is noise to a tokeniser, and can change how the
    tokens around it are split."""
    text = FILLER * 60
    window = rerank_window(text, ["nothing-matches-this"], width=200)
    assert not window.startswith(" ") and not window.endswith(" ")
    assert window.split()[0] in FILLER
    assert window.split()[-1] in FILLER.replace(".", "")


# ---------------------------------------------------------------------------
# When there is nothing to centre on
# ---------------------------------------------------------------------------

def test_no_matching_term_falls_back_to_the_opening():
    """What a vector-only hit looks like: it matched on meaning, so there is no
    term to centre on and the opening is as good a guess as any."""
    text = passage("nothing relevant", before=50)
    window = rerank_window(text, ["absent"])
    assert text.replace("\n", " ").startswith(window[:40])


def test_no_terms_at_all_does_not_raise():
    assert rerank_window(passage("x"), []) != ""


def test_empty_text_is_empty():
    assert rerank_window("", ["anything"]) == ""


def test_a_zero_width_returns_the_flattened_text_rather_than_nothing():
    """A misconfigured width must not silently send empty passages to the
    reranker, which would score every candidate identically."""
    assert rerank_window("a b c", ["b"], width=0) == "a b c"


# ---------------------------------------------------------------------------
# The default, and the batch
# ---------------------------------------------------------------------------

def test_the_default_width_is_a_couple_of_sentences():
    """Roughly 150 tokens. Below about 300 characters a match loses the context
    that makes it meaningful; above ~800 the tail is being paid for twice."""
    assert 300 <= RERANK_WINDOW_CHARS <= 800


def test_a_batch_keeps_its_order():
    """The scores come back positionally. A reordering here would attach every
    score to the wrong passage - and the result would look plausible."""
    texts = [passage(f"marker{i}", before=30) for i in range(5)]
    windows = windows_for(texts, ["marker0", "marker3"])
    assert len(windows) == 5
    assert "marker0" in windows[0]
    assert "marker3" in windows[3]


def test_the_cut_is_real_on_a_typical_chunk():
    """Not a claim about the model - just that the input shrank. The mean chunk
    in the owner's index is 1,070 characters."""
    text = FILLER * 20                          # ~1,260 characters
    assert len(rerank_window(text, ["meeting"])) < len(text) / 1.5
