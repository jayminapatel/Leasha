r"""Settings, Models & AI: every model Leasha needs, each with its own Download.

Layer: L5 view - thin. Which models Leasha needs, whether each is here and how
one is fetched is `app/core/model_catalogue.py`; the words are
`app/ui/presenter/needed_models.py`. This draws one line per model and runs
the engine on a worker.

2026-10-08, the owner: "do all the models needed to run the system ship with
the release or during the install can you put the download buttons to download
all needed models individually and a button for all". Each line: the model's
title, what it is for, its name (small, faint), about how big it is, where it
stands ("On this computer", "Not downloaded", "Downloading 45%...", "Did not
download - <reason>"), and a Download button that becomes Stop while that
model downloads. Download all fetches every missing one in turn; Stop ends the
whole run. A model whose program library is not installed here (faces without
insightface) says so in place of a button that could not work.

**Nothing blocks the window and nothing runs by itself**, the same rules as
`model_download.DownloadRow`, whose shape this follows: whether each model is
here is asked on a `CallableWorker` the first time the box is shown and after
every download; a download runs on a worker, one at a time, and its progress
comes back by signal through a relay. Only a press of a button goes online.
"""

from __future__ import annotations

import threading
from typing import Any, Iterable, Optional

from PySide6.QtCore import QCoreApplication, QObject, Qt, QThreadPool, Signal
from PySide6.QtWidgets import (QGridLayout, QGroupBox, QHBoxLayout, QLabel, QPushButton,
                               QVBoxLayout, QWidget)

from app.core import model_fetch
from app.ui.presenter.needed_models import (
    NEEDED_DOWNLOAD_ALL_TIP, NEEDED_DOWNLOAD_TIP, NEEDED_LOOKING, NEEDED_MISSING,
    NEEDED_MODELS_INTRO, NEEDED_MODELS_TITLE, NEEDED_NO_LIBRARY, NEEDED_PRESENT,
    NEEDED_STARTING, NEEDED_STOP_ALL_TIP, NEEDED_STOP_TIP, NEEDED_STOPPED,
    NEEDED_STOPPING, needed_failed_tip, needed_failed_words, needed_list_failed,
    needed_progress_words,
    needed_size_words, needed_summary,
)
from app.ui.widgets.buttons import refresh_icon, style_button
from app.ui.workers import CallableWorker, run

__all__ = ["NeededModelsBox"]

_DOWNLOAD = "Download"
_DOWNLOAD_ALL = "Download all"
_STOP = "Stop"


class _Relay(QObject):
    """Carries a run's news from the worker thread to the window's thread."""

    started = Signal(str)            # key
    said = Signal(str, str)          # key, progress line
    ended = Signal(str, str)         # key, model_fetch.DONE | STOPPED
    failed = Signal(str, object)     # key, the exception


def _library_ready(catalogue: Any, model: Any, settings: Any) -> bool:
    """Worker thread. Can this model be used here at all?

    The catalogue answers if it knows how (`library_available`); otherwise the
    one model with a library of its own - faces, which need insightface - is
    asked of `model_fetch.faces_installed()`, which never loads anything.
    """
    check = getattr(catalogue, "library_available", None)
    if callable(check):
        try:
            return bool(check(model.key, settings))
        except Exception:                                    # noqa: BLE001
            return True
    words = f"{getattr(model, 'key', '')} {getattr(model, 'model', '')}".lower()
    if "face" not in words and "buffalo" not in words:
        return True
    installed = getattr(model_fetch, "faces_installed", None)
    if callable(installed):
        return bool(installed())
    from app.extract import face_detect

    return bool(face_detect.available())


