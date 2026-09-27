r"""`/between` and plain-English date ranges. Order 0x §6a and §6b.

Layer: L4, with the two L5 helpers that read the same words (the chips under
the Search box and the offers on its notice bar).

**Decision D2 (owner, 2026-09-27):** the owner had asked for `/between X and
Y`; order 0w had already built `date:A..B`. To have one syntax rather than
two, `/between` is `/date` under another name, which also accepts its two
ends joined by "and" or "to". So every test here comes back to one question:
*does it reach exactly the `after`/`before` that `date:A..B` reaches?*

§6b is the typed sentence: "between March and June 2024" becomes an offer of
two filters, read by `translate_rules` - and a phrase that would need a guess
is left as words, because a wrong date filter hides documents silently.

No window here; the per-box scenarios are `test_date_forms_every_box.py`.
"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from app.search.commands import COMMANDS, command_for, expand_slashes, help_lines
from app.search.query import join_between_words, parse_query

TODAY = date(2026, 9, 27)


def _parsed(typed: str):
    """What every box does with a line: expand the slashes, then parse."""
    return parse_query(expand_slashes(typed), today=TODAY)


def _edges(typed: str) -> tuple:
    found = _parsed(typed)
    return found.after, found.before


# ---------------------------------------------------------------------------
# 6a - `/between` reaches exactly what `/date A..B` reaches
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(("typed", "same_as"), [
    ("/between 2024-03-01 and 2024-06-30", "/date 2024-03-01..2024-06-30"),
    ("/between 2024-03-01 to 2024-06-30", "/date 2024-03-01..2024-06-30"),
    ("/between 2024-03 and 2024-06", "/date 2024-03..2024-06"),
    ("/between 2019 to 2021", "/date 2019..2021"),
    ("/between 2019..2021", "/date 2019..2021"),               # the date: form too
    ("between:2024-03-01 AND 2024-06-30", "date:2024-03-01..2024-06-30"),
    ("between:2024-03-01 and 2024-06-30", "date:2024-03-01..2024-06-30"),
    ("/between 2017", "/date 2017"),                            # one value: one period
    ("/between 2024-06 and 2024-03", "/date 2024-06..2024-03"),  # swapped, the same way
    ('/between "2024-03-01 10:00" and "2024-03-02 09:00"',
     'date:"2024-03-01 10:00..2024-03-02 09:00"'),              # times of day survive
])
def test_between_reaches_the_same_dates_as_date(typed, same_as):
    assert _edges(typed) == _edges(same_as)
    assert _edges(typed) != (None, None)


def test_the_joining_words_leave_nothing_behind_as_search_words():
    """The operator pattern takes one value. Without the join, `and` and the
    second date would have been searched for as words."""
    found = _parsed("report /between 2024-03-01 and 2024-06-30 draft")
    assert found.terms == ("report", "draft")
    assert (found.after, found.before) == (date(2024, 3, 1), date(2024, 6, 30))
    assert not found.unknown_operators and not found.date_problems


def test_a_filter_after_the_range_is_still_a_filter():
    """`to:` is the recipient filter. `between:2024 to:priya` is a year and a
    person - the word "to" only joins when a value follows it."""
    found = _parsed("between:2024 to:priya")
    assert (found.after, found.before) == (date(2024, 1, 1), date(2024, 12, 31))
    assert found.recipients == ("priya",)


def test_a_value_already_a_range_is_not_joined_again():
    assert join_between_words("between:2017..2018 and more") == "between:2017..2018 and more"
    found = _parsed("/between 2017..2018 and more")
    assert (found.after, found.before) == (date(2017, 1, 1), date(2018, 12, 31))
    assert found.terms == ("and", "more")


def test_date_itself_does_not_take_joining_words():
    """D2 gives "X and Y" to `/between`. `date:2017 and 2018` keeps meaning
    what it always has - 2017, then two search words - so nobody's saved
    query changes under them."""
    found = _parsed("date:2017 and 2018")
    assert (found.after, found.before) == (date(2017, 1, 1), date(2017, 12, 31))
    assert found.terms == ("and", "2018")


def test_from_still_means_the_sender():
    """D2: `/from` keeps its meaning."""
    found = _parsed("/from dave /between 2024-01 and 2024-02")
    assert found.senders == ("dave",)
    assert found.after == date(2024, 1, 1)


def test_the_mail_rule_is_the_same_for_both_spellings():
    """`date:` on the Mail tab filters the sent date (0w). Both spellings must
    arrive at the Mail tab's query in the same shape."""
    from app.ui.presenter import mail_filters

    typed = mail_filters(_parsed("/between 2017-03 and 2017-06"))
    named = mail_filters(_parsed("/date 2017-03..2017-06"))
    assert typed == named and "after" in typed and "before" in typed


