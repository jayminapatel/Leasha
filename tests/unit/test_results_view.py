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
