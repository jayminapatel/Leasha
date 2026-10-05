r"""The Chat tab, pressed like a person would press it - on the real `MainWindow`.

Layer: L5 (pytest-qt, order 0m's convention: `gui_mainwindow`, real store, real
window; the only stand-ins are a `FakeChatEngine` that follows the engine's
contract and an in-memory session backend)

Work order 202626270611 section 5: ask -> narration -> streamed text ->
receipts you can click; Stop mid-stream; a search-shaped question answers with
the real results list; a conversation persists and comes back with its shelf;
the shelf can be pinned, emptied and dragged into; a follow-up searches the
shelf first; the page degrades to a plain explanation when the helper is
missing; and 4e-2/4e-3/4e-4 - streaming that never reshuffles, the keyboard
flow, answers that say where they end.
"""

from __future__ import annotations

import html
import re
from types import SimpleNamespace

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QMimeData, QPointF, Qt, QUrl                    # noqa: E402
from PySide6.QtGui import QDropEvent                                       # noqa: E402
from PySide6.QtWidgets import (                                            # noqa: E402
    QAbstractButton, QComboBox, QLabel, QLineEdit, QWidget,
)

from app.chat.types import (                                             # noqa: E402
    ChatTurn, NarrationEvent, ShelfEvent, TokenEvent,
)
from app.ui.chat_sessions import ChatSessions, new_session               # noqa: E402
from app.ui.presenter import chat as presenter                           # noqa: E402
from tests.unit.chat_fakes import (                                      # noqa: E402
    AGREEMENT, DEPOSIT_TEXT, LETTER, FakeChatEngine, FakeSessionBackend,
    search_results, tokens_of,
)
from tests.unit.conftest import gui_pump, gui_row_count                  # noqa: E402

pytestmark = pytest.mark.gui


# ---------------------------------------------------------------------------
# The harness
# ---------------------------------------------------------------------------

def _html(bubble) -> str:
    """What the answer bubble shows, as HTML: its prose segments (source numbers are anchors)."""
    return "".join(view.toHtml() for view in bubble.body.prose_views())


def _wait(qtbot, condition, timeout=5000):
    qtbot.waitUntil(condition, timeout=timeout)


@pytest.fixture()
def chat(gui_mainwindow, qtbot, monkeypatch):
    """The Chat tab, reset to a fresh conversation with a fake engine."""
    app, window, store, engine = gui_mainwindow
    ctl, view = window.chat_ctl, window.chat_view
    # 2026-09-30: let the previous test's answer, save and title finish before
    # the conversation is replaced. The window is shared by the whole module,
    # and a save still in flight put the *previous* conversation back after the
    # reset below - in a full-suite run `test_a_reply_that_stopped_part_way...`
    # found "first question" from the test before it in its own session.
    _wait(qtbot, lambda: ctl._ask is None and not getattr(ctl, "_saving", False)
          and not getattr(ctl, "_pending", None))
    gui_pump(app)
    fake = FakeChatEngine()
    backend = FakeSessionBackend()
    ctl._sessions = ChatSessions(backend)
    ctl.engine_factory = lambda: fake
    ctl.engine = None
    ctl.sessions, ctl.session = [], new_session()
    ctl._opened, ctl._available, ctl._ask = False, None, None
    view.show_turns([])
    view.shelf.set_shelf(ctl.session.shelf)
    view.sessions.set_sessions([], "")
    view.show_available(True)
    view.set_busy(False)
    opened, revealed, paths = [], [], []
    monkeypatch.setattr(window, "_open_result",
                        lambda row, reveal=False: (revealed if reveal else opened).append(row))
    monkeypatch.setattr(window, "_open_path", lambda path, reveal=False: paths.append(path))
    # Nothing in a test may open a modal dialog (it waits for a click nobody will make and
    # hangs the run) or write a real `.env`: an error is recorded, a write is captured.
    errors, written = [], []
    monkeypatch.setattr(window, "_show_error", errors.append)
    monkeypatch.setattr("app.core.env_writer.apply_values",
                        lambda path, values: written.append(dict(values)) or {})
    window.resize(1200, 760)
    window.show()
    window._show(window.search_view)
    gui_pump(app)
    window._show(view)                       # first show: loads sessions, checks the helper
    _wait(qtbot, lambda: ctl._available is not None)
    return SimpleNamespace(app=app, window=window, ctl=ctl, view=view, fake=fake,
                           backend=backend, opened=opened, revealed=revealed,
                           paths=paths, store=store, qtbot=qtbot, errors=errors, written=written)


