r"""Each model's processor, chosen per model and measured per machine.

Layer: L0/L3/L5

2026-10-04, the owner: "put the gpu flag in the settings and also have a test
button so this can be tested on this machine and a new machine for indexing ...
so it can get tested and the setting set". Measured that night: Florence-2 8.9 s
a photo on the processor, 3.9 s on the integrated graphics, same description.
"""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from app.core import model_devices
from app.index import device_test


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setattr(model_devices, "machine_fingerprint", lambda: "this-laptop")
    return SimpleNamespace(state_path=tmp_path, embed_device="cpu",
                           **{f"device_{m}": "auto" for m, _k, _l in model_devices.MODELS})


# --- choosing -------------------------------------------------------------------------

def test_automatic_follows_run_models_on_until_this_machine_is_tested(settings):
    assert model_devices.device_for(settings, "describe") == "cpu"
    assert model_devices.needs_test(settings)


def test_automatic_follows_this_machines_test(settings):
    model_devices.save_results(settings, {
        "fingerprint": "this-laptop",
        "models": {"describe": {"winner": "gpu"}, "faces": {"winner": "cpu"}}})
    assert model_devices.device_for(settings, "describe") == "gpu"
    assert model_devices.device_for(settings, "faces") == "cpu"
    assert not model_devices.needs_test(settings)


def test_another_computers_test_is_not_believed(settings, monkeypatch):
    model_devices.save_results(settings, {
        "fingerprint": "the-old-desktop", "models": {"describe": {"winner": "gpu"}}})
    assert model_devices.device_for(settings, "describe") == "cpu"
    assert model_devices.needs_test(settings), "a new machine is tested again"


def test_a_choice_made_by_hand_always_wins(settings):
    model_devices.save_results(settings, {
        "fingerprint": "this-laptop", "models": {"describe": {"winner": "gpu"}}})
    settings.device_describe = "cpu"
    settings.device_ocr = "gpu"
    assert model_devices.device_for(settings, "describe") == "cpu"
    assert model_devices.device_for(settings, "ocr") == "gpu"


def test_every_model_asks_for_its_own_processor(settings, monkeypatch):
    """The constructors read `device_for`, not the one shared setting."""
    from app.index.clip_embedder import ClipImageEmbedder
    from app.index.embedder import Embedder
    from app.search.rerank import Reranker

    seen = {}
    monkeypatch.setattr("app.index.embedder._device_for",
                        lambda s, m: seen.setdefault("meaning", m) and "gpu")
    monkeypatch.setattr("app.index.clip_embedder._device_for",
                        lambda s, m: seen.setdefault("pictures", m) and "gpu")
    monkeypatch.setattr("app.search.rerank._device_for",
                        lambda s, m: seen.setdefault("rerank", m) and "gpu")
    full = SimpleNamespace(**vars(settings), embed_dim=384, model_cache="", embed_quantised=False,
                           embed_model="BAAI/bge-small-en-v1.5", rerank_enabled=False,
                           rerank_top_n=30, rerank_window_chars=600, rerank_model="x")
    for build in (Embedder.from_settings, ClipImageEmbedder.from_settings,
                  Reranker.from_settings):
        try:
            build(full)
        except Exception:                        # noqa: BLE001 - no model here; asking is the point
            pass
    assert seen == {"meaning": "meaning", "pictures": "pictures", "rerank": "rerank"}


def test_a_device_that_is_not_one_of_the_three_is_refused(tmp_path):
    from app.core.config import load_settings
    from app.core.errors import AppErrorException

    env = tmp_path / ".env"
    env.write_text(f"DATA_PATH={tmp_path}\nDEVICE_FACES=npu\n", encoding="utf-8")
    with pytest.raises(AppErrorException) as raised:
        load_settings(env)
    assert "DEVICE_FACES" in str(raised.value.error.details or raised.value.error.message) \
        or raised.value.error.code == "ERR_CONFIG_INVALID"


# --- the test --------------------------------------------------------------------------

def _fake(cpu_s, gpu_s, cpu_answer, gpu_answer, gpu_fails=False):
    def runner(_settings, device, _samples):
        if device == "cpu":
            return cpu_s, cpu_answer
        if gpu_fails:
            raise RuntimeError("887A0020 device removed")
        return gpu_s, gpu_answer
    return runner


