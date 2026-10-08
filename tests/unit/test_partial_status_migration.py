r"""Work order 202626270114 (0b) section 6d: `files.status` gains `PARTIAL`.

Layer: L1

**The dangerous half of this change is the migration, not the feature** -
`test_name_only.py`'s own opening words, unchanged by six more years of
columns being added to `files` since it was written. `status` carries a
`CHECK` constraint, so admitting a new value means rebuilding the table,
and `chunks`, `messages`, `entity_mentions`, `file_tags`, `faces` and
`face_scans` all reference `files(id)` with `ON DELETE CASCADE` while this
store runs with `PRAGMA foreign_keys = ON` - a `DROP TABLE files` with
them enabled takes every one of them with it. Most of this file is about
proving that does not happen, against a real, fully-migrated v24 database
carrying real data in every column and every cascading table, not a
hand-built fixture that might not match what a real index looks like.
"""

from __future__ import annotations

import sqlite3

import pytest

from app.index.pipeline import Pipeline, PipelineConfig
from app.index.walker import WalkConfig
from app.storage.migrations import _status_allows, _v25_partial_status
from app.storage.sqlite_store import FileStatus, SqliteStore


class NullVectors:
    """The vector store is not what these tests are about."""

    def delete_by_file_ids(self, file_ids):
        pass

    def add(self, **kwargs):
        return len(kwargs.get("chunk_ids") or ())

    def __getattr__(self, name):
        return lambda *args, **kwargs: None


#: `files` exactly as v24 left it - the CHECK without `PARTIAL`, and every
#: column through `place` (v22). Copied from `_v25_partial_status`'s own
#: rebuilt shape, minus the one clause this migration adds.
OLD_FILES = """
CREATE TABLE files_old (
    id                INTEGER PRIMARY KEY,
    path              TEXT    NOT NULL UNIQUE,
    parent_dir        TEXT    NOT NULL,
    ext               TEXT    NOT NULL,
    size_bytes        INTEGER NOT NULL,
    mtime_ns          INTEGER NOT NULL,
    content_hash      TEXT,
    status            TEXT    NOT NULL,
    skip_code         TEXT,
    skip_detail       TEXT,
    indexed_at        INTEGER,
    source_kind       TEXT    NOT NULL,
    repo_id           INTEGER REFERENCES repos(id) ON DELETE SET NULL,
    volume_id         INTEGER,
    relative_path     TEXT,
    phash             TEXT,
    taken_at_ns       INTEGER,
    taken_at_is_hint  INTEGER NOT NULL DEFAULT 0,
    place             TEXT,
    CHECK (status IN ('PENDING','INDEXED','SKIPPED','FAILED','NAME_ONLY')));
INSERT INTO files_old SELECT id, path, parent_dir, ext, size_bytes, mtime_ns,
    content_hash, status, skip_code, skip_detail, indexed_at, source_kind,
    repo_id, volume_id, relative_path, phash, taken_at_ns, taken_at_is_hint,
    place FROM files;
DROP TABLE files;
ALTER TABLE files_old RENAME TO files;
CREATE INDEX IF NOT EXISTS idx_files_status ON files(status);
"""


def _populated(tmp_path):
    """A v24 database with real content in every table the rebuild could
    destroy, and a real value in every column the rebuild must carry
    forward - not the three tables and few columns v10's own fixture
    proved, six years and a dozen columns ago."""
    path = tmp_path / "index.db"
    with SqliteStore(path) as store:
        file_id = store.upsert_file(
            r"D:\a\notes.txt", size_bytes=10, mtime_ns=1, ext="txt",
            content_hash="HASH1", status=FileStatus.INDEXED)
        store.replace_chunks(file_id, [{
            "ordinal": 0, "text": "Barnsley Dairy", "page": None,
            "char_start": 0, "char_end": 14}])
        store.mark_indexed(file_id)
        store.set_file_tags(file_id, ["dairy", "commissioning"])

        message = store.upsert_file("pst://a.pst/E1", size_bytes=5, mtime_ns=1,
                                    source_kind="pst_message")
        store.set_message(message, subject="Hi", sender="a@b.c", sent_at=1)

        repo = store.upsert_repo(r"D:\Repo\x", kind="work")
        store.upsert_file(r"D:\Repo\x\a.cs", size_bytes=1, mtime_ns=1,
                          ext="cs", repo_id=repo)

        volume_id = store.upsert_volume(
            "GUID-1", kind="drive", name="Old WD", status="OFFLINE")
        photo_id = store.upsert_file(
            f"leasha-volume://{volume_id}/photo.jpg", size_bytes=1, mtime_ns=1,
            ext="jpg", volume_id=volume_id, relative_path="photo.jpg",
            taken_at_ns=1_700_000_000_000_000_000, taken_at_is_hint=True,
            place="Leeds")
        store.set_phashes({photo_id: "abc123abc123abc1"})

        pile_id = store.create_pile(name="Daddy")
        face_id = store.add_face(photo_id, (0, 0, 1, 1), b"\x00" * 16)
        store.assign_face(face_id, pile_id)
        store.mark_face_scanned(photo_id)
    return path


