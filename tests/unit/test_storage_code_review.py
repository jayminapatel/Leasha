"""The store's findings from the 2026-10-04 code review, each pinned.

Layer: L1. The owner: "comprehensive code review for performance consistency
and reliability ... Do the recommended push and finish all".
"""

from __future__ import annotations

import gc
import sqlite3
import threading
from pathlib import Path

import pytest

from app.core.errors import AppErrorException, make_error
from app.storage import migrations
from app.storage.sqlite_store import FileStatus, SqliteStore


@pytest.fixture()
def store(tmp_path):
    s = SqliteStore(tmp_path / "index.db").connect()
    yield s
    s.close()


def _file(store, path, **kw):
    values = dict(parent_dir=str(Path(path).parent), size_bytes=10, mtime_ns=1,
                  status="INDEXED", source_kind="file")
    values.update(kw)
    return store.upsert_file(str(path), **values)


def _statements(store):
    seen: list[str] = []
    store.conn.set_trace_callback(seen.append)
    return seen


# ---------------------------------------------------------------------------
# apply_batch_era: the folder Qt gives, matched as a folder
# ---------------------------------------------------------------------------

def test_a_folder_from_the_dialog_dates_the_photos_stored_under_it(store, tmp_path) -> None:
    scans = tmp_path / "Photos" / "Scans"
    inside = [_file(store, scans / "a.jpg"), _file(store, scans / "deep" / "b.jpg")]
    beside = _file(store, tmp_path / "Photos" / "Scans2" / "c.jpg")
    fact = _file(store, scans / "exif.jpg", taken_at_ns=5, taken_at_is_hint=False)

    changed = store.apply_batch_era(scans.as_posix(), 123)     # Qt: forward slashes

    assert changed == 2
    rows = {r.id: r for r in store.iter_files()}
    assert all(rows[i].taken_at_ns == 123 for i in inside)
    assert rows[beside].taken_at_ns is None, "Scans is not Scans2"
    assert rows[fact].taken_at_ns == 5, "a camera's own date is never overridden"


def test_the_batch_era_update_searches_the_path_index(store, tmp_path) -> None:
    _file(store, tmp_path / "Scans" / "a.jpg")
    seen = _statements(store)
    store.apply_batch_era(str(tmp_path / "Scans"), 1)
    store.conn.set_trace_callback(None)
    updates = [s for s in seen if s.lstrip().upper().startswith("UPDATE FILES SET TAKEN")]
    assert updates and all("LIKE" not in s for s in updates)
    plan = store.conn.execute(
        "EXPLAIN QUERY PLAN UPDATE files SET taken_at_ns = 1 "
        "WHERE path >= 'a' AND path < 'b'").fetchall()
    assert any("USING INDEX" in str(tuple(row)) or "USING COVERING" in str(tuple(row))
               for row in plan), plan


# ---------------------------------------------------------------------------
# Counts read again only after a write
# ---------------------------------------------------------------------------

def test_the_listed_count_is_read_once_until_something_is_written(store, tmp_path) -> None:
    _file(store, tmp_path / "a.txt")
    assert store.count_listed_files() == 1
    seen = _statements(store)
    assert store.count_listed_files() == 1
    store.conn.set_trace_callback(None)
    assert not [s for s in seen if "COUNT(*)" in s], seen

    _file(store, tmp_path / "b.txt")                       # this connection writes
    assert store.count_listed_files() == 2

    def elsewhere() -> None:                               # another connection writes
        _file(store, tmp_path / "c.txt")

    worker = threading.Thread(target=elsewhere)
    worker.start()
    worker.join()
    assert store.count_listed_files() == 3


def test_status_counts_follow_a_skip_that_moves_no_generation(store, tmp_path) -> None:
    """`mark_skipped` does not bump the generation - so the cache is not keyed
    on it."""
    file_id = _file(store, tmp_path / "a.txt", status="PENDING")
    before = store.status_counts()
    assert before["by_status"] == {"PENDING": 1}
    generation = store.generation

    def skip() -> None:
        store.mark_skipped(file_id, make_error("ERR_FILE_LOCKED", "test", path="a.txt"))

    worker = threading.Thread(target=skip)
    worker.start()
    worker.join()
    assert store.generation == generation
    after = store.status_counts()
    assert after["by_status"] == {"SKIPPED": 1}
    assert after["coded"] == [("SKIPPED", "ERR_FILE_LOCKED", 1)]
    after["by_status"]["SKIPPED"] = 99                      # a copy, not the cache
    assert store.status_counts()["by_status"] == {"SKIPPED": 1}


