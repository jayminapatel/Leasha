"""Settings, Models & AI: "Models Leasha uses" - each model, Download, Download all.

Layer: L5, offscreen. 2026-10-08, the owner: "can you put the download buttons
to download all needed models individually and a button for all". The engine
(`app/core/model_catalogue.py`) is replaced by a fake here, so nothing goes
online and every download is steered by the test.
"""

from __future__ import annotations

import sys
import threading
import types
from dataclasses import dataclass
from types import SimpleNamespace

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QThread  # noqa: E402
from PySide6.QtWidgets import QPushButton  # noqa: E402

from app.core import model_fetch  # noqa: E402
from app.core.errors import AppError, AppErrorException  # noqa: E402
from app.ui.presenter.needed_models import (  # noqa: E402
    NEEDED_MISSING, NEEDED_NO_LIBRARY, NEEDED_PRESENT, NEEDED_STOPPED,
    needed_failed_words, needed_progress_words, needed_size_words, needed_summary,
)


@dataclass(frozen=True)
class NeededModel:
    key: str
    title: str
    purpose: str
    model: str
    approx_mb: int
    install_default: bool
    optional_note: str = ""


MODELS = [
    NeededModel("meaning", "Meaning model", "Finds files by what they mean",
                "BAAI/bge-small-en-v1.5", 67, True),
    NeededModel("rerank", "Reranker", "Puts the best results first",
                "Xenova/ms-marco-MiniLM-L-6-v2", 80, True),
    NeededModel("photo", "Photo descriptions", "Describes what is in a photo",
                "florence-2-base", 1100, True),
    NeededModel("speech", "Speech", "Turns speech in videos into words", "whisper-base",
                800, True),
    NeededModel("faces", "Faces", "Groups photos of the same person", "buffalo_l", 330,
                False, "Only used when Recognise people in photos is on"),
]


class FakeEngine:
    """Stands in for `app.core.model_catalogue`. Thread-safe enough for one run."""

    def __init__(self, present=(), *, faces_library=True):
        self.here = set(present)
        self.faces_library = faces_library
        self.calls: list[str] = []
        self.threads: list[bool] = []
        self.gate = threading.Event()
        self.gate.set()
        self.fail: dict[str, str] = {}
        self.hold: set[str] = set()       # these wait for should_stop or the gate
        self.module = types.ModuleType("app.core.model_catalogue")
        self.module.NeededModel = NeededModel
        self.module.needed_models = lambda settings=None: list(MODELS)
        self.module.is_present = lambda key, settings=None: key in self.here
        self.module.download = self.download
        self.module.library_available = (
            lambda key, settings=None: key != "faces" or self.faces_library)

    def download(self, key, settings=None, *, on_progress=print, should_stop=None):
        from PySide6.QtWidgets import QApplication

        self.calls.append(key)
        self.threads.append(QThread.currentThread() is QApplication.instance().thread())
        on_progress(f"Downloading {key} ... 45%")
        if key in self.hold:
            for _ in range(500):
                if should_stop is not None and should_stop():
                    return model_fetch.STOPPED
                if self.gate.wait(0.01):
                    break
        if key in self.fail:
            raise AppErrorException(AppError(code="ERR_MODEL_DOWNLOAD", component="test",
                                             message=self.fail[key]))
        self.here.add(key)
        return model_fetch.DONE


def _install(monkeypatch, engine: FakeEngine) -> None:
    import app.core

    monkeypatch.setitem(sys.modules, "app.core.model_catalogue", engine.module)
    monkeypatch.setattr(app.core, "model_catalogue", engine.module, raising=False)


@pytest.fixture()
def make_box(monkeypatch, qtbot):
    def make(engine: FakeEngine):
        _install(monkeypatch, engine)
        from app.ui.widgets.needed_models_box import NeededModelsBox

        box = NeededModelsBox(SimpleNamespace(model_cache=None))
        qtbot.addWidget(box)
        box.resize(760, 600)
        box.show()
        qtbot.waitUntil(lambda: len(box.lines()) == len(MODELS)
                        and box.summary.text().find("on this computer") >= 0,
                        timeout=10_000)
        return box
    return make