def test_the_graphics_card_wins_only_when_faster_and_the_same(settings, monkeypatch):
    monkeypatch.setattr(device_test, "gpu_usable", lambda: (True, "usable"))
    runners = {
        "describe": _fake(8.9, 3.9, "A woman in a pink dress", "A woman in a pink dress"),
        "faces": _fake(0.9, 0.4, 3, 2),                       # faster, but wrong
        "ocr": _fake(2.0, 1.9, "Dear Sir", "Dear Sir"),       # the same, barely faster
        "rerank": _fake(0.2, None, [1, 0], [1, 0], gpu_fails=True),
        "meaning": _fake(0.2, 0.1, [[1.0, 0.0]], [[0.999, 0.01]]),
        "pictures": lambda s, d, x: (_ for _ in ()).throw(FileNotFoundError("not downloaded")),
    }
    results = device_test.run_device_test(settings, runners=runners)
    models = results["models"]
    assert models["describe"]["winner"] == "gpu"
    assert models["meaning"]["winner"] == "gpu"
    assert models["faces"]["winner"] == "cpu" and "different answer" in models["faces"]["note"]
    assert models["ocr"]["winner"] == "cpu", "a few percent is not worth a driver"
    assert models["rerank"]["winner"] == "cpu" and "failed" in models["rerank"]["note"]
    assert models["pictures"]["winner"] == "cpu" and "not available" in models["pictures"]["note"]
    assert model_devices.device_for(settings, "describe") == "gpu", "saved and followed"


def test_no_graphics_card_means_everything_stays_on_the_processor(settings, monkeypatch):
    monkeypatch.setattr(device_test, "gpu_usable", lambda: (False, "no display adapter was detected"))
    runners = {m: _fake(1.0, 0.1, 1, 1) for m, _k, _l in model_devices.MODELS}
    results = device_test.run_device_test(settings, runners=runners)
    assert {e["winner"] for e in results["models"].values()} == {"cpu"}
    assert results["gpu"] == "no display adapter was detected"


def test_the_fixed_samples_are_the_same_every_time(tmp_path):
    first = device_test._samples(tmp_path / "a")
    second = device_test._samples(tmp_path / "b")
    for name in ("page", "photo"):
        assert first[name].read_bytes() == second[name].read_bytes()


# --- the window ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def qapp():
    pytest.importorskip("PyQt6.QtWidgets", exc_type=ImportError)
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtWidgets import QApplication

    yield QApplication.instance() or QApplication([])


def test_the_devices_table_has_a_row_and_a_test_for_every_model(qapp, settings):
    from app.ui.widgets.device_box import DeviceBox, result_text

    box = DeviceBox(settings)
    assert set(box.combos) == {m for m, _k, _l in model_devices.MODELS}
    assert {c.objectName() for c in box.combos.values()} == {
        k for _m, k, _l in model_devices.MODELS}
    box.combos["describe"].setCurrentIndex(box.combos["describe"].findData("gpu"))
    assert box.values()["DEVICE_DESCRIBE"] == "gpu"
    box.show_results({"models": {"describe": {"cpu_s": 8.9, "gpu_s": 3.9, "winner": "gpu"}}})
    assert box.results["describe"].text() == (
        "Processor 8.90 s · graphics card 3.90 s - uses the graphics card")
    assert box.results["faces"].text() == "Not yet tested on this computer."
    assert result_text({"winner": "cpu", "note": "not available here"}) == "not available here"
    box.deleteLater()


def test_the_test_button_runs_the_test_and_shows_it(qapp, qtbot, settings, monkeypatch):
    from app.ui.widgets.device_box import DeviceBox

    ran = []

    def fake_run(s, models):
        ran.append(models)
        return {"fingerprint": "this-laptop",
                "models": {"faces": {"cpu_s": 0.9, "gpu_s": 0.5, "winner": "gpu"}}}

    monkeypatch.setattr(device_test, "run_device_test", fake_run)
    box = DeviceBox(settings)
    qtbot.addWidget(box)
    assert box.test(["faces"])
    qtbot.waitUntil(lambda: "graphics card 0.50 s" in box.results["faces"].text(), timeout=15000)
    assert ran == [["faces"]]
    assert box.test_all.isEnabled()


def test_a_run_tests_an_untested_machine_only_when_run_models_on_is_automatic(settings, monkeypatch):
    """Every run resolves its tuning first (`resolve_for_run`), so every way of
    starting one - Start, Index now, the schedule, the command line - tests an
    untested machine there, once."""
    from app.index import resolve

    ran = []
    monkeypatch.setattr(device_test, "gpu_usable", lambda: (True, "usable"))
    monkeypatch.setattr(device_test, "run_device_test",
                        lambda s: ran.append(1) or model_devices.save_results(
                            s, {"fingerprint": "this-laptop", "models": {}}))
    assert resolve.test_this_machine_if_new(settings) is False, "Processor chosen: not overruled"
    settings.embed_device = "auto"
    assert resolve.test_this_machine_if_new(settings) is True and ran == [1]
    assert resolve.test_this_machine_if_new(settings) is False, "once per machine"
    assert ran == [1]


def test_a_late_read_of_the_saved_results_never_paints_over_a_fresh_test(qapp, settings):
    """2026-10-05, the full suite: the box's first read of the saved results
    can finish after a quick test, and painted "Not yet tested" over it."""
    from app.ui.widgets.device_box import DeviceBox

    box = DeviceBox(settings)
    box._tested({"models": {"faces": {"cpu_s": 0.9, "gpu_s": 0.5, "winner": "gpu"}}})
    box._stored({})                                  # the saved read lands late
    assert "graphics card 0.50 s" in box.results["faces"].text()
    box.deleteLater()