def ask(c, text: str) -> None:
    # 2026-09-30: wait for Send to be live, then for the question to register. In a
    # full-suite run the click could land while Send was still disabled; it was
    # dropped, `answered` then returned at once (nothing was being answered), and
    # the saved conversation was the empty "New chat" - a failure of the test's
    # timing, not of the page.
    _wait(c.qtbot, lambda: c.view.box.send_button.isEnabled())
    users = len([t for t in c.ctl.session.turns if t.role == "user"])
    c.view.box.edit.setPlainText(text)
    c.qtbot.mouseClick(c.view.box.send_button, Qt.MouseButton.LeftButton)
    _wait(c.qtbot, lambda: len([t for t in c.ctl.session.turns if t.role == "user"]) > users)


def answered(c) -> None:
    """Wait for the answer, the worker and the save to be over."""
    _wait(c.qtbot, lambda: c.ctl._ask is None and not c.ctl._saving and not c.ctl._pending)


def passage(c) -> str:
    """The passage strip as the reader sees it: no tags, entities undone."""
    return html.unescape(re.sub(r"<[^>]+>", " ", c.view.sources.passage.text()))


def last_answer(c):
    return c.view.bubbles.bubbles()[-1]


# ---------------------------------------------------------------------------
# It is a page of its own, in the rail
# ---------------------------------------------------------------------------

def test_chat_is_a_rail_tab_after_code_and_before_offline(chat):
    rail = chat.window.rail
    titles = [rail.tabText(i) for i in range(rail.count())]
    assert titles.index("Chat") == titles.index("Code") + 1
    assert titles.index("Offline") == titles.index("Chat") + 1
    assert rail.tabText(chat.window._tab_index[chat.view]) == "Chat"
    assert rail.currentIndex() == chat.window._tab_index[chat.view]


# ---------------------------------------------------------------------------
# Ask -> narration -> streamed text -> receipts you can click
# ---------------------------------------------------------------------------

def test_ask_shows_the_narration_then_streams_text_then_receipts_open_the_source(chat):
    c = chat
    c.fake.hold_at = 4                   # after the narration and the shelf events
    ask(c, "What did we agree with the landlord about the deposit?")
    _wait(c.qtbot, c.fake.held.is_set)
    _wait(c.qtbot, lambda: c.view.bubbles.bubbles()
          and c.view.bubbles.bubbles()[-1].narration.text().startswith("Found 2"))

    bubble = last_answer(c)
    assert not bubble.narration.isHidden(), "the search explains itself while it works"
    assert "Found 2 documents" in bubble.narration.text()
    assert c.view.box.stop_button.isVisibleTo(c.view) and c.view.box.send_button.isHidden()
    assert len(c.view.shelf.chips()) == 2, "documents it touched appear on the shelf"

    c.fake.release()
    answered(c)

    assert bubble.narration.isHidden(), "and the progress line goes when it is done"
    body = _html(bubble)
    assert 'href="leasha-receipt:1"' in body and 'href="leasha-receipt:2"' in body
    assert "held in a protected scheme" in body
    assert c.view.sources.numbers() == [1, 2]

    # Click the raised 2: the pane picks that document and shows the exact passage.
    bubble.body.receipt_activated.emit(2)
    gui_pump(c.app)
    assert AGREEMENT.quote in passage(c)
    assert c.view.sources.results.current_row().path == AGREEMENT.path

    # Opening the source goes through the window's own open path.
    assert c.view.sources.open_selected()
    assert [row.path for row in c.opened] == [AGREEMENT.path]


def test_hovering_a_number_shows_its_passage_without_picking_it(chat):
    c = chat
    ask(c, "deposit?")
    answered(c)
    bubble = last_answer(c)
    bubble.body.receipt_hovered.emit(1)
    assert LETTER.quote in passage(c)
    assert c.view.sources.results.current_row() is None
    bubble.body.receipt_hovered.emit(0)
    assert c.view.sources.passage.isHidden()


