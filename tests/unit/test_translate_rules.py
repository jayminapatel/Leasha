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


def test_the_code_tab_offers_the_same_chips_as_search(store):
    """1 October 2026, owner: plain English is read the same way on every tab. This was
    `test_the_code_tab_offers_no_chips`."""
    from app.search.policy import CODE, SEARCH, for_surface
    from app.ui.presenter import chips_for

    sentence = "the invoice Dave sent me last year"
    assert chips_for(store, sentence, for_surface(CODE)) ==         chips_for(store, sentence, for_surface(SEARCH))


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


# --------------------------------------------------------------------------
# Applied, not only offered (owner decision 2026-09-27)
# --------------------------------------------------------------------------
#
# "mail from 2017" typed into the Search tab ran as `"mail" OR "2017"`. The
# owner decided that the readings the rules are sure of are *applied*: the
# words become real filters (shown as removable chips) and leave the search
# terms. `read()` above is unchanged - these are `apply()`.

from app.search.query import parse_query  # noqa: E402
from app.search.translate_rules import apply  # noqa: E402

TODAY_APPLY = date(2026, 9, 27)


class _OneJohn:
    """A store that knows exactly one John, and holds mail and documents."""

    def distinct_values(self, kind, limit=40, **_):
        return {
            "sender": ["john.smith@acme.com", "dave.smith@acme.com"],
            "recipient": ['["me@acme.com"]'],
            "ext": ["pst", "eml", "pdf", "docx"],
        }.get(kind, [])


def _applied(sentence, store=None, **kwargs):
    return apply(sentence, store if store is not None else _OneJohn(),
                 today=TODAY_APPLY, **kwargs)


def _year_2017(parsed):
    return parsed.after == date(2017, 1, 1) and parsed.before == date(2017, 12, 31)


@pytest.mark.parametrize("sentence", ["mail from 2017", "emails from 2017",
                                      "mail in 2017", "email 2017", "messages from 2017"])
def test_mail_from_a_year_is_a_filter_only_query(sentence):
    """**The owner's report.** Every spelling of it becomes the same query:
    mail - msg, eml *and* pst, exactly what typed `type:mail` means - sent in
    2017, and no search words at all, so it lists that year's mail newest
    first instead of hunting for the word "mail"."""
    parsed = parse_query(_applied(sentence).query)
    assert set(parsed.ext) == {"msg", "eml", "pst"}
    assert _year_2017(parsed)
    assert parsed.terms == () and not parsed.has_text
    assert parsed.has_filters


def test_mail_from_a_known_person_in_a_year_applies_all_three():
    applied = _applied("mail from John in 2017")
    parsed = parse_query(applied.query)
    assert parsed.senders == ("john.smith@acme.com",)
    assert set(parsed.ext) == {"msg", "eml", "pst"}
    assert _year_2017(parsed)
    assert parsed.terms == ()
    assert [f.label for f in applied.filters] == [
        "mail", "from john.smith@acme.com", "in 2017"]


def test_a_person_the_index_does_not_know_stays_a_word():
    parsed = parse_query(_applied("mail from Mortimer in 2017").query)
    assert parsed.senders == ()
    assert "Mortimer" in parsed.terms
    assert _year_2017(parsed)


def test_invoice_2017_keeps_the_year_as_a_word():
    r"""**Decided conservatively, and this is the line.** A bare year beside a
    document word stays a search term: an invoice's only date is often the
    day it was copied (`mtime_ns`) while its name says 2017, so a 2017 filter
    would hide the very file asked for. It is still offered as a chip; one
    click applies it. "from 2017" / "in 2017" say it is a date, so they apply.
    """
    applied = _applied("invoice 2017")
    assert applied.query == "invoice 2017" and applied.filters == ()
    parsed = parse_query(_applied("invoice from 2017").query)
    assert _year_2017(parsed) and parsed.terms == ("invoice",)


def test_before_and_after_a_year_are_its_edges():
    assert parse_query(_applied("mail before 2015").query).before == date(2014, 12, 31)
    assert parse_query(_applied("mail after 2015").query).after == date(2016, 1, 1)


def test_two_years_is_a_range_the_rules_do_not_read():
    parsed = parse_query(_applied("mail from 2016 to 2017").query)
    assert parsed.after is None and parsed.before is None
    assert "2017" in parsed.terms


def test_what_the_rules_do_not_recognise_stays_as_typed():
    applied = _applied("volcano homework essay")
    assert applied.query == "volcano homework essay" and not applied.changed


def test_leftover_words_that_say_something_stay_as_search_terms():
    parsed = parse_query(_applied("emails about the boiler from 2017").query)
    assert "boiler" in parsed.terms
    assert _year_2017(parsed) and set(parsed.ext) == {"msg", "eml", "pst"}


