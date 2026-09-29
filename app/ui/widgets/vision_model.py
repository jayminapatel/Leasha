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

from PyQt6.QtCore import QThreadPool, Qt
from PyQt6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

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
        self._offer(None)

    def _client(self) -> Any:
        from app.llm.ollama import OllamaClient

        return OllamaClient(self._url)

    def set_url(self, url: str) -> None:
        self._url = str(url or self._url)

    def showEvent(self, event: Any) -> None:       # noqa: N802 - Qt's naming
        super().showEvent(event)
        if not self._asked:
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
        probe = self._probe
        if probe is None:
            from app.chat.llm import probe_installed

            probe = probe_installed
        return probe(url)

    def _probed(self, installed: Any) -> None:
        self._probing = False
        self.look_again.setEnabled(True)
        if not isinstance(installed, InstalledModels):
            installed = InstalledModels()
        self.combo.populate(installed, self.combo.value())
        self.why.setText(self.combo.reason)
        self.why.setVisible(bool(self.combo.reason))
        self._offer(installed)

    def _offer(self, installed: Optional[InstalledModels]) -> None:
        have = set(installed.vision) if installed is not None else set()
        bare = {name.split(":")[0] for name in have}
        self.download.set_offers(
            (name, note) for name, note in VISION_SUGGESTED
            if name not in have and name not in bare)
