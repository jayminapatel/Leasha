"""Layer 6 — entity extraction and PMI scoring.

Every test here is a literal string in and a literal expectation out. That is
the point of `cooccurrence.py` being pure: the part of the graph that decides
what counts as a thing needs no database, no model and no Windows.

Several of these assert against noise this extractor produced when first run on
real prose. They are named for the failure, not the rule, so a future change that
reintroduces it fails with a description of what went wrong.
"""

from __future__ import annotations

import math

import pytest

from app.graph import cooccurrence as co


def displays(text: str, kind: str | None = None) -> set[str]:
    return {
        entity.display
        for entity in co.extract(text).counts
        if kind is None or entity.kind == kind
    }


def kinds(text: str) -> dict[str, str]:
    return {entity.display: entity.kind for entity in co.extract(text).counts}


# -- what should be found ----------------------------------------------------

def test_finds_a_capitalised_run_as_one_entity():
    assert "Acme Water Ltd" in displays("The pump at Acme Water Ltd failed.")


def test_a_name_survives_an_internal_connector():
    assert "Bank of England" in displays("Bank of England guidance applies here.")


def test_connectors_do_not_count_against_the_length_cap():
    """Four words that matter, two joining them - not a six-word heading.

    Charging the cap for "for" and "and" rejected a real organisation, which is
    the failure this guards.
    """
    assert "Department for Work and Pensions" in displays(
        "Contact the Department for Work and Pensions about it."
    )


def test_finds_email_addresses():
    assert displays("write to jenny.okonkwo@acme.co.uk today", "email") == {
        "jenny.okonkwo@acme.co.uk"
    }


def test_finds_acronyms_because_this_corpus_is_full_of_them():
    found = displays("HACCP and SCADA feed the MES.", "acronym")
    assert {"HACCP", "SCADA", "MES"} <= found


def test_an_ampersand_acronym_stays_whole():
    """P&ID, R&D and H&S are single terms; splitting them files 'ID' as a node."""
    assert "P&ID" in displays("The P&ID was reissued.", "acronym")
    assert "R&D" in displays("R&D signed it off.", "acronym")


def test_finds_filenames_with_a_known_extension():
    assert displays("see Report.docx attached", "file") == {"Report.docx"}


def test_ignores_extensions_this_app_knows_nothing_about():
    assert displays("visit example.com or run script.sh", "file") == set()


# -- what should NOT be found (each of these actually happened) --------------

def test_a_sentence_initial_stopword_is_not_an_entity():
    """'The pump failed' must not yield 'The'."""
    assert "The" not in displays("The pump failed overnight.")


def test_trailing_punctuation_does_not_smuggle_in_the_calendar():
    """The first run produced 'Tuesday.' - a stopword the full stop hid."""
    assert not {d for d in displays("It failed on Tuesday.") if d.startswith("Tuesday")}


def test_a_filename_does_not_drag_the_sentence_in_with_it():
    """A space-tolerant filename pattern produced entities out of whole clauses."""
    text = "about the HACCP review and attached Pasteuriser Report.docx"
    for display in displays(text, "file"):
        assert " " not in display, f"filename entity swallowed prose: {display!r}"


def test_a_filename_is_not_also_a_name():
    """It was previously counted twice: once as a file, once as a capitalised run."""
    text = "Jenny sent Pasteuriser Report.docx yesterday."
    assert "Pasteuriser Report.docx" not in displays(text, "name")


def test_a_heading_length_run_is_rejected():
    long_run = "Health And Safety Executive Guidance Note Seven Issued Today"
    assert long_run not in displays(long_run)


def test_single_letters_and_initials_are_not_entities():
    assert displays("A B C are the options.", "name") == set()


# -- identity ----------------------------------------------------------------

def test_case_and_spacing_variants_are_one_entity():
    assert co.normalise("Acme Ltd") == co.normalise("ACME LTD") == co.normalise("Acme  Ltd")


def test_a_line_wrapped_name_matches_the_unwrapped_one():
    """The common duplicate in a PDF corpus, and invisible once rendered."""
    assert co.normalise("Barnsley\nDairy") == co.normalise("Barnsley Dairy")


def test_the_same_entity_twice_in_a_chunk_is_counted_twice():
    counts = co.extract("Acme Water Ltd met Acme Water Ltd again.").counts
    entity = next(e for e in counts if e.display == "Acme Water Ltd")
    assert counts[entity] == 2


def test_extraction_is_deterministic():
    text = "Acme Water Ltd and HACCP at Barnsley Dairy, per jen@acme.co.uk, in Plan.docx."
    assert co.extract(text).counts == co.extract(text).counts


