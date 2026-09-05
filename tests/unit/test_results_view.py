r"""`ResultsView` itself - the widget, not the delegate that paints its rows.

Layer: L5

Most of this widget's rules are already covered where they are cheaper to
check: painting and geometry in `test_result_delegate.py`, "never blanks
while reading" in `test_results_liveness.py`, grouping in
`test_result_groups.py`. This file is for what genuinely needs a live
`QListView` - the stable-update rule (item 5d) and the keyboard-first flow
(item 6a) - because both are about what a real selection model does across
a real rebuild, which a source-text check cannot see.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.search.engine import SearchResult
from app.ui.results_view import ResultsView


def result(file_id: int, chunk_id: int, *, rank: int = 0, score: float = 0.9,
          path: str | None = None) -> SearchResult:
    return SearchResult(
        chunk_id=chunk_id, file_id=file_id,
        path=path or rf"D:\Archive\file{file_id}.pdf",
        text=f"chunk {chunk_id} pump station", score=score, rank=rank,
        ext="pdf", mtime_ns=1_700_000_000_000_000_000,
    )


# ---------------------------------------------------------------------------
# Item 5d: the stable-update rule
# ---------------------------------------------------------------------------

def test_the_current_row_survives_a_rebuild_that_adds_rows(qtbot):
    """Interim tier shows 3 documents; the full tier adds 2 more and
    re-ranks. The row that was current - document 2 - must still be current,
    by identity, not by the screen position it happened to occupy."""
    view = ResultsView()
    qtbot.addWidget(view)

    interim = [result(1, 1, rank=0), result(2, 2, rank=1), result(3, 3, rank=2)]
    view.show_results(interim, ["pump"])

    # Land on document 2's row (index 1) - the "row under the pointer".
    view._list.setCurrentIndex(view._model.index(1, 0))
    assert view.current_row().file_id == 2

    # The full tier: document 2 now ranks last, and two new documents appear
    # ahead of it - the re-rank and the append the item's wording describes.
    full = [result(4, 4, rank=0), result(5, 5, rank=1), result(1, 1, rank=2),
            result(3, 3, rank=3), result(2, 2, rank=4)]
    view.show_results(full, ["pump"], keep_scroll=True)

    after = view.current_row()
    assert after is not None
    assert after.file_id == 2, "the same document must still be the current row"


def test_a_fresh_search_does_not_try_to_preserve_the_old_anchor(qtbot):
    """`keep_scroll=False` - a genuinely new search - must not go hunting for
    the old current row in results that have nothing to do with it. Nothing
    is current afterwards (a new search selects nothing, same as before this
    item), which is the point: it must not silently land on document 9
    merely because *something* has to be "restored"."""
    view = ResultsView()
    qtbot.addWidget(view)

    view.show_results([result(1, 1)], ["pump"])
    view._list.setCurrentIndex(view._model.index(0, 0))

    view.show_results([result(9, 9)], ["station"], keep_scroll=False)
    assert view.current_row() is None


def test_no_current_row_means_nothing_to_anchor(qtbot):
    """No selection yet - the ordinary state right after a first search -
    must not raise or misbehave."""
    view = ResultsView()
    qtbot.addWidget(view)

    view.show_results([result(1, 1)], ["pump"])
    view.show_results([result(1, 1), result(2, 2)], ["pump"], keep_scroll=True)
    assert view._model.rowCount() >= 2


# ---------------------------------------------------------------------------
# Workspace §3b/§3c: dragging out, and the "Pin" menu action
# ---------------------------------------------------------------------------

def test_drag_is_on_by_default(qtbot):
    view = ResultsView()
    qtbot.addWidget(view)
    assert view._list.dragEnabled()


def test_set_drag_enabled_toggles_the_list(qtbot):
    view = ResultsView()
    qtbot.addWidget(view)
    view.set_drag_enabled(False)
    assert not view._list.dragEnabled()
    view.set_drag_enabled(True)
    assert view._list.dragEnabled()


def test_context_menu_wires_pin_to_the_row_under_the_cursor(qtbot, monkeypatch):
    view = ResultsView()
    qtbot.addWidget(view)
    view.show_results([result(1, 1)], ["pump"])
    from PyQt6.QtCore import QItemSelectionModel
    index = view._model.index(0, 0)
    view._list.setCurrentIndex(index)
    view._list.selectionModel().select(index, QItemSelectionModel.SelectionFlag.Select)

    captured: dict = {}

    def fake_show_for(_widget, _point, _path, actions):
        captured["actions"] = actions

    import app.ui.results_view as results_view_module
    monkeypatch.setattr(results_view_module, "show_for", fake_show_for)

    view._on_context_menu(view._list.visualRect(view._model.index(0, 0)).center())
    assert "actions" in captured and captured["actions"].pin is not None

    pinned: list = []
    view.pin_requested.connect(pinned.append)
    captured["actions"].pin()
    assert len(pinned) == 1
    assert pinned[0].file_id == 1


def test_rows_changed_fires_on_a_fresh_search(qtbot):
    view = ResultsView()
    qtbot.addWidget(view)
    seen: list = []
    view.rows_changed.connect(seen.append)
    view.show_results([result(1, 1), result(2, 2)], ["pump"])
    assert len(seen) == 1
    assert len(seen[0]) == 2


# ---------------------------------------------------------------------------
# Work order 0h §2d: "more like this", for a passage and for a photo alike
# ---------------------------------------------------------------------------

def test_context_menu_offers_more_like_this_for_a_text_row(qtbot, monkeypatch):
    view = ResultsView()
    qtbot.addWidget(view)
    view.show_results([result(1, 1)], ["pump"])
    from PyQt6.QtCore import QItemSelectionModel
    index = view._model.index(0, 0)
    view._list.setCurrentIndex(index)
    view._list.selectionModel().select(index, QItemSelectionModel.SelectionFlag.Select)

    captured: dict = {}

    def fake_show_for(_widget, _point, _path, actions):
        captured["actions"] = actions

    import app.ui.results_view as results_view_module
    monkeypatch.setattr(results_view_module, "show_for", fake_show_for)

    view._on_context_menu(view._list.visualRect(index).center())
    assert captured["actions"].similar is not None

    similar: list = []
    view.similar_requested.connect(similar.append)
    captured["actions"].similar()
    assert len(similar) == 1
    assert similar[0].file_id == 1


def test_context_menu_offers_more_like_this_for_a_photo_row_too(qtbot, monkeypatch):
    r"""The whole point of item 2d's UI half: a photo result is a
    `SearchResult` exactly like a passage, distinguished only by `ext` - so
    the same menu, the same signal, the same wiring, works for both without
    a special case in this view."""
    view = ResultsView()
    qtbot.addWidget(view)
    photo = result(1, 1, path=r"D:\Photos\2019\beach.jpg")
    photo.ext = "jpg"
    view.show_results([photo], [])
    from PyQt6.QtCore import QItemSelectionModel
    index = view._model.index(0, 0)
    view._list.setCurrentIndex(index)
    view._list.selectionModel().select(index, QItemSelectionModel.SelectionFlag.Select)

    captured: dict = {}

    def fake_show_for(_widget, _point, _path, actions):
        captured["actions"] = actions

    import app.ui.results_view as results_view_module
    monkeypatch.setattr(results_view_module, "show_for", fake_show_for)

    view._on_context_menu(view._list.visualRect(index).center())
    assert captured["actions"].similar is not None


def test_image_rows_keeps_only_photos_in_order():
    view = ResultsView()
    text_row = result(1, 1, path=r"D:\Docs\report.pdf")
    photo_a = result(2, 2, path=r"D:\Photos\a.jpg"); photo_a.ext = "jpg"
    photo_b = result(3, 3, path=r"D:\Photos\b.png"); photo_b.ext = "png"
    view.show_results([text_row, photo_a, photo_b], [])
    assert [row.file_id for row in view.image_rows()] == [2, 3]


# ---------------------------------------------------------------------------
# Item 2a: the chevron is a real click target, alongside the whole row
# ---------------------------------------------------------------------------

def test_a_click_on_the_chevron_expands_the_group(qtbot):
    from PyQt6.QtCore import QEvent, QPointF, Qt
    from PyQt6.QtGui import QMouseEvent

    from app.ui.result_delegate import ROLE_PAYLOAD

    view = ResultsView()
    qtbot.addWidget(view)
    view.resize(400, 300)
    view.show_results([result(1, 1, rank=0), result(1, 2, rank=1), result(1, 3, rank=2)],
                      ["pump"])
    index = view._model.index(0, 0)
    from PyQt6.QtWidgets import QStyleOptionViewItem

    option = QStyleOptionViewItem()
    view._list.initViewItemOption(option)
    option.rect = view._list.visualRect(index)
    rect = view._delegate.subtitle_rect(option, index.data(ROLE_PAYLOAD))
    assert rect is not None, "a 3-match group must paint a chevron line"

    point = QPointF(rect.center())
    press = QMouseEvent(QEvent.Type.MouseButtonPress, point, Qt.MouseButton.LeftButton,
                        Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    assert view.eventFilter(view._list.viewport(), press) is True
    assert 1 in view._expanded, "the click must have toggled the group open"


def test_a_click_off_the_chevron_does_not_toggle(qtbot):
    """A click on the name or date must fall through to Qt's ordinary
    selection handling, not be swallowed as a toggle."""
    from PyQt6.QtCore import QEvent, QPointF, Qt
    from PyQt6.QtGui import QMouseEvent

    view = ResultsView()
    qtbot.addWidget(view)
    view.resize(400, 300)
    view.show_results([result(1, 1, rank=0), result(1, 2, rank=1)], ["pump"])

    press = QMouseEvent(QEvent.Type.MouseButtonPress, QPointF(5, 5), Qt.MouseButton.LeftButton,
                        Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    assert view.eventFilter(view._list.viewport(), press) is False
    assert not view._expanded, "the name line is not the chevron's click target"


def test_the_whole_row_still_toggles_by_activation(qtbot):
    """Item 2a's own wording: "the whole row still toggles as it does
    today" - double-click/Enter (`activated`) must keep working exactly as
    it did before the chevron gained its own click target."""
    view = ResultsView()
    qtbot.addWidget(view)
    view.show_results([result(1, 1, rank=0), result(1, 2, rank=1)], ["pump"])
    view._on_activated(view._model.index(0, 0))
    assert 1 in view._expanded
    view._on_activated(view._model.index(0, 0))
    assert 1 not in view._expanded
