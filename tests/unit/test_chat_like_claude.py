r"""Order 1o: Chat as one calm column, actions on hover and in the right-click menu.

Layer: L5

2026-10-11, the owner: "the chat interface can it be like claude for example as
the current one looks dated may be buttons are not required just the context menu".
"""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytestmark = pytest.mark.gui


_MADE: list = []


@pytest.fixture
def app():
    """The application; every widget a test shows here is closed and deleted after it,
    so nothing is left open to change what a later test's window does."""
    from PySide6.QtCore import QThreadPool
    from PySide6.QtWidgets import QApplication

    application = QApplication.instance() or QApplication([])
    yield application
    for widget in _MADE:
        widget.close()
        widget.deleteLater()
    _MADE.clear()
    QThreadPool.globalInstance().waitForDone(5000)
    application.processEvents()


def _answer(app, *, last=True, stopped=False):
    from app.ui.widgets.chat_bubbles import AnswerBubble

    bubble = AnswerBubble()
    bubble.append("The boiler licence is **12400** and is current.")
    bubble.finish(SimpleNamespace(text=bubble.raw, kind="answer", partial=False),
                  stopped=stopped)
    bubble.set_last(last)
    _MADE.append(bubble)
    bubble.show()
    app.processEvents()
    return bubble


def _texts(menu):
    return [a.text() for a in menu.actions() if a.text()]


def test_the_actions_show_on_hover_only_and_keep_their_room(app):
    # UI phase B (2026-10-13): on an older answer. The newest keeps its row in view
    # (`test_ui_b_chat.py`).
    bubble = _answer(app, last=False)
    bubble.set_hover(False)                    # offscreen, a new widget may open under the pointer
    assert not bubble.actions.isVisible(), "the action row shows without the pointer"
    assert bubble.actions.sizePolicy().retainSizeWhenHidden()
    bubble.set_hover(True)
    assert bubble.actions.isVisible() and bubble.copy_button.isVisible()
    bubble.set_hover(False)
    assert not bubble.actions.isVisible()


def test_every_answer_action_is_in_its_menu(app):
    from app.ui.widgets.chat_message_menu import COPY_PLAIN, answer_menu

    bubble = _answer(app)
    assert _texts(answer_menu(bubble)) == ["Copy", COPY_PLAIN, "Regenerate"]
    stopped = _answer(app, stopped=True)
    assert _texts(answer_menu(stopped)) == ["Copy", COPY_PLAIN, "Try again"]
    older = _answer(app, last=False)
    assert _texts(answer_menu(older)) == ["Copy", COPY_PLAIN]


def test_the_menu_actions_do_what_the_buttons_did(app):
    from PySide6.QtGui import QGuiApplication

    from app.ui.widgets.chat_message_menu import COPY_PLAIN, answer_menu

    bubble = _answer(app)
    heard = []
    bubble.regenerate_requested.connect(lambda: heard.append("regenerate"))
    menu = answer_menu(bubble)
    actions = {a.text(): a for a in menu.actions() if a.text()}
    actions["Regenerate"].trigger()
    assert heard == ["regenerate"]
    actions[COPY_PLAIN].trigger()
    assert "12400" in QGuiApplication.clipboard().text()
    assert "**" not in QGuiApplication.clipboard().text()


def test_a_right_click_on_the_answer_text_opens_its_menu(app):
    from PySide6.QtCore import QPoint
    from PySide6.QtGui import QContextMenuEvent
    from PySide6.QtWidgets import QTextBrowser

    bubble = _answer(app)
    prose = bubble.findChildren(QTextBrowser)[0]
    event = QContextMenuEvent(QContextMenuEvent.Reason.Mouse, QPoint(3, 3), QPoint(3, 3))
    app.sendEvent(prose.viewport(), event)
    assert "Regenerate" in _texts(bubble.last_menu)
    bubble.last_menu.close()


def test_the_persons_message_has_copy_and_edit_in_its_menu(app):
    from app.ui.widgets.chat_bubbles import UserBubble
    from app.ui.widgets.chat_message_menu import user_menu

    bubble = UserBubble("What licence does the boiler have?")
    _MADE.append(bubble)
    bubble.set_last(True)
    assert _texts(user_menu(bubble)) == ["Copy", "Edit"]
    # UI phase B (2026-10-13): Edit is in view on the last message, pointer or not.
    assert not bubble.edit_button.isHidden()
    bubble.set_last(False)
    assert _texts(user_menu(bubble)) == ["Copy"]


def test_the_conversation_and_the_box_share_one_centred_column(app):
    from app.ui.chat_view import ChatView
    from app.ui.widgets.chat_bubbles import COLUMN_MAX

    view = ChatView()
    _MADE.append(view)
    view.resize(1800, 900)
    view.show()
    app.processEvents()
    assert view.bubbles.column_widget.maximumWidth() == COLUMN_MAX
    assert view.box.frame.objectName() == "chatComposer"
    footer = [view.box.footer.itemAt(i).widget() for i in range(view.box.footer.count())]
    assert view.speed in footer and view.model_picker in footer
    assert footer.index(view.speed) < footer.index(view.box.send_button)
    view.shutdown()


def test_an_empty_conversation_reads_like_the_search_pages_empty_box(app):
    from app.ui.presenter.chat import EMPTY_HEADING, STARTER_QUESTIONS
    from app.ui.widgets.chat_bubbles import BubbleList

    bubbles = BubbleList()
    _MADE.append(bubbles)
    assert bubbles.empty_heading.objectName() == "emptyHeadline"      # Search's headline
    assert bubbles.empty_heading.text() == EMPTY_HEADING
    # UI phase B (2026-10-13): the greeting's example questions are cards to click.
    assert [c.text() for c in bubbles.starters] == list(STARTER_QUESTIONS)
