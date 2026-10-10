"""Schema v36 (storage review S1, 2026-10-10): the mail-header index follows only its columns.

Layer: L1

`messages_au` re-indexed a message's subject, sender and recipients on *every*
UPDATE of its row, and since v35 `set_read_stamps` updates every message of an
archive read to its end - so each one was deleted from `messages_fts` and added
again, unchanged. The trigger now fires only for `UPDATE OF subject, sender,
recipients`. These tests hold both halves, and the upgrade of an older index.
"""

from __future__ import annotations

import sqlite3

import pytest

from app.storage.migrations import CURRENT_VERSION, _write_version
from app.storage.sqlite_store import SqliteStore

_OLD_TRIGGER = """CREATE TRIGGER messages_au AFTER UPDATE ON messages BEGIN
  INSERT INTO messages_fts(messages_fts, rowid, subject, sender, recipients)
  VALUES('delete', old.file_id, old.subject, old.sender, old.recipients);
  INSERT INTO messages_fts(rowid, subject, sender, recipients)
  VALUES (new.file_id, new.subject, new.sender, new.recipients);
END"""


@pytest.fixture
def store(tmp_path):
    with SqliteStore(tmp_path / "index.db") as opened:
        if not opened._has_message_index():
            pytest.skip("this SQLite has no trigram tokenizer, so no header index")
        yield opened


def _index_blocks(store) -> int:
    """Blocks in the header index. Any delete-and-insert adds some."""
    return int(store.conn.execute("SELECT COUNT(*) FROM messages_fts_data").fetchone()[0])


def _message(store, number: int, subject: str) -> str:
    path = f"pst://archive.pst/{number}"
    file_id = store.upsert_file(path=path, size_bytes=1, mtime_ns=1,
                                source_kind="pst_message")
    store.set_message(file_id, store_path="archive.pst", entry_id=str(number),
                      subject=subject, sender="anna@example.org",
                      recipients="ben@example.org", sent_at=1)
    return path


def _hits(store, text: str) -> int:
    return int(store.conn.execute(
        "SELECT COUNT(*) FROM messages_fts WHERE messages_fts MATCH ?",
        (f'"{text}"',)).fetchone()[0])


def test_read_stamps_leave_the_header_index_alone(store):
    paths = [_message(store, n, f"Barnsley dairy audit {n}") for n in range(20)]
    before = _index_blocks(store)
    changes_before = store.conn.total_changes
    assert store.set_read_stamps([(path, "stamp-1") for path in paths]) == 20
    assert _index_blocks(store) == before, "a read stamp re-indexed the message's headers"
    # One row changed per stamp and nothing else - no FTS delete and insert.
    assert store.conn.total_changes - changes_before == 20
    assert _hits(store, "Barnsley") == 20


def test_a_changed_subject_is_still_mirrored(store):
    path = _message(store, 1, "Sheep shearing rota")
    file_id = int(store.conn.execute(
        "SELECT id FROM files WHERE path = ?", (path,)).fetchone()[0])
    store.set_message(file_id, store_path="archive.pst", entry_id="1",
                      subject="Goat milking rota", sender="anna@example.org",
                      recipients="ben@example.org", sent_at=1)
    assert _hits(store, "Sheep") == 0
    assert _hits(store, "Goat") == 1
    with store.write() as conn:
        conn.execute("UPDATE messages SET sender = ? WHERE file_id = ?",
                     ("carol@example.org", file_id))
    assert _hits(store, "carol@") == 1
    assert _hits(store, "anna@") == 0


def test_an_index_made_by_the_previous_build_is_upgraded(tmp_path):
    db = tmp_path / "index.db"
    with SqliteStore(db) as store:
        if not store._has_message_index():
            pytest.skip("this SQLite has no trigram tokenizer, so no header index")
        path = _message(store, 1, "An archive of old letters")
    with sqlite3.connect(db) as conn:                   # the way v35 left it
        conn.execute("DROP TRIGGER messages_au")
        conn.execute(_OLD_TRIGGER)
        _write_version(conn, 35)
    with SqliteStore(db) as store:
        assert store.schema_version == CURRENT_VERSION >= 36
        sql = store.conn.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'messages_au'").fetchone()[0]
        assert "UPDATE OF subject, sender, recipients" in sql
        before = _index_blocks(store)
        store.set_read_stamps([(path, "stamp")])
        assert _index_blocks(store) == before
        assert _hits(store, "letters") == 1


def test_the_upgrade_creates_no_mail_trigger_where_there_was_none(tmp_path):
    """No trigram, or a bulk run's dropped triggers: v36 must not invent one."""
    db = tmp_path / "index.db"
    with SqliteStore(db):
        pass
    with sqlite3.connect(db) as conn:
        conn.execute("DROP TRIGGER IF EXISTS messages_au")
        _write_version(conn, 35)
    with SqliteStore(db) as store:
        assert store.schema_version == CURRENT_VERSION
        assert store.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'messages_au'").fetchone() is None
