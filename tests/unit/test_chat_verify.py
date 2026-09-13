r"""L8b §2a: no sentence without a receipt - the order's own load-bearing mechanism.

Layer: L8b

The mechanical half (splitting, marker extraction, the pass/fail logic) is
tested with a fake embedder whose vectors are chosen by hand, so similarity
is exact and every case is deterministic. The threshold itself is measured
against the real embedder in the last section - the same convention
`recency.WEIGHT` and `name_match.WEIGHT` hold themselves to.
"""

from __future__ import annotations

import pytest

from app.chat.verify import (
    SIMILARITY_THRESHOLD,
    cross_checked,
    extract_citations,
    resolve_quotes,
    split_sentences,
    verify_answer,
)


class FakeEmbedder:
    """Vectors chosen by the test, looked up by exact text. Unmapped text
    gets a vector orthogonal to everything else, so an unrecognised sentence
    or chunk always scores a similarity of zero rather than raising."""

    def __init__(self, vectors: dict[str, list[float]]):
        self.vectors = vectors
        self.calls = 0

    def embed(self, texts):
        self.calls += 1
        return [self.vectors.get(text, [0.0, 0.0, 1.0]) for text in texts]


# ---------------------------------------------------------------------------
# split_sentences
# ---------------------------------------------------------------------------

def test_splits_on_sentence_ending_punctuation():
    assert split_sentences("First one. Second one! Third one?") == [
        "First one.", "Second one!", "Third one?",
    ]


def test_a_single_sentence_is_returned_whole():
    assert split_sentences("Just one sentence.") == ["Just one sentence."]


def test_empty_text_is_an_empty_list():
    assert split_sentences("") == []
    assert split_sentences("   ") == []


def test_internal_whitespace_is_collapsed():
    assert split_sentences("One.\n\n  Two.") == ["One.", "Two."]


def test_a_citation_marker_after_the_period_stays_with_its_sentence():
    r"""**Found live, against the real model this order targets.** A model
    asked to end every sentence with a citation marker more often writes
    "Claim. [1]" than "Claim [1]." - splitting on the period alone stranded
    the marker as its own citation-only fragment with no sentence to
    belong to."""
    assert split_sentences("The deposit was 500 pounds. [1] The lease "
                           "started in 2019. [2]") == [
        "The deposit was 500 pounds. [1]", "The lease started in 2019. [2]",
    ]


def test_a_citation_marker_before_the_period_still_works_too():
    assert split_sentences("The deposit was 500 pounds [1]. The lease "
                           "started in 2019 [2].") == [
        "The deposit was 500 pounds [1].", "The lease started in 2019 [2].",
    ]


# ---------------------------------------------------------------------------
# extract_citations
# ---------------------------------------------------------------------------

def test_a_single_marker_is_extracted_and_stripped():
    clean, markers = extract_citations("The deposit was $500 [2].")
    assert clean == "The deposit was $500 ."
    assert markers == [2]


def test_no_marker_gives_an_empty_list():
    clean, markers = extract_citations("Nothing cited here.")
    assert clean == "Nothing cited here."
    assert markers == []


def test_multiple_markers_are_all_captured_in_order():
    _clean, markers = extract_citations("Confirmed twice [3][1].")
    assert markers == [3, 1]


def test_a_bracketed_word_that_is_not_a_number_is_left_alone():
    clean, markers = extract_citations("See the [note] below [1].")
    assert markers == [1]
    assert "[note]" in clean


# ---------------------------------------------------------------------------
# cross_checked - §2c, the gate embedding similarity cannot be
# ---------------------------------------------------------------------------

def test_a_number_present_in_the_source_passes():
    assert cross_checked("The deposit was 500 pounds.",
                         "the deposit held was 500 pounds")


def test_a_number_absent_from_the_source_fails():
    r"""**The case §2a's embedding check measurably cannot catch** - see
    `SIMILARITY_THRESHOLD`'s own docstring: changing 500 to 5,000 barely
    moves the similarity score."""
    assert not cross_checked("The deposit was 5,000 pounds.",
                             "the deposit held was 500 pounds")


def test_comma_grouping_does_not_matter():
    assert cross_checked("Invoiced 3,250 pounds.", "totals 3250 pounds")
    assert cross_checked("Invoiced 3250 pounds.", "totals 3,250 pounds")


