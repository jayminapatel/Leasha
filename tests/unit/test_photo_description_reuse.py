r"""A burst of near-identical photos is described by Florence once, not once a frame.

Layer: L3

2026-10-10, work order model-sequencing item 4c. A photo in the same folder,
taken within a minute by the camera's clock, whose perceptual hash is under
`photo_reuse.REUSE_PHASH_DISTANCE` bits from one Florence has already
described, borrows that description; the copy is marked
(`description_copied_from:<id>` in `index_state`) so it can be found and
redone. Florence-2 is faked: these tests count its calls.

The threshold is a judgement, not a measurement; the share of the owner's
library it skips is for a real run to record.
"""

from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.errors import make_error
from app.extract import florence_tagger
from app.index import photo_reuse
from app.storage.sqlite_store import SqliteStore

SECOND = 1_000_000_000
NOON = 1_750_000_000 * SECOND
#: A 64-bit hash and two neighbours one and two bits away from it - well under
#: the threshold - and one half the bits away, an unrelated picture.
BASE = 0x8F3C_21A0_5E7D_9B14
NEAR_1 = BASE ^ 0x1
NEAR_2 = BASE ^ 0x3
FAR = BASE ^ 0xFFFF_FFFF_0000_0000


@pytest.fixture(autouse=True)
def _not_deferred_after():
    yield
    florence_tagger.defer(False)


@pytest.fixture
def store(tmp_path):
    with SqliteStore(tmp_path / "index.db") as opened:
        yield opened


def _photo(store, folder, name, *, phash, taken_ns, code="ERR_NO_TEXT_LAYER", hint=False):
    path = f"{folder}/{name}"
    file_id = store.upsert_file(path=path, size_bytes=1, mtime_ns=1, ext="jpg",
                                source_kind="file", taken_at_ns=taken_ns,
                                taken_at_is_hint=hint)
    store.set_phashes({file_id: f"{phash:016x}"})
    store.mark_skipped(file_id, make_error(code, "extract.ocr", path=path))
    return file_id


def _pipeline(store):
    from app.index import pipeline as module

    built = module.Pipeline.__new__(module.Pipeline)
    built.store = store
    built._stop = threading.Event()
    built._stop.set()                  # as it is at every run's end
    built._interrupted = False
    built.governor = SimpleNamespace(
        wait_while_throttled=lambda should_stop: SimpleNamespace(action="go"),
        paused_seconds=0.0, pauses=0, paused=False, pause_reason="")
    built._log = __import__("app.core.logging", fromlist=["logger"]).logger
    built._announce_phase = lambda stats, on_progress, phase: None
    built._drain_unembedded = lambda stats, **_how: None
    return built


def _florence(monkeypatch):
    """A fake Florence-2 that says what it was shown and counts the calls."""
    asked: list[str] = []

    def tag(path):
        asked.append(Path(path).name)
        return florence_tagger.FlorenceResult(
            caption=f"A photo called {Path(path).stem}", tags=["sea"], elapsed_s=1.0)

    monkeypatch.setattr(florence_tagger, "available", lambda: True)
    monkeypatch.setattr(florence_tagger, "tag_image", tag)
    return asked


def _description(store, file_id):
    row = store.conn.execute(
        "SELECT text FROM chunks WHERE file_id = ? AND label = 'AI description'",
        (file_id,)).fetchone()
    return None if row is None else row[0]


def _the_scene(store, code="ERR_NO_TEXT_LAYER"):
    """A burst of three in one folder within twenty seconds; the same picture in
    another folder at the same moment; the same picture in the burst's folder an
    hour later; and a different picture in the burst's minute."""
    beach = "/photos/beach"
    return {
        "a": _photo(store, beach, "a.jpg", phash=NEAR_2, taken_ns=NOON, code=code),
        "b": _photo(store, beach, "b.jpg", phash=NEAR_1, taken_ns=NOON + 10 * SECOND, code=code),
        "c": _photo(store, beach, "c.jpg", phash=BASE, taken_ns=NOON + 20 * SECOND, code=code),
        "elsewhere": _photo(store, "/photos/garden", "elsewhere.jpg", phash=BASE,
                            taken_ns=NOON, code=code),
        "later": _photo(store, beach, "later.jpg", phash=BASE,
                        taken_ns=NOON + 3600 * SECOND, code=code),
        "other": _photo(store, beach, "other.jpg", phash=FAR,
                        taken_ns=NOON + 5 * SECOND, code=code),
    }


def test_a_burst_asks_florence_once_and_the_rest_are_marked_copies(store, monkeypatch):
    ids = _the_scene(store)
    asked = _florence(monkeypatch)
    stats = SimpleNamespace(enrichment_counts={}, current="")

    _pipeline(store)._drain_photo_tags(stats)

    burst = {"a.jpg", "b.jpg", "c.jpg"}
    assert len(burst & set(asked)) == 1, f"Florence once for the burst, not {asked}"
    assert {"elsewhere.jpg", "later.jpg", "other.jpg"} <= set(asked), \
        "another folder, an hour later, a different picture: each described itself"
    assert len(asked) == 4

    described = next(name for name in asked if name in burst)
    original = ids[described[0]]
    copies = {ids[n[0]] for n in burst - {described}}
    marked = photo_reuse.copied_descriptions(store.conn)
    assert marked == {copy: original for copy in copies}
    for copy in copies:
        assert _description(store, copy) == _description(store, original)
    assert stats.enrichment_counts["photo_tags"] == 6, "every photo has its description"
    states = dict(store.conn.execute("SELECT id, status FROM files").fetchall())
    assert all(status == "INDEXED" for status in states.values())


