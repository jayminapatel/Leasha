r"""The Indexing cockpit's lanes table: one fixed row per thread, and the heartbeat.

Layer: L5

**Every thread on screen, nothing silent** (the owner, 2026-10-14: *"any
thing the threads are executing should be visible on the indexing status page
nothing silent"*). One row per reader - Reader 1 to N, from `stats.workers` -
then one row per lane of the run in a fixed order (`presenter.cockpit.LANE_ORDER`:
finding files, pictures, meaning, writing, pictures beside meaning when there is
such a lane, the run's end). Each row says what the thread is doing, on what
(the file, cut with "…", its full path in the tooltip), for how long, and how
far through it is, with the device it runs on when the run says.

**Fixed geometry** (*"the sizes of boxes change on content etc it is disturbing
for the eye"*). Every row is one line of a fixed height; the columns are fixed
widths except "On", which takes what is left; the table's height is fixed for
`SHOWN_READERS` readers plus every lane, and more readers scroll inside it, so
the page never moves. Before a run and after one, the rows stay where they are
and show "-".

**Rows are made once and reused.** A reader's row is made when its number
first appears and kept; the rows are re-ordered only when the set of readers
changes (the pool grows or shrinks mid-run), never per tick, and a tick only
sets the words that changed.

**Force skip** (work order 0z lane B). Each reader's row ends in an icon-only
skip button - accessible name "Force skip reader 2", object name
`indexForceSkip2`, the skip-forward icon - that skips the file that reader has
open now, exactly as its time limit would. It only asks: `forceSkip` carries
the reader's number to `indexing_controls.force_skip_reader`. A pressed button
stays disabled while the same file is shown, so a second press cannot land on
the reader's next file. A reader with no file shows no button (its space kept).

**The heartbeat** (0x §3d) stays: "Working · last activity 2 s ago", redrawn
once a second from the last tick against the clock, under the table beside
the writer's line - and the same second redraws each row's "For" column. The
timer runs only while a run is shown and the page is visible. No words are
written here: they come from `presenter/live_progress.py` and
`presenter/cockpit.py`.
"""

from __future__ import annotations

import time
from typing import Any, Optional, Sequence

from PySide6.QtCore import QTimer, Qt, Signal
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QPushButton, QScrollArea, QSizePolicy,
    QVBoxLayout, QWidget,
)

from app.ui.presenter.cockpit import (
    LANE_ORDER, NO_VALUE, LaneRow, lane_rows, reader_rows,
)
from app.ui.presenter.live_progress import heartbeat_line, live_view
from app.ui.theme import SPACE_S, SPACE_XS
from app.ui.widgets.buttons import icon_button
from app.ui.widgets.cockpit_parts import repolish, tabular
from app.ui.widgets.elided_label import ElidedLabel

__all__ = ["COLUMNS", "FORCE_SKIP_LABEL", "HEARTBEAT_REDRAW_MS", "IndexingWorkers",
           "SHOWN_READERS"]

#: Work order 0z lane B. `{n}` is the reader's number, as its row names it.
FORCE_SKIP_LABEL = "Force skip reader {n}"

#: How often the heartbeat and the "For" column are redrawn between ticks. Work
#: order 0x §3d, "a heartbeat once a second". The text only counts whole
#: seconds, so redrawing faster would change nothing on screen.
HEARTBEAT_REDRAW_MS = 1000

#: How many reader rows the table has room for before it scrolls inside
#: itself. Four is the default reader count; a machine that runs more scrolls
#: rather than pushing the page down.
SHOWN_READERS = 4

#: The column headings (new words, 2026-10-14) and widths in average characters
#: of the font; "On" (None) takes what is left.
COLUMNS: tuple[tuple[str, Optional[int]], ...] = (
    ("Lane", 14), ("Doing", 26), ("On", None), ("For", 9), ("Done", 18), ("", 14),
)


def _row_height(widget: QWidget) -> int:
    """One line of text with room round it, and room for the compact button."""
    return max(24, widget.fontMetrics().height() + SPACE_S)