class _Line:
    """One model's widgets on the grid."""

    def __init__(self, model: Any) -> None:
        self.key = str(model.key)
        self.title_words = str(model.title)
        self.title = QLabel(self.title_words)
        font = self.title.font()
        font.setBold(True)
        self.title.setFont(font)
        self.title.setWordWrap(True)
        self.purpose = QLabel(str(model.purpose))
        self.purpose.setWordWrap(True)
        self.name = QLabel(str(model.model))
        self.name.setObjectName("settingsHint")
        self.name.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.note = QLabel(str(getattr(model, "optional_note", "") or ""))
        self.note.setObjectName("settingsHint")
        self.note.setWordWrap(True)
        if not self.note.text():
            self.note.hide()      # never `show()` here: it has no parent yet
        self.size = QLabel(needed_size_words(getattr(model, "approx_mb", 0)))
        self.status = QLabel(NEEDED_LOOKING)
        self.status.setWordWrap(True)
        # Wide enough that "On this computer" and "Downloading 45%..." stay on
        # one line when the page is narrow; longer reasons wrap below.
        self.status.setMinimumWidth(
            self.status.fontMetrics().horizontalAdvance("Downloading 100%...") + 8)
        self.button = QPushButton(_DOWNLOAD)
        self.button.setObjectName(f"neededDownload_{self.key}")
        self.button.setToolTip(NEEDED_DOWNLOAD_TIP)
        self.button.setAccessibleName(f"Download {self.title_words}")
        style_button(self.button)
        self.button.setEnabled(False)

        self.text = QWidget()
        self.text.setObjectName("rowCell")     # the theme leaves it transparent
        column = QVBoxLayout(self.text)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(1)
        for label in (self.title, self.purpose, self.name, self.note):
            column.addWidget(label)

    def widgets(self) -> tuple[QWidget, ...]:
        """The four grid cells, in column order."""
        return (self.text, self.size, self.status, self.button)

    def show_as_stop(self, stop: bool) -> None:
        """The line's button reads Stop while its model downloads, Download otherwise."""
        self.button.setText(_STOP if stop else _DOWNLOAD)
        self.button.setToolTip(NEEDED_STOP_TIP if stop else NEEDED_DOWNLOAD_TIP)
        self.button.setAccessibleName(
            f"Stop downloading {self.title_words}" if stop else f"Download {self.title_words}")
        refresh_icon(self.button)


