"""When indexing runs. *How fast* it runs is the Index Tuning screen.

Layer: L6 (UI), driving L3

**This panel used to hold both questions, and they are not the same question.**
A schedule is about the calendar; a memory ceiling, a worker count and what
gets read are about throughput, and they interact with one another in ways that
only make sense side by side - raise the workers, raise the threads, and the
machine gets slower while every control reads faster. So §4 of the index-tuning
order moved all of that onto one screen, `widgets/tuning_box.py`, and left this
one with the three settings that decide *when*.

The decisions live elsewhere and are tested: `app/index/schedule.py` decides
when a run is due, `app/index/resources.py` decides whether it may proceed. This
is widgets.
"""

from __future__ import annotations

from typing import Any, Optional

from PyQt6.QtCore import QTime, pyqtSignal
from PyQt6.QtWidgets import (
    QComboBox,
    QFormLayout,
    QGroupBox,
    QLabel,
    QSpinBox,
    QTimeEdit,
)

from app.ui.widgets.debounce import Debounced

__all__ = ["IndexingSettings"]


class IndexingSettings(QGroupBox):
    """The schedule: whether a run happens without being asked, and when."""

    schedule_changed = pyqtSignal(object)      # a SchedulePolicy

    def __init__(self, parent: Optional[Any] = None) -> None:
        super().__init__("When to index", parent)

        self.schedule = QComboBox()
        self.schedule.setToolTip(
            "When to index without being asked. Manual means only when you "
            "press Start.")
        self.schedule.setObjectName("INDEX_SCHEDULE")
        self.schedule.addItem("Only when I ask", "manual")
        self.schedule.addItem("Once, shortly after opening", "startup")
        self.schedule.addItem("Every few hours", "interval")
        self.schedule.addItem("Once a day", "daily")
        self.schedule.currentIndexChanged.connect(self._schedule_changed)

        self.interval_hours = QSpinBox()
        self.interval_hours.setToolTip(
            "How long to wait between automatic runs.\n\n"
            "An incremental run over a settled corpus takes seconds, so a short "
            "interval costs little - most of the time there is nothing new to "
            "read.")
        self.interval_hours.setObjectName("INDEX_INTERVAL_HOURS")
        self.interval_hours.setRange(1, 168)
        self.interval_hours.setSuffix(" hours")
        # Typing `168` otherwise emits at 1, 16 and 168 - three rounds of
        # writes for one number, two of them values nobody chose.
        self.interval_hours.setKeyboardTracking(False)
        self.interval_hours.valueChanged.connect(self._schedule_changed)

        self.daily_at = QTimeEdit()
        self.daily_at.setToolTip(
            "The time of day to start. Pick an hour the machine is on and you "
            "are not using it - indexing gets out of the way, but reading a "
            "terabyte is still work.")
        self.daily_at.setObjectName("INDEX_DAILY_AT")
        self.daily_at.setDisplayFormat("HH:mm")
        self.daily_at.timeChanged.connect(self._schedule_changed)

        self.schedule_status = QLabel("")
        self.schedule_status.setWordWrap(True)

        # **Emit once the person has stopped, not on every notch.** Each
        # emission persists a policy, so holding an arrow - which auto-repeats
        # about ten times a second - asked the UI thread for a committed
        # SQLite transaction ten times a second, and the window stopped
        # repainting between them.
        self._save_schedule = Debounced(
            lambda: self.schedule_changed.emit(self.current_policy()),
            parent=self)

        index_form = QFormLayout(self)
        index_form.addRow("Run", self.schedule)
        index_form.addRow("Every", self.interval_hours)
        index_form.addRow("At", self.daily_at)
        index_form.addRow(self.schedule_status)

    def load_indexing(self, settings: Any) -> None:
        """Fill the controls from Settings, without emitting on the way in.

        Blocking signals matters: setting three widgets would otherwise fire
        three change notifications before the panel has even been shown, each
        one writing a partially-populated policy back over the stored one.
        """
        from app.core.config import parse_daily_at

        widgets = (self.schedule, self.interval_hours, self.daily_at)
        for widget in widgets:
            widget.blockSignals(True)
        try:
            mode = str(getattr(settings, "index_schedule", "manual"))
            index = self.schedule.findData(mode)
            self.schedule.setCurrentIndex(index if index >= 0 else 0)
            self.interval_hours.setValue(int(getattr(settings, "index_interval_hours", 6)))
            hour, minute = parse_daily_at(str(getattr(settings, "index_daily_at", "02:00"))) or (2, 0)
            self.daily_at.setTime(QTime(hour, minute))
        finally:
            for widget in widgets:
                widget.blockSignals(False)
        # Anything queued before this load was about the *old* values. Firing it
        # now would write them straight back over what was just loaded.
        self._save_schedule.cancel()
        self._sync_schedule_rows()

    def current_policy(self):
        from app.index.schedule import SchedulePolicy

        time = self.daily_at.time()
        return SchedulePolicy(
            mode=str(self.schedule.currentData() or "manual"),
            interval_hours=int(self.interval_hours.value()),
            daily_at=(time.hour(), time.minute()),
        )

    def _sync_schedule_rows(self) -> None:
        """Show only the control the chosen mode actually uses.

        A greyed-out "Every 6 hours" beside "Once a day" is a question about
        which one wins, and the answer is not visible from the panel.
        """
        mode = str(self.schedule.currentData() or "manual")
        self.interval_hours.setVisible(mode == "interval")
        self.daily_at.setVisible(mode == "daily")
        for widget, label in ((self.interval_hours, "Every"), (self.daily_at, "At")):
            buddy = self._row_label(label)
            if buddy is not None:
                buddy.setVisible(widget.isVisible())

    def _row_label(self, text: str):
        for child in self.findChildren(QLabel):
            if child.text().rstrip(":") == text:
                return child
        return None

    def set_schedule_status(self, text: str) -> None:
        self.schedule_status.setText(text)

    def _schedule_changed(self, *_args) -> None:
        # The row-showing half is instant: it is a repaint, and delaying it
        # would make choosing "Once a day" feel broken. Only the persisting
        # half waits.
        self._sync_schedule_rows()
        self._save_schedule()

    def flush_pending(self) -> None:
        """Persist anything still inside the debounce window.

        **Called before the window closes.** Without this, changing the
        schedule and immediately closing loses the change - a worse bug than
        the sluggishness the debounce exists to fix.
        """
        self._save_schedule.flush()
