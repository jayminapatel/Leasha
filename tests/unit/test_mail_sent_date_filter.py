r"""`after:`/`before:` read a message's **sent** date, not its container's.

Layer: L1 (filters, migration) and L3 (the pipeline write), end to end.

**The bug this file exists for.** The owner typed "mail from 2017" and found no
2017 mail. `_date_clause` compares `taken_at_ns` when a row has one and
`mtime_ns` otherwise - and every message read out of a `.pst` is written with
the *archive's* `mtime_ns`, because a message has no file of its own. An
archive opened last week makes ten thousand letters from 2009-2019 "last week",
so no date filter could ever find them. `messages.sent_at` held the right
answer the whole time; only the Mail tab read it.

The fix writes the sent date into `files.taken_at_ns` - "the date a file is
from", which is exactly what the date filter already reads - at index time,
and a migration (v27) backfills every message indexed before it. These tests
prove both halves against the real `Pipeline`, `SqliteStore` and
`file_filter_sql`, and that nothing else reading `taken_at_ns` starts calling
a letter a photograph.
"""

from __future__ import annotations

import datetime
import sqlite3
import time
from pathlib import Path

import pytest

from app.extract import base
from app.extract.base import Document
from app.index.pipeline import Pipeline, PipelineConfig
from app.index.walker import WalkConfig
from app.search.keyword import search as keyword_search
from app.search.query import parse_query
from app.storage.migrations import CURRENT_VERSION, MIGRATIONS, apply_migrations
from app.storage.sqlite_store import SqliteStore
from tests.unit.test_index_freshness import NullVectors, fake_embedder, write_aged

NS = 1_000_000_000
#: The two dates the bug is about: a letter sent in 2017, inside an archive
#: modified an hour ago.
SENT_2017 = int(datetime.datetime(2017, 6, 15, 10, 30).timestamp())
SENT_2012 = int(datetime.datetime(2012, 3, 2, 9, 0).timestamp())


class OldLetters:
    """A `.pst` stand-in: two messages with old sent dates, one without."""

    name = "old-letters"
    extensions = (".pst",)
    reads_externally = True

    def extract(self, path: Path):
        for entry, sent in (("E2017", SENT_2017), ("E2012", SENT_2012), ("ENONE", None)):
            meta = {"entry_id": entry, "subject": f"Letter {entry}",
                    "sender": "john.smith@acme.com", "recipients": '["me@acme.com"]'}
            if sent is not None:
                meta["sent_at"] = sent
            yield Document(path=path, text=f"Letter {entry} about the boiler service.",
                           source_kind="pst_message",
                           virtual_path=f"pst://{path.name}/{entry}", meta=meta)


@pytest.fixture(autouse=True)
def _register():
    before = dict(base.REGISTRY)
    # The real `.pst` reader needs Outlook or libpff; this one stands in for
    # it under the real extension, so `type:mail` (msg, eml, pst) applies.
    base.REGISTRY.pop(".pst", None)
    base.register(OldLetters())
    yield
    base.REGISTRY.clear()
    base.REGISTRY.update(before)


@pytest.fixture()
def indexed(tmp_path: Path):
    root = tmp_path / "docs"
    root.mkdir()
    # Modified an hour ago: nine years after the newest letter in it.
    write_aged(root / "archive.pst", b"x" * 4096)
    store = SqliteStore(tmp_path / "index.db").connect()
    config = PipelineConfig(walk=WalkConfig(roots=[root]), workers=1)
    Pipeline(store, NullVectors(), fake_embedder(), config).run()
    yield store
    store.close()


def _subjects(store, query: str) -> set:
    hits = keyword_search(store, parse_query(query), limit=50)
    ids = [int(hit["file_id"]) for hit in hits]
    if not ids:
        return set()
    marks = ",".join("?" * len(ids))
    return {row[0] for row in store.conn.execute(
        f"SELECT subject FROM messages WHERE file_id IN ({marks})", ids)}


def test_a_message_is_found_by_the_year_it_was_sent(indexed):
    """**The owner's report, as a test.** Fails on the code before this fix:
    the 2017 letter carried the archive's mtime and matched no 2017 range."""
    assert _subjects(indexed, "type:mail after:2017-01-01 before:2017-12-31") == {
        "Letter E2017"}


def test_a_message_is_not_found_outside_the_year_it_was_sent(indexed):
    """The other half: the archive's own (recent) date must no longer place
    the letters. Before the fix every message matched "this year"."""
    this_year = datetime.date.today().year
    assert "Letter E2017" not in _subjects(indexed, f"type:mail after:{this_year}-01-01")
    assert _subjects(indexed, "type:mail after:2012-01-01 before:2012-12-31") == {
        "Letter E2012"}
    assert _subjects(indexed, "type:mail before:2010-12-31") == set()


def test_the_same_filter_with_words_still_uses_the_sent_date(indexed):
    """The BM25 path composes the same fragment as the filter-only browse."""
    assert _subjects(indexed, "boiler after:2017-01-01 before:2017-12-31") == {
        "Letter E2017"}


def test_a_message_with_no_sent_date_keeps_the_container_date(indexed):
    """No sent date is no date to prefer: the row falls back to `mtime_ns`
    exactly as before, rather than to 1970."""
    row = indexed.conn.execute(
        "SELECT f.taken_at_ns, f.taken_at_is_hint FROM files f "
        "JOIN messages m ON m.file_id = f.id WHERE m.subject = 'Letter ENONE'").fetchone()
    assert row[0] is None and row[1] == 0