# -- a range that cannot be read says so (non-negotiable #2) -------------------

@pytest.mark.parametrize(("typed", "said"), [
    ("/between 2024-03-01 and 2024-13-01",
     "between:2024-03-01..2024-13-01 isn't a date — there is no month 13. "
     "Try between:2024-12-01 or between:2024"),
    ("/between 2024-02-30 and 2024-03-01",
     "between:2024-02-30..2024-03-01 isn't a date — February 2024 has 29 days. "
     "Try between:2024-02-29 or between:2024-02"),
    ("/between banana and 2024",
     "between:banana..2024 isn't a date Leasha can read — try "
     "between:2017-01 and 2017-06, or between:2017-03-01 to 2017-03-14"),
    ("/between banana",
     "between:banana isn't a date Leasha can read — try "
     "between:2017-01 and 2017-06, or between:2017-03-01 to 2017-03-14"),
])
def test_a_mistyped_range_says_what_was_wrong(typed, said):
    found = _parsed(typed)
    assert found.date_problems == (said,)
    assert found.after is None and found.before is None
    # Not quietly searched for as words, either.
    assert found.terms == ()


def test_the_date_sentences_are_unchanged_word_for_word():
    """0w's sentences for `date:` are released text; `between:` got its own
    rather than a change to theirs."""
    assert _parsed("date:2017-13").date_problems == (
        "date:2017-13 isn't a date — there is no month 13. "
        "Try date:2017-12 or date:2017-01..2017-06",)
    assert _parsed("date:banana").date_problems == (
        "date:banana isn't a date Leasha can read — try date:2017, "
        "date:2017-03, date:2017-03-14 or a range, date:2017-01..2017-06",)


# -- offered, with a summary and an example, wherever /date is ----------------

def test_between_is_in_the_catalogue_with_its_own_row():
    command = command_for("between")
    assert command is not None and command.name == "between"
    assert command.alias_of == "date" and command.is_date
    assert command.summary and command.example.startswith("/between ")
    # The example is copy-pasteable and does what it says.
    assert _edges(command.example) == (date(2024, 3, 1), date(2024, 6, 30))
    names = [c.name for c in COMMANDS]
    assert names.index("between") == names.index("date") + 1


def test_the_cli_help_lists_it():
    text = "\n".join(help_lines())
    assert "/between 2024-03-01 and 2024-06-30" in text


def test_every_box_that_offers_date_offers_between():
    r"""The catalogues each box's `/` menu is built from. Search uses the
    merged list plus actions, Code the merged list, Files and Mail the index
    names, the mini-search `COMMANDS` itself."""
    from app.ui.presenter import CODE_COMMANDS, FILES_COMMANDS, MAIL_COMMANDS
    from app.ui.widgets.code_commands import ALL_CATALOGUE, SEARCH_CATALOGUE

    for names in (FILES_COMMANDS, MAIL_COMMANDS, CODE_COMMANDS):
        assert "date" in names and "between" in names
    for catalogue in (ALL_CATALOGUE, SEARCH_CATALOGUE, COMMANDS):
        offered = [c.name for c in catalogue]
        assert "date" in offered and "between" in offered


