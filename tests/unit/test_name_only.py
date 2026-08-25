r"""Every file in an indexed folder is findable by name.

Layer: L1

From `docs/WORKORDER-zip-archives.md` §1a, which the order calls *"the larger
half"*:

> *the files search should include all files, not just the ones we have read the
> content of - all files in the search folder.*

A `.zip`, `.mp4` or `.exe` produced **no row at all** - not indexed by name, not
in the skip ledger, nothing anywhere recording that it had been passed over.
*"Invisible is the worst answer"*: somebody who knows the file is there concludes
the index is broken, and they are not wrong.

**The dangerous half of this change is the migration, not the feature.**
`files.status` carries a CHECK constraint, so admitting a new value means
rebuilding the table - and `chunks`, `messages` and `entity_mentions` reference
`files(id)` with `ON DELETE CASCADE` while this store runs with
`PRAGMA foreign_keys = ON`. A `DROP TABLE files` with them enabled deletes every
chunk in the index. Most of this file is about proving that does not happen.
"""

from __future__ import annotations

import math
import sqlite3

import pytest

from app.index.embedder import Embedder, l2_normalise
from app.index.pipeline import Pipeline, PipelineConfig
from app.index.walker import WalkConfig, walk
from app.storage.migrations import _status_allows, _v10_name_only_status
from app.storage.sqlite_store import FileStatus, SqliteStore


class NullVectors:
    """The vector store is not what these tests are about."""

    def delete_by_file_ids(self, file_ids):
        pass

    def add(self, **kwargs):
        return len(kwargs.get("chunk_ids") or ())

    def __getattr__(self, name):
        return lambda *args, **kwargs: None


def _tiny_embedder(dim: int = 8) -> Embedder:
    def encode(texts):
        return [l2_normalise([math.sin(abs(hash(t)) % 100 + i) for i in range(dim)])
                for t in texts]

    return Embedder(dim=dim, encoder=encode)

#: `files` exactly as v9 declared it - the CHECK without `NAME_ONLY`.
OLD_FILES = """
CREATE TABLE files_old (
    id INTEGER PRIMARY KEY, path TEXT NOT NULL UNIQUE, parent_dir TEXT NOT NULL,
    ext TEXT NOT NULL, size_bytes INTEGER NOT NULL, mtime_ns INTEGER NOT NULL,
    content_hash TEXT, status TEXT NOT NULL, skip_code TEXT, skip_detail TEXT,
    indexed_at INTEGER, source_kind TEXT NOT NULL,
    repo_id INTEGER REFERENCES repos(id) ON DELETE SET NULL,
    CHECK (status IN ('PENDING','INDEXED','SKIPPED','FAILED')));
INSERT INTO files_old SELECT id,path,parent_dir,ext,size_bytes,mtime_ns,
    content_hash,status,skip_code,skip_detail,indexed_at,source_kind,repo_id
    FROM files;
DROP TABLE files;
ALTER TABLE files_old RENAME TO files;
"""


def _populated(tmp_path):
    """A database with content in every table the rebuild could destroy."""
    path = tmp_path / "index.db"
    with SqliteStore(path) as store:
        file_id = store.upsert_file(r"D:\a\notes.txt", size_bytes=10,
                                    mtime_ns=1, ext="txt")
        store.replace_chunks(file_id, [{
            "ordinal": 0, "text": "Barnsley Dairy", "page": None,
            "char_start": 0, "char_end": 14}])
        store.mark_indexed(file_id)

        message = store.upsert_file("pst://a.pst/E1", size_bytes=5, mtime_ns=1,
                                    source_kind="pst_message")
        store.set_message(message, subject="Hi", sender="a@b.c", sent_at=1)

        repo = store.upsert_repo(r"D:\Repo\x", kind="work")
        store.upsert_file(r"D:\Repo\x\a.cs", size_bytes=1, mtime_ns=1,
                          ext="cs", repo_id=repo)
    return path


def _rolled_back(path) -> sqlite3.Connection:
    """Reopen `path` with `files` returned to its v9 shape."""
    conn = sqlite3.connect(path)
    conn.isolation_level = None
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.executescript(OLD_FILES)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _counts(conn) -> dict:
    return {name: conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
            for name in ("files", "chunks", "messages", "files_fts", "repos")}


# --- the migration ----------------------------------------------------------