def _rolled_back(path) -> sqlite3.Connection:
    """Reopen `path` with `files` returned to its v24 shape."""
    conn = sqlite3.connect(path)
    conn.isolation_level = None
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.executescript(OLD_FILES)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _counts(conn) -> dict:
    return {name: conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
            for name in ("files", "chunks", "messages", "files_fts", "repos",
                         "file_tags", "faces", "face_scans", "piles")}


# --- the migration -----------------------------------------------------------

def test_the_migration_is_registered_and_current():
    from app.storage.migrations import CURRENT_VERSION, MIGRATIONS

    assert 25 in MIGRATIONS
    assert CURRENT_VERSION >= 25


def test_a_fresh_database_allows_partial(tmp_path):
    with SqliteStore(tmp_path / "i.db") as store:
        file_id = store.upsert_file(
            r"D:\a.txt", size_bytes=1, mtime_ns=1, status=FileStatus.PARTIAL)
        assert store.get_file_by_id(file_id).status == FileStatus.PARTIAL


def test_the_rebuild_keeps_every_row_in_every_cascading_table(tmp_path):
    r"""**The one that matters.** If the rebuild runs with foreign keys
    enabled, `DROP TABLE files` takes the entire index with it - every
    chunk, every message, every tag, every face - and the run afterwards
    would report a perfectly healthy empty database."""
    path = _populated(tmp_path)
    conn = _rolled_back(path)
    before = _counts(conn)
    assert not _status_allows(conn, "PARTIAL"), "the rollback did not take"

    _v25_partial_status(conn)

    assert _counts(conn) == before
    conn.close()


def test_the_rebuild_admits_the_new_status(tmp_path):
    conn = _rolled_back(_populated(tmp_path))
    _v25_partial_status(conn)

    conn.execute(
        "INSERT INTO files (path, parent_dir, ext, size_bytes, mtime_ns, "
        "status, source_kind) VALUES ('D:/p.txt','D:/','txt',1,1,'PARTIAL','file')")

    assert _status_allows(conn, "PARTIAL")
    conn.close()


def test_foreign_keys_are_on_again_afterwards(tmp_path):
    conn = _rolled_back(_populated(tmp_path))

    _v25_partial_status(conn)

    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    conn.close()


def test_every_column_survives_with_its_value_intact(tmp_path):
    r"""The rebuild's own column list, checked against real data rather
    than trusted from reading it - a column quietly left off the `INSERT`
    silently nulls itself for every existing row in every real index that
    ever runs this migration."""
    conn = _rolled_back(_populated(tmp_path))

    _v25_partial_status(conn)

    photo = conn.execute(
        "SELECT volume_id, relative_path, phash, taken_at_ns, "
        "taken_at_is_hint, place FROM files WHERE relative_path = 'photo.jpg'"
    ).fetchone()
    assert photo[0] is not None                  # volume_id
    assert photo[1] == "photo.jpg"                # relative_path
    assert photo[2] == "abc123abc123abc1"          # phash
    assert photo[3] == 1_700_000_000_000_000_000   # taken_at_ns
    assert photo[4] == 1                           # taken_at_is_hint
    assert photo[5] == "Leeds"                     # place

    repo_row = conn.execute(
        "SELECT repo_id FROM files WHERE path = 'D:\\Repo\\x\\a.cs'").fetchone()
    assert repo_row[0] is not None
    conn.close()


def test_every_index_is_recreated(tmp_path):
    conn = _rolled_back(_populated(tmp_path))

    _v25_partial_status(conn)

    names = {row[1] for row in conn.execute("PRAGMA index_list(files)")}
    assert names == {
        "idx_files_status", "idx_files_dir", "idx_files_ext", "idx_files_skip",
        "idx_files_volume", "idx_files_volume_relpath", "idx_files_source_kind",
        "idx_files_repo", "idx_files_mtime", "idx_files_phash",
        "idx_files_taken_at", "idx_files_place", "idx_files_content_hash",
        "sqlite_autoindex_files_1",
    }
    conn.close()


def test_a_deleted_file_still_cascades_after_the_rebuild(tmp_path):
    r"""The rebuild must not merely preserve the rows - it must preserve
    the `ON DELETE CASCADE` behaviour itself, or a file removed from a
    rebuilt database silently leaves its chunks and faces behind forever."""
    path = _populated(tmp_path)
    conn = _rolled_back(path)
    _v25_partial_status(conn)

    file_id = conn.execute(
        "SELECT id FROM files WHERE path = 'D:\\a\\notes.txt'").fetchone()[0]
    conn.execute("DELETE FROM files WHERE id = ?", (file_id,))

    remaining = conn.execute(
        "SELECT COUNT(*) FROM chunks WHERE file_id = ?", (file_id,)).fetchone()[0]
    assert remaining == 0, "ON DELETE CASCADE did not survive the rebuild"
    conn.close()


def test_re_running_the_migration_is_a_no_op(tmp_path):
    conn = _rolled_back(_populated(tmp_path))
    _v25_partial_status(conn)
    _v25_partial_status(conn)                       # already current - no-op

    columns = [row[1] for row in conn.execute("PRAGMA table_info(files)")]
    assert columns.count("status") == 1
    conn.close()


