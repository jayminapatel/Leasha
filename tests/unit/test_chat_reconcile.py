"""Natural prose in, only what the files support out.

Layer: L8b (no Qt, no model). Owner requirement 2026-09-20, point 3: a claim about what is IN the
archive must be supported by a retrieved passage; unsupported claims are dropped or softened,
**not the whole answer**; a partial useful answer beats a refusal.
"""

from __future__ import annotations

from app.chat.context import Piece, Source
from app.chat.reconcile import audit_answer, reconcile
from app.chat.types import ChatTurn, Receipt
from app.chat.verify import Verifier

AGREEMENT = ("The tenancy starts on 1 March 2024. The rent is 1,200 pounds per month. "
             "The tenant must give two months notice in writing. The landlord is Margaret Okafor. "
             "The deposit of 950 pounds will be returned within 10 days of the tenancy ending.")
REPORT = ("Leeds site safety report. Two observations were raised against guarding. "
          "The inspection was carried out by Helen Marsh on 4 March.")


def src(n, name, text, path=None):
    return Source(n=n, file_id=n, path=path or f"C:/docs/{name}", name=name,
                  pieces=[Piece(n * 10, text)], passage=text, meta=name)


def verifier():
    return Verifier([src(1, "tenancy-agreement.pdf", AGREEMENT), src(2, "safety-report.pdf", REPORT)])


def kept(raw, **kw):
    return reconcile(raw, verifier(), **kw)


# --------------------------------------------------------------------------- cited sentences

def test_a_supported_sentence_is_kept_and_a_fabricated_one_beside_it_is_taken_out():
    rec = kept("The rent is 1,200 pounds per month [1]. The rent will double next year to 2,400 pounds [1].")
    assert rec.text == "The rent is 1,200 pounds per month [1]."
    assert rec.supported == 1 and rec.partial
    assert "2,400" in rec.dropped[0][0]
    assert len(rec.receipts) == 1 and "1,200 pounds" in rec.receipts[0].quote


def test_the_whole_answer_is_not_blocked_by_one_bad_sentence_a_partial_answer_survives():
    raw = ("The tenant must give two months notice in writing [1]. The landlord is Margaret Okafor [1]. "
           "Helen Marsh signed the lease in Lisbon [1].")
    rec = kept(raw)
    assert rec.supported == 2 and len(rec.dropped) == 1 and "Lisbon" in rec.dropped[0][0]
    assert "two months notice" in rec.text and "Margaret Okafor" in rec.text and "Lisbon" not in rec.text


def test_a_marker_for_a_source_that_was_never_shown_is_dropped_unless_one_source_really_says_it():
    invented = kept("The rent will be 9,999 pounds per month [9].")
    assert invented.text == "" and not invented.has_support
    # ...but a true sentence with a wrong number is repaired to the source that holds it
    # (the guarantee is that the receipt supports the sentence, not that the model counted right)
    repaired = kept("The rent is 1,200 pounds per month [9].")
    assert repaired.text == "The rent is 1,200 pounds per month [1]." and repaired.receipts[0].name == "tenancy-agreement.pdf"


def test_a_misquote_is_dropped_and_a_real_quotation_is_kept():
    rec = kept('The letter says "the deposit is non-refundable in all cases" [1]. '
               'It says "will be returned within 10 days of the tenancy ending" [1].')
    assert rec.supported == 1 and "non-refundable" not in rec.text and "returned within 10 days" in rec.text


def test_a_reversed_meaning_is_dropped():
    rec = kept("The deposit will not be returned within 10 days of the tenancy ending [1].")
    assert not rec.has_support


def test_a_right_claim_with_the_wrong_source_number_is_repaired_to_the_source_that_holds_it():
    rec = kept("Two observations were raised against guarding [1].")            # it is in source 2
    assert rec.supported == 1 and rec.text == "Two observations were raised against guarding [1]."
    assert rec.receipts[0].name == "safety-report.pdf"                            # and the receipt is the real one


def test_source_numbers_are_renumbered_by_first_mention_and_receipts_line_up():
    rec = kept("Two observations were raised against guarding [2]. The rent is 1,200 pounds per month [1].")
    assert rec.text == ("Two observations were raised against guarding [1]. "
                        "The rent is 1,200 pounds per month [2].")
    assert [r.name for r in rec.receipts] == ["safety-report.pdf", "tenancy-agreement.pdf"]
    assert rec.receipts[0].quote and rec.receipts[1].quote


