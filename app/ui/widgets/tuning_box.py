r"""Index Tuning — one screen for everything that decides how fast a run goes.

Layer: L6 (UI)

**One screen, because the numbers interact.** Workers and ONNX threads
multiply; a batch is memory and so is the ceiling that governs it; and *what*
gets read decides the length of a run as surely as how quickly it is read.
Spread across three panels, somebody raises one control, gets a slower run, and
has nowhere to see why. Together, the oversubscription warning can appear under
the pair that caused it.

**Three modes, and switching is reversible.** Defaults and Auto-tune both mean
"the envelope decides"; Manual means "your number decides, clamped to what this
machine allows". A value typed in Manual is kept - stored but inert - when the
mode moves away from it, so trying Manual costs nothing and undoes cleanly.
That was the condition for having a Manual mode at all.

This class **composes and routes**. The bounds come from `app/core/envelope.py`,
the words from `app/ui/tuning.py`, and the controls from `tuning_groups.py` and
`long_run_box.py`; what lives here is which of them hears about a new profile.
"""

from __future__ import annotations

from typing import Any, Optional

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import (
    QComboBox,
    QGroupBox,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from app.ui.tuning import MODE_HELP, MODE_LABELS, MODES, footer_text
from app.ui.widgets.long_run_box import LongRunBox
from app.ui.widgets.machine_card import MachineCard
from app.ui.widgets.tuning_groups import ComputeBox, ResourcesBox, StrategyBox

__all__ = ["TuningBox"]


class TuningBox(QGroupBox):
    """The Index Tuning screen."""

    #: `{registry key: value}`, the shape `_settings_changed` writes.
    changed = pyqtSignal(dict)
    #: Coverage still emits `Settings`-field-named values, because the pipeline
    #: consumes that shape. Kept separate rather than merged so neither writer
    #: has to know about the other's naming.
    coverage_changed = pyqtSignal(dict)
    benchmark_requested = pyqtSignal()

    def __init__(self, settings: Any = None, parent: Optional[QWidget] = None
                 ) -> None:
        super().__init__("Index tuning", parent)
        self._profile: Any = None
        self._measured: dict = {}

        self.mode = QComboBox()
        self.mode.setObjectName("INDEX_TUNING_MODE")
        self.mode.setToolTip(
            "Defaults uses what this machine's specification implies.\n"
            "Auto-tune refines that with what past runs actually measured.\n"
            "Manual lets you set every control by hand, within the limits this\n"
            "machine allows.\n\n"
            "Switching back to Defaults keeps your manual values stored but\n"
            "inert, so experimenting costs nothing and undoes cleanly.")
        for value in MODES:
            self.mode.addItem(MODE_LABELS[value], value)
        self.mode.currentIndexChanged.connect(self._mode_changed)

        self.mode_help = QLabel("")
        self.mode_help.setWordWrap(True)
        self.mode_help.setObjectName("tuningModeHelp")

        #: §5d asks for this **in every mode**, Manual included: when the
        #: machine was last timed is a fact about the machine, not about the
        #: mode somebody has chosen.
        self.tuned_status = QLabel("Not yet timed on this computer.")
        self.tuned_status.setObjectName("tuningStatus")
        self.tuned_status.setWordWrap(True)

        self.machine = MachineCard()
        self.machine.profile_detected.connect(self.set_profile)
        self.machine.benchmark_requested.connect(self.benchmark_requested)

        self.compute = ComputeBox(settings)
        self.resources = ResourcesBox(settings)
        self.coverage = LongRunBox()
        self.strategy = StrategyBox(settings)

        for box in (self.compute, self.resources, self.strategy):
            box.changed.connect(self.changed)
        self.coverage.changed.connect(
            lambda: self.coverage_changed.emit(self.coverage.values()))

        #: §4f. **The evidence that makes Manual worth having.** Without it
        #: manual tuning is folklore: somebody moves a number, the run feels
        #: about the same, and nothing anywhere says where the time went.
        self.footer = QLabel(footer_text(None))
        self.footer.setObjectName("tuningFooter")
        self.footer.setWordWrap(True)

        layout = QVBoxLayout(self)
        layout.addWidget(self.mode)
        layout.addWidget(self.mode_help)
        layout.addWidget(self.tuned_status)
        layout.addWidget(self.machine)
        layout.addWidget(self.compute)
        layout.addWidget(self.resources)
        layout.addWidget(self.coverage)
        layout.addWidget(self.strategy)
        layout.addWidget(self.footer)

        if settings is not None:
            self.load(settings)

    # -- state --------------------------------------------------------------

    def load(self, settings: Any) -> None:
        """Fill every control from Settings without emitting on the way in."""
        self.mode.blockSignals(True)
        try:
            wanted = str(getattr(settings, "index_tuning_mode", "defaults"))
            found = self.mode.findData(wanted)
            self.mode.setCurrentIndex(found if found >= 0 else 0)
        finally:
            self.mode.blockSignals(False)

        self.compute.load(settings)
        self.resources.load(settings)
        self.coverage.load(settings)
        self.strategy.load(settings)
        self._apply()

    def current_mode(self) -> str:
        return str(self.mode.currentData() or "defaults")

    def set_profile(self, profile: Any, free_gb: int = 0) -> None:
        """Take a detected machine and re-bound everything against it."""
        self._profile = profile
        self.machine.show_profile(profile)
        self.resources.apply_profile(profile, free_gb)
        self._apply()

    def set_measured(self, measured: Optional[dict]) -> None:
        """§5's rates, when there are any. Auto-tune resolves against them."""
        self._measured = dict(measured or {})
        self._apply()

    def set_last_run(self, run: Optional[dict]) -> None:
        self.footer.setText(footer_text(run))

    def set_tuned_status(self, text: str) -> None:
        """"Tuned for this computer · last checked <date>", from §5d.

        Passed in rather than read here: it comes from the store, and this
        widget is built during `MainWindow.__init__` where nothing may touch
        a database.
        """
        self.tuned_status.setText(str(text or ""))

    def start_detection(self, index_path: Any = None) -> None:
        """Detect the machine, off the UI thread.

        Called on the first turn of the event loop rather than during
        construction: detection shells out to PowerShell, and the window's own
        rule is that nothing slow happens while it is being built.
        """
        self.machine.detect(index_path)

    # -- internals ----------------------------------------------------------

    def _apply(self) -> None:
        mode = self.current_mode()
        self.mode_help.setText(MODE_HELP.get(mode, ""))
        self.compute.apply_profile(self._profile, mode, self._measured)

    def _mode_changed(self, _index: int) -> None:
        self._apply()
        # **The mode is itself a setting**, so switching persists - otherwise
        # Manual would silently revert on the next start, which is exactly the
        # kind of quiet undo that makes people distrust a settings screen.
        self.changed.emit({"INDEX_TUNING_MODE": self.current_mode()})

    def flush_pending(self) -> None:
        """Persist anything still inside a debounce window, before closing."""
        for box in (self.compute, self.resources, self.strategy):
            box.flush()