def test_the_rebuild_keeps_every_chunk(tmp_path):
    r"""**The one that matters.**

    `chunks`, `messages` and `entity_mentions` cascade from `files(id)`. If the
    rebuild runs with foreign keys enabled, `DROP TABLE files` takes the entire
    index with it - every chunk, every vector's row, every message - and the
    run afterwards would report a perfectly healthy empty database.
    """
    path = _populated(tmp_path)
    conn = _rolled_back(path)
    before = _counts(conn)
    assert not _status_allows(conn, "NAME_ONLY"), "the rollback did not take"

    _v10_name_only_status(conn)

    assert _counts(conn) == before


def test_the_rebuild_admits_the_new_status(tmp_path):
    conn = _rolled_back(_populated(tmp_path))
    _v10_name_only_status(conn)

    conn.execute(
        "INSERT INTO files (path, parent_dir, ext, size_bytes, mtime_ns, "
        "status, source_kind) VALUES ('D:/m.mp4','D:/','mp4',9,1,'NAME_ONLY','file')")

    assert _status_allows(conn, "NAME_ONLY")


def test_foreign_keys_are_on_again_afterwards(tmp_path):
    """Leaving them off would silently disable every cascade for the life of
    the connection - a worse state than a failed migration, and invisible."""
    conn = _rolled_back(_populated(tmp_path))

    _v10_name_only_status(conn)

    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_the_rows_keep_their_identity_and_their_repository(tmp_path):
    """`id` is copied rather than regenerated, which is the only reason every
    existing reference stays valid."""
    conn = _rolled_back(_populated(tmp_path))
    before = conn.execute("SELECT id, path, repo_id FROM files ORDER BY id").fetchall()

    _v10_name_only_status(conn)

    assert conn.execute(
        "SELECT id, path, repo_id FROM files ORDER BY id").fetchall() == before
    joined = conn.execute(
        "SELECT f.path FROM chunks c JOIN files f ON f.id = c.file_id").fetchone()
    assert joined and joined[0].endswith("notes.txt")


def test_the_indexes_come_back(tmp_path):
    """A rebuild drops them with the table. Losing `idx_files_ext` would make
    the Code tab's type filter a full scan, silently."""
    conn = _rolled_back(_populated(tmp_path))

    _v10_name_only_status(conn)

    names = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='files'")}
    assert {"idx_files_status", "idx_files_dir", "idx_files_ext",
            "idx_files_skip"} <= names


def test_running_it_twice_is_harmless(tmp_path):
    """A migration has to survive being re-run - and on a fresh database the
    schema already permits the value, so this is the common path."""
    conn = _rolled_back(_populated(tmp_path))
    _v10_name_only_status(conn)
    before = _counts(conn)

    _v10_name_only_status(conn)

    assert _counts(conn) == before


def test_a_fresh_database_needs_no_rebuild(tmp_path):
    """`schema.sql` already declares the value, so the migration returns at
    once rather than copying a table for nothing."""
    with SqliteStore(tmp_path / "fresh.db") as store:
        assert _status_allows(store.conn, "NAME_ONLY")


# --- the status itself ------------------------------------------------------

def test_name_only_is_not_indexed_and_not_a_skip():
    r"""**A row claiming INDEXED with no chunks is the bug that made `--force`
    necessary** - every later run reports it unchanged, the totals look healthy,
    and its content is not searchable. Doing that deliberately for millions of
    rows would be worse. It is not `SKIPPED` either: nothing went wrong.
    """
    assert FileStatus.NAME_ONLY not in (
        FileStatus.INDEXED, FileStatus.SKIPPED, FileStatus.FAILED)
    assert FileStatus.NAME_ONLY in FileStatus.ALL


def test_a_name_only_file_is_findable_by_name(tmp_path):
    """The whole point. `upsert_file` already feeds `files_fts` for anything
    whose `source_kind` is `file`, so this costs nothing extra."""
    with SqliteStore(tmp_path / "index.db") as store:
        store.upsert_file(r"D:\media\holiday.mp4", size_bytes=99, mtime_ns=1,
                          ext="mp4", status=FileStatus.NAME_ONLY)

        found = store.search_files_by_name("holiday")

    assert [row["path"] for row in found] == [r"D:\media\holiday.mp4"]


