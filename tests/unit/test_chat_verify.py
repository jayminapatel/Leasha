"""No sentence without a receipt - the tests that carry the whole Chat order.

Layer: L8b. Work order `202626270611-chat-tab` sections 2a, 2b, 2c and 5.

Two levels. First the verifier on its own, sentence by sentence, each way a model can
lie having its own test: an invented figure, an invented name, a quotation that is not
in the document, a marker for a source that was never shown, no marker at all, the
source's own sentence with its meaning reversed. Then, in `test_chat_engine.py`, the
same lies arriving through the whole engine from a deliberately hallucinating model.

A property test closes the loop: for *any* sentence a generator can write, if the
verifier lets it through, then every meaningful word and every figure in it is in the
document it cites. That is the guarantee stated as something checkable.
"""

from __future__ import annotations

import re

import pytest
from hypothesis import given, settings, strategies as st

from app.chat.context import Piece, Source
from app.chat.text import content_tokens
from app.chat.types import ChatTurn, Receipt
from app.chat.verify import (
    SUPPORT,
    AnswerAssembler,
    Verifier,
    audit_turn,
    extract_numbers,
    normalise_marker_placement,
    split_markers,
    support_tokens,
)

LETTER = (
    "Dear tenant, this letter confirms the agreement about your tenancy deposit. "
    "The deposit is 950 pounds and it is held in a government approved scheme. "
    "The deposit will be returned within 10 days of the tenancy ending, less any agreed "
    "deductions. The boiler service is the landlord's responsibility. "
    "Signed, Margaret Okafor, Landlord."
)
QUOTE = "Twelve thousand pounds for the site, which is up four percent. A purchase order is needed."
MAIL = "Approved. Go ahead and buy the licence renewal at the quoted price."


def source(n: int, text: str, name: str = "doc.txt", meta: str = "") -> Source:
    return Source(n=n, file_id=n, path=f"C:/x/{name}", name=name,
                  pieces=[Piece(chunk_id=100 + n, text=text)], passage=text, meta=meta or name)


def run(text: str, *sources: Source, threshold: float = SUPPORT) -> AnswerAssembler:
    assembler = AnswerAssembler(Verifier(list(sources), threshold=threshold))
    assembler.feed(text)
    assembler.flush()
    return assembler


def dropped_reasons(assembler: AnswerAssembler) -> str:
    return " | ".join(reason for _sentence, reason in assembler.dropped)


# --------------------------------------------------------------------------- what passes

def test_a_supported_sentence_is_accepted_with_a_verbatim_receipt():
    got = run("The deposit is 950 pounds [1].", source(1, LETTER, "deposit-letter.docx"))
    assert [a.text for a in got.accepted] == ["The deposit is 950 pounds [1]."]
    receipt = got.receipts()[0]
    assert receipt.name == "deposit-letter.docx"
    assert receipt.quote in LETTER                       # cut from the stored text
    assert receipt.quote.startswith("The deposit is 950 pounds")
    assert receipt.chunk_id == 101


def test_a_marker_placed_after_the_full_stop_still_belongs_to_its_sentence():
    text = "The deposit is 950 pounds. [1] It will be returned within 10 days. [1]"
    got = run(text, source(1, LETTER))
    assert len(got.accepted) == 2 and not got.dropped


def test_the_receipt_quote_is_a_whole_sentence_not_a_fragment():
    """Work order 4e-1: quotes are cut at sentence boundaries."""
    got = run("It will be returned within 10 days [1].", source(1, LETTER))
    quote = got.receipts()[0].quote
    assert quote.startswith("The deposit will be returned")
    assert quote.endswith("agreed deductions.")


@pytest.mark.parametrize("figure", ["£950", "950", "950.00"])
def test_a_figure_written_differently_is_still_the_same_figure(figure):
    got = run(f"The deposit is {figure} [1].", source(1, LETTER))
    assert got.accepted, dropped_reasons(got)


def test_number_words_and_digits_are_the_same_quantity_in_both_directions():
    assert extract_numbers("Twelve thousand pounds") == extract_numbers("12,000 pounds")
    assert "40000" in extract_numbers("about 40k")
    got = run("The quote was 12,000 pounds for the site [1].", source(1, QUOTE))
    assert got.accepted, dropped_reasons(got)


def test_a_name_that_is_only_in_the_metadata_is_evidence():
    """The sender is in the header, not the body - the sentence is still true."""
    got = run("Priya approved the licence renewal [1].",
              source(1, MAIL, "approval.eml", meta="approval.eml priya.n@acme.com Approved: licence"))
    assert got.accepted, dropped_reasons(got)


# --------------------------------------------------------------------------- 2a: what dies

def test_a_sentence_with_no_marker_never_reaches_the_screen():
    got = run("The deposit is 950 pounds.", source(1, LETTER))
    assert not got.accepted and "no source marker" in dropped_reasons(got)


