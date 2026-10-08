r"""`QDrag` support for a list of results or pins. Workspace §3b.

Layer: L5 widget — the only Qt in drag-out. *Which* paths may be dragged is
decided in `app/ui/drag_out.py`, which is Qt-free and tested without a widget;
this is the one call that hands its answer to Qt.

**Overriding `mimeData`, not `startDrag`.** `QAbstractItemView.setDragEnabled
(True)` already runs the whole drag gesture - press, move far enough, build a
`QDrag`, execute it - once the model it is asking hands back something worth
dragging. A plain `QStandardItemModel` answers with its own internal format,
useful for reordering a list and nothing else; this model answers with file
URLs instead, so nothing in `results_view.py` or `pinned_panel.py` has to
touch `QDrag` at all.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from PySide6.QtCore import QMimeData, QUrl
from PySide6.QtGui import QStandardItemModel

from app.ui.drag_out import paths_for
from app.ui.result_delegate import ROLE_PAYLOAD

__all__ = ["DraggableResultsModel"]


class DraggableResultsModel(QStandardItemModel):
    """A `QStandardItemModel` whose drag payload is the real files it holds.

    `missing`, when given, is a zero-argument callable returning the paths
    already known not to exist - see `presenter.missing_paths`. Read here
    rather than stat'ed again: this runs at the moment a drag starts, which is
    the one moment this application's own rule says I/O may not run on the
    interface thread (`app/ui/drag_out.py`'s module docstring).
    """

    def __init__(self, parent: Any = None, *,
                missing: Optional[Callable[[], Any]] = None) -> None:
        super().__init__(parent)
        self._missing = missing or (lambda: ())

    def mimeData(self, indexes: Any) -> QMimeData:              # noqa: N802 - Qt override
        """Qt asks for this as a drag starts. UI thread, no stat: the paths come from
        the payloads and `missing` is what a worker already decided.
        """
        payloads = [index.data(ROLE_PAYLOAD) for index in (indexes or ())
                   if index.column() == 0]
        known_missing = set(self._missing() or ())
        paths = [path for path in paths_for(payloads) if path not in known_missing]
        data = QMimeData()
        if paths:
            data.setUrls([QUrl.fromLocalFile(path) for path in paths])
        return data
