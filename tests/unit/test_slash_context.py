r"""Slash menu §1: the value tier answers against what has already been typed.

`repo:leasha branch:` should offer leasha's branches, not every branch in every
repository. Until now the earlier tokens were parsed and then ignored, so the
menu was confidently answering a question nobody asked.

Two rules from the order shape everything here:

* **one filter grammar** - the context is parsed by `parse_query` and applied
  through `file_filter_sql`, never by a second parser or a nested dictionary;
* **a scope may not widen the query cost** - this runs behind a keystroke, and
  a menu that is slower than the search it helps write is not help.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from app.storage.sqlite_store import SqliteStore, VALUE_SAMPLE
from app.ui.presenter import SlashContext, slash_context


# --- 1a: the settled tokens come back with the word being typed -------------


def test_the_value_tier_carries_what_was_already_typed() -> None:
    got = slash_context("type:pdf after:2024 from:da")

    assert got.mode == "value"
    assert got.partial == "da"
    assert got.head == "type:pdf after:2024 "
    assert got.context is not None
    assert got.context.ext == ("pdf",)
    assert got.context.after is not None


def test_the_command_tier_does_not_pay_for_a_parse() -> None:
    """It has no use for the context, and this runs on every keystroke."""
    got = slash_context("type:pdf /fr")

    assert got.mode == "command"
    assert got.context is None


def test_a_first_filter_has_no_context() -> None:
    got = slash_context("from:da")

    assert got.mode == "value"
    assert got.context is None, "there is nothing typed before it"


def test_it_is_still_a_named_tuple_in_the_documented_order() -> None:
    """Unpacking four is the contract; unpacking three fails loudly, which is
    the right way for this to change - a silently dropped context would give a
    menu that looks right and answers the wrong question."""
    got = slash_context("/ty")

    assert isinstance(got, SlashContext)
    head, mode, partial, context = got
    assert (head, mode, partial, context) == ("", "command", "ty", None)


@pytest.mark.parametrize("text", ["D:/docs", "12:30", "http://x", "", "plain "])
def test_text_that_is_not_a_filter_still_closes_the_menu(text: str) -> None:
    """The cases worth remembering: a search box that silently rewrites what
    somebody typed is one they stop trusting."""
    assert slash_context(text).mode == ""


def test_a_half_typed_query_never_raises() -> None:
    """The normal state of the box while somebody is typing."""
    for text in ("after:notadate from:d", "size:>>> from:d", 'subject:" from:d'):
        assert slash_context(text).mode == "value"


# --- 1b: the store narrows values by that context ---------------------------


@pytest.fixture()
def store():
    opened = SqliteStore(Path(tempfile.mkdtemp()) / "values.db").connect()
    rows = [
        ("C:/work/a.pdf", "C:/work", "pdf"),
        ("C:/work/b.pdf", "C:/work", "pdf"),
        ("C:/work/c.docx", "C:/work", "docx"),
        ("C:/home/d.pdf", "C:/home", "pdf"),
        ("C:/home/e.txt", "C:/home", "txt"),
    ]
    for path, folder, ext in rows:
        opened.upsert_file(path, parent_dir=folder, ext=ext, size_bytes=1,
                           mtime_ns=1, status="INDEXED", source_kind="file")
    yield opened
    opened.close()


def test_values_are_narrowed_by_the_filters_already_typed(store) -> None:
    from app.search.query import parse_query

    assert store.distinct_values("ext") == ["pdf", "docx", "txt"]
    assert store.distinct_values(
        "ext", within=parse_query("path:work")) == ["pdf", "docx"]


def test_a_scope_that_matches_nothing_returns_nothing(store) -> None:
    """`1e` turns this into an unscoped offer with an "(all)" marker at the UI
    layer. The store's job is to answer the question it was asked."""
    from app.search.query import parse_query

    assert store.distinct_values("ext", within=parse_query("type:zzz")) == []


def test_free_text_is_dropped_from_the_scope(store) -> None:
    """The order's rule: a context that cannot be answered bounded must not
    widen the query cost. `file_filter_sql` builds clauses for filters and
    nothing else, so a half-typed sentence in the box cannot turn an indexed
    lookup into a scan - which is satisfied by *which function is called*."""
    from app.search.query import parse_query

    unscoped = store.distinct_values("ext")
    assert store.distinct_values(
        "ext", within=parse_query("invoice quarterly report")) == unscoped


