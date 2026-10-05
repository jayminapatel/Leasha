"""Settings, Models: the box that lists, removes, chooses and finds models (2026-09-30).

Layer: L5, offscreen. A temporary model folder; the dialog answers are stubbed.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QMessageBox  # noqa: E402

from app.ort import catalogue  # noqa: E402
from tests.unit.test_ort_models_manage import _place  # noqa: E402


@pytest.fixture()
def setup(tmp_path, monkeypatch, qtbot):
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setattr(catalogue, "_state_cache", [state])
    cache = tmp_path / "models"
    cat = catalogue.load(state)
    for key in ("florence-2-base", "florence-2-base-int8", "whisper-base"):
        _place(cache, cat.by_key(key))
    settings = SimpleNamespace(model_cache=cache, state_path=state, chat_engine="onnx",
                               transcribe_model="base", embed_model="BAAI/bge-small-en-v1.5",
                               rerank_model="Xenova/ms-marco-MiniLM-L-6-v2", embed_quantised=False)
    from app.ui.widgets.model_manager import ModelManagerBox

    box = ModelManagerBox(settings)
    qtbot.addWidget(box)
    box.show()
    qtbot.waitUntil(lambda: box.installed.rowCount() >= 3, timeout=10_000)
    return box, cache, state, qtbot


def _row_of(table, text):
    return next(r for r in range(table.rowCount()) if table.item(r, 0).text() == text)


def test_the_box_lists_what_is_here_and_what_can_be_downloaded(setup):
    box, _cache, _state, _qtbot = setup
    labels = [box.installed.item(r, 0).text() for r in range(box.installed.rowCount())]
    assert any("Florence-2 base (photo" in t for t in labels)
    status = {box.installed.item(r, 0).text(): box.installed.item(r, 4).text()
              for r in range(box.installed.rowCount())}
    assert status["Florence-2 base (photo tags and captions)"] == "in use"
    assert status["Florence-2 base, smaller copy (photo tags and captions)"].startswith("not used")
    assert box.available.rowCount() > 50, "the shipped Hugging Face list is offered"
    assert box.clean_button.isEnabled() and "GB" in box.clean_button.text()


def test_the_filter_narrows_the_available_list(setup):
    box, _cache, _state, _qtbot = setup
    before = box.available.rowCount()
    box.filter.setText("gemma")
    after = box.available.rowCount()
    assert 0 < after < before
    assert all("gemma" in box.available.item(r, 0).text().lower() for r in range(after))


def test_delete_asks_first_and_removes_the_copy(setup, monkeypatch):
    box, cache, _state, qtbot = setup
    asked = []
    monkeypatch.setattr(QMessageBox, "question",
                        lambda *a, **k: asked.append(a[2]) or QMessageBox.StandardButton.Yes)
    row = _row_of(box.installed, "Florence-2 base, smaller copy (photo tags and captions)")
    box.installed.selectRow(row)
    box._delete_selected()
    assert asked and "permanent" in asked[0].lower()
    qtbot.waitUntil(lambda: "freed" in box.status.text(), timeout=10_000)
    assert not list(cache.rglob("vision_encoder_int8.onnx"))
    assert list(cache.rglob("vision_encoder.onnx")), "the copy in use stays"


def test_use_this_is_remembered_for_its_job(setup):
    box, _cache, state, qtbot = setup
    row = _row_of(box.installed, "Florence-2 base, smaller copy (photo tags and captions)")
    box.installed.selectRow(row)
    assert box.use_button.isEnabled()
    box._use_selected()
    assert catalogue.chosen("photo", state) == "florence-2-base-int8"
    box._use_recommended()
    assert catalogue.chosen("photo", state) == ""
