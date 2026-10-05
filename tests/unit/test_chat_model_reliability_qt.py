r"""The model pickers and the Chat tab's engine - the code review of 2026-10-04, on Qt.

Layer: L5 (pytest-qt, `gui_mainwindow`). No Ollama and no model: the list comes from a
`menu_factory`, the engine from an `engine_factory`.

* the list never shows a model that is not the one answering - "Settings' choice"
  when Settings' model is not listed, and no change while a question runs (finding 9);
* a big model picked on the tab is warned about, and not loaded ahead when it does
  not fit (finding 2);
* an engine a worker built is kept only if nothing changed meanwhile, and only on the
  window thread; closing stops the pending load (finding 10);
* Interpret's start-up list asks Ollama only when its model is Ollama's (finding 8),
  and its remembered pick is loaded only if it fits (finding 2).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("PySide6")

from app.chat.roles import ModelOption                                   # noqa: E402
from app.ui.controllers import chat_controller                           # noqa: E402
from app.ui.widgets.model_picker import (                                # noqa: E402
    SETTINGS_CHOICE, ModelMenu, ModelPicker,
)

pytestmark = pytest.mark.gui

GB = 1024 ** 3
OPTIONS = [ModelOption("onnx:qwen-q4", "Qwen 2.5 1.5B · in Leasha · 1.7 GB", int(1.7 * GB)),
           ModelOption("ollama:qwen2.5:1.5b", "qwen2.5:1.5b · Ollama · 0.9 GB", GB),
           ModelOption("ollama:gemma4:26b", "gemma4:26b · Ollama · 17 GB", 17 * GB)]


def menu(default: str = "onnx:qwen-q4", free_mb: int = 8000) -> dict:
    return {"options": list(OPTIONS), "default": default, "free_mb": free_mb}


class Engine:
    def __init__(self, name: str = "e") -> None:
        self.name, self.warmed = name, 0

    def warm(self) -> bool:
        self.warmed += 1
        return True

    def available(self) -> tuple:
        return True, ""

    def suggest_modes(self) -> dict:
        return {}


# ---------------------------------------------------------------------------
# The pickers themselves
# ---------------------------------------------------------------------------

def test_the_list_says_settings_choice_rather_than_its_first_model(qtbot):
    picker = ModelPicker()
    qtbot.addWidget(picker)
    picker.set_options(OPTIONS, "ollama:mistral:latest")      # Settings' model, not listed
    assert picker.currentText() == SETTINGS_CHOICE and picker.value() == ""
    assert picker.count() == 4 and not picker.isHidden()
    picker.set_options(OPTIONS, "ollama:qwen2.5:1.5b")
    assert picker.count() == 3 and picker.value() == "ollama:qwen2.5:1.5b"
    picker.set_options(OPTIONS[:1], "")
    assert picker.isHidden(), "one real model is still not a choice"


def test_picking_settings_choice_hands_back_settings_model(qtbot):
    picker = ModelPicker()
    qtbot.addWidget(picker)
    picker.set_options(OPTIONS, "")
    with qtbot.waitSignal(picker.chosen) as caught:
        picker.activated.emit(0)
    assert caught.args == [""]


def test_interprets_menu_ticks_settings_choice_rather_than_its_first_model(qtbot):
    holder = ModelPicker()
    qtbot.addWidget(holder)
    menu_widget = ModelMenu(holder)
    menu_widget.set_options(OPTIONS, "ollama:mistral:latest")
    assert menu_widget.actions()[0].text() == SETTINGS_CHOICE and menu_widget.value() == ""
    assert sum(a.isChecked() for a in menu_widget.actions()) == 1
    menu_widget.set_options(OPTIONS, "ollama:qwen2.5:1.5b")
    assert len(menu_widget.actions()) == 3 and menu_widget.value() == "ollama:qwen2.5:1.5b"
    big = next(a for a in menu_widget.actions() if a.data() == "ollama:gemma4:26b")
    assert "large model" in big.toolTip()


def test_a_big_model_is_warned_about_beside_the_list(qtbot):
    from PySide6.QtWidgets import QHBoxLayout, QWidget

    host = QWidget()
    qtbot.addWidget(host)
    row = QHBoxLayout(host)
    picker = ModelPicker()
    row.addWidget(picker.warning)
    row.addWidget(picker)
    host.show()
    picker.set_options(OPTIONS, "ollama:gemma4:26b")
    assert "gemma4:26b is a large model" in picker.warning_text()
    assert not picker.warning.isHidden() and picker.warning.toolTip()
    picker.setCurrentIndex(0)
    assert picker.warning_text() == "" and picker.warning.isHidden()


# ---------------------------------------------------------------------------
# The Chat tab
# ---------------------------------------------------------------------------

@pytest.fixture()
def chat(gui_mainwindow, qtbot, monkeypatch):
    app, window, store, _engine = gui_mainwindow
    ctl, view = window.chat_ctl, window.chat_view
    qtbot.waitUntil(lambda: ctl._ask is None, timeout=5000)
    engine = Engine()
    ctl.engine_factory = lambda: engine
    ctl.engine, ctl.menu_factory = None, (lambda: menu())
    ctl._choice, ctl._picked, ctl._listed, ctl._opened = "", False, False, False
    ctl._options, ctl._deferred_list, ctl._closing = [], None, False
    ctl._settle.stop()
    view.model_picker.set_options([], "")
    store.set_state(chat_controller.MODEL_KEY, "")
    monkeypatch.setattr("app.ui.tasks.free_memory_mb", lambda: 16_000)
    yield SimpleNamespace(app=app, window=window, store=store, ctl=ctl, view=view,
                          engine=engine, qtbot=qtbot)
    ctl.menu_factory, ctl._closing, ctl._ask = None, False, None
    store.set_state(chat_controller.MODEL_KEY, "")


def test_a_big_model_picked_on_the_tab_is_not_loaded_when_it_does_not_fit(chat):
    """gemma4:26b (17 GB) on a laptop with 16 GB free: picked, remembered, not loaded."""
    chat.ctl._models_listed(("", menu()), full=True)
    chat.ctl._model_chosen("ollama:gemma4:26b")
    chat.qtbot.waitUntil(lambda: chat.ctl.engine is not None,
                         timeout=chat_controller.PICK_SETTLE_MS + 5000)
    chat.qtbot.wait(200)
    assert chat.engine.warmed == 0


def test_a_small_model_picked_on_the_tab_is_loaded_once_it_settles(chat):
    chat.ctl._models_listed(("", menu()), full=True)
    chat.ctl._model_chosen("ollama:qwen2.5:1.5b")
    chat.qtbot.waitUntil(lambda: chat.engine.warmed >= 1,
                         timeout=chat_controller.PICK_SETTLE_MS + 5000)
    # 2026-10-04: `warmed` is set on the worker; the engine is kept when the
    # finished signal reaches the window thread, a moment later. Asserting in
    # between failed on every run on the laptop - wait for the adoption itself.
    chat.qtbot.waitUntil(lambda: chat.ctl.engine is chat.engine, timeout=2000)


def test_an_engine_built_before_a_pick_is_not_kept_after_it(chat):
    """A probe or a preload finishing after a pick wrote back the old model's engine."""
    old, generation = chat.ctl._engine_now()
    chat.ctl._apply_choice("ollama:qwen2.5:1.5b")       # the pick lets the engine go
    chat.ctl._adopt(old, generation)
    assert chat.ctl.engine is None
    fresh, now = chat.ctl._engine_now()
    chat.ctl._adopt(fresh, now)
    assert chat.ctl.engine is fresh


