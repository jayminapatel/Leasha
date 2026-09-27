r"""The Indexing page's live log: what the run is doing, line by line, with times.

Layer: L5

Work order 0w §2b. **Pushed with the progress tick, not polled.** The Settings
debug pane polls the application log on a timer because that log is written
from every thread; this one reads `IndexStats.activity`, which already arrives
on the UI thread inside each progress snapshot and already passes through the
0.25 s paint throttle (`indexing_layout.paint_due`). So it costs nothing
between ticks and nothing at all when no run is going.

**Append only what is new.** Each entry carries a sequence number and each log
the run it belongs to, so a paint asks the snapshot for the entries after the
last one shown and appends those - never a `setPlainText` of the whole log,
which would drop the selection of anybody copying a line and fight the
scrollbar of anybody reading, several times a second. A new run's first tick
clears the old run's lines.

**It follows the run until the reader scrolls up, and waits for them there.**
The newest line is at the bottom. At the bottom, new lines keep it there; scrolled
up to read, it stays exactly where they left it - even as old lines fall off
the top - and following starts again the moment they scroll back down. No
checkbox: the scrollbar is already the control, and the debug pane's
"Scroll to the newest line" answers a question this pane never asks.

All wording is in `app/ui/presenter/activity.py`; the few words that are the
widget's own - its caption, tooltip and placeholder - are here, because
`indexing_layout.py` holds none.
"""

from __future__ import annotations

from typing import Any, Optional

from PyQt6.QtWidgets import QLabel, QPlainTextEdit, QVBoxLayout, QWidget

from app.ui.presenter.activity import LOG_LINES_SHOWN, activity_lines

__all__ = ["RunLog", "FOLLOW_SLACK"]

#: How close to the bottom still counts as "at the bottom", in lines. A reader
#: who is one line short of the end has not scrolled up to read; they are
#: watching, and a wheel notch or a resize should not stop the log following.
FOLLOW_SLACK = 1


class RunLog(QWidget):
    """A small read-only log of the run's entries. Hidden until there is one."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.caption = QLabel("What the run is doing")
        self.caption.setObjectName("settingsHint")

        self.view = QPlainTextEdit()
        self.view.setObjectName("indexRunLog")
        self.view.setReadOnly(True)
        self.view.setMaximumBlockCount(LOG_LINES_SHOWN)
        self.view.setPlaceholderText("Nothing has happened in this run yet.")
        self.view.setAccessibleName("Index run log")
        self.view.setToolTip(
            "What this index run has done so far, with the time of each step. "
            "Scroll up to read - the log stays where you leave it - and it "
            "follows the run again once you are back at the bottom.")
        # Room for a handful of lines, not a page: the bar and its sentence
        # above are still the headline, and the skips panel below still needs
        # the stretch.
        self.view.setMinimumHeight(96)
        self.view.setMaximumHeight(150)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        layout.addWidget(self.caption)
        layout.addWidget(self.view)

        #: The run whose lines are showing, and the last entry shown from it.
        self._run: Any = None
        self._seq = 0
        self.setVisible(False)

    def following(self) -> bool:
        """Is the reader at (or within `FOLLOW_SLACK` of) the bottom?"""
        bar = self.view.verticalScrollBar()
        return bar.value() >= bar.maximum() - FOLLOW_SLACK

    def show_activity(self, log: Any) -> None:
        """Append what `log` holds that is not on screen yet. UI thread, no I/O.

        `log` is a snapshot's `ActivityLog`, or None for a stats object that
        has none (a run published by another process). Never raises: a log
        line is not worth an abort inside a Qt slot.
        """
        try:
            self._append(log)
        except Exception:                        # noqa: BLE001 - see docstring
            return

    def _append(self, log: Any) -> None:
        if log is None:
            return
        run = getattr(log, "run", None)
        if run != self._run:
            self.view.clear()
            self._run, self._seq = run, 0
        fresh = log.since(self._seq)
        if not fresh:
            return
        self._seq = fresh[-1].seq

        bar = self.view.verticalScrollBar()
        follow = self.following()
        kept = bar.value()
        before = self.view.blockCount() if self.view.toPlainText() else 0
        lines = activity_lines(fresh)

        # One append for the lot: each call is a layout pass, and a tick after
        # a dropped paint can bring several lines at once.
        self.view.appendPlainText("\n".join(lines))
        self.setVisible(True)

        if follow:
            bar.setValue(bar.maximum())
            return
        # **Where the reader was, measured in their lines, not in pixels.**
        # Past the cap, each new line at the bottom pushes one off the top, so
        # the same scroll value would now show later lines than they were
        # reading. Moving back by however many fell off keeps their line still.
        dropped = max(0, before + len(lines) - self.view.blockCount())
        bar.setValue(max(0, kept - dropped))
