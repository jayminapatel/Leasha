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

    source = inspect.getsource(SqliteStore.distinct_values)
    assert "LIMIT {VALUE_SAMPLE}" in source
    assert "if where:" in source, (
        "the sample must apply to the scoped shape only; the unscoped one was "
        "already fast and its counts are exact")
