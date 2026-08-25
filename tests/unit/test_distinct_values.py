r"""The values the `/` menu offers, read from the index itself.

Layer: L1

`presenter.value_suggestions` decides *what* to offer; this is the query behind
it. It runs from a keystroke, so the two properties that matter are not about
correctness of the list - they are that the query is bounded and index-backed,
and that a person's own `%` or `_` means those characters rather than a wildcard.

`kind` is looked up in a table rather than interpolated into SQL. That is worth
a test of its own: the day somebody adds a source by pasting a column name, the
difference between a lookup and an f-string is the difference between an empty
menu and a query nobody planned for.
"""

from __future__ import annotations

import pytest

from app.storage.sqlite_store import SqliteStore


@pytest.fixture()
def store(tmp_path):
    with SqliteStore(tmp_path / "index.db") as opened:
        for n in range(5):
            opened.upsert_file(f"D:/Docs/report {n}.pdf", size_bytes=1,
                               mtime_ns=1, source_kind="file")
        for n in range(2):
            opened.upsert_file(f"D:/Docs/notes {n}.docx", size_bytes=1,
                               mtime_ns=1, source_kind="file")
        opened.upsert_file("D:/Odd/under_score.txt", size_bytes=1,
                           mtime_ns=1, source_kind="file")
        opened.upsert_file("D:/Odd/100%.txt", size_bytes=1, mtime_ns=1,
                           source_kind="file")

        for n in range(3):
            file_id = opened.upsert_file(
                f"pst://mail.pst/E{n}", size_bytes=1, mtime_ns=1,
                source_kind="pst_message")
            opened.set_message(file_id, sender="dave.smith@acme.com",
                               subject=f"one {n}")
        file_id = opened.upsert_file("pst://mail.pst/E9", size_bytes=1,
                                     mtime_ns=1, source_kind="pst_message")
        opened.set_message(file_id, sender="priya@acme.com", subject="two")

        opened.upsert_repo("D:/GIT/leasha", kind="git", name="leasha")
        opened.upsert_repo("D:/GIT/tools", kind="git", name="tools")
        yield opened


# --- what it returns --------------------------------------------------------

def test_extensions_come_back_commonest_first(store):
    """**Not alphabetically.** The type somebody wants is nearly always one of
    the three they have thousands of, and an alphabetical list buries it under
    the one `.adoc` in the corpus."""
    found = store.distinct_values("ext")

    assert found[0] == "pdf", f"expected the commonest first, got {found}"
    assert "docx" in found


def test_senders_come_back_commonest_first(store):
    found = store.distinct_values("sender")

    assert found[0] == "dave.smith@acme.com"
    assert "priya@acme.com" in found


def test_repositories_come_back_by_name(store):
    assert set(store.distinct_values("repo")) == {"leasha", "tools"}


def test_folders_come_back(store):
    found = store.distinct_values("folder")

    assert any("Docs" in value for value in found)


def test_the_prefix_narrows_it(store):
    assert store.distinct_values("ext", prefix="doc") == ["docx"]


def test_the_prefix_matches_anywhere_not_only_at_the_start(store):
    """The same rule `/from dave` follows: people type the middle of a thing.
    An anchored match makes the menu look empty for a value that is there."""
    assert "dave.smith@acme.com" in store.distinct_values("sender", prefix="smith")


# --- the properties that would be invisible if wrong ------------------------

def test_an_unknown_kind_is_an_empty_list_not_a_query(store):
    """`kind` is a lookup, never interpolated. A source nobody planned for
    returns nothing rather than reaching a column that was not indexed."""
    assert store.distinct_values("ext; DROP TABLE files") == []
    assert store.distinct_values("path") == []          # not a key: it is "folder"
    assert store.distinct_values("") == []


def test_a_wildcard_a_person_typed_is_a_literal_character(store):
    """`%` means percent. Treating it as "match anything" returns the whole
    index for a query that should return one file."""
    found = store.distinct_values("folder", prefix="%")

    assert found == [] or all("%" in value for value in found)


def test_an_underscore_a_person_typed_is_a_literal_character(store):
    """`_` matches any single character in LIKE. Unescaped, `under_score`
    quietly matches `underXscore` too."""
    every = store.distinct_values("folder")
    assert every, "the fixture wrote no folders"

    assert store.distinct_values("ext", prefix="p_f") == []


def test_the_limit_is_honoured(store):
    """No unbounded work behind a keystroke."""
    assert len(store.distinct_values("ext", limit=1)) == 1


def test_a_limit_of_zero_is_treated_as_one_rather_than_as_no_limit(store):
    """SQLite reads a negative LIMIT as "everything", so a caller that computes
    a limit and gets it wrong must not turn a bounded query into a full scan."""
    assert len(store.distinct_values("ext", limit=0)) == 1
    assert len(store.distinct_values("ext", limit=-5)) == 1


def test_an_empty_index_offers_nothing_rather_than_failing(tmp_path):
    with SqliteStore(tmp_path / "empty.db") as empty:
        assert empty.distinct_values("ext") == []
        assert empty.distinct_values("sender") == []
        assert empty.distinct_values("repo") == []
