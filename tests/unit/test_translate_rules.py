r"""Filters from a sentence, without a 4GB model.

Layer: L4. Deterministic and offline, so every one of these runs anywhere -
which is the point of the file under test.

**The bug these tests exist because of.** The first version asked the store
for kinds called `from`, `to` and `type`. Those are the *operator* names; the
store's are `sender`, `recipient` and `ext`, and `distinct_values` returns
`[]` for a kind it does not know. So no person chip ever fired, and the "only
offer a type the corpus actually holds" safeguard passed everything, because
the set it checked against was always empty. Nothing raised. The output looked
plausible. `test_a_name_the_index_does_not_know_is_not_a_chip` and
`test_a_type_the_corpus_does_not_hold_is_not_offered` are the two that would
have caught it.
"""

from __future__ import annotations

import pathlib
import tempfile
from datetime import date

import pytest

from app.search.translate_rules import KIND_WORDS, Chip, read

TODAY = date(2026, 8, 27)


@pytest.fixture(scope="module")
def store():
    from app.storage.sqlite_store import SqliteStore
    from tests.fixtures.evaluation import load_into

    built = SqliteStore(pathlib.Path(tempfile.mkdtemp()) / "rules.db").connect()
    load_into(built)
    return built


def _filters(sentence, store=None):
    return {chip.as_filter() for chip in read(sentence, store, today=TODAY).chips}


def _field(sentence, field, store=None):
    for chip in read(sentence, store, today=TODAY).chips:
        if chip.field == field:
            return chip.value
    return None


# --------------------------------------------------------------------------
# People
# --------------------------------------------------------------------------

def test_a_name_the_index_knows_becomes_a_sender_filter(store):
    assert _field("emails from Chris about buying a licence", "from", store) == (
        "chris.yates@acme.com")


def test_a_name_the_index_does_not_know_is_not_a_chip(store):
    """**High precision by construction.** The rules never invent a filter for
    a value the corpus cannot satisfy, so the failure mode is a chip that does
    not appear rather than one that silently empties the page."""
    assert _field("emails from Mortimer about the licence", "from", store) is None


def test_an_ambiguous_first_name_is_refused():
    """**Exactly one match, or nothing.** "Chris" matching two people is a coin
    toss, and a chip that quietly picks one of them is worse than no chip
    because the results then look complete."""
    class _Two:
        def distinct_values(self, kind, limit=40, **_):
            if kind == "sender":
                return ["chris.yates@acme.com", "chris.doyle@acme.com"]
            return []

    assert _field("emails from Chris", "from", _Two()) is None


def test_word_order_decides_sender_from_recipient(store):
    r"""**The same verb, opposite filters.**

    "the report Dave sent me" is a `from:` question; "what did I send to
    Priya" is a `to:` one. The verb cannot tell them apart - the pronoun's
    position can. The first version of this rule read both as `to:`.
    """
    assert _field("the invoice Dave sent me last year", "from", store) == (
        "dave.smith@acme.com")
    assert _field("what did I send to Priya", "to", store) == "priya.n@acme.com"


def test_only_one_person_becomes_a_filter(store):
    """Two names is a conversation, and two `from:` filters AND into nothing -
    a chip that empties the page. The second name stays as text, where it
    still matches."""
    found = [chip for chip in read("what Dave said to Priya", store,
                                   today=TODAY).chips if chip.field in
             ("from", "to")]
    assert len(found) <= 1


def test_a_recipient_stored_as_json_is_unpacked(store):
    r"""`distinct_values("recipient")` returns `'["me@acme.com"]'`, because
    that is how the column is stored. Left unpacked, every recipient lookup
    silently matched nothing."""
    from app.search.translate_rules import _known

    assert "priya.n@acme.com" in _known(store, "recipient")
    assert not any(value.startswith("[") for value in _known(store, "recipient"))


# --------------------------------------------------------------------------
# Kinds of file
# --------------------------------------------------------------------------

def test_an_everyday_word_becomes_a_type_filter(store):
    assert _field("spreadsheet with the audit findings", "type", store) == "xlsx"


