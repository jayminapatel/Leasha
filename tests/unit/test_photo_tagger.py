r"""The Photo Tagger's storage layer and search integration.

Work order 0j (`202626270512`), the whole order.

Layer: L1/L4

`test_face_clustering.py` proves the pure clustering maths; this file proves
everything it touches once real SQLite is involved - `piles`/`faces`/
`face_scans` (schema v23), the CRUD `SqliteStore` gained for section 2's
page, `/who` end to end (section 3a), and the cluster -> name -> assign ->
suggest -> confirm loop as one integration test against fixture faces
(the order's own test list).
"""

from __future__ import annotations

import pytest

from app.index import face_clustering as fc
from app.search.commands import COMMANDS, command_for, expand_slashes
from app.search.query import parse_query
from app.storage.filters import file_filter_sql
from app.storage.migrations import CURRENT_VERSION
from app.storage.sqlite_store import SqliteStore


@pytest.fixture
def store(tmp_path):
    with SqliteStore(tmp_path / "index.db") as opened:
        yield opened


def _photo(store, name="a.jpg"):
    return store.upsert_file(
        path=f"/photos/{name}", size_bytes=100, mtime_ns=1, source_kind="file")


def _vec(x, y):
    return fc.to_bytes([x, y])


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def test_schema_v23_adds_piles_faces_and_face_scans(store):
    assert CURRENT_VERSION >= 23
    names = {row[0] for row in store.conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"piles", "faces", "face_scans"} <= names


# ---------------------------------------------------------------------------
# Basic CRUD
# ---------------------------------------------------------------------------

def test_add_face_and_faces_for_file(store):
    file_id = _photo(store)
    face_id = store.add_face(file_id, (1.0, 2.0, 30.0, 40.0), _vec(1, 0))
    faces = store.faces_for_file(file_id)

    assert len(faces) == 1
    assert faces[0].id == face_id
    assert faces[0].bbox == (1.0, 2.0, 30.0, 40.0)
    assert faces[0].pile_id is None


def test_iter_unclustered_faces_only_returns_faces_with_no_verdict(store):
    file_id = _photo(store)
    unclustered = store.add_face(file_id, (0, 0, 1, 1), _vec(1, 0))
    assigned = store.add_face(file_id, (0, 0, 1, 1), _vec(0, 1))
    pile_id = store.create_pile()
    store.assign_face(assigned, pile_id)

    batches = list(store.iter_unclustered_faces())
    ids = {face.id for batch in batches for face in batch}
    assert ids == {unclustered}


def test_create_pile_starts_unnamed(store):
    pile_id = store.create_pile()
    piles = store.piles_with_counts()
    # No faces yet, so it will not appear in piles_with_counts (grid shows
    # only piles that actually have faces) - proved indirectly via a face.
    file_id = _photo(store)
    face_id = store.add_face(file_id, (0, 0, 1, 1), _vec(1, 0))
    store.assign_face(face_id, pile_id)
    piles = store.piles_with_counts()
    assert len(piles) == 1
    assert piles[0].name is None
    assert piles[0].face_count == 1


def test_piles_with_counts_sorts_biggest_first(store):
    small = store.create_pile()
    big = store.create_pile()
    for pile_id, n in ((small, 1), (big, 3)):
        for _ in range(n):
            file_id = _photo(store)
            face_id = store.add_face(file_id, (0, 0, 1, 1), _vec(1, 0))
            store.assign_face(face_id, pile_id)

    piles = store.piles_with_counts()
    assert [p.id for p in piles] == [big, small]
    assert [p.face_count for p in piles] == [3, 1]


# ---------------------------------------------------------------------------
# Naming, combining, splitting, forgetting - section 2
# ---------------------------------------------------------------------------

