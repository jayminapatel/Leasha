"""One migration step and its version: both or neither, once, and the version moves last.

Order 1u section 4d. `test_storage_code_review.py` pins the first two promises of `_apply_step` for an ordinary
step. These cover the rest of what `app.storage.migrations._apply_step` and `_roll_back` promise: a step
that fails part way through a run of several, a step that raises an `AppErrorException` of its own, the
steps that manage their own transaction, the version as another connection sees it while a step runs, and
a rollback that never hides the error that caused it.
"""

from __future__ import annotations

import sqlite3

import pytest

from app.core.errors import AppErrorException, make_error
from app.storage import migrations
from app.storage.sqlite_store import SqliteStore


@pytest.fixture()
def index(tmp_path):
    """A current index on disk, and the version one past it for a test step."""
    path = tmp_path / "index.db"
    SqliteStore(path).connect().close()
    return path, migrations.CURRENT_VERSION + 1


def _open(path):
    return sqlite3.connect(path, isolation_level=None)


def _has_table(conn, name):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE name = ?", (name,)).fetchone() is not None


def _add_steps(monkeypatch, start, *steps):
    for n, step in enumerate(steps):
        monkeypatch.setitem(migrations.MIGRATIONS, start + n, step)
    monkeypatch.setattr(migrations, "CURRENT_VERSION", start + len(steps) - 1)


def test_a_failure_in_the_second_of_two_steps_keeps_the_first_and_leaves_the_version_after_it(index, monkeypatch):
    path, first = index

    def good(conn):
        conn.execute("CREATE TABLE step_one (x INTEGER)")

    def bad(conn):
        conn.execute("CREATE TABLE step_two (x INTEGER)")
        raise sqlite3.OperationalError("database or disk is full")

    _add_steps(monkeypatch, first, good, bad)
    conn = _open(path)

    with pytest.raises(AppErrorException) as caught:
        migrations.apply_migrations(conn)

    err = caught.value.error
    assert err.code == "ERR_MIGRATION_FAILED"
    assert err.component == "storage.migrations"
    assert "database or disk is full" in (err.details or "")
    assert "bad" in (err.details or ""), "the details name the step that failed"
    assert migrations.read_version(conn) == first
    assert _has_table(conn, "step_one")
    assert not _has_table(conn, "step_two")
    assert not conn.in_transaction
    conn.close()


def test_an_app_error_raised_by_a_step_is_rolled_back_and_passes_through_as_itself(index, monkeypatch):
    path, target = index
    own = AppErrorException(make_error("ERR_CONFIG_INVALID", "storage.migrations", key="k", reason="r"))

    def refuses(conn):
        conn.execute("CREATE TABLE refused (x INTEGER)")
        raise own

    _add_steps(monkeypatch, target, refuses)
    conn = _open(path)

    with pytest.raises(AppErrorException) as caught:
        migrations.apply_migrations(conn)

    assert caught.value is own
    assert caught.value.error.code == "ERR_CONFIG_INVALID"
    assert migrations.read_version(conn) == target - 1
    assert not _has_table(conn, "refused")
    conn.close()


def test_the_version_is_not_visible_to_another_connection_until_the_step_has_finished(index, monkeypatch):
    path, target = index
    seen: list[int] = []
    watcher = _open(path)

    def slow(conn):
        conn.execute("CREATE TABLE slow_step (x INTEGER)")
        seen.append(migrations.read_version(watcher))

    _add_steps(monkeypatch, target, slow)
    conn = _open(path)

    assert migrations.apply_migrations(conn) == target

    assert seen == [target - 1], "mid-step, the file still says the old version"
    assert migrations.read_version(watcher) == target
    watcher.close()
    conn.close()


def test_a_second_run_over_a_current_index_applies_nothing(index, monkeypatch):
    path, target = index
    runs: list[int] = []
    _add_steps(monkeypatch, target, lambda conn: runs.append(1))
    conn = _open(path)

    assert migrations.apply_migrations(conn) == target
    assert migrations.apply_migrations(conn) == target
    assert migrations._apply_step(conn, target, migrations.MIGRATIONS[target]) == target

    assert runs == [1]
    conn.close()