def test_quotes_and_typed_operators_are_never_consumed():
    assert _applied('"mail from 2017"').query == '"mail from 2017"'
    typed = parse_query(_applied("emails type:pdf 2017").query)
    assert typed.ext == ("pdf",)                  # the typed type wins
    assert typed.after is None                     # and a bare year is no longer mail's


def test_a_typed_date_operator_wins_over_the_year_rule():
    """Order "dates" §1a. `date:` fills `after`/`before`, so the rule that
    reads "in 2017" steps aside exactly as it does for a typed `after:` -
    and the mail filter it did not contradict still applies."""
    for typed in ("date:2016", "date:2016-03..2016-06", "date:..2016"):
        applied = _applied(f"mail in 2017 {typed}")
        parsed = parse_query(applied.query)
        assert not any(f.kind == "date" for f in applied.filters), typed
        assert parsed.after != date(2017, 1, 1), typed
        assert typed in applied.query
        assert set(parsed.ext) == {"msg", "eml", "pst"}


def test_a_typed_date_that_does_not_parse_still_keeps_the_year_rule_away():
    """Order "dates" §1d. The person asked for a date filter and mistyped it;
    the box says what is wrong, and quietly applying "in 2017" instead would
    answer a question they did not ask."""
    applied = _applied("mail in 2017 date:2017-13")
    assert not any(f.kind == "date" for f in applied.filters)
    parsed = parse_query(applied.query)
    assert parsed.after is None and parsed.before is None
    assert parsed.date_problems


def test_a_declined_filter_puts_its_words_back():
    """Removing a chip restores normal behaviour for that part, and only that
    part: the mail filter stays, and "2017" is a search word again."""
    first = _applied("mail from 2017")
    date_chip = next(f for f in first.filters if f.kind == "date")
    again = parse_query(_applied("mail from 2017", declined=[date_chip.key]).query)
    assert again.after is None and again.before is None
    assert "2017" in again.terms
    assert set(again.ext) == {"msg", "eml", "pst"}


def test_mail_is_only_applied_when_the_corpus_holds_mail():
    class _NoMail(_OneJohn):
        def distinct_values(self, kind, limit=40, **_):
            return ["pdf", "docx"] if kind == "ext" else []

    assert "type:mail" not in _applied("mail from 2017", _NoMail()).query


def test_a_store_that_raises_still_applies_what_needs_no_store():
    class _Broken:
        def distinct_values(self, *_a, **_k):
            raise RuntimeError("the index is locked")

    parsed = parse_query(_applied("mail from 2017", _Broken()).query)
    assert set(parsed.ext) == {"msg", "eml", "pst"} and _year_2017(parsed)


def test_applying_asks_the_store_only_what_could_change_the_query():
    r"""**This runs before every search, keystrokes included.** Asking for
    every sender, recipient and extension up front measured 58 ms on 200,000
    files (see `_apply`); "mail from 2017" now needs one `holds_ext` seek, and
    a capitalised word is only looked up when it could become a filter."""
    asked: list = []

    class Counting(_OneJohn):
        def holds_ext(self, extensions):
            asked.append(("holds_ext", tuple(extensions)))
            return True

        def distinct_values(self, kind, limit=40, **kw):
            asked.append(("distinct", kind))
            return super().distinct_values(kind, limit=limit, **kw)

    _applied("mail from 2017", Counting())
    assert asked == [("holds_ext", ("msg", "eml", "pst"))]
    asked.clear()
    _applied("Budget Dave 2017", Counting())       # no mail, no preposition
    assert asked == []
    asked.clear()
    assert "from:john.smith@acme.com" in _applied("mail from John", Counting()).query
    assert ("distinct", "sender") in asked and ("distinct", "ext") not in asked


def test_holds_ext_is_a_real_answer(tmp_path):
    from app.storage.sqlite_store import SqliteStore

    s = SqliteStore(tmp_path / "h.db").connect()
    assert not s.holds_ext(("msg", "eml", "pst"))
    s.upsert_file("C:/a.pst#1", parent_dir="C:/", ext="pst", size_bytes=1, mtime_ns=1,
                  status="INDEXED", source_kind="pst_message")
    assert s.holds_ext(("msg", "eml", "pst")) and not s.holds_ext(("pdf",))
    assert not s.holds_ext(())
    s.close()


def test_every_mail_word_offers_the_same_mail_type_as_typing_it():
    """`emails` offered `type:eml` - the first of two extensions - which is
    not what `type:mail` means and missed every Outlook message."""
    for word in ("mail", "email", "emails", "messages"):
        assert _field(f"{word} about the boiler", "type", _OneJohn()) == "mail", word