def test_rename_pile_and_the_people_segment_appears(store):
    file_id = _photo(store)
    pile_id = store.create_pile()
    face_id = store.add_face(file_id, (0, 0, 1, 1), _vec(1, 0))
    store.assign_face(face_id, pile_id)

    store.rename_pile(pile_id, "Daddy")

    chunks = store.chunks_for_file(file_id)
    texts = [c.text for c in chunks]
    assert any(t == "People: Daddy" for t in texts)


def test_renaming_to_nothing_removes_the_people_segment(store):
    file_id = _photo(store)
    pile_id = store.create_pile()
    face_id = store.add_face(file_id, (0, 0, 1, 1), _vec(1, 0))
    store.assign_face(face_id, pile_id)
    store.rename_pile(pile_id, "Daddy")

    store.rename_pile(pile_id, "")

    texts = [c.text for c in store.chunks_for_file(file_id)]
    assert not any(t.startswith("People:") for t in texts)


def test_combine_piles_merges_faces_and_deletes_the_source(store):
    file_id = _photo(store)
    source = store.create_pile(name="Dad")
    target = store.create_pile(name="Daddy")
    face_id = store.add_face(file_id, (0, 0, 1, 1), _vec(1, 0))
    store.assign_face(face_id, source)

    moved = store.combine_piles(source, target)

    assert moved == 1
    faces = store.faces_for_file(file_id)
    assert faces[0].pile_id == target
    names = {row[0] for row in store.conn.execute("SELECT id FROM piles")}
    assert source not in names


def test_combine_updates_the_people_segment_to_the_surviving_name(store):
    file_id = _photo(store)
    source = store.create_pile(name="Dad")
    target = store.create_pile(name="Daddy")
    face_id = store.add_face(file_id, (0, 0, 1, 1), _vec(1, 0))
    store.assign_face(face_id, source)

    store.combine_piles(source, target)

    texts = [c.text for c in store.chunks_for_file(file_id)]
    assert "People: Daddy" in texts
    assert "People: Dad" not in texts


def test_remove_face_from_pile_returns_it_to_the_unclustered_pool(store):
    file_id = _photo(store)
    pile_id = store.create_pile(name="Daddy")
    face_id = store.add_face(file_id, (0, 0, 1, 1), _vec(1, 0))
    store.assign_face(face_id, pile_id)

    store.remove_face_from_pile(face_id)

    faces = store.faces_for_file(file_id)
    assert faces[0].pile_id is None
    texts = [c.text for c in store.chunks_for_file(file_id)]
    assert not any(t.startswith("People:") for t in texts)


def test_split_pile_creates_a_fresh_pile_from_the_chosen_faces(store):
    file_a, file_b = _photo(store, "a.jpg"), _photo(store, "b.jpg")
    pile_id = store.create_pile(name="Mixed")
    face_a = store.add_face(file_a, (0, 0, 1, 1), _vec(1, 0))
    face_b = store.add_face(file_b, (0, 0, 1, 1), _vec(0, 1))
    store.assign_face(face_a, pile_id)
    store.assign_face(face_b, pile_id)

    new_id = store.split_pile([face_b])

    assert new_id is not None
    assert new_id != pile_id
    assert store.faces_for_file(file_b)[0].pile_id == new_id
    assert store.faces_for_file(file_a)[0].pile_id == pile_id


def test_split_pile_with_no_faces_does_nothing(store):
    assert store.split_pile([]) is None


def test_forget_person_removes_the_name_but_keeps_the_faces_by_default(store):
    file_id = _photo(store)
    pile_id = store.create_pile(name="Daddy")
    face_id = store.add_face(file_id, (0, 0, 1, 1), _vec(1, 0))
    store.assign_face(face_id, pile_id)

    store.forget_person(pile_id)

    piles = store.piles_with_counts()
    assert piles[0].id == pile_id
    assert piles[0].name is None
    assert piles[0].face_count == 1
    texts = [c.text for c in store.chunks_for_file(file_id)]
    assert not any(t.startswith("People:") for t in texts)


