"""UI phase B on the real window: the list stays free while an answer is written.

Layer: L5 (pytest-qt on the real `MainWindow`, with the Chat tab's fake engine)

2026-10-13. The conversations list locked for the whole of an answer, because
switching would have thrown the answer's bubble away and written the reply into
whichever conversation was open when it landed. The answer now belongs to the
conversation that asked it: you can browse and switch while it is written, and it
is saved into its own conversation; only sending waits for it.
"""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from app.ui.chat_sessions import new_session                             # noqa: E402
from tests.unit.conftest import gui_pump                                 # noqa: E402
from tests.unit.test_chat_tab_qt import _wait, answered, ask, chat       # noqa: E402,F401

pytestmark = pytest.mark.gui


@pytest.mark.slow
def test_switching_away_mid_answer_keeps_the_answer_in_its_own_conversation(chat):
    c = chat
    c.fake.hold_at = 4
    ask(c, "What did we agree about the deposit?")
    _wait(c.qtbot, c.fake.held.is_set)
    other = new_session()                    # added now: the saved list has been read
    other.title = "Another chat"
    c.ctl.sessions.append(other)
    c.ctl._refresh_list()
    asking = c.ctl.session
    assert c.view.sessions.list.isEnabled()

    c.view.sessions.selected.emit(other.id)               # the person picks another
    gui_pump(c.app)
    assert c.ctl.session is other and c.view.bubbles.bubbles() == []

    c.view.sessions.selected.emit(asking.id)              # and comes back mid-answer
    gui_pump(c.app)
    assert len(c.view.bubbles.bubbles()) == 2, "the question and the answer being written"

    c.view.sessions.selected.emit(other.id)
    c.fake.release()
    answered(c)
    assert [t.role for t in asking.turns] == ["user", "assistant"]
    assert other.turns == [], "nothing was written into the conversation on screen"
    assert c.ctl.session is other and c.view.bubbles.bubbles() == []

    c.view.sessions.selected.emit(asking.id)
    gui_pump(c.app)
    assert "protected scheme" in c.view.bubbles.bubbles()[-1].raw


def test_a_pinned_conversation_is_saved_pinned(chat):
    c = chat
    ask(c, "keep me")
    answered(c)
    session = c.ctl.session
    before = session.updated
    c.view.sessions.pinned.emit(session.id, True)
    answered(c)
    assert c.backend.records[session.id]["pinned"] is True
    assert session.updated == before, "pinning does not move it in the dates"


def test_up_in_the_empty_box_brings_the_last_message_back_to_change(chat):
    from PySide6.QtCore import Qt

    c = chat
    ask(c, "deposit?")
    answered(c)
    edit = c.view.box.edit
    edit.setFocus()
    c.qtbot.keyClick(edit, Qt.Key.Key_Up)
    assert edit.toPlainText() == "deposit?"
    assert len(c.ctl.session.turns) == 2, "nothing is taken away until it is sent"
    edit.setPlainText("deposit, again?")
    c.qtbot.keyClick(edit, Qt.Key.Key_Return)
    answered(c)
    assert [t.text for t in c.ctl.session.turns if t.role == "user"] == ["deposit, again?"]


@pytest.mark.slow
def test_coming_back_mid_answer_to_a_conversation_with_earlier_answers_shows_the_live_one(chat):
    """Review of PR 72: the redraw reused one name for the parked answer and each stored one, so
    with an earlier answer in the conversation the live answer never came back to the page."""
    c = chat
    ask(c, "First question")
    answered(c)
    c.fake.hold_at = 4
    ask(c, "What did we agree about the deposit?")
    _wait(c.qtbot, c.fake.held.is_set)
    asking = c.ctl.session
    live = c.view.bubbles.bubbles()[-1]
    other = new_session()
    other.title = "Another chat"
    c.ctl.sessions.append(other)
    c.ctl._refresh_list()

    c.view.sessions.selected.emit(other.id)
    gui_pump(c.app)
    c.view.sessions.selected.emit(asking.id)
    gui_pump(c.app)
    shown = c.view.bubbles.bubbles()
    assert len(shown) == 4, "two questions, the earlier answer and the one being written"
    assert shown[-1] is live and len(set(map(id, shown))) == 4

    c.fake.release()
    answered(c)