class _Line(QFrame):
    """One row: status dot and name, Doing, On, For, Done, device, action cell."""

    def __init__(self, name: str, *, header: bool = False) -> None:
        super().__init__()
        self.setObjectName("laneHeader" if header else "laneRow")
        self.setFixedHeight(_row_height(self))
        layout = QHBoxLayout(self)
        layout.setContentsMargins(SPACE_S, 0, SPACE_XS, 0)
        layout.setSpacing(SPACE_S)
        width = self.fontMetrics().horizontalAdvance("n")

        self.dot = QLabel()
        self.dot.setObjectName("laneDot")
        self.dot.setFixedSize(SPACE_S, SPACE_S)
        if header:
            self.dot.setProperty("state", "none")
        layout.addWidget(self.dot)
        self.cells: list[QLabel] = []
        for (heading, chars) in COLUMNS[:5]:
            cell = ElidedLabel(heading if header else NO_VALUE, tip=False)
            if chars is not None:
                cell.setFixedWidth(chars * width)
                cell.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
            layout.addWidget(cell, 0 if chars is not None else 1)
            self.cells.append(cell)
        self.name, self.doing, self.on, self.took, self.done = self.cells
        if header:
            self.name.setText(COLUMNS[0][0])
        else:
            self.name.setText(name)
            self.took.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            tabular(self.took)
            tabular(self.done)
        self.device = ElidedLabel("", tip=False)
        self.device.setObjectName("laneDevice")
        self.device.setFixedWidth(COLUMNS[5][1] * width)
        self.device.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        layout.addWidget(self.device)
        #: Where a reader's skip button goes. The same width on every row, so
        #: the columns line up whether or not a row has one.
        self.action = QWidget()
        # The button system's transparent row cell, so the row shows through.
        self.action.setObjectName("rowCell")
        self.action.setFixedWidth(_row_height(self) + SPACE_S)
        holder = QHBoxLayout(self.action)
        holder.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.action)
        self.row: Optional[LaneRow] = None

    def paint(self, row: LaneRow, now: float) -> None:
        """Show `row`; only the words that changed are set."""
        self.row = row
        repolish(self.dot, "state", row.state)
        repolish(self, "state", row.state)
        self.name.setText(row.name)
        self.doing.setText(row.doing)
        self.on.setText(row.on)
        on_tip = row.on_tip if row.on != NO_VALUE else ""
        if self.on.toolTip() != on_tip:
            self.on.setToolTip(on_tip)
        self.took.setText(row.took(now))
        self.done.setText(row.done)
        self.device.setText(row.device)
        if self.device.toolTip() != row.device_tip:
            self.device.setToolTip(row.device_tip)
        repolish(self.device, "on", bool(row.device))
        tip = row.line or f"{row.name}: {row.doing}"
        if self.toolTip() != tip:
            self.setToolTip(tip)
            self.setAccessibleName(tip)

    def tick(self, now: float) -> None:
        """The once-a-second redraw: the "For" column only."""
        if self.row is not None:
            self.took.setText(self.row.took(now))


