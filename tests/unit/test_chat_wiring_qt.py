r"""What the Chat tab's controls hand to the engine and to the rest of the window.

Layer: L5 (pytest-qt, `gui_mainwindow`, the same fake engine as `test_chat_tab_qt.py`;
the real-engine half of this is `test_chat_wiring.py`).

Work order `202626270611-chat-tab` sections 3a, 3c and 3d. Each of these was built
on the tab's side and never connected: the Sources pane's right-click menu opened but
Pin, re-index and "More like this" had no listener in the chat view; a removed
document never reached the engine; and the Fast / Thoughtful control changed
nothing and did not say so.
"""

from __future__ import annotations

import dataclasses
from types import SimpleNamespace

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QPoint, Qt                                      # noqa: E402

from app.ui.presenter.chat import Shelf, speed_note                      # noqa: E402
from tests.unit.chat_fakes import AGREEMENT, LETTER                      # noqa: E402
from tests.unit.test_chat_tab_qt import answered, ask, chat, _wait       # noqa: E402,F401

pytestmark = pytest.mark.gui


def _source_row(receipt):
    """The row the Sources pane would emit for `receipt` (what its menu carries)."""
    from app.ui.presenter.chat import receipt_to_result

    return receipt_to_result(receipt, 1)


# ---------------------------------------------------------------------------
# Plain logic: no window needed
# ---------------------------------------------------------------------------

def test_pinning_puts_a_new_document_on_the_shelf_pinned_and_pins_an_existing_one():
    shelf = Shelf()
    assert shelf.pin("C:/a/one.txt") is True
    assert [(i.path, i.pinned) for i in shelf.items] == [("C:/a/one.txt", True)]

    shelf.add("C:/a/two.txt")                                             # touched, not kept
    assert shelf.items[1].pinned is False
    shelf.pin("C:/a/two.txt")
    assert shelf.items[1].pinned is True and len(shelf.items) == 2


def test_pinning_a_document_that_was_taken_off_puts_it_back_on_purpose():
    shelf = Shelf()
    shelf.add("C:/a/one.txt")
    shelf.remove("C:/a/one.txt")
    assert shelf.pin("C:/a/one.txt") is True                              # an explicit act
    assert "C:/a/one.txt" not in shelf.removed


def test_the_speed_note_speaks_only_when_the_control_would_change_nothing():
    assert speed_note({"fast": "small", "thoughtful": "big"}) == ""
    assert speed_note({}) == ""
    assert "small" in speed_note({"fast": "small", "thoughtful": "small"})
    chosen = speed_note({"fast": "small", "thoughtful": "big"}, explicit="qwen")
    assert "qwen" in chosen and "Settings" in chosen


# ---------------------------------------------------------------------------
# The Sources pane's right-click menu
# ---------------------------------------------------------------------------

def test_pin_from_a_source_pins_it_and_the_next_question_carries_it(chat):
    c = chat
    ask(c, "deposit?")
    answered(c)
    row = _source_row(AGREEMENT)

    c.view.sources.results.pin_requested.emit(row)

    item = next(i for i in c.ctl.session.shelf.items if i.path == AGREEMENT.path)
    assert item.pinned, "Pin did nothing: nothing in the chat view listened"
    chip = next(k for k in c.view.shelf.chips() if k.path == AGREEMENT.path)
    assert chip is not None

    ask(c, "and the notice period?")
    answered(c)
    assert AGREEMENT.path in c.fake.calls[-1]["scope"]


def test_a_document_taken_off_the_shelf_reaches_the_engine_as_removed(chat):
    c = chat
    ask(c, "deposit?")
    answered(c)
    c.qtbot.mouseClick(c.view.shelf.chips()[0].remove_button, Qt.MouseButton.LeftButton)
    removed_path = LETTER.path

    ask(c, "and when do I get it back?")
    answered(c)
    assert c.fake.calls[-1]["removed"] == [removed_path]


