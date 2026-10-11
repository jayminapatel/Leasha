"""Chat knows the contact book, and takes the same `/` switches as every other box."""

from __future__ import annotations

import pytest

from app.accounts.store import AccountStore  # noqa: F401
from app.chat.contacts import contact_answer
from app.chat.switches import split_switches
from app.contacts.card import Card, Field
from app.contacts.hub import ContactHub
from app.storage.sqlite_store import SqliteStore


@pytest.fixture
def store(tmp_path):
    s = SqliteStore(tmp_path / "k.db").connect()
    hub = ContactHub(s)
    hub.create(Card(scalars={"given": "Zane", "family": "Vandernest", "company": "Honeywell", "title": "Sales Lead"},
                    fields=[Field("phone", "+973 3333 0694", "work"), Field("email", "zane@honeywell.com", "work")]))
    hub.create(Card(scalars={"given": "Zanele", "family": "Dube"}, fields=[Field("phone", "+27 82 000 0000", "mobile")]))
    yield s
    s.close()


def test_a_question_about_a_person_in_the_book_is_answered_from_the_card(store):
    answer = contact_answer(store, "What is Zane's phone number?")
    assert answer.startswith("From your contacts") and "+973 3333 0694" in answer and "Honeywell" in answer
    assert "Zanele" not in answer and "+27 82" not in answer           # Zane is not Zanele


def test_a_question_that_is_not_about_a_person_in_the_book_is_left_alone(store):
    assert contact_answer(store, "What did we agree with the landlord about the lease?") is None
    assert contact_answer(store, "What is Nobody Atall's phone number?") is None
    assert contact_answer(store, "Zane") is None                       # no question asked of the card


def test_switches_become_filters_and_the_rest_is_the_question():
    text, filters = split_switches("/from dave /after 2024-01-01 what did he say about the contract")
    assert text == "what did he say about the contract"
    assert filters == ["from:dave", "after:2024-01-01"]
    assert split_switches("plain question") == ("plain question", [])
    assert split_switches("/type pdf") == ("/type pdf", ["type:pdf"])  # only switches: the question stays whole
    assert split_switches("/attachment *.xlsx budget")[1] == ["attachment:*.xlsx"]


def test_the_chat_box_offers_every_switch_and_inserts_the_one_chosen(qtbot):
    from PySide6.QtCore import Qt

    from app.search.commands import COMMANDS
    from app.ui.widgets.chat_commands import OFFERED
    from app.ui.widgets.chat_message_box import MessageBox

    box = MessageBox()
    qtbot.addWidget(box)
    box.show()
    assert set(OFFERED) == {c.name for c in COMMANDS}                    # all of them, nothing left out
    box.edit.setPlainText("what did he say /att")
    cursor = box.edit.textCursor()
    cursor.movePosition(cursor.MoveOperation.End)
    box.edit.setTextCursor(cursor)
    menu = box.commands
    assert menu.word() == "/att" and menu._list.isVisible()
    assert menu._list.item(0).data(Qt.ItemDataRole.UserRole) == "attachment"
    menu._insert("attachment")
    assert box.edit.toPlainText() == "what did he say /attachment "
    box.edit.setPlainText("an ordinary question")
    assert not menu._list.isVisible()                                   # no slash, no menu: Enter sends as before
