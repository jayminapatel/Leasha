r"""The Indexing page's Status shelf: on one screen, nothing crushed.

Layer: L5

Owner, 2026-10-05: *"the indexing page does not look right it is squashed"*,
then *"can the status page be redesigned the right side is mainly blank, and
ideally i would like to see all on one screen"*.

On the real window during a run, the "This index" panel's rows were drawn on
top of each other. Every panel stood in one column. Since the shelf was laid
out, the counts, the reader lines and their Force skip buttons, and the
timed-out panel have been added to it. At the owner's window size their
heights no longer fit, so Qt crushed the panel with the wrapped lines. The
right half of every line was empty.

What these pin, at a window the size of the owner's: no label in the panel
overlaps another, the buttons are on screen, and the shelf asks for no more
height than the window has.

*Note, 14 October 2026:* the two columns these pinned (the run on the left,
the index on the right) became the cockpit - one column of fixed rows with
"This index", the log and the lists in a tab panel at the bottom
(`widgets/cockpit_layout.py`). The layout test was rewritten for that order;
the not-crushed and one-screen tests are unchanged in what they check.
"""

from __future__ import annotations

import time

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QPoint, QRect  # noqa: E402
from PySide6.QtWidgets import QLabel  # noqa: E402

from app.index import pipeline as pipeline_module  # noqa: E402
from app.index.pipeline import IndexStats  # noqa: E402
from app.ui.presenter import StatRow  # noqa: E402

pytestmark = pytest.mark.gui

#: About the owner's maximised window at 125%, less the rail and the shelf list.
WIDTH, HEIGHT = 1150, 700

ROWS = [
    StatRow("Documents", "19,273",
            "1,218 indexed, 12 name only, 279 partial, 17,756 pending, 8 skipped"),
    StatRow("Searchable passages", "2,309"),
    StatRow("Meaning-based search covers", "78%",
            "1,795 of 2,309 passages have a vector. The rest are findable by "
            "exact words only.", warn=True),
    StatRow("Orphaned vectors", "258",
            "More vectors than passages. Rebuild to clear them.", warn=True),
    StatRow("Skipped", "8", "ERR_OCR_HELD (8)"),
    StatRow("Index location", r"D:\Leasha\Data", "on disk · 7.4 GB"),
    StatRow("Next run", "Only when you ask"),
]


def _running(view) -> None:
    now = time.time()
    snap = IndexStats(phase=pipeline_module.PHASE_READING, walk_complete=True,
                      seen=19273, indexed=2051).snapshot()
    snap.workers = {
        str(n): {"file": f"file-{n}.md", "path": rf"D:\JEFF\Kit\kit-v0.9{n}\file-{n}.md",
                 "started_at": now - 30, "stage": "reading", "item": 0, "inner": []}
        for n in range(1, 5)}
    snap.last_activity = now
    view._last_paint = 0.0
    view._on_progress(snap)


def _view(qtbot):
    from app.ui.indexing_view import IndexingView

    view = IndexingView()
    qtbot.addWidget(view)
    view.resize(WIDTH, HEIGHT)
    view.show()
    qtbot.waitExposed(view)
    view.stats_box.show_rows(ROWS)
    _running(view)
    # The log shows itself once the run has said something.
    from app.index.activity import KIND_PHASE, ActivityLog

    log = ActivityLog()
    log.record(KIND_PHASE, pipeline_module.PHASE_READING)
    view.run_log.show_activity(log)
    qtbot.wait(50)
    return view


def _in_view(view, widget) -> QRect:
    return QRect(widget.mapTo(view, QPoint(0, 0)), widget.size())


def _this_index_in_front(view, qtbot) -> None:
    """2026-10-14, the cockpit: "This index" is a tab of the bottom panel, so
    a test that reads its lines brings that tab to the front first."""
    from app.ui.widgets.cockpit_tabs import THIS_INDEX

    view.cockpit_tabs.setCurrentIndex(THIS_INDEX)
    qtbot.wait(50)


