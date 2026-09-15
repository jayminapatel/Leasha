r"""Work order 0f §3a: the EXIF date, from the photo's own bytes to `before:`.

**The bug this file exists to prove is fixed.** A photo taken in 2006 and
copied to a new drive in 2019 sorted, filtered and displayed as 2019, because
`files.mtime_ns` was the only date-bearing column in the table and every
`after:`/`before:` comparison was built against it. Commit `c58dca9`
(2026-08-28) diagnosed it precisely and left the acceptance test
`xfail(strict=True)` rather than guess at a fix: `mtime_ns` doubles as H1's
change-detection key, so EXIF could not simply be written into it - a photo
whose stored mtime was its shot date would look changed on every single
rescan, defeating incremental indexing for the whole image corpus.

So the shot date gets its own column, `files.taken_at_ns` (migration v18),
and the file's real mtime stays exactly what it was. Four things are proved
here, in this order:

  * the migration, on a fresh database and on one carried forward from v17;
  * `SqliteStore.upsert_file` storing it, and *not* blanking it for a caller
    that knows nothing about it;
  * `file_filter_sql` preferring it over `mtime_ns` when it is there -
    including the query plan, because a date filter that stopped using an
    index would be a regression measured in hundreds of milliseconds;
  * the whole thing end to end through a **real `Pipeline` run**, for both
    photo cases - one whose OCR found text and one whose did not, which take
    genuinely different write paths (`_write_one` and `_record_skip`).

`test_exif.py` keeps the original acceptance sentence from §4; this file is
the wiring underneath it.
"""

from __future__ import annotations

import datetime
import math
import os
import sqlite3
import time
from pathlib import Path

import pytest

from app.extract import ocr as ocr_module
from app.extract.exif import read_datetime
from app.index.embedder import Embedder, l2_normalise
from app.index.pipeline import Pipeline, PipelineConfig
from app.index.walker import WalkConfig
from app.search.query import parse_query
from app.search.keyword import _filter_only
from app.storage.filters import file_filter_sql, merge_by_date
from app.storage.migrations import CURRENT_VERSION, apply_migrations
from app.storage.sqlite_store import SqliteStore

PIL = pytest.importorskip("PIL", reason="Pillow is not installed")

#: The two dates the whole item is about, kept in one place so no test
#: quietly drifts to a different pair.
SHOT = datetime.datetime(2006, 6, 15, 10, 30, 0)
COPIED = datetime.date(2019, 3, 1)


# ---------------------------------------------------------------------------
# Fixture photos
# ---------------------------------------------------------------------------


def _photo(path: Path, *, taken: datetime.datetime | None = SHOT,
           modified: datetime.datetime | None = None,
           mtime: datetime.date = COPIED) -> Path:
    """A real JPEG whose EXIF date and file mtime deliberately disagree.

    `modified` writes the `DateTime` tag (36867 is `DateTimeOriginal`, 306 is
    `DateTime` - the timestamp photo software rewrites when it saves). A photo
    carrying both is the case §3a names by tag: *DateTimeOriginal* is the
    shot date, and it must win.
    """
    from PIL import Image

    path.parent.mkdir(parents=True, exist_ok=True)
    exif = Image.Exif()
    if taken is not None:
        exif[36867] = taken.strftime("%Y:%m:%d %H:%M:%S")
    if modified is not None:
        exif[306] = modified.strftime("%Y:%m:%d %H:%M:%S")
    # Big enough that the OCR ladder's own size gate does not reject it before
    # anything here gets a chance to run.
    Image.new("RGB", (400, 400), color="white").save(path, exif=exif)

    stamp = time.mktime(mtime.timetuple())
    os.utime(path, (stamp, stamp))
    return path


def _ns(when: datetime.datetime) -> int:
    return int(when.timestamp() * 1_000_000_000)


# ---------------------------------------------------------------------------
# `read_datetime` - which tag actually wins
# ---------------------------------------------------------------------------


