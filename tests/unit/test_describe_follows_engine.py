"""Describe follows the engine switched in Settings, without a restart.

Layer: L5. Work order model-sequencing, item A4 (2026-10-10).

Describe is decided by `CHAT_ENGINE`: Florence-2 inside Leasha for `onnx`, Ollama's
photo description model for `ollama` (`preview_window._describe_client`). Every
pop-out and the lightbox took that engine from `set_describe_options`, which the
window called once at start-up - so after "Chat, Interpret and Describe now run on
Ollama", a photo was still described by Florence-2 until Leasha restarted. The same
for the photo description model and Ollama's address, which are not marked as needing
a restart. Three Settings boxes also kept drawing the old engine's parts.

Stand-ins for the window and the Settings boxes; a real `PreviewWindow` on a real
picture. Nothing reaches a model.
"""

from __future__ import annotations

import os
import pathlib
import tempfile
from types import SimpleNamespace

import pytest

pytest.importorskip("PySide6.QtWidgets", exc_type=ImportError)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QImage                                  # noqa: E402
from PySide6.QtWidgets import QApplication                        # noqa: E402

from app.ui.widgets import preview_window                        # noqa: E402
from app.ui.widgets.preview_window import PreviewWindow          # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    yield QApplication.instance() or QApplication([])


@pytest.fixture(scope="module")
def png():
    folder = pathlib.Path(tempfile.mkdtemp())
    image = QImage(80, 60, QImage.Format.Format_RGB32)
    image.fill(0x336699)
    path = folder / "photo.png"
    image.save(str(path))
    return path


@pytest.fixture(autouse=True)
def describe_defaults(monkeypatch):
    """Each test starts from the settings' own defaults and puts them back after."""
    monkeypatch.setattr(preview_window, "_DESCRIBE",
                        {"ollama_url": "http://127.0.0.1:11434",
                         "ollama_vision_model": "llava", "chat_engine": "onnx"})


class _Row:
    def __init__(self, path):
        self.path = str(path)
        self.name = pathlib.Path(path).name
        self.page = 0


class _LiveSettings:
    """A stand-in for the frozen `Settings`: `model_copy` makes a changed copy."""

    def __init__(self, **fields):
        self.__dict__.update(dict(chat_engine="onnx", ollama_url="http://127.0.0.1:11434",
                                  ollama_model="qwen2.5:1.5b", ollama_vision_model="llava",
                                  model_cache=None, embed_device="auto"), **fields)

    def model_copy(self, update):
        return _LiveSettings(**{**self.__dict__, **update})


def _window(view=None):
    notes: list[str] = []
    window = SimpleNamespace(_settings=_LiveSettings(), _engine=None, settings_view=view,
                             _settings_overrides={},
                             notify=lambda message, ms=0: notes.append(message))
    return window, notes


class _Recorder:
    """A Settings box: records what it is told."""

    def __init__(self, *, asked: bool = False, visible: bool = True) -> None:
        self._asked = asked
        self._visible = visible
        self._settings = _LiveSettings()
        self.engines: list = []
        self.refreshed = 0

    def set_engine(self, engine, model_cache=None):
        self.engines.append((engine, model_cache))

    def refresh(self):
        self.refreshed += 1

    def isVisible(self):                                  # noqa: N802 - Qt's naming
        return self._visible


def test_an_open_pop_out_describes_with_the_engine_switched_in_settings(qapp, png):
    from app.ui.controllers.settings_controller import _apply_engine_change

    shown = PreviewWindow(_Row(png), state={})
    try:
        assert shown._chat_engine == "onnx"
        window, _notes = _window()
        _apply_engine_change(window, "ollama")
        assert shown._chat_engine == "ollama"             # already open, and it followed
        assert PreviewWindow(_Row(png), state={})._chat_engine == "ollama"   # and a new one
        _apply_engine_change(window, "onnx")
        assert shown._chat_engine == "onnx"
    finally:
        shown.close()


def test_describe_asks_the_engine_it_holds_at_the_moment_of_the_press(qapp, png, monkeypatch):
    from app.llm import engines
    from app.ui.controllers.settings_controller import _apply_engine_change

    asked: list = []
    monkeypatch.setattr(engines, "vision_model",
                        lambda settings, url, model: asked.append((settings.chat_engine, url, model)))
    shown = PreviewWindow(_Row(png), state={})
    try:
        _apply_engine_change(_window()[0], "ollama")
        preview_window._describe_client(shown._ollama_url, shown._ollama_vision_model,
                                        shown._chat_engine)
        assert asked == [("ollama", "http://127.0.0.1:11434", "llava")]
    finally:
        shown.close()


def test_a_pop_out_told_its_engine_keeps_it(qapp, png):
    from app.ui.controllers.settings_controller import _apply_engine_change

    shown = PreviewWindow(_Row(png), state={}, chat_engine="onnx",
                          ollama_vision_model="moondream")
    try:
        _apply_engine_change(_window()[0], "ollama")
        assert shown._chat_engine == "onnx" and shown._ollama_vision_model == "moondream"
    finally:
        shown.close()


def test_a_saved_photo_description_model_reaches_describe_and_survives_an_engine_switch(qapp, png):
    from app.ui.controllers.settings_controller import _apply_engine_change, _apply_written_settings

    shown = PreviewWindow(_Row(png), state={})
    try:
        window, _notes = _window()
        _apply_written_settings(window, {"OLLAMA_VISION_MODEL": "qwen2.5vl",
                                         "OLLAMA_URL": "http://box:11434"})
        assert (shown._ollama_vision_model, shown._ollama_url) == ("qwen2.5vl", "http://box:11434")
        # The window's start-up settings still say llava: the switch must not bring it back.
        _apply_engine_change(window, "ollama")
        assert (shown._ollama_vision_model, shown._ollama_url, shown._chat_engine) == (
            "qwen2.5vl", "http://box:11434", "ollama")
    finally:
        shown.close()


def test_the_settings_boxes_that_read_the_engine_follow_the_switch(qapp):
    from app.ui.controllers.settings_controller import _apply_engine_change

    vision = _Recorder(asked=False, visible=True)
    manager = _Recorder(asked=True)
    needed = _Recorder(asked=False)
    view = SimpleNamespace(_settings=None, vision_field=vision, model_manager=manager,
                           needed_models=needed)
    window, _notes = _window(view)
    _apply_engine_change(window, "ollama")

    assert vision.engines == [("ollama", None)]
    assert vision.refreshed == 1                          # on screen: Ollama's list asked for
    assert manager._settings.chat_engine == "ollama" and manager.refreshed == 1
    assert needed._settings.chat_engine == "ollama"
    assert needed.refreshed == 0                          # not drawn yet: reads it when it is

    _apply_engine_change(window, "onnx")
    assert vision.engines[-1] == ("onnx", None) and vision.refreshed == 1