def test_no_line_in_this_index_lies_on_another(qtbot) -> None:
    view = _view(qtbot)
    _this_index_in_front(view, qtbot)
    labels = [label for label in view.stats_box.findChildren(QLabel)
              if label.isVisible() and label.text()]
    assert len(labels) >= len(ROWS) * 2
    boxes = [(label.text(), _in_view(view, label)) for label in labels]
    for i, (text, box) in enumerate(boxes):
        # Each label is at least as tall as its own text needs.
        assert box.height() >= labels[i].heightForWidth(box.width()) - 1 or \
            not labels[i].wordWrap(), text
        for other_text, other in boxes[i + 1:]:
            assert not box.intersects(other), f"{text!r} lies on {other_text!r}"


def test_the_cockpit_reads_top_to_bottom(qtbot) -> None:
    """2026-10-14, the owner: *"That should be the cockpit for indexing"*. The
    two columns of 2026-10-05 (the run on the left, the index on the right -
    what this file pinned until then) became one column of fixed rows: the
    state and the buttons, the steps, the tiles, the bar, a row per thread,
    the machine, then the tabs holding the log, the lists and "This index"."""
    view = _view(qtbot)
    # 2026-10-14: the funnel's counts sit between the header and the steps.
    order = [view.pill, view.funnel, view.stepper, view.tiles, view.bar, view.detail,
             view.workers_panel, view.resources, view.cockpit_tabs]
    tops = [_in_view(view, widget).top() for widget in order]
    assert tops == sorted(tops), tops
    for upper, lower in zip(order, order[1:]):
        assert _in_view(view, upper).bottom() < _in_view(view, lower).top()
    # The buttons share the state pill's row, on the right.
    assert abs(_in_view(view, view.controls).center().y()
               - _in_view(view, view.pill).center().y()) < 12
    assert _in_view(view, view.controls).left() > _in_view(view, view.headline).left()


def test_everything_a_run_needs_is_on_one_screen(qtbot) -> None:
    view = _view(qtbot)
    for name in ("controls", "workers_panel", "resources", "cockpit_tabs", "run_log"):
        box = _in_view(view, getattr(view, name))
        assert box.bottom() <= view.height(), f"{name} runs off the bottom"
    _this_index_in_front(view, qtbot)
    assert _in_view(view, view.stats_box).top() < view.height()


def test_the_shelf_needs_no_more_height_than_the_owners_window(qtbot) -> None:
    """**The measure that matches the fault.** A test window simply grows to
    what the page asks for, so nothing there is ever crushed; a maximised
    window cannot grow, and Qt crushes the page instead. So the page must not
    *ask* for more than the owner's window has."""
    view = _view(qtbot)
    assert view.minimumSizeHint().height() <= HEIGHT


@pytest.mark.parametrize("width", [420, 560, 800])
def test_this_index_crushed_to_its_own_minimum_still_reads(qtbot, width) -> None:
    """**The fault itself.** When a page is short of height, Qt shrinks each
    panel towards the minimum that panel declares. The panel's wrapped lines
    declared one line's height each, so at its minimum they were drawn on top
    of one another - the owner's screenshot, 2026-10-05."""
    from app.ui.widgets.index_stats import IndexStats as Panel

    panel = Panel()
    qtbot.addWidget(panel)
    panel.show_rows(ROWS)
    panel.resize(width, 400)
    panel.show()
    qtbot.waitExposed(panel)
    floor = panel.minimumSizeHint().height()
    panel.resize(width, floor)
    qtbot.wait(20)
    labels = [label for label in panel.findChildren(QLabel)
              if label.isVisible() and label.text()]
    boxes = [(label.text(), QRect(label.mapTo(panel, QPoint(0, 0)), label.size()))
             for label in labels]
    for i, (text, box) in enumerate(boxes):
        needed = labels[i].heightForWidth(box.width())
        assert box.height() >= needed - 1, f"{text!r}: {box.height()} < {needed}"
        for other_text, other in boxes[i + 1:]:
            assert not box.intersects(other), f"{text!r} lies on {other_text!r}"
