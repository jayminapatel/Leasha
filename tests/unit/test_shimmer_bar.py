r"""`widgets/shimmer_bar.py` - the progress bar that shows it is alive.

Layer: L5. Owner, 2026-09-30: *"the progress bar is also not animating, make the
progress bar a gradient animation which animates when it is going on"*.

What is held: it animates only while it matters (busy, or a run is going, and
visible), it stops when hidden or finished, the fill is still exactly the
fraction done (the sweep never draws progress that has not happened), the text
reads on the fill in both themes, and the Indexing page and the rail use it.
"""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from PySide6.QtGui import QColor  # noqa: E402

from app.ui.widgets.shimmer_bar import ShimmerBar, fill_colours, text_on  # noqa: E402

pytestmark = pytest.mark.gui


def _bar(qtbot, *, show: bool = True) -> ShimmerBar:
    bar = ShimmerBar()
    qtbot.addWidget(bar)
    bar.resize(300, 20)
    if show:
        bar.show()
        qtbot.waitExposed(bar)
    return bar


def test_an_idle_bar_runs_no_timer(qtbot) -> None:
    bar = _bar(qtbot)
    bar.setRange(0, 10)
    bar.setValue(4)
    assert not bar.animating()


def test_a_busy_bar_moves_by_itself(qtbot) -> None:
    bar = _bar(qtbot)
    bar.setRange(0, 0)
    assert bar.animating()
    qtbot.waitUntil(lambda: bar.frames >= 3, timeout=2000)


def test_a_running_bar_shimmers_and_stops_when_the_run_ends(qtbot) -> None:
    bar = _bar(qtbot)
    bar.setRange(0, 100)
    bar.setValue(40)
    bar.set_active(True)
    assert bar.animating()
    qtbot.waitUntil(lambda: bar.frames >= 3, timeout=2000)
    bar.set_active(False)
    assert not bar.animating()


def test_the_sweep_never_changes_what_the_bar_says(qtbot) -> None:
    bar = _bar(qtbot)
    bar.setRange(0, 100)
    bar.setValue(40)
    bar.set_active(True)
    qtbot.waitUntil(lambda: bar.frames >= 3, timeout=2000)
    assert (bar.value(), bar.maximum()) == (40, 100)


def test_nothing_animates_while_hidden(qtbot) -> None:
    bar = _bar(qtbot, show=False)
    bar.setRange(0, 0)
    bar.set_active(True)
    assert not bar.animating()
    bar.show()
    qtbot.waitExposed(bar)
    assert bar.animating()
    bar.hide()
    assert not bar.animating()


def test_the_text_reads_on_the_fill_in_both_themes() -> None:
    assert text_on(*fill_colours("#2b1a7a")) == QColor("#ffffff")      # light theme
    assert text_on(*fill_colours("#9d8cf0")) != QColor("#ffffff")      # dark theme
    start, end = fill_colours("not a colour")
    assert start.isValid() and end.isValid()


def test_the_indexing_page_and_the_rail_use_it(qtbot) -> None:
    from app.ui.indexing_view import IndexingView
    from app.ui.widgets.indexing_bar import GlidingBar

    view = IndexingView()
    qtbot.addWidget(view)
    assert isinstance(view.bar, GlidingBar) and isinstance(view.bar, ShimmerBar)
    import app.ui.widgets.rail as rail

    assert "ShimmerBar()" in open(rail.__file__, encoding="utf-8").read()
