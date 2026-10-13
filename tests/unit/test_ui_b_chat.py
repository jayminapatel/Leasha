"""UI phase B: Chat should behave like a chat. 2026-10-13.

Layer: L5 (the presenter rules without a display; the widgets with one)

The owner asked for the app to feel like one world-class product, and for "chat
should behave like a chat". The approved review listed what a chat does that this
one did not: a conversation list grouped by date, with a filter, a right-click menu
and pinning; Up in an empty box to bring the last message back; the actions on the
newest answer always in view; the model and the reading named under each answer,
the narration folded into one line; starter questions you can click; and the shelf
saying "In scope (N)" only when something is.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from app.chat.types import ChatTurn, Receipt


def _at(days_ago: float, now: datetime) -> float:
    return (now - timedelta(days=days_ago)).timestamp()


def _session(title, updated, *, pinned=False, turns=()):
    return SimpleNamespace(id=title, title=title, updated=updated, pinned=pinned,
                           turns=list(turns))


# -- 1. the conversations list: by date, pinned first, filtered in memory ------------

def test_conversations_are_grouped_by_date_with_pinned_first():
    from app.ui.presenter.chat_history import PINNED_HEADING, group_sessions

    now = datetime(2026, 10, 13, 15, 0)
    sessions = [
        _session("today", _at(0.1, now)),
        _session("yesterday", _at(1, now)),
        _session("this week", _at(4, now)),
        _session("this month", _at(20, now)),
        _session("august", datetime(2026, 8, 3, 9).timestamp()),
        _session("last year", datetime(2025, 8, 3, 9).timestamp()),
        _session("kept", _at(40, now), pinned=True),
    ]
    groups = group_sessions(sessions, now.timestamp())
    assert [(h, [s.title for s in items]) for h, items in groups] == [
        (PINNED_HEADING, ["kept"]),
        ("Today", ["today"]),
        ("Yesterday", ["yesterday"]),
        ("Previous 7 days", ["this week"]),
        ("Previous 30 days", ["this month"]),
        ("August 2026", ["august"]),
        ("August 2025", ["last year"]),
    ]


def test_the_filter_matches_the_title_or_what_was_said_and_drops_empty_groups():
    from app.ui.presenter.chat_history import group_sessions

    now = datetime(2026, 10, 13, 15, 0)
    sessions = [
        _session("Boiler licence", _at(0.1, now)),
        _session("Holiday", _at(3, now), turns=[ChatTurn("user", "photos from the BEACH")]),
        _session("Rent", _at(3, now)),
    ]
    groups = group_sessions(sessions, now.timestamp(), query="beach")
    assert [(h, [s.title for s in items]) for h, items in groups] == [
        ("Previous 7 days", ["Holiday"])]
    assert [s.title for _h, items in group_sessions(sessions, now.timestamp(), query="boiler lic")
            for s in items] == ["Boiler licence"]


def test_a_pin_is_kept_with_the_conversation_in_the_stores_own_json():
    from app.ui.chat_sessions import ChatSessions, new_session, session_to_dict

    class PiecesStore:
        """The real store's shape: `save_session(id, title, turns, shelf)`."""
        def __init__(self):
            self.rows = {}

        def save_session(self, session_id, title, turns, shelf, model=""):
            new_id = session_id or len(self.rows) + 1
            self.rows[new_id] = {"id": new_id, "title": title, "turns": turns,
                                 "shelf": shelf, "updated_at": time.time()}
            return new_id

        def load_sessions(self, with_turns=True):
            return list(self.rows.values())

    session = new_session()
    session.turns.append(ChatTurn("user", "keep me at the top"))
    session.pinned = True
    store = PiecesStore()
    ChatSessions(store).save(session_to_dict(session))
    back = ChatSessions(store).load()
    assert [s.pinned for s in back] == [True]


# -- 4. the model and the work under each answer ----------------------------------

