r"""The People window, 2026-10-14: faces seen once, tiny faces left out, and a
choice of order.

Layer: L5 (with the store and the face finder beneath it).

The owner's index had 377 faces and 228 of them in no group - each seen only
once, most of them a background face about 21 pixels across - and the People
window had no way to name any of them. These hold the three changes: the face
finder keeps no face under `SMALLEST_FACE_PX`, the faces in no group get a tile
of their own, and the grid can be put in four orders, remembered.
"""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from app.core.face_size import SMALLEST_FACE_PX, big_enough
from app.index import face_clustering as fc
from app.storage.sqlite_store import SqliteStore

BIG = (0.0, 0.0, 120.0, 120.0)
TINY = (0.0, 0.0, 20.0, 24.0)


@pytest.fixture
def store(tmp_path):
    with SqliteStore(tmp_path / "index.db") as opened:
        yield opened


def _photo(store, name):
    return store.upsert_file(path=f"/photos/{name}", size_bytes=1, mtime_ns=1,
                             ext="jpg", source_kind="file")


def _face(store, name, bbox=BIG, vector=(1.0, 0.0)):
    return store.add_face(_photo(store, name), bbox, fc.to_bytes(list(vector)))


# -- the size rule ------------------------------------------------------------------

def test_a_face_is_big_enough_by_its_shorter_side():
    assert big_enough(SMALLEST_FACE_PX, 500)
    assert not big_enough(SMALLEST_FACE_PX - 1, 500)
    assert not big_enough(500, 21)


def test_the_face_finder_drops_a_face_too_small_to_match(monkeypatch, tmp_path):
    """A background face in a group photo is not kept; the face beside it is."""
    import numpy as np

    from app.extract import face_detect

    def found(x1, y1, x2, y2):
        return SimpleNamespace(bbox=np.array([x1, y1, x2, y2], dtype="float32"),
                               normed_embedding=np.ones(4, dtype="float32"), det_score=0.9)

    engine = SimpleNamespace(get=lambda _image: [found(10, 10, 130, 150), found(5, 5, 25, 29)])
    monkeypatch.setattr(face_detect, "_load", lambda: engine)
    monkeypatch.setattr(face_detect, "_read_bgr", lambda *_a: np.zeros((200, 200, 3)))

    faces = face_detect.detect_faces(tmp_path / "group.jpg")

    assert [f.bbox[2:] for f in faces] == [(120.0, 140.0)]


# -- the store ----------------------------------------------------------------------

def test_faces_seen_once_are_the_faces_in_no_group_big_enough_to_name(store):
    alone = _face(store, "alone.jpg")
    tiny = _face(store, "crowd.jpg", bbox=TINY)
    grouped = _face(store, "grouped.jpg")
    store.split_pile([grouped])
    asked = _face(store, "asked.jpg")
    store.suggest_face(asked, store.create_pile(name="Daddy"))
    near = _face(store, "near.jpg")
    store.suggest_face(near, store.create_pile())         # a group nobody has named

    seen = [face_id for face_id, _path, _bbox in store.faces_seen_once()]

    assert alone in seen and near in seen
    assert tiny not in seen, "too small to name"
    assert grouped not in seen, "in a group already"
    assert asked not in seen, "has its own 'Is this Daddy?' question"


def test_faces_seen_once_newest_first(store):
    older, newer = _face(store, "older.jpg"), _face(store, "newer.jpg")
    store.conn.execute("UPDATE faces SET created_at = 100 WHERE id = ?", (older,))
    store.conn.execute("UPDATE faces SET created_at = 200 WHERE id = ?", (newer,))
    store.conn.commit()

    assert [f[0] for f in store.faces_seen_once()] == [newer, older]


def test_naming_faces_makes_a_new_person_or_joins_the_one_with_that_name(store):
    first, second, third = (_face(store, f"{n}.jpg") for n in "abc")

    leah = store.name_faces([first, second], "Leah")
    joined = store.name_faces([third], "  leah ")

    assert joined == leah
    third_file = store.get_file("/photos/c.jpg").id
    assert "People: Leah" in [c.text for c in store.chunks_for_file(third_file)]
    piles = {p.id: p for p in store.piles_with_counts()}
    assert piles[leah].name == "Leah" and piles[leah].face_count == 3
    assert store.faces_seen_once() == []
    assert store.name_faces([], "Leah") is None
    assert store.name_faces([first], "   ") is None


