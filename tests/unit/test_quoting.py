"""Cutting quoted chains and signatures out of mail before it is indexed.

Layer: L2

Two failure directions, and they are not symmetric.

**Missing a quote** leaves duplication: the index is bigger than it needs to be
and results carry near-duplicates. Annoying, measurable, and fixed by a
re-index.

**Cutting too much** silently loses a real message. Nobody notices, because the
symptom is a document that simply never appears in any search - and there is
nothing to look at that would reveal why.

So every test here that asserts a cut is paired with one asserting that
something similar is *not* cut, and the module errs towards keeping too much.
"""

from __future__ import annotations

import pytest

from app.extract.quoting import MIN_KEPT_CHARS, strip_quoted


# -- the markers that mean "everything below is a copy" ----------------------

@pytest.mark.parametrize(("name", "body"), [
    ("outlook", "Yes, happy with that price.\n\n"
                "-----Original Message-----\nFrom: Dave\nSent: Monday\nThe licence quote."),
    ("forwarded", "See below.\n\n--- Forwarded message ---\nFrom: Dave\nThe original."),
    ("underscores", "Approved.\n\n" + "_" * 32 + "\nFrom: Chris\nSubject: Licence"),
    ("on-wrote", "Agreed, go ahead.\n\n"
                 "On Tue, 3 Jun 2025 at 14:22, Dave Smith <dave@acme.com> wrote:\nOriginal."),
    ("outlook-header", "Can you check this?\n\n"
                       "From: Dave Smith\nSent: Monday 3 June\nTo: Chris\nSubject: Licence"),
])
def test_a_quoted_chain_is_cut_at_its_marker(name: str, body: str) -> None:
    result = strip_quoted(body)
    assert result.changed, f"{name}: nothing was stripped"
    assert "Dave" not in result.text or name == "on-wrote" and False
    assert result.removed_chars > 0


def test_a_short_reply_is_kept_even_though_it_is_shorter_than_the_quote() -> None:
    """The case the first version of this module got wrong.

    A floor of 40 characters was meant to catch a marker matching at position
    zero. But replies in a thread are short by nature - "Approved." is nine
    characters - and those are exactly the messages this exists to separate from
    the chain they quote. The floor silently kept every one of them whole while
    reporting success.
    """
    result = strip_quoted("Approved.\n\n" + "_" * 32 + "\nFrom: Chris\nLong original message here.")

    assert result.text == "Approved."
    assert MIN_KEPT_CHARS <= 5, "a floor above a few characters discards real replies"


def test_inline_quoted_lines_are_removed_but_the_answers_are_kept() -> None:
    """A reply interleaved with `>` quotes has no single cut point, so the
    quoted lines go individually and what the person actually wrote stays."""
    result = strip_quoted(
        "See my answers below.\n\n"
        "> what is the licence cost\n"
        "Twelve thousand a year.\n"
        "> and the renewal date\n"
        "March."
    )
    assert "Twelve thousand a year." in result.text
    assert "March." in result.text
    assert "what is the licence cost" not in result.text


# -- signatures --------------------------------------------------------------

@pytest.mark.parametrize(("name", "body"), [
    ("rfc-delimiter", "Please raise the PO.\n\n-- \nChris Yates\nAcme Ltd\n0113 555 0000"),
    ("iphone", "Yes fine, buy it.\n\nSent from my iPhone"),
    ("outlook-app", "Approved, thanks.\n\nGet Outlook for iOS"),
    ("confidentiality", "Buy the licence please.\n\n"
                        "This email is confidential and intended solely for the addressee."),
])
def test_a_signature_is_cut(name: str, body: str) -> None:
    """A signature repeats on every message somebody has ever sent, so it
    carries no information about *this* one - and it makes every message from
    one person look alike to a retriever."""
    result = strip_quoted(body)
    assert result.changed, f"{name}: signature was kept"
    assert "Acme Ltd" not in result.text
    assert "iPhone" not in result.text


def test_signature_stripping_can_be_turned_off() -> None:
    body = "Please raise the PO.\n\n-- \nChris Yates"
    assert not strip_quoted(body, strip_signatures=False).changed


# -- and now the direction that must never happen ----------------------------

@pytest.mark.parametrize("body", [
    "Short note about the licence with no quoting at all.",
    "We agreed on Monday that Dave wrote the original message, so nothing to do.",
    "The report mentions an original message from the supplier.",
    "Costs are 100 -- 200 per seat depending on volume.",
    "Please review sections 1 -- 4 before Friday.",
])
def test_ordinary_prose_is_never_cut(body: str) -> None:
    """Every marker is anchored to the start of a line and specific enough that
    prose cannot trigger it. `wrote:` and `-----Original Message-----` appear in
    real sentences, and truncating one loses a document with no trace."""
    result = strip_quoted(body)
    assert not result.changed, f"prose was cut: {result.text!r}"
    assert result.text == body


def test_a_message_that_is_only_a_signature_is_kept_whole() -> None:
    """Cutting at position zero leaves nothing. Keeping it costs a little
    duplication; the alternative loses the message entirely."""
    body = "-- \nthat is the whole message"
    assert strip_quoted(body).text == body


def test_empty_and_whitespace_are_returned_unchanged() -> None:
    assert strip_quoted("").text == ""
    assert strip_quoted("   ").text == "   "


def test_the_result_reports_what_it_removed_and_why() -> None:
    """§6 asks for the effect to be measurable. A silent optimisation cannot be
    checked, and this one changes what ends up in the index."""
    result = strip_quoted("Yes.\n\n-----Original Message-----\n" + "x" * 500)

    assert result.removed_chars > 500
    assert result.original_chars == len(result.text) + result.removed_chars
    assert "quoted" in result.reason


# -- the effect on a real thread ---------------------------------------------

def test_a_deep_thread_stops_growing_quadratically() -> None:
    """The whole reason for the module.

    Each reply quotes everything before it, so an untouched twelve-message
    thread carries the first message twelve times. Stripped, each message
    contributes only what its author actually wrote.
    """
    history = ""
    total_raw = 0
    total_stripped = 0
    for depth in range(12):
        message = f"Reply number {depth}, with some content of its own.{history}"
        total_raw += len(message)
        total_stripped += len(strip_quoted(message).text)
        history = (
            "\n\n-----Original Message-----\n"
            f"From: someone{depth}\n{message}"
        )

    assert total_stripped < total_raw / 3, (
        f"stripping saved too little: {total_raw} -> {total_stripped}"
    )


def test_every_mail_path_strips_because_they_share_one_function() -> None:
    """EML, MSG, PST-via-Outlook and PST-via-libpff all call
    `build_email_document`. Stripping there rather than in each extractor is
    what stops four implementations disagreeing about what a thread looks
    like."""
    from pathlib import Path

    from app.extract.email_files import build_email_document

    document = build_email_document(
        Path("x.eml"),
        subject="Licence renewal", sender="chris@acme.com",
        recipients=["me@acme.com"], sent_at=None, conversation=None,
        body="Yes, buy it.\n\n-----Original Message-----\nFrom: Dave\nLong history.",
        attachments=[],
    )

    assert "Yes, buy it." in document.text
    assert "Long history" not in document.text
    # The headers still carry the sender and subject, which is how "the email
    # from Chris about the licence" finds anything at all.
    assert "chris@acme.com" in document.text
    assert "Licence renewal" in document.text
