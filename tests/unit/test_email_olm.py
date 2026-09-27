"""Outlook for Mac `.olm` exports (work order 0x, section 8b and 8c).

Layer: L2

Every fixture is a small zip built here in `tmp_path`, laid out the way public
descriptions of the format say a real export is - the element names are
**(UNCONFIRMED)** and so is everything these tests assume about them. Checking
against a real export is on `docs/MAC_VERIFICATION.md`.
"""

from __future__ import annotations

import zipfile
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr

import pytest

from app.core.errors import AppErrorException
from app.extract import email_olm
from app.extract import extract
from app.extract.base import SourceKind, extractor_for
from app.extract.email_olm import OlmExtractor

FOLDER = "Accounts/priya@example.com/com.microsoft.__Messages/Inbox"


def _address(tag: str, *pairs: tuple[str, str]) -> str:
    inner = "".join(
        f"<emailAddress OPFContactEmailAddressAddress={quoteattr(a)} "
        f"OPFContactEmailAddressName={quoteattr(n)}/>" for a, n in pairs)
    return f"<{tag}>{inner}</{tag}>"


def _message_xml(i: int, *, html: bool = False, extra: str = "") -> str:
    """One message member, in the assumed `<emails><email>` shape."""
    markup = escape(f"<html><body><p>Leeds note {i}</p></body></html>")
    body = (f"<OPFMessageCopyHTMLBody>{markup}</OPFMessageCopyHTMLBody>" if html
            else f"<OPFMessageCopyBody>Body of message {i} about Leeds.</OPFMessageCopyBody>")
    return (
        "<?xml version='1.0' encoding='UTF-8'?>"
        "<emails><email>"
        f"<OPFMessageCopySubject>Message {i}</OPFMessageCopySubject>"
        + _address("OPFMessageCopyFromAddresses", (f"sender{i}@example.com", f"Sender {i}"))
        + _address("OPFMessageCopyToAddresses", ("sam@example.com", "Sam"))
        + _address("OPFMessageCopyCCAddresses", ("lee@example.com", "Lee"))
        + "<OPFMessageCopySentTime>2025-06-03T09:15:00Z</OPFMessageCopySentTime>"
        f"<OPFMessageCopyMessageID>&lt;m{i}@example.com&gt;</OPFMessageCopyMessageID>"
        + body + extra +
        "<OPFSomethingNobodyDocumented>ignored</OPFSomethingNobodyDocumented>"
        "</email></emails>"
    )


def _olm(path: Path, count: int, *, extra_members: dict[str, str] | None = None) -> Path:
    """An `.olm` with `count` messages plus any extra raw members."""
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("Categories.xml", "<categories/>")
        for i in range(count):
            archive.writestr(f"{FOLDER}/message_{i:05d}.xml", _message_xml(i))
        for name, content in (extra_members or {}).items():
            archive.writestr(name, content)
    return path


def _keys(documents: list) -> list[tuple[str, str]]:
    return [(d.virtual_path, d.text) for d in documents]


def test_olm_is_claimed_and_resumable() -> None:
    extractor = extractor_for(Path("export.olm"))
    assert isinstance(extractor, OlmExtractor)
    assert extractor.supports_resume is True


def test_resume_key_matches_the_pipelines() -> None:
    """The pipeline reads the position under this exact key; drift = no resume."""
    from app.index.pipeline import RESUME_POSITION_META_KEY

    assert email_olm.RESUME_POSITION_META_KEY == RESUME_POSITION_META_KEY


def test_headers_body_and_date_are_parsed(tmp_path: Path) -> None:
    path = _olm(tmp_path / "export.olm", 1)
    [doc] = list(extract(path))
    assert doc.meta["subject"] == "Message 0"
    assert doc.meta["sender"] == "sender0@example.com"
    assert doc.meta["recipients"] == '["sam@example.com", "lee@example.com"]'
    assert doc.meta["sent_at"] == 1748942100
    assert doc.meta["conversation"] == "<m0@example.com>"
    assert doc.meta["folder_path"] == "Inbox"
    assert doc.meta["mbox_index"] == 0
    assert "Body of message 0 about Leeds." in doc.text
    assert doc.source_kind == SourceKind.EML
    assert doc.virtual_path == f"{path}/{FOLDER}/message_00000.xml"
    assert doc.warnings == ()


def test_html_body_is_turned_into_text(tmp_path: Path) -> None:
    path = tmp_path / "html.olm"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(f"{FOLDER}/message_00000.xml", _message_xml(0, html=True))
    [doc] = list(extract(path))
    assert "Leeds note 0" in doc.text
    assert "<p>" not in doc.text


def test_attachment_names_are_listed(tmp_path: Path) -> None:
    extra = ("<OPFMessageCopyAttachmentList>"
             "<messageAttachment OPFAttachmentName='plans.pdf' "
             "OPFAttachmentContentType='application/pdf'/>"
             "</OPFMessageCopyAttachmentList>")
    path = tmp_path / "attach.olm"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(f"{FOLDER}/message_00000.xml", _message_xml(0, extra=extra))
    [doc] = list(extract(path))
    assert doc.meta["attachment_names"] == ["plans.pdf"]


