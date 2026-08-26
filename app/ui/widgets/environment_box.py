"""The Environment section of Settings: run doctor, and record a session.

Layer: L5

Split out of `settings_view.py` because that file crossed the 250-line guard,
and the guard is right: Settings had grown to six independent sections in one
constructor, and the two diagnostic ones have nothing to do with index roots or
Outlook archives.

Both controls exist for the same reason. Every bug found in this window so far
was found by a person clicking and reported from memory - and the detail that
mattered was usually the one nobody thought to mention. `doctor` says what the
environment is; the recorder says what actually happened. Together they turn a
bug report from a description into evidence.

Thin, like every other widget here: the subprocess call and the text formatting
live in `presenter.py`, and the recorder itself in `debug_recorder.py`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from PyQt6.QtCore import QThreadPool, pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox,
    QGroupBox,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
)

from app.ui.debug_recorder import DEBUG_RECORDING_HELP, SESSION_DIRNAME
from app.ui.presenter import doctor_lines, doctor_report
from app.ui.workers import CallableWorker, open_in_explorer, run

__all__ = ["EnvironmentBox"]


class EnvironmentBox(QGroupBox):
    """Run doctor; switch session recording on and off; open the folder."""

    recording_toggled = pyqtSignal(bool)

    def __init__(self, settings: Any, parent: Optional[Any] = None) -> None:
        super().__init__("Environment", parent)
        self._settings = settings
        self._doctor_running = False

        self.output = QPlainTextEdit()
        self.output.setReadOnly(True)
        self.output.setPlaceholderText("Run doctor to check the environment.")

        self.run_doctor_button = QPushButton("Run doctor")
        self.run_doctor_button.setToolTip(
            "Check everything the application needs and report what is wrong.\n\n"
            "Reads only: the index, the model cache, disk space and the "
            "converters. It changes nothing, so it is always safe to press.")
        self.run_doctor_button.clicked.connect(lambda _checked=False: self.run_doctor())

        self.recording = QCheckBox("Record what I do, to help diagnose a problem")
        self.recording.setToolTip(DEBUG_RECORDING_HELP)
        self.recording.stateChanged.connect(
            lambda _s: self.recording_toggled.emit(self.recording.isChecked())
        )

        self.recording_status = QLabel("")
        self.recording_status.setWordWrap(True)

        open_folder = QPushButton("Open the recordings folder")
        open_folder.setToolTip(
            "Open the folder holding the session recordings in Explorer.")
        open_folder.clicked.connect(self._open_sessions)

        layout = QVBoxLayout(self)
        layout.addWidget(self.run_doctor_button)
        layout.addWidget(self.recording)
        layout.addWidget(self.recording_status)
        layout.addWidget(open_folder)
        layout.addWidget(self.output)

    # -- doctor --------------------------------------------------------------

    def run_doctor(self) -> None:
        """Run doctor.py in a worker and show its report.

        **This was the frozen window.** It used to call
        `subprocess.run(timeout=120)` directly on the UI thread. Doctor probes
        Outlook over COM and opens LanceDB, so it is slow by nature - and while
        it ran the event loop was stopped, so the window did not repaint,
        Windows painted "Not Responding" over it, and Ctrl+C could not end it
        either because Qt never lets the interpreter run to see a signal. End
        Task was the only way out, and from the outside that is a crash.

        Nothing about the check needed to be synchronous. It just looked
        harmless, which is how UI-thread I/O usually gets written.
        """
        if self._doctor_running:
            return                               # a second click would run it twice
        self._doctor_running = True
        self.run_doctor_button.setEnabled(False)
        self.output.setPlainText("Running…")

        worker = CallableWorker(doctor_report, component="ui.doctor")
        worker.signals.finished.connect(
            lambda report: self.output.setPlainText("\n".join(doctor_lines(report)))
        )
        worker.signals.failed.connect(
            lambda error: self.output.setPlainText(
                f"{getattr(error, 'message', error)}\n\n"
                f"{getattr(error, 'suggestion', '')}\n\n"
                f"{getattr(error, 'details', '')}"
            )
        )
        worker.signals.done.connect(self._doctor_finished)
        run(QThreadPool.globalInstance(), worker)

    def _doctor_finished(self) -> None:
        self._doctor_running = False
        self.run_doctor_button.setEnabled(True)

    # -- session recording ---------------------------------------------------

    def set_recording_status(self, text: str) -> None:
        self.recording_status.setText(text)

    def sessions_folder(self) -> Path:
        return Path(getattr(self._settings, "log_path", ".")) / SESSION_DIRNAME

    def _open_sessions(self) -> None:
        folder = self.sessions_folder()
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self.recording_status.setText(f"Could not open {folder}: {exc}")
            return
        error = open_in_explorer(str(folder), select=False)
        if error is not None:
            self.recording_status.setText(getattr(error, "message", str(error)))
