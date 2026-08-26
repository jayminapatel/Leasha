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
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
)

from app.ui.debug_recorder import DEBUG_RECORDING_HELP, SESSION_DIRNAME
from app.ui.presenter import (
    clear_logs,
    doctor_lines,
    doctor_report,
    logs_cleared_message,
    logs_summary,
)
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

        # -- the logs ---------------------------------------------------------
        # Asked for directly: *"there needs to be a button to clear logs"*. Every
        # run writes a file and none of them are ever removed, so the folder only
        # grows - and the person who notices is the person looking for disk
        # space, who then has to be told which folder is safe to empty.
        self.logs_status = QLabel("")
        self.logs_status.setWordWrap(True)
        self.logs_status.setObjectName("settingsHint")

        self.clear_logs_button = QPushButton("Clear logs")
        self.clear_logs_button.setToolTip(
            "Delete the log files in the logs folder.\n\n"
            "Nothing indexed is touched and no setting changes - these are "
            "records of what the application did, not data. Today's files are "
            "kept, because this session is still writing to them.")
        self.clear_logs_button.clicked.connect(
            lambda _checked=False: self.clear_logs())

        open_logs = QPushButton("Open the logs folder")
        open_logs.setToolTip("Open the logs folder in Explorer.")
        open_logs.clicked.connect(lambda _c=False: self._open_folder(self.logs_folder()))

        logs_row = QHBoxLayout()
        logs_row.addWidget(self.clear_logs_button)
        logs_row.addWidget(open_logs)
        logs_row.addStretch(1)

        layout = QVBoxLayout(self)
        layout.addWidget(self.run_doctor_button)
        layout.addWidget(self.recording)
        layout.addWidget(self.recording_status)
        layout.addWidget(open_folder)
        layout.addWidget(self.logs_status)
        layout.addLayout(logs_row)
        layout.addWidget(self.output)

        self.refresh_logs()

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
        self._open_folder(self.sessions_folder(), on_error=self.recording_status)

    def _open_folder(self, folder: Path, on_error: Any = None) -> None:
        target = on_error if on_error is not None else self.logs_status
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            target.setText(f"Could not open {folder}: {exc}")
            return
        error = open_in_explorer(str(folder), select=False)
        if error is not None:
            target.setText(getattr(error, "message", str(error)))

    # -- the logs -------------------------------------------------------------

    def logs_folder(self) -> Path:
        return Path(getattr(self._settings, "log_path", "."))

    def refresh_logs(self) -> None:
        """Say how much is in the logs folder. **On a worker, always.**

        It walks a folder, and a folder somebody has left for a year is not a
        folder to walk on the UI thread - which is the mistake `run_doctor`
        documents at length. A label is never worth a frozen window.
        """
        worker = CallableWorker(logs_summary, self.logs_folder(),
                                component="ui.logs.size")
        worker.signals.finished.connect(self.logs_status.setText)
        worker.signals.failed.connect(lambda _e: self.logs_status.setText(""))
        run(QThreadPool.globalInstance(), worker)

    def clear_logs(self) -> None:
        """Ask, then delete the log files, then say what went.

        **The confirmation names what is not at risk.** "Clear logs" sits on a
        Settings page next to controls that change how the index is built, and
        somebody who has just been warned about resetting an index is right to
        be careful. Nothing indexed is touched and no setting moves; these are
        records of what the application did.
        """
        folder = self.logs_folder()
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle("Clear the logs?")
        box.setText(f"Delete the log files in {folder}?")
        box.setInformativeText(
            "Nothing indexed is touched and no setting changes - these are "
            "records of what the application did.\n\n"
            "What it costs is the history: if something has been going wrong, "
            "the evidence goes with them. Today's files are kept, because this "
            "session is still writing to them.")
        box.setStandardButtons(
            QMessageBox.StandardButton.Cancel | QMessageBox.StandardButton.Yes)
        box.setDefaultButton(QMessageBox.StandardButton.Cancel)
        if box.exec() != QMessageBox.StandardButton.Yes:
            return

        self.clear_logs_button.setEnabled(False)
        self.logs_status.setText("Clearing…")

        worker = CallableWorker(clear_logs, folder, keep=self._logs_in_use(),
                                component="ui.logs.clear")
        worker.signals.finished.connect(
            lambda outcome: self.logs_status.setText(logs_cleared_message(outcome)))
        worker.signals.failed.connect(
            lambda error: self.logs_status.setText(
                f"{getattr(error, 'message', error)} "
                f"{getattr(error, 'suggestion', '')}".strip()))
        worker.signals.done.connect(self._logs_cleared)
        run(QThreadPool.globalInstance(), worker)

    def _logs_cleared(self) -> None:
        self.clear_logs_button.setEnabled(True)

    def _logs_in_use(self) -> tuple:
        """The files this session is writing to, which must survive.

        Asked of the logging system rather than guessed from today's date: the
        run log's name carries a timestamp and a process id, and a session that
        started before midnight is writing to yesterday's application log. A
        rule reconstructed here would be wrong in exactly those two cases.
        """
        from app.core.logging import open_log_files

        try:
            return tuple(open_log_files())
        except Exception:                        # noqa: BLE001 - keep nothing rather than fail
            return ()
