r"""A reset whose vector files Windows will not delete still resets.

Layer: L1

The owner's error, 2026-10-04 13:02: Reset index raised `ERR_UNEXPECTED` -
"This is a bug" - from `VectorStore.drop`, where LanceDB's `drop_table` met
`Access is denied. (os error 5)`. On Windows an open file cannot be deleted,
and something else had the table's files open: an index run, a folder watch
still exiting, the MCP server's own store, or a virus scanner. It did not
reproduce in one process, so the cause is outside the store; what the store
can do is not depend on deleting files. The SQLite half had already been
cleared, so the failure also left the index half reset.
"""

from __future__ import annotations

import random

import pytest

from app.storage import vector_store as vector_module
from app.storage.vector_store import VectorStore

DENIED = ("lance error: LanceError(IO): Access is denied. (os error 5), "
          r"C:\Users\runneradmin\.cargo\registry\src\lance-io-10.0.0\src\local.rs:47:14")


@pytest.fixture()
def vectors(tmp_path, monkeypatch):
    monkeypatch.setattr(vector_module, "DROP_RETRY_WAIT_S", 0.0)
    store = VectorStore(tmp_path / "vectors", dim=8).connect()
    store.ensure_table()
    ids = list(range(50))
    store.add(ids, ids, [[random.random() for _ in range(8)] for _ in ids])
    yield store
    store.close()


def _refuse(store, monkeypatch, times):
    real = store._db.drop_table
    calls = {"n": 0}

    def drop_table(name, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] <= times:
            raise RuntimeError(DENIED)
        return real(name, *args, **kwargs)

    monkeypatch.setattr(store._db, "drop_table", drop_table)
    return calls


def test_a_lock_that_clears_is_waited_out(vectors, monkeypatch):
    calls = _refuse(vectors, monkeypatch, times=2)
    vectors.drop()
    assert calls["n"] == 3
    assert vectors.table_name not in vectors._list_tables()


def test_a_lock_that_stays_empties_the_table_instead(vectors, monkeypatch):
    _refuse(vectors, monkeypatch, times=10**6)
    vectors.drop()                                   # no exception
    assert vectors.count() == 0
    ids = [1, 2]
    vectors.add(ids, ids, [[0.1] * 8, [0.2] * 8])    # and still usable
    assert vectors.count() == 2


def test_any_other_failure_is_still_raised(vectors, monkeypatch):
    def drop_table(name, *args, **kwargs):
        raise RuntimeError("lance error: something else entirely")

    monkeypatch.setattr(vectors._db, "drop_table", drop_table)
    with pytest.raises(RuntimeError, match="something else"):
        vectors.drop()
