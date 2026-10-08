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

**Work order 0x §4e: a filter and a Copy button**, on the caption's row.

* *The filter* ("All" / "Warnings and errors") answers "did anything go wrong?"
  without reading a run's whole story. Which lines count is decided in the
  Qt-free `presenter/log_filter.py`. To be able to change its mind, the widget
  keeps the entries it is showing (at most `LOG_LINES_SHOWN`, the same cap as
  the box) as `(entry, line)` pairs; switching the filter redraws the box from
  those once, and from then on new lines are appended only if they pass - so
  the append-only rule above still holds for every progress tick.
* *Copy* puts exactly the lines on screen - after the filter, each with its
  time - on the clipboard as plain text, for pasting into an email or a bug
  report. Selecting text by hand still works as it always did; the button is
  for "all of it" without a scroll-and-drag.
"""

from __future__ import annotations

from collections import deque
from typing import Any, Optional

from PySide6.QtWidgets import (
    QApplication, QComboBox, QHBoxLayout, QLabel, QPlainTextEdit, QPushButton,
    QSizePolicy, QVBoxLayout, QWidget,
)

from app.ui.presenter.activity import LOG_LINES_SHOWN, activity_line
from app.ui.presenter.log_filter import LOG_FILTER_ALL, LOG_FILTERS, shown_under

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

        # 0x §4e. The filter: a drop-down rather than two buttons, because it
        # is one question with two answers and it takes the least room on a
        # row that already holds the caption.
        self.filter = QComboBox()
        for key, words in LOG_FILTERS:
            self.filter.addItem(words, key)
        self.filter.setAccessibleName("Which lines the run log shows")
        self.filter.setToolTip(
            "Show every line, or only the warnings and errors - the lines "
            "that may need you to do something.")
        self.filter.currentIndexChanged.connect(lambda _i: self._refilter())

        self.copy_button = QPushButton("Copy")
        self.copy_button.setAccessibleName("Copy the run log")
        self.copy_button.setToolTip(
            "Copy the lines shown here, each with its time, as plain text - "
            "ready to paste into an email or a note.")
        self.copy_button.clicked.connect(lambda _c=False: self.copy_lines())

        # Caption on the left, the two controls on the right, on one row, so
        # the log box keeps all of its height for lines.
        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.addWidget(self.caption)
        header.addStretch(1)
        header.addWidget(self.filter)
        header.addWidget(self.copy_button)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        layout.addLayout(header)
        layout.addWidget(self.view)

        # **Tab follows the eye: filter, Copy, then the log.** Qt's default is
        # the order the widgets were made in, which put the log box first.
        QWidget.setTabOrder(self.filter, self.copy_button)
        QWidget.setTabOrder(self.copy_button, self.view)

        # Only ever as tall as its caption row and box: any spare height on the
        # page is not this widget's to hand out as a gap between the two.
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)

        #: The run whose lines are showing, and the last entry shown from it.
        self._run: Any = None
        self._seq = 0
        #: 0x §4e. Every entry the box could show, as `(entry, line)`, newest
        #: last, capped like the box. What the filter redraws from.
        self._kept: deque = deque(maxlen=LOG_LINES_SHOWN)
        self.setVisible(False)

    # -- 0x §4e: the filter and Copy --------------------------------------

    def choice(self) -> str:
        """The filter's key: `log_filter.LOG_FILTER_ALL` or `..._WARNINGS`."""
        return str(self.filter.currentData() or LOG_FILTER_ALL)

    def _refilter(self) -> None:
        """Redraw the box from the kept entries under the new filter choice.

        The one place the whole box is rewritten, and only when the person
        changes the filter - never on a progress tick. The reader is put back
        at the bottom, following, because the lines they were reading may not
        be in the new view at all.
        """
        choice = self.choice()
        shown = [line for entry, line in self._kept if shown_under(entry, choice)]
        self.view.setPlainText("\n".join(shown))
        # The empty box says why it is empty, which differs by choice.
        self.view.setPlaceholderText(
            "Nothing has happened in this run yet." if choice == LOG_FILTER_ALL
            else "No warnings or errors in this run.")
        bar = self.view.verticalScrollBar()
        bar.setValue(bar.maximum())

    def copy_lines(self) -> str:
        """Put the lines on screen on the clipboard; returns what was copied.

        Plain text, one line per entry, each starting with its time - exactly
        what the box shows under the current filter, and nothing it does not.
        """
        text = self.view.toPlainText()
        clipboard = QApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(text)
        return text

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
        """The appending itself: new entries since `_seq`, kept whatever the filter,
        drawn if it lets them through; the reader's place is preserved.
        """
        if log is None:
            return
        run = getattr(log, "run", None)
        if run != self._run:
            self.view.clear()
            self._kept.clear()
            self._run, self._seq = run, 0
        fresh = log.since(self._seq)
        if not fresh:
            return
        self._seq = fresh[-1].seq

        # Every new entry is kept, whatever the filter, so switching back to
        # "All" shows it; only those the filter lets through are drawn now.
        choice = self.choice()
        lines = []
        for entry in fresh:
            line = activity_line(entry)
            self._kept.append((entry, line))
            if shown_under(entry, choice):
                lines.append(line)
        # The log exists as soon as the run has said anything, even if the
        # filter hides all of it - otherwise the filter could never be changed
        # back, because it would be hidden along with the box.
        self.setVisible(True)
        if not lines:
            return

        bar = self.view.verticalScrollBar()
        follow = self.following()
        kept = bar.value()
        before = self.view.blockCount() if self.view.toPlainText() else 0

        # One append for the lot: each call is a layout pass, and a tick after
        # a dropped paint can bring several lines at once.
        self.view.appendPlainText("\n".join(lines))

        if follow:
            bar.setValue(bar.maximum())
            return
        # **Where the reader was, measured in their lines, not in pixels.**
        # Past the cap, each new line at the bottom pushes one off the top, so
        # the same scroll value would now show later lines than they were
        # reading. Moving back by however many fell off keeps their line still.
        dropped = max(0, before + len(lines) - self.view.blockCount())
        bar.setValue(max(0, kept - dropped))