def test_datetimeoriginal_beats_the_datetime_tag(tmp_path):
    r"""**The shot date, not the last-saved date.** §3a names
    `DateTimeOriginal` specifically, and the distinction is the whole item:
    tag 306 (`DateTime`) is rewritten by any photo software that re-saves the
    file, so a 2006 photograph opened and saved in 2019 carries a 2019
    `DateTime` beside its unchanged 2006 `DateTimeOriginal`. Preferring 306
    would reintroduce, inside EXIF, exactly the copy-date bug that reading
    EXIF at all exists to escape.
    """
    photo = _photo(tmp_path / "both.jpg", taken=SHOT,
                   modified=datetime.datetime(2019, 3, 1, 9, 0, 0))
    assert read_datetime(photo) == SHOT


def test_the_datetime_tag_is_still_used_when_it_is_all_there_is(tmp_path):
    """A scan or an export with no `DateTimeOriginal` is still better served
    by its EXIF timestamp than by the file's copy date."""
    photo = _photo(tmp_path / "scan.jpg", taken=None,
                   modified=datetime.datetime(2011, 5, 4, 8, 0, 0))
    assert read_datetime(photo) == datetime.datetime(2011, 5, 4, 8, 0, 0)


# ---------------------------------------------------------------------------
# The migration
# ---------------------------------------------------------------------------


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def test_the_migration_is_registered_and_current():
    r"""**2026-09-15 (offline-media merge):** this asserted `CURRENT_VERSION
    == 18` and broke the moment v19 (`_v19_offline_media_volumes`) was added -
    the same trap `test_phash_column_migration.py` records against itself for
    v17, and `test_query_plans.py` for v5. Retargeted the same way: the claim
    is "the shot-date migration is registered and nothing later dropped it",
    which has nothing to do with the literal number. The literal 18 stays as
    a floor because that genuinely is the version this column arrived at.
    """
    from app.storage.migrations import MIGRATIONS

    assert 18 in MIGRATIONS
    assert CURRENT_VERSION >= 18


def test_a_fresh_database_has_the_column(tmp_path):
    with SqliteStore(tmp_path / "index.db") as store:
        assert "taken_at_ns" in _columns(store.conn, "files")
        assert store.schema_version >= 18


def test_a_database_carried_forward_from_v17_gains_the_column(tmp_path):
    """The migration a real, existing index will actually run - a database
    that already has files in it, walked forward one version."""
    conn = sqlite3.connect(tmp_path / "carried.db")
    conn.isolation_level = None
    try:
        apply_migrations(conn)
        conn.execute(
            "INSERT INTO files (path, parent_dir, ext, size_bytes, mtime_ns, "
            "status, source_kind) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (r"C:\Photos\old.jpg", r"C:\Photos", "jpg", 100, 1, "INDEXED", "file"),
        )
        conn.execute("DROP INDEX IF EXISTS idx_files_taken_at")
        conn.execute("ALTER TABLE files DROP COLUMN taken_at_ns")
        conn.execute("UPDATE schema_version SET version = 17 WHERE id = 1")

        apply_migrations(conn)

        assert "taken_at_ns" in _columns(conn, "files")
        row = conn.execute("SELECT taken_at_ns FROM files WHERE path = ?",
                           (r"C:\Photos\old.jpg",)).fetchone()
        # Additive and nullable: the existing row survives untouched, with no
        # shot date, until a later run re-touches that photo.
        assert row is not None and row[0] is None
    finally:
        conn.close()


def test_re_running_the_migration_is_a_no_op(tmp_path):
    conn = sqlite3.connect(tmp_path / "twice.db")
    conn.isolation_level = None
    try:
        apply_migrations(conn)
        apply_migrations(conn)
        columns = [row[1] for row in conn.execute("PRAGMA table_info(files)")]
        assert columns.count("taken_at_ns") == 1
    finally:
        conn.close()