def test_a_sentence_with_no_numbers_passes_trivially():
    """This gate exists to catch a fabricated *number*, not to demand every
    sentence contain one."""
    assert cross_checked("The tenancy was renewed.", "completely unrelated text")


def test_every_number_in_the_sentence_must_be_present_not_just_one():
    assert not cross_checked("Filed on 12 June 2024 for 3,250 pounds.",
                             "filed on 12 June 2024")


# ---------------------------------------------------------------------------
# resolve_quotes - §2b, verbatim by construction
# ---------------------------------------------------------------------------

CHUNK = "The tenant must vacate the property by the end of the lease term."
#       0         1         2         3         4         5
#       0123456789012345678901234567890123456789012345678901234567890


def test_a_valid_directive_is_replaced_with_the_exact_source_text():
    text = resolve_quotes('The lease says {{quote:1:4:35}}.', [CHUNK])
    assert text == 'The lease says "tenant must vacate the property".'
    # And never the other way round - the substituted text is exactly
    # `CHUNK[4:35]`, not anything the model could have typed instead.
    assert CHUNK[4:35] == "tenant must vacate the property"


def test_an_out_of_range_chunk_number_is_dropped_not_substituted():
    text = resolve_quotes("As stated {{quote:5:0:10}}.", [CHUNK])
    assert text == "As stated ."


def test_a_range_outside_the_chunk_length_is_dropped():
    text = resolve_quotes(f"{{{{quote:1:0:{len(CHUNK) + 50}}}}}", [CHUNK])
    assert text == ""


def test_start_not_before_end_is_dropped():
    text = resolve_quotes("{{quote:1:20:20}}", [CHUNK])
    assert text == ""
    text = resolve_quotes("{{quote:1:30:10}}", [CHUNK])
    assert text == ""


def test_text_with_no_directive_at_all_is_unchanged():
    assert resolve_quotes("Nothing to quote here.", [CHUNK]) == "Nothing to quote here."


def test_multiple_directives_each_resolve_independently():
    text = resolve_quotes("First {{quote:1:0:3}} then {{quote:1:4:10}}.", [CHUNK])
    assert text == 'First "The" then "tenant".'


def test_a_single_brace_is_never_mistaken_for_a_directive():
    assert resolve_quotes("{not a quote directive}", [CHUNK]) == "{not a quote directive}"


# ---------------------------------------------------------------------------
# verify_answer
# ---------------------------------------------------------------------------

SUPPORTED = [1.0, 0.0, 0.0]
UNRELATED = [0.0, 1.0, 0.0]


def test_a_well_cited_sentence_is_rendered():
    embedder = FakeEmbedder({
        "The lease started in 2019 .": SUPPORTED,
        "the lease document, dated 2019, chunk one text": SUPPORTED,
    })
    answer = verify_answer("The lease started in 2019 [1].",
                           ["the lease document, dated 2019, chunk one text"],
                           embedder=embedder)
    assert answer.rendered == ("The lease started in 2019 .",)
    assert answer.dropped_count == 0


def test_a_sentence_with_no_citation_is_dropped():
    embedder = FakeEmbedder({"Uncited claim.": SUPPORTED, "chunk one": SUPPORTED})
    answer = verify_answer("Uncited claim.", ["chunk one"], embedder=embedder)
    assert answer.rendered == ()
    assert answer.dropped_count == 1
    assert answer.sentences[0].similarity == 0.0


def test_a_citation_to_a_chunk_that_was_never_retrieved_is_dropped():
    r"""**Treated as gravely as fabricating the claim itself** - `[5]` when
    only two chunks exist is the model inventing a source, not a typo."""
    embedder = FakeEmbedder({"A claim .": SUPPORTED, "chunk one": SUPPORTED,
                             "chunk two": SUPPORTED})
    answer = verify_answer("A claim [5].", ["chunk one", "chunk two"], embedder=embedder)
    assert answer.rendered == ()
    assert answer.dropped_count == 1


def test_a_cited_but_unsupported_sentence_is_dropped():
    embedder = FakeEmbedder({
        "Something the chunk never said .": SUPPORTED,
        "chunk one text": UNRELATED,
    })
    answer = verify_answer("Something the chunk never said [1].",
                           ["chunk one text"], embedder=embedder)
    assert answer.rendered == ()
    assert answer.sentences[0].similarity == pytest.approx(0.0)


