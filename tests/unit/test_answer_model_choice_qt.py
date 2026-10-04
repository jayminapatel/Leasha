r"""Picking the model at chat or search time, and loading it ahead - on the real window.

Layer: L5 (pytest-qt, `gui_mainwindow`). No Ollama and no model: the list comes from
a `menu_factory`, the engine from an `engine_factory`, Interpret's client from a
stand-in for `app.ui.tasks.interpret_client`.

2026-10-04, the owner: "the chat is really slow ... if multiple models are available
they should be listed so they can be changed at chat or search time".
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("PyQt6")

from app.chat.roles import ModelOption                                   # noqa: E402
from app.ui.controllers import chat_controller                           # noqa: E402
from app.ui.controllers.interpret_controller import MODEL_KEY as INTERPRET_KEY  # noqa: E402
from app.ui.widgets.model_picker import ModelMenu, ModelPicker           # noqa: E402
from tests.unit.conftest import gui_pump                                 # noqa: E402

pytestmark = pytest.mark.gui

GB = 1024 ** 3
OPTIONS = [ModelOption("onnx:qwen-q4", "Qwen 2.5 1.5B · in Leasha · 1.7 GB", int(1.7 * GB)),
           ModelOption("ollama:qwen2.5:1.5b", "qwen2.5:1.5b · Ollama · 0.9 GB", GB),
           ModelOption("ollama:gemma4:26b", "gemma4:26b · Ollama · 17 GB", 17 * GB)]


def menu(default: str = "onnx:qwen-q4", free_mb: int = 8000) -> dict:
    return {"options": list(OPTIONS), "default": default, "free_mb": free_mb}


class WarmEngine:
    def __init__(self) -> None:
        self.warmed = 0

    def warm(self) -> bool:
        self.warmed += 1
        return True

    def available(self) -> tuple:
        return True, ""

    def suggest_modes(self) -> dict:
        return {}


def _state(store, key: str) -> str:
    return store.get_state(key, "") or ""


@pytest.fixture()
def chat(gui_mainwindow, qtbot):
    app, window, store, _engine = gui_mainwindow
    ctl, view = window.chat_ctl, window.chat_view
    qtbot.waitUntil(lambda: ctl._ask is None, timeout=5000)
    engine = WarmEngine()
    ctl.engine_factory = lambda: engine
    ctl.engine, ctl.menu_factory = None, (lambda: menu())
    ctl._choice, ctl._picked, ctl._listed, ctl._opened = "", False, False, False
    ctl._options = []
    ctl._settle.stop()
    view.model_picker.set_options([], "")       # the window is shared: start from empty
    store.set_state(chat_controller.MODEL_KEY, "")
    yield SimpleNamespace(app=app, window=window, store=store, ctl=ctl, view=view,
                          engine=engine, qtbot=qtbot)
    ctl.menu_factory = None
    store.set_state(chat_controller.MODEL_KEY, "")


def test_the_chat_tab_lists_every_model_and_starts_on_settings_choice(chat):
    chat.window._show(chat.window.search_view)
    chat.window._show(chat.view)
    picker = chat.view.model_picker
    chat.qtbot.waitUntil(lambda: picker.count() == 3, timeout=5000)
    assert picker.value() == "onnx:qwen-q4", "the model Settings would use"
    assert not picker.isHidden()
    assert picker.toolTip() and picker.accessibleName() == "Which model answers"


def test_picking_a_model_applies_to_the_next_question_and_is_remembered(chat):
    chat.window._show(chat.window.search_view)
    chat.window._show(chat.view)
    picker = chat.view.model_picker
    chat.qtbot.waitUntil(lambda: picker.count() == 3, timeout=5000)
    chat.ctl.engine = chat.engine
    picker.setCurrentIndex(1)
    picker.activated.emit(1)
    assert chat.ctl._choice == "ollama:qwen2.5:1.5b"
    assert chat.ctl.engine is None, "rebuilt for the next question, no restart"
    chat.qtbot.waitUntil(lambda: _state(chat.store, chat_controller.MODEL_KEY)
                         == "ollama:qwen2.5:1.5b", timeout=5000)
    # Loaded once the pick has settled: the person is about to ask with it.
    chat.qtbot.waitUntil(lambda: chat.engine.warmed >= 1,
                         timeout=chat_controller.PICK_SETTLE_MS + 5000)


def test_the_picked_model_is_the_one_the_engine_is_built_with(chat):
    chat.ctl.engine_factory = None
    try:
        chat.ctl._choice = "ollama:mistral:latest"
        cfg = chat.ctl._default_engine().cfg
        assert (cfg.engine, cfg.answer_model) == ("ollama", "mistral:latest")
        chat.ctl._choice = "onnx:hf:onnx-community/gemma-3-1b-it-ONNX:4-bit"
        cfg = chat.ctl._default_engine().cfg
        assert (cfg.engine, cfg.onnx_model) == ("onnx", "hf:onnx-community/gemma-3-1b-it-ONNX:4-bit")
        chat.ctl._choice = ""
        cfg = chat.ctl._default_engine().cfg
        assert cfg.onnx_model == "" and cfg.answer_model == "", "no pick: Settings decide"
    finally:
        chat.ctl.engine_factory = lambda: chat.engine


def test_the_model_in_use_is_loaded_ahead_of_the_first_question(chat):
    chat.ctl.preload()
    chat.qtbot.waitUntil(lambda: chat.engine.warmed == 1, timeout=5000)


def test_a_model_too_big_for_the_free_memory_is_not_loaded_ahead(chat, qtbot):
    chat.ctl.menu_factory = lambda: menu(default="ollama:gemma4:26b", free_mb=16_000)
    chat.ctl.preload()
    qtbot.waitUntil(lambda: bool(chat.ctl._options), timeout=5000)
    qtbot.wait(200)
    assert chat.engine.warmed == 0


def test_a_remembered_model_that_is_gone_is_set_aside(chat):
    chat.store.set_state(chat_controller.MODEL_KEY, "ollama:removed:7b")
    chat.window._show(chat.window.search_view)
    chat.window._show(chat.view)
    chat.qtbot.waitUntil(lambda: chat.view.model_picker.count() == 3, timeout=5000)
    assert chat.ctl._choice == "" and chat.view.model_picker.value() == "onnx:qwen-q4"


def test_a_remembered_model_is_used_again(chat):
    chat.store.set_state(chat_controller.MODEL_KEY, "ollama:qwen2.5:1.5b")
    chat.window._show(chat.window.search_view)
    chat.window._show(chat.view)
    chat.qtbot.waitUntil(lambda: chat.view.model_picker.count() == 3, timeout=5000)
    assert chat.ctl._choice == "ollama:qwen2.5:1.5b"
    assert chat.view.model_picker.value() == "ollama:qwen2.5:1.5b"


def test_a_model_chosen_in_settings_afterwards_wins(chat):
    chat.ctl._apply_choice("ollama:qwen2.5:1.5b")
    chat.ctl._settings_changed({"CHAT_ENGINE": "ollama"})
    assert chat.ctl._choice == ""


def test_the_chat_model_loads_when_chat_is_first_opened_not_at_start_up(
        gui_mainwindow, qtbot, monkeypatch):
    """*2026-10-04, the owner (2.2a)*: this test asserted the opposite - a load a
    beat after start-up. Loading a model holds Python's lock for the whole load
    (5.5 s for the 1.66 GB chat model), which froze the window as it opened, so it
    now loads the first time Chat comes forward, and says so in the status bar."""
    _app, window, _store, _engine = gui_mainwindow
    preloaded, listed, said = [], [], []
    monkeypatch.setattr(chat_controller, "BACKGROUND_MODELS", True)
    monkeypatch.setattr(chat_controller, "PRELOAD_DELAY_MS", 10)
    monkeypatch.setattr(window.chat_ctl, "preload", lambda: preloaded.append(1))
    monkeypatch.setattr(window._translator, "enabled", False)
    window._start_model_choices()
    qtbot.wait(100)
    assert preloaded == [], "nothing loads at start-up"

    ctl = window.chat_ctl
    monkeypatch.setattr(ctl, "list_models", lambda **kw: listed.append(kw))
    ctl._opened, ctl._listed = False, False
    ctl._tab_changed(window._tab_index.get(window.chat_view))
    assert listed == [{"warm": True, "full": True}]

    monkeypatch.setattr(window, "notify", lambda text, *a, **k: said.append(text))
    monkeypatch.setattr(chat_controller, "LOADING_PAINT_MS", 1)
    ctl.engine = None
    ctl._warm(0)
    assert said and said[0] == chat_controller.LOADING_WORDS


def test_a_single_model_is_not_offered_as_a_choice(qtbot):
    picker = ModelPicker()
    qtbot.addWidget(picker)
    picker.set_options(OPTIONS[:1], "onnx:qwen-q4")
    assert picker.isHidden()
    picker.set_options(OPTIONS, "")
    # Dated note, 2026-10-04, code review: with no model named the list fell to its
    # first entry, which was not the model answering; it says "Settings' choice" now.
    assert not picker.isHidden() and picker.value() == ""


# ---------------------------------------------------------------------------
# Interpret, on the Search page
# ---------------------------------------------------------------------------

@pytest.fixture()
def interpret(gui_mainwindow, qtbot, monkeypatch):
    app, window, store, _engine = gui_mainwindow
    ctl = window.interpret_ctl
    translator = window._translator
    was = (translator.enabled, translator.client)
    translator.enabled = True
    window.search_view.set_interpret_enabled(True)
    ctl.menu_factory = lambda: menu(default="ollama:qwen2.5:1.5b")
    ctl._asked, ctl._picked, ctl._choice = False, False, ""
    built = []

    # Dated note, 2026-10-04, code review: the pick's size goes along, for the memory check.
    def fake_client(settings, value, *, warm=False, size_bytes=0):
        client = SimpleNamespace(model=value, warmed=warm)
        built.append(client)
        return client

    monkeypatch.setattr("app.ui.tasks.interpret_client", fake_client)
    store.set_state(INTERPRET_KEY, "")
    yield SimpleNamespace(app=app, window=window, store=store, ctl=ctl, menu=ctl.menu,
                          translator=translator, built=built, qtbot=qtbot)
    ctl.forget()
    translator.enabled, translator.client = was
    window.search_view.set_interpret_enabled(was[0])
    ctl.menu_factory = None


def test_interpret_lists_the_same_models_under_its_own_menu(interpret):
    assert isinstance(interpret.menu, ModelMenu)
    assert interpret.menu.menuAction() in interpret.window.search_view.more_menu.actions()
    interpret.ctl.list_if_needed()
    interpret.qtbot.waitUntil(lambda: len(interpret.menu.actions()) == 3, timeout=5000)
    assert interpret.menu.value() == "ollama:qwen2.5:1.5b"
    assert interpret.menu.menuAction().isVisible()
    interpret.window.search_view.set_interpret_enabled(False)
    assert not interpret.menu.menuAction().isVisible(), "shown only while Interpret is"


def test_picking_a_model_for_interpret_hands_the_translator_that_model(interpret):
    interpret.ctl.list_if_needed()
    interpret.qtbot.waitUntil(lambda: len(interpret.menu.actions()) == 3, timeout=5000)
    default = interpret.translator.client
    interpret.translator._cache["old"] = object()
    next(a for a in interpret.menu.actions() if a.data() == "onnx:qwen-q4").trigger()
    interpret.qtbot.waitUntil(lambda: interpret.translator.client is not default, timeout=5000)
    assert interpret.translator.client.model == "onnx:qwen-q4"
    assert interpret.translator.client.warmed, "Interpret is on, so it is loaded ahead"
    assert not interpret.translator._cache, "the old model's answers are not reused"
    interpret.qtbot.waitUntil(lambda: _state(interpret.store, INTERPRET_KEY) == "onnx:qwen-q4",
                              timeout=5000)
    interpret.ctl.forget()
    assert interpret.translator.client is default


def test_nothing_is_listed_for_interpret_while_it_is_off(interpret):
    interpret.translator.enabled = False
    interpret.ctl.list_if_needed()
    gui_pump(interpret.app)
    assert not interpret.ctl._asked