def test_the_partial_index_exists(tmp_path):
    """`WHERE taken_at_ns IS NOT NULL`, the same shape as `idx_files_phash`:
    most rows in a real index are not photographs and never will be, and an
    index entry for a NULL nobody can search for is pure write cost."""
    with SqliteStore(tmp_path / "index.db") as store:
        names = {row[1] for row in store.conn.execute("PRAGMA index_list(files)")}
        assert "idx_files_taken_at" in names


# ---------------------------------------------------------------------------
# `SqliteStore.upsert_file`
# ---------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path):
    with SqliteStore(tmp_path / "index.db") as opened:
        yield opened


def test_upsert_file_stores_the_shot_date(store):
    file_id = store.upsert_file(
        r"C:\Photos\a.jpg", size_bytes=1, mtime_ns=_ns(datetime.datetime(2019, 3, 1)),
        ext="jpg", status="INDEXED", taken_at_ns=_ns(SHOT))

    row = store.get_file_by_id(file_id)
    assert row.taken_at_ns == _ns(SHOT)
    # **The real mtime is untouched.** H1's change detection compares against
    # it, so a photo whose stored mtime became its shot date would look
    # changed on every rescan forever.
    assert row.mtime_ns == _ns(datetime.datetime(2019, 3, 1))


def test_a_caller_that_knows_nothing_about_it_does_not_blank_it(store):
    r"""COALESCEd on conflict, exactly as `repo_id` and `content_hash` are,
    and for the same reason: `_record_skip`, the PST path and every test
    call `upsert_file` without this argument, and none of them should be
    able to erase a shot date the images pass established."""
    path = r"C:\Photos\a.jpg"
    store.upsert_file(path, size_bytes=1, mtime_ns=5, ext="jpg",
                      status="INDEXED", taken_at_ns=_ns(SHOT))
    store.upsert_file(path, size_bytes=2, mtime_ns=6, ext="jpg",
                      status="INDEXED")                       # no taken_at_ns

    assert store.get_file(path).taken_at_ns == _ns(SHOT)


def test_a_file_with_no_shot_date_reads_as_none(store):
    file_id = store.upsert_file(r"C:\Docs\report.pdf", size_bytes=1, mtime_ns=1,
                                ext="pdf", status="INDEXED")
    assert store.get_file_by_id(file_id).taken_at_ns is None


# ---------------------------------------------------------------------------
# `file_filter_sql` - what `before:` and `after:` actually compare
# ---------------------------------------------------------------------------


def _matches(store, query: str) -> list[str]:
    # `file_filter_sql` returns a fragment with a leading ` AND `, to be
    # concatenated after a standing truth - the same `WHERE 1=1{where}` shape
    # `test_query_plans.py` and `_filter_only` both use.
    where, params = file_filter_sql(parse_query(query))
    return [r["path"] for r in
            store.conn.execute(f"SELECT path FROM files f WHERE 1=1{where}", params)]


def test_before_finds_the_photo_by_its_shot_date(store):
    """The acceptance sentence: 2006 photo, 2019 file date, `before:2010`."""
    store.upsert_file(r"C:\Photos\2006.jpg", size_bytes=1, ext="jpg",
                      status="INDEXED", mtime_ns=_ns(datetime.datetime(2019, 3, 1)),
                      taken_at_ns=_ns(SHOT))

    assert _matches(store, "before:2010") == [r"C:\Photos\2006.jpg"]


def test_after_excludes_the_photo_by_its_shot_date(store):
    """The same substitution in the other direction - a filter that only ever
    passes would satisfy the test above while meaning nothing."""
    store.upsert_file(r"C:\Photos\2006.jpg", size_bytes=1, ext="jpg",
                      status="INDEXED", mtime_ns=_ns(datetime.datetime(2019, 3, 1)),
                      taken_at_ns=_ns(SHOT))

    assert _matches(store, "after:2015") == []


