r"""The Chat tab as a conversation, pressed like a person would press it.

Layer: L5 (pytest-qt; the real `MainWindow`, the only stand-in is a scripted engine that
follows the engine's contract - see `tests/unit/test_chat_tab_qt.py`, whose harness this
reuses).

The owner's requirement of 2026-09-20 ("the chat has to behave like i am talking to ai chat
like in claude"), on screen: a slim "Thinking..." before the first word, markdown drawn as
it streams, Copy on every answer, Regenerate on the last one, Edit on the last message,
Try again beside a reply that stopped part-way, a short title made after the first answer,
the Web chip and its Allow / Skip, and a box that grows with what is typed.
"""

from __future__ import annotations

import threading
import time

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import Qt                                                # noqa: E402
from PySide6.QtGui import QGuiApplication                                    # noqa: E402

from app.chat.types import ChatTurn, NarrationEvent, SourcesEvent, TokenEvent   # noqa: E402
from app.ui.chat_sessions import session_from_dict, session_to_dict, new_session   # noqa: E402
from app.ui.presenter import chat as presenter                             # noqa: E402
from tests.unit.chat_fakes import LETTER, FakeChatEngine, tokens_of        # noqa: E402
from tests.unit.conftest import gui_pump                                   # noqa: E402
from tests.unit.test_chat_tab_qt import _html, _wait, answered, ask, chat, last_answer   # noqa: E402,F401

pytestmark = pytest.mark.gui


