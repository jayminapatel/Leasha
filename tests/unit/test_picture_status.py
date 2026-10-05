r"""The Indexing page says how far a photo library has got, as it does for files.

Layer: L1/L5

2026-10-04, the owner: "status of pictures indexing i.e. what is the status
like it has for files". With Florence-2 moved to the end of a run, the photos
waiting to be described are work the page must show.
"""

from __future__ import annotations

import os

import pytest

from app.core.errors import make_error
from app.index import face_clustering as fc
from app.storage.sqlite_store import SqliteStore
from app.ui.widgets.status_funnel import picture_line

PICTURE_EXTS = (".jpg", ".heic")


@pytest.fixture
def store(tmp_path):
    with SqliteStore(tmp_path / "index.db") as opened:
        yield opened


def test_the_counts_follow_a_photo_library(store):
    ids = [store.upsert_file(path=f"/p/{n}.jpg", size_bytes=1, mtime_ns=1, ext="jpg",
                             source_kind="file") for n in range(5)]
    store.upsert_file(path="/d/report.pdf", size_bytes=1, mtime_ns=1, ext="pdf",
                      source_kind="file")
    store.mark_indexed(ids[0])
    for file_id in ids[1:3]:                     # read, no text, waiting for tags
        store.mark_skipped(file_id, make_error("ERR_NO_TEXT_LAYER", "extract.ocr"))
    store.note_photo_untaggable(ids[2])          # ... one with nothing to say
    face = store.add_face(ids[0], (0, 0, 1, 1), fc.to_bytes([1.0, 0.0]))
    store.add_face(ids[0], (0, 0, 1, 1), fc.to_bytes([0.0, 1.0]))
    store.mark_face_scanned(ids[0])
    store.split_pile([face])

    assert store.picture_counts(PICTURE_EXTS) == {
        "pictures": 5, "read": 3, "faces_looked": 1, "faces": 2,
        "people": 1, "unsorted": 1, "to_describe": 1, "text_to_read": 0}


def test_the_line_reads_as_a_sentence():
    assert picture_line({}) == ""
    assert picture_line({"pictures": 15011, "read": 40}) == "Pictures: 40 of 15,011 read"
    line = picture_line({"pictures": 15011, "read": 40, "faces_looked": 40, "faces": 120,
                         "people": 8, "unsorted": 5, "to_describe": 300})
    assert line == ("Pictures: 40 of 15,011 read · faces looked for in 40 · "
                    "120 faces in 8 people, 5 still to sort · 300 waiting to be described")
    assert "1 face in 1 person" in picture_line(
        {"pictures": 1, "read": 1, "faces": 1, "people": 1})


def test_the_funnel_shows_both_lines(store):
    pytest.importorskip("PySide6.QtWidgets", exc_type=ImportError)
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from app.ui.widgets.status_funnel import StatusFunnel

    _app = QApplication.instance() or QApplication([])
    funnel = StatusFunnel()
    funnel._show_both({"files": {"Indexed": 3}, "pictures": {"pictures": 5, "read": 3}})
    assert funnel.text() == "Indexed 3\nPictures: 3 of 5 read"
    funnel.show_counts({"Indexed": 4})           # the summary's own read keeps the line
    assert funnel.text().endswith("Pictures: 3 of 5 read")
    funnel.deleteLater()
