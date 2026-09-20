"""An interrupted bulk index run must leave the word index able to index again.

Layer: L2. Index-tuning order `202626270114` section 6f (FTS trigger drop/restore).

A bulk run drops the FTS content triggers and restores them at the end, with a
dirty flag written *before* the drop so a killed run can be repaired on resume.
The repair rebuilt the rows already written - and never put the triggers back.
The drop is stored in the database, so every chunk indexed after a resume was
silently missing from the word index: keyword search covered less and less of the
corpus, and nothing said why. Found 2026-09-20 by reproducing it.
"""

from __future__ import annotations

import re

import pytest

from app.storage.migrations import CONTENT_TRIGGERS
from app.storage.sqlite_store import SqliteStore


@pytest.fixture()
def store(tmp_path):
    s = SqliteStore(tmp_path / "t.db").connect()
    yield s
    s.close()


def _add(store, key, text):
    file_id = store.upsert_file(key, size_bytes=10, mtime_ns=1, content_hash="h" + key)
    return store.replace_chunks(file_id, [{"ordinal": 0, "text": text}])


def _hits(store, word):
    with store.write() as conn:
        return [r[0] for r in conn.execute(
            "SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH ?", (word,))]


def _trigger_names(store):
    with store.write() as conn:
        return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")}


def test_the_dirty_flag_is_written_before_the_triggers_go(store):
    _add(store, "C:/a.txt", "alpha document")
    assert not store.get_state("fts_dirty")
    restore = store.drop_fts_triggers()
    assert restore, "nothing was dropped, so this proves nothing"
    assert store.get_state("fts_dirty") == "1"
    assert not {"chunks_ai", "chunks_ad", "chunks_au"} & _trigger_names(store)


def test_a_resume_after_an_interrupted_bulk_run_can_index_the_next_chunk(tmp_path):
    store = SqliteStore(tmp_path / "t.db").connect()
    _add(store, "C:/a.txt", "alpha document")
    store.drop_fts_triggers()                              # the bulk run starts ...
    _add(store, "C:/b.txt", "bravo document")              # ... writes with no trigger ...
    store.close()                                          # ... and is killed before restoring

    store = SqliteStore(tmp_path / "t.db").connect()       # the next run
    try:
        store.check_and_rebuild_fts_if_dirty()
        assert _hits(store, "bravo"), "what the interrupted run wrote is not searchable"
        assert not store.get_state("fts_dirty")

        _add(store, "C:/c.txt", "charlie document")        # an ordinary index, afterwards
        assert _hits(store, "charlie"), (
            "a chunk indexed after the resume is missing from the word index: the "
            "repair rebuilt the old rows but never put the triggers back")
    finally:
        store.close()


def test_the_mail_triggers_come_back_too(tmp_path):
    store = SqliteStore(tmp_path / "t.db").connect()
    try:
        with store.write() as conn:
            has_mail = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name = 'messages_fts'").fetchone()
        if not has_mail:
            pytest.skip("this SQLite has no trigram tokenizer, so there is no mail index")
        store.drop_fts_triggers()
        assert not {"messages_ai", "messages_ad", "messages_au"} & _trigger_names(store)
        store.check_and_rebuild_fts_if_dirty()
        assert {"messages_ai", "messages_ad", "messages_au"} <= _trigger_names(store)
    finally:
        store.close()


def test_a_clean_database_is_left_alone(store):
    """No flag, no work: the resume check must not rebuild on every run."""
    _add(store, "C:/a.txt", "alpha document")
    before = _trigger_names(store)
    store.check_and_rebuild_fts_if_dirty()
    assert _trigger_names(store) == before and _hits(store, "alpha")


def _normal(sql: str) -> str:
    return re.sub(r"\s+", " ", sql.replace("IF NOT EXISTS ", "")).strip().rstrip(";").strip()


def test_the_repair_list_matches_the_triggers_a_fresh_database_creates(store):
    """`CONTENT_TRIGGERS` is a copy of the migrations' definitions. If a later
    migration changes a trigger and this list is not updated, a repaired database
    would carry an older definition than a new one - this fails first."""
    with store.write() as conn:
        created = {r[0]: r[1] for r in conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type = 'trigger' "
            "AND (name LIKE 'chunks_a%' OR name LIKE 'messages_a%')")}
    assert created, "the fresh database has no FTS triggers to compare"
    copies = {re.search(r"TRIGGER (?:IF NOT EXISTS )?(\w+)", sql).group(1): sql
              for sql in CONTENT_TRIGGERS}
    for name, sql in created.items():
        assert name in copies, f"{name} exists in a fresh database but not in CONTENT_TRIGGERS"
        assert _normal(copies[name]) == _normal(sql), f"{name} has drifted from the migration"
