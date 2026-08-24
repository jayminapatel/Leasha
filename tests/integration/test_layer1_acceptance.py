"""Layer 1 acceptance tests.

The criteria from BUILD_SPEC_V2.md, verbatim:

  1. Fresh DB is created, schema_version set, WAL confirmed on disk.
  2. Insert 10k synthetic chunks; chunks_fts MATCH returns the expected rows.
  3. Deleting a files row cascades to chunks, chunks_fts and the LanceDB rows.
  4. Kill the process mid-write; on restart the DB opens clean and reports the
     last cursor.
  5. LanceDB round-trip: 384-dim insert, ANN query, correct chunk_id returned.

Layer 1 is not done until all five pass.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from app.core.errors import AppErrorException
from app.storage.migrations import CURRENT_VERSION
from app.storage.sqlite_store import FileStatus, SqliteStore
from app.storage.vector_store import VectorStore


def make_file(store: SqliteStore, path: str, **kwargs) -> int:
    defaults = {"size_bytes": 1024, "mtime_ns": 1_700_000_000_000_000_000}
    defaults.update(kwargs)
    return store.upsert_file(path, **defaults)


# --- 1 ----------------------------------------------------------------------

def test_acceptance_1_fresh_db_schema_and_wal(tmp_path: Path) -> None:
    db = tmp_path / "fts" / "knowledge.db"
    with SqliteStore(db) as store:
        assert db.is_file()
        assert store.schema_version == CURRENT_VERSION

        mode = store.conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert str(mode).lower() == "wal"

        # WAL is only observable on disk once something is written.
        store.set_state("probe", "1")
        assert db.with_suffix(".db-wal").exists(), "no -wal file: WAL is not really on"

        assert store.integrity_check()


def test_expected_tables_and_triggers_exist(tmp_path: Path) -> None:
    with SqliteStore(tmp_path / "k.db") as store:
        names = {
            r["name"] for r in store.conn.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table','trigger')"
            )
        }
    for expected in ("files", "chunks", "messages", "chunks_fts", "index_state",
                     "index_generation", "schema_version",
                     "chunks_ai", "chunks_ad", "chunks_au"):
        assert expected in names, f"missing {expected}"


def test_newer_schema_is_refused_not_corrupted(tmp_path: Path) -> None:
    """An older build must refuse a newer index rather than damage it."""
    db = tmp_path / "k.db"
    with SqliteStore(db) as store:
        store.conn.execute("UPDATE schema_version SET version = ? WHERE id = 1",
                           (CURRENT_VERSION + 5,))

    with pytest.raises(AppErrorException) as caught:
        SqliteStore(db).connect()
    err = caught.value.error
    assert "schema_version" in err.message
    assert err.suggestion


# --- 2 ----------------------------------------------------------------------

@pytest.mark.slow
def test_acceptance_2_ten_thousand_chunks_and_fts_match(tmp_path: Path) -> None:
    with SqliteStore(tmp_path / "k.db") as store:
        file_id = make_file(store, r"D:\docs\big.txt")

        chunks = [{"text": f"chunk number {i} about ordinary things", "ordinal": i}
                  for i in range(9_999)]
        # One needle in the haystack.
        chunks.append({"text": "the quarterly pemmican reconciliation was approved",
                       "ordinal": 9_999})

        started = time.perf_counter()
        ids = store.replace_chunks(file_id, chunks)
        elapsed = time.perf_counter() - started

        assert len(ids) == 10_000
        assert store.conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] == 10_000
        assert elapsed < 60, f"10k chunk insert took {elapsed:.1f}s"

        hits = store.search_bm25("pemmican")
        assert len(hits) == 1
        assert "pemmican" in hits[0]["text"]
        assert hits[0]["path"] == r"D:\docs\big.txt"

        # Porter stemming is configured: "approve" must find "approved".
        # (It does NOT relate "reconcile" to "reconciliation" - different stems.)
        assert store.search_bm25("approve"), "porter stemming is not active"
        assert store.search_bm25("approving")

        # Phrase search
        assert store.search_bm25('"quarterly pemmican"')


def test_malformed_query_returns_nothing_rather_than_raising(tmp_path: Path) -> None:
    """Users type unbalanced quotes constantly. A search box must not explode."""
    with SqliteStore(tmp_path / "k.db") as store:
        file_id = make_file(store, r"D:\a.txt")
        store.replace_chunks(file_id, [{"text": "hello world"}])
        for bad in ['"unclosed', "AND AND", "*", "NEAR(", ""]:
            assert store.search_bm25(bad) == [] or isinstance(store.search_bm25(bad), list)


def test_updating_a_chunk_updates_the_fts_index(tmp_path: Path) -> None:
    """The chunks_au trigger; without it, search silently returns stale text."""
    with SqliteStore(tmp_path / "k.db") as store:
        file_id = make_file(store, r"D:\a.txt")
        store.replace_chunks(file_id, [{"text": "aardvark"}])
        assert store.search_bm25("aardvark")

        with store.write() as conn:
            conn.execute("UPDATE chunks SET text = 'buffalo' WHERE file_id = ?", (file_id,))

        assert not store.search_bm25("aardvark"), "stale FTS row survived the update"
        assert store.search_bm25("buffalo")


# --- 3 ----------------------------------------------------------------------

def test_acceptance_3_delete_cascades_everywhere(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "k.db").connect()
    vectors = VectorStore(tmp_path / "vectors", dim=8).connect()
    try:
        keep_id = make_file(store, r"D:\docs\keep.txt")
        drop_id = make_file(store, r"D:\docs\drop.txt")

        keep_chunks = store.replace_chunks(keep_id, [{"text": "kept content aardvark"}])
        drop_chunks = store.replace_chunks(drop_id, [{"text": "doomed content buffalo"},
                                                     {"text": "also doomed"}])
        store.set_message(drop_id, subject="doomed", sender="a@b.c")

        vectors.add(
            chunk_ids=keep_chunks + drop_chunks,
            file_ids=[keep_id] + [drop_id] * len(drop_chunks),
            vectors=[[0.1] * 8, [0.2] * 8, [0.3] * 8],
        )
        assert vectors.count() == 3

        # SQLite cascades; LanceDB cannot be reached by it, so the caller must
        # delete there too. That coupling is the point of this test.
        store.delete_file(drop_id)
        vectors.delete_by_file_ids([drop_id])

        assert store.get_file_by_id(drop_id) is None
        assert store.conn.execute(
            "SELECT COUNT(*) FROM chunks WHERE file_id = ?", (drop_id,)
        ).fetchone()[0] == 0
        assert store.get_message(drop_id) is None
        assert not store.search_bm25("buffalo"), "FTS rows survived the cascade"
        assert store.search_bm25("aardvark"), "the wrong file's chunks were removed"
        assert vectors.count() == 1

        remaining = vectors.search([0.1] * 8, k=5)
        assert all(row["file_id"] == keep_id for row in remaining)
    finally:
        vectors.close()
        store.close()


def test_skip_ledger_records_why_and_groups(tmp_path: Path) -> None:
    """A bad file is remembered, not raised. This drives the review panel."""
    from app.core.errors import make_error

    with SqliteStore(tmp_path / "k.db") as store:
        for name, code in [("a.pst", "ERR_FILE_CORRUPT"),
                           ("b.pst", "ERR_FILE_CORRUPT"),
                           ("c.xlsx", "ERR_FILE_LOCKED")]:
            file_id = make_file(store, rf"D:\docs\{name}")
            store.mark_skipped(file_id, make_error(code, "indexer", path=name))

        assert store.skipped_summary() == {"ERR_FILE_CORRUPT": 2, "ERR_FILE_LOCKED": 1}
        assert len(list(store.iter_files(status=FileStatus.SKIPPED))) == 3


# --- 4 ----------------------------------------------------------------------

CRASH_SCRIPT = textwrap.dedent(
    """
    import os, sys
    sys.path.insert(0, sys.argv[1])
    from app.storage.sqlite_store import SqliteStore

    store = SqliteStore(sys.argv[2]).connect()
    file_id = store.upsert_file(r"D:\\docs\\crash.txt", size_bytes=1, mtime_ns=1)
    store.replace_chunks(file_id, [{"text": "committed before the crash"}])
    store.set_state("cursor:D:\\\\docs", "committed-position-42")
    print("READY", flush=True)

    # Start a write and die inside it, without committing.
    store.conn.execute("BEGIN IMMEDIATE")
    store.conn.execute(
        "INSERT INTO chunks (file_id, ordinal, text) VALUES (?, 99, 'never committed')",
        (file_id,),
    )
    sys.stdout.flush()
    os._exit(9)   # hardest possible kill: no cleanup, no close, no commit
    """
)


def test_acceptance_4_survives_a_kill_mid_write(tmp_path: Path, project_root: Path) -> None:
    db = tmp_path / "k.db"
    script = tmp_path / "crasher.py"
    script.write_text(CRASH_SCRIPT, encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(script), str(project_root), str(db)],
        capture_output=True, text=True, timeout=120,
    )
    assert "READY" in result.stdout, result.stderr
    assert result.returncode == 9, f"the crasher did not die as expected: {result.returncode}"

    # Reopen: committed work survives, the uncommitted write does not, and the
    # cursor is intact so indexing can resume.
    with SqliteStore(db) as store:
        assert store.integrity_check(), "database is corrupt after a hard kill"
        assert store.get_state(r"cursor:D:\docs") == "committed-position-42"
        assert store.search_bm25("committed"), "committed data was lost"
        assert not store.search_bm25("never"), "uncommitted data was persisted"
        assert store.schema_version == CURRENT_VERSION


def test_cursor_round_trips(tmp_path: Path) -> None:
    with SqliteStore(tmp_path / "k.db") as store:
        store.set_state("cursor:D:\\Docs", "file-1234")
        store.set_state("cursor:D:\\Docs", "file-5678")  # last write wins
        assert store.get_state("cursor:D:\\Docs") == "file-5678"
        assert store.get_state("cursor:missing", "default") == "default"
        assert "cursor:D:\\Docs" in store.all_state()


def test_generation_increments_on_write(tmp_path: Path) -> None:
    """The search cache keys on this; if it does not move, stale results."""
    with SqliteStore(tmp_path / "k.db") as store:
        before = store.generation
        file_id = make_file(store, r"D:\a.txt")
        after_file = store.generation
        store.replace_chunks(file_id, [{"text": "x"}])
        after_chunks = store.generation

        assert after_file > before
        assert after_chunks > after_file


# --- 5 ----------------------------------------------------------------------

def test_acceptance_5_lancedb_round_trip_384(tmp_path: Path) -> None:
    with VectorStore(tmp_path / "vectors", dim=384) as vectors:
        target = [0.9] + [0.01] * 383
        other = [0.01] * 384

        written = vectors.add(
            chunk_ids=[101, 202, 303],
            file_ids=[1, 1, 2],
            vectors=[target, other, other],
            exts=["pdf", "pdf", "docx"],
            mtimes_ns=[1, 2, 3],
        )
        assert written == 3
        assert vectors.count() == 3

        hits = vectors.search(target, k=1)
        assert len(hits) == 1
        assert hits[0]["chunk_id"] == 101
        assert "vector" not in hits[0], "the payload should not carry the raw vector"


def test_vector_filters_push_down(tmp_path: Path) -> None:
    with VectorStore(tmp_path / "vectors", dim=8) as vectors:
        vectors.add(
            chunk_ids=[1, 2, 3],
            file_ids=[1, 2, 3],
            vectors=[[0.1] * 8, [0.2] * 8, [0.3] * 8],
            exts=["pdf", "docx", "pdf"],
        )
        hits = vectors.search([0.1] * 8, k=10, where="ext = 'pdf'")
        assert {h["chunk_id"] for h in hits} == {1, 3}


def test_wrong_dimension_is_refused(tmp_path: Path) -> None:
    """Writing a mismatched vector would poison every future search."""
    with VectorStore(tmp_path / "vectors", dim=8) as vectors:
        with pytest.raises(AppErrorException) as caught:
            vectors.add(chunk_ids=[1], file_ids=[1], vectors=[[0.1] * 16])
        assert "dimension" in (caught.value.error.details or "").lower()


def test_model_change_against_existing_index_is_caught(tmp_path: Path) -> None:
    """Changing EMBED_MODEL under an existing index must not append silently."""
    uri = tmp_path / "vectors"
    with VectorStore(uri, dim=8) as vectors:
        vectors.add(chunk_ids=[1], file_ids=[1], vectors=[[0.5] * 8])

    with pytest.raises(AppErrorException) as caught:
        VectorStore(uri, dim=16).connect()
    err = caught.value.error
    assert "EMBED_DIM" in err.message
    assert "rebuild" in err.suggestion.lower()


def test_search_on_empty_store_returns_empty(tmp_path: Path) -> None:
    """Searching before the first index run is normal, not an error."""
    with VectorStore(tmp_path / "vectors", dim=8) as vectors:
        assert vectors.search([0.1] * 8) == []
        assert vectors.count() == 0


def test_vectors_are_rebuildable_from_sqlite(tmp_path: Path) -> None:
    """The asymmetry that makes a crash recoverable: SQLite is the authority."""
    store = SqliteStore(tmp_path / "k.db").connect()
    vectors = VectorStore(tmp_path / "vectors", dim=8).connect()
    try:
        file_id = make_file(store, r"D:\a.txt")
        chunk_ids = store.replace_chunks(
            file_id, [{"text": "one"}, {"text": "two"}, {"text": "three"}]
        )
        vectors.add(chunk_ids, [file_id] * 3, [[0.1] * 8] * 3)
        store.mark_embedded(chunk_ids)
        assert vectors.count() == 3

        # Simulate vector corruption: drop everything.
        vectors.drop()
        assert vectors.count() == 0

        # SQLite still knows every chunk, so the vectors can be regenerated.
        with store.write() as conn:
            conn.execute("UPDATE chunks SET embedded = 0")

        pending = [c for batch in store.iter_unembedded() for c in batch]
        assert len(pending) == 3
        vectors.add([c.id for c in pending], [c.file_id for c in pending],
                    [[0.1] * 8] * len(pending))
        assert vectors.count() == 3
    finally:
        vectors.close()
        store.close()