def test_a_sentence_citing_two_chunks_survives_if_either_supports_it():
    embedder = FakeEmbedder({
        "A claim .": SUPPORTED,
        "unrelated chunk": UNRELATED,
        "supporting chunk": SUPPORTED,
    })
    answer = verify_answer("A claim [1][2].", ["unrelated chunk", "supporting chunk"],
                           embedder=embedder)
    assert answer.rendered == ("A claim .",)


def test_multiple_sentences_are_each_checked_independently():
    embedder = FakeEmbedder({
        "Good sentence .": SUPPORTED,
        "Bad sentence .": UNRELATED,
        "the chunk": SUPPORTED,
    })
    answer = verify_answer("Good sentence [1]. Bad sentence [1].",
                           ["the chunk"], embedder=embedder)
    assert answer.rendered == ("Good sentence .",)
    assert answer.dropped_count == 1
    assert len(answer.sentences) == 2


def test_rendered_order_matches_the_original_answer():
    embedder = FakeEmbedder({
        "One .": SUPPORTED, "Two .": SUPPORTED, "Three .": SUPPORTED,
        "chunk": SUPPORTED,
    })
    answer = verify_answer("One [1]. Two [1]. Three [1].", ["chunk"], embedder=embedder)
    assert answer.rendered == ("One .", "Two .", "Three .")


def test_an_empty_answer_verifies_to_nothing():
    answer = verify_answer("", ["chunk"], embedder=FakeEmbedder({}))
    assert answer.sentences == ()
    assert answer.rendered == ()
    assert answer.dropped_count == 0


def test_no_chunks_at_all_means_every_marker_is_out_of_range():
    embedder = FakeEmbedder({"A claim .": SUPPORTED})
    answer = verify_answer("A claim [1].", [], embedder=embedder)
    assert answer.rendered == ()


def test_high_similarity_alone_is_not_enough_if_the_number_is_wrong():
    r"""**The composed gate, proven end to end.** A fake embedder that would
    happily pass this sentence on similarity alone must still see it
    dropped, because `verify_answer` requires both gates, not either."""
    embedder = FakeEmbedder({
        "The deposit was 5,000 pounds .": SUPPORTED,
        "the deposit held was 500 pounds": SUPPORTED,
    })
    answer = verify_answer("The deposit was 5,000 pounds [1].",
                           ["the deposit held was 500 pounds"], embedder=embedder)
    assert answer.rendered == ()
    assert answer.dropped_count == 1


def test_verify_answer_resolves_quotes_before_checking_citations():
    r"""**The system's own words, never the model's, reach the screen.** The
    sentence handed to the embedder and to `cross_checked` is the one with
    the quote already substituted - so a fabricated quote is not just
    "prevented from being wrong", the model never had a chance to type it
    in the first place.
    """
    chunk = "The deposit held under this agreement is 500 pounds exactly."
    embedder = FakeEmbedder({
        'The lease states "deposit held under this agreement is 500 pounds" .': SUPPORTED,
        chunk: SUPPORTED,
    })
    # No quote marks of its own around the directive - `resolve_quotes`
    # supplies them as part of the substitution.
    answer = verify_answer('The lease states {{quote:1:4:51}} [1].', [chunk], embedder=embedder)
    assert answer.rendered == ('The lease states "deposit held under this agreement is 500 pounds" .',)


def test_the_embedder_is_called_once_for_sentences_and_once_for_chunks():
    """Batched, not once per sentence - the same reasoning `Embedder.embed`
    itself is batched for."""
    embedder = FakeEmbedder({
        "One .": SUPPORTED, "Two .": SUPPORTED, "chunk": SUPPORTED,
    })
    verify_answer("One [1]. Two [1].", ["chunk"], embedder=embedder)
    assert embedder.calls == 2


# ---------------------------------------------------------------------------
# The measurement, kept honest
# ---------------------------------------------------------------------------