def test_a_worker_never_assigns_the_engine(chat):
    engine, _generation = chat.ctl._engine_now()
    assert engine is chat.engine and chat.ctl.engine is None
    result = chat.ctl._probe()
    assert result[4] is chat.engine and chat.ctl.engine is None
    chat.ctl._probed(result)
    assert chat.ctl.engine is chat.engine


def test_closing_stops_the_pending_load(chat):
    chat.ctl._models_listed(("", menu()), full=True)
    chat.ctl._model_chosen("ollama:qwen2.5:1.5b")
    assert chat.ctl._settle.isActive()
    chat.ctl.shutdown()
    assert not chat.ctl._settle.isActive()
    chat.ctl._warm(GB)
    chat.qtbot.wait(300)
    assert chat.engine.warmed == 0 and chat.ctl._warm_body(GB) == (None, -1, False)


def test_the_list_does_not_change_while_a_question_is_answered(chat):
    """The start-up list's saved pick landed during the first question."""
    chat.ctl._models_listed(("", menu()), full=True)
    assert chat.view.model_picker.value() == "onnx:qwen-q4"
    chat.ctl._ask = SimpleNamespace(released=False, engine=None, generation=-1)
    chat.ctl._models_listed(("ollama:qwen2.5:1.5b", menu()), warm=True, full=False)
    assert chat.ctl._choice == "" and chat.view.model_picker.value() == "onnx:qwen-q4"
    asking, chat.ctl._ask = chat.ctl._ask, None
    chat.ctl._show_deferred_list()
    assert chat.ctl._choice == "ollama:qwen2.5:1.5b"
    assert chat.view.model_picker.value() == "ollama:qwen2.5:1.5b"
    assert asking is not None