# --------------------------------------------------------------------------- sentences with no marker

def test_an_uncited_sentence_that_says_what_a_passage_says_is_attached_to_it():
    rec = kept("The landlord is Margaret Okafor.")
    assert rec.text == "The landlord is Margaret Okafor [1]." and rec.supported == 1


def test_an_uncited_sentence_that_contradicts_a_passage_is_dropped():
    rec = kept("The rent is 1,500 pounds per month.")
    assert rec.dropped and not rec.has_support


def test_an_uncited_sentence_with_an_invented_specific_is_dropped_but_plain_connective_prose_stays():
    rec = kept("Here is what I found. The rent is 1,200 pounds per month [1]. "
               "The inspector was Bartholomew Quill. That is a lot of detail, so ask me if you want more.")
    assert "Here is what I found." in rec.text and "ask me if you want more" in rec.text
    assert "Bartholomew" not in rec.text and rec.general == 2
    assert any("Bartholomew" in s for s, _r in rec.dropped)


def test_what_the_person_typed_themselves_is_not_an_invented_name():
    asked = "What is the rent for Bartholomew?"
    rec = kept("The rent for Bartholomew is 1,200 pounds per month [1].", known_text=asked)
    assert rec.supported == 1 and not rec.dropped and "Bartholomew" in rec.text    # kept as written
    strangers = kept("The rent for Bartholomew is 1,200 pounds per month [1].")     # nobody said that name
    assert not strangers.has_support
    smuggled = kept("The rent for Bartholomew is 9,999 pounds per month [1].", known_text=asked)
    assert not smuggled.has_support                                                   # a typed name excuses the name, not the figure


def test_a_paragraph_that_says_it_is_general_knowledge_may_carry_specifics_no_passage_has():
    rec = kept("The rent is 1,200 pounds per month [1].\n\n"
               "In general, tenancy deposits in England are protected by one of three schemes since 2007.")
    assert "since 2007" in rec.text and rec.supported == 1


def test_but_general_knowledge_cannot_misquote_the_files():
    rec = kept("The rent is 1,200 pounds per month [1].\n\n"
               'In general, the agreement says "all rent is payable in advance for the year".')
    assert "payable in advance" not in rec.text


# --------------------------------------------------------------------------- structure

def test_markdown_structure_survives_and_emptied_lines_go():
    raw = ("## What I found\n\n"
           "- The rent is 1,200 pounds per month [1].\n"
           "- The rent will triple in June [1].\n"
           "- **The landlord** is Margaret Okafor [1].\n\n"
           "1. Two observations were raised against guarding [2].\n")
    rec = kept(raw)
    lines = rec.text.split("\n")
    assert lines[0] == "## What I found"
    assert "- The rent is 1,200 pounds per month [1]." in lines
    assert all("triple" not in line for line in lines)                            # the dropped bullet is gone whole
    assert "- **The landlord** is Margaret Okafor [1]." in lines
    assert "1. Two observations were raised against guarding [2]." in lines


def test_code_fences_are_left_alone_even_with_things_that_look_like_claims():
    raw = "Here is code:\n\n```python\nrent = 9999  # not a claim about the files [4]\n```\n\nThe rent is 1,200 pounds per month [1]."
    rec = kept(raw)
    assert "rent = 9999  # not a claim about the files [4]" in rec.text and rec.supported == 1


def test_a_table_row_that_fails_is_removed_and_the_rest_stays():
    raw = "| Item | Value |\n|---|---|\n| Rent [1] | 1,200 pounds per month [1] |\n| Bonus | 5,000 pounds [1] |"
    rec = kept(raw)
    assert "1,200" in rec.text and "5,000" not in rec.text


def test_it_never_raises_and_fails_closed():
    assert reconcile(None, verifier()).text == ""                                  # type: ignore[arg-type]
    rec = reconcile("The rent is 1,200 pounds per month [1].", None)               # type: ignore[arg-type]
    assert not rec.has_support and rec.text == ""


def test_an_answer_of_only_conversation_has_no_support():
    rec = kept("Sure! Let me think about that for you. I hope this helps.")
    assert rec.general == 3 and not rec.has_support                               # the engine reads this as "nothing found"


# --------------------------------------------------------------------------- the independent audit

