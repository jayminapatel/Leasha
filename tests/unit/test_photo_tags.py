r"""shows: Florence-2's tag vocabulary, browsable and filterable.

Layer: L1 and L4

Work order 0i (202626270511) section 1c. test_ocr.py and
test_florence_tagger.py prove the tags get produced and written as a
labelled segment (1a/1b); this file proves the other half - that they land
in file_tags (schema v19) and that shows:dog actually narrows a search to
files carrying that tag, end to end against a real SQLite database.
"""

from __future__ import annotations

import pytest

from app.search.commands import COMMANDS, command_for, expand_slashes
from app.search.query import parse_query
from app.storage.filters import file_filter_sql
from app.storage.migrations import CURRENT_VERSION
from app.storage.sqlite_store import SqliteStore


@pytest.fixture
def store(tmp_path):
    with SqliteStore(tmp_path / "index.db") as opened:
        yield opened


def test_iso2(store):
    names = set(row[0] for row in store.conn.execute("SELECT 1"))
    assert names


def test_schema_v19_adds_file_tags(store):
    assert CURRENT_VERSION >= 19
    names = set(row[0] for row in store.conn.execute(
        "SELECT name FROM sqlite_master WHERE type = ?", ("table",)))
    assert "file_tags" in names


def test_set_file_tags_replaces_rather_than_accumulates(store):
    file_id = store.upsert_file(
        path="/photos/dog.jpg", size_bytes=100, mtime_ns=1, source_kind="file")
    store.set_file_tags(file_id, ["Dog", "Park", "dog"])
    rows = store.conn.execute(
        "SELECT tag FROM file_tags WHERE file_id = ? ORDER BY tag",
        (file_id,)).fetchall()
    got = [row["tag"] for row in rows]
    assert got == ["dog", "park"]


def test_set_file_tags_clears_old_on_retag(store):
    file_id = store.upsert_file(
        path="/photos/cat.jpg", size_bytes=100, mtime_ns=1, source_kind="file")
    store.set_file_tags(file_id, ["dog", "park"])
    store.set_file_tags(file_id, ["cat"])
    rows = store.conn.execute(
        "SELECT tag FROM file_tags WHERE file_id = ?", (file_id,)).fetchall()
    got = [row["tag"] for row in rows]
    assert got == ["cat"]


def test_set_file_tags_empty_clears_everything(store):
    file_id = store.upsert_file(
        path="/photos/x.jpg", size_bytes=100, mtime_ns=1, source_kind="file")
    store.set_file_tags(file_id, ["dog"])
    store.set_file_tags(file_id, [])
    rows = store.conn.execute(
        "SELECT tag FROM file_tags WHERE file_id = ?", (file_id,)).fetchall()
    assert rows == []


def test_shows_is_a_real_offered_command():
    command = command_for("shows")
    assert command is not None
    assert command.source == "shows"
    assert expand_slashes("/shows dog") == "shows:dog"


def test_the_vocabulary_is_browsable_with_counts(store):
    a = store.upsert_file(path="/p/a.jpg", size_bytes=1, mtime_ns=1, source_kind="file")
    b = store.upsert_file(path="/p/b.jpg", size_bytes=1, mtime_ns=1, source_kind="file")
    c = store.upsert_file(path="/p/c.jpg", size_bytes=1, mtime_ns=1, source_kind="file")
    store.set_file_tags(a, ["dog", "park"])
    store.set_file_tags(b, ["dog"])
    store.set_file_tags(c, ["cat"])
    rows = store.distinct_value_counts("shows")
    counts = dict((row.value, row.count) for row in rows)
    assert counts["dog"] == 2
    assert counts["cat"] == 1
    assert counts["park"] == 1


def test_a_photo_is_found_by_its_tag_end_to_end(store):
    tagged = store.upsert_file(
        path="/p/beach-dog.jpg", size_bytes=1, mtime_ns=1, source_kind="file")
    untagged = store.upsert_file(
        path="/p/other.jpg", size_bytes=1, mtime_ns=1, source_kind="file")
    store.set_file_tags(tagged, ["dog", "beach"])
    store.set_file_tags(untagged, ["cat"])
    parsed = parse_query(expand_slashes("/shows dog"))
    assert parsed.shows == ("dog",)
    assert parsed.has_filters
    where, params = file_filter_sql(parsed)
    sql = "SELECT id FROM files f WHERE 1=1 " + where
    rows = store.conn.execute(sql, params).fetchall()
    ids = set(row["id"] for row in rows)
    assert ids == set([tagged])


def test_a_negated_tag_excludes_the_file(store):
    tagged = store.upsert_file(
        path="/p/dog.jpg", size_bytes=1, mtime_ns=1, source_kind="file")
    other = store.upsert_file(
        path="/p/cat.jpg", size_bytes=1, mtime_ns=1, source_kind="file")
    store.set_file_tags(tagged, ["dog"])
    store.set_file_tags(other, ["cat"])
    parsed_excl = parse_query("-shows:dog")
    where, params = file_filter_sql(parsed_excl)
    sql = "SELECT id FROM files f WHERE 1=1 " + where
    rows = store.conn.execute(sql, params).fetchall()
    ids = set(row["id"] for row in rows)
    assert ids == set([other])


def test_shows_matches_the_field_alias_table():
    from app.search.query import _FIELD_ALIASES

    for command in COMMANDS:
        for spelling in command.spellings:
            assert spelling in _FIELD_ALIASES