def _status(box, key):
    return box.lines()[key].status.text()


# -- the words -------------------------------------------------------------------------

def test_sizes_read_in_mb_or_gb():
    assert needed_size_words(67) == "about 67 MB"
    assert needed_size_words(1100) == "about 1.1 GB"
    assert needed_size_words(0) == ""
    assert needed_size_words(None) == ""


def test_a_percentage_becomes_downloading_n_and_other_lines_show_as_they_came():
    assert needed_progress_words("Downloading florence ... 45%") == "Downloading 45%..."
    assert needed_progress_words("12.5 % of 1.1 GB") == "Downloading 12%..."
    assert needed_progress_words("Connecting to Hugging Face") == "Connecting to Hugging Face"
    assert needed_progress_words("") == "Downloading..."


def test_a_failure_names_its_reason():
    error = AppErrorException(AppError(code="ERR_MODEL_DOWNLOAD", component="test", message="No internet."))
    assert needed_failed_words(error) == "Did not download - No internet."
    assert needed_failed_words(OSError("disk full")) == "Did not download - disk full"


def test_the_summary_line_adds_up():
    present = {"meaning": True, "rerank": True, "photo": False, "speech": False,
               "faces": False}
    # 3 missing: 1100 + 800 (+ faces 330 when it can be downloaded)
    assert needed_summary(MODELS, present) == "2 of 5 on this computer - 2.2 GB still to download"
    assert needed_summary(MODELS, present, {"faces"}) == (
        "2 of 5 on this computer - 1.9 GB still to download")
    assert needed_summary(MODELS, dict.fromkeys(present, True)) == "All 5 on this computer."
    small = {"meaning": False, "rerank": True, "photo": True, "speech": True, "faces": True}
    assert needed_summary(MODELS, small) == "4 of 5 on this computer - 67 MB still to download"
    only_blocked = dict(present, photo=True, speech=True)
    assert needed_summary(MODELS, only_blocked, {"faces"}) == "4 of 5 on this computer."


# -- the box ---------------------------------------------------------------------------

def test_one_line_per_catalogue_model_with_its_words(make_box):
    box = make_box(FakeEngine(present={"meaning"}))
    lines = box.lines()
    assert list(lines) == [m.key for m in MODELS]
    for model in MODELS:
        line = lines[model.key]
        assert line.title.text() == model.title
        assert line.purpose.text() == model.purpose
        assert line.name.text() == model.model
        assert line.size.text() == needed_size_words(model.approx_mb)
    assert lines["faces"].note.text() == "Only used when Recognise people in photos is on"
    assert lines["faces"].note.isVisibleTo(box)
    assert not lines["meaning"].note.isVisibleTo(box)


def test_status_comes_from_is_present(make_box):
    box = make_box(FakeEngine(present={"meaning", "speech"}))
    assert _status(box, "meaning") == NEEDED_PRESENT
    assert _status(box, "speech") == NEEDED_PRESENT
    assert _status(box, "rerank") == NEEDED_MISSING
    assert not box.lines()["meaning"].button.isVisibleTo(box), "nothing to fetch"
    assert box.lines()["rerank"].button.isVisibleTo(box)
    assert box.lines()["rerank"].button.isEnabled()
    assert box.summary.text() == "2 of 5 on this computer - 1.5 GB still to download"


def test_faces_without_their_library_say_so_and_offer_no_button(make_box):
    box = make_box(FakeEngine(present={"meaning"}, faces_library=False))
    faces = box.lines()["faces"]
    assert faces.status.text() == NEEDED_NO_LIBRARY
    assert not faces.button.isVisibleTo(box)
    assert faces.note.isVisibleTo(box)
    assert "1.9 GB still to download" in box.summary.text(), "faces are not counted as fetchable"


