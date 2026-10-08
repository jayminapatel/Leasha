r"""The photo description model: a list of what can read pictures, and Download.

Layer: L5 view - thin. Which models are installed and which read pictures is
`app/chat/llm.probe_installed`; downloading is `widgets/model_download.py`.

**The owner, 2026-09-29:** *"where there are models it has to be dropdown only
no manual entry for models"*. `OLLAMA_VISION_MODEL` was a text box on the
Models page, with `llava` as its placeholder - so a typo was only found when
Describe failed. It is now the same drop-down the Chat roles grid uses for the
same setting (`chat_roles.ModelCombo`, vision models only, "Automatic" first),
with the reason beside it when the list is short, and a Download menu of the
picture-reading models in `app.llm.models.VISION_SUGGESTED` that are not
installed yet.

**Ollama is asked only when the field is first shown**, on a worker, and again
after a download or a press of Look again. Opening Settings on another page
contacts nothing.
"""

from __future__ import annotations

from typing import Any, Optional

from PySide6.QtCore import QThreadPool, Qt
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from app.chat.roles import InstalledModels
from app.llm.models import VISION_SUGGESTED
from app.ui.widgets.buttons import style_button
from app.ui.widgets.chat_roles import ModelCombo
from app.ui.widgets.model_download import DownloadRow
from app.ui.workers import CallableWorker, run

__all__ = ["VisionModelField"]


class VisionModelField(QWidget):
    """The drop-down, why it is short, and Download."""

    def __init__(self, combo: ModelCombo, *, url: str = "", saved: str = "",
                 probe: Any = None, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.combo = combo
        self._url = str(url or "http://127.0.0.1:11434")
        #: What asks Ollama; a test hands one in.
        self._probe = probe
        self._asked = False
        self._probing = False
        combo.populate(None, saved)

        self.why = QLabel("")
        self.why.setWordWrap(True)
        self.why.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.why.hide()
        self.look_again = QPushButton("Look again")
        self.look_again.setToolTip("Ask Ollama again which models are installed.")
        self.look_again.clicked.connect(lambda _c=False: self.refresh())
        style_button(self.look_again)
        self.download = DownloadRow("ollama", client_factory=self._client)
        self.download.finished.connect(lambda _n, _r: self.refresh())

        # The drop-down, then Look again and Download on one line (owner,
        # 2026-09-29: the buttons for a model sit together, as in Interpret's
        # and Chat's boxes), then why the list is short.
        buttons = QHBoxLayout()
        buttons.setContentsMargins(0, 0, 0, 0)
        buttons.addWidget(self.look_again)
        buttons.addWidget(self.download, 1)
        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.addWidget(combo)
        column.addLayout(buttons)
        column.addWidget(self.why)
        # `CHAT_ENGINE=onnx` (the default, 2026-09-29): photo tags and Describe
        # are Florence-2 inside Leasha, so this field offers that model's
        # Download instead of Ollama's list. See `set_engine`.
        self._engine = "ollama"
        self.onnx_note = QLabel("Photo tags and Describe use Florence-2, which runs inside "
                                "Leasha. Download it once; it then works without the internet.")
        self.onnx_note.setWordWrap(True)
        self.onnx_download = DownloadRow("onnx")
        self.onnx_download.setObjectName("photoOnnxModel")
        column.addWidget(self.onnx_note)
        column.addWidget(self.onnx_download)
        self.onnx_note.hide()
        self.onnx_download.hide()
        self._offer(None)

    def _client(self) -> Any:
        """A client for the current Ollama address; built on the worker, no I/O to make."""
        from app.llm.ollama import OllamaClient

        return OllamaClient(self._url)

    def set_url(self, url: str) -> None:
        """The Ollama address changed in Settings; the next probe uses it."""
        self._url = str(url or self._url)

    def set_engine(self, engine: str, model_cache: Any = None) -> None:
        """Ollama's vision list, or Florence-2 inside Leasha with its Download."""
        from app.ort import hub

        self._engine = "ollama" if engine == "ollama" else "onnx"
        onnx = self._engine == "onnx"
        for widget in (self.combo, self.look_again, self.download):
            widget.setVisible(not onnx)
        if onnx:
            self.why.hide()
        self.onnx_note.setVisible(onnx)
        self.onnx_download.setVisible(onnx)
        if onnx:
            self.onnx_download.set_model_cache(model_cache)
            self.onnx_download.set_target(hub.FLORENCE.key)

    def showEvent(self, event: Any) -> None:       # noqa: N802 - Qt's naming
        """First shown with Ollama as the engine: ask it once what is installed."""
        super().showEvent(event)
        # Ollama is asked only when it is the engine (2026-09-29).
        if not self._asked and self._engine == "ollama":
            self.refresh()

    def refresh(self) -> None:
        """Ask Ollama what is installed, on a worker. One at a time."""
        if self._probing:
            return
        self._asked = True
        self._probing = True
        self.look_again.setEnabled(False)
        worker = CallableWorker(self._run_probe, self._url, component="ui.models.vision")
        worker.signals.finished.connect(self._probed)
        worker.signals.failed.connect(lambda _e: self._probed(InstalledModels()))
        run(QThreadPool.globalInstance(), worker)

    def _run_probe(self, url: str) -> InstalledModels:
        """Worker thread: one request to Ollama for its installed models."""
        probe = self._probe
        if probe is None:
            from app.chat.llm import probe_installed

            probe = probe_installed
        return probe(url)

    def _probed(self, installed: Any) -> None:
        """UI thread: refill the drop-down, say why it is short, and offer downloads."""
        self._probing = False
        self.look_again.setEnabled(True)
        if not isinstance(installed, InstalledModels):
            installed = InstalledModels()
        self.combo.populate(installed, self.combo.value())
        self.why.setText(self.combo.reason)
        self.why.setVisible(bool(self.combo.reason))
        self._offer(installed)

    def _offer(self, installed: Optional[InstalledModels]) -> None:
        """Download offers the picture-reading models not installed yet."""
        have = set(installed.vision) if installed is not None else set()
        bare = {name.split(":")[0] for name in have}
        self.download.set_offers(
            (name, note) for name, note in VISION_SUGGESTED
            if name not in have and name not in bare)
