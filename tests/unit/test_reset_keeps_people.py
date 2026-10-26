r"""A reset keeps each named person's faces, so the faces found again rejoin them - 2026-10-26.

Layer: L1 (the store and schema 45), with the face grouping's own decision above it.

The owner: "index reset should not remove face name". A reset deleted every face with its
file, and the named piles were left with none: the faces found again were grouped into new
unnamed piles, and all eleven of the owner's named people were empty.
"""

from __future__ import annotations

import sqlite3

import pytest

from app.index import face_clustering as fc
from app.storage import migrations
from app.storage.sqlite_store import SqliteStore

BIG = (0.0, 0.0, 120.0, 120.0)
ANN, BOB = (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)


@pytest.fixture
def store(tmp_path):
    with SqliteStore(tmp_path / "index.db") as opened:
        yield opened


def _face(store, name, vector):
    file_id = store.upsert_file(path=f"/photos/{name}", size_bytes=1, mtime_ns=1,
                                ext="jpg", source_kind="file")
    return store.add_face(file_id, BIG, fc.to_bytes(list(vector)))


def _person(store, name, vector, count=3):
    pile = store.create_pile(name=name)
    for n in range(count):
        store.assign_face(_face(store, f"{name or 'x'}-{n}.jpg", vector), pile)
    return pile


def _regroup(store):
    """What the index run does with faces found again: `_drain_face_cluster`'s decision."""
    centroids = {pid: fc.centroid_of(e) for pid, e in store.pile_centroids().items()}
    faces = [(f.id, f.embedding) for batch in store.iter_unclustered_faces(batch_size=32)
             for f in batch]
    plan = fc.cluster_batch(faces, centroids)
    for face_id, pile_id, confidence in plan.assign:
        store.assign_face(face_id, pile_id, confidence=confidence)
    return plan


def _faces_of(store, pile_id):
    return store.conn.execute("SELECT COUNT(*) FROM faces WHERE pile_id = ?",
                              (pile_id,)).fetchone()[0]


def test_schema_45_adds_the_kept_faces_table_to_an_older_index():
    assert migrations.MIGRATIONS[45].__name__ == "_v45_pile_anchors"
    assert migrations.CURRENT_VERSION >= 45
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE piles (id INTEGER PRIMARY KEY, name TEXT)")
    migrations._v45_pile_anchors(conn)
    migrations._v45_pile_anchors(conn)                     # again: additive and idempotent
    assert {r[1] for r in conn.execute("PRAGMA table_info(pile_anchors)")} == {
        "id", "pile_id", "embedding"}


def test_after_a_reset_the_faces_found_again_rejoin_their_named_person(store):
    ann, bob = _person(store, "Ann Lee", ANN), _person(store, "Bob Bell", BOB)
    store.clear_index()
    assert _faces_of(store, ann) == _faces_of(store, bob) == 0, "a reset still empties the index"
    names = {r[0] for r in store.conn.execute("SELECT name FROM piles WHERE name IS NOT NULL")}
    assert names == {"Ann Lee", "Bob Bell"}, "the names are kept"

    for n in range(2):                                     # the next run finds them again
        _face(store, f"again-j{n}.jpg", ANN)
        _face(store, f"again-m{n}.jpg", BOB)
    _regroup(store)
    assert _faces_of(store, ann) == 2 and _faces_of(store, bob) == 2


def test_a_second_reset_keeps_them_too(store):
    ann = _person(store, "Ann Lee", ANN)
    store.clear_index()
    store.clear_index()                                    # no faces now: what was kept stays
    _face(store, "again.jpg", ANN)
    _regroup(store)
    assert _faces_of(store, ann) == 1


def test_an_unnamed_pile_keeps_nothing(store):
    _person(store, None, ANN)
    store.clear_index()
    assert store.conn.execute("SELECT COUNT(*) FROM pile_anchors").fetchone()[0] == 0


def test_a_person_with_many_faces_keeps_a_sample(store):
    _person(store, "Cy Cox", ANN, count=300)
    store.clear_index()
    kept = store.conn.execute("SELECT COUNT(*) FROM pile_anchors").fetchone()[0]
    assert 0 < kept <= SqliteStore.ANCHORS_PER_PERSON


def test_combining_moves_the_kept_faces_and_forgetting_drops_them(store):
    old = _person(store, "Ann Lee", ANN)
    store.clear_index()
    new = _person(store, None, ANN, count=1)
    store.combine_piles(old, new)
    assert [r[0] for r in store.conn.execute("SELECT DISTINCT pile_id FROM pile_anchors")] == [new]
    store.forget_person(new)
    assert store.conn.execute("SELECT COUNT(*) FROM pile_anchors").fetchone()[0] == 0