def test_the_sent_date_is_written_as_a_fact_not_a_guess(indexed):
    """`taken_at_is_hint = 0`: a sent date is a fact the mail client stamped,
    so `apply_batch_era` (which only overrides guesses) can never replace it."""
    row = indexed.conn.execute(
        "SELECT f.taken_at_ns, f.taken_at_is_hint FROM files f "
        "JOIN messages m ON m.file_id = f.id WHERE m.subject = 'Letter E2017'").fetchone()
    assert row[0] == SENT_2017 * NS and row[1] == 0


def test_the_timeline_still_places_mail_by_its_own_branch_only(indexed):
    """**Nothing else calls a letter a photograph.** The timeline's camera-date
    branch reads `taken_at_ns` but only for `source_kind = 'file'`, and mail
    has its own branch on `messages.sent_at` - so a letter is counted once, as
    sent, never as a camera date."""
    from app.reports.timeline import timeline_overview

    overview = timeline_overview(indexed, "everything")
    assert overview.by_camera == 0
    assert overview.by_sent_date == 2


# ---------------------------------------------------------------------------
# The backfill: v27 for every message indexed before this fix
# ---------------------------------------------------------------------------

def _v26_database(tmp_path: Path) -> sqlite3.Connection:
    """A database at v26 holding what an index built before the fix holds."""
    conn = sqlite3.connect(tmp_path / "old.db")
    conn.row_factory = sqlite3.Row
    apply_migrations(conn)
    recent = int(time.time()) * NS
    rows = [
        # id, path, ext, source_kind, taken_at_ns, hint
        (1, "C:/Mail/a.pst#E1", "pst", "pst_message", None, 0),    # the bug
        (2, "C:/Mail/b.eml", "eml", "eml", None, 0),                # a loose .eml
        (3, "C:/Mail/a.pst#E3", "pst", "pst_message", None, 0),     # no sent date
        (4, "C:/Pics/p.jpg", "jpg", "file", 1_150_000_000 * NS, 0),  # a real photo
        (5, "C:/Mail/1999/a.pst#E5", "pst", "pst_message", 915_148_800 * NS, 1),  # era guess
        (6, "C:/Docs/r.pdf", "pdf", "file", None, 0),
    ]
    for fid, path, ext, kind, taken, hint in rows:
        conn.execute(
            "INSERT INTO files (id, path, parent_dir, ext, size_bytes, mtime_ns, status, "
            "source_kind, taken_at_ns, taken_at_is_hint) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (fid, path, "C:/x", ext, 1, recent, "INDEXED", kind, taken, hint))
    for fid, sent in ((1, SENT_2017), (2, SENT_2012), (3, None), (5, SENT_2012)):
        conn.execute("INSERT INTO messages (file_id, subject, sent_at, has_attach) "
                     "VALUES (?,?,?,0)", (fid, f"m{fid}", sent))
    conn.execute("UPDATE schema_version SET version = 26 WHERE id = 1")
    conn.commit()
    return conn


def test_the_migration_backfills_the_sent_date(tmp_path):
    conn = _v26_database(tmp_path)
    assert apply_migrations(conn) == CURRENT_VERSION
    taken = {row[0]: (row[1], row[2]) for row in conn.execute(
        "SELECT id, taken_at_ns, taken_at_is_hint FROM files")}
    assert taken[1] == (SENT_2017 * NS, 0)
    assert taken[2] == (SENT_2012 * NS, 0)
    assert taken[3] == (None, 0)                      # nothing to backfill from
    assert taken[4] == (1_150_000_000 * NS, 0)        # the photograph is untouched
    assert taken[5] == (SENT_2012 * NS, 0)            # a fact beats a folder guess
    assert taken[6] == (None, 0)                      # a document is untouched


def test_the_backfill_makes_old_indexes_findable_without_a_reindex(tmp_path):
    from app.storage.filters import file_filter_sql

    conn = _v26_database(tmp_path)
    where, params = file_filter_sql(parse_query("type:mail after:2017-01-01 before:2017-12-31"))
    sql = f"SELECT f.id FROM files f WHERE 1=1{where}"
    assert [row[0] for row in conn.execute(sql, params)] == []     # the bug, at v26
    apply_migrations(conn)
    assert [row[0] for row in conn.execute(sql, params)] == [1]


def test_the_backfill_is_safe_to_run_twice(tmp_path):
    conn = _v26_database(tmp_path)
    apply_migrations(conn)
    before = conn.execute("SELECT id, taken_at_ns FROM files ORDER BY id").fetchall()
    MIGRATIONS[27](conn)
    assert conn.execute("SELECT id, taken_at_ns FROM files ORDER BY id").fetchall() == before


def test_an_absurd_sent_date_is_not_backfilled(tmp_path):
    """A corrupt `SentOn` far past 2262 would overflow nanoseconds into a
    REAL; it is left out rather than stored as a date that sorts wrongly."""
    conn = _v26_database(tmp_path)
    conn.execute("UPDATE messages SET sent_at = ? WHERE file_id = 1", (10 ** 12,))
    conn.commit()
    apply_migrations(conn)
    assert conn.execute("SELECT taken_at_ns FROM files WHERE id = 1").fetchone()[0] is None


def test_the_year_filter_stays_on_the_indexes(tmp_path):
    """**The filter is still an index seek, not a scan.** Both edges of a
    range go into each branch, so each branch is one bounded range on its own
    index - see `_range_clause`'s measurements."""
    from app.storage.filters import file_filter_sql

    conn = _v26_database(tmp_path)
    apply_migrations(conn)
    where, params = file_filter_sql(parse_query("type:mail after:2017-01-01 before:2017-12-31"))
    plan = " | ".join(row[3] for row in conn.execute(
        f"EXPLAIN QUERY PLAN SELECT f.id FROM files f WHERE 1=1{where}", params))
    assert "SCAN f" not in plan, plan
    assert "idx_files_taken_at" in plan or "idx_files_ext" in plan, plan