def test_the_footer_names_the_model_and_how_many_documents_were_read():
    from app.ui.presenter.chat import answer_meta

    receipts = [Receipt(1, "C:/a.pdf", "a.pdf", "q"), Receipt(2, "C:/b.pdf", "b.pdf", "q"),
                Receipt(1, "C:/a.pdf", "a.pdf", "q2")]
    turn = ChatTurn("assistant", "x [1]", receipts=receipts, model="qwen2.5:3b")
    assert answer_meta(turn) == "qwen2.5:3b \u00b7 Read 2 documents"
    one = ChatTurn("assistant", "x [1]", receipts=receipts[:1], model="")
    assert answer_meta(one) == "Read 1 document"
    listed = ChatTurn("assistant", "Here they are.", receipts=receipts, kind="find",
                      result_set=[object()], model="qwen2.5:3b")
    assert answer_meta(listed) == "qwen2.5:3b", "a list was found, not read"
    assert answer_meta(ChatTurn("assistant", "Hello!", kind="chat")) == ""


def test_the_narration_folds_into_one_line_that_keeps_what_was_read():
    from app.ui.presenter.chat import THINKING_LINE, work_summary

    steps = [THINKING_LINE, "Searching...", "Searching... found 4 documents",
             "Reading the lease and the 2019 letter..."]
    headline, lines = work_summary(steps)
    assert headline == "Searched your files \u00b7 4 found"
    assert lines == ["Searching... found 4 documents",
                     "Reading the lease and the 2019 letter..."]
    assert work_summary([THINKING_LINE]) is None, "a plain chat searched nothing"
    turn = ChatTurn("assistant", "x", kind="find", result_set=[object()] * 7)
    assert work_summary(["Looking for PDFs from Dave..."], turn)[0] == \
        "Searched your files \u00b7 7 found"


def test_the_work_lines_are_saved_with_the_turn():
    from app.chat import sessions as stored
    from app.ui import chat_sessions

    turn = ChatTurn("assistant", "x", work=["Searching... found 2 documents"])
    for module in (stored, chat_sessions):
        assert module.turn_from_dict(module.turn_to_dict(turn)).work == turn.work


# -- 5 and 6. starter questions, and the shelf's words -----------------------------

def test_the_starter_questions_are_the_existing_examples():
    from app.ui.presenter.chat import EMPTY_HINT, STARTER_QUESTIONS

    assert 3 <= len(STARTER_QUESTIONS) <= 4
    assert all(q in EMPTY_HINT for q in STARTER_QUESTIONS)
    assert STARTER_QUESTIONS[0] == "What did we agree with the landlord about the deposit?"


def test_the_shelf_says_in_scope():
    from app.ui.presenter.chat import shelf_count_label

    assert shelf_count_label(1) == "In scope (1)"
    assert shelf_count_label(4, 1) == "In scope (4), 1 kept"


# ===================================================================================
# The widgets
# ===================================================================================

pytest.importorskip("PySide6")


@pytest.fixture()
def sessions_list(qapp, qtbot):
    from app.ui.widgets.chat_session_list import SessionList

    widget = SessionList()
    qtbot.addWidget(widget)
    now = time.time()
    widget.set_sessions([_session("Boiler", now), _session("Rent", now - 86400 * 3),
                         _session("Old", now - 86400 * 60)], "Boiler")
    widget.show()
    qtbot.waitExposed(widget)
    return widget


def _rows(widget):
    from app.ui.widgets.chat_session_list import ID_ROLE

    out = []
    for i in range(widget.list.count()):
        item = widget.list.item(i)
        if not item.isHidden():
            out.append(("item" if item.data(ID_ROLE) else "head", item.text()))
    return out


def test_the_list_has_date_headings_you_cannot_pick(sessions_list):
    from PySide6.QtCore import Qt

    rows = _rows(sessions_list)
    assert rows[0] == ("head", "Today") and ("item", "Boiler") in rows
    assert ("head", "Previous 7 days") in rows
    head = sessions_list.list.item(0)
    assert not head.flags() & Qt.ItemFlag.ItemIsSelectable
    assert sessions_list.filter.placeholderText() == "Search chats"


def test_typing_in_the_filter_hides_the_rest(sessions_list, qtbot):
    qtbot.keyClicks(sessions_list.filter, "ren")
    assert _rows(sessions_list) == [("head", "Previous 7 days"), ("item", "Rent")]
    sessions_list.filter.clear()
    assert ("item", "Old") in _rows(sessions_list)


