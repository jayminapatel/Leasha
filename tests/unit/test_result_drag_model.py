r"""`DraggableResultsModel.mimeData()` hands out real file URLs. Workspace §3b.

Layer: L5 widget
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("PyQt6.QtWidgets", exc_type=ImportError)

import os                                                   # noqa: E402
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")        # noqa: E402

from PyQt6.QtGui import QStandardItem                        # noqa: E402
from PyQt6.QtWidgets import QApplication                     # noqa: E402

from app.ui.result_delegate import ROLE_PAYLOAD               # noqa: E402
from app.ui.widgets.result_drag_model import DraggableResultsModel  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def _item(payload) -> QStandardItem:
    item = QStandardItem()
    item.setData(payload, ROLE_PAYLOAD)
    return item


def _model(rows, *, missing=None) -> tuple:
    model = DraggableResultsModel(missing=missing)
    for row in rows:
        model.appendRow(_item(row))
    indexes = [model.index(i, 0) for i in range(model.rowCount())]
    return model, indexes


def test_mime_data_offers_file_urls_for_real_paths(qapp, tmp_path):
    target = tmp_path / "a.pdf"
    target.write_text("x", encoding="utf-8")
    model, indexes = _model([SimpleNamespace(path=str(target))])

    data = model.mimeData(indexes)
    assert data.hasUrls()
    # 2026-10-05: compared as paths. Turning every "/" into a backslash made
    # the expected text Windows-only, and a Mac's real path failed it.
    from pathlib import Path

    assert Path(data.urls()[0].toLocalFile()) == target


def test_mime_data_skips_a_mail_row(qapp):
    model, indexes = _model([SimpleNamespace(path="pst://Store/123")])
    data = model.mimeData(indexes)
    assert not data.hasUrls()


def test_mime_data_skips_paths_already_known_missing(qapp):
    model, indexes = _model(
        [SimpleNamespace(path=r"D:\gone.pdf")], missing=lambda: {r"D:\gone.pdf"})
    data = model.mimeData(indexes)
    assert not data.hasUrls()


def test_mime_data_of_an_empty_selection_has_no_urls(qapp):
    model, _ = _model([])
    data = model.mimeData([])
    assert not data.hasUrls()