def test_a_type_the_corpus_does_not_hold_is_not_offered(store):
    """The fixture corpus has no images, so "photos" must produce no `type:`
    chip - a filter that finds nothing is worse than no filter."""
    assert _field("photos from June 2024", "type", store) is None


def test_a_word_resolves_to_whichever_extension_the_corpus_has(store):
    """"drawings" means dwg, dxf or pdf. This corpus holds the pump station
    drawings as a pdf, so that is the filter offered."""
    assert _field("drawings of the pump station", "type", store) == "pdf"


def test_without_a_store_the_rules_still_read_what_they_can():
    """A machine with no index yet still gets dates and kinds - fewer chips,
    not none, and never a crash."""
    found = _filters("the report from June 2024")
    assert "type:pdf" in found
    assert "after:2024-06-01" in found


# --------------------------------------------------------------------------
# Attachments (§5b)
# --------------------------------------------------------------------------

@pytest.mark.parametrize("sentence", [
    "emails with something attached about the licence",
    "the message that had an attachment",
    "mail with attachments from Chris",
    "the email with an attachment",
])
def test_asking_for_something_attached_becomes_the_filter(sentence, store):
    r"""**`evaluate --builtin` scored its attachment question at 0%.**

    The filter works perfectly and no plain sentence had ever produced it:
    `has:attachment` was reachable only by typing the operator, which is
    exactly the knowledge tab one exists not to require.
    """
    assert "has:attachment" in _filters(sentence, store)


@pytest.mark.parametrize("sentence", [
    "the attached report about safety",
    "I attached the wrong file",
    "the licence quote from Chris",
])
def test_naming_an_attached_document_is_not_asking_for_a_filter(sentence, store):
    """"The attached report" is somebody naming a document, not asking for
    every message that carried one."""
    assert "has:attachment" not in _filters(sentence, store)


def test_the_chip_says_it_in_plain_words():
    """"has attachment" is the operator read aloud. Nobody says that."""
    assert Chip("has", "attachment").label() == "with an attachment"


# --------------------------------------------------------------------------
# Dates
# --------------------------------------------------------------------------

def test_a_bare_year_means_that_year():
    assert _filters("the audit findings 2024") >= {
        "after:2024-01-01", "before:2024-12-31"}


def test_before_a_year_means_before_it_not_during_it():
    """The bare year means the year; only the word changes that. This is the
    same distinction the parser had to be corrected on."""
    assert "before:2023-01-01" in _filters("reports before 2023")
    assert "after:2023-12-31" in _filters("reports after 2023")


def test_a_month_name_becomes_that_month():
    assert _filters("photos from June 2024") >= {
        "after:2024-06-01", "before:2024-06-30"}


def test_february_in_a_leap_year_has_twenty_nine_days():
    assert "before:2024-02-29" in _filters("invoices February 2024")
    assert "before:2023-02-28" in _filters("invoices February 2023")


def test_last_year_is_read_from_today_not_hardcoded():
    assert _filters("the licence quote last year") >= {
        "after:2025-01-01", "before:2025-12-31"}


@pytest.mark.parametrize("phrase", [
    "about six months ago", "last summer", "a while back", "recently",
])
def test_vague_dates_are_deliberately_not_read(phrase):
    """**A wrong date filter hides documents silently**, which is the failure
    this whole order exists to remove. These mean different spans to different
    people, so they stay as words - where they cost nothing."""
    assert not [chip for chip in read(f"the report {phrase}", today=TODAY).chips
                if chip.field in ("after", "before")]


# --------------------------------------------------------------------------
# Mail, residue and the shape of the answer
# --------------------------------------------------------------------------

def test_a_verb_says_this_is_about_mail():
    """**Verbs, because a noun is ambiguous and a verb is not.** "message"
    could be a document's subject; "emailed" could not."""
    assert read("what Dave emailed me", today=TODAY).mail
    assert not read("the pump station drawings", today=TODAY).mail