def test_the_right_click_menu_renames_pins_and_deletes(sessions_list):
    item = sessions_list.list.currentItem()
    menu = sessions_list.menu_for(item)
    words = [a.text() for a in menu.actions() if a.text()]
    assert words == ["Rename", "Pin to top", "Delete"]
    keys = {a.text(): a.shortcut().toString() for a in menu.actions() if a.text()}
    assert keys["Rename"] == "F2" and keys["Delete"] == "Del"
    pinned = []
    sessions_list.pinned.connect(lambda sid, on: pinned.append((sid, on)))
    next(a for a in menu.actions() if a.text() == "Pin to top").trigger()
    assert pinned == [("Boiler", True)]


def test_delete_and_f2_work_on_the_list(sessions_list, qtbot):
    from PySide6.QtCore import Qt

    asked, gone = [], []
    sessions_list.confirm_delete = lambda title: asked.append(title) or True
    sessions_list.deleted.connect(gone.append)
    sessions_list.list.setFocus()
    qtbot.keyClick(sessions_list.list, Qt.Key.Key_Delete)
    assert asked == ["Boiler"] and gone == ["Boiler"]
    qtbot.keyClick(sessions_list.list, Qt.Key.Key_F2)
    assert sessions_list.list.state() == sessions_list.list.State.EditingState


def test_the_list_stays_usable_while_an_answer_is_written(qapp, qtbot):
    from app.ui.chat_view import ChatView

    view = ChatView()
    qtbot.addWidget(view)
    view.set_busy(True)
    assert view.sessions.list.isEnabled() and view.sessions.filter.isEnabled()
    assert view.sessions.new_button.isEnabled()
    view.shutdown()


# -- 2. keys ---------------------------------------------------------------------

@pytest.fixture()
def box(qapp, qtbot):
    from app.ui.widgets.chat_message_box import MessageBox

    widget = MessageBox()
    qtbot.addWidget(widget)
    widget.show()
    qtbot.waitExposed(widget)
    widget.edit.setFocus()
    return widget


def test_enter_on_an_empty_box_does_nothing(box, qtbot):
    from PySide6.QtCore import Qt

    heard = []
    box.submitted.connect(heard.append)
    box.walk_requested.connect(heard.append)
    qtbot.keyClick(box.edit, Qt.Key.Key_Return)
    assert heard == [] and not hasattr(box, "open_source_requested")


def test_alt_up_and_down_walk_the_sources_and_up_recalls(box, qtbot):
    from PySide6.QtCore import Qt

    walked, recalled = [], []
    box.walk_requested.connect(walked.append)
    box.recall_requested.connect(lambda: recalled.append(True))
    qtbot.keyClick(box.edit, Qt.Key.Key_Down, Qt.KeyboardModifier.AltModifier)
    qtbot.keyClick(box.edit, Qt.Key.Key_Up, Qt.KeyboardModifier.AltModifier)
    assert walked == [1, -1] and recalled == []
    qtbot.keyClick(box.edit, Qt.Key.Key_Up)
    assert recalled == [True] and walked == [1, -1], "plain Up no longer walks"
    box.edit.setPlainText("typed")
    qtbot.keyClick(box.edit, Qt.Key.Key_Up)
    assert recalled == [True], "Up in a box with words in it is the caret's"


def test_a_recalled_message_is_sent_as_an_edit_and_esc_lets_it_go(box, qtbot):
    from PySide6.QtCore import Qt

    edited, sent = [], []
    box.edit_submitted.connect(edited.append)
    box.submitted.connect(sent.append)
    box.begin_edit("What licence?")
    assert box.edit.toPlainText() == "What licence?" and box.editing
    qtbot.keyClick(box.edit, Qt.Key.Key_Escape)
    assert box.edit.toPlainText() == "" and not box.editing
    box.begin_edit("What licence?")
    qtbot.keyClicks(box.edit, " now")
    qtbot.keyClick(box.edit, Qt.Key.Key_Return)
    assert edited == ["What licence? now"] and sent == [] and not box.editing