def test_a_pile_says_when_its_newest_face_was_found(store):
    one, two = _face(store, "one.jpg"), _face(store, "two.jpg")
    store.conn.execute("UPDATE faces SET created_at = 50 WHERE id = ?", (one,))
    store.conn.execute("UPDATE faces SET created_at = 90 WHERE id = ?", (two,))
    store.conn.commit()
    store.split_pile([one, two])

    assert store.piles_with_counts()[0].last_found == 90


def test_the_indexing_line_counts_people_with_faces_and_leaves_tiny_faces_out(store):
    """`people` was every group row, emptied ones too (53 for 26 on the owner's
    index); `unsorted` now matches the "Faces seen once" tile."""
    exts = ["jpg"]
    kept = _face(store, "kept.jpg")
    store.split_pile([kept])
    emptied = _face(store, "moved.jpg")
    store.split_pile([emptied])
    store.assign_face(emptied, store.piles_with_counts()[0].id)   # its group is now empty
    _face(store, "alone.jpg")
    _face(store, "crowd.jpg", bbox=TINY)

    counts = store.picture_counts(exts)

    assert counts["people"] == 1
    assert counts["unsorted"] == 1


# -- the order ----------------------------------------------------------------------

def _pile(pid, name, faces, last=0):
    return SimpleNamespace(id=pid, name=name, face_count=faces, last_found=last, samples=())


PILES = [_pile(1, None, 6, last=30), _pile(2, "Maya", 7, last=10),
         _pile(3, "Jaymin", 62, last=20), _pile(4, None, 2, last=40),
         _pile(5, None, 6, last=5)]


def _order(how):
    from app.ui.widgets.photo_tagger_page import sort_piles

    return [p.id for p in sort_piles(PILES, how)]


def test_most_faces_is_biggest_first_and_settles_a_tie_by_age():
    assert _order("faces") == [3, 2, 1, 5, 4]


def test_name_puts_named_people_a_to_z_then_the_rest_biggest_first():
    assert _order("name") == [3, 2, 1, 5, 4]
    assert _order("name")[:2] == [3, 2]          # Jaymin before Maya


def test_unnamed_first_is_the_naming_still_to_do():
    assert _order("unnamed") == [1, 5, 4, 3, 2]


def test_recently_found_follows_the_newest_face():
    assert _order("recent") == [4, 1, 3, 2, 5]


def test_an_order_this_build_does_not_know_is_most_faces():
    assert _order("whatever") == _order("faces")


def test_person_numbers_do_not_change_with_the_order():
    from app.ui.widgets.photo_tagger_page import size_ranks

    assert size_ranks(PILES) == {3: 1, 2: 2, 1: 3, 5: 4, 4: 5}
    assert size_ranks(list(reversed(PILES))) == size_ranks(PILES)


# -- the page -----------------------------------------------------------------------

pytest.importorskip("PySide6.QtWidgets", exc_type=ImportError)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    yield QApplication.instance() or QApplication([])


def _settle(qapp):
    from PySide6.QtCore import QThreadPool

    from app.ui import state_writes

    for _ in range(5):
        QThreadPool.globalInstance().waitForDone(5_000)
        state_writes.pool().waitForDone(5_000)
        qapp.processEvents()


def _tiles(page):
    from app.ui.widgets.photo_tagger_page import ROLE_PILE_ID

    return [(page._list.item(r).data(ROLE_PILE_ID), page._list.item(r).text())
            for r in range(page._list.count())]


def _two_people_and_one_stranger(store):
    big = [_face(store, f"big{n}.jpg") for n in range(3)]
    store.split_pile(big, new_name="Zoe")
    small = [_face(store, f"small{n}.jpg") for n in range(2)]
    store.split_pile(small, new_name="Adam")
    return _face(store, "stranger.jpg")


