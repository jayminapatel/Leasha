"""Storage review S7, 2026-10-10: the vector store's counts follow the real table.

Layer: L1

* (a) `_indexed_at_rows` was `count()` on every open, so a table whose ANN
  build failed or was killed past `INDEX_MIN_ROWS` was taken for indexed until
  it doubled. It is now what `list_indices()` says the index covers.
* (b) `maybe_create_index` swallowed every failure without a word. It still
  never raises, and now logs why.
* (c) `_approx_rows` grew with adds and never shrank with deletes, so churn
  brought retrains early. A delete now takes its rows off.

Against real LanceDB, as `test_vector_compaction.py` is: the subject is what
the library reports, and a fake would only assert beliefs about it.
"""

from __future__ import annotations

import random

import pytest

from app.storage import vector_store as module
from app.storage.vector_store import ImageVectorStore, VectorStore

pytest.importorskip("lancedb")

DIM = 8
ROWS = 300          # enough for LanceDB to train IVF_PQ on


def _fill(store, count: int = ROWS, start: int = 0, files: int = 30) -> None:
    rnd = random.Random(start)
    ids = list(range(start, start + count))
    store.add(ids, [i % files for i in ids],
              [[rnd.uniform(-1, 1) for _ in range(DIM)] for _ in ids])


def test_a_table_never_indexed_is_not_taken_for_indexed_on_open(tmp_path):
    with VectorStore(tmp_path / "v", dim=DIM) as store:
        _fill(store)
    with VectorStore(tmp_path / "v", dim=DIM) as store:
        assert store.count() == ROWS
        assert store._indexed_at_rows == 0, "rows without an index read back as indexed"


def test_a_killed_build_is_tried_again_on_the_next_run(tmp_path, monkeypatch):
    """The scenario of the review: past the threshold, no index - the next
    run's `maybe_create_index` must build it, not wait for the table to double."""
    monkeypatch.setattr(module, "INDEX_MIN_ROWS", 200)
    with VectorStore(tmp_path / "v", dim=DIM) as store:
        _fill(store)                     # the end-of-run build never happened
    with VectorStore(tmp_path / "v", dim=DIM) as store:
        assert store.maybe_create_index() is True
        assert any("vector" in list(i.columns) for i in store._table.list_indices())


def test_a_built_index_is_read_back_at_its_real_size(tmp_path):
    with VectorStore(tmp_path / "v", dim=DIM) as store:
        _fill(store)
        assert store.maybe_create_index(force=True) is True
        # The same index the deprecated call built: IVF_PQ, cosine.
        (index,) = [i for i in store._table.list_indices() if "vector" in list(i.columns)]
        stats = store._table.index_stats(index.name)
        assert stats.index_type == "IVF_PQ" and stats.distance_type == "cosine"
    with VectorStore(tmp_path / "v", dim=DIM) as store:
        assert store._indexed_at_rows == ROWS
        _fill(store, 50, start=ROWS)                 # added since: not covered
    with VectorStore(tmp_path / "v", dim=DIM) as store:
        assert store._indexed_at_rows == ROWS
        assert store.count() == ROWS + 50


def test_a_lancedb_that_cannot_say_keeps_the_old_answer(tmp_path, monkeypatch):
    with VectorStore(tmp_path / "v", dim=DIM) as store:
        _fill(store)

        def broken():
            raise AttributeError("no list_indices here")

        monkeypatch.setattr(store._table, "list_indices", broken, raising=False)
        assert store._rows_in_index() == ROWS


def test_a_failed_build_says_why_and_does_not_raise(tmp_path, monkeypatch):
    said: list[str] = []
    monkeypatch.setattr(module._log, "warning",
                        lambda message, *args, **kw: said.append(message.format(*args)))
    with VectorStore(tmp_path / "v", dim=DIM) as store:
        _fill(store)

        def refuse(*args, **kwargs):
            raise RuntimeError("not enough memory to train")

        monkeypatch.setattr(store._table, "create_index", refuse, raising=False)
        assert store.maybe_create_index(force=True) is False
        assert store._indexed_at_rows == 0
    assert any("not enough memory to train" in line for line in said), said


def test_deletes_take_their_rows_off_the_running_total(tmp_path):
    with VectorStore(tmp_path / "v", dim=DIM) as store:
        _fill(store)                                  # 30 files of 10 rows
        assert store._approx_rows == ROWS
        store.delete_by_file_ids([0, 1, 2])
        assert store._approx_rows == store.count() == ROWS - 30
        store.delete_by_chunk_ids([100, 101, 999_999])
        assert store._approx_rows == store.count() == ROWS - 32
        store.delete_by_file_ids([12345])             # nothing to delete
        assert store._approx_rows == ROWS - 32


def test_deleting_everything_counts_the_table_rather_than_trust_a_zero(tmp_path, monkeypatch):
    with VectorStore(tmp_path / "v", dim=DIM) as store:
        _fill(store, 20, files=2)
        asked: list[int] = []
        real = VectorStore.count

        def counting(self):
            asked.append(1)
            return real(self)

        monkeypatch.setattr(VectorStore, "count", counting)
        store.delete_by_file_ids([0, 1])
        assert asked, "a total of zero was taken on trust"
        assert store._approx_rows == 0
        # And after a drift (another process added rows) a recount restores it.
        monkeypatch.undo()
        _fill(store, 5, start=1000, files=1)
        store._approx_rows = 1                       # pretend it drifted low
        store.delete_by_chunk_ids([1000])
        assert store._approx_rows == 4


def test_the_image_store_shares_it(tmp_path):
    with ImageVectorStore(tmp_path / "v", dim=DIM) as store:
        _fill(store)
        store.delete_by_file_ids([5])
        assert store._approx_rows == store.count() == ROWS - 10
    with ImageVectorStore(tmp_path / "v", dim=DIM) as store:
        assert store._indexed_at_rows == 0