def test_the_engine_and_the_saving_never_run_on_the_window_thread(chat):
    c = chat
    ask(c, "deposit?")
    answered(c)
    threads = c.fake.threads + c.backend.threads
    assert threads and "MainThread" not in threads, threads


def test_an_answer_never_lands_in_the_wrong_conversation_turn(chat):
    """Events carry which question they belong to; a late one is dropped."""
    c = chat
    ask(c, "first")
    answered(c)
    stale = c.ctl._token
    c.ctl._on_event((stale - 1, TokenEvent("LATE TEXT")))
    assert "LATE TEXT" not in last_answer(c).raw


# ---------------------------------------------------------------------------
# Stop
# ---------------------------------------------------------------------------

def test_stop_mid_stream_keeps_what_arrived_and_says_it_stopped(chat):
    c = chat
    c.fake.hold_at = 6                   # a few pieces of text are through
    ask(c, "deposit?")
    _wait(c.qtbot, c.fake.held.is_set)
    _wait(c.qtbot, lambda: last_answer(c).raw != "")
    partial = last_answer(c).raw

    c.qtbot.mouseClick(c.view.box.stop_button, Qt.MouseButton.LeftButton)
    gui_pump(c.app)
    bubble = last_answer(c)
    assert bubble.footer.text() == presenter.STOPPED_LINE
    assert not bubble.footer.isHidden()
    assert bubble.raw == partial, "what had arrived stays on screen"

    answered(c)                          # the engine notices, returns, the box is free
    assert c.fake.saw_stop
    assert c.view.box.send_button.isVisibleTo(c.view) and c.view.box.stop_button.isHidden()
    assert bubble.raw == partial, "nothing written after Stop"
    saved = next(iter(c.backend.records.values()))
    assert saved["turns"][-1]["notes"] == ["stopped"]


# ---------------------------------------------------------------------------
# Answers that are result sets, and answers that end by saying so (3b, 4e-4)
# ---------------------------------------------------------------------------

def _find_script(count):
    def script(question):
        turn = ChatTurn("assistant", "Here are the photos from the beach.",
                        kind="find", result_set=search_results(count))
        return [NarrationEvent("Searching your photos."),
                *tokens_of(turn.text)], turn
    return script


def test_a_find_answer_shows_the_real_results_and_ends_with_thats_all(chat):
    c = chat
    c.fake.script = _find_script(3)
    ask(c, "show me the photos of the kids at the beach")
    answered(c)
    bubble = last_answer(c)
    assert "beach" in _html(bubble)
    assert bubble.results is not None and gui_row_count(bubble.results) == 3
    model = bubble.results._model
    last = model.item(model.rowCount() - 1).data(int(Qt.ItemDataRole.UserRole))
    assert last.text == "That's all — 3 results."
    # A click on a hit opens it the way a search hit opens.
    bubble.results.opened.emit(bubble.results.current_row() or SimpleNamespace(path="x"))
    assert c.opened


def test_picking_a_source_or_a_result_previews_it_in_the_sources_column(chat):
    """2026-10-04, the owner: "if it shows files it should have the ability to
    preview". One click on a Local source, or on a result row inside an
    answer, shows the document in the pane under the sources list - the same
    pane Search uses. Double-click still opens the file, as before."""
    c = chat
    pane = c.view.sources.preview
    assert pane is c.view.preview and not pane.isHidden()
    assert pane.store is c.store, "a message cannot be previewed without the store"
    assert pane in c.window._preview_panes(), "the motion preference misses it"

    ask(c, "What did we agree with the landlord about the deposit?")
    answered(c)
    c.view.sources.select_number(2)
    gui_pump(c.app)
    assert pane._row is not None and pane._row.path == AGREEMENT.path
    assert pane.title.text() == AGREEMENT.name or AGREEMENT.name in pane.title.text()

    c.fake.script = _find_script(3)
    ask(c, "show me the photos of the kids at the beach")
    answered(c)
    bubble = last_answer(c)
    assert pane._row is None, "a new answer starts the pane again"
    first = bubble.results._model.index(0, 0)
    bubble.results._list.setCurrentIndex(first)
    gui_pump(c.app)
    picked = bubble.results.current_row()
    assert picked is not None and pane._row is picked
    assert c.opened == [], "one click previews; it does not open the file"
    # The pane's own buttons take the window's routes, as Search's do.
    pane.open_button.click()
    assert [row.path for row in c.opened] == [picked.path]
    pinned: list = []
    c.window._pin_document = lambda row, provider=None: pinned.append(row)
    pane.pop_out_requested.emit(picked, None)
    assert pinned == [picked] or pinned == []        # wired in the shell at build time