def test_a_name_only_file_has_no_chunks_and_cannot_match_content(tmp_path):
    """It must not appear in content search results - there is nothing to
    match - and it does not need to."""
    with SqliteStore(tmp_path / "index.db") as store:
        store.upsert_file(r"D:\media\holiday.mp4", size_bytes=99, mtime_ns=1,
                          ext="mp4", status=FileStatus.NAME_ONLY)

        assert store.search_bm25("holiday") == []


@pytest.mark.parametrize("status", list(FileStatus.ALL))
def test_every_declared_status_is_actually_writable(status, tmp_path):
    """The CHECK and the constant have to agree. They disagreed for exactly as
    long as it took to run this."""
    with SqliteStore(tmp_path / "index.db") as store:
        store.upsert_file(rf"D:\a\{status}.bin", size_bytes=1, mtime_ns=1,
                          status=status)


# --- the walk ---------------------------------------------------------------
#
# The walk is where a file becomes invisible or does not. Everything below is
# about `Candidate.readable` being the *only* thing that changes: the row is
# written either way.

def _walked(root, **kwargs) -> dict[str, bool]:
    """`{filename: readable}` for one walk. Order is not part of the contract."""
    # `names` is not a knob: whole-filename support comes from the extractor
    # registry, because `Path("Makefile").suffix` is `""` and folding that
    # into the extension set would admit every extensionless file on disk.
    config = WalkConfig(roots=[root], extensions=frozenset({".txt"}), **kwargs)
    return {c.path.name: c.readable for c in walk(config)}


@pytest.fixture()
def mixed(tmp_path):
    """One of each reason a file used to disappear."""
    root = tmp_path / "corpus"
    root.mkdir()
    (root / "notes.txt").write_text("readable text", encoding="utf-8")
    (root / "Makefile").write_text("all:\n\techo hi\n", encoding="utf-8")
    (root / "holiday.mp4").write_bytes(b"\x00" * 64)     # no reader
    (root / "backup.zip").write_bytes(b"PK\x03\x04" + b"\x00" * 60)
    (root / "empty.txt").write_bytes(b"")                # nothing to read
    return root


def test_the_walk_yields_a_file_nothing_can_read(mixed):
    """The whole of §1a in one assertion: `.mp4` and `.zip` are *there*."""
    seen = _walked(mixed)

    assert set(seen) == {"notes.txt", "Makefile", "holiday.mp4",
                         "backup.zip", "empty.txt"}


def test_only_the_ones_with_a_reader_are_marked_readable(mixed):
    """`readable` is what stops the pipeline opening a 40GB disk image. It is
    not the same question as "does a row exist"."""
    seen = _walked(mixed)

    assert seen == {"notes.txt": True, "Makefile": True,
                    "holiday.mp4": False, "backup.zip": False,
                    "empty.txt": False}


def test_an_empty_file_is_a_name_not_a_disappearance(mixed):
    """A zero-byte marker is a real file somebody may go looking for. There is
    simply nothing in it to read."""
    assert _walked(mixed)["empty.txt"] is False


def test_a_file_over_the_ceiling_is_named_but_never_opened(tmp_path):
    """**The reason this is `readable=False` rather than a skip.** Hashing a
    20GB disk image costs minutes and yields nothing; not recording it at all
    costs the user the one thing they wanted, which is to know it is there."""
    root = tmp_path / "big"
    root.mkdir()
    (root / "image.txt").write_bytes(b"x" * 4096)        # has a reader...

    seen = _walked(root, max_file_bytes=1024)            # ...but is over the cap

    assert seen == {"image.txt": False}


def test_switching_it_off_restores_the_old_walk(mixed):
    """The setting exists for a media drive: two million video files in the
    index is a real objection and the answer is a checkbox, not an argument."""
    seen = _walked(mixed, name_only=False)

    assert set(seen) == {"notes.txt", "Makefile"}
    assert all(seen.values())


# --- the pipeline -----------------------------------------------------------

def _pipeline(store, root, **kwargs):
    config = PipelineConfig(
        walk=WalkConfig(roots=[root], extensions=frozenset({".txt"}), **kwargs),
    )
    return Pipeline(store, NullVectors(), _tiny_embedder(), config)