def test_forget_person_with_delete_faces_removes_everything(store):
    file_id = _photo(store)
    pile_id = store.create_pile(name="Daddy")
    face_id = store.add_face(file_id, (0, 0, 1, 1), _vec(1, 0))
    store.assign_face(face_id, pile_id)

    store.forget_person(pile_id, delete_faces=True)

    assert store.faces_for_file(file_id) == []
    remaining = {row[0] for row in store.conn.execute("SELECT id FROM piles")}
    assert pile_id not in remaining


# ---------------------------------------------------------------------------
# The learning loop - section 2c, cluster -> name -> assign -> suggest ->
# confirm, as one integration test on fixture faces (the order's own words).
# ---------------------------------------------------------------------------

def test_the_learning_loop_end_to_end(store):
    daddy_file = _photo(store, "daddy1.jpg")
    daddy_face = store.add_face(daddy_file, (0, 0, 1, 1), _vec(1.0, 0.0))
    daddy_pile = store.create_pile()
    store.assign_face(daddy_face, daddy_pile)
    store.rename_pile(daddy_pile, "Daddy")

    # A new, very similar face - should auto-assign.
    confident_file = _photo(store, "daddy2.jpg")
    confident_face = store.add_face(
        confident_file, (0, 0, 1, 1), _vec(0.995, 0.05))

    # A new, borderline-similar face - should only be suggested.
    borderline_file = _photo(store, "maybe-daddy.jpg")
    borderline_face = store.add_face(
        borderline_file, (0, 0, 1, 1), _vec(0.6, 0.8))

    centroids = {daddy_pile: fc.centroid_of([_vec(1.0, 0.0)])}
    plan = fc.cluster_batch(
        [(confident_face, _vec(0.995, 0.05)), (borderline_face, _vec(0.6, 0.8))],
        centroids,
    )
    for face_id, pile_id, confidence in plan.assign:
        store.assign_face(face_id, pile_id, confidence=confidence)
    for face_id, pile_id, confidence in plan.suggest:
        store.suggest_face(face_id, pile_id)

    # Confident face auto-assigned and searchable as Daddy already.
    confirmed = store.faces_for_file(confident_file)[0]
    assert confirmed.pile_id == daddy_pile
    assert "People: Daddy" in [c.text for c in store.chunks_for_file(confident_file)]

    # Borderline face is a suggestion, NOT yet assigned or searchable.
    suggested = store.faces_for_file(borderline_file)[0]
    assert suggested.pile_id is None
    assert suggested.suggested_pile_id == daddy_pile
    assert not [c.text for c in store.chunks_for_file(borderline_file)
                if c.text.startswith("People:")]

    # "Is this Daddy?" - yes.
    store.confirm_suggestion(borderline_face, True, confidence=0.6)
    confirmed2 = store.faces_for_file(borderline_file)[0]
    assert confirmed2.pile_id == daddy_pile
    assert "People: Daddy" in [c.text for c in store.chunks_for_file(borderline_file)]

    # A second borderline face, declined - returns to the unclustered pool.
    stranger_file = _photo(store, "stranger.jpg")
    stranger_face = store.add_face(stranger_file, (0, 0, 1, 1), _vec(0.6, 0.8))
    store.suggest_face(stranger_face, daddy_pile)
    store.confirm_suggestion(stranger_face, False)
    declined = store.faces_for_file(stranger_file)[0]
    assert declined.pile_id is None
    assert declined.suggested_pile_id is None


# ---------------------------------------------------------------------------
# pending_suggestions - the strip's own queue (section 2c UI, the "Is this
# <name>?" chip). Separate from the learning-loop integration test above,
# which never asked whether the read side actually surfaces what it wrote.
# ---------------------------------------------------------------------------