# -- PMI ---------------------------------------------------------------------

def test_pmi_is_zero_when_two_things_co_occur_exactly_by_chance():
    # p(a) = p(b) = 0.5, p(joint) = 0.25 -> log2(0.25 / 0.25) = 0
    assert co.pmi(25, 50, 50, 100) == pytest.approx(0.0)


def test_pmi_is_positive_when_they_attract():
    assert co.pmi(40, 50, 50, 100) > 0


def test_pmi_of_the_impossible_is_negative_infinity_not_an_exception():
    assert co.pmi(0, 10, 10, 100) == -math.inf
    assert co.pmi(5, 0, 10, 100) == -math.inf
    assert co.pmi(5, 10, 10, 0) == -math.inf


def test_npmi_is_one_when_two_things_never_appear_apart():
    assert co.npmi(10, 10, 10, 100) == pytest.approx(1.0)


def test_npmi_is_minus_one_when_they_never_appear_together():
    assert co.npmi(0, 10, 10, 100) == -1.0


def test_npmi_stays_comparable_as_the_corpus_grows():
    """The whole reason for normalising: a threshold chosen once must keep meaning.

    Raw PMI's ceiling depends on how rare the pair is, so the same relationship
    scores higher simply because the index got bigger - and any fixed cutoff
    slowly stops filtering anything.
    """
    small = co.npmi(10, 10, 10, 200)
    large = co.npmi(100, 100, 100, 20_000)
    assert small == pytest.approx(large)

    raw_small = co.pmi(10, 10, 10, 200)
    raw_large = co.pmi(100, 100, 100, 20_000)
    assert raw_large > raw_small + 1, "raw PMI should drift; that is why npmi exists"


def test_an_entity_in_every_chunk_carries_no_information():
    assert co.npmi(100, 100, 100, 100) == 0.0


# -- edge scoring ------------------------------------------------------------

def test_score_edges_normalises_direction():
    edges = co.score_edges({(9, 4): 5}, {4: 5, 9: 5}, 100)
    assert [(a, b) for a, b, _w, _s in edges] == [(4, 9)]


def test_score_edges_drops_a_single_co_occurrence():
    """One shared chunk is indistinguishable from coincidence, and it is most rows."""
    assert co.score_edges({(1, 2): 1}, {1: 5, 2: 5}, 100) == []


def test_score_edges_drops_pairs_no_better_than_chance():
    # Two ubiquitous entities: together often, but exactly as often as expected.
    assert co.score_edges({(1, 2): 25}, {1: 50, 2: 50}, 100, min_weight=2) == []


def test_score_edges_sorts_strongest_first():
    scored = co.score_edges(
        {(1, 2): 8, (3, 4): 8},
        {1: 8, 2: 8, 3: 40, 4: 40},
        100,
    )
    assert scored[0][:2] == (1, 2), "the exclusive pair should outrank the common one"


def test_pair_keys_yields_each_unordered_pair_once():
    assert list(co.pair_keys([3, 1, 2, 1])) == [(1, 2), (1, 3), (2, 3)]


def test_pair_keys_of_a_single_entity_is_empty():
    assert list(co.pair_keys([7])) == []


# ---------------------------------------------------------------------------
# Found by the first run against a real corpus of slide decks and product
# literature. Every test below pins a specific entity that came out wrong, and
# is named for the wrong output rather than for the rule that fixes it.
#
# The graph's top 25 entities on that corpus were "Connect", "Enterprise",
# "AI", "System", "DATA", "CLOUD", "DESIGN", "Optimize", "Access", "Slide",
# "Learn", "Use", "How", "Customers", "Ability" - capitalised English words,
# crowding out everything that was actually there.
# ---------------------------------------------------------------------------

def test_a_capitalised_ordinary_word_is_not_an_entity():
    for word in ("Connect", "Enterprise", "System", "Optimize", "Access", "Slide"):
        assert word not in displays(f"{word} is mentioned here."), word


def test_a_heading_set_in_capitals_is_not_a_set_of_acronyms():
    """Slide headings are ALL-CAPS, so "DATA CLOUD DESIGN" looked like three terms."""
    found = displays("DATA CLOUD DESIGN")
    assert "DATA" not in found
    assert "CLOUD" not in found
    assert "DESIGN" not in found


def test_a_run_of_nothing_but_ordinary_words_is_a_heading():
    assert "Enterprise System" not in displays("The Enterprise System was discussed.")


def test_a_real_name_containing_an_ordinary_word_survives():
    """The blocklist applies to what a run is *made of*, not to any word in it."""
    assert "PI System" in displays("The PI System stores the tags.")
    assert "Customer FIRST" in displays("The Customer FIRST agreement renews.")


