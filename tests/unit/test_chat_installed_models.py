"""Which models are installed, what they need in memory, and the roles grid that shows it.

Layer: L8b (the probe and the memory sentence) and L5 (the grid, pytest-qt). Work order
`202626270611-chat-tab` section 4d: dropdowns of INSTALLED models, the Describe role offering only
models that can read pictures, and one honest line about memory - "these two together need about
6 GB - you have 32". This was built without a test; these are them.
"""

from __future__ import annotations

import threading

import pytest

from app.chat.llm import probe_installed
from app.chat.roles import (
    MEMORY_OVERHEAD, NO_VISION_MODEL_LINE, OLLAMA_UNREACHABLE_LINE, InstalledModels, is_vision_name,
    ram_line, size_of,
)

GB = 1024 ** 3


def tags(*models):
    return {"models": [{"name": n, "size": int(s * GB)} for n, s in models]}


# --------------------------------------------------------------------------- the probe

def transport(tags_body, shows=None, *, fail_tags=False):
    calls: list[tuple] = []

    def call(method, path, payload):
        calls.append((method, path, payload))
        if path == "/api/tags":
            if fail_tags:
                raise ConnectionError("refused")
            return tags_body
        if path == "/api/show":
            body = (shows or {}).get(payload["model"])
            if isinstance(body, Exception):
                raise body
            return body if body is not None else {}
        raise AssertionError(path)

    call.calls = calls
    return call


def test_the_probe_lists_installed_models_their_sizes_and_which_can_read_pictures():
    t = transport(tags(("mistral:latest", 4.1), ("llava:7b", 4.7), ("nomic-embed-text:latest", 0.3)),
                  {"mistral:latest": {"capabilities": ["completion"]},
                   "llava:7b": {"capabilities": ["completion", "vision"]}})
    found = probe_installed("http://127.0.0.1:11434", transport=t)
    assert found.reachable and found.names == ("mistral:latest", "llava:7b")      # the embedder is not a chat model
    assert found.vision == ("llava:7b",) and round(found.sizes["mistral:latest"] / GB, 1) == 4.1
    assert found.ram_mb > 0


def test_capabilities_beat_the_name_and_the_name_is_only_the_fallback_for_older_builds():
    t = transport(tags(("mystery-model", 3.0), ("llava:7b", 4.7), ("qwen2.5vl:7b", 6.0)),
                  {"mystery-model": {"capabilities": ["completion", "vision"]},
                   "llava:7b": {"capabilities": ["completion"]},                    # says it cannot, though named like one
                   "qwen2.5vl:7b": {}})                                             # an older Ollama: no capabilities at all
    found = probe_installed("http://x", transport=t)
    assert found.vision == ("mystery-model", "qwen2.5vl:7b")


def test_an_ollama_that_is_not_answering_is_a_normal_state_never_an_exception():
    found = probe_installed("http://127.0.0.1:9", transport=transport({}, fail_tags=True))
    assert found.reachable is False and found.names == () and found.vision == ()


def test_one_model_whose_details_cannot_be_read_never_stops_the_list():
    t = transport(tags(("a:1b", 1.0), ("b:1b", 1.0)), {"a:1b": ConnectionError("boom"),
                                                       "b:1b": {"capabilities": ["vision"]}})
    found = probe_installed("http://x", transport=t)
    assert found.names == ("a:1b", "b:1b") and found.vision == ("b:1b",)


def test_vision_names():
    for name in ("llava:7b", "bakllava", "moondream", "minicpm-v", "qwen2.5vl:7b", "gemma3-vision"):
        assert is_vision_name(name), name
    for name in ("mistral", "llama3.2:1b", "gpt-oss:20b", "qwen2.5:1.5b"):
        assert not is_vision_name(name), name


# --------------------------------------------------------------------------- the memory sentence

SIZES = {"mistral:latest": int(4.1 * GB), "llama3.2:1b": int(1.2 * GB), "gpt-oss:20b": int(13 * GB)}


def test_size_of_tolerates_the_short_name_and_says_zero_when_it_does_not_know():
    assert size_of("mistral", SIZES) == SIZES["mistral:latest"]
    assert size_of("mistral:latest", SIZES) == SIZES["mistral:latest"]
    assert size_of("nothing", SIZES) == 0 and size_of("", SIZES) == 0


