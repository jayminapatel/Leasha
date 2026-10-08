r"""Download, Stop, and one line saying where a model download has got to.

Layer: L5 view - thin. What a download *is* lives in `app/core/model_fetch.py`,
which needs no display; this is two buttons and a sentence.

**The owner, 2026-09-29:** model lists are drop-downs only, the other options
are listed, and there is *"a mechanism to download"*. This row sits under each
model drop-down in Settings. Two shapes:

* **One model** (`set_target`) - the meaning model, the reranker, the speech
  model. The row says whether the chosen one is on this computer, and Download
  fetches it if it is not.
* **A menu of models** (`set_offers`) - the Ollama lists, which can only choose
  what Ollama has installed. Download opens a menu of the listed models that are
  not installed yet; choosing one pulls it, and the owner of the row refreshes
  its list when `finished` says so.

**Nothing blocks the window and nothing runs by itself.** Whether a model is
here is a disk look (or one loopback request to Ollama) and a download is
minutes of network: both run in a `CallableWorker`, and progress arrives by
signal. A download starts only from a press of Download, and its tooltip says
that this is the one thing on the page that goes online.
"""

from __future__ import annotations

import threading
from typing import Any, Callable, Iterable, Optional

from PySide6.QtCore import QCoreApplication, QObject, QThreadPool, Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QMenu, QPushButton, QWidget

from app.core import model_fetch
from app.ui.widgets.buttons import style_button
from app.ui.workers import CallableWorker, run

__all__ = ["DownloadRow", "DOWNLOAD_TIP"]

DOWNLOAD_TIP = ("Downloads the model from the internet, now, once. This is the only "
                "thing on this page that goes online, and only when you press it; "
                "search and indexing carry on while it runs.")


class _Relay(QObject):
    """Carries progress from the worker thread to the window's thread."""

    said = Signal(str)


def _size_words(name: str) -> str:
    """`" (about 1.2 GB)"` from the approximate table, or `""` when unknown."""
    mb = model_fetch.APPROX_MB.get(name, 0)
    if not mb:
        return ""
    return f" (about {mb / 1024:.1f} GB)" if mb >= 1024 else f" (about {mb} MB)"