def test_an_open_transaction_of_the_caller_is_committed_before_the_step_not_rolled_back_with_it(index, monkeypatch):
    path, target = index

    def bad(conn):
        raise sqlite3.OperationalError("boom")

    _add_steps(monkeypatch, target, bad)
    conn = _open(path)
    conn.execute("BEGIN")
    conn.execute("CREATE TABLE callers_own (x INTEGER)")

    with pytest.raises(AppErrorException):
        migrations._apply_step(conn, target, bad)

    assert _has_table(conn, "callers_own")
    assert migrations.read_version(conn) == target - 1
    conn.close()


# ---------------------------------------------------------------- steps with their own transaction

def test_an_own_transaction_step_moves_the_version_once_and_is_not_run_again(index, monkeypatch):
    path, target = index
    runs: list[int] = []

    def own(conn):
        runs.append(1)
        conn.executescript("BEGIN; CREATE TABLE own_tx (x INTEGER); COMMIT;")

    _add_steps(monkeypatch, target, own)
    monkeypatch.setattr(migrations, "_OWN_TRANSACTION", migrations._OWN_TRANSACTION | {target})
    conn = _open(path)

    assert migrations.apply_migrations(conn) == target
    assert migrations._apply_step(conn, target, own) == target

    assert runs == [1]
    assert _has_table(conn, "own_tx")
    assert not conn.in_transaction
    conn.close()


def test_a_failing_own_transaction_step_leaves_the_version_unchanged(index, monkeypatch):
    path, target = index

    def own_fails(conn):
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("CREATE TABLE own_half (x INTEGER)")
        raise sqlite3.IntegrityError("constraint failed")

    _add_steps(monkeypatch, target, own_fails)
    monkeypatch.setattr(migrations, "_OWN_TRANSACTION", migrations._OWN_TRANSACTION | {target})
    conn = _open(path)

    with pytest.raises(AppErrorException) as caught:
        migrations.apply_migrations(conn)

    assert caught.value.error.code == "ERR_MIGRATION_FAILED"
    assert migrations.read_version(conn) == target - 1
    assert not _has_table(conn, "own_half"), "its open transaction is rolled back"
    assert not conn.in_transaction
    conn.close()


# ---------------------------------------------------------------- the rest of the contract

def test_an_index_from_a_newer_build_is_refused_and_left_alone(index):
    path, newer = index
    conn = _open(path)
    conn.execute("UPDATE schema_version SET version = ? WHERE id = 1", (newer,))

    with pytest.raises(AppErrorException) as caught:
        migrations.apply_migrations(conn)

    assert caught.value.error.code == "ERR_CONFIG_INVALID"
    assert migrations.read_version(conn) == newer
    conn.close()


def test_a_gap_in_the_steps_is_reported_not_skipped(index, monkeypatch):
    path, target = index
    monkeypatch.setattr(migrations, "CURRENT_VERSION", target)          # no step registered for it
    conn = _open(path)

    with pytest.raises(AppErrorException) as caught:
        migrations.apply_migrations(conn)

    assert caught.value.error.code == "ERR_CONFIG_INVALID"
    assert migrations.read_version(conn) == target - 1
    conn.close()


def test_roll_back_ends_an_open_transaction_and_never_raises(tmp_path):
    conn = _open(tmp_path / "x.db")
    conn.execute("CREATE TABLE t (x INTEGER)")
    conn.execute("BEGIN")
    conn.execute("INSERT INTO t VALUES (1)")

    migrations._roll_back(conn)

    assert not conn.in_transaction
    assert conn.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 0
    migrations._roll_back(conn)                    # nothing open: nothing happens
    conn.close()
    migrations._roll_back(conn)                    # closed: still quiet


def test_a_failure_whose_rollback_also_fails_reports_the_first_error(index, monkeypatch):
    path, target = index

    def closes_then_fails(conn):
        conn.close()
        raise sqlite3.OperationalError("the real cause")

    _add_steps(monkeypatch, target, closes_then_fails)
    conn = _open(path)

    with pytest.raises(AppErrorException) as caught:
        migrations.apply_migrations(conn)

    assert caught.value.error.code == "ERR_MIGRATION_FAILED"
    assert "the real cause" in (caught.value.error.details or "")
