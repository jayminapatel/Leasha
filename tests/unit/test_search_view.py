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


# ---------------------------------------------------------------------------
# Workspace §3: the pinned panel and the timeline strip actually get built and
# wired, not just the results/preview pair that was here before.
# ---------------------------------------------------------------------------

def test_the_split_carries_a_pinned_panel_and_a_timeline(view):
    from app.ui.widgets.pinned_panel import PinnedPanel
    from app.ui.widgets.timeline_strip import TimelineStrip

    assert view.split.findChild(PinnedPanel) is not None
    assert view.split.findChild(TimelineStrip) is not None


def test_pinning_a_result_reaches_the_panel(view):
    from app.ui.widgets.pinned_panel import PinnedPanel

    panel = view.split.findChild(PinnedPanel)
    view.results._list.setCurrentIndex(view.results._model.index(0, 0))
    view.results.pin_requested.emit(view.results.current_row())
    assert len(panel.pins) == 1


def test_a_new_search_updates_the_timeline_without_being_asked(view):
    from app.ui.widgets.timeline_strip import TimelineStrip

    timeline = view.split.findChild(TimelineStrip)
    # Two genuinely different dates, wide enough apart to yield real bars -
    # `result()`'s shared constant timestamp would collapse to one bucket.
    old = result(10, 10, rank=0)
    old.mtime_ns = 1_500_000_000_000_000_000
    new = result(20, 20, rank=1)
    new.mtime_ns = 1_700_000_000_000_000_000
    view.results.show_results([old, new], ["pump"])
    assert timeline._layout.count() >= 1


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


def test_the_whole_flow_types_then_arrows_down_then_opens_the_third_result(view):
    """§8's own scenario, end to end: type, arrow down, Enter opens the
    result the arrows landed on - focus never left the box (§6a's own source
    guard, `test_the_filter_never_moves_focus_itself`, is what proves that;
    this proves the visible effect), and the preview followed each step.

    Nothing is current straight after a search - `ResultsView.show_results`
    clears the anchor - so the first ↓ lands on the first result, exactly
    like `test_arrow_down_moves_the_selection_without_leaving_the_box`
    already shows; three presses in total is what reaches the third."""
    followed: list = []
    view.results.selected.connect(followed.append)

    assert view.eventFilter(view.input, key_event(Qt.Key.Key_P)) is False  # typing continues
    for _ in range(3):
        assert view.eventFilter(view.input, key_event(Qt.Key.Key_Down)) is True

    opened = []
    view.result_opened.connect(opened.append)
    assert view.eventFilter(view.input, key_event(Qt.Key.Key_Return)) is True

    assert len(opened) == 1 and opened[0].file_id == 3
    # Every ↓ moved the selection - the preview's own signal must have fired
    # for each, which is what "the preview follows" means from outside the
    # widget that owns the preview pane.
    assert len(followed) >= 3
    assert followed[-1] is not None and followed[-1].file_id == 3