@pytest.mark.qt
def test_the_faces_seen_once_tile_comes_last_with_its_count(qapp, store):
    from app.ui.widgets.photo_tagger_page import SEEN_ONCE, PhotoTaggerPage

    _two_people_and_one_stranger(store)
    _face(store, "crowd.jpg", bbox=TINY)

    page = PhotoTaggerPage(store)
    _settle(qapp)

    tiles = _tiles(page)
    assert [t[0] for t in tiles][-1] == SEEN_ONCE
    assert tiles[-1][1] == "Faces seen once — 1 face(s)"
    assert tiles[0][1] == "Zoe — 3 face(s)"


@pytest.mark.qt
def test_only_faces_seen_once_still_shows_the_grid(qapp, store):
    from app.ui.widgets.photo_tagger_page import PhotoTaggerPage

    _face(store, "stranger.jpg")

    page = PhotoTaggerPage(store)
    _settle(qapp)

    assert page._list.isHidden() is False
    assert page._empty_note.isHidden() is True


@pytest.mark.qt
def test_changing_the_order_reorders_now_and_is_remembered(qapp, store):
    from app.ui.widgets.photo_tagger_page import SEEN_ONCE, SORT_STATE_KEY, PhotoTaggerPage

    _two_people_and_one_stranger(store)
    page = PhotoTaggerPage(store)
    _settle(qapp)

    page._sort_actions["name"].trigger()             # the menu line, as a click is
    _settle(qapp)

    assert page._sort_button.text() == "Sort: Name (A to Z)"
    assert page._sort_actions["name"].isChecked()

    assert [t[1] for t in _tiles(page)][:2] == ["Adam — 2 face(s)", "Zoe — 3 face(s)"]
    assert _tiles(page)[-1][0] == SEEN_ONCE, "the stranger's tile stays last"
    assert store.get_state(SORT_STATE_KEY) == "name"

    again = PhotoTaggerPage(store)
    _settle(qapp)
    assert again._sort_button.text() == "Sort: Name (A to Z)"
    assert _tiles(again)[0][1] == "Adam — 2 face(s)"


@pytest.mark.qt
def test_the_seen_once_tile_cannot_be_dragged_renamed_or_forgotten(qapp, store, monkeypatch):
    from app.ui.widgets import photo_tagger_page as module

    _face(store, "stranger.jpg")
    page = module.PhotoTaggerPage(store)
    _settle(qapp)
    opened, renamed = [], []
    monkeypatch.setattr(page, "_open_seen_once", lambda: opened.append(True))
    monkeypatch.setattr(page, "_rename", lambda *a: renamed.append(a))
    monkeypatch.setattr(page, "_forget", lambda *a: renamed.append(a))

    page._list.setCurrentRow(page._list.count() - 1)
    page._rename_current()
    page._forget_current()
    page._list.startDrag(None)                   # returns before any drag starts

    assert opened == [True] and renamed == []


@pytest.mark.qt
def test_the_seen_once_dialog_names_the_selected_faces(qapp, store, monkeypatch):
    from PySide6.QtWidgets import QInputDialog

    from app.ui.widgets import photo_tagger_page as module

    first, second = _face(store, "a.jpg"), _face(store, "b.jpg")
    dialog = module._ManageFacesDialog(store, None)
    _settle(qapp)

    assert dialog.windowTitle() == "Faces seen once"
    assert dialog._not_this.isHidden() and not dialog._name_them.isHidden()
    assert dialog._list.count() == 2

    dialog._list.selectAll()
    monkeypatch.setattr(QInputDialog, "getText", staticmethod(lambda *a, **k: ("Leah", True)))
    dialog._name_selected()
    _settle(qapp)

    leah = store.pile_id_named("Leah")
    assert {f.pile_id for f in store.faces_for_file(store.get_file("/photos/a.jpg").id)} == {leah}
    assert {face_id for face_id, *_ in store.faces_in_pile(leah)} == {first, second}
    assert dialog._list.count() == 0
    dialog.deleteLater()
