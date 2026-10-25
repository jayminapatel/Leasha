r"""A person's cover photo, chosen by the person - 2026-10-25.

Layer: L5 (with the store and schema 44 beneath it).

The owner: "in name the picture there should be an option to change the cover
photo". Each tile on the People page showed the pile's lowest-numbered face. Now
"Change the cover photo…" on a tile, or "Use as cover photo" on a face in the
manage dialog, records the face on the pile (`piles.cover_face_id`); the tile
shows it while it is still one of that person's faces, and the automatic face
again once it is not.
"""

from __future__ import annotations

import os
import sqlite3

import pytest

from app.index import face_clustering as fc
from app.storage import migrations
from app.storage.sqlite_store import SqliteStore

BIG = (0.0, 0.0, 120.0, 120.0)


@pytest.fixture
def store(tmp_path):
    with SqliteStore(tmp_path / "index.db") as opened:
        yield opened


def _face(store, name, vector=(1.0, 0.0)):
    file_id = store.upsert_file(path=f"/photos/{name}", size_bytes=1, mtime_ns=1,
                                ext="jpg", source_kind="file")
    return store.add_face(file_id, BIG, fc.to_bytes(list(vector)))


def _person(store, count=3, name=None):
    pile = store.create_pile(name=name)
    faces = [_face(store, f"{name or 'p'}{pile}-{n}.jpg") for n in range(count)]
    for face in faces:
        store.assign_face(face, pile)
    return pile, faces


def _pile(store, pile_id):
    return next(p for p in store.piles_with_counts() if p.id == pile_id)


# -- the migration --------------------------------------------------------------------

def test_schema_44_adds_the_cover_column_to_an_older_index():
    assert migrations.MIGRATIONS[44].__name__ == "_v44_pile_cover"
    assert migrations.CURRENT_VERSION >= 44
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE piles (id INTEGER PRIMARY KEY, name TEXT, created_at INTEGER)")
    conn.execute("CREATE TABLE faces (id INTEGER PRIMARY KEY)")
    conn.execute("INSERT INTO piles VALUES (1, 'Ann', 0)")
    migrations._v44_pile_cover(conn)
    migrations._v44_pile_cover(conn)                       # again: additive and idempotent
    cols = {r[1] for r in conn.execute("PRAGMA table_info(piles)")}
    assert "cover_face_id" in cols
    assert conn.execute("SELECT name, cover_face_id FROM piles").fetchone() == ("Ann", None)


# -- the store ------------------------------------------------------------------------

def test_no_choice_is_the_automatic_face(store):
    pile, faces = _person(store)
    record = _pile(store, pile)
    assert record.cover_face_id is None
    assert record.samples[0].face_id == faces[0]


def test_the_chosen_face_round_trips_and_survives_a_restart(tmp_path):
    path = tmp_path / "index.db"
    with SqliteStore(path) as store:
        pile, faces = _person(store, count=5)
        assert store.set_pile_cover(pile, faces[3]) is True
        record = _pile(store, pile)
        assert record.cover_face_id == faces[3]
        assert record.samples[0].face_id == faces[3]
        assert len(record.samples) == 4                    # still four, the cover once
        assert [s.face_id for s in record.samples].count(faces[3]) == 1
    with SqliteStore(path) as again:                       # the window opened again
        assert _pile(again, pile).samples[0].face_id == faces[3]


def test_a_face_from_another_person_is_refused(store):
    ann, _ = _person(store, name="Ann")
    _bo, bo_faces = _person(store, name="Bo")
    assert store.set_pile_cover(ann, bo_faces[0]) is False
    assert _pile(store, ann).cover_face_id is None


def test_the_choice_survives_a_regrouping_that_files_new_faces(store):
    pile, faces = _person(store, count=3)
    store.set_pile_cover(pile, faces[2])
    # A later run groups new faces as the pipeline does: only the unclustered ones.
    newcomers = [_face(store, f"new{n}.jpg") for n in range(3)]
    centroids = {p: fc.centroid_of(e) for p, e in store.pile_centroids().items()}
    batch = [f for chunk in store.iter_unclustered_faces() for f in chunk]
    plan = fc.cluster_batch([(f.id, f.embedding) for f in batch], centroids)
    for face_id, pile_id, confidence in plan.assign:
        store.assign_face(face_id, pile_id, confidence=confidence)
    record = _pile(store, pile)
    assert record.face_count == 6 and set(newcomers) <= {f for f, _p, _c in plan.assign}
    assert record.samples[0].face_id == faces[2]


def test_the_automatic_face_returns_when_the_chosen_one_moves_away(store):
    ann, faces = _person(store, name="Ann")
    bo, _ = _person(store, name="Bo")
    store.set_pile_cover(ann, faces[1])
    store.assign_face(faces[1], bo)                         # "Move to" Bo
    record = _pile(store, ann)
    assert record.cover_face_id is None
    assert record.samples[0].face_id == faces[0]
    # ... and Bo's tile is not taken over by Ann's choice.
    assert _pile(store, bo).cover_face_id is None