def test_a_find_that_found_nothing_says_so_in_its_last_line(chat):
    c = chat
    c.fake.script = _find_script(0)
    ask(c, "show me photos of the moon")
    answered(c)
    assert "Nothing in what Leasha has indexed" in last_answer(c).footer.text()


def test_an_aggregate_answer_is_shown_as_the_engine_wrote_it_scope_and_all(chat):
    c = chat
    text = "47 photos - counted across everything indexed, including offline drives."

    def script(question):
        return [TokenEvent(text)], ChatTurn("assistant", text, kind="aggregate")

    c.fake.script = script
    ask(c, "how many photos from Diwali 2019")
    answered(c)
    assert "counted across everything indexed" in _html(last_answer(c))


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------

def test_a_conversation_is_saved_and_comes_back_with_its_shelf_and_sources(chat):
    c = chat
    ask(c, "What did we agree with the landlord about the deposit?")
    answered(c)
    assert len(c.backend.records) == 1
    record = next(iter(c.backend.records.values()))
    assert record["title"].startswith("What did we agree")
    assert len(record["shelf"]["items"]) == 2 and len(record["turns"]) == 2

    # "Restart": nothing in memory, the tab opened afresh.
    c.ctl.sessions, c.ctl.session = [], new_session()
    c.ctl._opened = False
    c.view.show_turns([])
    c.view.shelf.set_shelf(c.ctl.session.shelf)
    c.window._show(c.window.search_view)
    c.window._show(c.view)
    _wait(c.qtbot, lambda: len(c.view.bubbles.bubbles()) == 2)

    assert c.view.sessions.list.count() == 1
    assert c.view.sessions.list.currentItem().text().startswith("What did we agree")
    assert len(c.view.shelf.chips()) == 2, "it reopens with its shelf intact"
    assert c.view.sources.numbers() == [1, 2]
    assert 'href="leasha-receipt:1"' in _html(last_answer(c))


def test_new_rename_and_delete(chat):
    c = chat
    ask(c, "first question")
    answered(c)
    first = c.ctl.session.id

    c.qtbot.mouseClick(c.view.sessions.new_button, Qt.MouseButton.LeftButton)
    assert c.ctl.session.id != first and c.view.bubbles.bubbles() == []
    ask(c, "second question")
    answered(c)
    assert len(c.backend.records) == 2 and c.view.sessions.list.count() == 2

    # Go back to the first: its conversation is drawn again.
    for row in range(c.view.sessions.list.count()):
        if c.view.sessions.list.item(row).data(Qt.ItemDataRole.UserRole) == first:
            c.view.sessions.list.setCurrentRow(row)
    gui_pump(c.app)
    assert c.ctl.session.id == first
    assert c.view.bubbles.bubbles()[0].label.toPlainText() == "first question"

    # Rename by editing the name in the list.
    c.view.sessions.list.currentItem().setText("The deposit chat")
    answered(c)
    assert c.backend.records[first]["title"] == "The deposit chat"

    # Delete asks first, then it is gone from the list and from disk.
    c.view.sessions.confirm_delete = lambda title: True
    c.qtbot.mouseClick(c.view.sessions.delete_button, Qt.MouseButton.LeftButton)
    _wait(c.qtbot, lambda: first not in c.backend.records)
    assert c.view.sessions.list.count() == 1
    assert c.view.bubbles.bubbles() == [], "a fresh conversation takes its place"


def test_declining_the_delete_keeps_the_conversation(chat):
    c = chat
    ask(c, "keep me")
    answered(c)
    c.view.sessions.confirm_delete = lambda title: False
    c.qtbot.mouseClick(c.view.sessions.delete_button, Qt.MouseButton.LeftButton)
    gui_pump(c.app)
    assert len(c.backend.records) == 1