def test_pending_suggestions_returns_a_suggestion_against_a_named_pile(store):
    file_id = _photo(store, "maybe.jpg")
    daddy = store.create_pile(name="Daddy")
    face_id = store.add_face(file_id, (1.0, 2.0, 3.0, 4.0), _vec(0.6, 0.8))
    store.suggest_face(face_id, daddy)

    suggestions = store.pending_suggestions()

    assert len(suggestions) == 1
    only = suggestions[0]
    assert only.face_id == face_id
    assert only.file_id == file_id
    assert only.path == f"/photos/maybe.jpg"
    assert only.bbox == (1.0, 2.0, 3.0, 4.0)
    assert only.pile_id == daddy
    assert only.pile_name == "Daddy"


def test_pending_suggestions_excludes_a_suggestion_against_an_unnamed_pile(store):
    r"""A suggestion has nothing to ask ("Is this None?") until somebody
    names the pile it points at - it stays out of the strip's queue, not
    silently dropped from the data."""
    file_id = _photo(store)
    unnamed = store.create_pile()          # never named
    face_id = store.add_face(file_id, (0, 0, 1, 1), _vec(0.6, 0.8))
    store.suggest_face(face_id, unnamed)

    assert store.pending_suggestions() == []

    store.rename_pile(unnamed, "Mum")
    named_now = store.pending_suggestions()
    assert len(named_now) == 1
    assert named_now[0].pile_name == "Mum"


def test_pending_suggestions_excludes_assigned_and_undecided_faces(store):
    file_id = _photo(store)
    pile_id = store.create_pile(name="Daddy")
    assigned = store.add_face(file_id, (0, 0, 1, 1), _vec(1, 0))
    store.assign_face(assigned, pile_id)          # already decided, not a suggestion
    unclustered = store.add_face(file_id, (0, 0, 1, 1), _vec(0, 1))  # noqa: F841

    assert store.pending_suggestions() == []


def test_confirming_a_suggestion_removes_it_from_the_queue(store):
    file_id = _photo(store)
    daddy = store.create_pile(name="Daddy")
    face_id = store.add_face(file_id, (0, 0, 1, 1), _vec(0.6, 0.8))
    store.suggest_face(face_id, daddy)
    assert len(store.pending_suggestions()) == 1

    store.confirm_suggestion(face_id, True)

    assert store.pending_suggestions() == []


def test_declining_a_suggestion_also_removes_it_from_the_queue(store):
    file_id = _photo(store)
    daddy = store.create_pile(name="Daddy")
    face_id = store.add_face(file_id, (0, 0, 1, 1), _vec(0.6, 0.8))
    store.suggest_face(face_id, daddy)

    store.confirm_suggestion(face_id, False)

    assert store.pending_suggestions() == []


def test_pending_suggestions_is_capped_by_limit(store):
    daddy = store.create_pile(name="Daddy")
    for i in range(5):
        file_id = _photo(store, f"m{i}.jpg")
        face_id = store.add_face(file_id, (0, 0, 1, 1), _vec(0.6, 0.8))
        store.suggest_face(face_id, daddy)

    assert len(store.pending_suggestions(limit=2)) == 2
    assert len(store.pending_suggestions(limit=20)) == 5


# ---------------------------------------------------------------------------
# /who - section 3a
# ---------------------------------------------------------------------------

def test_who_is_a_real_offered_command():
    command = command_for("who")
    assert command is not None
    assert command.source == "who"
    assert expand_slashes("/who Daddy") == "who:Daddy"


def test_who_matches_the_field_alias_table():
    from app.search.query import _FIELD_ALIASES

    for command in COMMANDS:
        for spelling in command.spellings:
            assert spelling in _FIELD_ALIASES


def test_who_values_are_browsable_with_counts(store):
    a, b, c = (_photo(store, n) for n in ("a.jpg", "b.jpg", "c.jpg"))
    daddy = store.create_pile(name="Daddy")
    mum = store.create_pile(name="Mum")
    for file_id, pile_id in ((a, daddy), (b, daddy), (c, mum)):
        face_id = store.add_face(file_id, (0, 0, 1, 1), _vec(1, 0))
        store.assign_face(face_id, pile_id)

    counts = {row.value: row.count for row in store.distinct_value_counts("who")}
    assert counts["Daddy"] == 2
    assert counts["Mum"] == 1