def test_the_git_range_switch_kept_its_row_and_lost_only_the_spelling():
    r"""`between` used to be a spelling of git's `/range`. Left there, the
    Code menu's merge would have dropped `/range` entirely (it skips a git
    switch whose spelling the index claims) and the Code box would have sent
    `/between 2024-03 and 2024-06` to git as a commit range."""
    from app.search.gitquery import git_command_for, wants_git
    from app.ui.presenter import code_route
    from app.ui.widgets.code_commands import ALL_CATALOGUE

    assert git_command_for("between") is None
    assert git_command_for("range").name == "range"
    assert "range" in [c.name for c in ALL_CATALOGUE]
    assert wants_git("/between 2024-03 and 2024-06") == ()

    route = code_route("/between 2017-03 and 2017-06")
    assert route.engine == "index"
    assert (route.parsed.after, route.parsed.before) == (date(2017, 3, 1), date(2017, 6, 30))
    assert code_route("/range v5.0..v6.0").engine == "git"


# -- the chip under the box, and the offers beside it -------------------------

def test_one_chip_covers_the_whole_range():
    """Removing the chip must take all four words with it - leaving "and
    2024-06" behind would search for the word "and"."""
    from app.ui.chips_logic import chips_for, without

    text = "report /between 2024-03 and 2024-06 draft"
    (chip,) = chips_for(text)
    assert chip.op == "between" and chip.label == "between: 2024-03 and 2024-06"
    assert without(text, chip) == "report draft"

    colon = "between:2024 to:priya x"
    assert [c.label for c in chips_for(colon)] == ["between: 2024", "to: priya"]


def test_a_typed_between_answers_the_date_offers():
    """A range typed by hand has said what an offered "after"/"before" would."""
    from app.search.translate_rules import Chip
    from app.ui.presenter import filter_offers

    chips = [Chip("after", "2016-01-01", "2016"), Chip("before", "2016-12-31", "2016")]
    for typed in ("report 2016 /between 2017 and 2018", "report 2016 between:2017..2018"):
        assert filter_offers(chips, typed) == []
    assert len(filter_offers(chips, "report 2016")) == 2


def test_between_values_name_their_period():
    from app.ui.presenter import value_rows

    rows = value_rows("between", ["2017-03"], today=TODAY)
    assert rows and "(1 Mar 2017 to 31 Mar 2017)" in rows[0]


# ---------------------------------------------------------------------------
# 6b - plain English in the typed sentence
# ---------------------------------------------------------------------------

def _range(sentence: str, today: date = TODAY) -> set:
    from app.search.translate_rules import read

    return {chip.as_filter() for chip in read(sentence, today=today).chips
            if chip.field in ("after", "before")}


@pytest.mark.parametrize(("sentence", "after", "before"), [
    ("letters between March and June 2024", "2024-03-01", "2024-06-30"),
    ("notes from March to June 2024", "2024-03-01", "2024-06-30"),
    ("reports between 2019 and 2021", "2019-01-01", "2021-12-31"),
    ("the survey from 2016 to 2017", "2016-01-01", "2017-12-31"),
    ("from 1 January 2024 to 5 February 2024", "2024-01-01", "2024-02-05"),
    ("from 1 Jan 2024 to 5 Feb 2024", "2024-01-01", "2024-02-05"),
    ("from 1st Jan to 5th Feb 2024", "2024-01-01", "2024-02-05"),
    ("between 3 Sept 2023 and 2 Mar 2024", "2023-09-03", "2024-03-02"),
    ("from March 2024 to June", "2024-03-01", "2024-06-30"),
    ("invoices between February and February 2024", "2024-02-01", "2024-02-29"),
])
def test_a_range_in_the_sentence_becomes_after_and_before(sentence, after, before):
    assert _range(sentence) == {f"after:{after}", f"before:{before}"}


def test_the_same_range_typed_and_said_reach_the_same_dates():
    """What the offer applies and what `/between` applies are one filter."""
    from app.search.translate_rules import read

    chips = read("letters between March and June 2024", today=TODAY).chips
    offered = parse_query(" ".join(c.as_filter() for c in chips
                                   if c.field in ("after", "before")))
    typed = _parsed("/between 2024-03 and 2024-06")
    assert (offered.after, offered.before) == (typed.after, typed.before)