def test_a_file_with_no_shot_date_still_filters_by_its_mtime(store):
    """**The fallback, which is every non-photograph in the corpus.** A
    change that made `before:` consider only `taken_at_ns` would silently
    stop matching every document, spreadsheet and email in the index."""
    store.upsert_file(r"C:\Docs\report.pdf", size_bytes=1, ext="pdf",
                      status="INDEXED", mtime_ns=_ns(datetime.datetime(2008, 4, 2)))

    assert _matches(store, "before:2010") == [r"C:\Docs\report.pdf"]
    assert _matches(store, "after:2015") == []


def test_the_date_filter_still_uses_an_index(store):
    r"""**A plan test, because the obvious spelling of this fix is a table
    scan.** `COALESCE(f.taken_at_ns, f.mtime_ns) <= ?` is not sargable -
    measured on a 200,000-row table it scanned in 5.79ms where the indexed
    form took 0.86ms, and `files` is targeted at twenty million rows. The
    clause is written as an explicit two-branch OR so SQLite can use
    `idx_files_mtime` for the ordinary files and `idx_files_taken_at` for
    the photographs.
    """
    for i in range(200):
        store.upsert_file(fr"C:\Docs\{i}.txt", size_bytes=1, ext="txt",
                          status="INDEXED", mtime_ns=i * 1_000_000_000)
    store.conn.execute("ANALYZE")

    where, params = file_filter_sql(parse_query("before:2010"))
    plan = " | ".join(
        row[3] for row in
        store.conn.execute(
            f"EXPLAIN QUERY PLAN SELECT id FROM files f WHERE 1=1{where}", params))

    assert "idx_files_mtime" in plan, plan
    assert "idx_files_taken_at" in plan, plan
    assert "SCAN f" not in plan, plan


# ---------------------------------------------------------------------------
# End to end, through a real `Pipeline` run
# ---------------------------------------------------------------------------


class _NullVectors:
    def delete_by_file_ids(self, file_ids):
        pass

    def add(self, **kwargs):
        return len(kwargs.get("chunk_ids") or ())

    def __getattr__(self, name):
        return lambda *args, **kwargs: None


def _embedder(dim: int = 8) -> Embedder:
    def encode(texts):
        return [l2_normalise([math.sin(abs(hash(t)) % 100 + i) for i in range(dim)])
                for t in texts]

    return Embedder(dim=dim, encoder=encode)


@pytest.fixture
def ocr_reads_text(monkeypatch):
    """OCR that always finds a caption - the `_write_one` photo path."""
    monkeypatch.setattr(ocr_module, "available", lambda: True)
    monkeypatch.setattr(
        ocr_module, "_load_engine",
        lambda: (lambda _s: ([([0, 0, 1, 1], "Barnsley Dairy 1998", 0.94)], 0.01)))


@pytest.fixture
def ocr_finds_nothing(monkeypatch):
    """OCR that finds no text - the `_record_skip` path, and the majority of
    any real photo corpus: an ordinary photograph has no caption in it."""
    monkeypatch.setattr(ocr_module, "available", lambda: True)
    monkeypatch.setattr(ocr_module, "_load_engine", lambda: (lambda _s: ([], 0.0)))


def _run(store, root):
    Pipeline(store, _NullVectors(), _embedder(),
             PipelineConfig(walk=WalkConfig(roots=[root]), workers=1,
                            ocr_mode="images")).run()


def test_a_photo_whose_ocr_found_text_indexes_as_its_shot_date(
        tmp_path, ocr_reads_text):
    root = tmp_path / "photos"
    photo = _photo(root / "IMG_2006.jpg")

    with SqliteStore(tmp_path / "index.db") as store:
        _run(store, root)

        row = store.get_file(str(photo))
        assert row is not None
        assert row.taken_at_ns == _ns(SHOT)
        assert row.mtime_ns == photo.stat().st_mtime_ns   # the real copy date
        assert _matches(store, "before:2010") == [str(photo)]