def test_download_runs_on_a_worker_shows_progress_then_on_this_computer(make_box, qtbot):
    engine = FakeEngine(present={"meaning", "photo", "speech", "faces"})
    engine.hold = {"rerank"}
    engine.gate.clear()
    box = make_box(engine)
    line = box.lines()["rerank"]
    line.button.click()
    qtbot.waitUntil(lambda: line.status.text() == "Downloading 45%...", timeout=5_000)
    assert line.button.text() == "Stop", "the line's button becomes Stop"
    assert not box.download_all.isEnabled(), "one run at a time"
    engine.gate.set()
    qtbot.waitUntil(lambda: line.status.text() == NEEDED_PRESENT and not box.busy,
                    timeout=5_000)
    assert engine.calls == ["rerank"]
    assert engine.threads == [False], "the download ran off the UI thread"
    assert box.summary.text() == "All 5 on this computer."
    assert not box.download_all.isEnabled()


def test_stop_sets_should_stop(make_box, qtbot):
    engine = FakeEngine(present={"meaning"})
    engine.hold = {"photo"}
    engine.gate.clear()
    box = make_box(engine)
    line = box.lines()["photo"]
    line.button.click()
    qtbot.waitUntil(lambda: line.status.text() == "Downloading 45%...", timeout=5_000)
    line.button.click()                                   # now "Stop"
    qtbot.waitUntil(lambda: not box.busy, timeout=5_000)
    assert line.status.text() == NEEDED_STOPPED
    assert line.button.text() == "Download"
    assert "photo" not in engine.here


def test_download_all_fetches_the_missing_ones_in_order(make_box, qtbot):
    engine = FakeEngine(present={"rerank"}, faces_library=False)
    box = make_box(engine)
    with qtbot.waitSignal(box.models_changed, timeout=10_000):
        box.download_all.click()
    assert engine.calls == ["meaning", "photo", "speech"], "missing, possible, in list order"
    qtbot.waitUntil(lambda: _status(box, "speech") == NEEDED_PRESENT, timeout=5_000)
    assert _status(box, "faces") == NEEDED_NO_LIBRARY


def test_stop_ends_the_whole_run(make_box, qtbot):
    engine = FakeEngine(present=set())
    engine.hold = {"rerank"}
    engine.gate.clear()
    box = make_box(engine)
    box.download_all.click()
    qtbot.waitUntil(lambda: _status(box, "rerank") == "Downloading 45%...", timeout=5_000)
    assert box.download_all.text() == "Stop"
    box.download_all.click()
    qtbot.waitUntil(lambda: not box.busy, timeout=5_000)
    assert engine.calls == ["meaning", "rerank"], "nothing started after Stop"
    assert _status(box, "meaning") == NEEDED_PRESENT
    assert _status(box, "rerank") == NEEDED_STOPPED
    assert _status(box, "photo") == NEEDED_MISSING
    assert box.download_all.text() == "Download all"
    assert box.download_all.isEnabled()


def test_a_failure_shows_on_its_line_and_the_run_carries_on(make_box, qtbot):
    engine = FakeEngine(present={"meaning"})
    engine.fail = {"photo": "The connection dropped."}
    box = make_box(engine)
    with qtbot.waitSignal(box.models_changed, timeout=10_000):
        box.download_all.click()
    assert engine.calls == ["rerank", "photo", "speech", "faces"]
    qtbot.waitUntil(lambda: not box.busy and _status(box, "faces") == NEEDED_PRESENT,
                    timeout=5_000)
    assert _status(box, "photo") == "Did not download - The connection dropped."
    assert box.lines()["photo"].status.toolTip() == "", "no detail was given to show"
    assert _status(box, "speech") == NEEDED_PRESENT
    photo = box.lines()["photo"]
    assert photo.button.isVisibleTo(box) and photo.button.isEnabled(), "it can be tried again"