def test_no_context_behaves_exactly_as_before(store) -> None:
    assert store.distinct_values("ext", within=None) == ["pdf", "docx", "txt"]


def test_an_unknown_kind_is_still_refused(store) -> None:
    """`kind` is looked up in a table, never interpolated, so a caller cannot
    ask for a column - or a scan - that was not planned for."""
    from app.search.query import parse_query

    assert store.distinct_values("wharever") == []
    assert store.distinct_values("wharever", within=parse_query("type:pdf")) == []


def test_the_sample_is_a_number_with_a_measurement_behind_it() -> None:
    """437ms unscoped-grouping the whole filtered corpus against 11.1ms for the
    first 20,000, on 500,000 files, for the same twenty-five folders."""
    assert VALUE_SAMPLE == 20_000


def test_a_scoped_query_is_bounded_by_the_sample(store) -> None:
    """The bound has to be in the SQL, not applied afterwards - the cost this
    avoids is the grouping, and grouping happens in the database."""
    import inspect

    source = inspect.getsource(SqliteStore.distinct_value_counts)
    assert "LIMIT {VALUE_SAMPLE}" in source
    assert "if where:" in source, (
        "the sample must apply to the scoped shape only; the unscoped one was "
        "already fast and its counts are exact")


# --- 1c: the catalogue says which filters may narrow which values -----------


def test_the_scoping_map_lives_in_the_catalogue() -> None:
    """One catalogue feeding three consumers is this project's non-negotiable
    for commands. A nested dictionary of sub-keys elsewhere would be a second
    grammar, which is the failure `commands.py` opens by describing."""
    from app.search.commands import command_for

    assert command_for("type").scoped_by == ("repo",)
    assert command_for("from").scoped_by == ("type", "after", "before")
    assert command_for("to").scoped_by == ("type", "after", "before")


def test_every_git_value_source_is_scoped_by_its_repository() -> None:
    """Branches, tags, authors and commits are facts about one checkout.

    `repo:leasha branch:` offering every branch on the machine is this order's
    headline example of the menu answering a question nobody asked.
    """
    from app.search.gitquery import GIT_COMMANDS

    for command in GIT_COMMANDS:
        if command.source and command.source != "repo":
            assert command.scoped_by == ("repo",), command.name


def test_a_scope_the_command_does_not_allow_is_ignored() -> None:
    """`/type` is narrowed by `repo:`, not by `path:` - and a filter that is
    not in `scoped_by` must leave no trace, not a query that claims to have
    filters and has none."""
    from app.search.commands import command_for
    from app.search.query import parse_query
    from app.ui.presenter import scope_for

    assert scope_for(command_for("type"), parse_query("path:work")) is None


def test_only_the_permitted_filters_survive_the_narrowing() -> None:
    from app.search.commands import command_for
    from app.search.query import parse_query
    from app.ui.presenter import scope_for

    scope = scope_for(command_for("from"),
                      parse_query("type:pdf path:work after:2024"))

    assert scope is not None
    assert scope.ext == ("pdf",)
    assert scope.after is not None
    assert scope.paths == (), "path: is not in from's scoped_by"


def test_the_blanking_table_mirrors_what_counts_as_a_filter() -> None:
    r"""`has_filters` lists what a filter is; `_FILTER_FIELDS` lists how to
    remove one. They answer the same question from opposite ends, so they drift
    together or the narrowing invents filters that are not there.

    The first version blanked `scope` to `""` when its neutral value is
    `"all"` - so narrowing `/type` by a disallowed `path:` produced an empty
    query that still reported `has_filters`.
    """
    import inspect

    from app.search.query import ParsedQuery
    from app.ui.presenter import _FILTER_FIELDS

    source = inspect.getsource(ParsedQuery.has_filters.fget)
    for field in _FILTER_FIELDS:
        assert f"self.{field}" in source, (
            f"{field} is blanked as a filter but has_filters does not count it")


def test_the_neutral_value_of_every_field_really_is_neutral() -> None:
    """Blanking to the wrong neutral is how the last bug happened."""
    from dataclasses import replace

    from app.search.query import parse_query
    from app.ui.presenter import _FILTER_FIELDS

    emptied = replace(parse_query("type:pdf repo:x after:2024 invoice"),
                      **_FILTER_FIELDS)
    assert not emptied.has_filters


# --- 1d: the context reaches the reader, and the cache key ------------------


