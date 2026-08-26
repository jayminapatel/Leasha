"""Section 5 of the 2026-08-26 review: core, extract, CLI and storage.

Nine small faults, each of which is invisible until the one situation that
provokes it. Every test here asserts the *outcome* the owner would notice,
not the mechanism that was changed to produce it.

One item in that section is deliberately **not** implemented, and this file is
where that is recorded: see `test_a_fresh_database_is_migrated_not_assumed_complete`.
"""

from __future__ import annotations

import sqlite3
import sys
import zipfile
from pathlib import Path

import pytest

from app.core.config import SETTING_KEYS, load_settings
from app.core.errors import AppErrorException
from app.storage import migrations as migrations_module
from app.storage.sqlite_store import SqliteStore

# --- M14: an environment variable must win whether or not .env mentions it ---


def test_an_env_var_overrides_a_setting_the_env_file_never_mentions(
    temp_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole point of an environment variable is to override without editing.

    Only keys already written in .env used to be overridden, so
    `set INDEX_WORKERS=6` did nothing unless INDEX_WORKERS was already there -
    which is exactly the case where somebody reaches for one.
    """
    text = temp_env.read_text(encoding="utf-8")
    assert "INDEX_WORKERS" not in text, "the fixture must not already set this"

    monkeypatch.setenv("INDEX_WORKERS", "6")
    assert load_settings(temp_env).index_workers == 6


def test_the_env_file_still_wins_over_nothing_at_all(temp_env: Path) -> None:
    """The override must not invent values for keys nobody set."""
    settings = load_settings(temp_env)
    assert settings.index_workers == 0          # the documented default


def test_every_key_load_settings_reads_is_in_setting_keys() -> None:
    """The canonical list is what makes the override complete; drift is silent.

    A key added to `load_settings` and forgotten here is a setting that cannot
    be overridden, with nothing to announce it - so the list is checked against
    the function rather than trusted.
    """
    import ast
    import inspect
    import textwrap

    from app.core import config as config_module

    tree = ast.parse(textwrap.dedent(inspect.getsource(config_module.load_settings)))
    read: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = node.func
        called = (name.attr if isinstance(name, ast.Attribute)
                  else name.id if isinstance(name, ast.Name) else "")
        if called in ("get", "path_of", "_require", "_as_int", "_as_bool"):
            for arg in node.args:
                if (isinstance(arg, ast.Constant) and isinstance(arg.value, str)
                        and arg.value.isupper()):
                    read.add(arg.value)

    missing = read - set(SETTING_KEYS)
    assert not missing, f"not overridable by an environment variable: {sorted(missing)}"


# --- L: a bad number names its key instead of raising ValueError ------------


@pytest.mark.parametrize("key", ["RERANK_TOP_N", "RERANK_WINDOW_CHARS"])
def test_a_bad_rerank_number_names_the_key(
    temp_env: Path, monkeypatch: pytest.MonkeyPatch, key: str
) -> None:
    """`int()` raises ValueError, which reaches the person as a traceback.

    Every other number in this file goes through `_as_int`, which produces an
    ERR_CONFIG_INVALID naming the key and saying what was expected. These two
    were the exceptions.
    """
    monkeypatch.setenv(key, "lots")
    with pytest.raises(AppErrorException) as caught:
        load_settings(temp_env)
    error = caught.value.error
    assert error.code == "ERR_CONFIG_INVALID"
    assert key in (error.message + str(error.details))
    assert error.suggestion


# --- L: a nested archive is not charged for itself and for its contents -----


def _zip_of(path: Path, members: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        for name, blob in members.items():
            archive.writestr(name, blob)
    return path


def test_a_nested_archive_costs_its_contents_and_not_twice_that(
    tmp_path: Path
) -> None:
    """A zip inside a zip used to spend its bytes at both levels.

    With a budget of N and an inner archive of N/2 + a little, the container
    was charged first and the members then found the budget spent - so an
    archive of archives read about half of what the setting promised, and the
    part it refused was reported as too large rather than as a miscount.
    """
    from app.extract import archive as archive_module

    inner_text = b"x" * 4_000
    inner = _zip_of(tmp_path / "inner.zip", {"a.txt": inner_text, "b.txt": inner_text})
    outer = _zip_of(tmp_path / "outer.zip", {"inner.zip": inner.read_bytes()})

    budget = archive_module._Budget(total=9_000)
    documents = list(archive_module.read_archive(outer, budget=budget))

    read = [d for d in documents if d.text]
    assert len(read) == 2, [d.key for d in documents]
    # Both members' bytes, and not the container's on top of them.
    assert budget.left == 9_000 - 2 * len(inner_text)


# --- L: cmd_extract streams rather than holding the corpus in memory --------


def test_extract_json_holds_no_list_of_every_record() -> None:
    """`--chunks --full` kept the text of every chunk until the last file.

    Asserted against the code rather than by measuring memory, because the
    threshold that makes it matter is a corpus nobody has in a test. The list
    is what made it O(corpus); its absence is the fix.
    """
    import ast
    import inspect
    import textwrap

    from app import cli

    for function in (cli.cmd_extract, cli._write_extract_json):
        tree = ast.parse(textwrap.dedent(inspect.getsource(function)))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == "append":
                owner = node.value
                name = getattr(owner, "id", "")
                assert name != "results", (
                    f"{function.__name__} accumulates every record again")


# --- L: clearing the index does not delete FTS rows one at a time -----------


def test_clearing_the_index_empties_the_fts_tables(tmp_path: Path) -> None:
    """Whatever route it takes, an empty index must have an empty FTS table."""
    with SqliteStore(tmp_path / "index.db") as store:
        file_id = store.upsert_file(
            "C:/docs/one.txt", size_bytes=3, mtime_ns=1,
            status="INDEXED", source_kind="file",
        )
        store.replace_chunks(file_id, [
            {"ordinal": 0, "text": "findable words here", "tokens": 3,
             "char_start": 0, "char_end": 19},
        ])
        assert store.conn.execute(
            "SELECT count(*) FROM chunks_fts").fetchone()[0] == 1

        store.clear_index()

        for table in ("chunks_fts", "files_fts"):
            assert store.conn.execute(
                f"SELECT count(*) FROM {table}").fetchone()[0] == 0, table


def test_the_content_triggers_survive_a_reset(tmp_path: Path) -> None:
    """They are dropped for the duration; a reset that lost them would leave a
    database that indexes everything afterwards and finds none of it."""
    with SqliteStore(tmp_path / "index.db") as store:
        before = {row[0] for row in store.conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'trigger'")}
        store.clear_index()
        after = {row[0] for row in store.conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'trigger'")}
        assert before == after

        file_id = store.upsert_file(
            "C:/docs/after.txt", size_bytes=3, mtime_ns=1,
            status="INDEXED", source_kind="file",
        )
        store.replace_chunks(file_id, [
            {"ordinal": 0, "text": "written after the reset", "tokens": 4,
             "char_start": 0, "char_end": 23},
        ])
        found = store.conn.execute(
            "SELECT count(*) FROM chunks_fts WHERE chunks_fts MATCH 'reset'"
        ).fetchone()[0]
        assert found == 1, "the FTS triggers did not come back"


# --- L: two characters plus a filter still means those two characters -------


def _three_files(store: SqliteStore) -> None:
    for name in ("quarterly.pdf", "notes.pdf", "quick.txt"):
        store.upsert_file(
            f"C:/docs/{name}", size_bytes=1, mtime_ns=1,
            status="INDEXED", source_kind="file",
        )


def test_a_two_character_query_with_a_filter_is_not_discarded(
    tmp_path: Path
) -> None:
    """`/type pdf` plus "q" answered with every PDF, newest first.

    The typed letter was dropped, so the list did not change as the person
    typed and read as a search that had stopped working.
    """
    with SqliteStore(tmp_path / "index.db") as store:
        _three_files(store)
        found = [r["path"] for r in store.search_files_by_name("q", ext=["pdf"])]
        assert found == ["C:/docs/quarterly.pdf"]

        assert store.search_files_by_name("z", ext=["pdf"]) == []


def test_an_empty_query_with_a_filter_still_means_everything(
    tmp_path: Path
) -> None:
    """Asked for: the list shows everything and filters as you type."""
    with SqliteStore(tmp_path / "index.db") as store:
        _three_files(store)
        found = {r["path"] for r in store.search_files_by_name("", ext=["pdf"])}
        assert found == {"C:/docs/quarterly.pdf", "C:/docs/notes.pdf"}


def test_a_short_query_without_a_filter_is_still_refused(tmp_path: Path) -> None:
    """One or two characters match nearly everything; a hundred arbitrary rows
    looks like a search that worked."""
    with SqliteStore(tmp_path / "index.db") as store:
        _three_files(store)
        assert store.search_files_by_name("q") == []


def test_a_limit_of_zero_returns_nothing_not_everything(tmp_path: Path) -> None:
    """SQLite reads a negative LIMIT as no limit at all."""
    with SqliteStore(tmp_path / "index.db") as store:
        _three_files(store)
        assert len(store.search_files_by_name("quarterly", limit=0)) == 1
        assert len(store.search_files_by_name("quarterly", limit=-1)) == 1


# --- L: a row never carries somebody else's content hash --------------------


def test_a_fast_pass_clears_the_hash_it_did_not_compute(tmp_path: Path) -> None:
    """`--fast` writes new size and mtime beside the *previous* contents' hash.

    A later verifying run compares against it. The case that is not merely
    wasteful is a file restored to an older version: the stale hash matches,
    the row is declared unchanged, and the index goes on serving text that is
    no longer in the file.
    """
    with SqliteStore(tmp_path / "index.db") as store:
        store.upsert_file(
            "C:/docs/one.txt", size_bytes=10, mtime_ns=1,
            content_hash="hash-of-the-old-contents",
            status="INDEXED", source_kind="file",
        )
        store.upsert_file(
            "C:/docs/one.txt", size_bytes=20, mtime_ns=2,
            content_hash=None, clear_hash=True,
            status="INDEXED", source_kind="file",
        )
        record = store.get_file("C:/docs/one.txt")
        assert record is not None
        assert record.content_hash is None, "a stale hash survived a fast pass"


def test_a_caller_that_simply_has_no_hash_does_not_blank_one(
    tmp_path: Path
) -> None:
    """The COALESCE exists for `_record_skip`, the PST path and every test.

    Clearing must be something a caller asks for, not the default - or the
    flag would have swapped one silent wrong answer for another.
    """
    with SqliteStore(tmp_path / "index.db") as store:
        store.upsert_file(
            "C:/docs/two.txt", size_bytes=10, mtime_ns=1,
            content_hash="a-real-hash", status="INDEXED", source_kind="file",
        )
        store.upsert_file(
            "C:/docs/two.txt", size_bytes=10, mtime_ns=1,
            status="INDEXED", source_kind="file",
        )
        record = store.get_file("C:/docs/two.txt")
        assert record is not None
        assert record.content_hash == "a-real-hash"


# --- the item that was measured and NOT done --------------------------------


def test_a_fresh_database_is_migrated_not_assumed_complete(tmp_path: Path) -> None:
    r"""schema.sql seeds at 4 **on purpose**, and this is the guard on that.

    The review asked for fresh databases to be seeded at `CURRENT_VERSION` so
    they would not replay ten migrations they do not need. Measured before
    doing it: the replay costs 2.9ms against schema.sql's own 11.3ms, and the
    migrations it would skip are not no-ops - v11 builds `chunks_vocab`, v12
    and v13 build `messages_fts` and its triggers, v14 the folded mail columns.
    A database seeded forward would come up with no mail search in it at all,
    and nothing anywhere to say so.

    So the seed stays at 4, and this test fails the day somebody moves it
    forward without completing schema.sql first.
    """
    schema = migrations_module.SCHEMA_FILE.read_text(encoding="utf-8")
    seeded = sqlite3.connect(tmp_path / "seeded.db")
    seeded.executescript(schema)
    assert (seeded.execute("SELECT version FROM schema_version").fetchone()[0]
            == migrations_module.SCHEMA_BASELINE_VERSION)

    before = {row[0] for row in seeded.execute("SELECT name FROM sqlite_master")}
    migrations_module.apply_migrations(seeded)
    after = {row[0] for row in seeded.execute("SELECT name FROM sqlite_master")}
    seeded.close()

    built = after - before
    assert "messages_fts" in built, (
        "schema.sql now builds messages_fts, so the seed may move forward - "
        "but only once every object below is in it too")
    assert "chunks_vocab" in built


def test_the_dead_dummyapp_scaffold_is_gone() -> None:
    """`config/settings.json` held `{"app_name": "DummyApp"}` in UTF-16 and was
    read by nothing. A configuration file nobody reads is a place to change a
    setting and watch nothing happen."""
    from app.core.config import project_root

    assert not (project_root() / "config" / "settings.json").exists()


if __name__ == "__main__":       # pragma: no cover
    sys.exit(pytest.main([__file__, "-q"]))
