r"""Every model is chosen from a drop-down, and a missing one can be downloaded.

Layer: L5 (and L0 for `app/core/model_fetch.py`, which needs no display)

**The owner, 2026-09-29:** *"where there are models it has to be dropdown only
no manual entry for models, if there are other options add them and put a
mechanism to download.. but no manual on models"*. What these hold:

* **no model can be typed anywhere** - every control named after a `*_MODEL`
  setting is a non-editable drop-down (the one exception is the read-only
  display of the meaning model in use, which nobody can type into either);
* **a saved model the list does not know is shown as itself, marked**, and is
  what the control reports - never silently swapped for the first entry;
* **the added options are ones the code can load** - checked against
  fastembed's own catalogue and the vision-name rule the Chat grid uses;
* **downloading never runs on the window's thread**, reports progress, and
  stops when asked - with a fake Ollama and a fake child process, so nothing
  here touches the network.
"""

from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

pytest.importorskip("PyQt6")

from PyQt6.QtWidgets import QApplication, QComboBox, QLineEdit, QWidget  # noqa: E402

from app.core import model_fetch  # noqa: E402
from app.core.errors import AppErrorException  # noqa: E402
from app.core.settings_registry import SETTINGS  # noqa: E402

MODEL_KEYS = sorted(s.key for s in SETTINGS if s.key.endswith("_MODEL"))


@pytest.fixture
def qapp():
    return QApplication.instance() or QApplication([])


def _wait(app, condition, seconds: float = 10.0) -> bool:
    import time

    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.processEvents()
        if condition():
            return True
        time.sleep(0.01)
    return False


# -- no manual entry, anywhere -----------------------------------------------------

def test_every_model_setting_is_known_here():
    """A new `*_MODEL` setting has to be looked at, not waved through."""
    assert MODEL_KEYS == [
        "CHAT_MODEL", "CHAT_PLANNER_MODEL", "CHAT_ROUTER_MODEL", "EMBED_MODEL",
        "OLLAMA_MODEL", "OLLAMA_VISION_MODEL", "RERANK_MODEL", "TRANSCRIBE_MODEL",
    ]


@pytest.mark.gui
def test_no_model_control_in_the_window_can_be_typed_into(gui_mainwindow):
    app, window, _store, _engine = gui_mainwindow
    for _ in range(20):
        app.processEvents()
    for key in MODEL_KEYS:
        controls = window.findChildren(QWidget, key)
        if key == "EMBED_MODEL":
            # Chosen in the "Change the meaning model" dialog (below); on the
            # page it is only the read-only display of the model in use.
            assert controls and all(isinstance(c, QLineEdit) and c.isReadOnly()
                                    for c in controls), key
            continue
        assert controls, f"{key} has no control in the window"
        for control in controls:
            assert isinstance(control, QComboBox), f"{key} is a {type(control).__name__}"
            assert not control.isEditable(), f"{key} can be typed into"
    # The Interpret model's box too, which carries the key as its name.
    assert window.findChild(QComboBox, "OLLAMA_MODEL") is not None


def test_the_meaning_model_dialog_cannot_be_typed_into(qapp):
    from app.ui.widgets.index_flows import RebuildVectorsDialog

    dialog = RebuildVectorsDialog("BAAI/bge-small-en-v1.5", chunk_count=1)
    assert not dialog.model.isEditable()


def test_the_rerank_model_cannot_be_typed_into_and_keeps_an_unlisted_one(qapp):
    from app.ui.widgets.search_box import NOT_LISTED, SearchBox

    box = SearchBox(SimpleNamespace(rerank_model="someone/old-reranker", rerank_enabled=True))
    assert not box.rerank_model.isEditable()
    assert box.chosen_model() == "someone/old-reranker"
    assert NOT_LISTED in box.rerank_model.currentText()
    assert box.values()["RERANK_MODEL"] == "someone/old-reranker", "the file must not change"


def test_a_listed_rerank_model_reports_its_identifier_only(qapp):
    from app.ui.widgets.search_box import SearchBox

    box = SearchBox(SimpleNamespace(rerank_model="jinaai/jina-reranker-v1-turbo-en"))
    assert box.chosen_model() == "jinaai/jina-reranker-v1-turbo-en"


