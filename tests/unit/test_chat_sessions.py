"""Saved conversations: the schema-26 table, the store methods, and the round trip.

Layer: L8b/L1. Work order `202626270611-chat-tab` section 3d. **The engine layer owns
this migration**; the tab calls `save_session` / `load_sessions` / `delete_session`.
"""

from __future__ import annotations

import sqlite3

import pytest

from app.chat import sessions
from app.chat.sessions import Session
from app.chat.types import ChatTurn, Receipt
from app.storage import migrations
from app.storage.migrations import CURRENT_VERSION, MIGRATIONS, _v26_chat_sessions
from app.storage.sqlite_store import SqliteStore
from tests.fixtures import chat_eval as fx
from tests.unit.chat_env import Env, ask


@pytest.fixture()
def store(tmp_path):
    with SqliteStore(tmp_path / "s.db") as s:
        yield s


def _conversation() -> list[ChatTurn]:
    receipt = Receipt(7, "C:/Archive/Car/insurance-renewal.pdf", "insurance-renewal.pdf",
                      "Excess is 250 pounds.", "page 1", 42)
    return [ChatTurn("user", "What is the car insurance excess?"),
            ChatTurn("assistant", "The excess is 250 pounds [1].", receipts=[receipt],
                     notes=["a note"])]


# --------------------------------------------------------------------------- the migration

def test_the_schema_is_at_26_and_the_migration_is_registered():
    assert CURRENT_VERSION >= 26
    assert MIGRATIONS[26] is _v26_chat_sessions


def test_a_fresh_database_has_the_table_and_the_version(store):
    assert store.schema_version == CURRENT_VERSION
    columns = {row["name"] for row in store.conn.execute("PRAGMA table_info(chat_sessions)")}
    assert columns == {"id", "title", "model", "turns_json", "shelf_json", "created_at", "updated_at"}


def test_the_migration_is_additive_and_idempotent(tmp_path):
    """Applied to a v25 database it adds one table and touches nothing else; applied
    twice it changes nothing (`CREATE ... IF NOT EXISTS`)."""
    conn = sqlite3.connect(tmp_path / "old.db")
    conn.execute("CREATE TABLE files (id INTEGER PRIMARY KEY, path TEXT)")
    conn.execute("INSERT INTO files (path) VALUES ('C:/keep/me.txt')")
    _v26_chat_sessions(conn)
    _v26_chat_sessions(conn)
    assert conn.execute("SELECT path FROM files").fetchall() == [("C:/keep/me.txt",)]
    assert conn.execute("SELECT count(*) FROM chat_sessions").fetchone() == (0,)
    conn.close()


def test_a_database_migrated_up_from_v25_keeps_its_documents(tmp_path):
    """A real index, built at v25, opened by this build."""
    path = tmp_path / "v25.db"
    with SqliteStore(path) as s:
        s.upsert_file(path="C:/x/keep.txt", parent_dir="C:/x", ext="txt", size_bytes=1,
                      mtime_ns=1, status="INDEXED", source_kind="file")
        s.conn.execute("DROP TABLE chat_sessions")
        s.conn.execute("UPDATE schema_version SET version = 25")
        s.conn.commit()
    with SqliteStore(path) as s:
        assert s.schema_version == CURRENT_VERSION
        assert s.conn.execute("SELECT count(*) FROM files").fetchone()[0] == 1
        assert s.load_sessions() == []


# --------------------------------------------------------------------------- the store methods

def test_a_session_round_trips_with_its_turns_receipts_and_shelf(store):
    turns = _conversation()
    session = sessions.save_session(store, Session(turns=turns, shelf=[turns[1].receipts[0]],
                                                   model="qwen2.5:1.5b"))
    assert session.id and session.title == "What is the car insurance excess?"
    (loaded,) = sessions.load_sessions(store)
    assert loaded.id == session.id and loaded.model == "qwen2.5:1.5b"
    assert [t.role for t in loaded.turns] == ["user", "assistant"]
    assert loaded.turns[1].receipts == turns[1].receipts               # frozen dataclass equality
    assert loaded.turns[1].notes == ["a note"] and loaded.shelf == turns[1].receipts
    assert loaded.created_at and loaded.updated_at >= loaded.created_at


def test_saving_again_updates_in_place_and_orders_by_last_use(store):
    a = sessions.save_session(store, Session(title="first", turns=_conversation()))
    b = sessions.save_session(store, Session(title="second", turns=_conversation()))
    store.conn.execute("UPDATE chat_sessions SET updated_at = updated_at - 100 WHERE id = ?", (b.id,))
    store.conn.commit()
    a.turns.append(ChatTurn("user", "one more"))
    sessions.save_session(store, a)
    listed = sessions.load_sessions(store)
    assert [s.title for s in listed] == ["first", "second"] and len(listed) == 2
    assert len(listed[0].turns) == 3


def test_a_list_for_a_sidebar_need_not_parse_every_conversation(store):
    sessions.save_session(store, Session(title="x", turns=_conversation()))
    (row,) = store.load_sessions(with_turns=False)
    assert row["turns"] == [] and row["title"] == "x"


def test_delete_removes_one_and_says_whether_there_was_one(store):
    a = sessions.save_session(store, Session(title="a", turns=_conversation()))
    b = sessions.save_session(store, Session(title="b", turns=_conversation()))
    assert sessions.delete_session(store, a.id) is True
    assert sessions.delete_session(store, a.id) is False
    assert [s.title for s in sessions.load_sessions(store)] == ["b"]
    assert b.id != a.id


def test_a_stale_id_makes_a_new_session_instead_of_losing_the_conversation(store):
    session = sessions.save_session(store, Session(title="kept", turns=_conversation()))
    sessions.delete_session(store, session.id)
    again = sessions.save_session(store, session)                     # the other window deleted it
    assert again.id is not None and [s.title for s in sessions.load_sessions(store)] == ["kept"]


def test_a_damaged_row_never_hides_the_others(store):
    good = sessions.save_session(store, Session(title="good", turns=_conversation()))
    store.conn.execute(
        "INSERT INTO chat_sessions (title, model, turns_json, shelf_json, created_at, updated_at) "
        "VALUES ('broken', '', '{not json', '[]', 1, 1)")
    store.conn.commit()
    listed = sessions.load_sessions(store)
    by_title = {s.title: s for s in listed}
    assert by_title["good"].turns and by_title["broken"].turns == []
    assert good.id in {s.id for s in listed}


def test_a_find_answer_reopens_with_its_result_rows(tmp_path):
    env = Env(tmp_path)
    try:
        turn, _events = ask(env.engine("extractive"), "Find the tenancy agreement")
        assert turn.kind == "find" and turn.result_set
        session = sessions.save_session(env.store, Session(turns=[ChatTurn("user", "Find it"), turn]))
        (loaded,) = sessions.load_sessions(env.store)
        restored = loaded.turns[1]
        assert restored.kind == "find"
        assert [r.path for r in restored.result_set] == [r.path for r in turn.result_set]
        assert restored.result_set[0].chunk_id == turn.result_set[0].chunk_id
        assert session.id == loaded.id
    finally:
        env.close()


def test_a_title_is_the_first_question_trimmed_at_a_word():
    long_question = "What did we agree with the landlord about the deposit and the boiler and the inventory check?"
    title = sessions.title_for([ChatTurn("user", long_question)], limit=40)
    assert title == "What did we agree with the landlord..."
    assert sessions.title_for([]) == "New conversation"