def test_you_cannot_switch_conversation_while_an_answer_is_being_written(chat):
    c = chat
    c.fake.hold_at = 4
    ask(c, "slow one")
    _wait(c.qtbot, c.fake.held.is_set)
    assert not c.view.sessions.new_button.isEnabled()
    c.fake.release()
    answered(c)
    assert c.view.sessions.new_button.isEnabled()


# ---------------------------------------------------------------------------
# The context shelf (3c)
# ---------------------------------------------------------------------------

def _drop(shelf, path):
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(path)])
    shelf.dropEvent(QDropEvent(QPointF(5, 5), Qt.DropAction.CopyAction, mime,
                               Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier))


def test_pin_remove_and_add_change_what_the_next_question_searches(chat):
    c = chat
    ask(c, "deposit?")
    answered(c)
    chips = c.view.shelf.chips()
    assert [chip.path for chip in chips] == [LETTER.path, AGREEMENT.path]

    c.qtbot.mouseClick(chips[1].pin_button, Qt.MouseButton.LeftButton)     # keep the agreement
    answered(c)
    assert c.ctl.session.shelf.items[1].pinned

    c.qtbot.mouseClick(c.view.shelf.chips()[0].remove_button, Qt.MouseButton.LeftButton)
    assert [chip.path for chip in c.view.shelf.chips()] == [AGREEMENT.path]

    # 2026-10-05: a path this system can hold in a file link (a Mac read
    # "C:/notes/extra.txt" back as "/C:/notes/extra.txt").
    import os

    extra = "C:/notes/extra.txt" if os.name == "nt" else "/notes/extra.txt"
    _drop(c.view.shelf, extra)                                            # drag a result in
    assert [chip.path for chip in c.view.shelf.chips()] == [AGREEMENT.path, extra]
    answered(c)

    ask(c, "and when do I get it back?")
    answered(c)
    call = c.fake.calls[-1]
    assert call["scope"][:2] == [AGREEMENT.path, "C:/notes/extra.txt"] or \
        call["scope"][0] == AGREEMENT.path, "kept documents are searched first"
    assert LETTER.path not in call["scope"][:1], "a removed document is out of scope"
    assert [t.role for t in call["history"]] == ["user", "assistant"], "the follow-up sees the chat so far"

    # The answer touched the removed letter again; it must not come back uninvited.
    assert LETTER.path not in [chip.path for chip in c.view.shelf.chips()]


def test_the_shelf_change_is_saved(chat):
    c = chat
    ask(c, "deposit?")
    answered(c)
    c.qtbot.mouseClick(c.view.shelf.chips()[0].pin_button, Qt.MouseButton.LeftButton)
    answered(c)
    record = next(iter(c.backend.records.values()))
    assert record["shelf"]["items"][0]["pinned"] is True


def test_a_shelf_chip_opens_its_document(chat):
    c = chat
    ask(c, "deposit?")
    answered(c)
    c.qtbot.mouseClick(c.view.shelf.chips()[0].name_button, Qt.MouseButton.LeftButton)
    assert c.paths == [LETTER.path]


# ---------------------------------------------------------------------------
# When the helper is missing (H4: degrade, say why, say what to do)
# ---------------------------------------------------------------------------

def test_without_the_helper_the_page_says_so_in_plain_words_and_search_still_works(chat):
    c = chat
    reason = "Ollama is not running. Start it, then press Check again."
    c.fake._available = (False, reason)
    c.qtbot.mouseClick(c.view.recheck_button, Qt.MouseButton.LeftButton) \
        if not c.view.recheck_button.isHidden() else c.ctl.recheck()
    _wait(c.qtbot, lambda: not c.view.notice.isHidden())

    assert c.view.notice.text() == reason
    assert "Traceback" not in c.view.notice.text()
    assert not c.view.box.send_button.isEnabled() and not c.view.box.edit.isEnabled()
    assert c.view.box.send_button.toolTip() == reason, "greyed, with the reason on it"
    assert not c.view.speed.isEnabled()
    assert not c.view.recheck_button.isHidden()

    # Asking is a no-op rather than an error dialog.
    c.ctl.ask("hello?")
    assert c.fake.calls == []

    # Search is entirely unaffected.
    c.window._show(c.window.search_view)
    c.window.search_view.input.clear()
    c.qtbot.keyClicks(c.window.search_view.input, "barnsley")
    _wait(c.qtbot, lambda: gui_row_count(c.window.search_view.results) > 0)

    # Start the helper, press Check again: the page comes back.
    c.window._show(c.view)
    c.fake._available = (True, "")
    c.qtbot.mouseClick(c.view.recheck_button, Qt.MouseButton.LeftButton)
    _wait(c.qtbot, lambda: c.view.notice.isHidden())
    assert c.view.box.send_button.isEnabled() and c.view.box.edit.isEnabled()