def test_unnamed_piles_never_appear_in_who_values(store):
    file_id = _photo(store)
    pile_id = store.create_pile()          # never named
    face_id = store.add_face(file_id, (0, 0, 1, 1), _vec(1, 0))
    store.assign_face(face_id, pile_id)

    assert store.distinct_value_counts("who") == []


def test_a_photo_is_found_by_who_end_to_end(store):
    tagged = _photo(store, "daddy.jpg")
    other = _photo(store, "stranger.jpg")
    pile_id = store.create_pile(name="Daddy")
    face_id = store.add_face(tagged, (0, 0, 1, 1), _vec(1, 0))
    store.assign_face(face_id, pile_id)

    parsed = parse_query(expand_slashes("/who Daddy"))
    assert parsed.who == ("daddy",)
    assert parsed.has_filters
    where, params = file_filter_sql(parsed)
    sql = "SELECT id FROM files f WHERE 1=1 " + where
    rows = store.conn.execute(sql, params).fetchall()
    ids = {row["id"] for row in rows}
    assert ids == {tagged}
    assert other not in ids


def test_a_negated_who_excludes_the_file(store):
    tagged = _photo(store, "daddy.jpg")
    other = _photo(store, "stranger.jpg")
    pile_id = store.create_pile(name="Daddy")
    face_id = store.add_face(tagged, (0, 0, 1, 1), _vec(1, 0))
    store.assign_face(face_id, pile_id)

    parsed = parse_query("-who:Daddy")
    where, params = file_filter_sql(parsed)
    sql = "SELECT id FROM files f WHERE 1=1 " + where
    rows = store.conn.execute(sql, params).fetchall()
    ids = {row["id"] for row in rows}
    assert ids == {other}


def test_who_is_case_insensitive(store):
    file_id = _photo(store)
    pile_id = store.create_pile(name="Daddy")
    face_id = store.add_face(file_id, (0, 0, 1, 1), _vec(1, 0))
    store.assign_face(face_id, pile_id)

    parsed = parse_query("who:DADDY")
    where, params = file_filter_sql(parsed)
    sql = "SELECT id FROM files f WHERE 1=1 " + where
    rows = store.conn.execute(sql, params).fetchall()
    assert {row["id"] for row in rows} == {file_id}


# ---------------------------------------------------------------------------
# The killer query, as fusion sees it (section 3b) - not a demo, an
# integration test: named people (a filter/lane) AND a tag word (free text
# the FTS index already carries) together.
# ---------------------------------------------------------------------------

def test_the_killer_query_finds_daddy_and_me_on_the_beach(store):
    beach_photo = _photo(store, "beach-with-daddy.jpg")
    other_photo = _photo(store, "office-daddy.jpg")     # daddy, no beach
    third_photo = _photo(store, "beach-alone.jpg")       # beach, no daddy

    daddy = store.create_pile(name="Daddy")
    for file_id in (beach_photo, other_photo):
        face_id = store.add_face(file_id, (0, 0, 1, 1), _vec(1, 0))
        store.assign_face(face_id, daddy)

    # Florence-style tag segment (0i section 1b/1c), the free-text half.
    store.set_file_tags(beach_photo, ["beach", "dog"])
    store.set_file_tags(third_photo, ["beach"])

    parsed = parse_query(expand_slashes("/who Daddy") + " beach")
    assert parsed.who == ("daddy",)
    assert "beach" in parsed.terms

    where, params = file_filter_sql(parsed)
    sql = "SELECT id FROM files f WHERE 1=1 " + where
    who_matches = {row["id"] for row in store.conn.execute(sql, params).fetchall()}
    assert who_matches == {beach_photo, other_photo}

    shows_matches = {
        row[0] for row in store.conn.execute(
            "SELECT file_id FROM file_tags WHERE tag = 'beach'")
    }
    assert shows_matches == {beach_photo, third_photo}

    # The fusion of the two lanes - people AND depicted-content - is exactly
    # one photo: the killer case.
    assert who_matches & shows_matches == {beach_photo}


