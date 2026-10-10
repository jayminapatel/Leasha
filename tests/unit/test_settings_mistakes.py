"""Settings cannot be used to stop the app starting, and say when a restart is needed.

2026-10-10. The owner asked for the settings pages to be mistake-proof. The audit found:

* typing `localhost:11434` in the Ollama address was written to `.env`, and the next
  start refused it (`ERR_CONFIG_INVALID`) - one typo, an app that would not open;
* the other free-text boxes took anything;
* eighteen settings only apply at the next start, and only one of them said so when it
  was saved.

`problem_with` (the registry) is asked by `env_writer` before anything is written; the two
address boxes fix what they can and refuse the rest; a saved restart-only setting says so.
"""

from __future__ import annotations

import filecmp
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core import settings_registry as reg
from app.core.config import load_settings
from app.core.env_writer import apply_values, write_env
from app.core.errors import AppErrorException


@pytest.fixture
def folders(tmp_path: Path) -> Path:
    for name in ("d", "p", "l"):
        (tmp_path / name).mkdir()
    return tmp_path


def _env(folders: Path, **extra: object) -> Path:
    env = folders / ".env"
    if env.exists():
        env.unlink()
    write_env(env, {"DATA_PATH": str(folders / "d"), "PROJECT_PATH": str(folders / "p"),
                    "LOG_PATH": str(folders / "l"), **extra})
    return env


# -- every value a control can produce starts the app ----------------------------------

def _produced_values():
    """What the controls can write, for the kinds that have a closed set or a range."""
    for setting in reg.SETTINGS:
        if setting.kind == "int":
            for value in (setting.minimum, setting.maximum, setting.default):
                if value is not None:
                    yield setting.key, value
        elif setting.kind == "choice":
            for value in setting.choices:
                yield setting.key, value
        elif setting.kind == "bool":
            yield setting.key, True
            yield setting.key, False


@pytest.mark.parametrize("key,value", sorted(set(_produced_values()), key=lambda kv: (kv[0], str(kv[1]))))
def test_every_value_a_control_can_produce_is_accepted_at_startup(folders, key, value):
    """The registry's ranges and choices must sit inside what `load_settings` accepts,
    or a control could write a value the next start refuses."""
    assert reg.problem_with(key, value) == "", "the guard refuses a value the control produces"
    load_settings(_env(folders, **{key: value}), check_writable=False)


# -- the writer refuses what the app could not start with -------------------------------------

@pytest.mark.parametrize("key,value", [
    ("OLLAMA_URL", "localhost:11434"),       # no scheme: the app refuses to start on this
    ("OLLAMA_URL", "ftp://127.0.0.1"),
    ("OLLAMA_URL", "http://"),
    ("CHAT_WEB_SEARXNG_URL", "search.example.com"),
    ("INDEX_DAILY_AT", "2am"),
    ("INDEX_DAILY_AT", "25:99"),
    ("INDEX_MEMORY_MB", 10),
    ("INDEX_CPU_PERCENT", 500),
    ("SEARCH_FIX_SPELLING", "sometimes"),
    ("DEVICE_OCR", "tpu"),
])
def test_a_value_the_app_could_not_use_is_refused_and_the_file_is_left_alone(folders, key, value):
    env = _env(folders)
    before = folders / "before.env"
    shutil.copyfile(env, before)

    with pytest.raises(AppErrorException) as raised:
        apply_values(env, {key: value})

    assert raised.value.error.code == "ERR_CONFIG_INVALID"
    assert filecmp.cmp(env, before, shallow=False), "a refused write changed the file"


def test_good_values_and_removals_are_still_written(folders):
    env = _env(folders)
    written = apply_values(env, {"OLLAMA_URL": "http://127.0.0.1:11434", "INDEX_DAILY_AT": "03:15",
                                 "INDEX_MEMORY_MB": 2048, "SEARCH_RELAX_ON_EMPTY": False})
    assert written["OLLAMA_URL"] == "http://127.0.0.1:11434" and written["INDEX_MEMORY_MB"] == "2048"
    removed = apply_values(env, {"OLLAMA_URL": None, "SOME_KEY_NOBODY_KNOWS": "x"})
    assert "OLLAMA_URL" not in removed and removed["SOME_KEY_NOBODY_KNOWS"] == "x"
    load_settings(env, check_writable=False)


def test_whatever_the_guard_lets_through_for_an_address_starts_the_app(folders):
    for text in ("http://127.0.0.1:11434", "https://ollama.example.com", "http://host:80/path"):
        assert reg.problem_with("OLLAMA_URL", text) == ""
        load_settings(_env(folders, OLLAMA_URL=text), check_writable=False)


def test_a_bare_address_is_made_whole_not_refused_by_the_helper():
    assert reg.normalise_url("localhost:11434") == ("http://localhost:11434", "")
    assert reg.normalise_url("  http://a:1  ") == ("http://a:1", "")
    assert reg.normalise_url("") == ("", "")
    assert reg.normalise_url("a b")[1] and reg.normalise_url("host:99999")[1]


# -- the boxes ---------------------------------------------------------------------------------

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402


@pytest.fixture
def qapp():
    return QApplication.instance() or QApplication([])


