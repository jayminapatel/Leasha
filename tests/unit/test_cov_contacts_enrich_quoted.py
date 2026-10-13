"""Signatures quoted in other people's mail: whose block is whose, its date, and which messages are read.

Order 1u section 4d. `app.contacts.enrich.quoted` had no test that imported it (static scan 2026-10-13). These pin
its promises: a quoted block is credited only to the address in its own From line, a From line with no
address credits nobody, the Sent line is read in the shapes Outlook and Gmail write, and `mail_quoting`
returns the messages *to* the person and not from them, newest first, inside the window, through the
trigram index or the scan that stands in for it.
"""

from __future__ import annotations

import json
from calendar import timegm

import pytest

from app.contacts.enrich import quoted
from app.storage.sqlite_store import SqliteStore


def _utc(y, mo, d, h=0, mi=0):
    return timegm((y, mo, d, h, mi, 0))


# ---------------------------------------------------------------- the Sent line

@pytest.mark.parametrize("text, expected", [
    ("Monday, 6 October 2026 14:05", _utc(2026, 10, 6, 14, 5)),
    ("6 October 2026 2:05 PM", _utc(2026, 10, 6, 14, 5)),
    ("Monday, October 6, 2026 2:05 PM", _utc(2026, 10, 6, 14, 5)),
    ("Mon, 6 Oct 2026 at 14:05", _utc(2026, 10, 6, 14, 5)),
    ("2026-10-06 14:05", _utc(2026, 10, 6, 14, 5)),
    ("6 October 2026", _utc(2026, 10, 6)),
    ("Mon, 06 Oct 2026 16:05:00 +0200", _utc(2026, 10, 6, 14, 5)),       # RFC 2822, with its zone
])
def test_a_quoted_sent_line_is_read_in_the_shapes_mail_programs_write(text, expected):
    assert quoted.parse_sent(text) == expected


@pytest.mark.parametrize("text", ["", "sometime last week", "Monday"])
def test_an_unreadable_sent_line_is_no_date_not_an_error(text):
    assert quoted.parse_sent(text) is None


# ---------------------------------------------------------------- the quoted blocks

THREAD = """Thanks both, see you then.

Bob

From: Ann Lee <Ann.Lee@Acme.example>
Sent: Monday, 6 October 2026 14:05
To: Bob Ray <bob@ray.example>
Subject: RE: Meeting

> Tuesday works.
>
> Ann Lee
> Senior Engineer

From: Cy Dee [mailto:cy@dee.example]
Sent: 5 October 2026 09:00
To: Ann Lee
Subject: Meeting

Can we meet?
Cy

From: Exchange Person
Sent: 4 October 2026 09:00
Subject: Earlier

No address here.
"""


def test_each_quoted_block_is_credited_to_the_address_in_its_own_from_line_only():
    blocks = quoted.quoted_messages(THREAD)

    assert [b.address for b in blocks] == ["ann.lee@acme.example", "cy@dee.example", ""]
    assert [b.sent_at for b in blocks] == [_utc(2026, 10, 6, 14, 5), _utc(2026, 10, 5, 9), _utc(2026, 10, 4, 9)]


def test_a_quoted_block_keeps_its_own_text_with_the_quote_marks_removed_and_stops_at_the_next():
    ann, cy, _nobody = quoted.quoted_messages(THREAD)

    assert "Tuesday works." in ann.text and "> " not in ann.text
    assert "Senior Engineer" in ann.text
    assert "Can we meet?" not in ann.text
    assert "Can we meet?" in cy.text and "Tuesday works." not in cy.text


def test_the_replying_senders_own_words_are_never_a_quoted_block():
    blocks = quoted.quoted_messages(THREAD)

    assert all("see you then" not in b.text for b in blocks)
    assert quoted.quoted_messages("Just a note, nothing quoted.\nFrom me, Bob") == []


def test_a_from_line_without_a_header_block_beneath_it_is_not_a_quoted_message():
    assert quoted.quoted_messages("From: Ann Lee <ann@acme.example>\n\nHello") == []


# ---------------------------------------------------------------- which messages are read

ANN = "ann@acme.example"


@pytest.fixture()
def store(tmp_path):
    s = SqliteStore(tmp_path / "q.db").connect()
    yield s
    s.close()


_n = [0]


def _mail(store, sender, to, sent):
    _n[0] += 1
    fid = store.upsert_file(f"mail/{_n[0]}.eml", parent_dir="mail", ext="eml", size_bytes=1, mtime_ns=_n[0],
                            status="INDEXED", source_kind="eml")
    store.set_message(fid, subject=f"m{_n[0]}", sender=sender, recipients=json.dumps(list(to)), sent_at=sent)
    return fid


@pytest.fixture(params=["indexed", "scan"])
def lookup(request, store):
    """Both ways in: the trigram index, and the scan an index without `messages_fts` falls back to."""
    if request.param == "scan":
        store._enrich_messages_fts = False
    return request.param


def test_the_messages_to_the_person_and_not_from_them_are_found_newest_first(store, lookup):
    old = _mail(store, "Bob <bob@ray.example>", [ANN, "me@home.example"], _utc(2026, 9, 1))
    new = _mail(store, "Cy <cy@dee.example>", [f"Ann Lee <{ANN}>"], _utc(2026, 10, 1))
    _mail(store, f"Ann <{ANN}>", ["bob@ray.example", ANN], _utc(2026, 10, 2))       # from her, copied to herself
    _mail(store, "Bob <bob@ray.example>", ["someone@else.example"], _utc(2026, 10, 3))

    rows = quoted.mail_quoting(store, ["Ann@Acme.example"])

    assert [r.file_id for r in rows] == [new, old]


def test_the_window_and_the_limit_bound_what_is_read(store, lookup):
    ids = [_mail(store, "Bob <bob@ray.example>", [ANN], _utc(2026, 1 + m, 1)) for m in range(6)]

    since = _utc(2026, 4, 1)
    assert [r.file_id for r in quoted.mail_quoting(store, [ANN], since=since)] == ids[:2:-1]
    assert [r.file_id for r in quoted.mail_quoting(store, [ANN], limit=2)] == [ids[5], ids[4]]


def test_no_address_reads_nothing(store, lookup):
    _mail(store, "Bob <bob@ray.example>", [ANN], _utc(2026, 1, 1))

    assert quoted.mail_quoting(store, ["", None]) == []