@pytest.mark.slow
def test_ctrl_shift_o_is_free_and_starts_a_new_chat(gui_mainwindow, qtbot):
    from PySide6.QtGui import QKeySequence

    _app, window, _store, _engine = gui_mainwindow
    owned = [a for a in window.actions()
             if a.shortcut() == QKeySequence("Ctrl+Shift+O")]
    assert len(owned) == 1, "one window-wide owner of the key"
    shown = [a.text() for a in window._menu_actions
             if a.shortcut().toString() == "Ctrl+Shift+O"]
    assert shown == ["New chat"], "and Help > Keyboard shortcuts lists it"
    taken = [a.shortcut().toString() for a in window.actions() + window._menu_actions]
    assert "Alt+Up" not in taken and "Alt+Down" not in taken
    started = []
    window.chat_ctl.new_chat = lambda: started.append(True)
    owned[0].trigger()
    assert started == [True] and window._current_view() is window.chat_view


# -- 3. the actions on the newest answer, and Edit on the last message -------------

def _finished(qtbot, *, model="", receipts=(), steps=()):
    from app.ui.widgets.chat_bubbles import AnswerBubble

    bubble = AnswerBubble()
    qtbot.addWidget(bubble)
    for step in steps:
        bubble.narrate(step)
    bubble.append("The licence is 12400.")
    bubble.finish(ChatTurn("assistant", "The licence is 12400.", receipts=list(receipts),
                           kind="chat", model=model))
    bubble.show()
    return bubble


def test_the_newest_answer_shows_copy_and_regenerate_without_the_pointer(qapp, qtbot):
    newest, older = _finished(qtbot), _finished(qtbot)
    newest.set_last(True)
    older.set_last(False)
    newest.set_hover(False)
    older.set_hover(False)
    assert newest.actions.isVisible() and newest.copy_button.isVisible()
    assert newest.regenerate_button.isVisible()
    assert not older.actions.isVisible(), "older answers keep hover-only"
    older.set_hover(True)
    assert older.actions.isVisible()


def test_edit_is_in_view_on_your_last_message(qapp, qtbot):
    from app.ui.widgets.chat_bubbles import UserBubble

    bubble = UserBubble("What licence?")
    qtbot.addWidget(bubble)
    bubble.set_last(True)
    bubble.set_hover(False)
    assert not bubble.edit_button.isHidden()
    bubble.set_last(False)
    assert bubble.edit_button.isHidden()


def test_the_answer_names_its_model_and_folds_its_search(qapp, qtbot):
    receipts = [Receipt(1, "C:/a.pdf", "a.pdf", "q"), Receipt(2, "C:/b.pdf", "b.pdf", "q")]
    bubble = _finished(qtbot, model="qwen2.5:3b", receipts=receipts,
                       steps=["Searching...", "Searching... found 2 documents",
                              "Reading a.pdf and b.pdf..."])
    assert bubble.meta.text() == "qwen2.5:3b \u00b7 Read 2 documents"
    assert not bubble.meta.isHidden()
    assert bubble.narration.isHidden()
    assert bubble.work_button.text().startswith("Searched your files \u00b7 2 found")
    assert not bubble.work_button.isHidden() and bubble.work_detail.isHidden()
    bubble.work_button.click()
    assert not bubble.work_detail.isHidden()
    assert "Reading a.pdf and b.pdf..." in bubble.work_detail.text()
    layout = bubble.layout()
    assert layout.indexOf(bubble.work_button) > layout.indexOf(bubble.body), "under the answer"


# -- 5. starter cards ------------------------------------------------------------

def test_a_starter_card_fills_the_box_and_sends(qapp, qtbot):
    from app.ui.chat_view import ChatView
    from app.ui.presenter.chat import STARTER_QUESTIONS

    view = ChatView()
    qtbot.addWidget(view)
    view.show_available(True)
    cards = view.bubbles.starters
    assert [c.text() for c in cards] == list(STARTER_QUESTIONS)
    sent = []
    view.question_submitted.connect(sent.append)
    cards[1].click()
    assert sent == [STARTER_QUESTIONS[1]]
    view.shutdown()


# -- 6. the shelf shows only when something is in scope ----------------------------

def test_the_shelf_hides_until_something_is_in_scope(qapp, qtbot):
    from app.ui.widgets.chat_shelf import ShelfBar

    shelf = ShelfBar()
    qtbot.addWidget(shelf)
    assert shelf.isHidden()
    shelf.add_receipt(Receipt(3, "C:/docs/lease.pdf", "lease.pdf", "q"))
    assert not shelf.isHidden() and shelf.button.text() == "In scope (1)"