def test_a_photo_with_no_ocr_text_at_all_still_indexes_as_its_shot_date(
        tmp_path, ocr_finds_nothing):
    r"""**The path the majority of a photo corpus actually takes.** A
    photograph with no text in it never yields a `Document` at all - `extract`
    raises `ERR_NO_TEXT_LAYER` and the row is written by `_record_skip`, not
    `_write_one`. A fix wired only into the document path would work on the
    test fixture and on almost none of the owner's twenty years of photos.
    """
    root = tmp_path / "photos"
    photo = _photo(root / "IMG_2006.jpg")

    with SqliteStore(tmp_path / "index.db") as store:
        _run(store, root)

        row = store.get_file(str(photo))
        assert row is not None
        assert row.taken_at_ns == _ns(SHOT)
        assert _matches(store, "before:2010") == [str(photo)]


def test_a_photo_with_no_exif_falls_back_to_its_file_time(
        tmp_path, ocr_finds_nothing):
    """`fallback to file time only when absent`, in §3a's own words."""
    root = tmp_path / "photos"
    photo = _photo(root / "plain.jpg", taken=None)

    with SqliteStore(tmp_path / "index.db") as store:
        _run(store, root)

        row = store.get_file(str(photo))
        assert row is not None
        assert row.taken_at_ns is None
        # 2019 mtime, so the 2010 cutoff must not match it.
        assert _matches(store, "before:2010") == []
        assert _matches(store, "after:2015") == [str(photo)]


def test_a_corrupt_photo_never_halts_the_run(tmp_path, ocr_finds_nothing):
    r"""**One bad file never halts a 100GB run.** A file with a `.jpg`
    extension and rubbish inside it must cost its own row's shot date and
    nothing else - the good photograph beside it still indexes, still
    carries its 2006 date, and is still found by `before:2010`.
    """
    root = tmp_path / "photos"
    good = _photo(root / "good.jpg")
    bad = root / "corrupt.jpg"
    bad.write_bytes(b"this is not a JPEG at all")

    with SqliteStore(tmp_path / "index.db") as store:
        _run(store, root)

        assert store.get_file(str(good)).taken_at_ns == _ns(SHOT)
        corrupt_row = store.get_file(str(bad))
        assert corrupt_row is not None          # still recorded, still findable
        assert corrupt_row.taken_at_ns is None  # quietly, with no shot date
        assert _matches(store, "before:2010") == [str(good)]


def test_rescanning_an_unchanged_photo_does_not_look_changed(
        tmp_path, ocr_finds_nothing):
    r"""**Why the shot date needed a column of its own.** The rejected fix was
    to write EXIF into `mtime_ns`; `app/index/walker.py` compares that same
    column against the file's live mtime to decide "unchanged", so a photo
    stored with its 2006 shot date would have been re-read, re-OCRed and
    re-embedded on every run for the life of the index. This asserts the
    property that fix would have destroyed.
    """
    root = tmp_path / "photos"
    photo = _photo(root / "IMG_2006.jpg")

    with SqliteStore(tmp_path / "index.db") as store:
        _run(store, root)
        first = store.get_file(str(photo))

        _run(store, root)                        # same bytes, same mtime
        second = store.get_file(str(photo))

        assert second.mtime_ns == photo.stat().st_mtime_ns
        assert second.taken_at_ns == _ns(SHOT)
        assert second.id == first.id


# ---------------------------------------------------------------------------
# Sort and display - §3a's third clause, "any date display use it"
#
# The lane-b note above named this the remaining gap: storage and `after:`/
# `before:` were fixed and tested here, but the SELECT sites that project
# `mtime_ns` onto a result row, and the ORDER BYs beside them, still ran on
# copy date. This is that second pass - `keyword.py`'s `_filter_only`
# (its SELECT and its ORDER BY), `SearchResult.taken_at_ns`, and
# `presenter.to_row`, which is the one place every UI surface reads a
# result's displayed date from.
# ---------------------------------------------------------------------------


