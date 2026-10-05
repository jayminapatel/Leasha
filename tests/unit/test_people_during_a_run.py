r"""People to name appear while photos are read, and HEIC photos can be read.

Layer: L2/L5

Found 2026-10-04 on the owner's library (15,011 pictures, a run stopped
part-way): 40 faces found and 0 piles, so the naming page was empty - grouping
ran only at the *start* of a run. And all 3,741 `.heic` photos failed every
picture model in the index process, because nothing there registered
`pillow-heif`. The owner: "the pictures for naming should be updated
periodically if not live".
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.index import face_clustering as fc
from app.storage.sqlite_store import SqliteStore


# --- HEIC ----------------------------------------------------------------------

def test_a_heic_photo_opens_once_the_indexer_has_registered_it(tmp_path):
    pillow_heif = pytest.importorskip("pillow_heif")
    from PIL import Image

    from app.extract.heif import register_heif

    path = tmp_path / "photo.heic"
    pillow_heif.from_pillow(Image.new("RGB", (32, 24), (200, 30, 30))).save(str(path))
    assert register_heif() is True
    with Image.open(path) as opened:
        assert opened.size == (32, 24)


def test_face_detection_reads_a_heic_photo_opencv_cannot(tmp_path):
    """OpenCV cannot decode HEIC; the faces lane reads it through Pillow, in
    the blue-green-red order the model expects."""
    pillow_heif = pytest.importorskip("pillow_heif")
    cv2 = pytest.importorskip("cv2")
    import numpy as np
    from PIL import Image

    from app.extract.face_detect import _read_bgr

    path = tmp_path / "photo.heic"
    pillow_heif.from_pillow(Image.new("RGB", (16, 12), (220, 10, 10))).save(str(path))
    assert cv2.imdecode(np.fromfile(str(path), dtype=np.uint8), cv2.IMREAD_COLOR) is None
    image = _read_bgr(path, cv2, np)
    assert image is not None and image.shape == (12, 16, 3)
    blue, green, red = (int(v) for v in image[6, 8])
    assert red > 150 and blue < 80, "channels reversed to BGR"


def test_every_picture_model_registers_heic_where_it_loads():
    """Read from the files, not run: the loaders need model downloads a unit
    test has not got, and other tests replace `_load` for their own session."""
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parents[2] / "app"
    for relative, name in (("extract/florence_tagger.py", "_load"),
                           ("extract/face_detect.py", "_load"),
                           ("extract/ocr.py", "_load_engine"),
                           ("index/clip_embedder.py", "__init__")):
        text = (root / relative).read_text(encoding="utf-8")
        found = [node for node in ast.walk(ast.parse(text))
                 if isinstance(node, ast.FunctionDef) and node.name == name]
        assert found, (relative, name)
        assert "register_heif()" in ast.get_source_segment(text, found[0]), relative


# --- grouping during a run ----------------------------------------------------

@pytest.fixture
def store(tmp_path):
    with SqliteStore(tmp_path / "index.db") as opened:
        yield opened


def _pipeline(store, monkeypatch, every):
    from app.index import pipeline as module

    monkeypatch.setattr(module, "FACE_CLUSTER_EVERY", every)
    built = module.Pipeline.__new__(module.Pipeline)
    built.store = store
    built.config = SimpleNamespace(people_recognition_enabled=True)
    built._log = module._log if hasattr(module, "_log") else __import__(
        "app.core.logging", fromlist=["logger"]).logger
    built._face_stats = SimpleNamespace(enrichment_counts={})
    built._faces_since_cluster = 0
    calls = []
    built._drain_face_cluster = lambda stats: calls.append(stats)
    return built, calls


def test_faces_are_grouped_every_few_found_not_only_at_the_next_run(store, monkeypatch):
    from app.extract import face_detect

    built, calls = _pipeline(store, monkeypatch, every=3)
    face = face_detect.FaceDetection(bbox=(0, 0, 10, 10),
                                     embedding=fc.to_bytes([1.0, 0.0]), confidence=0.9)
    monkeypatch.setattr(face_detect, "detect_faces", lambda path: [face, face])

    for number in range(4):                      # 2, 4 (group), 6, 8 (group)
        file_id = store.upsert_file(path=f"/photos/p{number}.jpg", size_bytes=1,
                                    mtime_ns=1, source_kind="file")
        built._maybe_detect_faces(SimpleNamespace(path=Path(f"/photos/p{number}.jpg")), file_id)

    assert len(calls) == 2


def test_the_stamp_changes_when_faces_are_grouped(store):
    file_id = store.upsert_file(path="/photos/a.jpg", size_bytes=1, mtime_ns=1,
                                source_kind="file")
    first = store.add_face(file_id, (0, 0, 1, 1), fc.to_bytes([1.0, 0.0]))
    before = store.faces_stamp()
    store.split_pile([first])
    assert store.faces_stamp() != before
    assert store.faces_stamp()[:3] == (1, 1, 1)


# --- the naming page follows -------------------------------------------------

@pytest.fixture(scope="module")
def qapp():
    pytest.importorskip("PySide6.QtWidgets", exc_type=ImportError)
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    yield QApplication.instance() or QApplication([])


def test_the_page_reloads_when_the_stamp_moves_and_not_otherwise(qapp, store, monkeypatch):
    from app.ui.widgets.photo_tagger_page import PhotoTaggerPage

    page = PhotoTaggerPage(store)
    reloads = []
    monkeypatch.setattr(page, "reload", lambda: reloads.append(1))
    page._stamp_ready((0, 0, 0, 0))              # the first stamp only records
    page._stamp_ready((0, 0, 0, 0))              # nothing changed
    assert reloads == []
    page._stamp_ready((5, 1, 5, 0))              # a run grouped five faces
    assert reloads == [1]
    page.deleteLater()


# --- two groups for one person (2026-10-05) -------------------------------------

def test_naming_a_second_group_after_a_named_person_combines_them(store):
    """The owner: "there are two sets both are jason they need to be merged" -
    the second name raised `UNIQUE constraint failed: piles.name`."""
    photo = store.upsert_file(path="/photos/j.jpg", size_bytes=1, mtime_ns=1,
                              source_kind="file")
    first = store.add_face(photo, (0, 0, 1, 1), fc.to_bytes([1.0, 0.0]))
    second = store.add_face(photo, (0, 0, 1, 1), fc.to_bytes([0.9, 0.1]))
    jason = store.split_pile([first])
    other = store.split_pile([second])
    store.rename_pile(jason, "Jason")

    assert store.pile_id_named("jason", exclude=other) == jason, "any case"
    store.rename_pile(other, "jason")                     # no IntegrityError

    piles = store.conn.execute("SELECT id, name FROM piles").fetchall()
    assert [(r[0], r[1]) for r in piles] == [(jason, "Jason")]
    owners = {r[0] for r in store.conn.execute("SELECT pile_id FROM faces")}
    assert owners == {jason}


def test_the_page_asks_before_combining_and_no_leaves_both(qapp, store, monkeypatch):
    from PySide6.QtCore import QThreadPool
    from PySide6.QtWidgets import QMessageBox

    from app.ui.widgets.photo_tagger_page import PhotoTaggerPage

    photo = store.upsert_file(path="/photos/k.jpg", size_bytes=1, mtime_ns=1,
                              source_kind="file")
    jason = store.split_pile([store.add_face(photo, (0, 0, 1, 1), fc.to_bytes([1.0, 0.0]))])
    other = store.split_pile([store.add_face(photo, (0, 0, 1, 1), fc.to_bytes([0.0, 1.0]))])
    store.rename_pile(jason, "Jason")
    page = PhotoTaggerPage(store)
    monkeypatch.setattr(page, "reload", lambda: None)

    monkeypatch.setattr(QMessageBox, "question",
                        lambda *a, **k: QMessageBox.StandardButton.No)
    page._name_checked(other, "Jason", jason)
    QThreadPool.globalInstance().waitForDone(5000)
    assert store.conn.execute("SELECT count(*) FROM piles").fetchone()[0] == 2

    monkeypatch.setattr(QMessageBox, "question",
                        lambda *a, **k: QMessageBox.StandardButton.Yes)
    page._name_checked(other, "Jason", jason)
    QThreadPool.globalInstance().waitForDone(5000)
    assert store.conn.execute("SELECT count(*) FROM piles").fetchone()[0] == 1
    page.deleteLater()


# --- what a face was told it is not (2026-10-05) --------------------------------

def test_no_to_a_suggestion_holds_for_the_next_grouping(store):
    """The chip's No promises "Leasha will not guess this one on its own again"
    - and it did not: the next grouping could suggest the same face again."""
    from app.index.face_clustering import cluster_batch

    photo = store.upsert_file(path="/photos/n.jpg", size_bytes=1, mtime_ns=1,
                              source_kind="file")
    jason = store.split_pile([store.add_face(photo, (0, 0, 1, 1), fc.to_bytes([1.0, 0.0]))])
    face = store.add_face(photo, (0, 0, 1, 1), fc.to_bytes([1.0, 0.05]))
    store.suggest_face(face, jason)
    store.confirm_suggestion(face, False)

    assert store.declined_piles([face]) == {face: {jason}}
    plan = cluster_batch([(face, fc.to_bytes([1.0, 0.05]))],
                         {jason: fc.to_bytes([1.0, 0.0])}, store.declined_piles([face]))
    assert not plan.assign and not plan.suggest, "never Jason again"


def test_not_this_person_takes_a_face_out_for_good(store):
    photo = store.upsert_file(path="/photos/m.jpg", size_bytes=1, mtime_ns=1,
                              source_kind="file")
    face = store.add_face(photo, (0, 0, 1, 1), fc.to_bytes([1.0, 0.0]))
    jason = store.split_pile([face, store.add_face(photo, (0, 0, 1, 1), fc.to_bytes([1.0, 0.0]))])
    assert len(store.faces_in_pile(jason)) == 2
    store.not_this_person(face)
    assert len(store.faces_in_pile(jason)) == 1
    assert store.declined_piles([face]) == {face: {jason}}


# --- accept all (2026-10-05) ------------------------------------------------------

def _suggested(store, name, how_many):
    photo = store.upsert_file(path=f"/photos/{name}.jpg", size_bytes=1, mtime_ns=1,
                              source_kind="file")
    pile = store.split_pile([store.add_face(photo, (0, 0, 1, 1), fc.to_bytes([1.0, 0.0]))])
    store.rename_pile(pile, name)
    faces = [store.add_face(photo, (0, 0, 1, 1), fc.to_bytes([1.0, 0.0]))
             for _ in range(how_many)]
    for face in faces:
        store.suggest_face(face, pile)
    return pile, faces


def test_accept_all_files_every_suggestion_for_one_person_or_everyone(store):
    """The owner: "need to mass accept names as most cases the system was right"."""
    jason, _ = _suggested(store, "Jason", 3)
    sarita, _ = _suggested(store, "Sarita", 2)
    assert store.suggestion_counts() == [(jason, "Jason", 3), (sarita, "Sarita", 2)]

    assert store.accept_all_suggestions(sarita) == 2
    assert store.suggestion_counts() == [(jason, "Jason", 3)]
    assert len(store.faces_in_pile(sarita)) == 3

    assert store.accept_all_suggestions() == 3
    assert store.suggestion_counts() == [] and store.pending_suggestions() == []
    assert len(store.faces_in_pile(jason)) == 4
    people = store.conn.execute(
        "SELECT text FROM chunks WHERE text LIKE '%Jason%'").fetchall()
    assert people, "the photo's People line names them, as a single Yes does"


def test_the_page_asks_with_the_numbers_before_accepting_all(qapp, store, monkeypatch):
    from PySide6.QtCore import QThreadPool
    from PySide6.QtWidgets import QMessageBox

    from app.ui.widgets.photo_tagger_page import PhotoTaggerPage

    jason, _ = _suggested(store, "Jason", 2)
    page = PhotoTaggerPage(store)
    monkeypatch.setattr(page, "reload", lambda: None)
    page._counts_ready(store.suggestion_counts(), page._generation)
    assert page._accept_bar.isVisibleTo(page)
    assert [a.text() for a in page._accept_menu.actions() if a.text()] == [
        "Everyone (2)", "Jason (2)"]

    asked = []
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: asked.append(a[2])
                        or QMessageBox.StandardButton.No)
    assert page.accept_all(None) is False
    assert "File 2 face(s)" in asked[0] and "Jason: 2" in asked[0]
    assert store.suggestion_counts(), "No leaves them waiting"

    monkeypatch.setattr(QMessageBox, "question",
                        lambda *a, **k: QMessageBox.StandardButton.Yes)
    assert page.accept_all(jason) is True
    QThreadPool.globalInstance().waitForDone(5000)
    assert store.suggestion_counts() == []
    page.deleteLater()
