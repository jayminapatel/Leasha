"""Apple Mail `.emlx` and `.partial.emlx` (work order 0x, section 8a and 8c).

Layer: L2

Every fixture is built here, in `tmp_path`, from the published description of
the format - a byte-count line, that many bytes of RFC 822 message, then an
Apple XML plist. No binary fixture is committed. Checking against a real
`~/Library/Mail/V10` folder is on `docs/MAC_VERIFICATION.md` (UNCONFIRMED on
macOS).
"""

from __future__ import annotations

import plistlib
from pathlib import Path

import pytest

from app.core.errors import AppErrorException
from app.extract import extract
from app.extract.base import SourceKind, extractor_for
from app.extract.email_emlx import EmlxExtractor, split_emlx

MESSAGE = (
    b"Date: Tue, 3 Jun 2025 09:15:00 +0000\r\n"
    b"From: Priya Shah <priya@example.com>\r\n"
    b"To: sam@example.com\r\n"
    b"Cc: lee@example.com\r\n"
    b"Subject: Survey results for Leeds\r\n"
    b"Message-ID: <root@example.com>\r\n"
    b"\r\n"
    b"The survey came back positive for the Leeds site.\r\n"
    b"\r\n"
    b"On Mon, Sam wrote:\r\n"
    b"> earlier text that should be stripped as a quote\r\n"
)

PARTIAL_MESSAGE = (
    b"Date: Tue, 3 Jun 2025 09:15:00 +0000\r\n"
    b"From: priya@example.com\r\n"
    b"To: sam@example.com\r\n"
    b"Subject: Site plans\r\n"
    b"MIME-Version: 1.0\r\n"
    b"Content-Type: multipart/mixed; boundary=\"XX\"\r\n"
    b"\r\n"
    b"--XX\r\n"
    b"Content-Type: text/plain\r\n"
    b"\r\n"
    b"Plans attached for the Leeds extension.\r\n"
    b"--XX\r\n"
    b"Content-Type: application/pdf\r\n"
    b"Content-Disposition: attachment; filename=\"plans.pdf\"\r\n"
    b"X-Apple-Content-Length: 482113\r\n"
    b"\r\n"
    b"\r\n"
    b"--XX--\r\n"
)


def _plist(**values: object) -> bytes:
    """The trailing property list, in Apple's XML flavour."""
    return plistlib.dumps({"flags": 8590195713, **values}, fmt=plistlib.FMT_XML)


def _emlx(path: Path, message: bytes, *, plist: bytes | None = None,
          declared: int | None = None) -> Path:
    """Write an `.emlx`: count line, message, plist - exactly as Apple Mail does."""
    count = len(message) if declared is None else declared
    path.write_bytes(f"{count}\n".encode() + message + (plist if plist is not None else _plist()))
    return path


def _eml(path: Path, message: bytes) -> Path:
    path.write_bytes(message)
    return path


def test_both_names_are_claimed_by_the_emlx_reader() -> None:
    assert isinstance(extractor_for(Path("12345.emlx")), EmlxExtractor)
    assert isinstance(extractor_for(Path("12345.partial.emlx")), EmlxExtractor)


def test_split_reads_the_count_line_and_leaves_the_plist() -> None:
    raw = b"5\nHELLO<plist/>"
    assert split_emlx(raw) == (b"HELLO", b"<plist/>", 5)


def test_headers_body_and_date_are_parsed(tmp_path: Path) -> None:
    [doc] = list(extract(_emlx(tmp_path / "1.emlx", MESSAGE)))
    assert doc.meta["subject"] == "Survey results for Leeds"
    assert doc.meta["sender"] == "priya@example.com"
    assert doc.meta["recipients"] == '["sam@example.com", "lee@example.com"]'
    assert doc.meta["sent_at"] == 1748942100
    assert doc.meta["conversation"] == "<root@example.com>"
    assert "survey came back positive" in doc.text
    assert doc.source_kind == SourceKind.EML
    assert doc.meta["attachments_downloaded"] is True
    assert doc.warnings == ()


