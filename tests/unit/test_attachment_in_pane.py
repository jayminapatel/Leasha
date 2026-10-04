"""An attachment's pages in the preview pane, read into memory, no copy. 2026-10-04.

Layer: L5

The owner, offered "view an attachment in the pane without saving a copy":
"do both". The pane reads the attachment's bytes on its worker and draws a
PDF's pages, a picture or a workbook's grid from memory; a file inside a zip
on disk the same. Anything it cannot draw shows the words it showed before.
It never searches an archive for the message (38 s on a 4.9 GB archive) -
only where the index says the message is.
"""

from __future__ import annotations

import io
import os
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.errors import AppErrorException
from app.storage.sqlite_store import SqliteStore
from tests.unit.test_open_attachment import CountingFolder, _install
from tests.unit.test_pst_libpff import FakeAttachment, FakeFolder, FakeMessage

MESSAGE = "pst://2024/2097188"
REPORT = f"{MESSAGE}/attachments/report.pdf"


def _pdf(pages: int = 1) -> bytes:
    import pymupdf

    document = pymupdf.open()
    for number in range(pages):
        document.new_page().insert_text((72, 72), f"page {number + 1}")
    data = document.tobytes()
    document.close()
    return data


def _xlsx() -> bytes:
    import openpyxl

    book = openpyxl.Workbook()
    book.active.append(["pump", 2])
    out = io.BytesIO()
    book.save(out)
    return out.getvalue()


def _png() -> bytes:
    from PyQt6.QtCore import QBuffer, QIODevice
    from PyQt6.QtGui import QColor, QImage

    image = QImage(4, 2, QImage.Format.Format_RGB32)
    image.fill(QColor("red"))
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, "PNG")
    return bytes(buffer.data())


# -- the reader: never the slow search, never too much ------------------------

@pytest.fixture()
def archive(monkeypatch):
    others = [FakeMessage(identifier=100 + i) for i in range(50)]
    target = FakeMessage(identifier=2097188, attachments=[FakeAttachment("report.pdf", b"%PDF-1")])
    inbox = CountingFolder("Inbox", messages=[*others, target])
    _install(monkeypatch, FakeFolder("Top", children=[inbox]))
    return inbox


def test_for_the_pane_the_reader_does_not_search_when_the_position_is_wrong(archive):
    from app.extract.pst_attachment import read_attachment

    with pytest.raises(AppErrorException) as raised:
        read_attachment("x.pst", "2097188", "report.pdf", folder_path="Top/Inbox",
                        folder_index=3, search=False)
    assert "does not say where" in raised.value.error.details
    assert archive.fetched == 1, "it searched the folder behind a down-arrow"


def test_the_reader_refuses_an_attachment_over_the_limit_before_reading_it(archive):
    from app.extract.pst_attachment import read_attachment

    with pytest.raises(AppErrorException) as raised:
        read_attachment("x.pst", "2097188", "report.pdf", folder_path="Top/Inbox",
                        folder_index=50, max_bytes=3)
    assert "too large" in raised.value.error.details


def test_a_zip_on_disk_is_read_from_the_disk_not_whole_into_memory(tmp_path, monkeypatch):
    from app.extract.pst_attachment import member_of

    archive = tmp_path / "backup.zip"
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("q3/report.txt", b"figures")
        z.writestr("big.bin", b"x" * 1000)
    monkeypatch.setattr(Path, "read_bytes", lambda self: pytest.fail("read the whole zip"))
    assert member_of(archive, "q3/report.txt") == b"figures"
    assert member_of(archive, "big.bin", max_bytes=10) is None


# -- what the pane is given ----------------------------------------------------

@pytest.fixture()
def store(tmp_path):
    s = SqliteStore(tmp_path / "s.db").connect()
    message_id = s.upsert_file(MESSAGE, size_bytes=1, mtime_ns=1, ext="pst",
                               source_kind="pst_message", status="INDEXED")
    s.set_message(message_id, store_path="D:/OutlookArchive/2024.pst", entry_id="2097188",
                  folder_path="Top/Inbox", folder_index=50, subject="Quarterly report")
    s.upsert_file(REPORT, size_bytes=10, mtime_ns=1, ext="pdf",
                  source_kind="pst_message", status="INDEXED")
    yield s
    s.close()


def _row(store, path):
    return SimpleNamespace(file_id=store.get_file(path).id, path=path, name=Path(path).name)