class ConversationalFake(FakeChatEngine):
    """A scripted engine that also takes what the conversational engine takes."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.kwargs: list[dict] = []
        self.titles: list[tuple] = []
        self.title_text = "Deposit rules"

    def ask(self, question, history, emit, should_stop, scope=None, style=None, removed=None,
            variant=0, web=False, web_gate=None):
        self.kwargs.append({"variant": variant, "web": web, "gate": web_gate, "question": question})
        return super().ask(question, history, emit, should_stop, scope=scope, style=style,
                           removed=removed)

    def title(self, question, answer=""):
        self.titles.append((question, answer))
        return self.title_text


def _use(c, engine) -> None:
    c.ctl.engine_factory = lambda: engine
    c.ctl.engine = None
    c.fake = engine


def _script(text, *, kind="chat", turn_kwargs=None, narration=("Thinking...",), chunk=7):
    def script(question):
        events = [NarrationEvent(t) for t in narration] + tokens_of(text, chunk)
        return events, ChatTurn("assistant", text, kind=kind, **(turn_kwargs or {}))
    return script


def _answer_pair(c, question, reply):
    c.fake.script = _script(reply)
    ask(c, question)
    answered(c)


# ---------------------------------------------------------------------------
# Streaming that feels right
# ---------------------------------------------------------------------------

def test_a_slim_thinking_line_shows_before_the_first_word_and_goes_when_it_arrives(chat):
    c = chat
    c.fake.hold_at = 0                                # nothing has been sent yet
    c.fake.script = _script("Hello there, how can I help?", narration=())
    ask(c, "hi")
    _wait(c.qtbot, c.fake.held.is_set)
    bubble = last_answer(c)
    assert bubble.narration.text() == presenter.THINKING_LINE and not bubble.narration.isHidden()
    assert bubble.body.isHidden()                     # no empty box, just the slim line

    c.fake.release()
    answered(c)
    assert bubble.narration.isHidden() and "Hello there" in bubble.body_text()


def test_markdown_is_drawn_and_a_code_block_arrives_with_its_copy_button(chat):
    c = chat
    reply = ("Here is **the short version**:\n\n- first point\n- second point\n\n"
             "```python\nprint('hello')\n```\n\nThat is all.")
    _answer_pair(c, "show me an example", reply)
    bubble = last_answer(c)
    html = _html(bubble)
    assert "<li" in html and "first point" in html                   # a real list, not dashes
    assert "font-weight" in html and "the short version" in html     # bold
    assert bubble.body.code_blocks() == ["print('hello')"]
    block = bubble.body.code_views()[0]
    c.qtbot.mouseClick(block.copy_button, Qt.MouseButton.LeftButton)
    assert QGuiApplication.clipboard().text() == "print('hello')"


def test_the_view_sticks_to_the_bottom_while_streaming_unless_the_reader_scrolled_up(chat):
    c = chat
    long = "A sentence that takes room on the page. " * 400
    c.fake.hold_at = 30
    c.fake.script = _script(long, chunk=300)
    ask(c, "write something long")
    _wait(c.qtbot, c.fake.held.is_set)
    bar = c.view.bubbles.verticalScrollBar()
    _wait(c.qtbot, lambda: bar.maximum() > 0)
    assert c.view.bubbles.follows_newest() and bar.value() >= bar.maximum() - 4

    bar.setValue(0)                                    # the reader scrolls up to re-read
    gui_pump(c.app)
    assert not c.view.bubbles.follows_newest()
    c.fake.release()
    answered(c)
    assert bar.value() == 0, "it stays exactly where the reader left it"


def test_enter_sends_shift_enter_is_a_new_line_and_the_box_grows_to_a_few_lines(chat):
    c = chat
    edit = c.view.box.edit
    one = edit.height()
    edit.setPlainText("word " * 400)                  # wraps onto many lines with no Enter pressed
    gui_pump(c.app)
    assert edit.height() > one, "it grows with what is typed, not only with Enter"
    assert edit.height() <= edit.fontMetrics().lineSpacing() * 5 + 24, "and stops at a few lines"
    edit.clear()
    gui_pump(c.app)
    assert edit.height() == one
    c.qtbot.keyClicks(edit, "first")
    c.qtbot.keyClick(edit, Qt.Key.Key_Return, Qt.KeyboardModifier.ShiftModifier)
    c.qtbot.keyClicks(edit, "second")
    assert edit.toPlainText() == "first\nsecond"
    c.fake.script = _script("ok")
    c.qtbot.keyClick(edit, Qt.Key.Key_Return)
    answered(c)
    assert c.fake.calls[-1]["question"] == "first\nsecond"


# ---------------------------------------------------------------------------
# Message actions
# ---------------------------------------------------------------------------

def test_copy_puts_the_answer_on_the_clipboard_without_the_source_numbers(chat):
    c = chat
    c.fake.script = lambda q: (tokens_of("The deposit is two months rent [1]."),
                               ChatTurn("assistant", "The deposit is two months rent [1].",
                                        receipts=[LETTER]))
    ask(c, "deposit?")
    answered(c)
    bubble = last_answer(c)
    assert not bubble.copy_button.isHidden()
    c.qtbot.mouseClick(bubble.copy_button, Qt.MouseButton.LeftButton)
    assert QGuiApplication.clipboard().text() == "The deposit is two months rent."
    assert bubble.copy_button.text() == presenter.COPIED_LABEL


def test_regenerate_is_offered_on_the_last_answer_only_and_asks_again_a_little_warmer(chat):
    c = chat
    engine = ConversationalFake()
    _use(c, engine)
    _answer_pair(c, "first question", "First answer.")
    _answer_pair(c, "second question", "Second answer.")
    answers = [b for b in c.view.bubbles.bubbles() if hasattr(b, "regenerate_button")]
    assert answers[0].regenerate_button.isHidden() and not answers[1].regenerate_button.isHidden()

    c.fake.script = _script("A different second answer.")
    c.qtbot.mouseClick(answers[1].regenerate_button, Qt.MouseButton.LeftButton)
    answered(c)
    assert [k["question"] for k in engine.kwargs][-1] == "second question"
    assert engine.kwargs[-1]["variant"] == 1
    texts = [b.body_text() for b in c.view.bubbles.bubbles() if hasattr(b, "regenerate_button")]
    assert texts == ["First answer.", "A different second answer."]      # replaced, not stacked
    assert [t.text for t in c.ctl.session.turns] == [
        "first question", "First answer.", "second question", "A different second answer."]


def test_edit_brings_the_last_message_back_into_the_box_and_takes_the_exchange_out(chat):
    c = chat
    _answer_pair(c, "first question", "First answer.")
    _answer_pair(c, "wrong question", "An answer to the wrong thing.")
    users = [b for b in c.view.bubbles.bubbles() if hasattr(b, "edit_button")]
    assert users[0].edit_button.isHidden() and not users[1].edit_button.isHidden()

    c.qtbot.mouseClick(users[1].edit_button, Qt.MouseButton.LeftButton)
    gui_pump(c.app)
    assert c.view.box.edit.toPlainText() == "wrong question"
    assert [t.text for t in c.ctl.session.turns] == ["first question", "First answer."]
    assert len(c.view.bubbles.bubbles()) == 2


def test_a_reply_that_stopped_part_way_keeps_its_words_and_offers_try_again(chat):
    c = chat
    engine = ConversationalFake()
    _use(c, engine)
    note = "Ollama stopped answering part-way. Start it again, then try again."
    engine.script = _script("The deposit rules are these:", turn_kwargs={"partial": True, "notes": [note]})
    ask(c, "explain the deposit rules")
    answered(c)
    bubble = last_answer(c)
    assert "The deposit rules are these:" in bubble.body_text()
    assert bubble.footer.text() == note and not bubble.footer.isHidden()
    assert not bubble.retry_button.isHidden() and bubble.regenerate_button.isHidden()

    engine.script = _script("The deposit rules are these: two months, held in a scheme.")
    c.qtbot.mouseClick(bubble.retry_button, Qt.MouseButton.LeftButton)
    answered(c)
    assert engine.kwargs[-1]["question"] == "explain the deposit rules" and engine.kwargs[-1]["variant"] == 0
    assert len([t for t in c.ctl.session.turns if t.role == "user"]) == 1     # asked again, not twice
    assert "held in a scheme" in last_answer(c).body_text()


def test_a_failure_offers_try_again_too(chat):
    c = chat
    c.fake.script = lambda q: ([], ChatTurn("assistant", "Ollama is not running. Start it, then try again.",
                                            kind="error"))
    ask(c, "hello")
    answered(c)
    assert not last_answer(c).retry_button.isHidden()


def test_a_short_title_replaces_the_first_words_after_the_first_answer(chat):
    c = chat
    engine = ConversationalFake()
    _use(c, engine)
    _answer_pair(c, "What did we agree with the landlord about the deposit and the scheme?", "Two months rent.")
    _wait(c.qtbot, lambda: c.ctl.session.title == "Deposit rules")
    assert engine.titles and engine.titles[0][1] == "Two months rent."
    assert c.view.sessions.list.currentItem().text() == "Deposit rules"
    _wait(c.qtbot, lambda: not c.ctl._saving and not c.ctl._pending)
    assert next(iter(c.backend.records.values()))["title"] == "Deposit rules"
    _answer_pair(c, "and another thing", "More.")
    assert len(engine.titles) == 1, "it is asked for once"


def test_a_renamed_conversation_keeps_the_persons_name(chat):
    c = chat
    engine = ConversationalFake()
    _use(c, engine)
    c.ctl.session.titled = True
    c.ctl.session.title = "My name for it"
    _answer_pair(c, "hello", "Hi.")
    assert engine.titles == [] and c.ctl.session.title == "My name for it"


def test_new_fields_survive_saving_and_reopening():
    session = new_session()
    session.auto_titled, session.web = True, True
    session.turns = [ChatTurn("user", "hi"),
                     ChatTurn("assistant", "Partial", kind="chat", partial=True, model="mistral")]
    back = session_from_dict(session_to_dict(session))
    assert back.auto_titled and back.web
    assert back.turns[1].partial and back.turns[1].model == "mistral" and back.turns[1].kind == "chat"


def test_a_conversation_reopens_with_its_retry_on_a_partial_last_answer(chat):
    c = chat
    c.ctl.session.turns = [ChatTurn("user", "q"), ChatTurn("assistant", "half", kind="chat", partial=True,
                                                            notes=["stopped"])]
    c.view.show_turns(c.ctl.session.turns)
    assert not last_answer(c).retry_button.isHidden()


# ---------------------------------------------------------------------------
# Local sources, and the web
# ---------------------------------------------------------------------------

def test_the_sources_pane_says_it_is_the_local_sources(chat):
    c = chat
    assert c.view.sources.heading.text() == "Local sources"
    assert "your files" in c.view.sources.empty.text()


def test_a_web_source_in_the_pane_is_marked_as_the_web_and_the_heading_says_so(chat):
    from app.chat.types import Receipt
    from app.ui.presenter.chat import receipt_to_result

    c = chat
    web = Receipt(None, "https://en.wikipedia.org/wiki/Personal_Storage_Table", "Personal Storage Table",
                  "A Personal Storage Table is an open proprietary file format.", "Web", None)
    c.view.sources.add(1, LETTER)
    assert c.view.sources.heading.text() == "Local sources"           # no web source: the words are unchanged
    c.view.sources.add(2, web)
    assert c.view.sources.heading.text() == "Local sources and the web"
    assert receipt_to_result(web, 2).text.startswith("From the web: ")
    assert not receipt_to_result(LETTER, 1).text.startswith("From the web")
    c.view.sources.clear()
    assert c.view.sources.heading.text() == "Local sources"


def test_a_web_page_never_lands_on_the_shelf_of_the_persons_documents(chat):
    from app.chat.types import Receipt
    from app.ui.presenter.chat import Shelf

    web = Receipt(None, "https://en.wikipedia.org/wiki/Personal_Storage_Table", "Personal Storage Table",
                  "A Personal Storage Table is an open proprietary file format.", "Web", None)
    shelf = Shelf()
    assert shelf.add_receipt(LETTER) is True and shelf.add_receipt(web) is False
    assert shelf.paths() == [LETTER.path]


def test_source_numbers_are_live_links_while_the_answer_streams_and_the_pane_follows_the_final_turn(chat):
    c = chat
    other = LETTER.__class__(file_id=9, path="C:/mail/other.pdf", name="other.pdf", quote="Other words here.",
                             locator="page 1", chunk_id=9)

    def script(q):
        text = "The deposit is held in a scheme [1]. Other words here [2]."
        events = [SourcesEvent((LETTER, other)), *tokens_of(text, 6)]
        # the checked answer dropped the first claim: what was [2] is now [1]
        return events, ChatTurn("assistant", "Other words here [1].", receipts=[other])

    c.fake.script = script
    ask(c, "deposit?")
    answered(c)
    bubble = last_answer(c)
    assert "leasha-receipt:1" in _html(bubble) and "leasha-receipt:2" not in _html(bubble)
    assert c.view.sources.numbers() == [1]
    assert c.view.sources.receipt(1).path == other.path             # rebuilt from the finished turn


def test_the_web_chip_is_absent_until_settings_allow_the_web_and_off_until_turned_on(chat):
    c = chat
    engine = ConversationalFake()
    _use(c, engine)
    assert c.view.box.web.isHidden(), "off by default: no chip at all"
    c.ctl._settings_changed({"CHAT_WEB_ENABLED": True})
    assert not c.view.box.web.isHidden() and not c.view.box.web.isChecked()
    _answer_pair(c, "hello", "Hi.")
    assert engine.kwargs[-1]["web"] is False                          # chip off: nothing may leave

    c.view.box.web.click()
    assert c.ctl.session.web is True
    _answer_pair(c, "what is a pst file", "From the web: ...")
    assert engine.kwargs[-1]["web"] is True
    _wait(c.qtbot, lambda: not c.ctl._saving and not c.ctl._pending)
    assert next(iter(c.backend.records.values()))["web"] is True      # remembered with the conversation

    c.ctl._settings_changed({"CHAT_WEB_ENABLED": False})
    assert c.view.box.web.isHidden()
    _answer_pair(c, "another", "Hi.")
    assert engine.kwargs[-1]["web"] is False, "Settings off means off, whatever the chip says"


def test_ask_first_shows_the_exact_phrase_and_waits_for_allow_or_skip(chat):
    c = chat
    c.ctl._settings_changed({"CHAT_WEB_ENABLED": True})
    c.view.box.web.setChecked(True)

    class Waiting(ConversationalFake):
        def ask(self, question, history, emit, should_stop, **kw):
            self.kwargs.append({"question": question, "web": kw.get("web"), "variant": 0})
            self.answer = kw["web_gate"]("pst file format")
            return ChatTurn("assistant", "Done.", kind="chat")

    engine = Waiting()
    _use(c, engine)
    engine.script = lambda q: ([], ChatTurn("assistant", "Done."))
    ask(c, "what is a pst file")
    bubble_ready = lambda: c.view.bubbles.bubbles() and hasattr(c.view.bubbles.bubbles()[-1], "web_prompt") \
        and not c.view.bubbles.bubbles()[-1].web_prompt.isHidden()
    _wait(c.qtbot, bubble_ready)
    bubble = last_answer(c)
    assert 'Search the web for: "pst file format"?' == bubble.web_question.text()
    assert c.ctl._ask is not None, "the engine is still waiting: nothing has been sent"
    c.qtbot.mouseClick(bubble.web_allow, Qt.MouseButton.LeftButton)
    answered(c)
    assert engine.answer is True and bubble.web_prompt.isHidden()

    ask(c, "and again")
    _wait(c.qtbot, bubble_ready)
    c.qtbot.mouseClick(last_answer(c).web_skip, Qt.MouseButton.LeftButton)
    answered(c)
    assert engine.answer is False


def test_stop_lets_go_of_a_search_that_is_waiting_for_an_answer(chat):
    c = chat
    c.ctl._settings_changed({"CHAT_WEB_ENABLED": True})
    c.view.box.web.setChecked(True)

    class Waiting(ConversationalFake):
        def ask(self, question, history, emit, should_stop, **kw):
            self.answer = kw["web_gate"]("some phrase")
            return ChatTurn("assistant", "", kind="answer")

    engine = Waiting()
    _use(c, engine)
    ask(c, "what is a pst file")
    _wait(c.qtbot, lambda: c.view.bubbles.bubbles() and not last_answer(c).web_prompt.isHidden())
    c.qtbot.mouseClick(c.view.box.stop_button, Qt.MouseButton.LeftButton)
    answered(c)
    assert engine.answer is False


def test_a_web_source_opens_in_the_browser_and_a_file_opens_the_usual_way(chat, monkeypatch):
    from types import SimpleNamespace

    from PySide6.QtGui import QDesktopServices

    c = chat
    urls = []
    monkeypatch.setattr(QDesktopServices, "openUrl", lambda url: urls.append(url.toString()) or True)
    c.view._opened(SimpleNamespace(path="https://en.wikipedia.org/wiki/Personal_Storage_Table"))
    assert urls == ["https://en.wikipedia.org/wiki/Personal_Storage_Table"] and not c.opened
    c.view._opened(SimpleNamespace(path="C:/mail/letter.pdf"))
    assert [row.path for row in c.opened] == ["C:/mail/letter.pdf"] and len(urls) == 1


def test_the_settings_page_says_what_leaves_the_computer_and_shows_the_web_section_in_every_mode(chat):
    from app.ui.widgets.chat_box import WEB_INTRO

    box = chat.window.settings_view.chat_box
    for manual in (True, False):
        box.set_manual(manual)
        assert not box.web_part.isHidden() or box.isHidden() is False
        assert box.manual_part.isHidden() == (not manual)
    assert "never a file name" in WEB_INTRO and "one short search phrase" in WEB_INTRO
    for key in ("CHAT_WEB_ENABLED", "CHAT_WEB_ASK_FIRST", "CHAT_WEB_SHOW_QUERY", "CHAT_WEB_PROVIDER",
                "CHAT_WEB_SEARXNG_URL", "CHAT_WEB_BRAVE_KEY"):
        assert box.controls[key].toolTip(), key
    assert box.controls["CHAT_WEB_ENABLED"].isChecked() is False           # off by default
    assert box.controls["CHAT_WEB_ASK_FIRST"].isChecked() is True          # ask first by default
    key_box = box.controls["CHAT_WEB_BRAVE_KEY"]
    assert key_box.echoMode() != key_box.EchoMode.Normal                  # a key is not shown on screen


def test_no_web_service_is_offered_without_saying_what_was_seen_of_it(chat):
    """Live-probed 2026-09-20: only Wikipedia answered; DuckDuckGo asked for proof of a person;
    SearXNG and Brave could not be tried without the person's own server / key. The choice says
    so, and what is stored stays the bare word."""
    combo = chat.window.settings_view.chat_box.controls["CHAT_WEB_PROVIDER"]
    shown = {combo.itemData(i): combo.itemText(i) for i in range(combo.count())}
    assert set(shown) == {"auto", "duckduckgo", "wikipedia", "searxng", "brave"}
    assert shown["auto"] == "auto"
    # 2026-09-20: not "experimental" - that word is on the chat tab's banner deny-list
    # (`test_no_fixed_string_in_the_tab_is_a_warning_banner`). The note still says what was seen.
    assert "not confirmed working" in shown["duckduckgo"] and "proof of a person" in shown["duckduckgo"]
    assert "unverified" in shown["searxng"] and "unverified" in shown["brave"]
    assert "works" in shown["wikipedia"] and "unverified" not in shown["wikipedia"]
    assert all(text.startswith(key) for key, text in shown.items())
    combo.setCurrentIndex(combo.findData("duckduckgo"))
    assert combo.currentData() == "duckduckgo"


def test_the_new_settings_have_controls_that_emit_their_key(chat):
    box = chat.window.settings_view.chat_box
    seen = []
    box.changed.connect(seen.append)
    box.controls["CHAT_WEB_ENABLED"].setChecked(True)
    box.controls["CHAT_STYLE_NOTE"].setText("Keep answers short.")
    box.controls["CHAT_STYLE_NOTE"].editingFinished.emit()
    box.controls["CHAT_CONTEXT_TOKENS"].setValue(4096)
    assert {"CHAT_WEB_ENABLED": True} in seen
    assert {"CHAT_STYLE_NOTE": "Keep answers short."} in seen
    assert {"CHAT_CONTEXT_TOKENS": 4096} in seen
    # ...and the window writes each one to `.env` (captured here, never written): the setting is used
    assert {"CHAT_WEB_ENABLED": True} in chat.written and {"CHAT_CONTEXT_TOKENS": 4096} in chat.written
    assert chat.errors == []


# ---------------------------------------------------------------------------
# Stop always ends a turn, even when the model is still loading and cannot be interrupted
# ---------------------------------------------------------------------------

def test_stop_hands_the_box_back_even_if_the_engine_never_notices(chat, monkeypatch):
    """A model that is still loading sends nothing to stop between. The bubble closes at once,
    and after a short grace the box is free again; the orphaned worker's late words are ignored
    and the next question is answered by a fresh engine."""
    from app.ui.controllers import chat_controller

    monkeypatch.setattr(chat_controller, "STOP_GRACE_MS", 150)
    c = chat
    stuck = threading.Event()

    class Deaf(ConversationalFake):
        def ask(self, question, history, emit, should_stop, **kw):
            stuck.wait(timeout=20)                       # ignores should_stop entirely
            emit(TokenEvent("LATE WORDS FROM THE ORPHAN"))
            return ChatTurn("assistant", "LATE WORDS FROM THE ORPHAN", kind="chat")

    deaf = Deaf()
    _use(c, deaf)
    ask(c, "first question")
    _wait(c.qtbot, lambda: c.ctl._ask is not None)
    c.qtbot.mouseClick(c.view.box.stop_button, Qt.MouseButton.LeftButton)
    assert c.view.box.stop_button.text() == "Stopping..." and not c.view.box.stop_button.isEnabled()
    _wait(c.qtbot, lambda: c.ctl._ask is None, timeout=3000)          # handed back, engine still stuck
    assert c.view.box.send_button.isVisibleTo(c.view) and c.view.box.stop_button.text() == "Stop"

    fresh = ConversationalFake()
    c.ctl.engine_factory = lambda: fresh
    fresh.script = _script("A clean answer to the next question.")
    ask(c, "second question")
    answered(c)
    stuck.set()                                                        # the orphan finally wakes up ...
    time.sleep(0.3)
    gui_pump(c.app)
    texts = [b.body_text() for b in c.view.bubbles.bubbles() if hasattr(b, "regenerate_button")]
    assert texts[-1] == "A clean answer to the next question." and all("LATE WORDS" not in t for t in texts)
    assert c.ctl.engine is fresh and c.view.box.send_button.isVisibleTo(c.view)     # ... and changes nothing