def test_the_document_is_identical_in_shape_to_the_same_message_as_eml(tmp_path: Path) -> None:
    """The whole point of 8a: an Apple Mail message is indexed as an `.eml` is.

    Same text (so quote-stripping ran identically), same metadata, same kind.
    The one extra key is `attachments_downloaded`.
    """
    [emlx] = list(extract(_emlx(tmp_path / "1.emlx", MESSAGE)))
    [eml] = list(extract(_eml(tmp_path / "1.eml", MESSAGE)))
    assert emlx.text == eml.text
    assert emlx.segments == eml.segments
    assert emlx.source_kind == eml.source_kind
    extra = {k: v for k, v in emlx.meta.items() if k != "attachments_downloaded"}
    assert extra == eml.meta
    assert eml.meta["quoted_removed"] > 0         # quoting removal really happened


def test_received_date_from_the_plist_is_used_when_there_is_no_date_header(tmp_path: Path) -> None:
    undated = b"From: a@example.com\r\nSubject: No date\r\n\r\nBody text here.\r\n"
    path = _emlx(tmp_path / "2.emlx", undated, plist=_plist(**{"date-received": 1700000000}))
    [doc] = list(extract(path))
    assert doc.meta["sent_at"] == 1700000000


def test_a_damaged_plist_costs_only_the_fallback_date(tmp_path: Path) -> None:
    path = _emlx(tmp_path / "3.emlx", MESSAGE, plist=b"<?xml version='1.0'?><plist><dict>")
    [doc] = list(extract(path))
    assert doc.meta["subject"] == "Survey results for Leeds"


def test_partial_is_indexed_and_says_its_attachments_were_not_downloaded(tmp_path: Path) -> None:
    [doc] = list(extract(_emlx(tmp_path / "7.partial.emlx", PARTIAL_MESSAGE)))
    assert "Plans attached for the Leeds extension" in doc.text
    assert doc.meta["attachment_names"] == ["plans.pdf"]
    assert doc.meta["attachments_downloaded"] is False
    [warning] = doc.warnings
    assert warning.code == "ERR_MAIL_ATTACHMENTS_NOT_DOWNLOADED"
    assert not warning.is_fatal
    assert "plans.pdf" in warning.message


def test_a_first_line_that_is_not_a_count_is_a_structured_skip(tmp_path: Path) -> None:
    """Non-negotiable #3: a damaged file raises a structured, non-fatal error."""
    path = tmp_path / "bad.emlx"
    path.write_bytes(b"not a number\n" + MESSAGE)
    with pytest.raises(AppErrorException) as caught:
        list(extract(path))
    assert caught.value.error.code == "ERR_FILE_CORRUPT"
    assert not caught.value.error.is_fatal


def test_binary_junk_with_no_newline_is_a_structured_skip(tmp_path: Path) -> None:
    path = tmp_path / "junk.emlx"
    path.write_bytes(b"\x00\xff" * 5000)
    with pytest.raises(AppErrorException) as caught:
        list(extract(path))
    assert caught.value.error.code == "ERR_FILE_CORRUPT"


def test_a_file_cut_short_is_indexed_as_far_as_it_goes_and_warned(tmp_path: Path) -> None:
    path = _emlx(tmp_path / "4.emlx", MESSAGE, plist=b"", declared=len(MESSAGE) + 500)
    [doc] = list(extract(path))
    assert doc.meta["subject"] == "Survey results for Leeds"
    assert [w.code for w in doc.warnings] == ["ERR_MAIL_PARTIAL"]


def test_an_empty_message_is_no_text_not_success(tmp_path: Path) -> None:
    path = _emlx(tmp_path / "5.emlx", b"\r\n")
    with pytest.raises(AppErrorException) as caught:
        list(extract(path))
    assert caught.value.error.code == "ERR_NO_TEXT_LAYER"


def test_reading_never_modifies_the_file(tmp_path: Path) -> None:
    """Non-negotiable #10."""
    path = _emlx(tmp_path / "6.partial.emlx", PARTIAL_MESSAGE)
    before = path.read_bytes()
    list(extract(path))
    assert path.read_bytes() == before
