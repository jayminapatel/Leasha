"""Work order 0h §1b: the second LanceDB table, mirroring the text table's
delete/compaction/crash-ordering contracts (M6/M8/H7 - see `REVIEW-2026-08-26.md`
and `WORKORDER-202626082352-review-remediation.md` for what those names mean).

`ImageVectorStore` is a thin subclass of `VectorStore` - a different table
name, a different width, and `chunk_id` always equal to `file_id` - so most of
these tests are the *same* tests `test_vector_compaction.py` runs against the
text table, run again against this one. That repetition is deliberate: it is
the proof that inheriting `VectorStore` actually carries the guarantees over,
rather than an assumption about what a subclass "should" do.
"""

from __future__ import annotations

import pytest

from app.storage.vector_store import (
    IMAGE_TABLE_NAME,
    IMAGE_VECTOR_DIM,
    COMPACT_EVERY_ROWS,
    ImageVectorStore,
    VectorStore,
)

pytest.importorskip("lancedb")

DIM = 8


@pytest.fixture
def store(tmp_path):
    with ImageVectorStore(tmp_path / "vectors", dim=DIM) as opened:
        yield opened


def image_batch(count: int, start_file_id: int = 1):
    file_ids = list(range(start_file_id, start_file_id + count))
    vectors = [[0.1 * (i + 1)] * DIM for i in range(count)]
    return file_ids, vectors


# ---------------------------------------------------------------------------
# It really is a second table, in the same LanceDB directory
# ---------------------------------------------------------------------------


def test_defaults_to_its_own_table_name_and_width() -> None:
    assert IMAGE_TABLE_NAME == "image_vectors"
    assert IMAGE_TABLE_NAME != "chunks"
    assert IMAGE_VECTOR_DIM == 512


def test_shares_the_lancedb_directory_with_the_text_table(tmp_path) -> None:
    """One `lancedb.connect(uri)` serves any number of named tables - this is
    a second table, not a second store location."""
    with VectorStore(tmp_path / "vectors", dim=384) as text_store:
        text_store.ensure_table()
    with ImageVectorStore(tmp_path / "vectors", dim=DIM) as image_store:
        image_store.ensure_table()
        assert image_store.uri == text_store.uri
        assert "chunks" in image_store._list_tables()
        assert "image_vectors" in image_store._list_tables()


# ---------------------------------------------------------------------------
# `add_images`: file_id is the whole key
# ---------------------------------------------------------------------------


def test_add_images_writes_chunk_id_equal_to_file_id(store) -> None:
    file_ids, vectors = image_batch(3)
    store.add_images(file_ids, vectors)

    rows = store.search(vectors[0], k=10)
    assert {row["file_id"] for row in rows} == set(file_ids)
    for row in rows:
        assert row["chunk_id"] == row["file_id"]


def test_add_images_returns_the_row_count(store) -> None:
    file_ids, vectors = image_batch(4)
    assert store.add_images(file_ids, vectors) == 4


# ---------------------------------------------------------------------------
# M6 shape: delete-before-add ordering, no orphans from a kill mid-run
# ---------------------------------------------------------------------------


def test_deleting_replaces_rather_than_accumulates(store) -> None:
    """A re-index must not leave the old vector behind next to the new one."""
    file_ids, vectors = image_batch(2)
    store.add_images(file_ids, vectors)
    assert store.count() == 2

    store.delete_by_file_ids([file_ids[0]])
    _, new_vectors = image_batch(1, start_file_id=file_ids[0])
    store.add_images([file_ids[0]], new_vectors)

    assert store.count() == 2, "delete-then-add must not leave an orphaned old row"


def test_a_file_never_embedded_leaves_no_row(store) -> None:
    """The M6-shape guarantee: a file whose CLIP embedding never happened (a
    crash, a broken model) simply has no row here - never a half-written one,
    because `add_images` is one atomic LanceDB `add` call."""
    file_ids, vectors = image_batch(3)
    store.add_images(file_ids[:2], vectors[:2])   # the third was never embedded

    assert store.count() == 2
    assert file_ids[2] not in {row["file_id"] for row in store.search(vectors[0], k=10)}


# ---------------------------------------------------------------------------
# H7 shape: batched delete, not one Lance version per file
# ---------------------------------------------------------------------------


def test_deleting_a_batch_writes_one_version_not_one_per_file(store) -> None:
    file_ids, vectors = image_batch(20)
    store.add_images(file_ids, vectors)
    store.ensure_table()
    before = len(list(store._table.list_versions()))

    store.delete_by_file_ids(file_ids)          # one call, twenty files

    after = len(list(store._table.list_versions()))
    assert after - before <= 2, (
        "one batched delete must cost roughly one dataset version, not one "
        "per file - the exact pathology H7 removed from the text table"
    )
    assert store.count() == 0


def test_deleting_from_an_empty_table_writes_no_version(store) -> None:
    store.ensure_table()
    before = len(list(store._table.list_versions()))
    store.delete_by_file_ids([1, 2, 3])
    assert len(list(store._table.list_versions())) == before


# ---------------------------------------------------------------------------
# M8 shape: no synchronous IVF_PQ retrain from add()
# ---------------------------------------------------------------------------


def test_add_images_never_builds_the_ann_index_itself(store, monkeypatch) -> None:
    """Inherited from `VectorStore.add` unchanged: index training happens once,
    at the end of a run, never synchronously on the write path."""
    calls: list[int] = []
    monkeypatch.setattr(store, "maybe_create_index", lambda **_kw: calls.append(1))

    file_ids, vectors = image_batch(5)
    store.add_images(file_ids, vectors)

    assert not calls, "add_images must not train the ANN index inline"


def test_compaction_is_inherited_and_works(store) -> None:
    file_ids, vectors = image_batch(4)
    store.add_images(file_ids, vectors)
    assert store.maybe_compact(force=True) is True
    assert store.maybe_compact() is False, "must not compact on every batch"


def test_compaction_threshold_is_shared_with_the_text_table() -> None:
    assert 1_000 <= COMPACT_EVERY_ROWS <= 200_000


# ---------------------------------------------------------------------------
# Dimension guard, inherited
# ---------------------------------------------------------------------------


def test_wrong_width_vector_is_refused(store) -> None:
    from app.core.errors import AppErrorException

    with pytest.raises(AppErrorException):
        store.add_images([1], [[0.1] * (DIM + 1)])


def test_reopening_with_a_different_dim_is_refused(tmp_path) -> None:
    from app.core.errors import AppErrorException

    with ImageVectorStore(tmp_path / "v", dim=8) as first:
        first.ensure_table()

    with pytest.raises(AppErrorException):
        ImageVectorStore(tmp_path / "v", dim=16).connect()