def test_the_automatic_face_returns_when_the_chosen_one_leaves_every_group(store):
    pile, faces = _person(store)
    store.set_pile_cover(pile, faces[0])
    store.not_this_person(faces[0])
    record = _pile(store, pile)
    assert record.cover_face_id is None and record.samples[0].face_id == faces[1]


def test_a_deleted_face_clears_the_choice(store):
    pile, faces = _person(store)
    store.set_pile_cover(pile, faces[1])
    file_id = store.conn.execute("SELECT file_id FROM faces WHERE id = ?",
                                 (faces[1],)).fetchone()[0]
    store.clear_faces_for_file(file_id)
    stored = store.conn.execute("SELECT cover_face_id FROM piles WHERE id = ?",
                                (pile,)).fetchone()[0]
    assert stored is None
    assert _pile(store, pile).samples[0].face_id == faces[0]


def test_none_goes_back_to_the_automatic_face(store):
    pile, faces = _person(store)
    store.set_pile_cover(pile, faces[2])
    assert store.set_pile_cover(pile, None) is True
    assert _pile(store, pile).samples[0].face_id == faces[0]


# -- the page -------------------------------------------------------------------------

@pytest.fixture(scope="module")
def qapp():
    pytest.importorskip("PySide6.QtWidgets", exc_type=ImportError)
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    yield QApplication.instance() or QApplication([])


def _settle(qapp):
    from PySide6.QtCore import QThreadPool

    for _ in range(5):
        QThreadPool.globalInstance().waitForDone(5_000)
        qapp.processEvents()


def test_the_tile_shows_the_chosen_face(qapp, store):
    from app.ui.widgets.face_crops import face_key
    from app.ui.widgets.photo_tagger_page import ROLE_FACE_KEY, PhotoTaggerPage

    pile, faces = _person(store, count=3)
    store.set_pile_cover(pile, faces[2])
    page = PhotoTaggerPage(store)
    _settle(qapp)
    item = page._item_for(pile)
    chosen = _pile(store, pile).samples[0]
    assert chosen.face_id == faces[2]
    assert item.data(ROLE_FACE_KEY) == face_key(chosen.path, chosen.bbox)
    page.deleteLater()


def test_the_tile_menu_has_change_the_cover_photo_with_an_icon(qapp, store, monkeypatch):
    from app.ui.widgets import menus, photo_tagger_page
    from app.ui.widgets.icons import ICON_NAMES
    from app.ui.widgets.photo_tagger_page import PhotoTaggerPage

    pile, _faces = _person(store, name="Ann")
    page = PhotoTaggerPage(store)
    _settle(qapp)
    shown = []
    monkeypatch.setattr(photo_tagger_page.menus, "show", lambda menu, *a, **k: shown.append(menu))
    chosen = []
    monkeypatch.setattr(PhotoTaggerPage, "_choose_cover", lambda self, p: chosen.append(p))
    item = page._item_for(pile)
    page._on_context_menu(page._list.visualItemRect(item).center())
    line = menus.lines(shown[0])["Change the cover photo…"]
    assert menus.glyph_for("Change the cover photo…") in ICON_NAMES
    assert not line.icon().isNull()
    line.trigger()
    assert chosen == [pile]
    page.deleteLater()


def test_use_as_cover_photo_on_a_face_sets_it_off_the_window_thread(qapp, store, monkeypatch):
    from app.ui.widgets import menus, photo_tagger_page
    from app.ui.widgets.icons import ICON_NAMES
    from app.ui.widgets.photo_tagger_page import _ManageFacesDialog

    pile, faces = _person(store, name="Ann")
    dialog = _ManageFacesDialog(store, pile, None, pile_name="Ann", choosing_cover=True)
    _settle(qapp)
    shown = []
    monkeypatch.setattr(photo_tagger_page.menus, "show", lambda menu, *a, **k: shown.append(menu))
    item = dialog._list.item(2)
    dialog._on_face_menu(dialog._list.visualItemRect(item).center())
    line = menus.lines(shown[0])["Use as cover photo"]
    assert menus.glyph_for("Use as cover photo") in ICON_NAMES and not line.icon().isNull()

    threads = []
    real = store.set_pile_cover

    def recording(*args):
        import threading

        threads.append(threading.current_thread() is threading.main_thread())
        return real(*args)

    monkeypatch.setattr(store, "set_pile_cover", recording)
    line.trigger()
    _settle(qapp)
    assert threads == [False]                               # written on a worker
    assert _pile(store, pile).samples[0].face_id == faces[2]
    assert dialog.result() == dialog.DialogCode.Accepted    # chosen: the window closes
    dialog.deleteLater()


def test_the_faces_seen_once_have_no_cover_line(qapp, store):
    from PySide6.QtCore import Qt

    from app.ui.widgets.photo_tagger_page import _ManageFacesDialog

    dialog = _ManageFacesDialog(store, None, None, choosing_cover=True)
    assert dialog._list.contextMenuPolicy() != Qt.ContextMenuPolicy.CustomContextMenu
    assert dialog._choosing_cover is False
    dialog.deleteLater()