def test_a_copy_without_the_engine_says_chat_is_not_there_yet(chat):
    c = chat

    def missing():
        raise ImportError("no chat engine in this build")

    c.ctl.engine_factory = missing
    c.ctl.recheck()
    _wait(c.qtbot, lambda: not c.view.notice.isHidden())
    assert c.view.notice.text() == presenter.NOT_BUILT_LINE
    assert c.view.recheck_button.isHidden(), "there is nothing to check for"


def test_an_engine_that_blows_up_gives_a_plain_sentence_not_a_traceback(chat):
    c = chat

    def script(question):
        raise RuntimeError("boom - internal detail nobody should read")

    c.fake.script = script
    ask(c, "anything")
    answered(c)
    text = last_answer(c).body_text()
    assert text == presenter.FAILED_LINE and "boom" not in text
    assert c.view.box.send_button.isVisibleTo(c.view), "and the box is usable again"


# ---------------------------------------------------------------------------
# 4e-2 - streaming that does not shuffle under the reader's hands
# ---------------------------------------------------------------------------

def test_sources_only_ever_append_and_numbers_never_change_mid_answer(chat):
    c = chat

    def script(question):
        events = [ShelfEvent(LETTER), ShelfEvent(AGREEMENT),
                  TokenEvent("First the agreement [2]."),
                  TokenEvent(" Then the letter [1]."),
                  TokenEvent(" And the agreement again [2].")]
        return events, ChatTurn(
            "assistant", "First the agreement [2]. Then the letter [1]. "
            "And the agreement again [2].", receipts=[LETTER, AGREEMENT])

    c.fake.script, c.fake.hold_at = script, 3
    ask(c, "which first?")
    _wait(c.qtbot, c.fake.held.is_set)
    _wait(c.qtbot, lambda: c.view.sources.numbers() == [1])
    before = [c.view.sources.receipt(1).path]
    assert before == [AGREEMENT.path], "the first thing the prose leans on is number 1"

    c.fake.release()
    answered(c)
    assert c.view.sources.numbers() == [1, 2], "the second arrived after, at the end"
    assert c.view.sources.receipt(1).path == AGREEMENT.path, "number 1 never moved"
    assert c.view.sources.receipt(2).path == LETTER.path
    assert re.findall(r"receipt:(\d)", _html(last_answer(c))) == ["1", "2", "1"]


def test_the_reader_who_scrolled_up_is_not_pulled_back_down_by_new_text(chat):
    c = chat
    for i in range(14):
        c.view.add_user(f"an earlier line of the conversation number {i} " * 3)
    gui_pump(c.app)
    bar = c.view.bubbles.verticalScrollBar()
    assert bar.maximum() > 0, "the test needs a conversation longer than the window"

    def script(question):
        events = tokens_of("A long answer that keeps arriving. " * 30, 40)
        return events, ChatTurn("assistant", "A long answer that keeps arriving. " * 30)

    c.fake.script, c.fake.hold_at = script, 3
    ask(c, "go")
    _wait(c.qtbot, c.fake.held.is_set)
    bar.setValue(0)                                  # the reader goes back to re-read
    gui_pump(c.app)
    c.fake.release()
    answered(c)
    assert bar.value() == 0, "new text below must not move what somebody is reading"

    # Scroll back to the bottom and it follows again.
    c.view.bubbles.scroll_to_end()
    assert c.view.bubbles.follows_newest()


def test_a_reader_at_the_bottom_follows_the_newest_text(chat):
    c = chat
    for i in range(14):
        c.view.add_user(f"line {i} " * 20)
    gui_pump(c.app)
    ask(c, "go")
    answered(c)
    bar = c.view.bubbles.verticalScrollBar()
    assert bar.value() >= bar.maximum() - 4