def test_the_ollama_address_box_completes_a_bare_address_and_saves_it_once(qapp):
    from app.ui.widgets.model_box import ModelBox

    box = ModelBox(client_factory=lambda: SimpleNamespace(available_models=lambda: []))
    box.set_url("http://127.0.0.1:11434")
    sent: list[str] = []
    box.url_changed.connect(sent.append)

    box.url.setText("localhost:11500")
    box._url_edited()
    assert sent == ["http://localhost:11500"] and box.url.text() == "http://localhost:11500"

    box._url_edited()                                   # focus left again, nothing new
    assert sent == ["http://localhost:11500"], "an unchanged address was saved again"


def test_the_ollama_address_box_refuses_what_is_not_an_address_and_goes_back(qapp):
    from app.ui.widgets.model_box import ModelBox

    box = ModelBox(client_factory=lambda: SimpleNamespace(available_models=lambda: []))
    box.set_url("http://127.0.0.1:11434")
    sent: list[str] = []
    box.url_changed.connect(sent.append)

    box.url.setText("ftp://nope")
    box._url_edited()

    assert sent == [] and box.url.text() == "http://127.0.0.1:11434"
    assert "not changed" in box.status.text()


def _chat_box():
    from app.ui.widgets.chat_box import ChatBox

    settings = SimpleNamespace(
        chat_engine="onnx", chat_model="", ollama_url="http://127.0.0.1:11434",
        ollama_model="qwen2.5:1.5b", model_cache=None, chat_max_rounds=3,
        chat_context_tokens=4096, chat_verify_strictness=70, chat_web_enabled=False,
        chat_web_ask_first=True, chat_style_note="", chat_planner_model="",
        chat_router_model="", chat_web_provider="auto", chat_web_searxng_url="",
        chat_web_brave_key="", chat_web_show_query=True)
    return ChatBox(settings)


def test_the_web_search_address_box_completes_and_refuses_like_the_ollama_one(qapp):
    box = _chat_box()
    sent: list[dict] = []
    box.changed.connect(sent.append)
    control = box.controls["CHAT_WEB_SEARXNG_URL"]

    control.setText("search.example.com")
    box._emit("CHAT_WEB_SEARXNG_URL")
    assert sent[-1] == {"CHAT_WEB_SEARXNG_URL": "http://search.example.com"}
    assert control.text() == "http://search.example.com"

    count = len(sent)
    control.setText("not an address")
    box._emit("CHAT_WEB_SEARXNG_URL")
    assert len(sent) == count and control.text() == "http://search.example.com"


# -- a saved setting that needs a restart says so ------------------------------------------

def _window():
    notes: list[str] = []
    window = SimpleNamespace(
        notify=lambda message, ms=0: notes.append(message), _settings_overrides={},
        _apply_search_preferences=lambda: None, _apply_hotkey=lambda: None, _engine=None)
    return window, notes


def test_a_saved_setting_that_needs_a_restart_says_so(qapp):
    from app.ui.controllers.settings_controller import _apply_written_settings

    window, notes = _window()
    _apply_written_settings(window, {"DEVICE_OCR": "cpu", "EMBED_QUANTISED": True})

    assert len(notes) == 1 and "Restart Leasha" in notes[0]
    assert reg.by_key("DEVICE_OCR").label in notes[0]


def test_a_setting_that_applies_at_once_does_not_ask_for_a_restart(qapp):
    from app.ui.controllers.settings_controller import _apply_written_settings

    window, notes = _window()
    _apply_written_settings(window, {"SEARCH_RELAX_ON_EMPTY": False})

    assert not any("Restart" in note for note in notes)


# -- what only matters while its switch is on is greyed out while it is off ---------------------

def test_the_media_options_follow_their_switches(qapp):
    from app.ui.widgets.media_box import MediaBox

    box = MediaBox(SimpleNamespace(video_indexing_enabled=False, audio_transcription_enabled=False,
                                   transcribe_model="base", video_keyframe_interval_s=60,
                                   video_keyframe_cap=200, model_cache=None))
    assert not box.interval.isEnabled() and not box.cap.isEnabled()
    assert not box.model.isEnabled() and not box.download.isEnabled()

    box.video.setChecked(True)
    assert box.interval.isEnabled() and box.cap.isEnabled()
    assert not box.model.isEnabled(), "the speech model followed the video switch"

    box.audio.setChecked(True)
    assert box.model.isEnabled()
    box.video.setChecked(False)
    assert not box.interval.isEnabled() and box.model.isEnabled()


def test_the_web_search_options_follow_the_web_switch(qapp):
    box = _chat_box()
    options = [box.controls[key] for key in box.controls
               if key.startswith("CHAT_WEB_") and key != "CHAT_WEB_ENABLED"]
    assert options, "no web options found"
    assert not any(control.isEnabled() for control in options)
    assert box.controls["CHAT_WEB_ENABLED"].isEnabled(), "the switch itself must stay usable"

    box.controls["CHAT_WEB_ENABLED"].setChecked(True)
    assert all(control.isEnabled() for control in options)


def test_the_shortcut_options_follow_the_shortcut_switch(qapp):
    from app.ui.widgets.search_behaviour_box import SearchBehaviourBox

    box = SearchBehaviourBox(SimpleNamespace(mini_search_enabled=False, mini_search_hotkey="Ctrl+Alt+K"))
    assert not box.mini_hotkey.isEnabled() and not box.mini_prefill.isEnabled()
    assert box.mini_search.isEnabled()
    box.mini_search.setChecked(True)
    assert box.mini_hotkey.isEnabled() and box.mini_prefill.isEnabled()