def test_missing_elements_are_fine(tmp_path: Path) -> None:
    """A message with only a subject is still a message (defensive parsing)."""
    path = tmp_path / "sparse.olm"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(f"{FOLDER}/message_00000.xml",
                         "<emails><email><OPFMessageCopySubject>Only this"
                         "</OPFMessageCopySubject></email></emails>")
    [doc] = list(extract(path))
    assert doc.meta["subject"] == "Only this"
    assert doc.meta["sender"] == ""
    assert doc.meta["sent_at"] is None


def test_messages_are_streamed_in_member_name_order(tmp_path: Path) -> None:
    path = _olm(tmp_path / "export.olm", 5)
    documents = list(OlmExtractor().extract(path))
    assert [d.meta["subject"] for d in documents] == [f"Message {i}" for i in range(5)]
    assert [d.meta["mbox_index"] for d in documents] == list(range(5))


def test_resume_skips_earlier_messages_without_parsing_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _olm(tmp_path / "export.olm", 10)
    opened: list[str] = []
    real = email_olm._read_message

    def counting(archive, entry):
        opened.append(entry.filename)
        return real(archive, entry)

    monkeypatch.setattr(email_olm, "_read_message", counting)
    documents = list(extract(path, resume_from=6))
    assert [d.meta["mbox_index"] for d in documents] == [6, 7, 8, 9]
    assert opened == [f"{FOLDER}/message_{i:05d}.xml" for i in range(6, 10)]


def test_a_resumed_read_matches_an_uninterrupted_one(tmp_path: Path) -> None:
    """Interrupted after message 3, resumed from 4: the union is identical,
    key for key and text for text, to one straight read."""
    path = _olm(tmp_path / "export.olm", 8)
    whole = list(extract(path))
    first = list(extract(path))[:4]                      # "killed" after 4
    rest = list(extract(path, resume_from=first[-1].meta["mbox_index"] + 1))
    assert _keys(first + rest) == _keys(whole)
    assert [d.meta for d in first + rest] == [d.meta for d in whole]


def test_a_traversal_member_is_refused_and_the_rest_are_read(tmp_path: Path) -> None:
    path = _olm(tmp_path / "export.olm", 2, extra_members={
        "../../evil/message_99999.xml": _message_xml(99),
    })
    documents = list(extract(path))
    subjects = [d.meta["subject"] for d in documents]
    assert "Message 99" not in subjects
    assert subjects == ["Message 0", "Message 1"]
    # Counted, and said, on the last message - not silently dropped.
    [warning] = documents[-1].warnings
    assert warning.code == "ERR_MAIL_PARTIAL"
    assert "refused" in warning.message


def test_a_bomb_member_is_refused_from_the_header(tmp_path: Path) -> None:
    """Archive.py's own `_refusal` decides it - a claimed 2MB expanding
    thousands to one - so nothing is decompressed to decline it."""
    path = _olm(tmp_path / "export.olm", 1, extra_members={
        f"{FOLDER}/message_zzzz.xml": "<emails><email><OPFMessageCopySubject>"
                                      + "A" * (2 * 1024 * 1024)
                                      + "</OPFMessageCopySubject></email></emails>",
    })
    documents = list(extract(path))
    assert [d.meta["subject"] for d in documents] == ["Message 0"]
    assert documents[-1].warnings[0].code == "ERR_MAIL_PARTIAL"


def test_one_damaged_message_never_stops_the_rest(tmp_path: Path) -> None:
    path = _olm(tmp_path / "export.olm", 3, extra_members={
        f"{FOLDER}/message_00001b.xml": "<emails><email><unclosed>",
    })
    documents = list(extract(path))
    assert [d.meta["subject"] for d in documents] == ["Message 0", "Message 1", "Message 2"]
    assert documents[-1].warnings[0].code == "ERR_MAIL_PARTIAL"
    assert "could not be read" in documents[-1].warnings[0].message


def test_a_file_that_is_not_a_zip_is_a_structured_skip(tmp_path: Path) -> None:
    path = tmp_path / "broken.olm"
    path.write_bytes(b"this is not a zip at all" * 100)
    with pytest.raises(AppErrorException) as caught:
        list(extract(path))
    assert caught.value.error.code == "ERR_FILE_CORRUPT"
    assert not caught.value.error.is_fatal


def test_an_export_where_nothing_is_readable_is_corrupt_not_empty(tmp_path: Path) -> None:
    path = tmp_path / "all-bad.olm"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(f"{FOLDER}/message_00000.xml", "<broken")
    with pytest.raises(AppErrorException) as caught:
        list(extract(path))
    assert caught.value.error.code == "ERR_FILE_CORRUPT"


def test_member_count_ceiling_is_checked_before_listing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _olm(tmp_path / "export.olm", 3)
    monkeypatch.setattr(email_olm, "_member_limit", lambda: 2)
    with pytest.raises(AppErrorException) as caught:
        list(extract(path))
    assert caught.value.error.code == "ERR_ARCHIVE_TOO_LARGE"


def test_reading_never_modifies_the_export(tmp_path: Path) -> None:
    """Non-negotiable #10."""
    path = _olm(tmp_path / "export.olm", 2)
    before = path.read_bytes()
    list(extract(path))
    assert path.read_bytes() == before