def _turn(text, receipts):
    return ChatTurn("assistant", text, receipts=receipts, kind="answer")


R1 = Receipt(1, "C:/a.pdf", "a.pdf", "The rent is 1,200 pounds per month.", "", 1)


def test_the_audit_accepts_a_reconciled_answer():
    rec = kept("The rent is 1,200 pounds per month [1]. Happy to help with the rest.")
    assert audit_answer(_turn(rec.text, rec.receipts)) == []


def test_the_audit_flags_a_marker_with_no_receipt_and_a_receipt_with_no_quote():
    assert audit_answer(_turn("The rent is 1,200 pounds [2].", [R1]))
    assert audit_answer(_turn("The rent is 1,200 pounds [1].", [Receipt(1, "C:/a.pdf", "a.pdf", "", "", 1)]))


def test_the_audit_flags_an_invented_specific_in_an_unmarked_sentence_but_not_prose():
    assert audit_answer(_turn("The rent is 1,200 pounds [1]. It rose to 9,999 pounds last May.", [R1]))
    assert audit_answer(_turn("The rent is 1,200 pounds [1]. Ask Bartholomew Quill.", [R1]))
    assert audit_answer(_turn("The rent is 1,200 pounds [1]. Anything else you want to know?", [R1])) == []
    general = "The rent is 1,200 pounds [1].\n\nIn general, schemes were introduced in 2007."
    assert audit_answer(_turn(general, [R1])) == []


def test_the_audit_ignores_turns_that_make_no_claim_about_files():
    assert audit_answer(ChatTurn("assistant", "Hello, 9,999 friends!", kind="chat")) == []
    assert audit_answer(ChatTurn("user", "hi", kind="answer")) == []


# --------------------------------------------------------------------------- softening, and model habits

def test_a_fluent_tail_the_passage_does_not_carry_is_cut_back_to_the_clause_it_does_support():
    """Seen with a real model (mistral, 2026-09-20): "Your landlord is Margaret Okafor, as stated in the
    deposit agreement letter and the email reply from her." - the name is right, the tail is padding."""
    padded = ("Your landlord is Margaret Okafor, as stated in the deposit agreement letter and the "
              "email reply from her solicitor about the boiler last winter")
    rec = kept(padded + " [1].")
    assert rec.text == "Your landlord is Margaret Okafor [1]." and rec.supported == 1 and not rec.dropped


def test_softening_never_rescues_a_wrong_figure_a_wrong_name_or_a_reversed_meaning():
    for wrong in ("The rent is 1,500 pounds per month, as stated in the tenancy agreement [1].",
                  "The landlord is Bartholomew Quill, as stated in the tenancy agreement [1].",
                  "The deposit will not be returned within 10 days, as stated in the tenancy agreement [1]."):
        assert not kept(wrong).has_support, wrong


def test_markers_written_as_a_list_are_read_as_one_group():
    rec = kept("Two observations were raised against guarding. [2], [1]")
    assert "[1]" in rec.text and rec.supported == 1
    both = kept("The landlord is Margaret Okafor and two observations were raised against guarding [1], [2].")
    assert both.text.count("[") <= 2


def test_the_engines_own_words_are_not_shown_back_to_the_model():
    from app.chat.memory import turn_text

    general = ChatTurn("assistant", "I couldn't find that in your files.\n\n**Not from your files:** A PST file is a mailbox.",
                       kind="general")
    assert turn_text(general) == "A PST file is a mailbox."
    partial = ChatTurn("assistant", "The rent is 1,200 pounds [1]. I could not confirm the rest of that from your files.")
    assert turn_text(partial) == "The rent is 1,200 pounds."
    assert turn_text(ChatTurn("assistant", "I could not confirm the rest of that from your files.")) != ""


def test_a_limit_stated_plainly_an_unmarked_sentence_that_shares_little_with_any_passage_is_conversation():
    """Not a guarantee, and not pretended to be one: a sentence with **no marker**, no invented specific
    and less than half its words in any passage is treated as conversation and kept. The prompt asks a
    model to mark every claim about the files, the checks above catch what a passage contradicts or a
    figure/name/quotation that no passage has, and `audit_answer` re-checks the finished turn - but a
    vague, unmarked, general-sounding claim about the files is not something a lexical check can see."""
    rec = kept("The landlord is generally responsible for repairs to the structure of the building.")
    assert rec.general == 1 and rec.text.endswith("building.")