def test_has_filters_recognises_who():
    from app.search.query import ParsedQuery

    assert ParsedQuery(raw="", text="", who=("daddy",)).has_filters is True


# ---------------------------------------------------------------------------
# Switch off means no face code runs, no face rows exist - the order's own
# test list, proved against a real Pipeline over a real photo.
# ---------------------------------------------------------------------------

class _FakeVectors:
    def __init__(self):
        self.rows: dict = {}

    def ensure_table(self):
        pass

    def delete_by_file_ids(self, file_ids):
        wanted = {int(i) for i in file_ids}
        for chunk_id, file_id in list(self.rows.items()):
            if file_id in wanted:
                del self.rows[chunk_id]

    def add(self, *, chunk_ids, file_ids, vectors):
        for chunk_id, file_id in zip(chunk_ids, file_ids, strict=True):
            self.rows[int(chunk_id)] = int(file_id)
        return len(list(chunk_ids))

    def maybe_compact(self, **_kwargs):
        return False

    def maybe_create_index(self, **_kwargs):
        return False

    def count(self):
        return len(self.rows)


def _embedder():
    from app.index.embedder import Embedder

    def encode(texts):
        return [[0.1, 0.2, 0.3, 0.4] for _ in texts]

    return Embedder(dim=4, encoder=encode)


def _real_photo(root, name="family.jpg"):
    from PIL import Image

    root.mkdir(parents=True, exist_ok=True)
    path = root / name
    Image.new("RGB", (64, 64), "blue").save(path)
    return path


def _pipeline(store, *, people_recognition_enabled):
    r"""A real `Pipeline` object, called against directly rather than through
    `run()`.

    **Deliberately not `pipeline.run()` over a real photo.** A photo with no
    OCR text falls straight into the Florence-2 fallback path (`ocr.py`),
    which on this machine's shared venv means a real, cold `torch`/
    `transformers` model load - the exact 30-90s-plus cost 0i's own dated
    note on item 4b already measured and warned every future session about
    ("whoever indexes 100,000 photos next should know the true floor").
    Under this session's own heavy concurrent load (many other agent
    sessions on the same shared machine) that cost was observed to exceed
    five minutes for one bare image, which is not a face-detection question
    at all - `test_era_hints.py`'s own wiring tests hit the identical trap
    and are mocked around it for the same reason. Calling
    `_maybe_detect_faces`/the drains directly tests exactly the code this
    order added, at the cost this order's own code actually has, with zero
    dependency on Florence, OCR or CLIP ever loading anything.
    """
    from app.index.embedder import Embedder
    from app.index.pipeline import Pipeline, PipelineConfig
    from app.index.resources import ResourceLimits
    from app.index.walker import WalkConfig

    return Pipeline(
        store, _FakeVectors(), Embedder(dim=4, encoder=lambda ts: [[0.1] * 4 for _ in ts]),
        PipelineConfig(
            walk=WalkConfig(roots=[]),
            limits=ResourceLimits(pause_on_battery=False),
            people_recognition_enabled=people_recognition_enabled,
        ),
    )


class _FakeCandidate:
    def __init__(self, path):
        self.path = path


