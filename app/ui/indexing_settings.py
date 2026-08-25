"""When indexing runs, and how much of the machine it may use.

Layer: L6 (UI), driving L3

Its own module because `settings_view.py` had grown past the length a view is
allowed to be, and the rule that keeps views short is the rule that keeps logic
out of them. Nine controls with real consequences deserve their own panel
anyway.

**The wording on these controls is the feature.** Somebody choosing a memory
ceiling needs to know that exceeding it *pauses* rather than fails, or they will
set it far too high out of fear of losing a run - and then it protects nothing.
Every tooltip here says what happens when the limit is hit.

The decisions live elsewhere and are tested: `app/index/schedule.py` decides
when a run is due, `app/index/resources.py` decides whether it may proceed. This
is widgets.
"""

from __future__ import annotations

from typing import Any, Optional

from PyQt6.QtCore import QTime, pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox,
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
    """The Indexing group: a schedule, and four ceilings."""

    schedule_changed = pyqtSignal(object)      # a SchedulePolicy
    limits_changed = pyqtSignal(dict)          # the resource ceilings
    theme_changed = pyqtSignal(str)            # system | light | dark

    def __init__(self, parent: Optional[Any] = None) -> None:
        super().__init__("Indexing", parent)
        # --- when and how hard indexing runs
        #
        # Every control here is a ceiling, not a target. The wording matters as
        # much as the widget: somebody choosing a memory cap needs to know that
        # exceeding it pauses rather than fails, or they will set it too high
        # out of fear of losing a run.
        self.schedule = QComboBox()
        self.schedule.setObjectName("INDEX_SCHEDULE")
        self.schedule.addItem("Only when I ask", "manual")
        self.schedule.addItem("Once, shortly after opening", "startup")
        self.schedule.addItem("Every few hours", "interval")
        self.schedule.addItem("Once a day", "daily")
        self.schedule.currentIndexChanged.connect(self._schedule_changed)

        self.interval_hours = QSpinBox()
        self.interval_hours.setObjectName("INDEX_INTERVAL_HOURS")
        self.interval_hours.setRange(1, 168)
        self.interval_hours.setSuffix(" hours")
        self.interval_hours.valueChanged.connect(self._schedule_changed)

        self.daily_at = QTimeEdit()
        self.daily_at.setObjectName("INDEX_DAILY_AT")
        self.daily_at.setDisplayFormat("HH:mm")
        self.daily_at.timeChanged.connect(self._schedule_changed)

        self.schedule_status = QLabel("")
        self.schedule_status.setWordWrap(True)

        self.workers = QSpinBox()
        self.workers.setObjectName("INDEX_WORKERS")
        self.workers.setRange(0, 32)
        self.workers.setSpecialValueText("Automatic")
        self.workers.setToolTip(
            "How many files are read at once.\n"
            "Automatic uses half your cores, capped at four - deliberately not all of\n"
            "them, so the machine stays usable while indexing."
        )

        self.memory_mb = QSpinBox()
        self.memory_mb.setObjectName("INDEX_MEMORY_MB")
        self.memory_mb.setRange(256, 32_000)
        self.memory_mb.setSingleStep(100)
        self.memory_mb.setSuffix(" MB")
        self.memory_mb.setToolTip(
            "Memory ceiling. Above it indexing PAUSES and resumes - it does not fail,\n"
            "and nothing already indexed is lost."
        )

        self.cpu_percent = QSpinBox()
        self.cpu_percent.setObjectName("INDEX_CPU_PERCENT")
        self.cpu_percent.setRange(0, 100)
        self.cpu_percent.setSuffix(" %")
        self.cpu_percent.setSpecialValueText("No limit")
        self.cpu_percent.setToolTip(
            "Pause while the whole machine is busier than this, so indexing gets out\n"
            "of the way of whatever you are doing. 0 turns the check off."
        )

        self.min_free_gb = QSpinBox()
        self.min_free_gb.setObjectName("MIN_FREE_GB")
        self.min_free_gb.setRange(1, 500)
        self.min_free_gb.setSuffix(" GB")
        self.min_free_gb.setToolTip(
            "Indexing stops rather than filling the disk, and everything already\n"
            "indexed is kept - so this is a floor to protect the machine, not a\n"
            "budget for the index."
        )

        self.pause_on_battery = QCheckBox("Pause while on battery")
        self.pause_on_battery.setObjectName("INDEX_PAUSE_ON_BATTERY")
        self.theme = QComboBox()
        self.theme.addItem("Follow Windows", "system")
        self.theme.addItem("Always light", "light")
        self.theme.addItem("Always dark", "dark")
        self.theme.setToolTip(
            "The app follows your Windows light/dark setting by default,\n"
            "and switches immediately when you change it."
        )
        self.theme.currentIndexChanged.connect(
            lambda _i: self.theme_changed.emit(str(self.theme.currentData() or "system"))
        )

        self.low_priority = QCheckBox("Run at low priority")
        self.low_priority.setObjectName("INDEX_LOW_PRIORITY")
        self.low_priority.setToolTip(
            "Let everything else have the processor and the disk first.\n"
            "Leave this on unless indexing is the only thing this machine does."
        )

        # **Emit once the person has stopped, not on every notch.**
        #
        # Each emission persists five settings keys, so holding an arrow - which
        # auto-repeats about ten times a second - asked the UI thread for fifty
        # committed SQLite transactions a second. The window stopped repainting
        # between them, which is what "the up down buttons are not always
        # responsive" actually was. The buttons were fine; the handler was not.
        self._save_limits = Debounced(
            lambda: self.limits_changed.emit(self.current_limits()), parent=self)
        self._save_schedule = Debounced(
            lambda: self.schedule_changed.emit(self.current_policy()), parent=self)

        for widget in (
            self.workers, self.memory_mb, self.cpu_percent, self.interval_hours,
            self.min_free_gb,
        ):
            # Typing `1500` otherwise emits at 1, 15, 150 and 1500 - four rounds
            # of writes for one number, three of them values nobody chose.
            widget.setKeyboardTracking(False)

        for widget in (
            self.workers, self.memory_mb, self.cpu_percent, self.min_free_gb,
        ):
            widget.valueChanged.connect(self._limits_changed)
        self.pause_on_battery.stateChanged.connect(self._limits_changed)
        self.low_priority.stateChanged.connect(self._limits_changed)

        index_form = QFormLayout(self)
        index_form.addRow("Run", self.schedule)
        index_form.addRow("Every", self.interval_hours)
        index_form.addRow("At", self.daily_at)
        index_form.addRow(self.schedule_status)
        index_form.addRow("Files at once", self.workers)
        index_form.addRow("Memory ceiling", self.memory_mb)
        index_form.addRow("Pause above", self.cpu_percent)
        index_form.addRow("Stop below", self.min_free_gb)
        index_form.addRow(self.pause_on_battery)
        index_form.addRow(self.low_priority)
        index_form.addRow("Appearance", self.theme)


    def load_indexing(self, settings: Any) -> None:
        """Fill the controls from Settings, without emitting on the way in.

        Blocking signals matters: setting nine widgets would otherwise fire nine
        change notifications before the panel has even been shown, each one
        writing a partially-populated policy back over the stored one.
        """
        from app.core.config import parse_daily_at

        widgets = (
            self.schedule, self.interval_hours, self.daily_at, self.workers,
            self.memory_mb, self.cpu_percent, self.pause_on_battery,
            self.low_priority, self.theme, self.min_free_gb,
        )
        for widget in widgets:
            widget.blockSignals(True)
        try:
            mode = str(getattr(settings, "index_schedule", "manual"))
            index = self.schedule.findData(mode)
            self.schedule.setCurrentIndex(index if index >= 0 else 0)
            self.interval_hours.setValue(int(getattr(settings, "index_interval_hours", 6)))
            hour, minute = parse_daily_at(str(getattr(settings, "index_daily_at", "02:00"))) or (2, 0)
            self.daily_at.setTime(QTime(hour, minute))
            self.workers.setValue(int(getattr(settings, "index_workers", 0)))
            self.min_free_gb.setValue(int(getattr(settings, "min_free_gb", 5)))
            self.memory_mb.setValue(int(getattr(settings, "index_memory_mb", 1500)))
            self.cpu_percent.setValue(int(getattr(settings, "index_cpu_percent", 80)))
            self.pause_on_battery.setChecked(bool(getattr(settings, "index_pause_on_battery", True)))
            self.low_priority.setChecked(bool(getattr(settings, "index_low_priority", True)))
            theme = str(getattr(settings, "ui_theme", "system"))
            index = self.theme.findData(theme)
            self.theme.setCurrentIndex(index if index >= 0 else 0)
        finally:
            for widget in widgets:
                widget.blockSignals(False)
        # Anything queued before this load was about the *old* values. Firing it
        # now would write them straight back over what was just loaded.
        self._save_limits.cancel()
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

    def current_limits(self) -> dict:
        return {
            "index_workers": int(self.workers.value()),
            "index_memory_mb": int(self.memory_mb.value()),
            "index_cpu_percent": int(self.cpu_percent.value()),
            "index_pause_on_battery": bool(self.pause_on_battery.isChecked()),
            "index_low_priority": bool(self.low_priority.isChecked()),
            "min_free_gb": int(self.min_free_gb.value()),
        }

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

    def _limits_changed(self, *_args) -> None:
        self._save_limits()

    def flush_pending(self) -> None:
        """Persist anything still inside the debounce window.

        **Called before the window closes.** Without this, changing a ceiling
        and immediately closing loses the change - a worse bug than the
        sluggishness the debounce exists to fix.
        """
        self._save_schedule.flush()
        self._save_limits.flush()