def test_one_model_and_two_models_are_summed_with_the_overhead_and_set_against_this_computer():
    one = ram_line(["mistral"], SIZES, ram_mb=32 * 1024)
    assert one.startswith("mistral:latest needs about ") and "This computer has 32 GB." in one
    two = ram_line(["mistral", "llama3.2:1b"], SIZES, ram_mb=32 * 1024)
    expected = (SIZES["mistral:latest"] + SIZES["llama3.2:1b"]) * MEMORY_OVERHEAD / GB
    assert f"together need about {expected:.1f} GB" in two and "mistral:latest and llama3.2:1b" in two


def test_a_choice_that_will_not_fit_comfortably_says_so_and_offers_the_one_model_answer():
    line = ram_line(["gpt-oss:20b", "mistral"], SIZES, ram_mb=16 * 1024)
    assert "more than is comfortable" in line and "one model for every job" in line
    assert "comfortable" not in ram_line(["llama3.2:1b"], SIZES, ram_mb=16 * 1024)


def test_no_sentence_is_better_than_a_made_up_number():
    assert ram_line([], SIZES, ram_mb=8192) == ""
    assert ram_line(["ghost-model"], SIZES, ram_mb=8192) == ""
    partial = ram_line(["mistral", "ghost-model"], SIZES, ram_mb=32 * 1024)
    assert "No size is known for ghost-model" in partial and partial.startswith("mistral:latest needs")


# --------------------------------------------------------------------------- the grid

pytest.importorskip("PyQt6")
from PyQt6.QtWidgets import QComboBox, QLabel, QWidget                          # noqa: E402

from app.ui.widgets.chat_box import ChatBox                                      # noqa: E402
from app.ui.widgets.chat_roles import AUTOMATIC, ModelCombo                      # noqa: E402
from tests.unit.conftest import gui_pump                                         # noqa: E402
from tests.unit.test_chat_tab_qt import chat                                     # noqa: E402,F401

pytestmark_gui = pytest.mark.gui

INSTALLED = InstalledModels(
    reachable=True, names=("mistral:latest", "llama3.2:1b", "llava:7b"), vision=("llava:7b",),
    sizes={"mistral:latest": int(4.1 * GB), "llama3.2:1b": int(1.2 * GB), "llava:7b": int(4.7 * GB)},
    ram_mb=32 * 1024)


@pytest.mark.gui
def test_a_role_lists_automatic_then_only_what_is_installed_with_sizes(qtbot):
    combo = ModelCombo("CHAT_MODEL")
    qtbot.addWidget(combo)
    combo.populate(INSTALLED, "")
    assert [combo.itemData(i) for i in range(combo.count())] == ["", "mistral:latest", "llama3.2:1b", "llava:7b"]
    assert combo.itemText(0) == AUTOMATIC and "4.1 GB" in combo.itemText(1) and combo.value() == ""


@pytest.mark.gui
def test_describe_offers_only_models_that_can_read_pictures_and_says_what_to_pull_when_there_are_none(qtbot):
    describe = ModelCombo("OLLAMA_VISION_MODEL", vision=True)
    qtbot.addWidget(describe)
    describe.populate(INSTALLED, "")
    assert [describe.itemData(i) for i in range(describe.count())] == ["", "llava:7b"]
    describe.populate(InstalledModels(reachable=True, names=("mistral:latest",)), "")
    assert describe.count() == 1 and not describe.isEnabled() and describe.reason == NO_VISION_MODEL_LINE
    assert "ollama pull llava" in describe.reason


@pytest.mark.gui
def test_a_saved_choice_is_never_dropped_because_the_list_is_short_or_ollama_is_away(qtbot):
    combo = ModelCombo("CHAT_MODEL")
    qtbot.addWidget(combo)
    combo.populate(INSTALLED, "phi3.5:3.8b")
    assert combo.value() == "phi3.5:3.8b" and "not installed" in combo.currentText()
    assert "ollama pull phi3.5:3.8b" in combo.reason
    combo.populate(InstalledModels(reachable=False), "qwen2.5:1.5b")        # Ollama not answering
    assert combo.value() == "qwen2.5:1.5b" and combo.reason == OLLAMA_UNREACHABLE_LINE
    combo.populate(INSTALLED, "mistral")                                    # the short name finds its installed spelling
    assert combo.value() == "mistral:latest"


