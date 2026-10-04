"""Open on a file that came out of an email: save a read-only copy, open it. 2026-10-04.

Layer: L5

The owner: "build the open on attachment, save a copy and open it", and "it
must still have the option to open in outlook". The bytes are read back out
of the archive by the direct reader, found by the folder and position the
index now records (a search of a 4.9 GB archive took 38 s), written to
Leasha's cache read-only, and opened. Open in Outlook stays beside it.
"""

from __future__ import annotations

import os
import stat
import sys
import types
import zipfile
from io import BytesIO
from pathlib import Path

import pytest

from app.core.errors import AppErrorException
from app.storage.sqlite_store import SqliteStore
from tests.unit.test_pst_libpff import FakeArchive, FakeAttachment, FakeFolder, FakeMessage

SHEET = b"PK-pretend-spreadsheet-bytes"
MESSAGE = "pst://2024/2097188"
ATTACHMENT = f"{MESSAGE}/attachments/Model CED.xlsm"


# -- the reader ---------------------------------------------------------------

def _install(monkeypatch, root) -> FakeArchive:
    from app.extract import pst_libpff

    archive = FakeArchive(root)
    module = types.ModuleType("pypff")
    module.file = lambda: archive                       # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "pypff", module)
    monkeypatch.setattr(pst_libpff, "available", lambda: True)
    return archive


