r"""Empty vectors are found, and their files embedded again.

Layer: L1

Order 1h item 2a (2026-10-10). Before 2026-09-30 DirectML embedding on the
owner's Iris Xe returned all-zero vectors without raising, and they were written
and flagged embedded. `reembed --check` counts them; `reembed --bad` deletes their
files' vectors and embeds those files again.
"""

from __future__ import annotations

import argparse
from types import SimpleNamespace

import numpy as np
import pytest

from app.storage.sqlite_store import SqliteStore
from app.storage.vector_store import VectorStore

DIM = 4


def _vectors(rows):
    return np.asarray(rows, dtype=np.float32)


def test_the_scan_finds_the_empty_vectors(tmp_path) -> None:
    """All zeros is what the driver wrote. LanceDB refuses a NaN at `add`
    ("Vector column contains NaN values"), so that kind cannot be stored."""
    with VectorStore(tmp_path / "v", dim=DIM) as store:
        store.ensure_table()
        store.add(chunk_ids=[1, 2, 3, 4], file_ids=[10, 10, 11, 12],
                  vectors=_vectors([[1, 0, 0, 0], [0, 0, 0, 0],
                                    [0, 0, 0, 0], [0, 0.5, 0.5, 0]]))
        assert sorted(store.bad_vectors(batch_size=2)) == [(2, 10), (3, 11)]


def test_an_empty_store_has_nothing_to_report(tmp_path) -> None:
    with VectorStore(tmp_path / "v", dim=DIM) as store:
        assert store.bad_vectors() == []


def _file_with_chunks(store, name, texts):
    file_id = store.upsert_file(path=f"/docs/{name}", size_bytes=1, mtime_ns=1,
                                ext="txt", source_kind="file")
    ids = store.replace_chunks(file_id, [{"ordinal": i, "text": t} for i, t in enumerate(texts)])
    store.mark_embedded(ids)
    return file_id, ids


def test_a_file_is_queued_whole_so_its_good_vectors_are_not_lost(tmp_path) -> None:
    with SqliteStore(tmp_path / "index.db") as store:
        bad_file, _ = _file_with_chunks(store, "a.txt", ["one", "two"])
        good_file, _ = _file_with_chunks(store, "b.txt", ["three"])
        queued = store.mark_files_unembedded([bad_file])
        flags = dict(store.conn.execute(
            "SELECT file_id, MIN(embedded) FROM chunks GROUP BY file_id").fetchall())
    assert queued == 2
    assert flags[bad_file] == 0 and flags[good_file] == 1


@pytest.fixture
def stores(tmp_path, monkeypatch):
    settings = SimpleNamespace(log_path=tmp_path / "logs", fts_db=tmp_path / "index.db",
                               vector_path=tmp_path / "v", embed_dim=DIM)
    settings.log_path.mkdir()
    monkeypatch.setattr("app.cli.index._load", lambda args: settings)
    monkeypatch.setattr("app.cli.index.setup_logging", lambda *a, **k: None)
    with SqliteStore(settings.fts_db) as store:
        bad_file, bad_ids = _file_with_chunks(store, "a.txt", ["one", "two"])
        good_file, good_ids = _file_with_chunks(store, "b.txt", ["three"])
    with VectorStore(settings.vector_path, dim=DIM) as vectors:
        vectors.ensure_table()
        vectors.add(chunk_ids=[*bad_ids, *good_ids], file_ids=[bad_file, bad_file, good_file],
                    vectors=_vectors([[1, 0, 0, 0], [0, 0, 0, 0], [0, 1, 0, 0]]))
    return settings, bad_file, good_file


def _args(**flags):
    return argparse.Namespace(**{"all": False, "check": False, "bad": False,
                                 "quiet": True, **flags})


def test_check_reports_and_changes_nothing(stores, capsys) -> None:
    from app.cli.index import cmd_reembed

    settings, _bad, _good = stores
    assert cmd_reembed(_args(check=True)) == 0
    assert "1 passage(s) in 1 file(s) have an empty vector" in capsys.readouterr().out
    with VectorStore(settings.vector_path, dim=DIM) as vectors:
        assert len(vectors.bad_vectors()) == 1


def test_bad_embeds_the_whole_file_again_and_leaves_no_empty_vector(stores, monkeypatch) -> None:
    from app.cli import index as cli_index

    class _Model:
        def embed(self, texts):
            return _vectors([[0.5, 0.5, 0, 0]] * len(texts))

    monkeypatch.setattr("app.index.embedder.Embedder.from_settings", lambda settings: _Model())
    settings, bad_file, good_file = stores
    assert cli_index.cmd_reembed(_args(bad=True)) == 0
    with VectorStore(settings.vector_path, dim=DIM) as vectors:
        assert vectors.bad_vectors() == []
        assert vectors.count() == 3, "two for the file embedded again, one untouched"
    with SqliteStore(settings.fts_db) as store:
        assert store.conn.execute("SELECT COUNT(*) FROM chunks WHERE embedded = 0").fetchone()[0] == 0
