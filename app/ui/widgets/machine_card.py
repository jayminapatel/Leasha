r"""What this machine is — read-only, and never read on the UI thread.

Layer: L6 (UI)

**Detection shells out.** `compute_profile` asks PowerShell for the display
adapters and for the index volume's seek penalty, and either call can take
seconds on a machine with a sleeping disk or a locked-down policy. That is a
freeze, so both buttons here go through `CallableWorker` and the card says what
it is doing while it waits - §4b's rule, and the reason it is a rule.

The card holds **no policy**. It renders `tuning.machine_line`, which is pure
and tested; every number it shows is a fact from detection rather than anything
this screen decided.
"""

from __future__ import annotations

from typing import Any, Optional

from PySide6.QtCore import Qt, QThreadPool, Signal
from PySide6.QtWidgets import (
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.ui.tuning import machine_line
from app.ui.workers import CallableWorker, run

__all__ = ["MachineCard"]


class MachineCard(QGroupBox):
    """The ComputeProfile in one line, with Re-detect and Benchmark now."""

    #: A freshly detected profile. The screen re-resolves its controls against
    #: it; the card does not decide what that means.
    profile_detected = Signal(object)
    #: Asked for, not performed. Benchmarking wants the settings and the model
    #: cache, which the window owns and this widget deliberately does not.
    benchmark_requested = Signal()

    def __init__(self, profile: Any = None,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__("This machine", parent)
        # Its own single-threaded pool, for the same reason the indexing view
        # has one: detection must never take a slot from a search somebody is
        # waiting on, and two detections at once are two PowerShell processes
        # answering the same question.
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(1)

        self.line = QLabel("")
        self.line.setObjectName("machineLine")
        self.line.setWordWrap(True)
        # Selectable so it can be pasted into a support message. It is the
        # first thing anybody would be asked for.
        self.line.setTextInteractionFlags(
            self.line.textInteractionFlags()
            | Qt.TextInteractionFlag.TextSelectableByMouse)

        self.detected_note = QLabel("")
        self.detected_note.setWordWrap(True)
        self.detected_note.setObjectName("machineNote")

        self.redetect = QPushButton("Re-detect")
        self.redetect.setToolTip(
            "Look at the machine again. Worth doing after adding memory, "
            "installing a graphics driver, or moving the index to another "
            "drive - the stored answer is only refreshed when something it "
            "watches changes.")
        self.redetect.clicked.connect(lambda _c=False: self.detect(refresh=True))

        self.benchmark = QPushButton("Benchmark now")
        self.benchmark.setToolTip(
            "Time this machine's model on a small sample. It is the only way "
            "to know whether the graphics card is actually faster here - the "
            "specification sheet cannot tell you.")
        self.benchmark.clicked.connect(
            lambda _c=False: self.benchmark_requested.emit())

        buttons = QHBoxLayout()
        buttons.addWidget(self.redetect)
        buttons.addWidget(self.benchmark)
        buttons.addStretch(1)

        layout = QVBoxLayout(self)
        layout.addWidget(self.line)
        layout.addWidget(self.detected_note)
        layout.addLayout(buttons)

        if profile is not None:
            self.show_profile(profile)
        else:
            # **Not blank, and not detected here either.** A blank line is
            # indistinguishable from a broken one, and detecting inside a
            # constructor is the UI-thread freeze this whole card exists to
            # avoid. The screen calls `detect()` once the event loop is up.
            self.line.setText("Looking at this machine…")

    # -- detection ----------------------------------------------------------

    def detect(self, index_path: Any = None, *, refresh: bool = False) -> None:
        """Re-detect, off the UI thread. Safe to call from the first turn of
        the event loop, which is where the screen calls it.

        `refresh=True` (the Re-detect button) makes `compute_profile.detect`
        ask the machine again; without it the two PowerShell probes are the
        ones this process already ran (2026-09-20: once per process)."""
        self.redetect.setEnabled(False)
        self.line.setText("Looking at this machine…")

        from app.core.compute_profile import detect as _detect

        worker = CallableWorker(_detect, index_path, component="ui.tuning",
                                refresh=refresh)
        worker.signals.finished.connect(self._detected)
        worker.signals.failed.connect(self._failed)
        worker.signals.done.connect(lambda: self.redetect.setEnabled(True))
        run(self._pool, worker)

    def _detected(self, profile: Any) -> None:
        """UI thread: the worker's profile. Painted, then handed up for re-bounding."""
        self.show_profile(profile)
        self.profile_detected.emit(profile)

    def _failed(self, error: Any) -> None:
        # **Detection failing is not an error dialogue.** Everything downstream
        # already treats an unknown field as "no", so the screen still works;
        # what would be wrong is showing a stale line as though it were fresh.
        self.line.setText("This machine could not be examined.")
        self.detected_note.setText(
            f"{getattr(error, 'message', error)} Tuning still works - the "
            f"unknown parts are treated as absent, which is the cautious "
            f"answer.")

    def show_profile(self, profile: Any) -> None:
        """Render a profile that somebody else detected."""
        self.line.setText(machine_line(profile))
        unknowns = tuple(getattr(profile, "unknowns", ()) or ())
        self.detected_note.setText(
            "Could not be detected: " + ", ".join(str(u) for u in unknowns)
            if unknowns else "")