#: `(chunk, genuinely-supported sentence, genuinely-fabricated sentence)`.
#: The five pairs `SIMILARITY_THRESHOLD`'s own docstring records the sweep
#: for - kept here, not invented twice, so the constant's measurement and
#: its regression test can never quietly drift apart.
_MEASURED_PAIRS = [
    (
        "The tenancy agreement for 14 Elm Street was signed on 3 March 2019 "
        "and the deposit held was 500 pounds.",
        "The deposit for the Elm Street tenancy was 500 pounds.",
        "The kitchen was renovated with a new gas oven in 2021.",
    ),
    (
        "The safety report for the Leeds site was filed by Dave Whitfield "
        "on 12 June 2024, noting three outstanding actions on the valve "
        "isolation procedure.",
        "Dave Whitfield filed the Leeds safety report in June 2024.",
        "The Leeds site passed its audit with zero findings.",
    ),
    (
        "Invoice 4471 from Acme Fabrication, dated 9 January 2023, totals "
        "3,250 pounds for structural steel supplied to the pump station "
        "project.",
        "Acme Fabrication invoiced 3,250 pounds for the pump station steel.",
        "The pump station project was cancelled due to budget overruns.",
    ),
    (
        "Minutes of the 2026-08-14 site meeting record that the licence "
        "renewal was approved subject to the fire marshal's final sign-off.",
        "The licence renewal was approved pending the fire marshal's sign-off.",
        "The site meeting was rescheduled to the following week.",
    ),
    (
        "Chris Okafor's email of 5 May confirms the shipment of 40 units "
        "was delayed at customs and is now expected on 20 May.",
        "Chris Okafor said the 40-unit shipment would arrive around 20 May.",
        "Chris Okafor resigned from the shipping department in May.",
    ),
]


def _real_embedder():
    from app.index.embedder import Embedder

    try:
        embedder = Embedder()
        embedder.embed(["warm-up"])
    except Exception as exc:                        # noqa: BLE001 - no model in this environment
        pytest.skip(f"real embedder not available here: {exc}")
    return embedder


def test_the_shipped_threshold_still_earns_its_place():
    r"""**A verification threshold without its measurement is not done.**

    All five measured pairs - genuinely supported sentences and genuinely
    topical-but-fabricated ones - must land on the correct side of
    `SIMILARITY_THRESHOLD`. This is the sweep `SIMILARITY_THRESHOLD`'s own
    docstring quotes; failing here means that docstring is now stale,
    whichever direction the numbers moved.
    """
    embedder = _real_embedder()

    for chunk, supported, fabricated in _MEASURED_PAIRS:
        clean_supported, _ = extract_citations(supported)
        clean_fabricated, _ = extract_citations(fabricated)
        vectors = embedder.embed([clean_supported, clean_fabricated, chunk])
        sim_supported = sum(a * b for a, b in zip(vectors[0], vectors[2]))
        sim_fabricated = sum(a * b for a, b in zip(vectors[1], vectors[2]))

        assert sim_supported >= SIMILARITY_THRESHOLD, (
            f"{supported!r} scored {sim_supported:.3f} against threshold "
            f"{SIMILARITY_THRESHOLD} - re-measure before shipping")
        assert sim_fabricated < SIMILARITY_THRESHOLD, (
            f"{fabricated!r} scored {sim_fabricated:.3f} against threshold "
            f"{SIMILARITY_THRESHOLD} - the threshold no longer separates "
            f"genuine support from fabrication; re-run the sweep")


def test_embedding_similarity_alone_cannot_catch_a_wrong_number():
    r"""**The measured proof that §2c is necessary, not decorative.**
    Changing one digit in an otherwise-correct sentence must still score
    above `SIMILARITY_THRESHOLD` on similarity alone - if a future embedding
    model ever caught this by itself, that would be worth knowing, but it
    is not what `BAAI/bge-small-en-v1.5` does today, and `cross_checked` is
    the gate this project relies on instead.
    """
    embedder = _real_embedder()
    chunk = ("The tenancy agreement for 14 Elm Street was signed on 3 March "
            "2019 and the deposit held was 500 pounds.")
    wrong_number = "The deposit for the Elm Street tenancy was 5,000 pounds."

    vectors = embedder.embed([wrong_number, chunk])
    similarity = sum(a * b for a, b in zip(vectors[0], vectors[1]))

    assert similarity >= SIMILARITY_THRESHOLD, (
        f"a wrong-number sentence now scores {similarity:.3f}, below "
        f"threshold - if the embedding model changed enough to catch this "
        f"on its own, update this test's expectation and say so; do not "
        f"just delete it")
    assert not cross_checked(wrong_number, chunk), (
        "cross_checked should still catch what similarity does not")