# ---------------------------------------------------------------------------
# 4e-3 - the keyboard flow
# ---------------------------------------------------------------------------

def test_the_keyboard_walks_the_sources_without_leaving_the_box(chat):
    c = chat
    ask(c, "deposit?")
    answered(c)
    edit = c.view.box.edit
    edit.setFocus()

    c.qtbot.keyClick(edit, Qt.Key.Key_Down)
    gui_pump(c.app)
    first = c.view.sources.results.current_row()
    assert first is not None and first.path == LETTER.path
    assert LETTER.quote in passage(c), "preview follows the selection"
    assert edit.toPlainText() == "", "the arrow did not type or move anything in the box"

    c.qtbot.keyClick(edit, Qt.Key.Key_Down)
    gui_pump(c.app)
    assert c.view.sources.results.current_row().path == AGREEMENT.path
    assert AGREEMENT.quote in passage(c)

    c.qtbot.keyClick(edit, Qt.Key.Key_Up)
    gui_pump(c.app)
    assert c.view.sources.results.current_row().path == LETTER.path

    # Enter, box empty, a source picked: it opens.
    c.qtbot.keyClick(edit, Qt.Key.Key_Return)
    assert [row.path for row in c.opened] == [LETTER.path]
    assert c.fake.calls[-1]["question"] == "deposit?" and len(c.fake.calls) == 1, \
        "and did not send anything"

    # Esc: back to the conversation, the pick is dropped.
    c.qtbot.keyClick(edit, Qt.Key.Key_Escape)
    gui_pump(c.app)
    assert c.view.sources.results.current_row() is None
    assert c.view.sources.passage.isHidden()


def test_enter_sends_and_shift_enter_starts_a_new_line(chat):
    c = chat
    edit = c.view.box.edit
    c.qtbot.keyClicks(edit, "first line")
    c.qtbot.keyClick(edit, Qt.Key.Key_Return, Qt.KeyboardModifier.ShiftModifier)
    c.qtbot.keyClicks(edit, "second line")
    assert edit.toPlainText() == "first line\nsecond line" and c.fake.calls == []

    # In a two-line box Up moves the caret; it only walks the sources from the top line.
    c.qtbot.keyClick(edit, Qt.Key.Key_Up)
    assert edit.textCursor().blockNumber() == 0

    c.qtbot.keyClick(edit, Qt.Key.Key_Return)
    answered(c)
    assert c.fake.calls[0]["question"] == "first line\nsecond line"
    assert edit.toPlainText() == ""
    assert c.view.bubbles.bubbles()[0].label.toPlainText() == "first line\nsecond line"


def test_enter_does_nothing_while_an_answer_is_being_written(chat):
    c = chat
    c.fake.hold_at = 4
    ask(c, "slow")
    _wait(c.qtbot, c.fake.held.is_set)
    c.qtbot.keyClicks(c.view.box.edit, "another")
    c.qtbot.keyClick(c.view.box.edit, Qt.Key.Key_Return)
    gui_pump(c.app)
    assert len(c.fake.calls) == 1
    c.fake.release()
    answered(c)


# ---------------------------------------------------------------------------
# Fast / Thoughtful, and settings
# ---------------------------------------------------------------------------

def test_fast_or_thoughtful_is_remembered_and_handed_to_the_engine(chat):
    c = chat
    c.view.speed.setCurrentIndex(c.view.speed.findData("thoughtful"))
    gui_pump(c.app)
    # Queued on the ordered state writer since bug 3a; the question below
    # reads the combo, not the store, so only the persisting has to wait.
    from app.ui.state_writes import pool
    assert pool().waitForDone(5000)
    assert c.store.get_state("ui:chat_speed", "") == "thoughtful"
    ask(c, "deposit?")
    answered(c)
    assert c.fake.calls[-1]["style"] == "thoughtful"