class NeededModelsBox(QGroupBox):
    """Models Leasha uses: one line per model, Download each or all."""

    #: Emitted after a run of downloads ends, so other model lists look again.
    models_changed = Signal()

    def __init__(self, settings: Any = None, parent: Optional[QWidget] = None) -> None:
        super().__init__(NEEDED_MODELS_TITLE, parent)
        self.setObjectName("neededModels")
        self._settings = settings
        self._models: list[Any] = []
        self._present: dict[str, bool] = {}
        self._blocked: set[str] = set()
        self._failed: dict[str, str] = {}
        self._failed_tips: dict[str, str] = {}
        self._stopped: set[str] = set()
        self._progress: dict[str, str] = {}
        self._lines: dict[str, _Line] = {}
        self._asked = False
        self._looking = False
        self._busy = False
        self._run_all = False
        self._current = ""
        self._stop = threading.Event()
        self._relay = _Relay(self)
        self._relay.started.connect(self._started)
        self._relay.said.connect(self._said)
        self._relay.ended.connect(self._ended)
        self._relay.failed.connect(self._failed_one)
        app = QCoreApplication.instance()
        if app is not None:
            # A download left running would hold the closing application open.
            app.aboutToQuit.connect(self._stop.set)
        self.destroyed.connect(lambda _o=None, e=self._stop: e.set())

        intro = QLabel(NEEDED_MODELS_INTRO)
        intro.setWordWrap(True)
        self.summary = QLabel(NEEDED_LOOKING)
        self.summary.setObjectName("neededSummary")
        self.summary.setWordWrap(True)
        self.download_all = QPushButton(_DOWNLOAD_ALL)
        self.download_all.setObjectName("neededDownloadAll")
        self.download_all.setToolTip(NEEDED_DOWNLOAD_ALL_TIP)
        self.download_all.setAccessibleName("Download all models not on this computer")
        self.download_all.clicked.connect(lambda _c=False: self._all_pressed())
        style_button(self.download_all)
        self.download_all.setEnabled(False)
        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.addWidget(self.summary, 1)
        top.addWidget(self.download_all)

        self._grid_host = QWidget()
        self._grid_host.setObjectName("rowCell")   # transparent on the card
        self._grid = QGridLayout(self._grid_host)
        self._grid.setContentsMargins(0, 4, 0, 0)
        self._grid.setHorizontalSpacing(16)
        self._grid.setVerticalSpacing(12)
        self._grid.setColumnStretch(0, 3)
        self._grid.setColumnStretch(2, 2)

        column = QVBoxLayout(self)
        column.addWidget(intro)
        column.addLayout(top)
        column.addWidget(self._grid_host)

    # -- what is listed, and what is here (worker) ---------------------------------

    @property
    def busy(self) -> bool:
        return self._busy

    def lines(self) -> dict[str, Any]:
        """Each model's line by key, for the window's tests."""
        return dict(self._lines)

    def showEvent(self, event: Any) -> None:            # noqa: N802 - Qt's naming
        super().showEvent(event)
        if not self._asked:
            self.refresh()

    def refresh(self) -> None:
        """Ask, on a worker, which models are needed and which are here."""
        if self._busy or self._looking:
            return
        self._asked = True
        self._looking = True
        worker = CallableWorker(self._look, self._settings, component="ui.models.needed")
        worker.signals.finished.connect(self._looked)
        worker.signals.failed.connect(self._look_failed)
        run(QThreadPool.globalInstance(), worker)

    @staticmethod
    def _look(settings: Any) -> tuple[list, dict, set]:
        """Worker thread: the catalogue, a disk look per model, a library check."""
        from app.core import model_catalogue

        models = list(model_catalogue.needed_models(settings))
        present = {m.key: bool(model_catalogue.is_present(m.key, settings)) for m in models}
        blocked = {m.key for m in models
                   if not present[m.key] and not _library_ready(model_catalogue, m, settings)}
        return models, present, blocked

    def _looked(self, found: Any) -> None:
        """UI thread, no I/O - the worker fetched it all."""
        self._looking = False
        models, present, blocked = found
        if [m.key for m in models] != [m.key for m in self._models]:
            self._build_lines(models)
        self._models = list(models)
        self._present = dict(present)
        self._blocked = set(blocked)
        for key, here in self._present.items():
            if here:
                self._failed.pop(key, None)
                self._stopped.discard(key)
        self._show()

    def _look_failed(self, error: Any) -> None:
        """UI thread: the catalogue could not be read; the summary says so."""
        self._looking = False
        self.summary.setText(needed_list_failed(error))

    def _build_lines(self, models: Iterable[Any]) -> None:
        """Rebuild the grid for a new list of models. Old lines are `deleteLater`d."""
        for line in self._lines.values():
            for widget in line.widgets():
                self._grid.removeWidget(widget)
                widget.deleteLater()
        self._lines = {}
        top = Qt.AlignmentFlag.AlignTop
        for row, model in enumerate(models):
            line = _Line(model)
            line.button.clicked.connect(lambda _c=False, k=line.key: self._line_pressed(k))
            self._grid.addWidget(line.text, row, 0, top)
            self._grid.addWidget(line.size, row, 1, top)
            self._grid.addWidget(line.status, row, 2, top)
            self._grid.addWidget(line.button, row, 3, top | Qt.AlignmentFlag.AlignRight)
            # Shown now, not on Qt's queued "show if not hidden" a turn later,
            # so the lines are whole the moment the answer lands.
            for widget in (line.text, line.size, line.status):
                widget.show()
            self._lines[line.key] = line

    # -- drawing --------------------------------------------------------------------

    def _status_of(self, key: str) -> str:
        """One model's status words, in priority order: downloading, failed, here, blocked ..."""
        if self._busy and key == self._current:
            return self._progress.get(key, NEEDED_STARTING)
        if key in self._failed:
            return self._failed[key]
        if self._present.get(key):
            return NEEDED_PRESENT
        if key in self._blocked:
            return NEEDED_NO_LIBRARY
        if key in self._stopped:
            return NEEDED_STOPPED
        if key not in self._present:
            return NEEDED_LOOKING
        return NEEDED_MISSING

    def _show(self) -> None:
        """Redraw every line and the Download all button from the box's state."""
        for key, line in self._lines.items():
            line.status.setText(self._status_of(key))
            if key in self._failed:
                tip = self._failed_tips.get(key, "")
            elif key in self._blocked and not self._present.get(key):
                # How to get the library, from the engine that needs it.
                tip = str(getattr(model_fetch, "FACES_NEED_INSIGHTFACE", "") or "")
            else:
                tip = ""
            line.status.setToolTip(tip)
            current = self._busy and key == self._current
            line.show_as_stop(current)
            wanted = current or (not self._present.get(key, True) and key not in self._blocked)
            line.button.setVisible(wanted)
            line.button.setEnabled(current or (wanted and not self._busy))
        if self._busy and self._stop.is_set():
            line = self._lines.get(self._current)
            if line is not None:
                line.button.setEnabled(False)
        if self._models and self._present:
            self.summary.setText(needed_summary(self._models, self._present, self._blocked))
        missing = self._missing_keys()
        if self._busy and self._run_all:
            self.download_all.setText(_STOP)
            self.download_all.setToolTip(NEEDED_STOP_ALL_TIP)
            self.download_all.setAccessibleName("Stop downloading the models")
            self.download_all.setEnabled(not self._stop.is_set())
        else:
            self.download_all.setText(_DOWNLOAD_ALL)
            self.download_all.setToolTip(NEEDED_DOWNLOAD_ALL_TIP)
            self.download_all.setAccessibleName("Download all models not on this computer")
            self.download_all.setEnabled(bool(missing) and not self._busy)
        refresh_icon(self.download_all)

    def _missing_keys(self) -> list[str]:
        """What Download all fetches: not here, and possible here. In list order."""
        return [m.key for m in self._models
                if self._present.get(m.key) is False and m.key not in self._blocked]

    # -- downloading ------------------------------------------------------------------

    def _line_pressed(self, key: str) -> None:
        if self._busy:
            if key == self._current:
                self._stop_pressed()
            return
        self.start([key], all_of_them=False)

    def _all_pressed(self) -> None:
        if self._busy:
            if self._run_all:
                self._stop_pressed()
            return
        self.start(self._missing_keys(), all_of_them=True)

    def start(self, keys: list[str], *, all_of_them: bool = False) -> None:
        """Download `keys` one after another on a worker. One run at a time."""
        keys = [k for k in keys if k]
        if not keys or self._busy:
            return
        self._busy = True
        self._run_all = all_of_them
        self._current = keys[0]
        self._stop.clear()
        for key in keys:
            self._failed.pop(key, None)
            self._stopped.discard(key)
            self._progress.pop(key, None)
        self._show()
        worker = CallableWorker(self._fetch, list(keys), self._stop, self._settings,
                                component="ui.models.needed")
        worker.signals.finished.connect(lambda _r: self._run_over())
        worker.signals.failed.connect(lambda _e: self._run_over())
        run(QThreadPool.globalInstance(), worker)

    def _fetch(self, keys: list[str], stop: threading.Event, settings: Any) -> int:
        """Worker thread. Blocks for as long as the downloads take.

        A failure is reported on its own line and the run carries on to the
        next model; Stop ends the run before the next one starts.
        """
        from app.core import model_catalogue

        relay = self._relay
        done = 0

        def tell(signal: Any, *args: Any) -> None:
            """Worker thread: emit through the relay; a box already gone stops the run."""
            try:
                signal.emit(*args)
            except RuntimeError:                # the box has gone; stop quietly
                stop.set()

        for key in keys:
            if stop.is_set():
                break
            tell(relay.started, key)
            try:
                result = model_catalogue.download(
                    key, settings,
                    on_progress=lambda line, k=key: tell(relay.said, k, str(line)),
                    should_stop=stop.is_set)
            except Exception as exc:                          # noqa: BLE001
                tell(relay.failed, key, exc)
                continue
            tell(relay.ended, key, str(result))
            done += 1
        return done

    def _started(self, key: str) -> None:
        """UI thread, via the relay: the run moved on to `key`."""
        if not self._busy:
            return
        self._current = key
        self._progress.pop(key, None)
        self._show()

    def _said(self, key: str, line: str) -> None:
        """UI thread, via the relay: a progress line for the model downloading now."""
        if self._busy and key == self._current and not self._stop.is_set():
            self._progress[key] = needed_progress_words(line)
            line_widgets = self._lines.get(key)
            if line_widgets is not None:
                line_widgets.status.setText(self._progress[key])

    def _ended(self, key: str, result: str) -> None:
        """UI thread, via the relay: one model finished or stopped."""
        self._progress.pop(key, None)
        if result == model_fetch.STOPPED:
            self._stopped.add(key)
        else:
            self._present[key] = True
            self._failed.pop(key, None)
        self._show()

    def _failed_one(self, key: str, error: Any) -> None:
        """UI thread, via the relay: one model failed; the run carries on."""
        self._progress.pop(key, None)
        self._failed[key] = needed_failed_words(error)
        self._failed_tips[key] = needed_failed_tip(error)
        self._show()

    def _stop_pressed(self) -> None:
        self._stop.set()
        line = self._lines.get(self._current)
        if line is not None:
            line.status.setText(NEEDED_STOPPING)
            line.button.setEnabled(False)
        if self._run_all:
            self.download_all.setEnabled(False)

    def _run_over(self) -> None:
        """UI thread: the run's worker finished; look again at what is here."""
        if self._busy and self._stop.is_set() and self._current:
            if self._current not in self._failed and not self._present.get(self._current):
                self._stopped.add(self._current)
        self._busy = False
        self._run_all = False
        self._current = ""
        self._show()
        self.models_changed.emit()
        self.refresh()
