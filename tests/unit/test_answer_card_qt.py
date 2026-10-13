r"""The Search page's answer card, typed into for real.

Layer: L7

Order `smarter-answers` 3c. A question typed into the Search box gets a card above
the results with the answer, its numbered sources and "Continue in Chat"; a word
gets none; the switch off means none; Chat busy means none; a changed box drops
an answer still on its way. The results are drawn either way and never wait.
"""

from __future__ import annotations

import pytest
from PySide6.QtCore import Qt

from tests.unit.chat_fakes import DEPOSIT_TEXT, FakeChatEngine
from tests.unit.conftest import gui_pump

pytestmark = pytest.mark.gui

QUESTION = "What did we agree with the landlord about the deposit?"


@pytest.fixture()
def card(gui_mainwindow, qtbot, monkeypatch):
    app, window, _store, _engine = gui_mainwindow
    ctl = window.chat_ctl
    fake = FakeChatEngine()
    ctl.engine_factory = lambda: fake
    ctl.engine = None
    ctl._ask = None
    view = window.search_view
    card = view.answer_card
    card.answer_timer.setInterval(10)
    opened, continued = [], []
    card.open_requested.disconnect()
    card.open_requested.connect(opened.append)
    card.continue_requested.disconnect()
    card.continue_requested.connect(continued.append)
    monkeypatch.setitem(window._settings_overrides, "search_answers", True)
    view.input.clear()
    gui_pump(app)
    card.fake, card.opened, card.continued, card.view = fake, opened, continued, view
    yield card
    view.input.clear()
    card.answer_timer.setInterval(1200)


@pytest.mark.slow
def test_a_question_gets_an_answer_with_sources_above_the_results(card, qtbot):
    qtbot.keyClicks(card.view.input, QUESTION)

    qtbot.waitUntil(lambda: "protected scheme" in card.body.text(), timeout=5000)
    assert not card.isHidden()
    assert card.fake.calls and card.fake.calls[-1]["question"] == QUESTION
    assert "landlord-2019-03.pdf" in card.sources.text()
    assert "held in a protected scheme" in card.body.text()

    card._link("receipt:1")
    assert card.opened and "landlord" in str(getattr(card.opened[0], "path", ""))

    qtbot.mouseClick(card.more, Qt.MouseButton.LeftButton)
    assert card.continued == [QUESTION]


def test_a_word_gets_no_card(card, qtbot):
    before = len(card.asked)
    qtbot.keyClicks(card.view.input, "barnsley")
    qtbot.wait(200)
    assert len(card.asked) == before and card.isHidden()
    assert not card.answer_timer.isActive()


def test_the_switch_off_means_no_card(card, qtbot, monkeypatch, gui_mainwindow):
    _app, window, _store, _engine = gui_mainwindow
    monkeypatch.setitem(window._settings_overrides, "search_answers", False)
    before = len(card.asked)
    qtbot.keyClicks(card.view.input, QUESTION)
    qtbot.wait(200)
    assert len(card.asked) == before and card.isHidden()


def test_nothing_is_asked_while_chat_is_answering(card, qtbot, gui_mainwindow):
    _app, window, _store, _engine = gui_mainwindow
    window.chat_ctl._ask = object()                      # Chat is mid-answer
    try:
        before = len(card.asked)
        qtbot.keyClicks(card.view.input, QUESTION)
        qtbot.wait(200)
        assert len(card.asked) == before
    finally:
        window.chat_ctl._ask = None


def test_a_changed_box_drops_the_answer_on_its_way(card, qtbot):
    card.fake.hold_at = 0
    qtbot.keyClicks(card.view.input, QUESTION)
    qtbot.waitUntil(lambda: not card.isHidden(), timeout=3000)   # "Reading your files..."
    card.view.input.setText("barnsley")
    card.fake.release()
    qtbot.wait(300)
    assert card.isHidden() and "protected scheme" not in card.body.text()
