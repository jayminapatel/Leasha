r"""Indexing > Tuning > Devices: which processor each model runs on, and a test.

Layer: L5

2026-10-04, the owner: "put the gpu flag in the settings and also have a test
button so this can be tested on this machine and a new machine for indexing".
One row per model the indexer and search use, each with its own choice
(Automatic / Processor / Graphics card, `model_devices`) and what the last test
on this machine measured; a Test button per row and one for the whole machine.

**No I/O here.** The stored results are read, and the test is run, on workers
(`device_test.run_device_test` loads every model twice - minutes, and the
window pauses while each loads, which the status line says before it starts).
"""

from __future__ import annotations

from typing import Any, Optional

from PySide6.QtCore import QThreadPool, Signal
from PySide6.QtWidgets import (QComboBox, QGridLayout, QGroupBox, QHBoxLayout, QLabel,
                             QPushButton, QVBoxLayout)

from app.core.model_devices import MODELS

__all__ = ["DeviceBox", "result_text", "CHOICE_LABELS"]

CHOICE_LABELS = (("auto", "Automatic"), ("cpu", "Processor"), ("gpu", "Graphics card"))
INTRO = ("Which processor each model runs on. Automatic uses what Test this machine "
         "measured here, and \"Run models on\" until it has run. Takes effect on restart.")
TESTING = ("Testing on the processor and the graphics card - about two minutes. "
           "The window may pause for a few seconds while each model loads.")
UNTESTED = "Not yet tested on this computer."


def result_text(entry: Optional[dict]) -> str:
    """`Processor 8.9 s · graphics card 3.9 s - uses the graphics card`."""
    if not entry:
        return UNTESTED
    parts = []
    if entry.get("cpu_s") is not None:
        parts.append(f"Processor {float(entry['cpu_s']):.2f} s")
    if entry.get("gpu_s") is not None:
        parts.append(f"graphics card {float(entry['gpu_s']):.2f} s")
    uses = "the graphics card" if entry.get("winner") == "gpu" else "the processor"
    line = " · ".join(parts) + (f" - uses {uses}" if parts else "")
    note = str(entry.get("note") or "")
    if note:
        line = f"{line} ({note})" if line else note
    return line