def test_the_typed_sentence_is_never_altered(store):
    """§3b: chips, not rewrites. What makes a wrong guess cost a glance is
    that the words are still there, unchanged, beside it."""
    sentence = "the invoice Dave sent me last year"
    assert read(sentence, store, today=TODAY).sentence == sentence


def test_the_residue_is_what_a_model_would_be_given(store):
    """§3c: rules run first and Ollama receives only what they could not
    claim, so the prompt is smaller and the deterministic part stays
    deterministic."""
    reading = read("the invoice Dave sent me last year", store, today=TODAY)
    assert "dave" not in reading.residue.lower()
    assert "invoice" not in reading.residue.lower()
    assert "invoice" in reading.sentence


def test_nothing_recognised_is_an_honest_empty_answer(store):
    reading = read("volcano homework essay", store, today=TODAY)
    assert reading.chips == () and not reading.mail and not reading.found


def test_an_empty_sentence_is_not_an_error():
    assert read("", today=TODAY).chips == ()
    assert read(None, today=TODAY).chips == ()


def test_a_store_that_raises_costs_chips_not_the_search():
    """**Never raises.** Interpret is a convenience; a store having a bad day
    must cost the chips, never the query."""
    class _Broken:
        def distinct_values(self, *_args, **_kwargs):
            raise RuntimeError("the index is locked")

    assert read("emails from Chris", _Broken(), today=TODAY).mail is True


def test_a_chip_says_what_it_means_in_plain_words():
    assert Chip("from", "dave.smith@acme.com").label() == "from dave.smith@acme.com"
    assert Chip("type", "pdf").label() == "pdf files"
    assert Chip("after", "2024-01-01").label() == "after 2024-01-01"


def test_a_value_with_a_space_is_quoted_for_the_parser():
    assert Chip("from", "Dave Smith").as_filter() == 'from:"Dave Smith"'


def test_every_kind_word_lists_at_least_one_extension():
    """A word mapping to nothing is a rule that can never fire - the kind of
    dead entry this codebase keeps finding."""
    assert all(options for options in KIND_WORDS.values())


# --------------------------------------------------------------------------
# Where the chips are read (§3b)
# --------------------------------------------------------------------------

def test_chips_are_offered_and_the_query_is_left_alone(store):
    """**§3b in one assertion.** What runs is what was typed; the filters sit
    beside it, one click from being applied. That is what makes a wrong guess
    cost a glance rather than a page of results."""
    from app.search.policy import SEARCH, for_surface
    from app.ui.presenter import chips_for

    sentence = "the invoice Dave sent me last year"
    chips = chips_for(store, sentence, for_surface(SEARCH))
    assert "from dave.smith@acme.com" in [chip.label() for chip in chips]
    assert read(sentence, store, today=TODAY).sentence == sentence


def test_the_code_tab_offers_no_chips(store):
    """`auto_chips` off means off - the power surfaces keep this behind the
    Interpret button, where somebody asked for it."""
    from app.search.policy import CODE, for_surface
    from app.ui.presenter import chips_for

    assert chips_for(store, "the invoice Dave sent me last year",
                     for_surface(CODE)) == ()


def test_the_retrieval_path_still_cannot_reach_the_translator():
    r"""**The guard that moved this out of the engine, asserted from this
    side too.**

    `engine.py` must not know translation exists - the retrieval path is
    never allowed to spend a second on a model. Chips are a thing said *about*
    a query rather than part of running one, so they live with the translator
    on the presenter side. This is the second time a module whose name
    contains "translate" was imported into the engine in this order, and the
    guard caught it both times.
    """
    import pathlib

    import app.search.engine as engine

    text = pathlib.Path(engine.__file__ or "").read_text(encoding="utf-8")
    assert "translate" not in text.replace("# ", "")


def test_the_tables_are_marked_for_the_deferred_tuning_pass():
    """§3d: the owner deferred tuning to after the indexing work, and asked
    for a `[TUNE]` marker on each table so the deferral is visible where the
    tables are, not only in the order."""
    import app.search.translate_rules as rules

    source = pathlib.Path(rules.__file__ or "").read_text(encoding="utf-8")
    assert source.count("[TUNE]") >= 5
