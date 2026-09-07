r"""Work order 0h §2a: `files.phash` - the migration, and the store surface
around it.

Mirrors `test_review_2026_08_26.py`'s own shape for exercising a migration
directly against a raw `sqlite3.Connection`: a fresh database walks the whole
chain via `SqliteStore`; a database frozen at v16 (the version before this
one) proves the upgrade path an existing, real index will actually take.
"""

from __future__ import annotations

import sqlite3

import pytest

from app.storage.migrations import CURRENT_VERSION, apply_migrations
from app.storage.sqlite_store import SqliteStore


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _indexes(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA index_list({table})")}


# ---------------------------------------------------------------------------
# The migration itself
# ---------------------------------------------------------------------------


def test_the_migration_is_registered_and_current():
    r"""**2026-09-07 (work order 0f §3a):** this asserted
    `CURRENT_VERSION == 17` and broke the moment v18 (`taken_at_ns`) was
    added - the same trap `test_query_plans.py` records against itself in its
    own comment: *"`CURRENT_VERSION`, not the literal 5. This asserted `== 5`
    and broke the moment v6 was added - the claim is 'a database from before
    this migration gets it and keeps its rows', which has nothing to do with
    the number."*

    Retargeted onto what this file is actually about, in the shape
    `test_identifiers.py::test_the_migration_is_registered_and_current`
    already uses for exactly this: the pHash migration is registered, and no
    later migration has dropped it. The literal 17 stays as a floor because
    that genuinely is the version this column arrived at.
    """
    from app.storage.migrations import MIGRATIONS

    assert 17 in MIGRATIONS
    assert CURRENT_VERSION >= 17


def test_a_fresh_database_has_the_column(tmp_path):
    with SqliteStore(tmp_path / "index.db") as store:
        assert "phash" in _columns(store.conn, "files")
        assert store.schema_version >= 17


def test_a_database_carried_forward_from_v16_gains_the_column(tmp_path):
    r"""The migration a real, existing index will actually run: not a fresh
    schema, a database that already has years of files in it, walked forward
    one version."""
    path = tmp_path / "carried.db"
    conn = sqlite3.connect(path)
    conn.isolation_level = None
    try:
        apply_migrations(conn)                     # fresh, at CURRENT_VERSION
        conn.execute(
            "INSERT INTO files (path, parent_dir, ext, size_bytes, mtime_ns, "
            "status, source_kind) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (r"C:\Photos\old.jpg", r"C:\Photos", "jpg", 100, 1, "INDEXED", "file"),
        )
        conn.execute("DROP INDEX IF EXISTS idx_files_phash")
        conn.execute("ALTER TABLE files DROP COLUMN phash")
        conn.execute("UPDATE schema_version SET version = 16 WHERE id = 1")

        apply_migrations(conn)

        assert "phash" in _columns(conn, "files")
        # The existing row survived the migration untouched, with no pHash -
        # additive and nullable, exactly as the migration's docstring says.
        row = conn.execute(
            "SELECT phash FROM files WHERE path = ?", (r"C:\Photos\old.jpg",)
        ).fetchone()
        assert row is not None
        assert row[0] is None
    finally:
        conn.close()


def test_re_running_the_migration_is_a_no_op(tmp_path):
    """Migrations are additive and re-runnable - `apply_migrations` on an
    already-current database must not raise or duplicate the column."""
    path = tmp_path / "twice.db"
    conn = sqlite3.connect(path)
    conn.isolation_level = None
    try:
        apply_migrations(conn)
        apply_migrations(conn)                      # already current - no-op
        columns = [row[1] for row in conn.execute("PRAGMA table_info(files)")]
        assert columns.count("phash") == 1
    finally:
        conn.close()


def test_the_partial_index_exists():
    """`idx_files_phash` - a partial index, `WHERE phash IS NOT NULL`, the
    same shape as `idx_files_skip` - so an exact-match lookup does not scan
    every file that has never been hashed."""
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        with SqliteStore(Path(tmp) / "index.db") as store:
            names = {row[1] for row in
                     store.conn.execute("PRAGMA index_list(files)")}
            assert "idx_files_phash" in names


# ---------------------------------------------------------------------------
# `SqliteStore.set_phashes`
# ---------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path):
    with SqliteStore(tmp_path / "index.db") as opened:
        yield opened


def _make_file(store, path: str) -> int:
    return store.upsert_file(
        path, size_bytes=1, mtime_ns=1, ext="jpg",
        parent_dir=path.rsplit("\\", 1)[0], status="INDEXED",
        source_kind="file",
    )


def test_set_phashes_writes_the_value(store):
    file_id = _make_file(store, r"C:\Photos\a.jpg")
    store.set_phashes({file_id: "abc123abc123abc1"})

    row = store.get_file_by_id(file_id)
    assert row.phash == "abc123abc123abc1"


def test_set_phashes_writes_a_whole_batch_in_one_call(store):
    ids = [_make_file(store, fr"C:\Photos\{i}.jpg") for i in range(5)]
    store.set_phashes({file_id: f"hash{file_id:012x}" for file_id in ids})

    for file_id in ids:
        row = store.get_file_by_id(file_id)
        assert row.phash == f"hash{file_id:012x}"


def test_set_phashes_of_an_empty_dict_does_nothing(store):
    file_id = _make_file(store, r"C:\Photos\a.jpg")
    store.set_phashes({})                            # must not raise
    assert store.get_file_by_id(file_id).phash is None


def test_set_phashes_drops_empty_values_rather_than_writing_them(store):
    """`files.phash` is nullable so 'not computed yet' stays distinguishable
    from 'computed and empty' - the second can never legitimately happen, so
    an empty value is dropped rather than written as one."""
    file_id = _make_file(store, r"C:\Photos\a.jpg")
    store.set_phashes({file_id: "", 999_999: None})  # neither should write
    assert store.get_file_by_id(file_id).phash is None


def test_a_file_with_no_phash_yet_reads_as_none(store):
    file_id = _make_file(store, r"C:\Photos\untouched.jpg")
    assert store.get_file_by_id(file_id).phash is None
    assert store.get_file(r"C:\Photos\untouched.jpg").phash is None