def test_the_real_right_click_menu_of_a_source_pins_re_indexes_and_finds_similar(chat, monkeypatch):
    """Not a signal emitted by hand: the source's own list is right-clicked, the
    actions it builds are captured where the menu would pop up, and each one is
    invoked - so what is proven is the menu the person sees."""
    c = chat
    ask(c, "deposit?")
    answered(c)
    assert c.view.sources.select_number(1)

    captured = []
    monkeypatch.setattr("app.ui.results_view.show_for",
                        lambda widget, point, path, actions: captured.append(actions))
    reindexed, similar = [], []
    monkeypatch.setattr(c.window, "_reindex_for", reindexed.append)
    monkeypatch.setattr(c.window.search_view.results, "similar_requested",
                        SimpleNamespace(emit=similar.append))

    c.view.sources.results._on_context_menu(QPoint(2, 2))
    actions = captured[-1]
    assert actions.pin and actions.reindex and actions.similar

    actions.reindex()
    assert len(reindexed) == 1
    path = reindexed[0].path
    actions.pin()
    assert any(i.path == path and i.pinned for i in c.ctl.session.shelf.items)
    actions.similar()
    assert len(similar) == 1 and similar[0].path == path


def test_re_index_from_a_source_starts_indexing_that_documents_folder(chat, monkeypatch):
    c = chat
    seen = []
    monkeypatch.setattr(c.window, "_reindex_for", seen.append)
    row = _source_row(LETTER)
    c.view.sources.results.reindex_requested.emit(row)
    assert seen == [row], "Re-index did nothing: nothing in the chat view listened"


def test_more_like_this_from_a_source_runs_on_the_search_page(chat, monkeypatch):
    c = chat
    emitted = []
    monkeypatch.setattr(c.window.search_view.results, "similar_requested",
                        SimpleNamespace(emit=emitted.append))
    c.window._show(c.view)
    row = dataclasses.replace(_source_row(LETTER), chunk_id=42)

    c.view.sources.results.similar_requested.emit(row)

    assert len(emitted) == 1 and emitted[0].chunk_id == 42
    assert c.window.rail.currentIndex() == c.window._tab_index[c.window.search_view], (
        "the answer to 'More like this' is shown on the Search page")


def test_more_like_this_for_a_source_with_no_passage_id_asks_the_index_first(chat, monkeypatch):
    """A source built from a receipt can carry a negative chunk id; the file's own
    first passage is looked up on a worker rather than sending a search nowhere."""
    c = chat
    emitted = []
    monkeypatch.setattr(c.window.search_view.results, "similar_requested",
                        SimpleNamespace(emit=emitted.append))
    monkeypatch.setattr("app.ui.controllers.chat_controller.first_chunk_id",
                        lambda store, path: 77)
    row = dataclasses.replace(_source_row(LETTER), chunk_id=-1)     # a receipt with no passage id

    c.view.sources.results.similar_requested.emit(row)
    _wait(c.qtbot, lambda: bool(emitted))
    assert emitted[0].chunk_id == 77


def test_more_like_this_for_an_unknown_file_does_nothing_instead_of_searching_nowhere(chat, monkeypatch):
    c = chat
    emitted = []
    monkeypatch.setattr(c.window.search_view.results, "similar_requested",
                        SimpleNamespace(emit=emitted.append))
    monkeypatch.setattr("app.ui.controllers.chat_controller.first_chunk_id",
                        lambda store, path: 0)
    c.view.sources.results.similar_requested.emit(
        dataclasses.replace(_source_row(LETTER), chunk_id=-1))
    c.app.processEvents()
    c.qtbot.wait(150)
    assert emitted == []


# ---------------------------------------------------------------------------
# Fast / Thoughtful says when it changes nothing
# ---------------------------------------------------------------------------

def test_the_speed_control_says_so_when_fast_and_thoughtful_are_the_same_model(chat):
    c = chat
    c.fake.suggest_modes = lambda: {"fast": "mistral", "thoughtful": "mistral"}
    c.ctl._check()
    _wait(c.qtbot, lambda: c.view.speed_note.isVisibleTo(c.view))
    assert "mistral" in c.view.speed_note.text()


def test_the_speed_control_is_quiet_when_there_is_a_real_choice(chat):
    c = chat
    c.fake.suggest_modes = lambda: {"fast": "small", "thoughtful": "big"}
    c.ctl._check()
    _wait(c.qtbot, lambda: c.ctl._available is not None)
    c.qtbot.wait(100)
    assert not c.view.speed_note.isVisibleTo(c.view)