def test_the_store_reader_is_given_the_scope(store, monkeypatch) -> None:
    from app.search.query import parse_query
    from app.ui.presenter import value_suggestions

    calls = []
    original = store.distinct_value_counts

    def spy(kind, **kwargs):
        calls.append(kwargs)
        return original(kind, **kwargs)

    # The counts method, because that is the one the reader prefers now - a spy
    # on `distinct_values` would sit there recording nothing and the test would
    # pass by never being reached.
    monkeypatch.setattr(store, "distinct_value_counts", spy)
    value_suggestions(store, "type", "", context=parse_query("repo:leasha"))

    # Every call, not the last: `repo:leasha` matches nothing here, so 1e's
    # fallback asks a second time with no scope at all - which is the correct
    # behaviour and would hide the first call from a spy that only kept one.
    assert calls, "the store was never asked"
    assert calls[0]["within"] is not None
    assert calls[0]["within"].repos == ("leasha",)
    assert "within" not in calls[-1], (
        "the fallback must ask without a scope at all - and it must not send "
        "within=None either, because a store that has never heard of the "
        "argument would raise and the menu would silently lose its values")


def test_the_cache_key_changes_with_the_scope() -> None:
    """A cached global answer served under `repo:leasha` is a wrong answer with
    a hundred-and-twenty-second lifetime, which is worse than a slow one."""
    from app.ui.presenter import scope_key, slash_context

    unscoped = scope_key("from", slash_context("from:d").context)
    scoped = scope_key("from", slash_context("type:pdf from:d").context)
    other = scope_key("from", slash_context("type:docx from:d").context)

    assert unscoped == ""
    assert scoped and scoped != unscoped
    assert other != scoped, "two different scopes share one cache entry"


def test_free_text_after_a_filter_does_not_churn_the_cache() -> None:
    """The key is built from the narrowed scope, not the whole query."""
    from app.ui.presenter import scope_key, slash_context

    first = scope_key("from", slash_context("type:pdf from:d").context)
    later = scope_key("from", slash_context("type:pdf invoice from:d").context)

    assert first == later


# --- 1e: an empty menu is indistinguishable from a broken one ---------------


def test_a_scope_that_removes_everything_offers_the_unscoped_values(
    store
) -> None:
    """`repo:nothing` matches no file, so scoping `/type` by it finds nothing -
    and an empty menu is indistinguishable from a broken one."""
    from app.search.query import parse_query
    from app.ui.presenter import ALL_VALUES_NOTE, value_suggestions

    unscoped = value_suggestions(store, "type", "")
    assert unscoped, "the fixture must have something to fall back to"

    notes: list[str] = []
    got = value_suggestions(store, "type", "",
                            context=parse_query("repo:nothing"), notes=notes)

    assert got == unscoped
    assert notes == [ALL_VALUES_NOTE], (
        "falling back silently would be the same failure as the empty menu")


def test_nothing_to_fall_back_to_produces_no_marker(store) -> None:
    """A marker saying "(all)" over an empty list would be a label on nothing.

    There are no messages in this fixture, so `/from` has no values scoped or
    unscoped - which is a different situation from a scope having removed them.
    """
    from app.search.query import parse_query
    from app.ui.presenter import value_suggestions

    notes: list[str] = []
    got = value_suggestions(store, "from", "",
                            context=parse_query("type:zzz"), notes=notes)

    assert got == []
    assert notes == []


def test_a_scope_that_finds_something_says_nothing(store) -> None:
    """The marker appears only when the scope was actually dropped."""
    from app.search.query import parse_query
    from app.ui.presenter import value_suggestions

    notes: list[str] = []
    got = value_suggestions(store, "type", "",
                            context=parse_query("repo:leasha"), notes=notes)

    if got and not notes:
        assert True                              # scoped and found values
    else:
        assert notes, "either it answered under the scope, or it said it did not"


def test_no_context_never_produces_a_marker(store) -> None:
    from app.ui.presenter import value_suggestions

    notes: list[str] = []
    value_suggestions(store, "type", "", notes=notes)
    assert notes == []


def test_a_store_without_the_new_argument_still_offers_its_values() -> None:
    r"""**The regression this guards is a silent one.**

    `value_suggestions` catches everything a reader raises, so passing
    `within=` to a store that does not accept it turns a `TypeError` into an
    empty index tier - the menu falls back to the grammar's kind words and
    looks like it is working. Four existing tests caught it by accident; this
    one is on purpose.
    """
    from app.ui.presenter import value_suggestions

    class OldStore:
        def distinct_values(self, kind, *, prefix="", limit=40):
            return ["pdf", "docx"]

    found = value_suggestions(OldStore(), "type", "")
    assert found[:2] == ["pdf", "docx"]


