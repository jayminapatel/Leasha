r"""Work order 1h §5c: no pictures, no picture lane, no vision host.

Layer: L4

2026-10-10. The picture lane ran on every search, including on an index with
no pictures, and its first call embeds the query with the CLIP text tower -
which in the window starts the vision host (`RemoteEmbedder`), about four
seconds, for a table that cannot answer. The engine now asks the table first,
counts it once per index generation, and skips the lane when it is empty.
The CLIP text encoders here stand in for the vision host: a call to `embed`
is the host being started.
"""

from __future__ import annotations

import pytest

from app.search import engine as engine_module
from app.search.engine import NOTICE_NO_IMAGES, SearchEngine


class _VisionHost:
    """The CLIP text tower behind the vision host. `embed` starts the host."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def embed(self, texts):
        self.calls.extend(texts)
        return [[0.1] * 512 for _ in texts]


class _Pictures:
    """A picture table that says how many rows it has, and counts the asking."""

    def __init__(self, rows: int = 0, *, broken: bool = False) -> None:
        self.rows = rows
        self.broken = broken
        self.counted = 0
        self.searched = 0

    def count(self) -> int:
        self.counted += 1
        if self.broken:
            raise RuntimeError("the picture table is being rebuilt")
        return self.rows

    def search(self, vector, *, k: int = 100, where=None):
        self.searched += 1
        return []


class _NoVectors:
    def search(self, *_args, **_kwargs):
        return []


class _TextModel:
    def embed(self, texts):
        return [[0.0] * 384 for _ in texts]

    def warm_up(self) -> None:
        pass


@pytest.fixture()
def store(tmp_path):
    from app.storage.sqlite_store import SqliteStore

    db = SqliteStore(tmp_path / "index.db").connect()
    file_id = db.upsert_file("C:/work/homework.txt", parent_dir="C:/work", ext="txt",
                             size_bytes=1, mtime_ns=1, status="INDEXED",
                             source_kind="file")
    db.replace_chunks(file_id, [{"ordinal": 0, "text": "my volcano homework"}])
    yield db
    db.close()


def _engine(store, pictures, host):
    return SearchEngine(store, _NoVectors(), _TextModel(), image_vectors=pictures,
                        clip_text_embedder=host, log_usage=False)


def test_the_first_search_on_an_index_with_no_pictures_starts_no_vision_host(store):
    pictures, host = _Pictures(rows=0), _VisionHost()
    engine = _engine(store, pictures, host)
    try:
        response = engine.search("volcano homework")
    finally:
        engine.close()

    assert response.results, "the other lanes must still answer"
    assert host.calls == [], "the vision host was started for an empty picture table"
    assert pictures.searched == 0
    assert response.image_count == 0
    assert not any(n.code == NOTICE_NO_IMAGES for n in response.notices), \
        "an empty table is not a broken lane"


def test_a_real_empty_picture_store_is_skipped(store, tmp_path):
    """The real `ImageVectorStore`, before any photo was indexed: no table."""
    from app.storage.vector_store import ImageVectorStore

    host = _VisionHost()
    with ImageVectorStore(tmp_path / "img_vectors") as pictures:
        engine = _engine(store, pictures, host)
        try:
            engine.search("volcano homework")
        finally:
            engine.close()

    assert host.calls == []


def test_pictures_present_means_the_lane_runs(store):
    pictures, host = _Pictures(rows=3), _VisionHost()
    engine = _engine(store, pictures, host)
    try:
        engine.search("volcano homework")
    finally:
        engine.close()

    assert host.calls == ["volcano homework"]
    assert pictures.searched == 1


def test_the_count_is_kept_until_the_index_generation_moves(store):
    pictures, host = _Pictures(rows=0), _VisionHost()
    engine = _engine(store, pictures, host)
    try:
        engine.search("volcano homework", use_cache=False)
        engine.search("homework", use_cache=False)
        assert pictures.counted == 1, "the table was counted on every search"

        pictures.rows = 1                       # the indexer wrote a photo...
        store.bump_generation()                 # ...and the generation moved
        engine.search("volcano homework", use_cache=False)
    finally:
        engine.close()

    assert pictures.counted == 2
    assert host.calls == ["volcano homework"], "the new photo's lane never ran"


def test_an_empty_answer_is_asked_again_after_a_while(store, monkeypatch):
    """A photo's vector can reach LanceDB after the write that moved the
    generation, so "empty" is not believed for ever."""
    monkeypatch.setattr(engine_module, "PICTURE_RECHECK_S", 0.0)
    pictures, host = _Pictures(rows=0), _VisionHost()
    engine = _engine(store, pictures, host)
    try:
        engine.search("volcano homework", use_cache=False)
        pictures.rows = 1                       # no generation bump this time
        engine.search("volcano homework", use_cache=False)
    finally:
        engine.close()

    assert pictures.counted == 2
    assert host.calls == ["volcano homework"]


def test_a_table_that_cannot_be_counted_is_searched_as_before(store):
    """Unknown is "yes": the lane runs and can say what is wrong itself."""
    pictures, host = _Pictures(broken=True), _VisionHost()
    engine = _engine(store, pictures, host)
    try:
        engine.search("volcano homework")
    finally:
        engine.close()

    assert host.calls == ["volcano homework"]