def test_an_unlisted_speech_model_is_shown_rather_than_swapped_for_base(qapp):
    from app.ui.widgets.media_box import NOT_LISTED, MediaBox

    box = MediaBox(SimpleNamespace(transcribe_model="large-v2", model_cache=None))
    assert not box.model.isEditable()
    assert box.values()["TRANSCRIBE_MODEL"] == "large-v2"
    assert NOT_LISTED in box.model.currentText()


def test_the_interpret_model_marks_a_saved_model_that_is_not_installed(qapp):
    from app.ui.widgets.model_box import ModelBox

    box = ModelBox(client_factory=lambda: SimpleNamespace(available_models=lambda: []))
    box._configured = "mistral"
    box._show_models(["llama3.2:3b"])
    assert not box.model.isEditable()
    assert box.model.currentData() == "mistral"
    assert "not installed" in box.model.currentText()
    offered = [name for name, _note in box.download._offers]
    assert "qwen2.5:1.5b" in offered and "llama3.2:3b" not in offered


def test_the_photo_model_on_the_models_page_is_a_drop_down(qapp):
    from app.chat.roles import InstalledModels
    from app.ui.widgets.chat_roles import ModelCombo
    from app.ui.widgets.vision_model import VisionModelField

    combo = ModelCombo("OLLAMA_VISION_MODEL", vision=True, automatic="Automatic (llava)")
    field = VisionModelField(combo, saved="bakllava:old")
    assert not combo.isEditable()
    assert combo.value() == "bakllava:old", "a saved value is kept when Ollama is not asked"

    field._probed(InstalledModels(reachable=True, names=("moondream:latest", "mistral"),
                                  vision=("moondream:latest",)))
    assert combo.value() == "bakllava:old" and "not installed" in combo.currentText()
    assert combo.findData("moondream:latest") >= 0
    offered = [name for name, _note in field.download._offers]
    assert "llava" in offered and "moondream" not in offered


# -- the added options are loadable --------------------------------------------------

def test_every_listed_meaning_model_is_in_fastembeds_catalogue_with_its_width():
    fastembed = pytest.importorskip("fastembed")
    from app.ui.widgets.index_flows import EMBED_MODELS

    catalogue = {m["model"]: m["dim"] for m in fastembed.TextEmbedding.list_supported_models()}
    for identifier, dim, _note in EMBED_MODELS:
        assert identifier in catalogue, f"{identifier} cannot be loaded by fastembed"
        assert catalogue[identifier] == dim, f"{identifier} is {catalogue[identifier]} wide"


def test_every_listed_reranker_is_in_fastembeds_catalogue():
    pytest.importorskip("fastembed")
    from fastembed.rerank.cross_encoder import TextCrossEncoder

    from app.ui.widgets.search_box import RERANK_MODELS

    known = {m["model"] for m in TextCrossEncoder.list_supported_models()}
    for identifier, _note in RERANK_MODELS:
        assert identifier in known, identifier


def test_every_suggested_vision_model_is_recognised_as_one():
    from app.chat.roles import is_vision_name
    from app.llm.models import VISION_SUGGESTED

    assert len(VISION_SUGGESTED) >= 5
    for name, _note in VISION_SUGGESTED:
        assert is_vision_name(name), name


def test_the_speech_sizes_are_ones_faster_whisper_names():
    """faster-whisper 1.2.1's own table (`faster_whisper.utils._MODELS`), copied
    here because the package is optional and absent from most test machines."""
    from app.extract import transcribe

    faster_whisper_1_2_1 = {
        "tiny", "tiny.en", "base", "base.en", "small", "small.en", "medium", "medium.en",
        "large-v1", "large-v2", "large-v3", "large", "distil-large-v2", "distil-medium.en",
        "distil-small.en", "distil-large-v3", "distil-large-v3.5", "large-v3-turbo", "turbo",
    }
    assert set(transcribe.MODELS) <= faster_whisper_1_2_1
    for name in transcribe.MODELS:
        assert name in model_fetch.APPROX_MB, name