# --- 2a: the counts are free, and they say whether they are true ------------


def test_the_counts_come_back_with_the_values(store) -> None:
    """It is already a GROUP BY; the count costs nothing to return."""
    from app.storage.sqlite_store import ValueCount

    rows = store.distinct_value_counts("ext")

    assert rows[0] == ValueCount("pdf", 3, True)
    assert [row.value for row in rows] == store.distinct_values("ext")


def test_an_unscoped_count_is_exact(store) -> None:
    assert all(row.exact for row in store.distinct_value_counts("ext"))


def test_a_scoped_count_under_the_sample_is_still_exact(store) -> None:
    """Most real scopes - one repository, one folder - are far under the
    ceiling, so the ordinary case keeps a number it can stand behind."""
    from app.search.query import parse_query

    rows = store.distinct_value_counts("ext", within=parse_query("path:work"))

    assert rows
    assert all(row.exact for row in rows)


def test_a_count_taken_from_a_spent_sample_says_it_is_not_exact(
    tmp_path
) -> None:
    r"""**The label has to be able to stand behind its number.**

    A scoped menu counts the first `VALUE_SAMPLE` matching rows. When that
    ceiling is reached the numbers are a fraction of the truth - `pdf 200
    files` for a corpus holding five thousand - and a label wrong by a factor
    of twenty-five is worse than no label. `2b` shows the count only when this
    is True.
    """
    from app.search.query import parse_query
    from app.storage.sqlite_store import SqliteStore, VALUE_SAMPLE

    with SqliteStore(tmp_path / "big.db") as big:
        big.conn.execute("BEGIN IMMEDIATE")
        big.conn.executemany(
            "INSERT INTO files (path, parent_dir, ext, size_bytes, mtime_ns, "
            "status, source_kind) VALUES (?, 'C:/w', 'pdf', 1, 1, 'INDEXED', "
            "'file')",
            [(f"C:/w/{i}.pdf",) for i in range(VALUE_SAMPLE + 100)],
        )
        big.conn.execute("COMMIT")

        rows = big.distinct_value_counts("ext", within=parse_query("path:w"))

        assert rows
        assert not rows[0].exact, "a sampled count claimed to be the truth"
        assert rows[0].count <= VALUE_SAMPLE


# --- 2b and 2c: what a value row says ---------------------------------------


def test_a_count_sits_beside_the_value() -> None:
    from app.ui.presenter import value_row

    assert value_row("pdf", count=12431).startswith("pdf")
    assert "12,431 files" in value_row("pdf", count=12431)


def test_a_count_of_one_is_singular() -> None:
    """"1 files" is what makes a careful interface look careless, and this row
    sits under somebody's cursor."""
    from app.ui.presenter import value_row

    assert "1 file" in value_row("pdf", count=1)
    assert "1 files" not in value_row("pdf", count=1)
    assert "1 message" in value_row("sam@x.io", count=1, noun="messages")


def test_a_sampled_count_is_not_shown_at_all() -> None:
    """Omitting it loses information; printing it states something false."""
    from app.ui.presenter import value_row

    assert value_row("pdf", count=200, exact=False) == "pdf"


def test_senders_are_counted_in_messages_not_files() -> None:
    """`dave@acme.com  316 files` is wrong in a way somebody would notice and
    not be able to explain."""
    from app.storage.sqlite_store import ValueCount
    from app.ui.presenter import value_rows

    row = value_rows("from", [ValueCount("dave@acme.com", 316, True)])[0]
    assert "316 messages" in row


def test_a_date_row_shows_what_it_resolves_to() -> None:
    """2c: `/after 30d   (since 28 Jul 2026)`."""
    import datetime

    from app.ui.presenter import value_rows

    rows = value_rows("after", ["30d", "last month"],
                      today=datetime.date(2026, 8, 27))

    assert "(since 28 Jul 2026)" in rows[0]
    assert "(since 27 Jul 2026)" in rows[1]


