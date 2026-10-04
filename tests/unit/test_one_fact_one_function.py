"""One function per fact, and two lists showing the same thing for the same row.

2026-10-04, the owner: *"where ever possible the same code should run for
functions so they are all consistent and standard"*; Files, Code and Search
show the type badge and the date the Search tab's way (its plain/technical
setting); Mail keeps its exact dates. Each test here puts one row through two
(or more) surfaces and asserts they agree.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from app.storage.sqlite_store import SqliteStore, volume_synthetic_path

MESSAGE = "pst://2024/212470"
ATTACHMENT = f"{MESSAGE}/attachments/costs.docx"
MBOX_MESSAGE = "D:/Mail/old.mbox/123"
OLM_MESSAGE = "D:/Mail/mac.olm/Accounts/x/message_00001.xml"
ARCHIVE_SIZE = 2_000_000_000
SENT = int(time.mktime((2023, 5, 17, 9, 30, 0, 0, 0, -1)))
SHOT_NS = int(time.mktime((2019, 8, 2, 14, 0, 0, 0, 0, -1))) * 1_000_000_000
COPIED_NS = int(time.mktime((2025, 1, 9, 10, 0, 0, 0, 0, -1))) * 1_000_000_000
NOW = time.mktime((2026, 10, 4, 12, 0, 0, 0, 0, -1))


def _result(path, *, file_id=1, ext="", mtime_ns=COPIED_NS, taken_at_ns=None,
            volume_id=None, relative_path="", text="", page=None):
    return SimpleNamespace(path=path, file_id=file_id, chunk_id=file_id * 10, ext=ext,
                           mtime_ns=mtime_ns, taken_at_ns=taken_at_ns, volume_id=volume_id,
                           relative_path=relative_path, text=text, page=page, label="",
                           score=1.0, rank=1, sources=())


def _group(result, details=None, register="plain"):
    from app.ui.presenter import group_results, to_rows

    rows = to_rows([result], [])
    return group_results(rows, details=details or {}, now=NOW, register=register)[0], rows[0]


@pytest.fixture()
def store(tmp_path):
    s = SqliteStore(tmp_path / "facts.db").connect()
    mail = dict(parent_dir="D:/Mail", mtime_ns=COPIED_NS, status="INDEXED",
                source_kind="pst_message")
    message_id = s.upsert_file(MESSAGE, ext="pst", size_bytes=ARCHIVE_SIZE, content_hash="m1",
                               **mail)
    s.set_message(message_id, subject="School trip", sender="Dave Smith <dave@x.com>",
                  sent_at=SENT, has_attach=1)
    s.upsert_file(ATTACHMENT, ext="docx", size_bytes=4096, content_hash="a1", **mail)
    # The same message in a second archive - a duplicate at the archive's size.
    s.upsert_file("pst://2025/212470", ext="pst", size_bytes=ARCHIVE_SIZE, content_hash="m1",
                  **mail)
    s.upsert_file(MBOX_MESSAGE, ext="mbox", size_bytes=ARCHIVE_SIZE, content_hash="m2",
                  parent_dir="D:/Mail", mtime_ns=COPIED_NS, status="INDEXED", source_kind="eml")
    s.upsert_file("D:/Mail/note.eml", ext="eml", size_bytes=2048, content_hash="m3",
                  parent_dir="D:/Mail", mtime_ns=COPIED_NS, status="INDEXED", source_kind="eml")
    yield s
    s.close()


# -- 1. a message inside an archive has no size of its own ------------------

def test_every_surface_gives_an_archived_message_no_size():
    from app.core.row_facts import own_size
    from app.ui.presenter.rows import mail_rows

    rows = mail_rows([
        {"file_id": 1, "path": MESSAGE, "size_bytes": ARCHIVE_SIZE, "subject": "a"},
        {"file_id": 2, "path": MBOX_MESSAGE, "size_bytes": ARCHIVE_SIZE, "subject": "b"},
        {"file_id": 3, "path": OLM_MESSAGE, "size_bytes": ARCHIVE_SIZE, "subject": "c"},
        {"file_id": 4, "path": "D:/Mail/note.eml", "size_bytes": 2048, "subject": "d"},
    ])
    assert [row.size for row in rows] == ["", "", "", "2.0 KB"]
    # The timeline and the Files list read the same rule.
    for row in rows:
        assert own_size(ARCHIVE_SIZE if row.file_id < 4 else 2048, row.path, "eml") == row.size_bytes


def test_size_filter_ignores_an_archived_messages_size(store):
    from app.search.query import parse_query
    from app.storage.filters import file_filter_sql

    parsed = parse_query("size:>1gb")
    assert parsed.sizes                      # the filter was read
    where, params = file_filter_sql(parsed)
    found = {row[0] for row in store.conn.execute(
        f"SELECT f.path FROM files f WHERE 1=1{where}", params)}
    assert found == set()
    where, params = file_filter_sql(parse_query("size:>1kb"))
    found = {row[0] for row in store.conn.execute(
        f"SELECT f.path FROM files f WHERE 1=1{where}", params)}
    assert found == {ATTACHMENT, "D:/Mail/note.eml"}


def test_the_space_report_does_not_count_an_archived_message_at_the_archives_size(store):
    from app.reports.space import find_duplicate_groups, total_reclaimable_bytes

    assert total_reclaimable_bytes(store) == 0
    assert all(group.size_bytes < ARCHIVE_SIZE for group in find_duplicate_groups(store))


# -- 2. an attachment is dated by its message, on Files as on Search --------

def test_files_and_search_date_and_place_an_attachment_the_same(store):
    from app.ui.presenter import file_rows
    from app.ui.tasks import file_row_context, mail_details

    page = store.conn.execute(
        "SELECT id, path, ext, size_bytes, mtime_ns, taken_at_ns, status, skip_code, "
        "source_kind, volume_id, relative_path FROM files WHERE path = ?", (ATTACHMENT,))
    files_row = file_rows(file_row_context(store, [dict(r) for r in page]), now=NOW)[0]

    result = _result(ATTACHMENT, file_id=files_row.file_id, ext="docx")
    group, _row = _group(result, mail_details(store, [result]))

    assert files_row.modified == group.when
    assert files_row.mtime_ns == SENT * 1_000_000_000
    assert files_row.folder == group.folder == "from Dave Smith · School trip"
    from app.ui.presenter import kind_tag

    assert files_row.kind == kind_tag(group.kind) == "DOC"


# -- 3. a photo's date: the list, its order and the preview pane ------------

def test_a_photos_date_is_one_date_on_files_search_and_the_pane():
    from app.ui.inspector import preview_facts
    from app.ui.presenter import file_rows

    files_row = file_rows([{"id": 7, "path": "D:/Pics/a.jpg", "ext": "jpg", "size_bytes": 10,
                            "mtime_ns": COPIED_NS, "taken_at_ns": SHOT_NS,
                            "status": "INDEXED"}], now=NOW)[0]
    group, row = _group(_result("D:/Pics/a.jpg", file_id=7, ext="jpg", taken_at_ns=SHOT_NS))
    assert files_row.modified == group.when
    # The column sorts on the date the list is ordered by.
    assert files_row.mtime_ns == SHOT_NS
    assert dict(preview_facts(files_row))["Modified"] == dict(preview_facts(row))["Modified"]


def test_the_pane_subtitle_no_longer_carries_a_second_date(tmp_path):
    from app.ui.preview_loader import _describe

    path = tmp_path / "a.txt"
    path.write_text("hello")
    assert _describe(path) == "5 B"


# -- 4. a message's name ------------------------------------------------------

def test_a_blank_subject_is_named_the_same_on_mail_search_and_the_timeline():
    from app.core.row_facts import message_name
    from app.ui.presenter.rows import mail_rows

    mail = mail_rows([{"file_id": 1, "path": MESSAGE, "subject": "  "}])[0]
    group, _row = _group(_result(MESSAGE, ext="pst"), {1: {"subject": "", "sender": ""}})
    assert mail.subject == group.name == message_name("") == "(no subject)"


def test_a_pinned_search_result_and_the_pane_never_show_the_key():
    from app.ui.pinned import Pin, _as_pin
    from app.ui.presenter import display_name

    group, row = _group(_result(MESSAGE, ext="pst"),
                        {1: {"subject": "Trip", "sender": "Dave <d@x.com>"}})
    assert _as_pin(row).label == group.name == "Dave — Trip"
    assert display_name(row.name, row.path) == group.name
    assert "212470" not in Pin(MESSAGE).label


# -- 5. how many attachments ---------------------------------------------------

def test_the_attachment_count_is_the_real_one_or_no_number():
    headers = "Subject: Trip\nFrom: Dave\nAttachments: a.pdf, b.pdf, c.jpg\n\nHello"
    counted, _ = _group(_result(MESSAGE, ext="pst", text=headers),
                        {1: {"subject": "Trip", "has_attach": 1}})
    unknown, _ = _group(_result(MESSAGE, ext="pst", text="Hello"),
                        {1: {"subject": "Trip", "has_attach": 1}})
    assert counted.folder == "3 attachments"
    assert unknown.folder == "attachments"


# -- 6. folders -----------------------------------------------------------------

def test_a_drive_folder_reads_the_same_on_files_and_search():
    from app.ui.presenter import file_rows

    path = volume_synthetic_path(3, "Photos/2019/a.jpg")
    files_row = file_rows([{"id": 9, "path": path, "ext": "jpg", "size_bytes": 1,
                            "mtime_ns": COPIED_NS, "status": "INDEXED", "volume_id": 3,
                            "relative_path": "Photos/2019/a.jpg",
                            "volume_label": "Holiday drive"}], now=NOW)[0]
    group, _ = _group(_result(path, file_id=9, ext="jpg", volume_id=3,
                              relative_path="Photos/2019/a.jpg"),
                      {9: {"volume_label": "Holiday drive"}})
    assert files_row.folder == group.folder == "Holiday drive > Photos > 2019"


def test_the_panes_folder_is_never_the_location():
    from app.ui.inspector import preview_facts

    facts = dict(preview_facts(SimpleNamespace(path="D:/Docs/Reports/a.pdf", ext="pdf",
                                               mtime_ns=COPIED_NS, location="page 3", page=3)))
    assert facts["Folder"] == "Docs > Reports"
    assert facts["Page"] == "3"


# -- 7. the type badge -----------------------------------------------------------

@pytest.mark.parametrize("ext, word", [("docx", "DOC"), ("DOCX", "DOC"), ("dockerfile", "DOCK"),
                                        ("", ""), ("py", "PY")])
def test_files_code_search_and_the_pane_show_one_badge(ext, word):
    from app.ui.inspector import preview_facts
    from app.ui.presenter import file_rows, repo_file_rows

    name = f"x.{ext}" if ext else "LICENSE"
    files_row = file_rows([{"id": 1, "path": f"D:/a/{name}", "ext": ext, "size_bytes": 1,
                            "mtime_ns": COPIED_NS, "status": "INDEXED"}], now=NOW)[0]
    code_row = repo_file_rows([{"path": f"D:/a/{name}", "ext": ext, "mtime_ns": COPIED_NS}],
                              now=NOW)[0]
    group, _ = _group(_result(f"D:/a/{name}", ext=ext))
    from app.ui.presenter import kind_tag

    assert files_row.kind == code_row.kind == kind_tag(group.kind) == word
    assert dict(preview_facts(files_row)).get("Kind", "") == word


# -- 8. the note on a file matches its Status word -------------------------------

@pytest.mark.parametrize("status, code", [("SKIPPED", "ERR_OCR_HELD"), ("PENDING", None),
                                          ("SKIPPED", "ERR_FILE_TIMEOUT"), ("FAILED", "ERR_X")])
def test_the_files_note_is_the_status_columns_sentence(status, code):
    from app.core.file_state import explain
    from app.ui.presenter import file_rows

    from app.core.file_state import derive

    row = file_rows([{"id": 1, "path": "D:/a/x.pdf", "ext": "pdf", "size_bytes": 1,
                      "mtime_ns": COPIED_NS, "status": status, "skip_code": code}])[0]
    # *Corrected 4 October 2026, the same night:* an existing note keeps its
    # words where they were true (the standing rule on wording); the Status
    # column's sentence replaces only a note that was wrong - a file whose word
    # is not its status's plain one, such as a held picture ("Deferred").
    if row.status != derive(status, None):
        assert row.note.startswith(explain(row.status))
    else:
        assert row.note.startswith({"SKIPPED": "indexed by name only", "FAILED": "could not be read",
                                    "PENDING": "not indexed yet"}[status])


# -- 9. the date register --------------------------------------------------------

@pytest.mark.parametrize("register", ["plain", "technical"])
def test_files_and_code_follow_the_search_tabs_register(register):
    from app.ui.presenter import file_rows, repo_file_rows, set_date_register

    group, _ = _group(_result("D:/a/x.py", ext="py"), register=register)
    set_date_register(register)
    try:
        files_row = file_rows([{"id": 1, "path": "D:/a/x.py", "ext": "py", "size_bytes": 1,
                                "mtime_ns": COPIED_NS, "status": "INDEXED"}], now=NOW)[0]
        code_row = repo_file_rows([{"path": "D:/a/x.py", "ext": "py",
                                    "mtime_ns": COPIED_NS}], now=NOW)[0]
    finally:
        set_date_register("plain")
    assert files_row.modified == code_row.seen == group.when


# -- 10. one size formatter --------------------------------------------------------

def test_one_size_formatter_everywhere():
    from app.reports.inheritance import _size_words as inheritance_words
    from app.reports.space import _size_words as space_words
    from app.reports.timeline_words import size_words
    from app.ui.presenter import format_size

    three_tb = 3 * 1024 ** 4
    for value in (0, 812, 4_200_000, three_tb):
        assert (format_size(value) == size_words(value) == space_words(value)
                == inheritance_words(value))
    assert format_size(three_tb) == "3.0 TB"