def test_the_list_is_not_read_until_the_box_is_shown(monkeypatch, qtbot):
    engine = FakeEngine()
    asked = []
    engine.module.needed_models = lambda settings=None: asked.append(1) or list(MODELS)
    _install(monkeypatch, engine)
    from app.ui.widgets.needed_models_box import NeededModelsBox

    box = NeededModelsBox(None)
    qtbot.addWidget(box)
    qtbot.wait(100)
    assert not asked, "nothing is looked at while the page is only being built"
    box.show()
    qtbot.waitUntil(lambda: bool(asked) and len(box.lines()) == len(MODELS), timeout=10_000)


def test_every_button_has_an_icon_a_tooltip_and_a_name(make_box, qtbot):
    engine = FakeEngine(present={"meaning"})
    engine.hold = {"rerank"}
    engine.gate.clear()
    box = make_box(engine)

    def check():
        for button in box.findChildren(QPushButton):
            assert not button.icon().isNull(), button.text()
            assert button.toolTip().strip(), button.text()
            assert button.accessibleName().strip(), button.text()
    check()
    box.download_all.click()
    qtbot.waitUntil(lambda: _status(box, "rerank") == "Downloading 45%...", timeout=5_000)
    check()                                                 # the Stop faces too
    engine.gate.set()
    qtbot.waitUntil(lambda: not box.busy, timeout=10_000)


def test_the_box_is_first_on_models_and_ai(monkeypatch, qtbot, tmp_path):
    _install(monkeypatch, FakeEngine())
    from app.core.config import load_settings
    from app.ui.settings_view import CATEGORY_MODELS, SettingsView
    from app.ui.widgets.needed_models_box import NeededModelsBox
    from tests.unit.test_pages_reorg import ENV

    env = tmp_path / ".env"
    env.write_text(ENV.format(d=tmp_path.as_posix()), encoding="utf-8")
    view = SettingsView(load_settings(env))
    qtbot.addWidget(view)
    page = view._nav.page(CATEGORY_MODELS)
    boxes = [page.layout().itemAt(i).widget() for i in range(page.layout().count())]
    first = next(w for w in boxes if w is not None)
    assert isinstance(first, NeededModelsBox)
    assert first is view.needed_models
    assert first.title() == "Models Leasha uses"


def test_a_failure_keeps_its_detail_and_fix_for_the_tooltip():
    from app.core.errors import make_error
    from app.ui.presenter.needed_models import needed_failed_tip

    error = AppErrorException(make_error("ERR_MODEL_DOWNLOAD", "test", model="x",
                                         details="could not start the download: refused"))
    tip = needed_failed_tip(error)
    assert tip.startswith("could not start the download: refused")
    assert "internet connection" in tip, "what to do comes after"
    assert needed_failed_tip(OSError("disk full")) == ""


def test_the_real_catalogue_fills_the_box(monkeypatch, qtbot, tmp_path):
    """The engine as written, its disk look and the faces library stubbed."""
    import app.core.model_catalogue as real
    from app.core.config import load_settings
    from app.ui.widgets.needed_models_box import NeededModelsBox
    from tests.unit.test_pages_reorg import ENV

    monkeypatch.setattr(real, "is_present", lambda key, settings=None: key == "search")
    monkeypatch.setattr(model_fetch, "faces_installed", lambda: False)
    env = tmp_path / ".env"
    env.write_text(ENV.format(d=tmp_path.as_posix()), encoding="utf-8")
    settings = load_settings(env)
    box = NeededModelsBox(settings)
    qtbot.addWidget(box)
    box.show()
    qtbot.waitUntil(lambda: len(box.lines()) == len(real.KEYS), timeout=10_000)
    assert list(box.lines()) == list(real.KEYS)
    assert _status(box, "search") == NEEDED_PRESENT
    assert _status(box, "rerank") == NEEDED_MISSING
    faces = box.lines()["faces"]
    assert faces.status.text() == NEEDED_NO_LIBRARY and not faces.button.isVisibleTo(box)
    assert "insightface" in faces.status.toolTip(), "the tooltip says how to get it"
    assert faces.note.isVisibleTo(box) and "Recognise people in photos" in faces.note.text()
    assert box.summary.text().startswith(f"1 of {len(real.KEYS)} on this computer - ")