def test_a_marker_for_a_source_that_was_never_shown_is_refused():
    got = run("The matter was closed in full last spring [7].", source(1, LETTER))
    assert not got.accepted and "which was not shown" in dropped_reasons(got)


def test_a_true_sentence_with_a_marker_for_a_missing_source_is_repaired_to_the_one_that_supports_it():
    got = run("The deposit is 950 pounds [7].", source(1, LETTER))
    assert got.accepted and got.receipts()[0].chunk_id == 101


def test_a_spelled_out_number_is_a_number():
    """"Six" against a source that says "two" is a wrong figure, not a wrong word."""
    got = run("The tenant must give six months notice [1].",
              source(1, "The tenant must give two months notice. Pets are not allowed."))
    assert not got.accepted and "figure 6" in dropped_reasons(got)
    got = run("The tenant must give two months notice [1].",
              source(1, "The tenant must give 2 months notice. Pets are not allowed."))
    assert got.accepted, dropped_reasons(got)
    assert extract_numbers("no one came") == set() and extract_numbers("one month") == {"1"}


def test_a_sentence_the_source_does_not_support_is_dropped():
    got = run("The garden was landscaped by a team of specialists in spring [1].", source(1, LETTER))
    assert not got.accepted and "supported" in dropped_reasons(got)


def test_meaning_reversed_is_dropped_even_though_every_word_is_present():
    got = run("The deposit will not be returned within 10 days of the tenancy ending [1].",
              source(1, LETTER))
    assert not got.accepted and "opposite" in dropped_reasons(got)


def test_words_scattered_across_a_long_passage_do_not_add_up_to_support():
    long_text = ("The pump arrived on Monday. Delivery was signed for at the gate. "
                 "The budget was approved in March. Cover for the shutdown is arranged. "
                 "The training is booked for December. The rota is agreed by Friday.")
    got = run("The pump budget covers the training rota [1].", source(1, long_text))
    assert not got.accepted


def test_a_collage_of_the_documents_own_words_is_not_a_sentence():
    """Every word is in the letter and no two of them are together: bag-of-words
    support cannot see that, adjacency can."""
    got = run("Letter and deposit and scheme and deductions [1].", source(1, LETTER))
    assert not got.accepted and "strings together words" in dropped_reasons(got)



def test_a_restatement_that_reorders_and_skips_words_still_passes():
    """Found by running a real 1.5B model, whose restatements reorder ("the landlord has
    10 days to return the deposit") what the document says ("the deposit will be returned
    within 10 days")."""
    got = run("The landlord has 10 days to return the deposit after the tenancy ends [1].",
              source(1, LETTER + " The landlord is Margaret Okafor."))
    assert got.accepted, dropped_reasons(got)


def test_the_title_and_the_folder_are_evidence_for_a_name_in_the_body():
    """"Two observations were raised against guarding in the final Leeds report" - Leeds is
    in the document's title line and its folder, not in the sentence that has the figure."""
    text = ("Leeds site safety report. Findings from the annual inspection. Two observations "
            "were raised against guarding and one against permit to work.")
    got = run("Two observations were raised against guarding in the Leeds safety report [1].",
              source(1, text, "safety-report-final.pdf"))
    assert got.accepted, dropped_reasons(got)


def test_an_embedding_can_rescue_a_synonym_but_never_a_figure_or_a_name():
    approval = "Approved. Go ahead and buy the licence renewal at the quoted price."
    sources = [source(1, approval, "approval.eml", meta="approval.eml priya.n@acme.com")]
    plain = AnswerAssembler(Verifier(sources))
    plain.feed("Yes, Priya approved the licence purchase [1].")
    plain.flush()
    assert not plain.accepted                                       # "purchase" is not "buy"
    rescued = AnswerAssembler(Verifier(sources, similarity=lambda a, b: 0.9))
    rescued.feed("Yes, Priya approved the licence purchase [1].")
    rescued.flush()
    assert rescued.accepted
    liar = AnswerAssembler(Verifier(sources, similarity=lambda a, b: 0.99))
    liar.feed("Yes, Priya approved the licence purchase of 40,000 pounds for Jonathan [1].")
    liar.flush()
    assert not liar.accepted                                          # figure and name checks stand


def test_a_reordered_restatement_that_keeps_the_documents_phrases_still_passes():
    got = run("Within 10 days of the tenancy ending the deposit will be returned [1].",
              source(1, LETTER))
    assert got.accepted, dropped_reasons(got)


def test_a_single_word_answer_is_not_checkable_and_is_dropped():
    got = run("Yes [1].", source(1, LETTER))
    assert not got.accepted and "too little" in dropped_reasons(got)


# --------------------------------------------------------------------------- 2c: figures, dates, names