# -- downloading: off the window's thread, with progress and Stop ------------------

class _FakeOllama:
    """`pull` the way `OllamaClient.pull` behaves, recording where it ran."""

    def __init__(self, *, forever: bool = False) -> None:
        self.threads: list[threading.Thread] = []
        self.forever = forever
        self.installed: list[str] = []

    def available_models(self) -> list[str]:
        return list(self.installed)

    def pull(self, name, *, on_status=None, should_stop=None, timeout=0):
        self.threads.append(threading.current_thread())
        for done in range(0, 101, 25):
            if should_stop is not None and should_stop():
                return False
            on_status({"status": "pulling", "total": 100 << 20, "completed": done << 20})
            if self.forever:
                should_stop_seen = threading.Event()
                while not (should_stop and should_stop()):
                    should_stop_seen.wait(0.01)
                return False
        self.installed.append(name)
        return True


def test_a_download_runs_off_the_window_thread_and_says_how_far_it_got(qapp):
    from app.ui.widgets.model_download import DownloadRow

    fake = _FakeOllama()
    row = DownloadRow("ollama", client_factory=lambda: fake)
    seen: list[str] = []
    row._relay.said.connect(seen.append)
    ended: list[tuple] = []
    row.finished.connect(lambda name, result: ended.append((name, result)))

    row.set_offers([("llava", "7B")])
    assert row.download.isEnabled()
    row.start("llava")

    assert _wait(qapp, lambda: ended), "the download never finished"
    assert ended == [("llava", model_fetch.DONE)]
    assert fake.threads and all(t is not threading.main_thread() for t in fake.threads), (
        "the pull ran on the window's thread")
    assert any("%" in text for text in seen), seen
    assert "downloaded" in row.status.text()


def test_stop_ends_a_download_and_says_what_was_kept(qapp):
    from app.ui.widgets.model_download import DownloadRow

    fake = _FakeOllama(forever=True)
    row = DownloadRow("ollama", client_factory=lambda: fake)
    ended: list[tuple] = []
    row.finished.connect(lambda name, result: ended.append((name, result)))
    row.start("llama3.2-vision")
    assert _wait(qapp, lambda: fake.threads), "the download never started"
    assert row.stop.isVisibleTo(row)

    row.stop.click()

    assert _wait(qapp, lambda: ended)
    assert ended == [("llama3.2-vision", model_fetch.STOPPED)]
    assert "kept" in row.status.text()


def test_a_failed_download_says_what_happened_and_what_to_do(qapp):
    from app.core.errors import make_error
    from app.ui.widgets.model_download import DownloadRow

    class Broken(_FakeOllama):
        def pull(self, name, **_kw):
            raise AppErrorException(make_error("ERR_MODEL_DOWNLOAD", "test", model=name,
                                               details="no such model"))

    row = DownloadRow("ollama", client_factory=Broken)
    row.start("nonsense")
    assert _wait(qapp, lambda: "ERR_MODEL_DOWNLOAD" in row.status.text())
    assert "press Download again" in row.status.text()
    assert row.download.isEnabled()


def test_whether_a_model_is_here_is_asked_off_the_window_thread(qapp, monkeypatch):
    from app.ui.widgets.model_download import DownloadRow

    asked: list[threading.Thread] = []

    def present(kind, name, **_kw):
        asked.append(threading.current_thread())
        return False

    monkeypatch.setattr(model_fetch, "present", present)
    row = DownloadRow("rerank", model_cache="C:/models")
    row.show()
    row.set_target("Xenova/ms-marco-MiniLM-L-6-v2")
    assert _wait(qapp, lambda: row.download.isEnabled())
    assert asked and asked[0] is not threading.main_thread()
    assert "about 80 MB" in row.status.text()
    assert "nothing is downloaded unless you press it" in row.status.text()
    row.hide()


