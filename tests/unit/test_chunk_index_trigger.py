"""Schema v28 (work order 0x item 5d): the keyword index follows only its own columns.

Layer: L1

`chunks_au` re-indexed a passage on *every* UPDATE of its row, and the
commonest one is the embedding thread's `UPDATE chunks SET embedded = 1` - so
every passage the indexer wrote was indexed twice, with a delete marker left
between. The trigger now fires only for `UPDATE OF text, symbols`. These tests
hold both halves: the wasted work is gone, and a real change to a passage's
words is still mirrored into the index exactly as before.
"""

from __future__ import annotations

import sqlite3

import pytest

from app.storage.migrations import CURRENT_VERSION, _write_version
from app.storage.sqlite_store import SqliteStore


@pytest.fixture
def store(tmp_path):
    with SqliteStore(tmp_path / "index.db") as opened:
        yield opened


def _index_pages(store) -> int:
    """How many blocks the keyword index holds. Any re-indexing adds some."""
    return int(store.conn.execute("SELECT COUNT(*) FROM chunks_fts_data").fetchone()[0])


def _add(store, text: str) -> list[int]:
    file_id = store.upsert_file(path=f"/{text[:8]}.txt", size_bytes=1, mtime_ns=1)
    return store.replace_chunks(file_id, [{"text": text}])


def _hits(store, word: str) -> int:
    return int(store.conn.execute(
        "SELECT COUNT(*) FROM chunks_fts WHERE chunks_fts MATCH ?", (word,)).fetchone()[0])


def test_marking_passages_embedded_leaves_the_keyword_index_alone(store):
    ids = _add(store, "the dairy audit for Barnsley")
    before = _index_pages(store)
    store.mark_embedded(ids)
    assert _index_pages(store) == before, "marking a passage embedded re-indexed its words"
    assert _hits(store, "barnsley") == 1


def test_marking_everything_unembedded_leaves_it_alone_too(store):
    ids = _add(store, "quarterly report on valve pressure")
    store.mark_embedded(ids)
    before = _index_pages(store)
    store.mark_all_unembedded()
    assert _index_pages(store) == before
    assert _hits(store, "valve") == 1


def test_changing_a_passages_words_is_still_mirrored(store):
    (chunk_id,) = _add(store, "an old sentence about sheep")
    with store.write() as conn:
        conn.execute("UPDATE chunks SET text = ? WHERE id = ?",
                     ("a new sentence about goats", chunk_id))
    assert _hits(store, "sheep") == 0
    assert _hits(store, "goats") == 1


def test_changing_the_symbols_column_is_still_mirrored(store):
    (chunk_id,) = _add(store, "ResetPasswordHandler")
    with store.write() as conn:
        conn.execute("UPDATE chunks SET symbols = ? WHERE id = ?", ("Wibble", chunk_id))
    assert _hits(store, "wibble") == 1


def test_an_index_made_by_the_previous_build_is_upgraded(tmp_path):
    """A v27 database carries the old, unconditional trigger; opening it with
    this build replaces it, and nothing else about the index changes."""
    db = tmp_path / "index.db"
    with SqliteStore(db) as store:
        ids = _add(store, "an archive of old letters")
    with sqlite3.connect(db) as conn:                   # put it back the way v27 left it
        conn.execute("DROP TRIGGER chunks_au")
        conn.execute("""CREATE TRIGGER chunks_au AFTER UPDATE ON chunks BEGIN
            INSERT INTO chunks_fts(chunks_fts, rowid, text, symbols)
            VALUES ('delete', old.id, old.text, old.symbols);
            INSERT INTO chunks_fts(rowid, text, symbols) VALUES (new.id, new.text, new.symbols);
        END""")
        _write_version(conn, 27)
    with SqliteStore(db) as store:
        # CURRENT_VERSION, not the literal: v29 (order 0z lane D) came after.
        assert store.schema_version == CURRENT_VERSION >= 28
        sql = store.conn.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'chunks_au'").fetchone()[0]
        assert "UPDATE OF text, symbols" in sql
        before = _index_pages(store)
        store.mark_embedded(ids)
        assert _index_pages(store) == before
        assert _hits(store, "letters") == 1