def test_a_fabricated_number_dies_in_validation():
    got = run("The deposit is 1,950 pounds [1].", source(1, LETTER))
    assert not got.accepted                       # a short sentence with a wrong figure
    # ...and when everything else in the sentence is right, the figure alone kills it:
    got = run("The deposit is 1,950 pounds and it is held in a government approved scheme [1].",
              source(1, LETTER))
    assert not got.accepted and "1950" in dropped_reasons(got)


def test_a_fabricated_year_and_month_die():
    got = run("The deposit will be returned in 2031 [1].", source(1, LETTER))
    assert not got.accepted
    june = "The tenancy ends in June 2025 and the tenant must give two months notice."
    got = run("The tenancy ends in March 2025 and the tenant must give two months notice [1].",
              source(1, june))
    assert not got.accepted and "month" in dropped_reasons(got)
    got = run("The tenancy ends in June 2025 and the tenant must give two months notice [1].",
              source(1, june))
    assert got.accepted, dropped_reasons(got)


def test_an_invented_name_dies():
    got = run("Jonathan Featherstone signed the letter [1].", source(1, LETTER))
    assert not got.accepted
    # ...and when the rest of the sentence is well supported, the name alone kills it.
    got = run("The deposit of 950 pounds was agreed and held in a government approved scheme "
              "by Jonathan Featherstone [1].", source(1, LETTER))
    assert not got.accepted and "who is not in the source" in dropped_reasons(got)


def test_the_real_name_passes():
    got = run("Margaret Okafor signed the letter [1].", source(1, LETTER))
    assert got.accepted, dropped_reasons(got)


def test_a_date_in_another_notation_is_the_same_date():
    got = run("The audit is on 9 June [1].", source(1, "Audit scheduled for 2025-06-09."))
    assert got.accepted, dropped_reasons(got)


# --------------------------------------------------------------------------- 2b: quotes are verbatim

def test_a_misquote_dies():
    """Misquote-injection: words in quotation marks that the document does not contain."""
    got = run('The letter says "the deposit is strictly non-refundable" [1].', source(1, LETTER))
    assert not got.accepted and "quotation marks" in dropped_reasons(got)


def test_a_real_quotation_passes_and_the_receipt_points_at_it():
    got = run('The letter says "held in a government approved scheme" [1].', source(1, LETTER))
    assert got.accepted, dropped_reasons(got)
    assert "government approved scheme" in got.receipts()[0].quote


def test_a_quotation_with_curly_quotes_is_compared_as_typed_in_the_document():
    text = "He wrote \u201cwe will not renew the licence this year\u201d in his reply."
    got = run('He wrote "we will not renew the licence this year" [1].', source(1, text))
    assert got.accepted, dropped_reasons(got)


# --------------------------------------------------------------------------- numbering and streaming

def test_sources_are_numbered_in_first_mention_order_and_never_renumbered():
    s1, s2, s3 = source(1, LETTER, "a.docx"), source(2, QUOTE, "b.eml"), source(3, MAIL, "c.eml")
    got = run("Go ahead and buy the licence renewal [3]. The deposit is 950 pounds [1]. "
              "The quoted price was approved [3]. Twelve thousand pounds for the site [2].",
              s1, s2, s3)
    assert len(got.accepted) == 4, dropped_reasons(got)
    names =[r.name for r in got.receipts()]
    assert names == ["c.eml", "a.docx", "b.eml"]         # first mention: 3, 1, 2
    texts = [a.text for a in got.accepted]
    assert texts[0].endswith("[1].")                     # source 3 became [1] ...
    assert texts[1].endswith("[2].")                     # ... source 1 became [2]
    assert texts[-1].endswith("[3].")                    # ... and it does not move


def test_streamed_text_is_judged_sentence_by_sentence():
    assembler = AnswerAssembler(Verifier([source(1, LETTER)]))
    text = "The deposit is 950 pounds [1]. It will be returned within 10 days [1]."
    delivered: list[str] = []
    for at in range(0, len(text), 5):
        delivered += [a.text for a in assembler.feed(text[at:at + 5])]
        assert all(sentence.endswith(("[1].",)) for sentence in delivered)
    delivered += [a.text for a in assembler.flush()]
    assert len(delivered) == 2
    # nothing was ever handed out half-written
    assert delivered[0] == "The deposit is 950 pounds [1]."


def test_a_marker_that_arrives_in_pieces_still_belongs_to_the_sentence_before_it():
    """Found by running a real 1.5B model: it streams "...ends. [1]The next", and the
    piece boundary can fall inside the marker."""
    letter = "The landlord has 10 days to return the deposit after the tenancy ends."
    answer = (letter + " [1]" + letter + " [1]")
    for size in (1, 2, 3, 5, 7):
        assembler = AnswerAssembler(Verifier([source(1, letter)]))
        for at in range(0, len(answer), size):
            assembler.feed(answer[at:at + size])
        assembler.flush()
        assert len(assembler.accepted) == 1 and not assembler.dropped, (size, assembler.dropped)