@pytest.mark.gui
def test_the_box_asks_ollama_on_a_worker_fills_every_role_and_says_what_they_need(qtbot):
    threads: list[str] = []

    def probe(url):
        threads.append(threading.current_thread().name)
        return INSTALLED

    from types import SimpleNamespace

    box = ChatBox(SimpleNamespace(index_tuning_mode="manual", chat_model="mistral", ollama_url="http://h:1",
                                  ollama_model="mistral", chat_max_rounds=3, chat_verify_strictness=70,
                                  ollama_vision_model="llava:7b"), probe=probe)
    qtbot.addWidget(box)
    box.show()
    qtbot.waitUntil(lambda: bool(threads), timeout=5000)
    qtbot.waitUntil(lambda: box.roles.ram.isVisibleTo(box), timeout=5000)
    assert threads and "MainThread" not in threads                          # never on the window thread
    assert box.controls["CHAT_MODEL"].value() == "mistral:latest"
    assert box._describe.value() == "llava:7b"
    ram = box.roles.ram.text()
    assert "together need about" in ram and "mistral:latest" in ram and "This computer has 32 GB." in ram
    assert "3 models installed" in box.status.text()


@pytest.mark.gui
def test_the_tuning_part_hides_outside_manual_and_the_web_section_never_does(qtbot):
    from types import SimpleNamespace

    box = ChatBox(SimpleNamespace(index_tuning_mode="defaults"), probe=lambda url: INSTALLED)
    qtbot.addWidget(box)
    box.show()
    assert box.manual_part.isHidden() and not box.web_part.isHidden() and box.isVisible()
    box.set_manual(True)
    assert not box.manual_part.isHidden() and not box.web_part.isHidden()


@pytest.mark.gui
def test_a_number_with_an_envelope_states_its_range_its_reason_and_what_automatic_uses(qtbot):
    from types import SimpleNamespace

    box = ChatBox(SimpleNamespace(index_tuning_mode="manual"), probe=lambda url: InstalledModels(
        reachable=True, names=("mistral:latest",), ram_mb=4096))
    qtbot.addWidget(box)
    box.show()
    qtbot.waitUntil(lambda: box._installed is not None, timeout=5000)
    spin = box.controls["CHAT_CONTEXT_TOKENS"]
    note = box.notes["CHAT_CONTEXT_TOKENS"]
    assert (spin.minimum(), spin.maximum()) == (2048, 32768)
    assert "4GB of RAM" in note.text() and "Automatic uses 4096" in note.text()
    rounds = box.notes["CHAT_MAX_ROUNDS"]
    assert "Automatic uses 2" in rounds.text()                              # under 8GB a retry costs a model reload


@pytest.mark.gui
def test_choosing_a_role_emits_its_key_and_the_photo_model_stays_in_step_with_its_other_control(chat):
    c = chat
    box = c.window.settings_view.chat_box
    box.set_installed(INSTALLED)
    seen: list[dict] = []
    box.changed.connect(seen.append)
    combo = box.controls["CHAT_MODEL"]
    combo.setCurrentIndex(combo.findData("llama3.2:1b"))
    combo.activated.emit(combo.currentIndex())
    assert {"CHAT_MODEL": "llama3.2:1b"} in seen
    assert "llama3.2:1b" in box.roles.ram.text()

    c.ctl.attach_settings()                                                 # the wiring the window does once Settings exists
    vision = c.window.settings_view.vision_model
    box._describe.select("llava:7b")
    box._describe.activated.emit(box._describe.currentIndex())
    gui_pump(c.app)
    assert vision.text() == "llava:7b" and {"OLLAMA_VISION_MODEL": "llava:7b"} in seen
    vision.setText("")
    vision.editingFinished.emit()
    assert box._describe.value() == ""


@pytest.mark.gui
def test_the_chat_tuning_mode_reaches_the_engine_and_the_group(chat):
    c = chat
    c.ctl.attach_settings()
    box = c.window.settings_view.chat_box
    c.ctl.engine = object()
    c.ctl._opened = False                                                   # (a tab that is open re-checks Ollama at once)
    c.ctl._tuning_changed({"INDEX_TUNING_MODE": "manual"})
    assert not box.manual_part.isHidden() and c.ctl.engine is None          # rebuilt under the new mode
    c.ctl._tuning_changed({"INDEX_TUNING_MODE": "defaults"})
    assert box.manual_part.isHidden()
