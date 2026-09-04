r"""The search box's keyboard-first flow - item 6a.

Layer: L5

Everything else `SearchView` does - the two debounce timers, dispatch,
staleness - is exercised indirectly through `test_results_liveness.py`'s
source-text checks, because building a real `SearchEngine` for every test
here would mean testing the engine, not the view. This file is narrower: it
builds a real `SearchView` with a bare stand-in engine, populates its
`ResultsView` directly (skipping the worker), and drives `eventFilter` the
way Qt itself would - because "does focus leave the box" and "does Enter
open the right row" are exactly the two things a source-text check cannot
answer.
"""

from __future__ import annotations

from typing import Any

import pytest
from PyQt6.QtCore import QEvent, Qt
from PyQt6.QtGui import QKeyEvent

from app.search.engine import SearchResult
from app.ui.search_view import SearchView


class FakeEngine:
    """Just enough for construction - `store` is read with `getattr`."""
    store = None


def result(file_id: int, chunk_id: int, *, rank: int = 0) -> SearchResult:
    return SearchResult(
        chunk_id=chunk_id, file_id=file_id, path=rf"D:\Archive\file{file_id}.pdf",
        text="pump station", score=0.9, rank=rank, ext="pdf",
        mtime_ns=1_700_000_000_000_000_000,
    )


def key_event(key: int, *, ctrl: bool = False) -> QKeyEvent:
    mods = Qt.KeyboardModifier.ControlModifier if ctrl else Qt.KeyboardModifier.NoModifier
    return QKeyEvent(QEvent.Type.KeyPress, key, mods)


@pytest.fixture()
def view(qtbot):
    widget = SearchView(FakeEngine())
    qtbot.addWidget(widget)
    widget.results.show_results([result(1, 1), result(2, 2), result(3, 3)], ["pump"])
    return widget


def test_arrow_down_moves_the_selection_without_leaving_the_box(view):
    consumed = view.eventFilter(view.input, key_event(Qt.Key.Key_Down))
    assert consumed is True
    assert view.results.current_row() is not None


def test_the_filter_never_moves_focus_itself():
    """`hasFocus()` is unreliable under the offscreen platform this suite
    runs on, so the guarantee is checked at the source: nothing in the
    dispatch path may call `setFocus`/`clearFocus` on anything - focus
    staying in the box is a fact about what this code does *not* do."""
    import inspect

    from app.ui.results_view import ResultsView

    for owner, name in ((SearchView, "eventFilter"), (ResultsView, "forward_key"),
                       (ResultsView, "open_current")):
        body = inspect.getsource(getattr(owner, name))
        assert "setFocus" not in body and "clearFocus" not in body, name


def test_arrow_up_also_moves_the_selection(view):
    view.results._list.setCurrentIndex(view.results._model.index(1, 0))
    assert view.eventFilter(view.input, key_event(Qt.Key.Key_Up)) is True
    assert view.results.current_row().file_id == 1


def test_enter_opens_the_selected_result(view, qtbot):
    view.results._list.setCurrentIndex(view.results._model.index(2, 0))
    opened = []
    view.result_opened.connect(opened.append)
    assert view.eventFilter(view.input, key_event(Qt.Key.Key_Return)) is True
    assert len(opened) == 1
    assert opened[0].file_id == 3


def test_ctrl_enter_reveals_instead_of_opening(view):
    view.results._list.setCurrentIndex(view.results._model.index(0, 0))
    revealed = []
    view.reveal_requested.connect(revealed.append)
    opened = []
    view.result_opened.connect(opened.append)
    assert view.eventFilter(view.input, key_event(Qt.Key.Key_Return, ctrl=True)) is True
    assert len(revealed) == 1
    assert not opened


def test_enter_with_nothing_selected_is_left_to_the_box(view):
    """Today's behaviour when nothing is selected is unchanged - the item's
    own wording - so the filter must not consume the key at all, leaving the
    box's own `returnPressed` (a full search) to run as it always did."""
    assert view.results.current_row() is None
    assert view.eventFilter(view.input, key_event(Qt.Key.Key_Return)) is False


def test_keys_on_other_widgets_are_not_intercepted(view):
    """The filter is installed on the search box specifically - a stray key
    event routed to it from anything else must pass straight through."""
    assert view.eventFilter(view.results, key_event(Qt.Key.Key_Down)) is False


def test_a_plain_letter_is_never_intercepted(view):
    """Typing continues the query - only navigation keys are claimed."""
    assert view.eventFilter(view.input, key_event(Qt.Key.Key_A)) is False