def test_a_leading_marker_and_a_wrong_source_number_are_repaired_not_fatal():
    """A small model writes "[2] The tenant must give two months notice." when the
    supporting source is [1]. The sentence is true; the receipt is the source that
    actually supports it."""
    s1 = source(1, "The tenant must give two months notice. Pets are not allowed.", "a.pdf")
    s2 = source(2, "Approved. Go ahead and buy the licence renewal.", "b.eml")
    got = run("[2] The tenant must give two months notice.", s1, s2)
    assert [a.body for a in got.accepted] == ["The tenant must give two months notice"]
    assert got.receipts()[0].name == "a.pdf"


def test_a_wrong_claim_is_not_repaired_by_hunting_for_a_source_that_fits():
    s1 = source(1, "The tenant must give two months notice.", "a.pdf")
    s2 = source(2, "Approved. Go ahead and buy the licence renewal.", "b.eml")
    got = run("The tenant must give six months notice [2].", s1, s2)
    assert not got.accepted


def test_the_same_sentence_twice_is_shown_once():
    got = run("The deposit is 950 pounds [1]. The deposit is 950 pounds [1].", source(1, LETTER))
    assert len(got.accepted) == 1


def test_a_verifier_that_crashes_fails_closed(monkeypatch):
    def explode(self, sentence):
        raise RuntimeError("boom")

    monkeypatch.setattr(Verifier, "_verify", explode)
    got = run("The deposit is 950 pounds [1].", source(1, LETTER))
    assert not got.accepted and "could not be checked" in dropped_reasons(got)


# --------------------------------------------------------------------------- the independent audit

def _turn(text: str, receipts: list[Receipt], kind: str = "answer") -> ChatTurn:
    return ChatTurn("assistant", text, receipts=receipts, kind=kind)


def test_audit_finds_an_unreceipted_sentence_in_a_finished_turn():
    receipt = Receipt(1, "C:/x/a.docx", "a.docx", "The deposit is 950 pounds.")
    assert audit_turn(_turn("The deposit is 950 pounds [1].", [receipt])) == []
    assert audit_turn(_turn("The deposit is 950 pounds [1]. And more besides.", [receipt]))
    assert audit_turn(_turn("The deposit is 950 pounds [2].", [receipt]))
    assert audit_turn(_turn("The deposit is 950 pounds [1].",
                            [Receipt(1, "x", "x", "")]))       # an empty quote is no receipt


def test_audit_leaves_system_written_turns_alone():
    assert audit_turn(_turn("47 files - counted across everything.", [], kind="aggregate")) == []
    assert audit_turn(ChatTurn("user", "How many?", kind="answer")) == []


def test_markers_parse_in_every_shape_a_model_writes():
    assert split_markers("It is 950 [1][2].")[1] == [1, 2]
    assert split_markers("It is 950 [1, 2].")[1] == [1, 2]
    assert normalise_marker_placement("It is 950. [1]") == "It is 950 [1]."


# --------------------------------------------------------------------------- the property

_WORDS = ("deposit pounds tenancy landlord scheme returned deductions boiler service Margaret "
          "Okafor signed letter agreement 950 10 days government approved held Jonathan "
          "purchase invoice 2031 March June garden quote licence never not").split()


@settings(max_examples=150, deadline=None)
@given(words=st.lists(st.sampled_from(_WORDS), min_size=2, max_size=12),
       marker=st.sampled_from(["[1]", "[2]", "[1][2]", "[9]", ""]))
def test_whatever_the_verifier_accepts_is_present_in_the_document_it_cites(words, marker):
    """The guarantee, as a property: accepted sentences only contain words and
    figures that appear in one of the documents they cite."""
    s1, s2 = source(1, LETTER, "a.docx"), source(2, QUOTE, "b.eml")
    sentence = " ".join(words) + f" {marker}."
    got = run(sentence, s1, s2)
    for accepted in got.accepted:
        evidence = " ".join(LETTER if n == 1 else QUOTE
                            for n in accepted.verdict.cited)
        evidence += " a.docx b.eml"
        have = support_tokens(evidence)
        wanted = support_tokens(accepted.body)
        assert len(wanted & have) / len(wanted) >= SUPPORT - 1e-9, (accepted.body, wanted - have)
        # ...and the exact things - every figure - are never approximate:
        for number in extract_numbers(accepted.body):
            assert number in extract_numbers(evidence), (accepted.body, number)
        for number in extract_numbers(accepted.body):
            assert number in extract_numbers(evidence), (accepted.body, number)
        assert re.search(r"\[\d+\]", accepted.text)