def test_merge_by_date_prefers_the_shot_date_over_mtime(store):
    r"""`app.storage.filters.merge_by_date` in isolation, before trusting it
    inside a real query. Two already-`LIMIT`-bounded branches, exactly the
    shape `_filter_only` and `browse_files` hand it.
    """
    no_shot_date = [{"id": 1, "mtime_ns": 500, "taken_at_ns": None}]
    shot_date = [{"id": 2, "mtime_ns": 100, "taken_at_ns": 900}]

    merged = merge_by_date(no_shot_date, shot_date, limit=10, newest_first=True)

    assert [row["id"] for row in merged] == [2, 1]  # 900 beats 500


def test_filter_only_ranks_a_photo_by_its_shot_date_not_its_copy_date(store):
    r"""The acceptance sentence's sort half, not just its filter half.

    **Why these dates, and not a plain re-use of `SHOT`/`COPIED`.** A photo
    shot in 2006 and copied in 2019 already sorts *below* a document whose
    own `mtime_ns` is 2020, whichever column drives the sort - 2019 and 2006
    are both less than 2020 - so that pairing would pass whether or not this
    fix exists, and would prove nothing about which column the sort actually
    uses. To tell "sorts by `mtime_ns`" apart from "sorts by `taken_at_ns`
    first", the photo's shot date and its copy date have to sit on
    *opposite* sides of the other file's date: shot after it, copied before
    it.
    """
    newer_photo = store.upsert_file(
        r"C:\Photos\newer.jpg", size_bytes=1, ext="jpg", status="INDEXED",
        mtime_ns=_ns(datetime.datetime(2006, 1, 1)),        # an old copy time
        taken_at_ns=_ns(datetime.datetime(2023, 1, 1)))     # a recent shot
    store.replace_chunks(newer_photo, [{"text": "a photograph"}])

    older_document = store.upsert_file(
        r"C:\Docs\older.txt", size_bytes=1, ext="txt", status="INDEXED",
        mtime_ns=_ns(datetime.datetime(2020, 1, 1)))        # no shot date at all
    store.replace_chunks(older_document, [{"text": "a document"}])

    where, params = file_filter_sql(parse_query(""))
    rows = _filter_only(store, where, params, 10)

    paths = [row["path"] for row in rows]
    assert paths == [r"C:\Photos\newer.jpg", r"C:\Docs\older.txt"], paths
    # The bug this proves fixed: ordering on the raw `mtime_ns` column alone
    # would have put these the other way round - 2020 above 2006.
    assert rows[0]["mtime_ns"] < rows[1]["mtime_ns"]


def test_the_display_date_on_a_result_row_is_the_shot_date_when_present():
    r"""The acceptance sentence's display half - `SearchResult` through
    `presenter.to_row`, the one path every UI surface reads a result's date
    from (`to_row`'s own docstring: "turn a `SearchResult` into something a
    list widget can draw").
    """
    from app.search.engine import SearchResult
    from app.ui.presenter import to_row

    result = SearchResult(
        chunk_id=1, file_id=1, path=r"C:\Photos\2006.jpg", text="",
        score=1.0, rank=1,
        mtime_ns=_ns(datetime.datetime(2019, 3, 1)), taken_at_ns=_ns(SHOT),
    )

    row = to_row(result, terms=())

    assert row.mtime_ns == _ns(SHOT)


def test_a_result_with_no_shot_date_still_shows_its_mtime():
    """The fallback half of the same substitution - every non-photo result in
    the corpus must keep showing exactly what it always has."""
    from app.search.engine import SearchResult
    from app.ui.presenter import to_row

    copied = _ns(datetime.datetime(2020, 1, 1))
    result = SearchResult(
        chunk_id=1, file_id=1, path=r"C:\Docs\report.pdf", text="",
        score=1.0, rank=1, mtime_ns=copied,
    )

    row = to_row(result, terms=())

    assert row.mtime_ns == copied
