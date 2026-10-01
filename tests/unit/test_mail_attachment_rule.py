"""What is read out of a file attached to an email.

Layer: L2 (extraction), through the real `pst_libpff` attachment loop on a fake
`pypff` - the same fakes as `test_pst_libpff` - so it runs anywhere.

**Owner, 1 October 2026:** *"for mails indexing for zips in mails only index by
name and no ocr on pictures, the zip name and the file names inside the zip
should be indexed, only for office documents contents should be indexed"* - and
PDFs are to be read too. See `app/extract/mail_attachments.py`.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest

from app.extract import base, mail_attachments, progress, pst_libpff, reading
from app.extract.base import Document
from app.extract.mail_attachments import CONTENTS, NAME_ONLY, NAMES_INSIDE, rule
from tests.unit.test_junk_images import _Attachment
from tests.unit.test_pst_libpff import FakeAttachment, FakeFolder, FakeMessage, install_fake


class _Counting:
    """A stand-in reader that records every file it is handed."""

    def __init__(self, name, extensions):
        self.name, self.extensions, self.calls = name, extensions, []

    def extract(self, path: Path):
        self.calls.append(path.name)
        yield Document(path=path, text=f"Contents of {path.name}.", source_kind="file")


@pytest.fixture()
def readers():
    before = dict(base.REGISTRY)
    base.REGISTRY.get(".png")                  # load the real readers first
    for ext in (".png", ".docx", ".pdf", ".txt"):
        dict.pop(base.REGISTRY, ext, None)
    found = {"ocr": _Counting("ocr", (".png",)), "office": _Counting("docx", (".docx", ".pdf")),
             "text": _Counting("text", (".txt",))}
    for reader in found.values():
        base.register(reader)
    yield found
    base.REGISTRY.clear()
    base.REGISTRY.update(before)


def _zip(*names: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as out:
        for name in names:
            out.writestr(name, f"the contents of {name}")
    return buffer.getvalue()


def _read(monkeypatch, *attachments):
    message = FakeMessage(1, subject="Holiday", plain="See attached.",
                          headers="Message-ID: <m1@example.com>\nFrom: maya@example.com\n",
                          attachments=list(attachments))
    install_fake(monkeypatch, FakeFolder("", children=[FakeFolder("Inbox", messages=[message])]))
    with reading.reading(images=reading.IMAGES_READ):
        documents = list(pst_libpff.read_archive(Path("Archive2007.pst")))
    assert progress.frames() == []
    return {d.meta.get("attachment_name"): d for d in documents if d.meta.get("attachment_name")}


@pytest.mark.parametrize("name, expected", [
    ("report.docx", CONTENTS), ("Budget.XLSX", CONTENTS), ("deck.pptx", CONTENTS),
    ("old.doc", CONTENTS), ("old.xls", CONTENTS), ("old.ppt", CONTENTS),
    ("voucher.pdf", CONTENTS),
    ("photos.zip", NAMES_INSIDE),
    # Plain text, CSV and HTML are read too (the owner's second answer, 1 October).
    ("notes.txt", CONTENTS), ("costs.csv", CONTENTS), ("page.html", CONTENTS),
    ("receipt.jpg", NAME_ONLY), ("scan.png", NAME_ONLY), ("drawing.dwg", NAME_ONLY),
    ("installer.exe", NAME_ONLY), ("no-extension", NAME_ONLY),
])
def test_only_office_documents_and_pdfs_are_read(name, expected):
    assert rule(name) == expected


def test_an_office_document_and_a_pdf_are_read(monkeypatch, readers):
    found = _read(monkeypatch, FakeAttachment("Brief.docx", b"d"), FakeAttachment("Voucher.pdf", b"p"))
    assert sorted(readers["office"].calls) == ["Brief.docx", "Voucher.pdf"]
    assert "Contents of" in found["Voucher.pdf"].text


def test_a_picture_is_kept_by_name_and_never_ocrd(monkeypatch, readers):
    found = _read(monkeypatch, FakeAttachment("receipt.png", b"\x89PNG"))
    assert readers["ocr"].calls == []
    assert found["receipt.png"].text == "receipt.png"
    assert found["receipt.png"].meta["contents_read"] is False
    assert found["receipt.png"].virtual_path.endswith("/1/attachments/receipt.png")


def test_an_inline_picture_gets_no_row_at_all(monkeypatch, readers):
    found = _read(monkeypatch, _Attachment("image001.png", b"\x89PNG", inline=True))
    assert found == {} and readers["ocr"].calls == []


def test_plain_text_is_read(monkeypatch, readers):
    found = _read(monkeypatch, FakeAttachment("notes.txt", b"pump station notes"))
    assert readers["text"].calls == ["notes.txt"]
    assert "Contents of" in found["notes.txt"].text


def test_any_other_type_is_kept_by_name_and_never_opened(monkeypatch, readers):
    found = _read(monkeypatch, FakeAttachment("drawing.dwg", b"AC1032"))
    assert found["drawing.dwg"].text == "drawing.dwg"
    assert found["drawing.dwg"].meta["contents_read"] is False


def test_a_zip_gives_its_name_and_its_members_names_and_nothing_inside(monkeypatch, readers):
    data = _zip("italy/hotel.txt", "italy/flights.pdf", "itinerary.docx")
    found = _read(monkeypatch, FakeAttachment("Holiday 2007.zip", data))
    row = found["Holiday 2007.zip"]
    assert readers["text"].calls == [] and readers["office"].calls == []
    for word in ("Holiday 2007.zip", "hotel.txt", "flights.pdf", "itinerary.docx"):
        assert word in row.text
    assert row.meta == {**row.meta, "contents_read": False, "members": 3}
    assert len([name for name in found if name.startswith("Holiday")]) == 1


def test_a_damaged_zip_still_keeps_its_name():
    assert mail_attachments.names_inside(b"not a zip at all") == []
    row = mail_attachments.name_only_document("broken.zip", "pst://a/1",
                                              inside=mail_attachments.names_inside(b"x"))
    assert row.text == "broken.zip" and row.virtual_path == "pst://a/1/attachments/broken.zip"
