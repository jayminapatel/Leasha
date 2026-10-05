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

**Force skip** (work order 0z lane B). Under the lines, one small button per
busy reader - "Force skip reader 2" - that skips the file that reader has open
now, exactly as its time limit would, with the reason naming the person
(`app/index/file_watch.py`). The button only asks: `forceSkip` carries the
reader's number to `indexing_controls.force_skip_reader`, which marks the file
on the run (a flag, or one line to the indexing process) and returns. The
buttons are made once per reader number and reused, as the lines' label is.
A pressed button stays disabled while the same file is shown, so a second
press cannot land on the reader's next file.
"""

from __future__ import annotations

from typing import Any, Optional

from PySide6.QtCore import QTimer, Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from app.ui.presenter.live_progress import heartbeat_line, live_view
from app.ui.widgets.buttons import style_button

__all__ = ["FORCE_SKIP_LABEL", "HEARTBEAT_REDRAW_MS", "IndexingWorkers"]

#: Work order 0z lane B. `{n}` is the reader's number, as its line says it.
FORCE_SKIP_LABEL = "Force skip reader {n}"

#: How often the heartbeat line is redrawn between ticks. Work order 0x §3d,
#: "a heartbeat once a second". A constant (non-negotiable 11): the text only
#: counts whole seconds, so redrawing faster would change nothing on screen.
HEARTBEAT_REDRAW_MS = 1000


class IndexingWorkers(QWidget):
    """Three labels: the readers, the heartbeat, and the work behind them.

    `show_live(stats)` paints a progress snapshot; `clear()` empties and hides
    the panel. Both are UI-thread only and do no I/O.

    `forceSkip(reader)` is emitted when a Force skip button is pressed, with
    the reader's number as a string ("2").
    """

    forceSkip = Signal(str)

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
        # 0z lane B: the Force skip buttons, under the lines they act on.
        self.skip_row = QWidget()
        self.skip_row.setObjectName("indexForceSkip")
        self._skip_layout = QHBoxLayout(self.skip_row)
        self._skip_layout.setContentsMargins(0, 0, 0, 0)
        self._skip_layout.addStretch(1)
        #: Reader number -> its button. Made on first need, then reused.
        self.skip_buttons: dict[str, QPushButton] = {}
        #: Reader number -> (file, started_at) a press was sent for.
        self._skipped: dict[str, tuple] = {}
        #: Reader number -> (file, started_at) as last painted.
        self._current: dict[str, tuple] = {}
        layout.insertWidget(1, self.skip_row)
        self.skip_row.setVisible(False)

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
        self._show_skips(getattr(stats, "workers", None))
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
        self._show_skips(None)
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

    def _show_skips(self, workers: Any) -> None:
        """One Force skip button per busy reader; the rest hidden."""
        busy: dict[str, tuple] = {}
        if isinstance(workers, dict):
            for key, worker in workers.items():
                if isinstance(worker, dict) and worker.get("file"):
                    busy[str(key)] = (worker.get("file"), worker.get("started_at"))
        for key in sorted(busy, key=lambda k: (len(k), k)):
            button = self.skip_buttons.get(key)
            if button is None:
                button = QPushButton(FORCE_SKIP_LABEL.format(n=key))
                button.setObjectName(f"indexForceSkip{key}")
                button.clicked.connect(lambda _c=False, k=key: self._pressed(k))
                # 2026-10-05 (owner: "the force skip needs icons too"): the
                # button system's look and its skip-forward icon. Made after
                # the window styled its buttons, so it asks for them itself.
                style_button(button)
                self.skip_buttons[key] = button
                # In reader order: they were made as readers first got a file,
                # so the row read "1, 3, 4, 2" on the owner's screen.
                ordered = sorted(self.skip_buttons, key=lambda k: (len(k), k))
                self._skip_layout.insertWidget(ordered.index(key), button)
            button.setToolTip(
                f"Stop reading {busy[key][0]} and skip it. Everything else carries "
                "on. It is recorded as skipped by you, and read again when it "
                "changes.")
            if self._skipped.get(key) != busy[key]:
                self._skipped.pop(key, None)
            button.setEnabled(key not in self._skipped)
            button.setVisible(True)
        for key, button in self.skip_buttons.items():
            if key not in busy:
                button.setVisible(False)
                self._skipped.pop(key, None)
        self.skip_row.setVisible(bool(busy))
        self._current = busy

    def _pressed(self, key: str) -> None:
        current = self._current.get(key)
        if current is None:
            return
        self._skipped[key] = current
        button = self.skip_buttons.get(key)
        if button is not None:
            button.setEnabled(False)
        self.forceSkip.emit(key)

    @staticmethod
    def _set(label: QLabel, text: str) -> None:
        if label.text() != text:
            label.setText(text)
        label.setVisible(bool(text))