def test_a_pick_during_an_answer_is_loaded_after_it(chat):
    chat.ctl._models_listed(("", menu()), full=True)
    chat.ctl._ask = SimpleNamespace(released=False, engine=None, generation=-1)
    chat.ctl._model_chosen("ollama:qwen2.5:1.5b")
    assert not chat.ctl._settle.isActive() and chat.ctl._rebuild_after


# ---------------------------------------------------------------------------
# Interpret
# ---------------------------------------------------------------------------

@pytest.fixture()
def interpret(gui_mainwindow, qtbot, monkeypatch):
    app, window, store, _engine = gui_mainwindow
    ctl, translator = window.interpret_ctl, window._translator
    was = (translator.enabled, translator.client)
    translator.enabled = True
    ctl._asked, ctl._full, ctl._picked, ctl._choice = False, False, False, ""
    built = []

    def fake_client(settings, value, *, warm=False, size_bytes=0):
        client = SimpleNamespace(model=value, warmed=warm, size_bytes=size_bytes)
        built.append(client)
        return client

    monkeypatch.setattr("app.ui.tasks.interpret_client", fake_client)
    yield SimpleNamespace(window=window, store=store, ctl=ctl, translator=translator,
                          built=built, qtbot=qtbot, monkeypatch=monkeypatch)
    ctl.forget()
    ctl.menu_factory = None
    ctl._asked, ctl._full, ctl._picked = False, False, False
    translator.enabled, translator.client = was


def test_interprets_start_up_list_asks_ollama_only_for_ollamas_model(interpret):
    asked = []
    interpret.monkeypatch.setattr(
        "app.ui.tasks.answer_model_menu",
        lambda settings, engine="", name="", *, ollama=True: asked.append((engine, ollama))
        or menu())
    interpret.ctl._default = SimpleNamespace(engine="onnx", model="qwen-q4")
    interpret.ctl._models("qwen-q4", "onnx", False)
    interpret.ctl._models("qwen-q4", "onnx", True)
    interpret.ctl._default = SimpleNamespace(model="qwen2.5:1.5b")      # an Ollama client
    interpret.ctl._models("qwen2.5:1.5b", "ollama", False)
    assert asked == [("onnx", False), ("onnx", True), ("ollama", True)]


def test_interprets_menu_opening_makes_the_full_list_once(interpret):
    calls = []
    interpret.monkeypatch.setattr(interpret.ctl, "start", lambda *, full=True: calls.append(full))
    interpret.ctl.menu_factory = lambda: menu()
    interpret.ctl.list_if_needed()                                      # start-up
    interpret.ctl.list_if_needed()                                      # again: nothing
    interpret.window.search_view.more_menu.aboutToShow.emit()           # the person opens it
    interpret.window.search_view.more_menu.aboutToShow.emit()
    assert calls == [False, True]


def test_interprets_remembered_pick_is_loaded_with_its_size_known(interpret):
    interpret.ctl.menu_factory = lambda: menu(default="ollama:qwen2.5:1.5b")
    interpret.store.set_state("ui:interpret_model", "ollama:gemma4:26b")
    interpret.ctl._listed(("ollama:gemma4:26b", menu(default="ollama:qwen2.5:1.5b")))
    interpret.qtbot.waitUntil(lambda: bool(interpret.built), timeout=5000)
    assert interpret.built[-1].size_bytes == 17 * GB, "so `warm_if_fits` can say no"
    interpret.store.set_state("ui:interpret_model", "")