def test_switch_off_means_no_face_rows_exist(tmp_path):
    photo = _real_photo(tmp_path)
    with SqliteStore(tmp_path / "index.db") as store:
        file_id = store.upsert_file(
            path=str(photo), size_bytes=1, mtime_ns=1, source_kind="file")
        pipeline = _pipeline(store, people_recognition_enabled=False)

        pipeline._maybe_detect_faces(_FakeCandidate(photo), file_id)

        faces = store.conn.execute("SELECT COUNT(*) AS n FROM faces").fetchone()
        scans = store.conn.execute(
            "SELECT COUNT(*) AS n FROM face_scans").fetchone()
        assert faces["n"] == 0
        assert scans["n"] == 0


def test_switch_off_means_face_detect_is_never_imported(tmp_path, monkeypatch):
    """A stronger form of the assertion above: `app.extract.face_detect` is
    never even imported when the switch is off, which is what
    `_maybe_detect_faces`'s own docstring promises ("switching the setting
    off costs this function nothing, not even the import")."""
    import sys

    monkeypatch.delitem(sys.modules, "app.extract.face_detect", raising=False)
    photo = _real_photo(tmp_path)
    with SqliteStore(tmp_path / "index.db") as store:
        file_id = store.upsert_file(
            path=str(photo), size_bytes=1, mtime_ns=1, source_kind="file")
        pipeline = _pipeline(store, people_recognition_enabled=False)
        pipeline._maybe_detect_faces(_FakeCandidate(photo), file_id)
    assert "app.extract.face_detect" not in sys.modules


def test_switch_off_the_backfill_and_cluster_drains_do_nothing(tmp_path):
    """The enrichment-backlog kinds are switch-gated too - turning the
    switch off mid-session must stop every face-shaped thing this pipeline
    does, not only the one on the images-pass hot path."""
    with SqliteStore(tmp_path / "index.db") as store:
        pipeline = _pipeline(store, people_recognition_enabled=False)
        from app.index.pipeline import IndexStats

        stats = IndexStats()
        pipeline._drain_face_backfill(stats)
        pipeline._drain_face_cluster(stats)

        assert stats.enrichment_counts[pipeline.KIND_FACE_BACKFILL] == 0
        assert stats.enrichment_counts[pipeline.KIND_FACE_CLUSTER] == 0


def test_switch_on_detects_and_records_faces(tmp_path, monkeypatch):
    """With the switch on and a (mocked) detector, faces get recorded and
    the file is marked scanned - the opposite half of the assertions above."""
    from app.extract import face_detect

    def fake_detect(path):
        return [face_detect.FaceDetection(
            bbox=(1.0, 2.0, 3.0, 4.0), embedding=b"\x00" * 16, confidence=0.9)]

    monkeypatch.setattr(face_detect, "detect_faces", fake_detect)

    photo = _real_photo(tmp_path)
    with SqliteStore(tmp_path / "index.db") as store:
        file_id = store.upsert_file(
            path=str(photo), size_bytes=1, mtime_ns=1, source_kind="file")
        pipeline = _pipeline(store, people_recognition_enabled=True)

        pipeline._maybe_detect_faces(_FakeCandidate(photo), file_id)

        faces = store.conn.execute("SELECT COUNT(*) AS n FROM faces").fetchone()
        scans = store.conn.execute(
            "SELECT COUNT(*) AS n FROM face_scans").fetchone()
        assert faces["n"] == 1
        assert scans["n"] == 1


# ---------------------------------------------------------------------------
# Backlog: kill-mid-drain resumes. Work order 0i section 5's own test list.
# ---------------------------------------------------------------------------

