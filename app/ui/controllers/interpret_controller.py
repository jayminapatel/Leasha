"""Which model Interpret uses, picked on the Search page (2026-10-04).

Layer: L5

The owner: "if multiple models are available they should be listed so they can be
changed at chat or search time". Interpret lives in the Search page's `⋯` menu, and
so does its list: `ModelMenu` ("Interpret with"), shown while Interpret is. The list
is the Chat tab's (`app.ui.tasks.answer_model_menu`); a pick builds that model's
client on a worker (`interpret_client`), loads it when Interpret is on, and hands
it to the translator. Remembered as window state (`MODEL_KEY`), so Settings' own
Interpret model and `CHAT_ENGINE` are never rewritten - and a model chosen in
Settings afterwards wins (`forget`). Search itself uses no model and is untouched.

Nothing here touches the store, the disk or the network on the window thread.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from PySide6.QtCore import QObject, QThreadPool

from app.core.logging import logger
from app.ui.state_writes import save_state
from app.ui.workers import CallableWorker, run

__all__ = ["InterpretModels", "MODEL_KEY"]

_log = logger.bind(component="ui.translate")

#: Window state: the model picked for Interpret (`app.chat.roles.option_value`), or
#: "" for the one Settings chose.
MODEL_KEY = "ui:interpret_model"


class InterpretModels(QObject):
    """Interpret's model menu on the Search page. A `QObject` parented to the
    window; every list and load runs on a worker.
    """
    def __init__(self, window: Any, menu: Any, translator: Any) -> None:
        """Hold the menu, the translator and Settings' client; nothing runs yet."""
        super().__init__(window)
        self._w = window
        self.menu = menu
        self.translator = translator
        #: The client Settings built, given back when the pick is cleared.
        self._default = getattr(translator, "client", None)
        self._choice = ""
        self._picked = False
        self._asked = False
        #: 2026-10-04, code review: whether the list made included Ollama's models.
        self._full = False
        self._token = 0
        #: The options last listed, for a pick's size (`_apply`).
        self._options: list = []
        #: Replaces the real listing (tests).
        self.menu_factory: Optional[Callable[[], dict]] = None
        menu.chosen.connect(self.choose)

    @property
    def default_client(self) -> Any:
        """The client Settings built - the one Settings' Interpret model belongs to."""
        return self._default

    # -- the list ----------------------------------------------------------------
    def list_if_needed(self) -> None:
        """List the models and apply a remembered pick - once, and only while Interpret
        is switched on: nothing asks Ollama for somebody who does not use it.

        2026-10-04, code review: at start-up Ollama was asked even with the model
        inside Leasha in use, unlike Chat's preload. Now the same rule: Ollama is
        asked at start-up only when the model in use is Ollama's, and the full list
        is made when the person opens the `⋯` menu (this slot, called by its signal)."""
        from app.ui.controllers import chat_controller

        if not getattr(self.translator, "enabled", False):
            return
        full = self.sender() is not None         # the menu opening, not start-up
        if self._full or (self._asked and not full):
            return
        if chat_controller.BACKGROUND_MODELS or self.menu_factory is not None:
            self._asked = True
            self._full = self._full or full
            self.start(full=full)

    def start(self, *, full: bool = True) -> None:
        """List the models and apply a remembered pick, on a worker."""
        current = str(getattr(self._default, "model", "") or "")
        engine = ("" if self._default is None
                  else "onnx" if getattr(self._default, "engine", "") == "onnx" else "ollama")
        worker = CallableWorker(self._models, current, engine, full, component="ui.translate")
        worker.signals.finished.connect(self._listed)
        worker.signals.failed.connect(lambda error: _log.debug("no Interpret model list ({})", error))
        run(QThreadPool.globalInstance(), worker)

    def _models(self, current: str, engine: str = "", full: bool = True) -> tuple:
        """`(saved pick, menu)`. Worker."""
        saved = ""
        try:
            saved = self._w._store.get_state(MODEL_KEY, "") or ""
        except Exception as exc:                          # noqa: BLE001
            _log.debug("no remembered Interpret model ({})", exc)
        if self.menu_factory is not None:
            return saved, self.menu_factory()
        from app.chat.roles import parse_option
        from app.llm.engines import engine_of
        from app.ui.tasks import answer_model_menu

        runner = parse_option(self._choice or saved)[0] or engine or engine_of(self._w._settings)
        return saved, answer_model_menu(self._w._settings, engine, current,
                                        ollama=full or runner == "ollama")

    def _listed(self, result: tuple) -> None:
        """UI thread: show the options and apply a remembered pick if it is still there."""
        saved, menu = result
        options = list(menu.get("options") or [])
        values = {str(o.value) for o in options}
        self._options = options
        if not self._picked and saved and (saved in values or not options):
            self._apply(saved)
        selected = self._choice if self._choice in values else str(menu.get("default") or "")
        self.menu.set_options(options, selected)

    # -- a pick --------------------------------------------------------------------
    def choose(self, value: str) -> None:
        """Picked in the menu: remembered, and used from the next press."""
        self._picked = True
        if str(value or "") != self._choice:
            save_state(self._w._store, MODEL_KEY, str(value or ""), component="ui.translate")
            self._apply(str(value or ""))

    def settings_chose(self, model: str) -> None:
        """Settings' Interpret model was saved: a *different* model there wins over the
        pick (switching Interpret on or changing its time limit keeps the pick). With the
        model inside Leasha, Settings' Ollama name changes nothing, so neither does this."""
        if getattr(self._default, "engine", "") == "onnx":
            return
        if model and model != str(getattr(self._default, "model", "") or ""):
            self.forget()
            self._asked = self._full = False              # listed again: the default moved

    def engine_changed(self, client: Any) -> None:
        """Settings changed `CHAT_ENGINE` (2026-10-10): `client` is now the one Settings
        builds. A model picked under the other engine is dropped, the translator takes
        the new client, and the list is made again the next time the menu opens."""
        self._default = client
        self._token += 1                                  # a client still being built is dropped
        if self._choice:
            self._choice = ""
            save_state(self._w._store, MODEL_KEY, "", component="ui.translate")
        self._asked = self._full = False
        self._options = []
        self._hand_over(client)

    def forget(self) -> None:
        """Settings chose Interpret's model: that wins, and the pick is cleared."""
        self._token += 1                                  # a client still being built is dropped
        if self._choice:
            self._choice = ""
            save_state(self._w._store, MODEL_KEY, "", component="ui.translate")
        if self._default is not None:
            self._hand_over(self._default)

    def _apply(self, value: str) -> None:
        """Use `value` from the next press: build its client on a worker, loading it
        ahead only when Interpret is on and it fits in memory.
        """
        self._choice = value
        self._token += 1
        if not value:
            if self._default is not None:
                self._hand_over(self._default)
            return
        from app.ui.tasks import interpret_client

        token = self._token
        # 2026-10-04, code review: loaded ahead only if it fits in the memory free
        # (`tasks.warm_if_fits`) - a remembered 26B pick was loaded at every start-up.
        size = next((int(o.size_bytes) for o in self._options if str(o.value) == value), 0)
        worker = CallableWorker(interpret_client, self._w._settings, value,
                                warm=bool(getattr(self.translator, "enabled", False)),
                                size_bytes=size, component="ui.translate")
        worker.signals.finished.connect(lambda client, t=token: self._built(t, client))
        worker.signals.failed.connect(lambda error: _log.warning("Interpret's model: {}", error))
        run(QThreadPool.globalInstance(), worker)

    def _built(self, token: int, client: Any) -> None:
        """UI thread: the client landed; ignored if another pick came first."""
        if token != self._token:
            return                                        # picked again meanwhile
        self._hand_over(client if client is not None else self._default)

    def _hand_over(self, client: Any) -> None:
        """The translator uses `client` from the next press. Its remembered answers
        came from the other model, so they go (as `reconfigure` does for Settings)."""
        if client is None or client is self.translator.client:
            return
        self.translator.client = client
        self.translator.reconfigure()