def test_a_range_with_no_year_is_this_years_while_that_is_in_the_past():
    """The lone-month rule has always read "June" as this June; a range with
    no year follows it - while this year's range has begun."""
    assert _range("from 1 Jan to 5 Feb") == {"after:2026-01-01", "before:2026-02-05"}
    assert _range("between September and October") == {
        "after:2026-09-01", "before:2026-10-31"}


@pytest.mark.parametrize("sentence", [
    # No year, and this year's has not happened yet: last year's, or plans?
    "from 1 Oct to 5 Nov",
    # No year, and it crosses New Year: two years, neither said.
    "from 1 Dec to 5 Feb",
    # Backwards once the one year is filled in - probably Nov 2023 to Feb
    # 2024, but "probably" is a guess.
    "between November and February 2024",
    # Backwards with both years typed.
    "between 2021 and 2019",
    # Not a day.
    "from 31 February 2024 to 3 March 2024",
])
def test_a_range_that_would_need_a_guess_is_not_read_at_all(sentence):
    """**Not even half of it.** "between November and February 2024" read by
    the lone-month rule would have become November alone - a filter nobody
    asked for, hiding everything else."""
    assert _range(sentence) == set()


@pytest.mark.parametrize("sentence", [
    "about six months ago", "last summer", "a while back", "recently",
    "March and June 2024",             # two months mentioned, not a span
    "2016 to 2017",                    # no opening word: a score, a version
])
def test_vague_or_unmarked_phrases_are_still_not_ranges(sentence):
    from app.search.translate_rules import read

    chips = read(f"the report {sentence}", today=TODAY).chips
    ranged = {c.as_filter() for c in chips if c.field in ("after", "before")}
    # The existing single-date rules may still read a lone year or month in
    # these - that is 0w's behaviour, unchanged. What must not appear is a
    # range spanning two of the values.
    assert not ({"after:2024-03-01", "before:2024-06-30"} <= ranged)
    assert not ({"after:2016-01-01", "before:2017-12-31"} <= ranged)
    if sentence in ("about six months ago", "last summer", "a while back", "recently"):
        assert ranged == set()


def test_a_bare_year_beside_a_document_word_is_still_a_word_when_applied():
    """0w's line: "invoice 2017" keeps 2017 as a search word. A range is
    offered, never applied - `apply()` is untouched by §6b."""
    from app.search.translate_rules import apply

    assert apply("invoice 2017", today=TODAY).query == "invoice 2017"
    assert apply("invoices between 2019 and 2021", today=TODAY).filters == ()


def test_a_short_month_name_alone_is_not_a_month():
    """"mar" and "dec" are bits of words and names. Only the range phrase
    reads short names. ("may" is left out of this test on purpose: it is also
    a full month name, and the lone-month rule has read it as May since
    before §6b - a separate question, not changed here.)"""
    assert _range("the mar report") == set()
    assert _range("dec notes") == set()


def test_chat_keeps_its_own_rule_about_years():
    r"""Chat drops a date filter when the question names no year ("which
    March?" - `app/chat/plan.py`). A yearless range is dropped there the same
    way; one with a year is kept."""
    from app.chat.plan import make_plan

    assert not [f for f in make_plan("notes from 1 Jan to 5 Feb", today=TODAY).filters
                if f.startswith(("after:", "before:"))]
    kept = make_plan("notes between March and June 2024", today=TODAY).filters
    assert "after:2024-03-01" in kept and "before:2024-06-30" in kept


def test_a_time_range_is_a_datetime_on_both_sides():
    after, before = _edges('/between "2024-03-01 10:00" and "2024-03-02 09:00"')
    assert isinstance(after, datetime) and isinstance(before, datetime)
    assert (after.hour, before.hour, before.minute) == (10, 9, 0)