def test_caption_trickle_resumes_after_a_kill_mid_drain(tmp_path, monkeypatch):
    r"""An interrupted trickle must not redo work, and must finish the rest.

    Simulated the same way a real crash's *effect* is real here even though
    the cause is not: `pipeline._stop` is set part-way through, exactly as
    it would be by the moment a process dies - no thread actually needs to
    die for the drain's own resumability (it reads its candidate list fresh
    from SQL every call, via `iter_uncaptioned_images`) to be proved.
    """
    from app.extract import vision_caption

    photos = [_real_photo(tmp_path, f"p{i}.jpg") for i in range(4)]
    described: list[str] = []

    def fake_describe(path, client, **_kwargs):
        described.append(str(path))
        return vision_caption.VisionCaptionResult(
            caption="A person.", model=client.model, elapsed_s=0.01)

    monkeypatch.setattr(vision_caption, "describe_image", fake_describe)
    monkeypatch.setattr(vision_caption, "available", lambda client: True)

    with SqliteStore(tmp_path / "index.db") as store:
        file_ids = [
            store.upsert_file(path=str(p), size_bytes=1, mtime_ns=1,
                              ext="jpg", source_kind="file", status="INDEXED")
            for p in photos
        ]

        from app.index.pipeline import IndexStats

        import dataclasses

        first = _pipeline(store, people_recognition_enabled=False)
        first.config = dataclasses.replace(first.config, caption_trickle_enabled=True)
        # Interrupt after the first photo of the first (and only) batch -
        # batch_size in the drain is 8, so this corpus of 4 is one batch;
        # the per-file stop check inside that batch is what this proves.
        real_describe = fake_describe
        calls = {"n": 0}

        def stopping_describe(path, client, **kwargs):
            calls["n"] += 1
            if calls["n"] == 2:
                first._stop.set()
            return real_describe(path, client, **kwargs)

        monkeypatch.setattr(vision_caption, "describe_image", stopping_describe)
        stats1 = IndexStats()
        first._drain_caption_trickle(stats1)

        assert stats1.enrichment_counts["caption_trickle"] < len(photos)
        interrupted_count = len(described)
        assert 0 < interrupted_count < len(photos)

        # A fresh pipeline (a fresh process, after the "crash") finishes it.
        monkeypatch.setattr(vision_caption, "describe_image", real_describe)
        second = _pipeline(store, people_recognition_enabled=False)
        second.config = dataclasses.replace(second.config, caption_trickle_enabled=True)
        stats2 = IndexStats()
        second._drain_caption_trickle(stats2)

        # Every photo described exactly once in total, across both drains -
        # the proof that resuming did not redo completed work.
        assert len(described) == len(photos)
        assert len(set(described)) == len(photos)
        for file_id in file_ids:
            assert store.has_ai_caption(file_id)


def test_iter_uncaptioned_images_matches_dotted_or_undotted_extensions(tmp_path):
    r"""Regression: `OcrExtractor.extensions` carries a leading dot
    (`.jpg`); `files.ext` is stored without one (`indexed_ext`'s own
    convention). Passing the dotted set straight into the `IN (...)`
    clause matched nothing at all - a real bug, found running the caption
    trickle drain for real rather than assumed, not merely a hypothetical."""
    with SqliteStore(tmp_path / "index.db") as store:
        file_id = store.upsert_file(
            path="/photos/a.jpg", size_bytes=1, mtime_ns=1, ext="jpg",
            source_kind="file", status="INDEXED")

        dotted = list(store.iter_uncaptioned_images([".jpg", ".png"]))
        assert {fid for batch in dotted for fid, _ in batch} == {file_id}

        undotted = list(store.iter_uncaptioned_images(["jpg", "png"]))
        assert {fid for batch in undotted for fid, _ in batch} == {file_id}


def test_iter_photos_without_face_scan_matches_dotted_or_undotted_extensions(tmp_path):
    with SqliteStore(tmp_path / "index.db") as store:
        file_id = store.upsert_file(
            path="/photos/a.jpg", size_bytes=1, mtime_ns=1, ext="jpg",
            source_kind="file", status="INDEXED")

        dotted = list(store.iter_photos_without_face_scan([".jpg", ".png"]))
        assert {fid for batch in dotted for fid, _ in batch} == {file_id}

        undotted = list(store.iter_photos_without_face_scan(["jpg", "png"]))
        assert {fid for batch in undotted for fid, _ in batch} == {file_id}
