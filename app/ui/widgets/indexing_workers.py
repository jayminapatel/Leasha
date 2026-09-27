r"""The Indexing page's per-reader lines and heartbeat. Work order 0x §4d.

Layer: L5

**One line per reader, not one shared name.** Several files are read at once,
and the page used to show a single file name that flickered between them. This
panel sits under the bar and shows, for each reader, where it is - down to the
message inside an archive - and how long it has been on that file, then one
heartbeat line ("Working · last activity 2 s ago"), then what the embedder or
the writer is doing behind the readers when there is something to say.

**No words are written here.** Every sentence comes from the Qt-free presenter
(`app/ui/presenter/live_progress.py`, `live_view`), where the wording is
tested without a window. This file only puts the lines on screen and hides
the panel when there is nothing to show.

**Why a one-second timer of its own.** Progress ticks stop arriving in the
stretches of a run that have nothing to count (building the vector index can
take minutes), and a heartbeat that stops beating when nothing is happening
is the one moment it is needed. So the heartbeat line is redrawn once a
second from the last tick, against the clock - no store read, no I/O, one
`setText` on one label. The timer runs only while a run is being shown and
the panel is visible; a hidden page or an idle page runs no timer at all.
"""

from __future__ import annotations

from typing import Any, Optional

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QLabel, QVBoxLayout, QWidget

from app.ui.presenter.live_progress import heartbeat_line, live_view

__all__ = ["HEARTBEAT_REDRAW_MS", "IndexingWorkers"]

#: How often the heartbeat line is redrawn between ticks. Work order 0x §3d,
#: "a heartbeat once a second". A constant (non-negotiable 11): the text only
#: counts whole seconds, so redrawing faster would change nothing on screen.
HEARTBEAT_REDRAW_MS = 1000


class IndexingWorkers(QWidget):
    """Three labels: the readers, the heartbeat, and the work behind them.

    `show_live(stats)` paints a progress snapshot; `clear()` empties and hides
    the panel. Both are UI-thread only and do no I/O.
    """

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("indexWorkers")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)

        #: One line per reader, joined with newlines into one label: a label
        #: per reader would be created and destroyed as the pool grows and
        #: shrinks (§6g), for text that is only ever read.
        self.lines = QLabel("")
        self.lines.setObjectName("indexWorkerLines")
        self.lines.setWordWrap(True)
        self.heartbeat = QLabel("")
        self.heartbeat.setObjectName("indexHeartbeat")
        self.heartbeat.setWordWrap(True)
        self.writer = QLabel("")
        self.writer.setObjectName("indexWriter")
        self.writer.setWordWrap(True)
        for label in (self.lines, self.heartbeat, self.writer):
            layout.addWidget(label)
            label.setVisible(False)

        #: The last snapshot painted, so the timer can redraw the heartbeat
        #: against the clock without a new tick. None when nothing is shown.
        self._stats: Any = None
        self._timer = QTimer(self)
        self._timer.setInterval(HEARTBEAT_REDRAW_MS)
        self._timer.timeout.connect(self._beat)
        self.setVisible(False)

    def show_live(self, stats: Any, *, stopping: bool = False) -> None:
        """Paint one progress snapshot. A stop under way clears the panel:
        the counts line already says "Stopping after the current file…"."""
        if stats is None or stopping:
            self.clear()
            return
        self._stats = stats
        view = live_view(stats)
        self._set(self.lines, "\n".join(view.workers))
        self._paint_heartbeat(view.heartbeat, view.quiet)
        self._set(self.writer, view.writer)
        showing = bool(view.workers or view.heartbeat or view.writer)
        self.setVisible(showing)
        if showing and not self._timer.isActive():
            self._timer.start()

    def clear(self) -> None:
        """Nothing is running: empty, hidden, and no timer."""
        self._stats = None
        self._timer.stop()
        for label in (self.lines, self.heartbeat, self.writer):
            self._set(label, "")
        self.setVisible(False)

    @property
    def beating(self) -> bool:
        """Whether the one-second redraw is running. For tests and the page."""
        return self._timer.isActive()

    def hideEvent(self, event: Any) -> None:       # noqa: N802 - Qt's name
        # Nobody can see it, so nothing needs redrawing. `showEvent` starts it
        # again only while there is a run to show.
        self._timer.stop()
        super().hideEvent(event)

    def showEvent(self, event: Any) -> None:       # noqa: N802 - Qt's name
        super().showEvent(event)
        if self._stats is not None and not self._timer.isActive():
            self._timer.start()

    def _beat(self) -> None:
        """The once-a-second redraw: the heartbeat line only, from the last tick."""
        if self._stats is None or not self.isVisible():
            self._timer.stop()
            return
        text, quiet = heartbeat_line(self._stats)
        self._paint_heartbeat(text, quiet)

    def _paint_heartbeat(self, text: str, quiet: bool) -> None:
        # A dynamic property rather than a colour set here, so the theme's
        # stylesheet decides how a quiet warning looks - and a screen reader
        # gets the same words either way.
        if bool(self.heartbeat.property("quiet")) != quiet:
            self.heartbeat.setProperty("quiet", quiet)
            self.heartbeat.style().unpolish(self.heartbeat)
            self.heartbeat.style().polish(self.heartbeat)
        self._set(self.heartbeat, text)

    @staticmethod
    def _set(label: QLabel, text: str) -> None:
        if label.text() != text:
            label.setText(text)
        label.setVisible(bool(text))