class IndexingWorkers(QWidget):
    """The lanes table, its header and the heartbeat line under it.

    `show_live(stats)` paints a progress snapshot; `clear()` puts every row
    back to "-" and stops the clock. Both are UI-thread only and do no I/O.
    `forceSkip(reader)` is emitted when a skip button is pressed, with the
    reader's number as a string ("2"); `rowsChanged` when the set of reader
    rows changed, so the page can put the new buttons in its Tab order.
    """

    forceSkip = Signal(str)
    rowsChanged = Signal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("indexWorkers")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setAccessibleName("What each thread of the index run is doing")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.header = _Line("", header=True)
        layout.addWidget(self.header)

        body = QWidget()
        body.setObjectName("laneBody")
        self._body = QVBoxLayout(body)
        self._body.setContentsMargins(0, 0, 0, 0)
        self._body.setSpacing(0)
        self._readers_box = QVBoxLayout()
        self._readers_box.setSpacing(0)
        #: Said in the readers' place while there are none, so the empty part of
        #: the table explains itself (2026-10-14, new words).
        self.no_readers = QLabel("Readers show here while a run reads files.")
        self.no_readers.setObjectName("settingsHint")
        self.no_readers.setFixedHeight(_row_height(self))
        self.no_readers.setContentsMargins(SPACE_S, 0, SPACE_S, 0)
        self._readers_box.addWidget(self.no_readers)
        self._lanes_box = QVBoxLayout()
        self._lanes_box.setSpacing(0)
        self._body.addLayout(self._readers_box)
        self._body.addLayout(self._lanes_box)
        self._body.addStretch(1)

        self.area = QScrollArea()
        self.area.setObjectName("laneTable")
        self.area.setFrameShape(QFrame.Shape.NoFrame)
        self.area.setWidgetResizable(True)
        self.area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.area.setWidget(body)
        self.area.setFixedHeight(_row_height(self) * (SHOWN_READERS + len(LANE_ORDER)))
        layout.addWidget(self.area)

        # The heartbeat and the writer's line, side by side under the table.
        footer = QHBoxLayout()
        footer.setContentsMargins(SPACE_S, SPACE_XS, SPACE_S, 0)
        footer.setSpacing(SPACE_S)
        self.heartbeat = ElidedLabel("")
        self.heartbeat.setObjectName("indexHeartbeat")
        self.writer = ElidedLabel("")
        self.writer.setObjectName("indexWriter")
        footer.addWidget(self.heartbeat, 1)
        footer.addWidget(self.writer, 1)
        layout.addLayout(footer)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)

        #: Reader number -> its row, and its skip button. Made on first need,
        #: then reused for as long as the page lives.
        self.reader_rows: dict[str, _Line] = {}
        self.skip_buttons: dict[str, QPushButton] = {}
        #: The readers shown now, in order.
        self.readers: list[str] = []
        #: Reader number -> (file, started_at) a press was sent for.
        self._skipped: dict[str, tuple] = {}
        #: Reader number -> (file, started_at) as last painted.
        self._current: dict[str, tuple] = {}
        #: Lane id -> its row. The fixed lanes are made now; "beside" on demand.
        self.lane_rows: dict[str, _Line] = {}
        self._paint_lanes(lane_rows(None), time.time())

        #: The last snapshot painted, so the timer can redraw against the clock
        #: without a new tick. None when nothing is shown.
        self._stats: Any = None
        self._timer = QTimer(self)
        self._timer.setInterval(HEARTBEAT_REDRAW_MS)
        self._timer.timeout.connect(self._beat)

    # -- painting ---------------------------------------------------------

    def show_live(self, stats: Any, *, stopping: bool = False) -> None:
        """Paint one progress snapshot. A stop under way clears the table:
        the headline already says "Stopping after the current file…"."""
        if stats is None or stopping:
            self.clear()
            return
        self._stats = stats
        now = time.time()
        view = live_view(stats, now=now)
        self._paint_readers(reader_rows(stats, now=now), now)
        self._paint_lanes(lane_rows(stats), now)
        self._paint_heartbeat(view.heartbeat, view.quiet)
        self.writer.setText(view.writer)
        if self.isVisible() and not self._timer.isActive():
            self._timer.start()

    def clear(self) -> None:
        """Nothing is running: every row back to "-", no buttons, no timer."""
        self._stats = None
        self._timer.stop()
        now = time.time()
        idle = [LaneRow(key, f"Reader {key}") for key in self.readers]
        self._paint_readers(idle, now)
        self._paint_lanes(lane_rows(None), now)
        self._paint_heartbeat("", False)
        self.writer.setText("")

    def busy_readers(self) -> list[str]:
        """The readers with a file open now, in order. For tests and the page."""
        return [key for key in self.readers
                if self.reader_rows[key].row is not None
                and self.reader_rows[key].row.state == "working"]

    @property
    def beating(self) -> bool:
        """Whether the one-second redraw is running. For tests and the page."""
        return self._timer.isActive()

    def hideEvent(self, event: Any) -> None:       # noqa: N802 - Qt's name
        """Stop the once-a-second redraw while nobody can see it."""
        self._timer.stop()
        super().hideEvent(event)

    def showEvent(self, event: Any) -> None:       # noqa: N802 - Qt's name
        super().showEvent(event)
        if self._stats is not None and not self._timer.isActive():
            self._timer.start()

    def _beat(self) -> None:
        """The once-a-second redraw: the heartbeat and every row's "For"."""
        if self._stats is None or not self.isVisible():
            self._timer.stop()
            return
        now = time.time()
        text, quiet = heartbeat_line(self._stats, now=now)
        self._paint_heartbeat(text, quiet)
        for line in list(self.reader_rows.values()) + list(self.lane_rows.values()):
            line.tick(now)

    def _paint_heartbeat(self, text: str, quiet: bool) -> None:
        """Set the heartbeat text and its `quiet` property; the stylesheet
        decides how a long silence looks, so the words carry the meaning."""
        repolish(self.heartbeat, "quiet", bool(quiet))
        self.heartbeat.setText(text)

    # -- the readers --------------------------------------------------------

    def _paint_readers(self, rows: Sequence[LaneRow], now: float) -> None:
        keys = [row.key for row in rows]
        if keys != self.readers:
            self._arrange(keys)
        busy: dict[str, tuple] = {}
        for row in rows:
            self.reader_rows[row.key].paint(row, now)
            if row.state == "working" and row.file:
                busy[row.key] = (row.file, row.started)
        self._show_skips(busy)

    def _arrange(self, keys: list[str]) -> None:
        """Put the reader rows for `keys` in order; keep the others for later.

        Only when the set of readers changed - the pool grew or shrank - never
        on an ordinary tick.
        """
        for key in self.readers:
            line = self.reader_rows[key]
            self._readers_box.removeWidget(line)
            line.setVisible(False)
        for key in keys:
            line = self.reader_rows.get(key)
            if line is None:
                line = self.reader_rows[key] = _Line(f"Reader {key}")
                button = icon_button(
                    FORCE_SKIP_LABEL.format(n=key),
                    tooltip=f"{FORCE_SKIP_LABEL.format(n=key)}.",
                    name=FORCE_SKIP_LABEL.format(n=key))
                button.setObjectName(f"indexForceSkip{key}")
                button.setProperty("compact", True)
                button.style().unpolish(button)
                button.style().polish(button)
                policy = button.sizePolicy()
                policy.setRetainSizeWhenHidden(True)
                button.setSizePolicy(policy)
                button.clicked.connect(lambda _c=False, k=key: self._pressed(k))
                button.setVisible(False)
                line.action.layout().addWidget(button)
                self.skip_buttons[key] = button
            self._readers_box.addWidget(line)
            line.setVisible(True)
        gone = set(self.readers) - set(keys)
        for key in gone:
            self._skipped.pop(key, None)
            self.skip_buttons[key].setVisible(False)
        self.readers = list(keys)
        self.no_readers.setVisible(not keys)
        self.rowsChanged.emit()

    def _show_skips(self, busy: dict[str, tuple]) -> None:
        """One live skip button per busy reader; the rest hidden, space kept."""
        for key in self.readers:
            button = self.skip_buttons[key]
            if key not in busy:
                button.setVisible(False)
                self._skipped.pop(key, None)
                continue
            button.setToolTip(
                f"{FORCE_SKIP_LABEL.format(n=key)}. Stop reading {busy[key][0]} and "
                "skip it. Everything else carries on. It is recorded as skipped by "
                "you, and read again when it changes.")
            if self._skipped.get(key) != busy[key]:
                self._skipped.pop(key, None)
            button.setEnabled(key not in self._skipped)
            button.setVisible(True)
        self._current = busy

    def _pressed(self, key: str) -> None:
        """A Force skip press: remember which file it was for, so the button stays
        off until that reader moves on, then ask the page to skip it."""
        current = self._current.get(key)
        if current is None:
            return
        self._skipped[key] = current
        button = self.skip_buttons.get(key)
        if button is not None:
            button.setEnabled(False)
        self.forceSkip.emit(key)

    # -- the lanes ----------------------------------------------------------

    def _paint_lanes(self, rows: Sequence[LaneRow], now: float) -> None:
        shown = {row.key for row in rows}
        for index, row in enumerate(rows):
            line = self.lane_rows.get(row.key)
            if line is None:
                line = self.lane_rows[row.key] = _Line(row.name)
                self._lanes_box.insertWidget(index, line)
            line.setVisible(True)
            line.paint(row, now)
        for key, line in self.lane_rows.items():
            if key not in shown:
                line.setVisible(False)
