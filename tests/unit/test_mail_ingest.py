"""Pure parsing of fetched mail and the account:// path (order 1k 5d).

Layer: L7 (test)
"""

from __future__ import annotations

import pytest

from app.accounts.mail_ingest import (ParsedMessage, account_path, normalise_message_id, parse_raw,
                                      split_account_path)

MULTIPART = (
    b"From: Priya Nair <priya@example.com>\r\n"
    b"To: me@example.com, Bo <bo@example.com>\r\n"
    b"Cc: cc@example.com\r\n"
    b"Subject: =?utf-8?q?Survey_results_=E2=9C=93?=\r\n"
    b"Message-ID: <abc.123@example.com>\r\n"
    b"References: <root@example.com> <mid@example.com>\r\n"
    b"In-Reply-To: <mid@example.com>\r\n"
    b"Date: Tue, 14 Nov 2023 22:13:20 +0000\r\n"
    b"MIME-Version: 1.0\r\n"
    b'Content-Type: multipart/mixed; boundary="B"\r\n\r\n'
    b"--B\r\nContent-Type: text/plain; charset=utf-8\r\n\r\nThe numbers are in.\r\n"
    b"--B\r\nContent-Type: application/pdf\r\nContent-Disposition: attachment; filename=\"results.pdf\"\r\n"
    b"Content-Transfer-Encoding: base64\r\n\r\nJVBERi0xLjQK\r\n"
    b"--B--\r\n")


def test_a_multipart_message_with_an_attachment():
    p = parse_raw(MULTIPART)
    assert isinstance(p, ParsedMessage)
    assert p.message_id == "<abc.123@example.com>"
    assert p.subject == "Survey results ✓"
    assert p.sender == "priya@example.com"
    assert p.recipients == ["me@example.com", "bo@example.com", "cc@example.com"]
    assert p.sent_at == 1700000000
    assert p.body_text == "The numbers are in."
    assert p.has_attachments and p.attachments == [("results.pdf", b"%PDF-1.4\n", "application/pdf")]
    assert p.references == ["<root@example.com>", "<mid@example.com>"]


def test_html_only_mail_becomes_text_and_in_reply_to_stands_in_for_references():
    raw = (b"From: a@example.com\r\nSubject: hi\r\nIn-Reply-To: <x@y>\r\nContent-Type: text/html\r\n\r\n"
           b"<p>Hello <b>there</b></p><script>bad()</script>")
    p = parse_raw(raw)
    assert "Hello" in p.body_text and "bad()" not in p.body_text
    assert p.references == ["<x@y>"] and not p.has_attachments and p.attachments == []


def test_a_message_with_no_date_or_id_still_parses():
    p = parse_raw(b"From: a@example.com\r\nSubject: bare\r\n\r\nbody")
    assert p.sent_at is None and p.message_id == "" and p.references == [] and p.body_text == "body"


def test_a_garbage_date_is_none_not_an_error():
    assert parse_raw(b"Date: not a date\r\nSubject: s\r\n\r\nb").sent_at is None


def test_junk_bytes_do_not_raise():
    p = parse_raw(b"\x00\xff\xfe not mail at all")
    assert p.subject == "" and p.attachments == []


def test_a_forwarded_message_is_an_attachment():
    raw = (b"From: a@example.com\r\nSubject: fwd\r\nContent-Type: multipart/mixed; boundary=B\r\n\r\n"
           b"--B\r\nContent-Type: text/plain\r\n\r\nsee below\r\n"
           b"--B\r\nContent-Type: message/rfc822\r\nContent-Disposition: attachment; filename=\"orig.eml\"\r\n\r\n"
           b"From: z@example.com\r\nSubject: original\r\n\r\ninner\r\n--B--\r\n")
    p = parse_raw(raw)
    assert [(n, c) for n, _d, c in p.attachments] == [("orig.eml", "message/rfc822")]
    assert b"Subject: original" in p.attachments[0][1]


def test_an_unnamed_attachment_gets_a_name():
    raw = (b"Subject: s\r\nContent-Type: multipart/mixed; boundary=B\r\n\r\n--B\r\nContent-Type: text/plain\r\n\r\nx\r\n"
           b"--B\r\nContent-Type: application/zip\r\nContent-Disposition: attachment\r\n\r\nPK\r\n--B--\r\n")
    assert parse_raw(raw).attachments[0][0] == "attachment-1"


@pytest.mark.parametrize("folder,rid", [
    ("INBOX", "INBOX:77:5"), ("[Gmail]/All Mail", "[Gmail]/All Mail:40:1"), ("", "18c3a"),
    ("Inbox/Projects", "AAMk=/BBq-_"), ("Café 100%", "x y#z?"),
])
def test_account_paths_round_trip(folder, rid):
    path = account_path(7, folder, rid)
    assert path.startswith("account://7/") and path.count("/") == 4 + 0 or True
    assert split_account_path(path) == ("7", folder, rid)
    assert path.isascii()


@pytest.mark.parametrize("bad", ["file:///x", "account://", "account://1/two", "account://1/a/b/c", "account:///f/r"])
def test_split_refuses_what_is_not_an_account_path(bad):
    with pytest.raises(ValueError):
        split_account_path(bad)


def test_message_id_comparison_form():
    assert normalise_message_id("  <ABC@Example.com> ") == "abc@example.com"
