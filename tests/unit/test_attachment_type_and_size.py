"""An attachment's row is its own file: its type and its size, not the archive's. 2026-10-04.

Layer: L3

The owner's Files page, `*.pst`: every attachment read out of a mail archive
listed as **PST, 4.9 GB** - the archive's `ext` and `size_bytes`, written to
every document the archive produced. Read in his index: 17,952 rows, all
`ext='pst'`, each with its archive's byte count. Three fixes, three tests:
the reader carries the size, the pipeline writes the attachment's own type
and size, and a migration repairs the rows already written (type from the
key; size blanked, since it was never stored).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from app.storage.migrations import _v31_attachment_type_and_size
from app.storage.sqlite_store import SqliteStore

ARCHIVE_SIZE = 4_900_000_000
MESSAGE = "pst://2024/212470"
SHEET = f"{MESSAGE}/attachments/207398_KNPC new CFP Units.xlsm"
INSIDE_ZIP = f"{MESSAGE}/attachments/tools.zip/inner/report.docx"


def _item(meta: dict, path: str = r"D:\OutlookArchive\2024.pst") -> SimpleNamespace:
    return SimpleNamespace(meta=meta, candidate=SimpleNamespace(path=Path(path), size_bytes=ARCHIVE_SIZE))


def test_an_attachment_row_takes_its_own_type_and_size():
    from app.index.pipeline import _row_type_and_size

    ext, size = _row_type_and_size(_item({"attachment_name": "Model CED.xlsm", "attachment_size": 401_204}))
    assert (ext, size) == ("xlsm", 401_204)


def test_a_message_row_keeps_the_archives():
    from app.index.pipeline import _row_type_and_size

    assert _row_type_and_size(_item({})) == ("pst", ARCHIVE_SIZE)


def test_an_attachment_without_a_size_is_not_given_the_archives():
    from app.index.pipeline import _row_type_and_size

    assert _row_type_and_size(_item({"attachment_name": "notes.pdf"})) == ("pdf", 0)


def test_the_reader_puts_the_attachments_size_on_the_document():
    from tests.unit.test_email_pst import DECK, FakeAttachment, FakeFolder, FakeSession, FakeStore, message
    from app.extract.email_pst import walk_session

    item = message("with-attach")
    item.attachments = [FakeAttachment("costs.csv", DECK)]
    documents = list(walk_session(FakeSession(FakeStore("s", FakeFolder("Root", items=[item])))))
    attached = [d for d in documents if "attachment_name" in d.meta]
    assert attached and attached[0].meta["attachment_size"] == len(DECK)


@pytest.fixture()
def store(tmp_path):
    s = SqliteStore(tmp_path / "attach.db").connect()
    common = dict(parent_dir="D:/Mail", mtime_ns=1, status="INDEXED", source_kind="pst_message")
    s.upsert_file(path=MESSAGE, ext="pst", size_bytes=ARCHIVE_SIZE, content_hash="a", **common)
    s.upsert_file(path=SHEET, ext="pst", size_bytes=ARCHIVE_SIZE, content_hash="b", **common)
    s.upsert_file(path=INSIDE_ZIP, ext="pst", size_bytes=ARCHIVE_SIZE, content_hash="c", **common)
    s.upsert_file(path="D:/Docs/notes.txt", ext="txt", size_bytes=10, content_hash="d",
                  parent_dir="D:/Docs", mtime_ns=1, status="INDEXED", source_kind="file")
    yield s
    s.close()


def _row(store, path):
    return store.conn.execute("SELECT ext, size_bytes FROM files WHERE path = ?", (path,)).fetchone()


def test_the_migration_gives_rows_already_written_their_type_and_blanks_the_size(store):
    _v31_attachment_type_and_size(store.conn)
    _v31_attachment_type_and_size(store.conn)                      # idempotent
    assert tuple(_row(store, SHEET)) == ("xlsm", 0)
    assert tuple(_row(store, INSIDE_ZIP)) == ("docx", 0), "a member inside a zipped attachment"
    assert tuple(_row(store, MESSAGE)) == ("pst", ARCHIVE_SIZE), "the message is not an attachment"
    assert tuple(_row(store, "D:/Docs/notes.txt")) == ("txt", 10)


def test_the_migration_leaves_a_row_already_right_alone(store):
    store.upsert_file(path=f"{MESSAGE}/attachments/fixed.pdf", ext="pdf", size_bytes=777,
                      content_hash="e", parent_dir="D:/Mail", mtime_ns=1, status="INDEXED",
                      source_kind="pst_message")
    _v31_attachment_type_and_size(store.conn)
    assert tuple(_row(store, f"{MESSAGE}/attachments/fixed.pdf")) == ("pdf", 777)


def test_the_files_page_shows_a_blank_size_for_an_attachment_not_yet_sized(store):
    from app.ui.presenter.rows import file_rows

    _v31_attachment_type_and_size(store.conn)
    rows = store.conn.execute("SELECT * FROM files WHERE path IN (?, ?)", (SHEET, "D:/Docs/notes.txt")).fetchall()
    shown = {r.name: r for r in file_rows([dict(r) for r in rows])}
    assert shown["207398_KNPC new CFP Units.xlsm"].kind == "XLSM"
    assert shown["207398_KNPC new CFP Units.xlsm"].size == ""
    assert shown["notes.txt"].size == "10 B"


def test_the_schema_moved_to_31():
    from app.storage.migrations import CURRENT_VERSION, MIGRATIONS

    # At least: 32 followed the same night (the message's place in its archive).
    assert CURRENT_VERSION >= 31 and 31 in MIGRATIONS