def test_the_pipeline_writes_a_row_for_a_file_it_cannot_read(tmp_path, mixed):
    with SqliteStore(tmp_path / "index.db") as store:
        stats = _pipeline(store, mixed).run()

        # `Path(...).name` does not split a backslash off Windows, and this
        # project has been bitten by that six times. Split on both.
        by_name = {record.path.replace("\\", "/").rsplit("/")[-1]: record.status
                   for record in store.iter_files()}

    assert stats.name_only == 3           # .mp4, .zip and the empty .txt
    assert stats.indexed == 2             # notes.txt and Makefile
    assert by_name["holiday.mp4"] == FileStatus.NAME_ONLY
    assert by_name["notes.txt"] == FileStatus.INDEXED


def test_the_types_are_counted_so_a_corpus_can_be_understood(tmp_path, mixed):
    """*"30% of your corpus is .dwg"* is the sentence this number produces.
    A bare total says a lot went unread and nothing about what to do."""
    with SqliteStore(tmp_path / "index.db") as store:
        stats = _pipeline(store, mixed).run()

    assert stats.name_only_by_ext == {"mp4": 1, "zip": 1, "txt": 1}


def test_a_name_only_row_is_not_reread_on_the_next_run(tmp_path, mixed):
    """`_classify` treats NAME_ONLY as UNCHANGED. Without that, every run
    rewrites every unreadable file forever - which on a media drive is the
    entire run."""
    with SqliteStore(tmp_path / "index.db") as store:
        _pipeline(store, mixed).run()
        again = _pipeline(store, mixed).run()

    assert (again.indexed, again.name_only) == (0, 0)
    assert again.unchanged == 5


def test_a_name_only_row_never_claims_to_have_content(tmp_path, mixed):
    """The bug this status exists to prevent: a row saying INDEXED while
    holding no chunks is indistinguishable from a failed extraction, and it is
    what made `--force` necessary in the first place."""
    with SqliteStore(tmp_path / "index.db") as store:
        _pipeline(store, mixed).run()

        assert store.search_bm25("holiday") == []
        assert [r["path"] for r in store.search_files_by_name("holiday")]


def test_a_file_nothing_can_read_is_never_hashed(tmp_path, mixed, monkeypatch):
    r"""**The promise, stated as a test.** *"Nothing is opened."*

    It was not true for a while, and the reason is worth keeping written down.
    `has_changed` pays for a hash when a file was touched in the last few
    minutes - a deliberate rule, because mtime and size can both match across a
    real edit. A NAME_ONLY row holds no hash to compare the fresh one against,
    so `fresh != None` was true every time, and a 4GB `.mp4` copied in this
    morning got read from end to end on the next pass. Every test above still
    passed: the row was correct, the counts were correct, and the run was
    quietly reading gigabytes it had promised not to touch.

    Files written by a test are always "recent", so this fixture is precisely
    the case that was broken.
    """
    import app.index.walker as walker_module

    hashed: list[str] = []
    real = walker_module.content_hash

    def spy(path, **kwargs):
        hashed.append(path.name)
        return real(path, **kwargs)

    monkeypatch.setattr(walker_module, "content_hash", spy)

    with SqliteStore(tmp_path / "index.db") as store:
        _pipeline(store, mixed).run()
        hashed.clear()
        again = _pipeline(store, mixed).run()

    assert again.name_only == 0                    # nothing rewritten...
    assert "holiday.mp4" not in hashed              # ...and nothing read
    assert "backup.zip" not in hashed


def test_a_name_only_row_is_reexamined_once_its_type_becomes_readable(tmp_path):
    r"""The other half of not hashing: **NAME_ONLY must not mean settled.**

    A file is name-only for reasons that change - the size ceiling gets raised,
    an extractor gets added for its type. Trusting the row the way `INDEXED` is
    trusted would leave it findable by name for ever, with nothing anywhere
    saying why its contents never became searchable.
    """
    root = tmp_path / "corpus"
    root.mkdir()
    (root / "report.txt").write_text("Northern pump station commissioning.",
                                     encoding="utf-8")

    with SqliteStore(tmp_path / "index.db") as store:
        first = _pipeline(store, root, max_file_bytes=8).run()   # over the cap
        assert (first.name_only, first.indexed) == (1, 0)

        second = _pipeline(store, root).run()                    # cap raised
        assert second.indexed == 1

        assert [r["path"] for r in store.search_bm25("commissioning")]