def test_nothing_is_asked_while_the_row_has_never_been_shown(qapp, monkeypatch):
    from app.ui.widgets.model_download import DownloadRow

    asked: list[str] = []
    monkeypatch.setattr(model_fetch, "present", lambda *a, **k: asked.append("x") or False)
    row = DownloadRow("embed", model_cache="C:/models")
    row.set_target("BAAI/bge-small-en-v1.5")
    for _ in range(5):
        qapp.processEvents()
    assert asked == [], "a page being built must not go looking on disk"


# -- the fetch module, no display -------------------------------------------------------

def test_ollama_pull_reports_each_line_and_finishes_on_success():
    from app.llm.ollama import OllamaClient

    lines = [{"status": "pulling manifest"},
             {"status": "pulling abc", "total": 10, "completed": 5},
             {"status": "success"}]
    sent: list = []

    def transport(method, url, payload, timeout):
        sent.append((method, url, payload))
        return iter(lines)

    client = OllamaClient("http://127.0.0.1:11434", transport=transport)
    seen: list[dict] = []
    assert client.pull("llava", on_status=seen.append) is True
    assert sent[0][0] == "STREAM" and sent[0][1].endswith("/api/pull")
    assert sent[0][2]["model"] == "llava" and sent[0][2]["stream"] is True
    assert seen == lines


def test_ollama_pull_turns_an_error_line_into_the_download_error():
    from app.llm.ollama import OllamaClient

    client = OllamaClient(transport=lambda *a: iter([{"error": "pull model manifest: file does not exist"}]))
    with pytest.raises(AppErrorException) as raised:
        client.pull("no-such-model")
    assert raised.value.error.code == "ERR_MODEL_DOWNLOAD"
    assert "does not exist" in raised.value.error.details


def test_ollama_pull_stops_when_asked():
    from app.llm.ollama import OllamaClient

    client = OllamaClient(transport=lambda *a: iter([{"status": "pulling", "total": 9}] * 50))
    assert client.pull("llava", should_stop=lambda: True) is False


class _FakeChild:
    def __init__(self, rounds: int, code: int = 0) -> None:
        self.rounds, self.code, self.killed, self.returncode = rounds, code, False, None

    def poll(self):
        if self.killed:
            self.returncode = -9
        elif self.rounds <= 0:
            self.returncode = self.code
        else:
            self.rounds -= 1
        return self.returncode

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        return self.poll()


def test_a_file_model_downloads_in_a_child_running_the_applications_own_loader(tmp_path):
    started: list = []

    def popen(argv, **kwargs):
        started.append((argv, kwargs))
        return _FakeChild(rounds=2)

    said: list[str] = []
    result = model_fetch.fetch("rerank", "Xenova/ms-marco-MiniLM-L-6-v2",
                               model_cache=tmp_path, on_progress=said.append, popen=popen)
    assert result == model_fetch.DONE
    argv = started[0][0]
    assert "TextCrossEncoder" in argv[2] and argv[3] == "Xenova/ms-marco-MiniLM-L-6-v2"
    assert argv[4] == str(tmp_path)
    assert any("Downloading" in s for s in said)


def test_the_speech_model_goes_where_transcribe_loads_it_from(tmp_path, monkeypatch):
    started: list = []
    # The child is faked and writes nothing, so the checksum check that follows a
    # real download (2026-09-30) is not what this test is about.
    monkeypatch.setattr(model_fetch, "_check_download", lambda *a, **k: None)
    model_fetch.fetch("speech", "base", model_cache=tmp_path,
                      popen=lambda argv, **k: started.append(argv) or _FakeChild(0))
    # 2026-09-29: the ONNX export of that size, into MODEL_CACHE like every ONNX
    # model (transcribe also looks in MODEL_CACHE\whisper for older downloads).
    assert started[0][3] == "onnx-community/whisper-base"
    assert started[0][4] == str(tmp_path)
    assert "snapshot_download" in started[0][2] and "onnx/encoder_model.onnx" in started[0][5]
    assert started[0][6], "the pinned revision from catalogue.json is passed"