# --- the pipeline wiring ------------------------------------------------------

def _tiny_embedder():
    from app.index.embedder import Embedder, l2_normalise
    import math

    def encode(texts):
        return [l2_normalise([math.sin(abs(hash(t)) % 100 + i) for i in range(8)])
                for t in texts]

    return Embedder(dim=8, encoder=encode)


def test_a_file_is_partial_the_instant_its_chunks_land_unembedded(tmp_path):
    r"""§6d's whole point: between the chunks being written and
    `_embed_pending` catching up, the file is `PARTIAL`, not `PENDING` -
    it already has real, keyword-searchable content, and `PENDING` would
    say otherwise.

    `_embed_pending` is monkeypatched to a no-op so the write half is
    observed in isolation, the same technique `test_switch_off_means_no_
    face_rows_exist` already uses to isolate one stage of a real pipeline
    from the rest of it.
    """
    corpus = tmp_path / "docs"
    corpus.mkdir()
    (corpus / "a.txt").write_text("Barnsley Dairy commissioning notes.",
                                  encoding="utf-8")

    with SqliteStore(tmp_path / "index.db") as store:
        pipeline = Pipeline(
            store, NullVectors(), _tiny_embedder(),
            PipelineConfig(walk=WalkConfig(roots=[corpus]), workers=1,
                           min_free_gb=0, required_free_gb=0),
        )
        pipeline._embed_pending = lambda pending: None       # never promotes

        pipeline.run()

        record = store.get_file(str(corpus / "a.txt"))
        assert record is not None
        assert record.status == FileStatus.PARTIAL
        assert store.chunks_for_file(record.id), (
            "PARTIAL with no chunks would be a worse lie than PENDING")


def test_keyword_search_finds_a_partial_file(tmp_path):
    r"""§6d's approved semantics: "keyword search treats it exactly like
    INDEXED." Proved directly against `chunks_fts`, the same table every
    keyword query actually reads - `files.status` was never part of that
    query, which is *why* this was safe to build (see the order's own
    2026-09-16 note)."""
    with SqliteStore(tmp_path / "index.db") as store:
        file_id = store.upsert_file(
            r"D:\a.txt", size_bytes=1, mtime_ns=1, status=FileStatus.PENDING)
        store.replace_chunks(file_id, [{
            "ordinal": 0, "text": "Barnsley Dairy commissioning", "page": None,
            "char_start": 0, "char_end": 28}])
        store.upsert_file(
            r"D:\a.txt", size_bytes=1, mtime_ns=1, status=FileStatus.PARTIAL)

        hits = store.conn.execute(
            "SELECT c.file_id FROM chunks_fts f JOIN chunks c ON c.id = f.rowid "
            "WHERE chunks_fts MATCH 'Barnsley'"
        ).fetchall()

    assert {row[0] for row in hits} == {file_id}


def test_partial_promotes_to_indexed_once_embedded(tmp_path):
    """The M6 repair's own promotion path, unaffected by this status
    existing - `_drain_unembedded` -> `_embed_pending` -> `mark_indexed_
    many` runs exactly as it always has, and now finds a `PARTIAL` file
    instead of a `PENDING` one on the way in."""
    with SqliteStore(tmp_path / "index.db") as store:
        file_id = store.upsert_file(
            r"D:\a.txt", size_bytes=1, mtime_ns=1, status=FileStatus.PARTIAL)
        store.replace_chunks(file_id, [{
            "ordinal": 0, "text": "Barnsley Dairy", "page": None,
            "char_start": 0, "char_end": 14}])

        # Dated note, 2026-10-08: with "Make text searchable first" on, the
        # repair parks these for the run's model thread instead of embedding
        # them here, so this direct call is the classic mode's. The text-first
        # promotion is `test_text_first.py`'s stop-and-resume test.
        pipeline = Pipeline(
            store, NullVectors(), _tiny_embedder(),
            PipelineConfig(walk=WalkConfig(roots=[]), workers=1, two_phase=False),
        )
        from app.index.pipeline import IndexStats

        pipeline._drain_unembedded(IndexStats())

        assert store.get_file_by_id(file_id).status == FileStatus.INDEXED


def test_a_file_with_nothing_extracted_stays_pending_not_partial(tmp_path):
    r"""`PARTIAL` means "chunks exist"; a file `_write_one` never gave any
    text to (an image with no OCR text, say, before CLIP's own separate
    embed runs) has nothing to be partial about yet."""
    corpus = tmp_path / "docs"
    corpus.mkdir()
    (corpus / "empty.txt").write_bytes(b"")

    with SqliteStore(tmp_path / "index.db") as store:
        pipeline = Pipeline(
            store, NullVectors(), _tiny_embedder(),
            PipelineConfig(walk=WalkConfig(roots=[corpus]), workers=1,
                           min_free_gb=0, required_free_gb=0),
        )
        pipeline.run()

        record = store.get_file(str(corpus / "empty.txt"))
        assert record is not None
        assert record.status != FileStatus.PARTIAL