class DownloadRow(QWidget):
    """[Download] [Stop]  where it has got to."""

    #: `(model name, "done" | "stopped")` once a download has ended.
    finished = Signal(str, str)

    def __init__(self, kind: str, *, model_cache: Any = None,
                 client_factory: Optional[Callable[[], Any]] = None,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.kind = kind
        self._model_cache = model_cache
        self._client_factory = client_factory
        self._target = ""
        self._offers: list[tuple[str, str]] = []
        self._busy = False
        self._probe_generation = 0
        self._pending = False
        self._stop = threading.Event()
        self._relay = _Relay(self)
        self._relay.said.connect(self._say)
        app = QCoreApplication.instance()
        if app is not None:
            # A download left running would hold the thread pool - and so the
            # closing application - open until it finished.
            app.aboutToQuit.connect(self._stop.set)
        self.destroyed.connect(lambda _o=None, e=self._stop: e.set())

        self.download = QPushButton("Download")
        self.download.setObjectName(f"download_{kind}")
        self.download.setToolTip(DOWNLOAD_TIP)
        self.download.clicked.connect(lambda _c=False: self._pressed())
        self.stop = QPushButton("Stop")
        self.stop.setObjectName(f"stop_download_{kind}")
        self.stop.setToolTip("Stop this download. What has arrived is kept, so "
                             "Download carries on from there next time.")
        self.stop.clicked.connect(lambda _c=False: self._stop_pressed())
        self.stop.hide()
        self.status = QLabel("")
        self.status.setObjectName(f"download_status_{kind}")
        self.status.setWordWrap(True)
        self.menu = QMenu(self)

        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(self.download)
        row.addWidget(self.stop)
        row.addWidget(self.status, 1)
        style_button(self.download)
        style_button(self.stop)
        self.download.setEnabled(False)

    # -- what it offers ------------------------------------------------------------

    @property
    def busy(self) -> bool:
        return self._busy

    def set_model_cache(self, model_cache: Any) -> None:
        self._model_cache = model_cache

    def set_target(self, name: str) -> None:
        """The one model this row is about. Asks, on a worker, whether it is here."""
        self._target = str(name or "").strip()
        if self._busy:
            return
        if not self._target:
            self.download.setEnabled(False)
            self.status.setText("")
            return
        self.download.setEnabled(False)
        self._probe_generation += 1
        if not self.isVisible():
            # Asked when the row is first seen, not while a page is being
            # built that may never be opened.
            self._pending = True
            self.status.setText("")
            return
        self._pending = False
        self.status.setText(f"Looking for {self._target} on this computer...")
        worker = CallableWorker(self._is_here, self._target, component="ui.models.download")
        worker.signals.finished.connect(
            lambda here, g=self._probe_generation, n=self._target: self._probed(g, n, here))
        worker.signals.failed.connect(
            lambda _e, g=self._probe_generation, n=self._target: self._probed(g, n, False))
        run(QThreadPool.globalInstance(), worker)

    def showEvent(self, event: Any) -> None:       # noqa: N802 - Qt's naming
        """First shown: ask the question `set_target` put off while the row was hidden."""
        super().showEvent(event)
        if self._pending and self._target and not self._busy:
            self.set_target(self._target)

    def _is_here(self, name: str) -> bool:
        """Worker thread: a disk look, or one request to Ollama."""
        client = self._client_factory() if self._client_factory is not None else None
        return model_fetch.present(self.kind, name, model_cache=self._model_cache,
                                   client=client)

    def _probed(self, generation: int, name: str, here: Any) -> None:
        """UI thread: whether the model is here. Dropped if the target changed meanwhile."""
        if generation != self._probe_generation or self._busy:
            return                               # a newer choice has been made since
        if here:
            self.status.setText(f"{name} is on this computer.")
            self.download.setEnabled(False)
            return
        self.status.setText(
            f"{name} is not on this computer yet{_size_words(name)}. Download fetches "
            "it; nothing is downloaded unless you press it.")
        self.download.setEnabled(True)

    def set_offers(self, offers: Iterable[tuple[str, str]]) -> None:
        """`(name, note)` for each listed model not installed. Download becomes a menu."""
        self._offers = [(str(n), str(note or "")) for n, note in offers if str(n).strip()]
        self.menu.clear()
        for name, note in self._offers:
            action = self.menu.addAction(f"{name}  -  {note}" if note else name)
            action.triggered.connect(lambda _c=False, n=name: self.start(n))
        if not self._busy:
            self.download.setEnabled(bool(self._offers))
            if not self._offers and not self.status.text():
                self.status.setText("")

    # -- downloading ---------------------------------------------------------------

    def _pressed(self) -> None:
        """Download: the menu of offers, or the one target."""
        if self._offers and not self._target:
            self.menu.popup(self.download.mapToGlobal(self.download.rect().bottomLeft()))
            return
        self.start(self._target)

    def start(self, name: str) -> None:
        """Download `name` on a worker. One at a time per row."""
        name = str(name or "").strip()
        if not name or self._busy:
            return
        self._busy = True
        # One event for the row's life, so quitting and closing reach it.
        self._stop.clear()
        self.download.setEnabled(False)
        self.stop.show()
        self.stop.setEnabled(True)
        self.status.setText(f"Starting to download {name}...")
        worker = CallableWorker(self._fetch, name, self._stop, component="ui.models.download")
        worker.signals.finished.connect(lambda result, n=name: self._ended(n, str(result)))
        worker.signals.failed.connect(lambda error, n=name: self._failed(n, error))
        run(QThreadPool.globalInstance(), worker)

    def _fetch(self, name: str, stop: threading.Event) -> str:
        """Worker thread. Blocks for as long as the download takes."""
        client = self._client_factory() if self._client_factory is not None else None
        relay = self._relay

        def said(text: str) -> None:
            """Worker thread: forward a progress line; a row already gone stops the download."""
            try:
                relay.said.emit(text)
            except RuntimeError:                 # the row has gone; stop quietly
                stop.set()

        return model_fetch.fetch(self.kind, name, model_cache=self._model_cache,
                                 client=client, on_progress=said, stop=stop)

    def _say(self, text: str) -> None:
        """UI thread, via the relay: a progress line, while a download is running."""
        if self._busy:
            self.status.setText(text)

    def _stop_pressed(self) -> None:
        """Ask the worker to stop at the next chunk; the row says so meanwhile."""
        self._stop.set()
        self.stop.setEnabled(False)
        self.status.setText("Stopping...")

    def _ended(self, name: str, result: str) -> None:
        """UI thread: the download finished or stopped. Re-asks whether the target is here."""
        self._busy = False
        self.stop.hide()
        if result == model_fetch.STOPPED:
            self.status.setText(f"Stopped. What arrived of {name} is kept; Download "
                                "carries on from there.")
        else:
            self.status.setText(f"{name} is downloaded and ready to use.")
        self.download.setEnabled(bool(self._offers) and not self._target)
        self.finished.emit(name, result)
        if self._target:
            self.set_target(self._target)

    def _failed(self, name: str, error: Any) -> None:
        """UI thread: the download raised. The error's code, message and suggestion, in a line."""
        self._busy = False
        self.stop.hide()
        code = getattr(error, "code", "ERR_MODEL_DOWNLOAD")
        message = getattr(error, "message", "") or f"{name} could not be downloaded."
        suggestion = getattr(error, "suggestion", "")
        self.status.setText(f"[{code}] {message} {suggestion}".strip())
        self.download.setEnabled(True)