def test_an_attached_pdf_previews_as_its_pages_under_its_message(store, monkeypatch):
    from app.extract import pst_attachment
    from app.ui.preview_loader import KIND_PDF, load_preview_for

    asked = {}

    def reader(archive, entry_id, name, **where):
        asked.update(where)
        return _pdf(3)

    monkeypatch.setattr(pst_attachment, "read_attachment", reader)
    preview = load_preview_for(_row(store, REPORT), store=store)
    assert preview.kind == KIND_PDF and preview.meta["data"][:4] == b"%PDF"
    assert preview.meta["mail"].card.subject == "Quarterly report"
    assert preview.title == "Quarterly report" and "report.pdf" in preview.notice
    assert asked["search"] is False and asked["max_bytes"] > 0


def test_an_attachment_that_cannot_be_read_previews_its_words_as_before(store, monkeypatch):
    from app.extract import pst_attachment
    from app.ui.preview_loader import KIND_TEXT, load_preview_for

    def reader(*_a, **_k):
        raise AppErrorException(pst_attachment._error(Path("x.pst"), "report.pdf", "gone").error)

    monkeypatch.setattr(pst_attachment, "read_attachment", reader)
    preview = load_preview_for(_row(store, REPORT), store=store)
    assert preview.kind == KIND_TEXT and "mail" in preview.meta


def test_a_type_the_pane_cannot_draw_is_never_read(store, monkeypatch):
    from app.extract import pst_attachment
    from app.ui.preview_loader import in_memory_preview

    monkeypatch.setattr(pst_attachment, "read_attachment",
                        lambda *a, **k: pytest.fail("read a .docx for the pane"))
    assert in_memory_preview(f"{MESSAGE}/attachments/letter.docx", {"store_path": "x"}) is None
    assert in_memory_preview(r"D:\Docs\report.pdf", None) is None, "a file on disk is not this"


def test_an_attached_workbook_previews_as_its_grid(store):
    from app.ui.preview_loader import KIND_SPREADSHEET, in_memory_preview

    message = store.get_message(store.get_file(MESSAGE).id)
    preview = in_memory_preview(f"{MESSAGE}/attachments/costs.xlsx", message,
                                reader=lambda *a, **k: _xlsx())
    assert preview.kind == KIND_SPREADSHEET
    assert preview.meta["sheets"] and preview.title == "costs.xlsx"


def test_a_pdf_inside_a_zip_on_disk_previews_as_its_pages(tmp_path):
    from app.ui.preview_loader import KIND_PDF, load_preview_for

    archive = tmp_path / "backup.zip"
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("q3/report.pdf", _pdf())
    preview = load_preview_for(SimpleNamespace(path=f"{archive}/q3/report.pdf", name="report.pdf"))
    assert preview.kind == KIND_PDF and preview.title == "report.pdf"


# -- what the pane draws -------------------------------------------------------

@pytest.fixture(scope="module")
def qapp():
    pytest.importorskip("PyQt6.QtWidgets", exc_type=ImportError)
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtWidgets import QApplication

    yield QApplication.instance() or QApplication([])


@pytest.mark.gui
def test_a_picture_attachment_decodes_from_memory(qapp):
    from app.ui.preview_loader import KIND_IMAGE, decode_image_data, in_memory_preview

    assert decode_image_data(b"not a picture") is None
    preview = in_memory_preview(f"{MESSAGE}/attachments/map.png", {"store_path": "x"},
                                reader=lambda *a, **k: _png())
    assert preview.kind == KIND_IMAGE and preview.meta["image"].width() == 4


@pytest.mark.gui
def test_the_pane_draws_pdf_pages_and_a_picture_from_memory(qapp):
    from app.ui.preview_loader import KIND_IMAGE, KIND_PDF, Preview, decode_image_data
    from app.ui.widgets.preview import PreviewPane

    pane = PreviewPane()
    if pane._pdf is None:
        pytest.skip("this Qt build has no PDF module")
    pane._rendered(Preview(kind=KIND_PDF, path=REPORT, title="report.pdf",
                           meta={"data": _pdf(2)}), pane._generation)
    assert pane.stack.currentWidget() is pane._pdf
    assert pane._pdf_document.pageCount() == 2

    pane._rendered(Preview(kind=KIND_IMAGE, path=f"{MESSAGE}/attachments/map.png",
                           meta={"image": decode_image_data(_png())}), pane._generation)
    assert pane.stack.currentWidget() is pane.image
