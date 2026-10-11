r"""Order 1j section 1: the Space Report counts files on disk only.

Layer: L2

2026-10-11, the owner: "the space report should not include duplicates from
pst's it should be only for files". Measured on the owner's index that day:
31,748 `pst_message` rows and 58,256 zip members shared a `content_hash`, and
two of the report's queries filtered nothing at all. Default D1: a real file
on disk (`source_kind = 'file'`) - deleting a message or a zip member gives no
room back.
"""

from __future__ import annotations

from app.reports.space import (
    SCOPE_SENTENCE,
    find_duplicate_groups,
    find_near_duplicate_photo_groups,
    find_source_duplicate_share,
    find_source_uniqueness,
    render_space_document,
    total_reclaimable_bytes,
)
from app.storage.sqlite_store import SqliteStore

MB = 1_000_000


def _seed(store: SqliteStore) -> None:
    rows = [
        # A real file, twice: the only duplicate the report may name.
        ("D:/Data/report.pdf", "file", "h-file", 4 * MB),
        ("D:/Backup/report.pdf", "file", "h-file", 4 * MB),
        # A PST attachment, twice.
        ("pst://1/10/attachments/plan.docx", "pst_message", "h-attach", 2 * MB),
        ("pst://1/11/attachments/plan.docx", "pst_message", "h-attach", 2 * MB),
        # A PST message, twice (carrying its archive's size).
        ("pst://1/20", "pst_message", "h-mail", 900 * MB),
        ("pst://1/21", "pst_message", "h-mail", 900 * MB),
        # A member inside a zip, twice.
        ("D:/Data/a.zip/photo.jpg", "archive", "h-zip", 3 * MB),
        ("D:/Data/b.zip/photo.jpg", "archive", "h-zip", 3 * MB),
        # A file that exists nowhere else, and a message that exists nowhere else.
        ("D:/Data/only.txt", "file", "h-only", 1 * MB),
        ("pst://1/30", "pst_message", "h-only-mail", 900 * MB),
    ]
    for path, kind, digest, size in rows:
        store.upsert_file(path, size_bytes=size, mtime_ns=1, content_hash=digest,
                          status="INDEXED", source_kind=kind)


def test_only_the_file_is_a_duplicate_in_every_section(tmp_path):
    with SqliteStore(tmp_path / "s.db") as store:
        _seed(store)
        groups = find_duplicate_groups(store)
        assert [g.content_hash for g in groups] == ["h-file"]
        assert {c.path for c in groups[0].copies} == {"D:/Data/report.pdf", "D:/Backup/report.pdf"}
        assert total_reclaimable_bytes(store) == 4 * MB

        share = find_source_duplicate_share(store)
        (local,) = share
        assert (local.duplicate_count, local.total_count) == (2, 3)

        unique = find_source_uniqueness(store)
        (only,) = unique
        assert only.file_count == 1, "a message counted as a file that exists nowhere else"


def test_near_duplicate_photos_leave_out_zip_members(tmp_path):
    with SqliteStore(tmp_path / "s.db") as store:
        for path, kind, digest in (("D:/Pics/a.jpg", "archive", "p1"),
                                   ("D:/Pics/b.jpg", "archive", "p2")):
            file_id = store.upsert_file(path, size_bytes=MB, mtime_ns=1, content_hash=digest,
                                        status="INDEXED", source_kind=kind)
            with store.write() as conn:
                conn.execute("UPDATE files SET phash = ? WHERE id = ?", ("ffff0000ffff0000", file_id))
        assert find_near_duplicate_photo_groups(store) == []


def test_the_document_says_what_it_covers():
    document = render_space_document([], [], generated_at=1_700_000_000)
    assert SCOPE_SENTENCE in document
    assert "not mail" in SCOPE_SENTENCE and ".zip" in SCOPE_SENTENCE