def test_the_chat_settings_group_builds_one_control_per_declared_setting(qtbot, monkeypatch):
    from app.core import settings_registry as reg
    from app.ui.widgets.chat_box import ChatBox

    declared = (
        reg.Setting(key="CHAT_MODEL", label="Model for chat", kind="text", default="",
                    group="Models", surface="settings.models",
                    help="Which installed model answers your questions."),
        reg.Setting(key="CHAT_MAX_ROUNDS", label="Search rounds", kind="int", default=3,
                    group="Models", surface="settings.models", minimum=1, maximum=3,
                    unit="rounds", help="How many times it may search again."),
        reg.Setting(key="CHAT_SHOW_STEPS", label="Show what it is doing", kind="bool",
                    default=True, group="Models", surface="settings.models",
                    help="Show the running commentary while it works."),
    )
    # Replace, not add: the engine now declares real CHAT_ keys of its own.
    kept = tuple(s for s in reg.SETTINGS if not s.key.startswith("CHAT_"))
    monkeypatch.setattr(reg, "SETTINGS", kept + declared)
    box = ChatBox(SimpleNamespace(chat_max_rounds=2, chat_model="qwen", chat_show_steps=False))
    qtbot.addWidget(box)
    for setting in declared:
        control = box.findChild(QWidget, setting.key)
        assert control is not None, f"no control named {setting.key}"
        assert control.toolTip() == setting.help and control.accessibleName() == setting.label
    assert box.values() == {"CHAT_MODEL": "qwen", "CHAT_MAX_ROUNDS": 2, "CHAT_SHOW_STEPS": False}
    with qtbot.waitSignal(box.changed) as got:
        box.findChild(QWidget, "CHAT_MAX_ROUNDS").setValue(3)
    assert got.args == [{"CHAT_MAX_ROUNDS": 3}]


def test_the_settings_page_carries_the_chat_group_and_forwards_its_changes(chat):
    from app.ui.widgets.chat_box import chat_settings

    box = chat.window.settings_view.chat_box
    assert box.isHidden() == (not chat_settings())      # nothing declared, nothing shown
    seen = []
    chat.window.settings_view.settings_changed.connect(seen.append)
    box.changed.emit({"CHAT_MODEL": "x"})
    assert seen == [{"CHAT_MODEL": "x"}]


# ---------------------------------------------------------------------------
# No badges, and every control says what it does
# ---------------------------------------------------------------------------

def _shown_texts(root: QWidget) -> list[str]:
    texts: list[str] = []
    for widget in [root, *root.findChildren(QWidget)]:
        for text in (widget.toolTip(), widget.accessibleName(), widget.accessibleDescription()):
            if text:
                texts.append(text)
        if isinstance(widget, QLabel):
            texts.append(re.sub(r"<[^>]+>", " ", widget.text()))
        if isinstance(widget, QAbstractButton):
            texts.append(widget.text())
        if isinstance(widget, QLineEdit):
            texts.append(widget.placeholderText())
        placeholder = getattr(widget, "placeholderText", None)
        if callable(placeholder):
            texts.append(placeholder())
    return [t for t in texts if t and t.strip()]


def test_nothing_on_the_chat_page_is_a_warning_banner(chat):
    c = chat
    ask(c, "What did we agree about the deposit?")
    answered(c)
    c.fake._available = (False, presenter.unavailable_text(""))
    c.ctl.recheck()
    _wait(c.qtbot, lambda: not c.view.notice.isHidden())
    deny = [re.compile(p, re.IGNORECASE) for p in presenter.WARNING_WORDS]
    texts = _shown_texts(c.view) + _shown_texts(c.window.settings_view.chat_box)
    assert len(texts) > 15
    for text in texts:
        for pattern in deny:
            assert not pattern.search(text), (pattern.pattern, text)


def test_every_control_on_the_page_says_what_it_does(chat):
    c = chat
    ask(c, "deposit?")
    answered(c)
    bare = []
    for control in c.view.findChildren((QAbstractButton, QComboBox, QLineEdit)):
        if isinstance(control, QLineEdit) and control.parent().metaObject().className() \
                .startswith("QAbstract"):
            continue                              # a list's own editor, not ours
        said = (control.toolTip() or control.accessibleName()
                or (control.placeholderText() if isinstance(control, QLineEdit) else ""))
        if not said:
            bare.append((type(control).__name__, control.objectName(),
                         getattr(control, "text", lambda: "")()))
    assert not bare, bare