# ---------------------------------------------------------------------------
# Connections of threads that have gone are closed
# ---------------------------------------------------------------------------

def test_a_finished_threads_connection_does_not_stay_open(store, tmp_path) -> None:
    _file(store, tmp_path / "a.txt")
    opened_before = len(store._open)

    def read() -> None:
        store.count_messages()

    for _ in range(5):
        worker = threading.Thread(target=read)
        worker.start()
        worker.join()
        gc.collect()
    # Each new thread's connection closes the ones whose threads have gone.
    assert len(store._open) <= opened_before + 1
    assert store.count_listed_files() == 1                 # this thread's is untouched


def test_a_connection_in_use_is_never_closed_by_pruning(store) -> None:
    conn = store.conn
    with conn._guard:
        conn._busy += 1
    try:
        conn._holder = lambda: None                        # its thread "gone"
        with store._conns():
            assert store._prune_orphans() == 0
        assert conn in store._open
    finally:
        with conn._guard:
            conn._busy -= 1


# ---------------------------------------------------------------------------
# Migrations: a step and its version, both or neither
# ---------------------------------------------------------------------------

def test_a_failed_step_leaves_the_index_as_it_was(tmp_path, monkeypatch) -> None:
    path = tmp_path / "index.db"
    SqliteStore(path).connect().close()
    target = migrations.CURRENT_VERSION + 1

    def half_then_fail(conn: sqlite3.Connection) -> None:
        conn.execute("CREATE TABLE half_done (x INTEGER)")
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(migrations, "CURRENT_VERSION", target)
    monkeypatch.setitem(migrations.MIGRATIONS, target, half_then_fail)
    conn = sqlite3.connect(path, isolation_level=None)
    with pytest.raises(AppErrorException) as caught:
        migrations.apply_migrations(conn)
    assert caught.value.error.code == "ERR_MIGRATION_FAILED"
    assert caught.value.error.suggestion
    assert migrations.read_version(conn) == target - 1
    assert conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name = 'half_done'").fetchone() is None
    conn.close()


def test_a_step_another_process_finished_is_not_run_twice(tmp_path, monkeypatch) -> None:
    path = tmp_path / "index.db"
    SqliteStore(path).connect().close()
    target = migrations.CURRENT_VERSION + 1
    runs: list[int] = []
    monkeypatch.setattr(migrations, "CURRENT_VERSION", target)
    monkeypatch.setitem(migrations.MIGRATIONS, target,
                        lambda conn: runs.append(1) or conn.execute(
                            "CREATE TABLE once_only (x INTEGER)"))
    first = sqlite3.connect(path, isolation_level=None)
    second = sqlite3.connect(path, isolation_level=None)
    # Both saw the old version; the first one finishes the step.
    assert migrations.read_version(second) == target - 1
    migrations.apply_migrations(first)
    assert migrations._apply_step(second, target, migrations.MIGRATIONS[target]) == target
    assert runs == [1]
    first.close()
    second.close()


def test_the_own_transaction_list_is_the_migrations_that_open_their_own() -> None:
    import inspect

    found = set()
    for version, migration in migrations.MIGRATIONS.items():
        source = inspect.getsource(migration)
        if any(word in source for word in ("executescript", "BEGIN IMMEDIATE",
                                           "foreign_keys", "_backfill_symbols(")):
            found.add(version)
    assert found == set(migrations._OWN_TRANSACTION)


# ---------------------------------------------------------------------------
# v33
# ---------------------------------------------------------------------------

