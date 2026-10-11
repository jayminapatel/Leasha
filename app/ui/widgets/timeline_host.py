r"""The Reports list's item key, and the guard that keeps its names readable.

Layer: L5 widget helper

Until order 1i (2026-10-11) this module also hosted the Life Timeline in the
Reports pane. The timeline is a rail page of its own now ("Browse",
`MainWindow.timeline_view`), so what is left is what the Reports list itself
needs, kept out of `reports_view.py` because that view is held under the
presenter split's line budget (`test_every_qt_view_keeps_its_logic_in_the_presenter`).
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import QEvent, QObject, Qt

__all__ = ["keep_names_readable", "REPORT_KEY"]

#: The item-data role each Reports list entry carries its key ("inheritance",
#: "space") under.
#:
#: **`Qt.ItemDataRole.UserRole`, not `1`** (order 0x section 9, review finding
#: 13). Role 1 *is* `DecorationRole`: the key was being handed to Qt as the
#: item's icon, and although a string draws nothing, the list still reserved an
#: icon's width for it - every report name sat behind a ~36px blank indent.
#: Here rather than in `reports_view.py` because that view is at its line
#: guard.
REPORT_KEY = Qt.ItemDataRole.UserRole


class _FitsItsNames(QObject):
    """Holds a list at least as wide as its longest name. See `keep_names_readable`."""

    #: Past this a name may be cut rather than take the pane's room.
    CEILING = 240

    def eventFilter(self, watched: Any, event: Any) -> bool:      # noqa: N802 - Qt's name
        """On show, or a font or style change, hold the list at its longest name's width."""
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
