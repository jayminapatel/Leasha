"""Sentences and quotes: the text handling that makes "verbatim" a property of the code.

Layer: L8b. Work order `202626270611-chat-tab` section 4e-1 (quotes cut at sentence
boundaries) and its own test line: *"property test (snapped span is a substring of the
chunk, verbatim, whole words)"*.
"""

from __future__ import annotations

import re

import pytest
from hypothesis import given, settings, strategies as st

from app.chat.text import (
    DEFAULT_QUOTE_MAX,
    content_tokens,
    sentence_spans,
    sentences,
    snap_span,
    stem,
)
from tests.fixtures.chat_eval import CORPUS


# --------------------------------------------------------------------------- sentences

def test_plain_sentences_split_where_a_person_would_split_them():
    assert sentences("It is done. Nothing else remains! Is that clear? Yes.") == [
        "It is done.", "Nothing else remains!", "Is that clear?", "Yes."]


@pytest.mark.parametrize("text, expected", [
    ("Dr. Rao will see you at 9.15 on Thursday. Bring the card.", 2),
    ("The rent is 1,200.50 pounds per month. Payable monthly.", 2),
    ("Mr. Smith and Mrs. Jones met J. Patel. They agreed.", 2),
    ("See fig. 3 for details. It shows the manifold.", 2),
    ("The list (see below.) is short. It has two items.", 2),
])
def test_abbreviations_and_decimals_do_not_end_a_sentence(text, expected):
    assert len(sentences(text)) == expected, sentences(text)


def test_a_hard_wrapped_paragraph_stays_one_sentence():
    wrapped = "The deposit will be returned within\nten days of the tenancy ending, less any\nagreed deductions."
    assert len(sentences(wrapped)) == 1


def test_headers_and_list_items_are_their_own_sentences():
    mail = "Subject: Deposit\nFrom: a@b.com\nTo: c@d.com\n\nThank you. The deposit stays."
    got = sentences(mail)
    assert got[:3] == ["Subject: Deposit", "From: a@b.com", "To: c@d.com"]
    assert len(sentences("- first item\n- second item\n- third item")) == 3


def test_spans_are_exact_slices():
    text = "First one.  Second one!\n\nThird."
    for start, end in sentence_spans(text):
        assert text[start:end] == text[start:end].strip()
    assert [text[s:e] for s, e in sentence_spans(text)] == ["First one.", "Second one!", "Third."]


# --------------------------------------------------------------------------- snapping

LONG = ("The deposit is 950 pounds and it is held in a government approved scheme. "
        "The deposit will be returned within 10 days of the tenancy ending, less any agreed "
        "deductions. The boiler service is the landlord's responsibility. Signed, Margaret "
        "Okafor, Landlord.")


def test_a_span_starting_mid_word_snaps_to_a_whole_sentence():
    at = LONG.index("returned")                          # the middle of the second sentence
    start, end = snap_span(LONG, at + 3, at + 30)
    assert LONG[start:end].startswith("The deposit will be returned")
    assert LONG[start:end].endswith("agreed deductions.")


def test_a_span_across_two_sentences_widens_to_both():
    at = LONG.index("scheme")
    start, end = snap_span(LONG, at, at + 40)
    assert LONG[start:end].startswith("The deposit is 950")
    assert "deductions." in LONG[start:end]


def test_an_over_long_request_is_narrowed_not_extended():
    start, end = snap_span(LONG, 0, len(LONG), max_chars=120)
    assert end - start <= 120
    assert LONG[start:end] in LONG


def test_one_sentence_longer_than_the_limit_is_cut_at_a_word():
    sentence = "word " * 200 + "end."
    start, end = snap_span(sentence, 100, 130, max_chars=80)
    piece = sentence[start:end]
    assert 0 < len(piece) <= 80
    assert piece == piece.strip() and not piece.startswith("ord") and " " not in piece[:0]
    assert all(w == "word" or w == "end." for w in piece.split())      # never a cut-off word


def test_empty_text_snaps_to_nothing():
    assert snap_span("", 0, 5) == (0, 0)


_ALPHABET = st.sampled_from(list("abcdefghij  .,!?\n") + ["The", "deposit", "Dr.", "3.5", "1,200"])


@settings(max_examples=200, deadline=None)
@given(pieces=st.lists(_ALPHABET, min_size=1, max_size=60),
       a=st.integers(0, 400), b=st.integers(0, 400),
       limit=st.integers(20, 400))
def test_a_snapped_span_is_a_substring_of_whole_words(pieces, a, b, limit):
    """Work order 4e-1's property test, over arbitrary text and arbitrary offsets."""
    text = "".join(pieces)
    start, end = snap_span(text, min(a, b), max(a, b), max_chars=limit)
    assert 0 <= start <= end <= len(text)
    quote = text[start:end]
    if not quote:
        return
    assert quote == quote.strip()                                    # no ragged whitespace
    assert text.count(quote) >= 1                                    # verbatim
    if max((len(run) for run in re.findall(r"[A-Za-z0-9]+", text)), default=0) > limit:
        return          # one unbroken token longer than the limit: a cut is unavoidable
    # whole words: it does not begin or end in the middle of a run of letters
    assert start == 0 or not (text[start - 1].isalnum() and text[start].isalnum())
    assert end == len(text) or not (text[end - 1].isalnum() and text[end].isalnum())
    # never longer than asked, unless a single unbroken word forces it
    assert len(quote) <= limit or len(quote.split()) <= 1


def test_every_sentence_of_every_fixture_document_snaps_to_itself():
    for doc in CORPUS:
        for start, end in sentence_spans(doc.text):
            if end - start > DEFAULT_QUOTE_MAX:
                continue
            assert snap_span(doc.text, start + 1, end - 1) == (start, end), doc.path


# --------------------------------------------------------------------------- tokens

def test_light_stemming_joins_spellings_without_joining_different_words():
    assert stem("licences") == stem("licence") == stem("licenced")
    assert stem("approved") == stem("approve")
    assert stem("raising") == stem("raise")
    assert stem("news") == "news" and stem("class") == "class"


def test_content_tokens_drop_stop_words_and_keep_figures():
    assert content_tokens("The deposit is 950 pounds") == ["deposit", "950", "pound"]
    assert content_tokens("it was in the") == []
