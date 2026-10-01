"""Files that arrived attached to an email are listed on the Files tab.

Layer: L1 (store) and L5 (the Files worker).

**Owner, 1 October 2026:** *"files in emails should come up on the files list
tab"*. Each attachment has always been its own `files` row, keyed
`<message>/attachments/<name>`, but the Files list read `source_kind = 'file'`
only and `files_fts` held no attachment names, so neither browsing nor typing
the name found one. The message itself stays on the Mail tab.
"""

from __future__ import annotations

import pytest

from app.storage.migrations import _v30_attachment_names
from app.storage.sqlite_store import SqliteStore
from app.ui.tasks import browse_files_typed

MESSAGE = "pst://2007/2102436"
ATTACHMENT = f"{MESSAGE}/attachments/Hotel Voucher.pdf"


@pytest.fixture()
def store(tmp_path):
    s = SqliteStore(tmp_path / "attach.db").connect()
    common = dict(parent_dir="D:/Mail", size_bytes=10, mtime_ns=1, status="INDEXED")
    s.upsert_file(path="D:/Docs/notes.txt", ext="txt", content_hash="a",
                  source_kind="file", **{**common, "parent_dir": "D:/Docs"})
    s.upsert_file(path=MESSAGE, ext="pst", content_hash="b", source_kind="pst_message", **common)
    s.upsert_file(path=ATTACHMENT, ext="pst", content_hash="c", source_kind="pst_message",
                  **common)
    yield s
    s.close()


def _paths(page) -> set:
    return {row["path"] for row in page["rows"]}


def test_an_attachment_is_listed_and_its_message_is_not(store):
    page = browse_files_typed(store, "", limit=500)
    assert _paths(page) == {"D:/Docs/notes.txt", ATTACHMENT}


def test_an_attachment_is_found_by_its_name(store):
    assert _paths(browse_files_typed(store, "voucher", limit=500)) == {ATTACHMENT}


def test_the_index_total_counts_attachments(store):
    assert store.count_listed_files() == 2


def test_attachments_indexed_before_this_change_are_backfilled(store):
    attachment_id = store.get_file(ATTACHMENT).id
    store.conn.execute("DELETE FROM files_fts WHERE rowid = ?", (attachment_id,))
    assert _paths(browse_files_typed(store, "voucher", limit=500)) == set()

    _v30_attachment_names(store.conn)
    _v30_attachment_names(store.conn)                    # idempotent
    assert _paths(browse_files_typed(store, "voucher", limit=500)) == {ATTACHMENT}
    names = store.conn.execute(
        "SELECT name FROM files_fts WHERE rowid = ?", (attachment_id,)).fetchall()
    assert [row[0] for row in names] == ["Hotel Voucher.pdf"]
