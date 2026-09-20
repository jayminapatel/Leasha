"""The tab's session adapter against the REAL `SqliteStore` - the test that was missing.

Layer: L5 (no Qt; blocking calls, as a worker would make them).

**Why this file exists.** `ChatSessions` used to call the store's `save_session(record)` with one
dict; the real store's method is `save_session(session_id, title, turns, shelf, model)`, so every
save against the real store raised `TypeError` and **no conversation was ever kept** outside the
tests' in-memory double (found on 2026-09-20 by grabbing the real window). Every test used the
double. These use the store.
"""

from __future__ import annotations

import pytest

from app.chat.types import ChatTurn, Receipt
from app.storage.sqlite_store import SqliteStore
from app.ui.chat_sessions import ChatSessions, new_session, session_to_dict


@pytest.fixture()
def store(tmp_path):
    s = SqliteStore(tmp_path / "sessions.db").connect()
    yield s
    s.close()


LETTER = Receipt(101, "C:/mail/landlord.pdf", "landlord.pdf", "The deposit is 950 pounds.", "page 2", 1001)


def _session(title="Deposit"):
    session = new_session()
    session.title = title
    session.turns = [ChatTurn("user", "What is the deposit?"),
                     ChatTurn("assistant", "The deposit is 950 pounds [1].", receipts=[LETTER], model="mistral"),
                     ChatTurn("user", "shorter"),
                     ChatTurn("assistant", "950 pounds.", kind="chat", partial=True, notes=["stopped"])]
    session.shelf.add(LETTER.path, LETTER.name, LETTER.file_id, explicit=True)
    session.shelf.remove("C:/mail/spam.pdf")
    session.titled, session.auto_titled, session.web = False, True, True
    return session


def test_a_conversation_saved_to_the_real_store_comes_back_whole(store):
    original = _session()
    ChatSessions(store).save(session_to_dict(original))

    found = ChatSessions(store).load()                    # a new adapter: a restart
    assert len(found) == 1
    back = found[0]
    assert back.id == original.id                         # the tab's own id survives
    assert back.title == "Deposit" and back.auto_titled and back.web and not back.titled
    assert [(t.role, t.text, t.kind) for t in back.turns] == [
        (t.role, t.text, t.kind) for t in original.turns]
    assert back.turns[1].receipts == [LETTER] and back.turns[1].model == "mistral"
    assert back.turns[3].partial and back.turns[3].notes == ["stopped"]
    assert [i.path for i in back.shelf.items] == [LETTER.path] and back.shelf.items[0].pinned
    assert back.shelf.removed == {"C:/mail/spam.pdf"}


def test_saving_the_same_conversation_again_updates_it_instead_of_adding_another(store):
    sessions = ChatSessions(store)
    session = _session()
    sessions.save(session_to_dict(session))
    session.title = "A better title"
    session.turns.append(ChatTurn("user", "thanks"))
    sessions.save(session_to_dict(session))
    found = ChatSessions(store).load()
    assert len(found) == 1 and found[0].title == "A better title" and found[0].turns[-1].text == "thanks"


def test_two_conversations_and_delete(store):
    sessions = ChatSessions(store)
    a, b = _session("first"), _session("second")
    sessions.save(session_to_dict(a))
    sessions.save(session_to_dict(b))
    assert {s.title for s in ChatSessions(store).load()} == {"first", "second"}
    sessions.delete(a.id)
    assert [s.title for s in ChatSessions(store).load()] == ["second"]
    sessions.delete("never-existed")                      # nothing to remove is not an error


def test_deleting_after_a_restart_still_finds_the_row(store):
    session = _session()
    ChatSessions(store).save(session_to_dict(session))
    restarted = ChatSessions(store)
    restarted.load()                                      # the adapter learns the store's id here
    restarted.delete(session.id)
    assert ChatSessions(store).load() == []


def test_a_row_from_before_this_adapter_still_opens(store):
    """A session the store holds with no `_meta` (saved by the store's own callers): it opens, with a
    stable id of its own."""
    store.save_session(None, "Old one", [{"role": "user", "text": "hello", "kind": "answer", "notes": [],
                                          "receipts": [], "result_set": None}], [{"path": "C:/x.pdf", "name": "x.pdf"}])
    found = ChatSessions(store).load()
    assert len(found) == 1 and found[0].title == "Old one" and found[0].turns[0].text == "hello"
    assert found[0].id.startswith("store-") and [i.path for i in found[0].shelf.items] == ["C:/x.pdf"]