class CountingFolder(FakeFolder):
    """Counts the messages fetched, so a walk can be told from a direct hit."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.fetched = 0

    def get_sub_message(self, index):
        self.fetched += 1
        return super().get_sub_message(index)


@pytest.fixture()
def archive(monkeypatch):
    others = [FakeMessage(identifier=100 + i) for i in range(50)]
    target = FakeMessage(identifier=2097188, attachments=[
        FakeAttachment("notes.txt", b"other"), FakeAttachment("Model CED.xlsm", SHEET)])
    inbox = CountingFolder("Inbox", messages=[*others, target])
    root = FakeFolder("Top of Personal Folders", children=[inbox])
    _install(monkeypatch, root)
    return inbox


def test_the_reader_goes_straight_to_the_message_where_the_index_says(archive):
    from app.extract.pst_attachment import read_attachment

    data = read_attachment("D:/OutlookArchive/2024.pst", "2097188", "Model CED.xlsm",
                           folder_path="Top of Personal Folders/Inbox", folder_index=50)
    assert data == SHEET
    assert archive.fetched == 1, "it walked the folder instead of taking the message by position"


def test_without_a_position_it_searches_and_still_finds_it(archive):
    from app.extract.pst_attachment import read_attachment

    assert read_attachment("x.pst", "2097188", "Model CED.xlsm") == SHEET
    assert archive.fetched == 51


def test_a_stale_position_is_checked_and_searched_past(archive):
    from app.extract.pst_attachment import read_attachment

    data = read_attachment("x.pst", "2097188", "Model CED.xlsm",
                           folder_path="Top of Personal Folders/Inbox", folder_index=3)
    assert data == SHEET, "a position that names another message must not be trusted"


@pytest.mark.parametrize("entry_id,name,why", [
    ("2097188", "missing.pdf", "no longer has that attachment"),
    ("999", "Model CED.xlsm", "no longer in the archive"),
    ("00000000ABCDEF0123456789ABCDEF0123456789ABCD", "Model CED.xlsm", "through Outlook"),
])
def test_what_cannot_be_found_says_so_and_points_at_open_in_outlook(archive, entry_id, name, why):
    from app.extract.pst_attachment import read_attachment

    with pytest.raises(AppErrorException) as raised:
        read_attachment("x.pst", entry_id, name)
    error = raised.value.error
    assert error.code == "ERR_ATTACHMENT_OPEN"
    assert why in error.details and "Open in Outlook" in error.suggestion


def test_the_direct_reader_records_where_each_message_sits_and_each_attachments_size(monkeypatch):
    from app.extract import pst_libpff
    from tests.unit.test_pst_libpff import install_fake, read_contents_of

    read_contents_of(monkeypatch, ".txt")
    message = FakeMessage(7, attachments=[FakeAttachment("costs.txt", b"pump,2\n")])
    install_fake(monkeypatch, FakeFolder("Top", children=[
        FakeFolder("Inbox", messages=[FakeMessage(6), message])]))
    documents = list(pst_libpff.read_archive(Path("2024.pst")))
    mail = [d for d in documents if "attachment_name" not in d.meta]
    attached = [d for d in documents if "attachment_name" in d.meta]
    assert [m.meta["folder_index"] for m in mail] == [0, 1]
    assert {m.meta["folder_path"] for m in mail} == {"Top/Inbox"}
    assert attached and attached[0].meta["attachment_size"] == len(b"pump,2\n"), (
        "the direct reader's attachments must carry their size, as the Outlook route's do")


def test_the_position_is_stored_on_the_message_row(tmp_path):
    store = SqliteStore(tmp_path / "s.db").connect()
    try:
        file_id = store.upsert_file(MESSAGE, size_bytes=1, mtime_ns=1, ext="pst",
                                    source_kind="pst_message", status="INDEXED")
        store.set_message(file_id, store_path="D:/a.pst", entry_id="2097188",
                          folder_path="Top/Inbox", folder_index=50)
        row = store.get_message(file_id)
        assert (row["folder_path"], row["folder_index"]) == ("Top/Inbox", 50)
    finally:
        store.close()


def test_the_schema_is_32_and_the_migration_adds_the_columns_once(tmp_path):
    import sqlite3

    from app.storage.migrations import CURRENT_VERSION, _v32_message_position

    assert CURRENT_VERSION == 32
    conn = sqlite3.connect(tmp_path / "old.db")
    conn.execute("CREATE TABLE messages (file_id INTEGER PRIMARY KEY, store_path TEXT)")
    _v32_message_position(conn)
    _v32_message_position(conn)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(messages)")}
    assert {"folder_path", "folder_index"} <= columns


def test_the_pipeline_writes_the_position():
    """The `quoted_removed` lesson: a column the pipeline does not list stays NULL."""
    source = Path("app/index/pipeline.py").read_text(encoding="utf-8")
    start = source.index("def _store_message_meta")
    body = source[start:source.index("\n    def ", start + 1)]
    assert '"folder_path"' in body and '"folder_index"' in body


# -- the copy -----------------------------------------------------------------

@pytest.fixture()
def store(tmp_path):
    s = SqliteStore(tmp_path / "s.db").connect()
    message_id = s.upsert_file(MESSAGE, size_bytes=1, mtime_ns=1, ext="pst",
                               source_kind="pst_message", status="INDEXED")
    s.set_message(message_id, store_path="D:/OutlookArchive/2024.pst", entry_id="2097188",
                  folder_path="Top/Inbox", folder_index=50, subject="CED")
    s.upsert_file(ATTACHMENT, size_bytes=len(SHEET), mtime_ns=1, ext="xlsm",
                  source_kind="pst_message", status="INDEXED")
    yield s
    s.close()


def test_the_copy_is_the_attachment_read_only_in_leashas_cache(store, tmp_path):
    from app.ui.attachment_open import OPENED_FOLDER
    from app.ui.tasks import save_attachment_copy

    asked = {}

    def reader(archive, entry_id, name, **where):
        asked.update(archive=archive, entry_id=entry_id, name=name, **where)
        return SHEET

    copy = save_attachment_copy(store, ATTACHMENT, tmp_path / "cache", reader=reader)
    assert copy.read_bytes() == SHEET and copy.name == "Model CED.xlsm"
    assert copy.parent.parent == tmp_path / "cache" / OPENED_FOLDER
    assert not os.access(copy, os.W_OK), "the copy must be read-only"
    assert asked == {"archive": "D:/OutlookArchive/2024.pst", "entry_id": "2097188",
                     "name": "Model CED.xlsm", "folder_path": "Top/Inbox", "folder_index": 50,
                     "search": True, "max_bytes": None}
    # A second Open replaces this session's copy rather than failing on it.
    assert save_attachment_copy(store, ATTACHMENT, tmp_path / "cache", reader=reader) == copy


def test_a_document_from_inside_a_zipped_attachment_opens_as_itself(store, tmp_path):
    from app.ui.tasks import save_attachment_copy

    zipped = BytesIO()
    with zipfile.ZipFile(zipped, "w") as z:
        z.writestr("q3/report.docx", b"the report")
    inner = f"{MESSAGE}/attachments/pack.zip/q3/report.docx"
    copy = save_attachment_copy(store, inner, tmp_path / "cache",
                                reader=lambda *a, **k: zipped.getvalue())
    assert copy.name == "report.docx" and copy.read_bytes() == b"the report"


def test_an_attachment_whose_message_is_not_indexed_says_so(store, tmp_path):
    from app.ui.tasks import save_attachment_copy

    with pytest.raises(AppErrorException) as raised:
        save_attachment_copy(store, "pst://2024/1/attachments/x.pdf", tmp_path / "cache",
                             reader=lambda *a, **k: b"")
    assert raised.value.error.code == "ERR_ATTACHMENT_OPEN"


def test_copies_from_an_earlier_session_are_swept_and_clear_removes_the_rest(store, tmp_path):
    from app.ui import attachment_open
    from app.ui.tasks import save_attachment_copy

    cache = tmp_path / "cache"
    old = cache / attachment_open.OPENED_FOLDER / "old"
    old.mkdir(parents=True)
    (old / "stale.pdf").write_bytes(b"x")
    (old / "stale.pdf").chmod(stat.S_IREAD)
    os.utime(old, (1, 1))                                 # long before this session
    copy = save_attachment_copy(store, ATTACHMENT, cache, reader=lambda *a, **k: SHEET)
    assert not old.exists() and copy.exists()
    attachment_open.clear_opened(cache)
    assert not (cache / attachment_open.OPENED_FOLDER).exists()


def test_only_a_file_from_a_mail_archive_is_opened_this_way():
    from app.ui.attachment_open import is_archive_attachment

    assert is_archive_attachment(ATTACHMENT)
    assert not is_archive_attachment(MESSAGE)
    assert not is_archive_attachment(r"D:\Docs\attachments\notes.txt")


@pytest.mark.gui
def test_open_saves_the_copy_and_opens_it_on_a_worker(store, tmp_path, qtbot, monkeypatch):
    from app.extract import pst_attachment
    from app.ui import workers

    monkeypatch.setattr(pst_attachment, "read_attachment", lambda *a, **k: SHEET)
    monkeypatch.setattr(workers, "_CONTEXT", workers.OpenContext(cache_path=tmp_path / "cache"))
    monkeypatch.setattr(workers, "open_in_explorer",
                        lambda path, select=True: opened.append(path))
    opened, notes, errors = [], [], []
    workers.open_row_async(store, ATTACHMENT, on_error=errors.append, on_note=notes.append)
    qtbot.waitUntil(lambda: bool(notes or errors), timeout=5000)
    assert not errors and opened and Path(opened[0]).read_bytes() == SHEET
    assert notes[0] == ("Opened a copy of 'Model CED.xlsm' from the email. "
                        "Changes to it are not saved back to the email.")


@pytest.mark.gui
def test_the_window_routes_open_on_an_attachment_to_the_copy_and_keeps_open_in_outlook(
        gui_mainwindow, monkeypatch):
    from app.ui import shell, workers
    from app.ui.presenter.mail import OPEN_IN_OUTLOOK, original_target
    from app.ui.presenter.opening import plan_for

    _app, window, _store, _engine = gui_mainwindow
    asked = []
    monkeypatch.setattr(shell, "open_row_async",
                        lambda store, row, **k: asked.append((row.path, store)))
    window._open_path(ATTACHMENT)
    assert asked == [(ATTACHMENT, window._store)] and plan_for(ATTACHMENT).how == "copy"
    assert workers._CONTEXT.cache_path == window._settings.cache_path
    # And the parent message still offers Outlook, for the attachment's preview too.
    target = original_target({"entry_id": "2097188", "store_path": "D:/a/2024.pst"}, ATTACHMENT)
    assert target.kind == "outlook" and target.label == OPEN_IN_OUTLOOK


# -- the same for zips (the owner: "the same should be for zips i presume") --

def _zip(entries: dict) -> bytes:
    out = BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        for name, data in entries.items():
            z.writestr(name, data)
    return out.getvalue()


def test_a_file_inside_a_zip_takes_its_own_type_and_size_not_the_zips():
    from types import SimpleNamespace

    from app.index.pipeline import _row_type_and_size

    item = SimpleNamespace(
        meta={"inside_archive": r"D:\a\backup.zip", "member": "q3/report.docx", "member_size": 4096},
        candidate=SimpleNamespace(path=Path(r"D:\a\backup.zip"), size_bytes=90_000_000))
    assert _row_type_and_size(item) == ("docx", 4096)


def test_a_file_inside_a_zipped_attachment_is_the_members_not_the_zips():
    from types import SimpleNamespace

    from app.index.pipeline import _row_type_and_size

    item = SimpleNamespace(
        meta={"attachment_name": "pack.zip", "attachment_size": 2_000_000,
              "inside_archive": r"C:\tmp\pack.zip", "member": "inner/sheet.xlsx", "member_size": 512},
        candidate=SimpleNamespace(path=Path(r"D:\OutlookArchive\2024.pst"), size_bytes=4_900_000_000))
    assert _row_type_and_size(item) == ("xlsx", 512)


def test_the_archive_reader_records_each_members_size(tmp_path):
    from app.extract.archive import read_archive

    archive = tmp_path / "backup.zip"
    archive.write_bytes(_zip({"q3/report.txt": b"quarterly figures here"}))
    documents = [d for d in read_archive(archive) if d.meta.get("member", "").endswith("report.txt")]
    assert documents and documents[0].meta["member_size"] == len(b"quarterly figures here")


def test_open_on_a_file_inside_a_zip_on_disk_copies_it_out(store, tmp_path):
    from app.ui.attachment_open import opens_from_a_copy, zip_member_of
    from app.ui.tasks import save_attachment_copy

    archive = tmp_path / "docs" / "backup.zip"
    archive.parent.mkdir()
    archive.write_bytes(_zip({"q3/report.docx": b"the report",
                              "old/inner.zip": _zip({"deep/note.txt": b"nested"})}))
    member = f"{archive}/q3/report.docx"
    assert zip_member_of(member) == (str(archive), "q3/report.docx") and opens_from_a_copy(member)
    copy = save_attachment_copy(store, member, tmp_path / "cache")
    assert copy.name == "report.docx" and copy.read_bytes() == b"the report"
    assert not os.access(copy, os.W_OK)
    deep = save_attachment_copy(store, f"{archive}/old/inner.zip/deep/note.txt", tmp_path / "cache")
    assert deep.read_bytes() == b"nested", "a zip inside a zip"
    assert archive.read_bytes() == archive.read_bytes(), "the zip is only read"


def test_a_member_the_zip_no_longer_holds_says_so(store, tmp_path):
    from app.ui.tasks import save_attachment_copy

    archive = tmp_path / "backup.zip"
    archive.write_bytes(_zip({"a.txt": b"x"}))
    with pytest.raises(AppErrorException) as raised:
        save_attachment_copy(store, f"{archive}/gone.docx", tmp_path / "cache")
    assert raised.value.error.code == "ERR_ATTACHMENT_OPEN"
    assert "no longer holds" in raised.value.error.details


def test_neither_a_plain_file_a_zip_itself_nor_a_catalogued_drive_is_copied():
    from app.ui.attachment_open import opens_from_a_copy

    assert not opens_from_a_copy(r"D:\Docs\notes.txt")
    assert not opens_from_a_copy(r"D:\Docs\backup.zip")
    assert not opens_from_a_copy("leasha-volume://1/Holiday/pack.zip/beach.jpg")


def test_show_in_folder_on_a_zip_member_shows_the_zip():
    from app.ui.presenter.opening import plan_for

    plan = plan_for(r"D:\Docs\backup.zip/q3/report.docx", reveal=True)
    assert (plan.how, plan.path) == ("reveal", r"D:\Docs\backup.zip")


# -- every page's Open, not only Search's (the owner's screenshot, 2026-10-04) --
#
# Double-click on an attachment on the Files page said "Not found on disk":
# Open-from-a-copy was in the window's route only. There is one route now
# (`workers.open_row_async`), and every page takes it.

@pytest.mark.gui
def test_the_files_page_double_click_on_an_attachment_opens_the_copy(gui_mainwindow, monkeypatch):
    from types import SimpleNamespace

    from app.ui import files_view, workers

    _app, window, _store, _engine = gui_mainwindow
    asked = []
    monkeypatch.setattr(files_view, "open_row_async",
                        lambda store, row, **k: asked.append(row.path))
    window.files_view._open(SimpleNamespace(path=ATTACHMENT, volume_id=None), reveal=False)
    assert asked == [ATTACHMENT]
    assert "open_row_async" in workers.__all__


@pytest.mark.gui
def test_show_in_folder_on_an_attachment_shows_its_archive(gui_mainwindow, qtbot, monkeypatch):
    from app.ui import workers

    _app, window, store, _engine = gui_mainwindow
    message_id = store.upsert_file(MESSAGE, size_bytes=1, mtime_ns=1, ext="pst",
                                   source_kind="pst_message", status="INDEXED")
    store.set_message(message_id, store_path="D:/OutlookArchive/2024.pst", entry_id="2097188")
    shown = []
    monkeypatch.setattr(workers, "open_in_explorer",
                        lambda path, select=False: shown.append((path, select)))
    workers.open_async(ATTACHMENT, reveal=True)
    qtbot.waitUntil(lambda: bool(shown), timeout=5_000)
    assert shown == [("D:/OutlookArchive/2024.pst", True)]