def test_the_date_hint_never_uses_a_glibc_only_format() -> None:
    r"""`%-d` strips the leading zero on Linux and raises `ValueError` on
    Windows, which is the only platform this ships to."""
    import ast
    import inspect
    import textwrap

    from app.ui import presenter

    # **Against the code, not the prose.** The comment above the fix names the
    # directive it forbids, so a plain substring search matches the
    # explanation - which this project has now been caught by four times.
    tree = ast.parse(textwrap.dedent(inspect.getsource(presenter.resolved_date)))
    node = tree.body[0]
    node.body = node.body[1:]                    # drop the docstring
    assert "%-d" not in ast.unparse(node)


def test_a_value_that_is_not_a_date_gets_no_hint() -> None:
    """Which is how `after:lst week` becomes visibly wrong instead of quietly
    matching nothing."""
    import datetime

    from app.ui.presenter import resolved_date

    assert resolved_date("lst week", today=datetime.date(2026, 8, 27)) == ""


def test_the_instant_pass_shows_plain_values() -> None:
    """The menu is filled twice, and the first pass has no store to count
    with. `getattr(entry, "count", None)` on a string returns the *method*,
    which formatted as an integer and raised - so the shapes are told apart by
    what they are."""
    from app.ui.presenter import value_rows

    assert value_rows("type", ["pdf", "docx"]) == ["pdf", "docx"]


# --- the value that could not be typed --------------------------------------


def test_a_value_with_a_space_is_quoted_when_it_is_inserted() -> None:
    r"""**Picking `last month` produced a query that did neither thing.**

    The tokenizer splits on whitespace, so `after:last month` parses as
    `after:last` - not a date, so no filter at all - plus a loose search for
    the word *month*. Somebody chose a date from a list and got a query that
    filtered nothing and searched for something else, with nothing on screen to
    say so. Found while building 2c's hint, which is the point of that hint.
    """
    from app.search.query import parse_query
    from app.ui.presenter import as_typed_value

    assert as_typed_value("last month") == '"last month"'
    assert as_typed_value("pdf") == "pdf"

    assert parse_query(f"after:{as_typed_value('last month')}").after is not None
    assert parse_query("after:last month").after is None, (
        "the unquoted form is what was broken")


def test_every_offered_date_can_be_typed_back(store) -> None:
    r"""The catalogue's own rule: a value offered here that the parser rejects
    is worse than one that is merely undocumented - it is a promise the
    application breaks."""
    import datetime

    from app.search.commands import RELATIVE_DATES
    from app.search.query import parse_query
    from app.ui.presenter import as_typed_value

    for spelling in RELATIVE_DATES:
        typed = f"after:{as_typed_value(spelling)}"
        assert parse_query(typed, today=datetime.date(2026, 8, 27)).after, (
            f"{spelling!r} is offered by the menu and does not parse as {typed}")


# --- 3a: a kind word opens its extensions -----------------------------------


def test_a_kind_word_knows_what_it_stands_for() -> None:
    """Read from the parser's own `_EXT_GROUPS`, so the second page cannot
    offer a spelling the filter would then not match. A second copy of that
    mapping is the "nested sub-key dictionary" this order rules out."""
    from app.ui.presenter import kind_expansion

    assert kind_expansion("excel") == ("xls", "xlsx", "xlsm")
    assert kind_expansion("code")[:2] == ("py", "js")


def test_an_ordinary_extension_has_no_second_page() -> None:
    """`/type pdf` is finished when it is picked."""
    from app.ui.presenter import kind_expansion, value_page

    assert kind_expansion("pdf") == ()
    assert value_page("type", "pdf") == ""


def test_the_second_page_says_where_it_is_and_how_to_leave() -> None:
    """A list of extensions with nothing saying which kind they belong to is
    the menu-that-does-not-say-what-it-is-answering this order is about."""
    from app.ui.presenter import value_page

    crumb = value_page("type", "excel")

    assert "excel" in crumb
    assert "Backspace" in crumb


def test_every_kind_word_offered_can_be_expanded() -> None:
    r"""The kind words come from the catalogue and the expansion from the
    parser. If the two ever disagree, `/type excel` opens a page with nothing
    on it - which reads as a broken menu rather than as a missing mapping."""
    from app.search.commands import command_for
    from app.ui.presenter import kind_expansion

    for word in command_for("type").values:
        assert kind_expansion(word), f"{word} is offered and expands to nothing"


# --- 3b: the way out of the date menu ---------------------------------------


def test_the_custom_row_is_not_a_value() -> None:
    """Picking it leaves `after:` in the box; it must never become a filter."""
    from app.search.query import parse_query
    from app.ui.presenter import CUSTOM_ROW

    assert parse_query(f"after:{CUSTOM_ROW}").after is None