def test_the_end_of_run_description_pass_borrows_too(store, monkeypatch):
    """The same rule where photos are described today: the run's end, before
    their text is read (`_drain_picture_text`)."""
    from app.extract import ocr

    ids = _the_scene(store, code="ERR_PICTURE_TEXT_LATER")
    asked = _florence(monkeypatch)
    monkeypatch.setattr(ocr, "ocr_image", lambda path: SimpleNamespace(
        text="", empty=True, engine_missing=False, error=None))

    _pipeline(store)._drain_picture_text(SimpleNamespace(enrichment_counts={}, current=""))

    assert len({"a.jpg", "b.jpg", "c.jpg"} & set(asked)) == 1
    assert len(asked) == 4
    assert len(photo_reuse.copied_descriptions(store.conn)) == 2
    states = dict(store.conn.execute("SELECT id, status FROM files").fetchall())
    assert all(states[i] == "INDEXED" for i in ids.values()), \
        "a copied description indexes the photo as an original one does"


def test_a_copy_is_never_copied_from(store, monkeypatch):
    """Photos one minute apart in a chain: the third is two minutes from the
    only original, so it is described itself rather than borrowing a copy."""
    folder = "/photos/walk"
    first = _photo(store, folder, "1.jpg", phash=BASE, taken_ns=NOON + 120 * SECOND)
    _photo(store, folder, "2.jpg", phash=BASE, taken_ns=NOON + 60 * SECOND)
    _photo(store, folder, "3.jpg", phash=BASE, taken_ns=NOON)
    asked = _florence(monkeypatch)

    _pipeline(store)._drain_photo_tags(SimpleNamespace(enrichment_counts={}, current=""))

    assert asked == ["3.jpg", "1.jpg"], "newest first; 2 borrows from 3, 1 cannot borrow from 2"
    assert set(photo_reuse.copied_descriptions(store.conn).values()) == {
        store.conn.execute("SELECT id FROM files WHERE path LIKE '%3.jpg'").fetchone()[0]}
    assert first not in photo_reuse.copied_descriptions(store.conn)


def test_a_guessed_date_is_not_a_shot_time(store, monkeypatch):
    """A date read from a folder's or a file's name is the same for a whole
    folder; it says nothing about two photos being one burst."""
    folder = "/photos/1998"
    _photo(store, folder, "scan1.jpg", phash=BASE, taken_ns=NOON, hint=True)
    _photo(store, folder, "scan2.jpg", phash=NEAR_1, taken_ns=NOON, hint=True)
    asked = _florence(monkeypatch)

    _pipeline(store)._drain_photo_tags(SimpleNamespace(enrichment_counts={}, current=""))

    assert sorted(asked) == ["scan1.jpg", "scan2.jpg"]
    assert photo_reuse.copied_descriptions(store.conn) == {}


def test_an_earlier_runs_description_is_borrowed_with_one_query_per_folder(store, monkeypatch):
    """The originals of a folder are read once, however many of its photos ask."""
    folder = "/photos/party"
    earlier = _photo(store, folder, "0.jpg", phash=BASE, taken_ns=NOON)
    store.add_caption_chunk(earlier, "Balloons\nTags: party", label="AI description")
    store.mark_indexed(earlier)
    for n in range(1, 6):
        _photo(store, folder, f"{n}.jpg", phash=NEAR_1, taken_ns=NOON + n * SECOND)

    reuse = photo_reuse.DescriptionReuse(store.conn)
    queries: list[str] = []
    real_execute = store.conn.execute

    class Counting:
        def execute(self, sql, *args):
            queries.append(sql)
            return real_execute(sql, *args)

    reuse._conn = Counting()
    ids = [row[0] for row in store.conn.execute(
        "SELECT id FROM files WHERE id != ? ORDER BY id DESC", (earlier,))]
    reuse.prepare(ids)
    found = [reuse.sibling(i) for i in ids]

    assert found == [(earlier, "Balloons\nTags: party")] * 5
    assert len(queries) == 2, "one for the batch's facts, one for the folder"


def test_nothing_to_compare_means_florence_as_before(store):
    """A photo with no hash, or one whose folder has no original yet."""
    lone = store.upsert_file(path="/photos/x/lone.jpg", size_bytes=1, mtime_ns=1,
                             ext="jpg", source_kind="file", taken_at_ns=NOON)
    reuse = photo_reuse.DescriptionReuse(store.conn)
    reuse.prepare([lone])
    assert reuse.sibling(lone) is None
    assert reuse.sibling(12345) is None
