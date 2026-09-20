r"""How the Reports page carries the timeline in its right-hand pane.

Layer: L5 widget helper

The Reports page is a list of reports beside one pane. Most reports fill the
pane with a document or a table; the timeline is a browsing surface with its
own controls, so it is one more occupant of the same pane rather than a second
page. These two functions are that hosting, kept out of `reports_view.py`
because that view is held under the presenter split's line budget
(`test_every_qt_view_keeps_its_logic_in_the_presenter`) and nothing here is the
Reports page's own business.
"""

from __future__ import annotations

from typing import Any

from app.ui.timeline_view import TimelineView

__all__ = ["attach_timeline", "show_timeline_only", "show_timeline"]


def attach_timeline(page: Any, store: Any, layout: Any) -> TimelineView:
    """Build the timeline, add it to the pane, and pass its signals up through
    `page` (`error`, `opened`, `reveal_requested`) so the shell needs to know
    only the Reports page."""
    timeline = TimelineView(store)
    timeline.hide()
    timeline.error.connect(page.error)
    timeline.opened.connect(page.opened)
    timeline.reveal_requested.connect(page.reveal_requested)
    layout.addWidget(timeline, 1)
    return timeline


def show_timeline_only(page: Any, wandering: bool) -> bool:
    """Give the pane to the timeline (True) or take it back from it (False).

    While it has the pane the document's own furniture - the "as of" line, the
    progress line, Export and both document views - is hidden: a browsing
    surface has no document to export and its own status line.
    """
    page.timeline.setVisible(wandering)
    for part in (page.timestamp, page.progress_label, page.export):
        part.setVisible(not wandering)
    if wandering:
        page.body.hide()
        page.space_table.hide()
    return wandering


def show_timeline(page: Any, then: Any) -> None:
    """Select the timeline on the Reports page, then hand it to `then` (for
    example `lambda timeline: timeline.browse_month_of(when_ns)`). The one way
    other pages - a result's menu, the results' timeline strip - reach it."""
    for row in range(page.list.count()):
        if page.list.item(row).data(1) == "timeline":
            page.list.setCurrentRow(row)
            break
    then(page.timeline)
