"""A table whose folder holds no readable version is set aside, so the store opens and Reset index works.

The owner (2026-10-12): the window opened with an error, and Reset index ended in "Table 'chunks' exists but could
not be loaded ... `_versions` was not found" - on every read, for the rest of the session.
"""

from __future__ import annotations

import shutil

import pytest

from app.storage.vector_store import VectorStore

pytest.importorskip("lancedb")

DIM = 8


def _rows(n=3):
    return list(range(1, n + 1)), list(range(1, n + 1)), [[0.1 * (i + 1)] * DIM for i in range(n)]


def _break(folder):
    """What the owner's folder looked like: `chunks.lance` there, `_versions` empty, nothing else."""
    for child in folder.iterdir():
        shutil.rmtree(child) if child.is_dir() else child.unlink()
    (folder / "_versions").mkdir()


def test_an_empty_dataset_is_set_aside_and_the_store_opens_empty(tmp_path):
    with VectorStore(tmp_path / "v", dim=DIM) as store:
        ids, files, vectors = _rows()
        store.add(ids, files, vectors)
    _break(tmp_path / "v" / "chunks.lance")
    with VectorStore(tmp_path / "v", dim=DIM) as store:               # used to raise on connect
        assert store.count() == 0
        store.drop()                                                  # Reset index: used to raise
        ids, files, vectors = _rows(2)
        store.add(ids, files, vectors)                                # and the next index run can write
        assert store.count() == 2
    assert any(p.name.startswith("chunks.lance.broken-") for p in (tmp_path / "v").iterdir())


def test_a_dataset_that_has_versions_is_never_touched(tmp_path):
    with VectorStore(tmp_path / "v", dim=DIM) as store:
        store.add(*_rows())
    folder = tmp_path / "v" / "chunks.lance"
    # damage something other than the versions: the error must still be raised, the folder left alone
    for child in (folder / "data").iterdir():
        child.unlink()
    try:
        with VectorStore(tmp_path / "v", dim=DIM) as store:
            store.count()
    except Exception:                                                # noqa: BLE001 - whichever way LanceDB says it
        pass
    assert (folder / "_versions").is_dir() and any((folder / "_versions").iterdir())
    assert not any(p.name.startswith("chunks.lance.broken-") for p in (tmp_path / "v").iterdir())