def test_stop_kills_the_child(tmp_path):
    child = _FakeChild(rounds=10_000)
    stop = threading.Event()
    stop.set()
    result = model_fetch.fetch("embed", "BAAI/bge-small-en-v1.5", model_cache=tmp_path,
                               stop=stop, popen=lambda *a, **k: child)
    assert result == model_fetch.STOPPED and child.killed


def test_a_child_that_fails_raises_the_download_error(tmp_path):
    with pytest.raises(AppErrorException) as raised:
        model_fetch.fetch("embed", "BAAI/bge-small-en-v1.5", model_cache=tmp_path,
                          popen=lambda *a, **k: _FakeChild(0, code=1))
    assert raised.value.error.code == "ERR_MODEL_DOWNLOAD"


def test_no_model_cache_means_nothing_is_downloaded():
    with pytest.raises(AppErrorException):
        model_fetch.fetch("embed", "BAAI/bge-small-en-v1.5", model_cache=None,
                          popen=lambda *a, **k: pytest.fail("started a download"))


def test_an_ollama_model_is_here_by_full_or_short_name():
    client = SimpleNamespace(available_models=lambda: ["llava:latest", "qwen2.5vl:7b"])
    assert model_fetch.present("ollama", "llava", client=client)
    assert model_fetch.present("ollama", "qwen2.5vl:7b", client=client)
    assert not model_fetch.present("ollama", "qwen2.5vl:3b", client=client)
    assert not model_fetch.present("ollama", "moondream", client=client)


def test_a_fastembed_model_is_found_in_the_hugging_face_layout(tmp_path):
    pytest.importorskip("fastembed")
    snap = tmp_path / "models--Xenova--ms-marco-MiniLM-L-6-v2" / "snapshots" / "abc" / "onnx"
    snap.mkdir(parents=True)
    assert not model_fetch.present("rerank", "Xenova/ms-marco-MiniLM-L-6-v2", model_cache=tmp_path)
    (snap / "model.onnx").write_bytes(b"x")
    assert model_fetch.present("rerank", "Xenova/ms-marco-MiniLM-L-6-v2", model_cache=tmp_path)


# -- the buttons for a model sit on one line (owner, 2026-09-29) ------------------------

def _row_of(widget, box) -> int:
    from PyQt6.QtCore import QPoint

    return widget.mapTo(box, QPoint(0, widget.height() // 2)).y()


def _laid_out(qapp, box):
    # As the window does: `MainWindow` runs `style_all` over its whole tree,
    # so every button in a row has the button system's height.
    from app.ui.widgets.buttons import style_all

    style_all(box)
    box.resize(700, box.sizeHint().height())
    box.show()
    for _ in range(3):
        qapp.processEvents()
    return box


def test_interpret_refresh_test_and_download_are_on_one_line(qapp):
    """The owner: "the refresh, test and download buttons on ollama should be on
    one line". Download sat on a row of its own under Refresh and Test."""
    from app.ui.widgets.model_box import ModelBox

    box = _laid_out(qapp, ModelBox(
        client_factory=lambda: SimpleNamespace(available_models=lambda: [])))
    row = _row_of(box.refresh_button, box)
    assert _row_of(box.test_button, box) == row
    assert _row_of(box.download.download, box) == row
    box.close()


def test_chat_look_again_and_download_are_on_one_line(qapp):
    """"see for others too" - Chat's Look again and Download were two rows."""
    from app.chat.roles import InstalledModels
    from app.ui.widgets.chat_box import ChatBox

    box = _laid_out(qapp, ChatBox(None, probe=lambda _url: InstalledModels(
        reachable=False, names=(), vision=())))
    assert _row_of(box.download.download, box) == _row_of(box.look_again, box)
    box.close()


def test_photo_model_look_again_and_download_are_on_one_line(qapp):
    from app.ui.widgets.chat_roles import ModelCombo
    from app.ui.widgets.vision_model import VisionModelField

    combo = ModelCombo("OLLAMA_VISION_MODEL", vision=True, automatic="Automatic (llava)")
    field = _laid_out(qapp, VisionModelField(combo, saved="llava"))
    assert _row_of(field.download.download, field) == _row_of(field.look_again, field)
    field.close()