def test_v33_blanks_an_ost_attachments_archive_size_and_nothing_else(store) -> None:
    archive = 4_900_000_000
    mail = dict(parent_dir="pst://Box", mtime_ns=1, status="INDEXED",
                source_kind="pst_message")
    for box, ext in (("Ost", "ost"), ("Pst", "pst")):
        store.upsert_file(f"pst://{box}/E1", ext=ext, size_bytes=archive, **mail)
        store.upsert_file(f"pst://{box}/E1/attachments/a.docx", ext="docx",
                          size_bytes=archive, **mail)
        store.upsert_file(f"pst://{box}/E1/attachments/b.pdf", ext="pdf",
                          size_bytes=4096, **mail)
    with store.write() as conn:
        migrations._v33_outlook_attachment_sizes_and_skip_index(conn)
        migrations._v33_outlook_attachment_sizes_and_skip_index(conn)   # idempotent
    sizes = {r.path: r.size_bytes for r in store.iter_files()}
    for box in ("Ost", "Pst"):
        assert sizes[f"pst://{box}/E1/attachments/a.docx"] == 0
        assert sizes[f"pst://{box}/E1/attachments/b.pdf"] == 4096
        assert sizes[f"pst://{box}/E1"] == archive, "the message row keeps its own"


def test_the_skipped_rows_with_these_codes_are_a_search(store, tmp_path) -> None:
    assert store.schema_version >= 33
    keep = _file(store, tmp_path / "locked.txt", status="PENDING")
    other = _file(store, tmp_path / "bad.txt", status="PENDING")
    store.mark_skipped(keep, make_error("ERR_FILE_LOCKED", "t", path="x"))
    store.mark_skipped(other, make_error("ERR_FILE_CORRUPT", "t", path="x"))
    found = [r.id for r in store.iter_files(FileStatus.SKIPPED,
                                            skip_codes=("ERR_FILE_LOCKED",))]
    assert found == [keep]
    assert list(store.iter_files(FileStatus.SKIPPED, skip_codes=())) == []
    plan = store.conn.execute(
        "EXPLAIN QUERY PLAN SELECT * FROM files WHERE status = ? AND skip_code IN (?) "
        "ORDER BY id", ("SKIPPED", "ERR_FILE_LOCKED")).fetchall()
    assert any("idx_files_status_skip" in str(tuple(row)) for row in plan), plan


# ---------------------------------------------------------------------------
# The filename index is written when the name can have changed
# ---------------------------------------------------------------------------

def test_reading_a_named_file_again_does_not_rewrite_its_name(store, tmp_path) -> None:
    path = tmp_path / "Docs" / "report.docx"
    store.add_waiting_files([{"path": str(path), "parent_dir": str(path.parent),
                              "ext": "docx", "size_bytes": 1, "mtime_ns": 1}])
    seen = _statements(store)
    _file(store, path)
    store.conn.set_trace_callback(None)
    assert not [s for s in seen if "files_fts" in s], seen

    moved = tmp_path / "Elsewhere"
    _file(store, path, parent_dir=str(moved))                # its folder changed
    row = store.conn.execute(
        "SELECT folder FROM files_fts WHERE files_fts MATCH ?", ('"report"',)).fetchone()
    assert row is not None and row[0] == str(moved)


def test_a_new_file_is_named(store, tmp_path) -> None:
    _file(store, tmp_path / "budget.xlsx")
    assert store.conn.execute(
        "SELECT COUNT(*) FROM files_fts WHERE files_fts MATCH ?", ('"budget"',)).fetchone()[0] == 1


# ---------------------------------------------------------------------------
# The Files tab's name half lists what the rest of the tab lists
# ---------------------------------------------------------------------------

def test_a_zip_member_is_not_found_by_name_on_the_files_tab(store, tmp_path) -> None:
    from app.search.query import parse_query

    _file(store, tmp_path / "quarterly.docx")
    _file(store, f"{tmp_path / 'backup.zip'}/quarterly-old.docx", source_kind="archive")
    parsed = parse_query("quarterly")
    paths = [row["path"] for row in store.browse_files(parsed, limit=50)]
    assert paths == [str(tmp_path / "quarterly.docx")]
    assert store.count_browse_files(parsed) == 1


# ---------------------------------------------------------------------------
# The vector store opened before its table existed
# ---------------------------------------------------------------------------

def test_a_vector_table_created_later_is_counted_so_deletes_reach_it(tmp_path) -> None:
    pytest.importorskip("lancedb")
    from app.storage.vector_store import VectorStore

    dim = 8
    with VectorStore(tmp_path / "v", dim=dim) as reader:
        assert reader.count() == 0
        with VectorStore(tmp_path / "v", dim=dim) as writer:
            writer.ensure_table()
            writer.add([1, 2], [1, 2], [[0.1] * dim, [0.2] * dim])
        reader.delete_by_file_ids([1])
        assert reader._approx_rows >= 1
        assert reader.count() == 1
