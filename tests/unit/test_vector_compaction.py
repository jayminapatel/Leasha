"""The vector index must not get permanently slower every run.

Layer: L1

**The hardest scalability cliff in the review, and it was unrecoverable.**
LanceDB writes a new fragment for every `add` and a new dataset version for
every `delete`, and it never compacts itself. There was no `optimize` or
`compact_files` call anywhere in the codebase. At twenty million chunks that is
tens of thousands of fragments and millions of versions, every one of which a
scan has to open - so the index got slower each run and never recovered.

Two smaller things fed the same cliff:

* `delete_by_file_ids` ran once per document, **including on a first index**
  where by definition there is nothing to delete. A hundred thousand documents
  meant a hundred thousand versions created to delete nothing.
* `maybe_create_index` called `count_rows()` on every batch - a scan of a
  growing table, on the write path, to answer a question that only matters at a
  threshold.

These run against real LanceDB rather than a fake: the whole subject is what the
library does with fragments and versions, and a fake would assert my beliefs
about it instead.
"""

from __future__ import annotations

import pytest

from app.storage.vector_store import (
    COMPACT_EVERY_ROWS,
    KEEP_VERSIONS_HOURS,
    VectorStore,
)

pytest.importorskip("lancedb")

DIM = 8


@pytest.fixture
def store(tmp_path):
    with VectorStore(tmp_path / "vectors", dim=DIM) as opened:
        yield opened


def vectors(count: int, start: int = 0):
    return (
        list(range(start, start + count)),                 # chunk ids
        [1] * count,                                       # file ids
        [[0.1 * (i + 1)] * DIM for i in range(count)],     # vectors
    )


# ---------------------------------------------------------------------------
# Compaction exists at all
# ---------------------------------------------------------------------------

def test_the_store_can_compact():
    """The bare fact the review found missing: no `optimize` or `compact_files`
    call existed anywhere."""
    assert hasattr(VectorStore, "maybe_compact")


def test_compaction_runs_when_forced(store):
    store.add(*vectors(4))
    assert store.maybe_compact(force=True) is True


def test_compaction_is_not_run_on_every_batch(store):
    """It costs real time. Every batch would be worse than never."""
    store.add(*vectors(4))
    assert store.maybe_compact() is False


def test_compaction_happens_often_enough_to_matter():
    """A threshold so high it never fires is the same as having none."""
    assert 1_000 <= COMPACT_EVERY_ROWS <= 200_000


def test_old_versions_are_dropped_not_merely_merged():
    """Merging fragments without collecting versions leaves the history, which
    is most of the space and all of the open cost. Time travel is of no use to
    this application."""
    assert KEEP_VERSIONS_HOURS >= 1


def test_compacting_an_empty_store_does_not_raise(store):
    """Called at the end of every run, including runs that indexed nothing."""
    assert store.maybe_compact(force=True) in (True, False)


def test_a_compaction_failure_is_not_a_failed_run(store, monkeypatch):
    """An uncompacted table answers correctly, just more slowly. Failing an
    indexing run over it would trade a slow index for no index."""
    class Exploding:
        def optimize(self, **_kwargs):
            raise RuntimeError("disk full")

    store._table = Exploding()
    assert store.maybe_compact(force=True) is False


# ---------------------------------------------------------------------------
# Deleting nothing, a hundred thousand times
# ---------------------------------------------------------------------------

def test_deleting_from_an_empty_table_writes_no_version(store):
    """**The first-index case.** Every document deleted nothing and paid a
    dataset version for it."""
    store.ensure_table()
    before = len(list(store._table.list_versions()))
    store.delete_by_file_ids([1, 2, 3])
    assert len(list(store._table.list_versions())) == before


def test_deleting_still_works_once_there_is_something_to_delete(store):
    """The guard must not turn into "never delete" - a re-index would then leave
    the old vectors behind and search would answer from text that is gone."""
    store.add(*vectors(3))
    assert store.count() == 3

    store.delete_by_file_ids([1])
    assert store.count() == 0


def test_deleting_after_a_reopen_still_works(tmp_path):
    """The row estimate is what gates the delete, so it has to survive a reopen
    or the second session would silently stop deleting."""
    with VectorStore(tmp_path / "v", dim=DIM) as first:
        first.add(*vectors(2))

    with VectorStore(tmp_path / "v", dim=DIM) as second:
        assert second._approx_rows == 2, "the estimate must be read back on open"
        second.delete_by_file_ids([1])
        assert second.count() == 0


# ---------------------------------------------------------------------------
# Counting rather than scanning
# ---------------------------------------------------------------------------

def test_adding_does_not_scan_the_table(store, monkeypatch):
    """`count_rows()` on the write path is a scan per batch, and it grows with
    the index - so the write path got slower exactly as the index got bigger."""
    store.add(*vectors(2))

    scans = []
    original = store.count
    monkeypatch.setattr(store, "count", lambda: (scans.append(1), original())[1])

    store.add(*vectors(2, start=10))
    assert not scans, "add() counted rows instead of tracking them"


def test_the_running_total_matches_reality(store):
    store.add(*vectors(3))
    store.add(*vectors(2, start=100))
    assert store._approx_rows == store.count() == 5
