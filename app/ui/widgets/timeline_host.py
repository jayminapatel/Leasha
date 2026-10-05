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

from PySide6.QtCore import QEvent, QObject, Qt

from app.ui.timeline_view import TimelineView

__all__ = ["attach_timeline", "show_timeline_only", "show_timeline", "keep_names_readable",
           "REPORT_KEY"]

#: The item-data role each Reports list entry carries its key ("inheritance",
#: "space", "timeline") under.
#:
#: **`Qt.ItemDataRole.UserRole`, not `1`** (order 0x section 9, review finding
#: 13). Role 1 *is* `DecorationRole`: the key was being handed to Qt as the
#: item's icon, and although a string draws nothing, the list still reserved an
#: icon's width for it - every report name sat behind a ~36px blank indent.
#: Here rather than in `reports_view.py` because that view is at its line
#: guard, and this module is the other reader of the key.
REPORT_KEY = Qt.ItemDataRole.UserRole


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
    keep_names_readable(page.list)
    return timeline


class _FitsItsNames(QObject):
    """Holds a list at least as wide as its longest name. See `keep_names_readable`."""

    #: Past this a name may be cut rather than take the pane's room.
    CEILING = 240

    def eventFilter(self, watched: Any, event: Any) -> bool:      # noqa: N802 - Qt's name
        if event.type() in (QEvent.Type.Show, QEvent.Type.FontChange, QEvent.Type.StyleChange,
                            QEvent.Type.PolishRequest):
            if watched.count():
                needed = watched.sizeHintForColumn(0) + 2 * watched.frameWidth() + 6
                watched.setMinimumWidth(min(needed, self.CEILING))
        return False                                      # never swallows the event


def keep_names_readable(names: Any) -> None:
    """Stop the report list being squeezed until its names are unreadable.

    **Why.** The list and the pane share a splitter, and the pane takes every
    pixel its contents ask for. With the timeline open - a row of thirteen
    month buttons - it asked for so much that the list was pressed down to
    about sixty pixels and read "Digi", "The" and "Brow": you could no longer
    tell which report you were in (grabbed 2026-09-27, order 0x section 9).

    A list's own minimum is only its scrollbar's, so it is given one: the
    width of its longest name, measured through the style that paints it
    (padding and item border included), whenever it is shown or its font or
    style changes. The pane still gets everything else. The watcher is a
    child of the list, so it lives and dies with it.
    """
    watcher = _FitsItsNames(names)
    names.installEventFilter(watcher)


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
        if page.list.item(row).data(REPORT_KEY) == "timeline":
            page.list.setCurrentRow(row)
            break
    then(page.timeline)