def test_a_possessive_is_the_same_entity_as_the_name():
    """"AVEVA’s" and "AVEVA" were two nodes sitting beside each other, same label.

    Both apostrophes, because a Word document and a PDF disagree about which one
    they use, and a corpus contains both.
    """
    curly = displays("AVEVA’s Insight platform.", "name")
    straight = displays("AVEVA's Insight platform.", "name")
    plain = displays("The AVEVA Insight platform.", "name")
    assert curly == straight == plain == {"AVEVA Insight"}


def test_an_all_caps_word_joins_a_name_instead_of_breaking_it():
    """This fragmentation is where the standalone "System" nodes came from.

    Product names mix ALL-CAPS and Title Case constantly. Treating the capital
    word as a wall split every one of them into pieces.
    """
    found = displays("AVEVA System Platform runs the site.")
    assert "AVEVA System Platform" in found
    assert "System" not in found


def test_a_conjunction_does_not_weld_two_acronyms_into_one_entity():
    """"SCADA and MES" is two entities and a conjunction, not a product."""
    found = displays("SCADA and MES feed the historian.")
    assert "SCADA and MES" not in found
    assert {"SCADA", "MES"} <= found


def test_a_conjunction_still_joins_a_genuine_name():
    assert "Department for Work and Pensions" in displays(
        "The Department for Work and Pensions replied."
    )


def test_a_new_name_after_a_conjunction_starts_a_new_entity():
    found = displays("AVEVA System Platform and AVEVA CONNECT were compared.")
    assert "AVEVA System Platform" in found
    assert "AVEVA CONNECT" in found
    assert not any(len(name.split()) > 4 for name in found)


def test_an_imperative_opening_a_sentence_is_not_part_of_the_name():
    """Marketing prose is full of them: "Discover X", "Learn about Y"."""
    found = displays("Discover AVEVA Insight today.")
    assert "AVEVA Insight" in found
    assert "Discover AVEVA Insight" not in found


def test_a_run_of_initials_is_not_a_name():
    """Once ALL-CAPS words could join runs, "A B C" became the entity "B C"."""
    assert displays("A B C are the options.", "name") == set()


# ---------------------------------------------------------------------------
# Second pass over the same real corpus. The first round of fixes cleared out
# the ordinary nouns; what surfaced underneath was slide-bullet grammar -
# "Provide", "Accelerate", "Ideal", "Operational", "Flexible" - plus heading
# numbering ("II") and ALL-CAPS adjectives ("OPEN", "HYBRID").
# ---------------------------------------------------------------------------

def test_a_lone_word_that_only_ever_opens_a_sentence_is_not_a_name():
    """Its capital is grammar, and a slide bullet is its own sentence.

    "Provide real-time insight." and "Accelerate delivery." were entering the
    graph as the entities "Provide" and "Accelerate". No blocklist can keep up
    with the supply of verbs, so the rule is evidential instead: a lone
    Title-Case word whose only appearance is in the one position where every
    word is capitalised has shown nothing.
    """
    found = displays("Provide real-time insight. Accelerate your delivery. Deploy quickly.")
    assert found == set(), f"sentence-opening verbs became entities: {found}"


def test_a_real_name_survives_because_it_also_appears_mid_sentence():
    """The cost of the rule above, and why it is small.

    A name that matters is mentioned mid-sentence somewhere, and that occurrence
    is still collected.
    """
    text = "Barnsley Dairy was audited. The team visited Barnsley yesterday."
    assert "Barnsley" in displays(text)


def test_a_multi_word_name_opening_a_sentence_is_still_a_name():
    """The rule is about lone words only - two words is evidence by itself."""
    assert "Barnsley Dairy" in displays("Barnsley Dairy was audited last week.")


def test_all_caps_adjectives_from_a_heading_are_not_acronyms():
    assert displays("Ideal for OPEN HYBRID deployments.", "acronym") == set()


def test_roman_numerals_are_heading_numbering_not_terms():
    assert "II" not in displays("Section II covers the AVEVA PI System.")
    assert "AVEVA PI System" in displays("Section II covers the AVEVA PI System.")


def test_a_roman_numeral_breaks_a_run_instead_of_propping_it_up():
    """"OPEN HYBRID II" slipped past the all-ordinary-words check on the "II"."""
    assert "OPEN HYBRID II" not in displays("Ideal for OPEN HYBRID II deployments.")


def test_a_roman_numeral_does_not_swallow_the_name_beside_it():
    found = displays("Barnsley Dairy II was audited by Jenny Okonkwo.")
    assert "Barnsley Dairy" in found
    assert "Jenny Okonkwo" in found