class DeviceBox(QGroupBox):
    """The Devices group. `changed` carries the `.env` values, as its siblings do."""

    changed = Signal(dict)

    def __init__(self, settings: Any = None, parent: Optional[Any] = None) -> None:
        super().__init__("Devices", parent)
        self._settings = settings
        self._pool = QThreadPool.globalInstance()
        self._busy = False
        self.combos: dict[str, QComboBox] = {}
        self.results: dict[str, QLabel] = {}
        self.tests: dict[str, QPushButton] = {}

        intro = QLabel(INTRO)
        intro.setWordWrap(True)
        grid = QGridLayout()
        for row, (model, key, label) in enumerate(MODELS):
            combo = QComboBox()
            combo.setObjectName(key)
            for value, text in CHOICE_LABELS:
                combo.addItem(text, value)
            combo.setToolTip(f"Which processor runs {label.lower()}. Takes effect on restart.")
            combo.currentIndexChanged.connect(lambda _i: self.changed.emit(self.values()))
            result = QLabel(UNTESTED)
            result.setObjectName(f"{key}_result")
            result.setWordWrap(True)
            test = QPushButton("Test")
            test.setToolTip(f"Time {label.lower()} on the processor and the graphics card")
            test.clicked.connect(lambda _c=False, m=model: self.test([m]))
            grid.addWidget(QLabel(label), row, 0)
            grid.addWidget(combo, row, 1)
            grid.addWidget(test, row, 2)
            grid.addWidget(result, row, 3)
            self.combos[model], self.results[model], self.tests[model] = combo, result, test
        grid.setColumnStretch(3, 1)

        self.test_all = QPushButton("Test this machine")
        self.test_all.setObjectName("deviceTestAll")
        self.test_all.setToolTip("Time every model on the processor and the graphics card, "
                                 "and use the faster one that gives the same answer")
        self.test_all.clicked.connect(lambda _c=False: self.test(None))
        self.status = QLabel("")
        self.status.setWordWrap(True)
        row = QHBoxLayout()
        row.addWidget(self.test_all)
        row.addWidget(self.status, 1)

        layout = QVBoxLayout(self)
        layout.addWidget(intro)
        layout.addLayout(grid)
        layout.addLayout(row)
        if settings is not None:
            self.load(settings)

    # -- state ------------------------------------------------------------------------

    def load(self, settings: Any) -> None:
        """Fill the choices from Settings, quietly, and read the last test on a worker."""
        self._settings = settings
        for model, combo in self.combos.items():
            combo.blockSignals(True)
            try:
                value = str(getattr(settings, f"device_{model}", "auto") or "auto")
                found = combo.findData(value)
                combo.setCurrentIndex(found if found >= 0 else 0)
            finally:
                combo.blockSignals(False)
        self._read_results()

    def values(self) -> dict:
        return {key: str(self.combos[model].currentData() or "auto")
                for model, key, _label in MODELS}

    # -- the test ------------------------------------------------------------------------

    def _read_results(self) -> None:
        """Read the saved test results on a worker (`_results_for_this_machine`);
        `_stored` paints them unless a test has started since.
        """
        from app.ui.later import when_done
        from app.ui.workers import CallableWorker, run

        if self._settings is None:
            return
        worker = CallableWorker(_results_for_this_machine, self._settings,
                                component="ui.devices.read")
        when_done(self, worker, finished=self._stored)
        run(self._pool, worker)

    def _stored(self, results: Any) -> None:
        """What was saved - unless a test has started since, whose answer is newer.
        2026-10-05, the full suite: a slow read of the saved results landed after
        a quick test and painted "Not yet tested" over its answer."""
        if self._busy or getattr(self, "_tested_here", False):
            return
        self.show_results(results)

    def test(self, models: Optional[list[str]] = None) -> bool:
        """Run the test on a worker. False when one is already running."""
        from app.index.device_test import run_device_test
        from app.ui.later import when_done
        from app.ui.workers import CallableWorker, run

        if self._busy or self._settings is None:
            return False
        self._set_busy(True)
        self.status.setText(TESTING)
        worker = CallableWorker(run_device_test, self._settings, models,
                                component="ui.devices.test")
        when_done(self, worker, finished=self._tested,
                  failed=lambda error: self._failed(error))
        run(self._pool, worker)
        return True

    def _tested(self, results: Any) -> None:
        """The test's worker finished: paint its results and release the buttons."""
        self._tested_here = True
        self._set_busy(False)
        self.show_results(results)
        self.status.setText("Tested. Rows on Automatic use the faster processor that "
                            "gave the same answer, from the next restart.")

    def _failed(self, error: Any) -> None:
        """The test's worker raised: say so in the status line, never a dialog."""
        self._set_busy(False)
        self.status.setText(f"The test could not finish: {getattr(error, 'message', error)}")

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self.test_all.setEnabled(not busy)
        for button in self.tests.values():
            button.setEnabled(not busy)

    def show_results(self, results: Any) -> None:
        """Paint a stored or fresh result. UI thread, no I/O."""
        models = (results or {}).get("models") or {} if isinstance(results, dict) else {}
        for model, label in self.results.items():
            label.setText(result_text(models.get(model)))
        gpu = (results or {}).get("gpu") if isinstance(results, dict) else None
        if gpu and gpu != "usable":
            self.status.setText(f"No graphics card to test: {gpu}.")


def _results_for_this_machine(settings: Any) -> dict:
    """The stored test if it was this computer's, else `{}`. Worker."""
    from app.core.model_devices import load_results, machine_fingerprint

    results = load_results(settings)
    return results if results.get("fingerprint") == machine_fingerprint() else {}
