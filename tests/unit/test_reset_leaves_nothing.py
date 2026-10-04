r"""A reset leaves no derived data, stated as an invariant rather than a list.

Layer: L1

From `docs/WORKORDER-202626081439-reset-leaves-nothing.md`. Raised by the owner
as a rule: *"when an index is reset all data must be reset, i.e. all db with
nothing."*

It did not. `clear_index()` deleted from a hand-written list of tables, and two
were missing from it:

* **`files_fts`** - a *standalone* FTS5 table with no `content=` and no
  triggers, so nothing cascades into it. `delete_file()` already documents
  exactly this and clears it by hand; `clear_index()` did not. It was visible,
  because `count_named_files()` was a bare `COUNT(*)` over that table and feeds
  the Files tab summary - so a reset index still reported the old number.
* **`repos`** - no cascade, no owner. Every repository ever detected survived a
  reset, which is why resetting the index did not clear the wrong attribution
  that `WORKORDER-202626081149-code-tab.md` §2 is about.

**The list is what failed, so the test does not use one.** `files_fts` arrived
in schema v4 and `repos` in v6; both were created, wired into writes, and not
added to the loop. A third table will be added eventually. `sqlite_master` is
asked what tables exist, so the next one is covered without anybody remembering
to come back here.
"""

from __future__ import annotations

import pytest

from app.storage.sqlite_store import FileStatus, SqliteStore

#: The only tables allowed to hold rows after a reset.
#:
#: `schema_version` and `index_generation` are not derived data - they describe
#: the database itself, and losing either would make a reset indistinguishable
#: from a corrupt file. `index_state` is the interesting one: it holds the
#: indexing cursors *and* the user's settings, and only the cursors go. A reset
#: that also forgot which folders to index would be one nobody could recover
#: from without setting the application up again.
KEEPS_ROWS = {"schema_version", "index_generation", "index_state"}

#: FTS5 makes several shadow tables per virtual table - `_data`, `_idx`,
#: `_content`, `_docsize`, `_config`. They are storage, not content, and
#: `_config` is never empty even for an empty index.
SHADOW_SUFFIXES = ("_data", "_idx", "_content", "_docsize", "_config")


def user_tables(store) -> list[str]:
    """Every real table in the database, discovered rather than remembered."""
    rows = store.conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' "
        "AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return sorted(
        str(row["name"]) for row in rows
        if not str(row["name"]).endswith(SHADOW_SUFFIXES)
    )


def row_count(store, table: str) -> int:
    return int(store.conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"])


@pytest.fixture()
def filled(tmp_path):
    """An index with something in every table a reset is supposed to empty."""
    with SqliteStore(tmp_path / "index.db") as store:
        repo_id = store.upsert_repo(r"D:\Repo", name="Repo", kind="work")
        for number in range(5):
            store.upsert_file(
                rf"D:\Repo\file {number}.py", size_bytes=100, mtime_ns=1,
                ext="py", status=FileStatus.INDEXED, source_kind="file",
                repo_id=repo_id)
        store.set_state("ui:roots", r"D:\Repo")
        store.set_state("ui:theme", "dark")
        store.set_state("index:cursor", "12345")
        yield store


# --- the invariant ----------------------------------------------------------

def test_a_reset_leaves_every_table_empty(filled):
    """**A1 and A2 together, and A2 is the point.**

    Asserted by enumerating `sqlite_master` rather than by checking the tables
    somebody thought of. Both misses so far were a table that existed, was
    written to, and was absent from a list - so a test written from the same
    list would have passed while the bug shipped.
    """
    before = user_tables(filled)
    assert "files_fts" in before and "repos" in before, (
        "the fixture is not exercising the two tables that were missed")

    filled.clear_index()

    left = {table: row_count(filled, table)
            for table in user_tables(filled)
            if table not in KEEPS_ROWS and row_count(filled, table)}

    assert left == {}, f"a reset left rows behind: {left}"


def test_the_filename_index_is_cleared(filled):
    """A3. `files_fts` is standalone - no `content=`, no triggers - so nothing
    cascades into it and it has to be cleared by hand."""
    assert row_count(filled, "files_fts") == 5

    filled.clear_index()

    assert row_count(filled, "files_fts") == 0
    assert filled.count_named_files() == 0


def fts_data_rows(store, table: str) -> int:
    return row_count(store, f"{table}_data")


def test_the_filename_index_gives_back_its_storage(filled, tmp_path):
    """Measured 2026-10-04 on the owner's own index after a reset: `files_fts`
    held 0 rows and 6,873,743 bytes across 8 levels - 93% of the file.

    `DELETE FROM` a standalone FTS5 table writes delete markers beside the
    segments they cancel; only a merge removes either, and nothing merged
    `files_fts`. A reset now leaves it the size of a fresh one."""
    with SqliteStore(tmp_path / "fresh.db") as fresh:
        empty = fts_data_rows(fresh, "files_fts")

    filled.clear_index()

    assert fts_data_rows(filled, "files_fts") == empty


def test_the_merge_covers_the_filename_index(tmp_path):
    """`optimize_fts` merged `chunks_fts` alone, so `files_fts` grew a segment
    per write for the life of the index."""
    with SqliteStore(tmp_path / "index.db") as store:
        for number in range(30):
            store.upsert_file(
                rf"D:\Docs\note {number}.txt", size_bytes=1, mtime_ns=1,
                ext="txt", status=FileStatus.INDEXED, source_kind="file")
        before = fts_data_rows(store, "files_fts")

        assert store.optimize_fts() is True

        assert fts_data_rows(store, "files_fts") < before
        assert store.search_files_by_name("note 7", limit=5)


def test_the_count_cannot_outlive_the_list_it_labels(filled):
    """`count_named_files` was a bare `COUNT(*)` over a standalone FTS table,
    so it could report more than the list could ever show. The list joins
    `files`; the count now joins it too, and they answer the same question."""
    assert filled.count_named_files() == 5

    # An orphan, exactly as a missed delete would leave one.
    with filled.write() as conn:
        conn.execute(
            "INSERT INTO files_fts(rowid, name, folder) VALUES (?, ?, ?)",
            (999_999, "ghost.py", r"D:\Gone"))

    assert filled.count_named_files() == 5, "an orphan was counted as a file"


def test_repositories_do_not_survive_a_reset(filled):
    """A6. Every repository ever detected used to survive, which is why
    resetting the index did not clear a wrong attribution."""
    assert row_count(filled, "repos") == 1

    filled.clear_index()

    assert row_count(filled, "repos") == 0
    assert filled.repos_list() == []


def test_the_settings_survive(filled):
    """A4. The other half of the rule, and the reason it is not "delete
    everything": a reset that forgot the indexed folders would be one nobody
    could recover from without setting the application up again."""
    filled.clear_index()

    assert filled.get_state("ui:roots", "") == r"D:\Repo"
    assert filled.get_state("ui:theme", "") == "dark"
    assert filled.get_state("index:cursor", "") == "", "a cursor survived"


def test_the_generation_is_bumped(filled):
    """A5. The search cache is keyed on it. Without the bump a cache built from
    the index that was just destroyed keeps answering - and it would be at its
    most convincing right after a reset, when the results still look right."""
    before = filled.generation

    filled.clear_index()

    assert filled.generation > before


def test_resetting_an_empty_index_is_a_no_op(tmp_path):
    """A7. Pressing it twice must not be a way to break the database."""
    with SqliteStore(tmp_path / "index.db") as store:
        assert store.clear_index() == 0
        assert store.clear_index() == 0
        assert store.count_named_files() == 0
