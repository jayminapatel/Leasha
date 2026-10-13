"""Order 1s section 4: the tail the index cut is kept, and an original can be read again.

Layer: L2, L3 and L7 (test)

`strip_quoted` cuts a message at its signature or its quoted chain so a thread is
indexed once - and with it went the one block enrichment reads for a person's title
and phone. 4a keeps that cut-off tail beside the message (`message_tails`, schema 42),
never in `chunks`, so search is exactly what it was; 4b reads an original message again,
read-only, for mail indexed before the tail was kept.
"""

from __future__ import annotations

import email.policy
import math
import sys
import types
from email.message import EmailMessage
from pathlib import Path

import pytest

from app.extract.quoting import TAIL_MAX_CHARS, strip_quoted, tail_for_storage
from app.storage import migrations
from app.storage.sqlite_store import SqliteStore

#: A mailbox with every kind of cut: a `-- ` signature padded with a tracking link, "Sent from my
#: iPhone", a confidentiality notice, a quoted chain, and one message with nothing to cut. In
#: this module, not under `tests/fixtures/`, so changing it reruns these tests and not the suite.
MAILBOX = """From ann@example.com Mon Sep 01 09:00:00 2025
Date: Mon, 1 Sep 2025 09:00:00 +0000
From: Ann Lee <ann@example.com>
To: owner@example.org
Subject: Site visit on Thursday
Message-ID: <tail1@example.com>

Thursday at ten works for us. I will bring the drawings.

-- {SPACE}
Ann Lee
Senior Engineer, Example Works Ltd
T +44 20 7946 0000
M +44 7700 900000

https://protect-eu.mimecast.com/s/abc123?domain=example.com


www.example.com

From bob@example.net Tue Sep 02 10:00:00 2025
Date: Tue, 2 Sep 2025 10:00:00 +0000
From: Bob Stone <bob@example.net>
To: owner@example.org
Subject: Re: Quote
Message-ID: <tail2@example.net>
In-Reply-To: <q0@example.org>
References: <q0@example.org>

Yes, approved.

Sent from my iPhone

From cara@example.co.uk Wed Sep 03 11:00:00 2025
Date: Wed, 3 Sep 2025 11:00:00 +0000
From: cara@example.co.uk
To: owner@example.org
Subject: Contract
Message-ID: <tail3@example.co.uk>

Please find the signed contract attached.

Cara Diaz | Legal Counsel
This email and any attachments are confidential and intended only for the
addressee. If you received it in error please delete it.

From dev@example.com Thu Sep 04 12:00:00 2025
Date: Thu, 4 Sep 2025 12:00:00 +0000
From: Dev Shah <dev@example.com>
To: owner@example.org
Subject: Re: Budget
Message-ID: <tail4@example.com>
References: <b0@example.org>

Fine by me, go ahead.

-----Original Message-----
From: Owner <owner@example.org>
Sent: 03 September 2025 18:00
To: Dev Shah <dev@example.com>
Subject: Budget

Can we agree the budget?

Owner Name
Mobile 07700 900111

From eve@example.com Fri Sep 05 13:00:00 2025
Date: Fri, 5 Sep 2025 13:00:00 +0000
From: eve@example.com
To: owner@example.org
Subject: Lunch
Message-ID: <tail5@example.com>

Lunch on Friday? No signature here at all.
"""

#: `chunks` for `MAILBOX` as the indexer wrote them **before section 4a** (commit e8df1ac),
#: `(path, ordinal, text)`. Keeping the tail must leave every one of them exactly as it was.
CHUNKS_BEFORE_TAILS = [
    [
        "tails.mbox/0",
        0,
        "Subject: Site visit on Thursday\nFrom: ann@example.com\nTo: owner@example.org\n\nThursday at ten works for us. I will bring the drawings."
    ],
    [
        "tails.mbox/1",
        0,
        "Subject: Re: Quote\nFrom: bob@example.net\nTo: owner@example.org\n\nYes, approved."
    ],
    [
        "tails.mbox/2",
        0,
        "Subject: Contract\nFrom: cara@example.co.uk\nTo: owner@example.org\n\nPlease find the signed contract attached.\n\nCara Diaz | Legal Counsel"
    ],
    [
        "tails.mbox/3",
        0,
        "Subject: Re: Budget\nFrom: dev@example.com\nTo: owner@example.org\n\nFine by me, go ahead."
    ],
    [
        "tails.mbox/4",
        0,
        "Subject: Lunch\nFrom: eve@example.com\nTo: owner@example.org\n\nLunch on Friday? No signature here at all."
    ]
]


# -- 4a: the cut is returned, not thrown away ------------------------------------

def test_a_signature_cut_hands_back_what_it_cut():
    result = strip_quoted("Yes, Thursday works.\n\n-- \nAnn Lee\nSenior Engineer\nM +44 7700 900000")
    assert result.text == "Yes, Thursday works."
    assert result.tail.startswith("-- \nAnn Lee")
    assert "Senior Engineer" in result.tail and result.reason.startswith("signature")


def test_a_quoted_chain_is_the_tail_when_it_comes_first():
    result = strip_quoted("Fine by me.\n\n-----Original Message-----\nFrom: x\nSent: y\nTo: z\n\nOld words.")
    assert result.text == "Fine by me." and result.reason.startswith("quoted")
    assert result.tail.startswith("-----Original Message-----") and "Old words." in result.tail


@pytest.mark.parametrize("text", ["Just a message with nothing to cut.",
                                  "-- \nA message that opens with the marker"])
def test_no_cut_means_no_tail(text):
    result = strip_quoted(text)
    assert result.tail == "" and result.text == text


def test_the_stored_tail_is_cleaned_and_capped():
    raw = ("-- \nAnn Lee\n\n\n\nSenior Engineer\n<https://protect-eu.mimecast.com/s/abc>\n"
           "https://example.com/track?id=1\n  www.example.com  \nM +44 7700 900000\n\n\n")
    # Lines are right-stripped as the readers already strip a body (`normalise_whitespace`), so
    # the RFC 3676 delimiter is stored as "--".
    assert tail_for_storage(raw) == "--\nAnn Lee\n\nSenior Engineer\nM +44 7700 900000"
    assert len(tail_for_storage("x" * (TAIL_MAX_CHARS * 3))) == TAIL_MAX_CHARS
    assert tail_for_storage("\n\nhttps://only.example.com\n") == ""


def test_the_document_carries_the_tail_and_its_text_is_unchanged():
    from app.extract.email_files import build_email_document

    body = "Thursday works.\n\n-- \nAnn Lee\nSenior Engineer"
    doc = build_email_document(Path("a.eml"), subject="S", sender="ann@example.com", recipients=[],
                               sent_at=1, conversation=None, body=body, attachments=[])
    assert "Senior Engineer" not in doc.text
    assert doc.meta["tail"] == "--\nAnn Lee\nSenior Engineer"
    assert doc.meta["tail_reason"].startswith("signature")
    plain = build_email_document(Path("b.eml"), subject="S", sender="a@b.c", recipients=[], sent_at=1,
                                 conversation=None, body="Nothing cut here.", attachments=[])
    assert plain.meta["tail"] == ""


# -- schema 42 -------------------------------------------------------------------

@pytest.fixture()
def store(tmp_path):
    s = SqliteStore(tmp_path / "k.db").connect()
    yield s
    s.close()


def test_schema_42_adds_the_tails_table(store):
    assert migrations.CURRENT_VERSION >= 42 and 42 in migrations.MIGRATIONS
    cols = {r[1] for r in store.conn.execute("PRAGMA table_info(message_tails)")}
    assert cols == {"file_id", "text", "reason"}
    store.conn.execute("DROP TABLE message_tails")
    migrations._v42_message_tails(store.conn)
    migrations._v42_message_tails(store.conn)                 # twice is harmless
    assert store.conn.execute("SELECT COUNT(*) FROM message_tails").fetchone()[0] == 0


def _message(store, path, *, tail="", reason="signature: --", quoted_removed=None, **fields):
    fid = store.upsert_file(path, size_bytes=1, mtime_ns=1, ext=Path(path).suffix.lstrip("."),
                            status="INDEXED", source_kind=fields.pop("source_kind", "eml"))
    store.replace_chunks(fid, [{"text": "Subject: S\nFrom: ann@example.com", "label": "Headers"},
                               {"text": "Thursday works.", "label": "Body"}])
    store.set_message(fid, subject="S", sender="ann@example.com", sent_at=100,
                      quoted_removed=quoted_removed, **fields)
    if tail:
        store.set_message_tail(fid, tail, reason)
    return fid


def test_a_tail_is_written_replaced_cleared_and_goes_with_its_file(store):
    fid = _message(store, "m/1.eml", tail="-- \nAnn Lee")
    assert store.message_tail(fid) == ("-- \nAnn Lee", "signature: --")
    store.set_message_tail(fid, "-- \nAnn Lee\nDirector", "signature: --")
    assert store.message_tail(fid)[0].endswith("Director")
    store.set_message_tail(fid, "", "")
    assert store.message_tail(fid) is None
    store.set_message_tail(fid, "-- \nAnn", "signature: --")
    store.delete_file(fid)
    assert store.conn.execute("SELECT COUNT(*) FROM message_tails").fetchone()[0] == 0


def test_a_tail_is_not_in_the_word_index(store):
    fid = _message(store, "m/1.eml", tail="-- \nAnn Lee\nZanzibarian Engineer")
    hits = store.conn.execute("SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH 'zanzibarian'").fetchall()
    assert hits == [] and fid


# -- the indexer: every message written keeps its tail; search is unchanged ------------

class _NullVectors:
    def delete_by_file_ids(self, file_ids):
        pass

    def add(self, **kwargs):
        return len(kwargs.get("chunk_ids") or ())

    def __getattr__(self, name):
        return lambda *args, **kwargs: None


def _index(store, root):
    from app.index.embedder import Embedder, l2_normalise
    from app.index.pipeline import Pipeline, PipelineConfig
    from app.index.walker import WalkConfig

    embedder = Embedder(dim=8, encoder=lambda ts: [l2_normalise([math.sin(len(t) + i) for i in range(8)])
                                                   for t in ts])
    return Pipeline(store, _NullVectors(), embedder, PipelineConfig(
        walk=WalkConfig(roots=[root]), workers=1, min_free_gb=0, required_free_gb=0)).run()


@pytest.fixture()
def mailbox(tmp_path):
    root = tmp_path / "mail"
    root.mkdir()
    # The RFC 3676 delimiter's trailing space is put back here, where no editor can strip it.
    (root / "tails.mbox").write_bytes(MAILBOX.replace("{SPACE}", "").encode("utf-8"))
    return root


def _chunks(store, root):
    rows = store.conn.execute("SELECT f.path, c.ordinal, c.text FROM chunks c JOIN files f ON f.id = c.file_id "
                              "ORDER BY f.path, c.ordinal").fetchall()
    prefix = str(root / "tails.mbox")
    return [[str(r[0]).replace(prefix, "tails.mbox").replace("\\", "/"), r[1], r[2]] for r in rows]


def _tails(store, root):
    prefix = str(root / "tails.mbox")
    return {str(r[0]).replace(prefix, "tails.mbox").replace("\\", "/"): (r[1], r[2]) for r in store.conn.execute(
        "SELECT f.path, t.text, t.reason FROM message_tails t JOIN files f ON f.id = t.file_id")}


def test_search_is_unchanged_the_chunks_match_the_ones_written_before_tails(tmp_path, mailbox):
    # Every row, in order, word for word: keeping the tail changed nothing searched.
    golden = CHUNKS_BEFORE_TAILS
    with SqliteStore(tmp_path / "i.db") as store:
        _index(store, mailbox)
        assert _chunks(store, mailbox) == golden


def test_every_cut_message_keeps_its_tail_and_an_uncut_one_has_none(tmp_path, mailbox):
    with SqliteStore(tmp_path / "i.db") as store:
        _index(store, mailbox)
        tails = _tails(store, mailbox)
    assert set(tails) == {"tails.mbox/0", "tails.mbox/1", "tails.mbox/2", "tails.mbox/3"}
    text, reason = tails["tails.mbox/0"]
    assert "Senior Engineer, Example Works Ltd" in text and "mimecast" not in text and "\n\n\n" not in text
    assert reason.startswith("signature")
    assert tails["tails.mbox/1"][0] == "Sent from my iPhone"
    assert tails["tails.mbox/3"][1].startswith("quoted") and "Mobile 07700 900111" in tails["tails.mbox/3"][0]


def _append(mbox: Path, n: int) -> None:
    with mbox.open("a", encoding="utf-8", newline="\n") as f:
        f.write(f"\nFrom zed@example.com Sat Sep 06 14:00:00 2025\nDate: Sat, 6 Sep 2025 14:00:00 +0000\n"
                f"From: zed@example.com\nTo: owner@example.org\nSubject: New {n}\nMessage-ID: <new{n}@x>\n\n"
                f"A new message {n}.\n")


def test_an_unchanged_message_keeps_its_tail_and_one_indexed_before_tails_gains_it(tmp_path, mailbox):
    mbox = mailbox / "tails.mbox"
    with SqliteStore(tmp_path / "i.db") as store:
        _index(store, mailbox)
        before = _tails(store, mailbox)
        # The archive changes (one new message), so its old messages are met again and skipped
        # by their text hash - the fast path. It must not lose the tails it has...
        _append(mbox, 1)
        stats = _index(store, mailbox)
        assert stats.unchanged_documents >= 4
        assert _tails(store, mailbox) == before
        # ...and a message indexed before schema 42, with no tail row, gets one on that pass.
        store.conn.execute("DELETE FROM message_tails")
        store.conn.commit()
        _append(mbox, 2)
        _index(store, mailbox)
        assert _tails(store, mailbox) == before


def test_a_rewritten_message_replaces_its_tail(tmp_path, mailbox):
    mbox = mailbox / "tails.mbox"
    with SqliteStore(tmp_path / "i.db") as store:
        _index(store, mailbox)
        text = mbox.read_text(encoding="utf-8").replace("Senior Engineer, Example Works Ltd", "Director, Example")
        text = text.replace("Yes, approved.\n\nSent from my iPhone", "Yes, approved after all.")
        mbox.write_text(text, encoding="utf-8", newline="\n")
        _index(store, mailbox)
        tails = _tails(store, mailbox)
    assert "Director, Example" in tails["tails.mbox/0"][0] and "Senior Engineer" not in tails["tails.mbox/0"][0]
    assert "tails.mbox/1" not in tails                         # the signature went, and so did its tail


def test_account_mail_keeps_its_tail(tmp_path):
    from app.accounts.mail_index import MailIndexer
    from app.accounts.provider import RemoteMessage
    from tests.unit.mail_sync_fakes import raw_message

    with SqliteStore(tmp_path / "index.db") as store:
        store.conn.execute("INSERT INTO accounts (provider, address, name, colour, added_at) "
                           "VALUES ('google','me@x.com','Home','#fff',0)")
        store.conn.commit()
        indexer = MailIndexer(store)
        msg = RemoteMessage("r1", raw_message("Hi", "Words about a heron.\n\n-- \nAnn Lee\nDirector"),
                            folder="INBOX", received_at=1_709_640_000)
        indexer.apply_page(1, [indexer.prepare(1, msg)], [])
        fid = store.get_file("account://1/INBOX/r1").id
        assert store.message_tail(fid)[0] == "--\nAnn Lee\nDirector"
        indexer.apply_page(1, [indexer.prepare(1, RemoteMessage(
            "r1", raw_message("Hi", "Words about a heron."), folder="INBOX", received_at=1_709_640_000))], [])
        assert store.message_tail(fid) is None


# -- full_text: the indexed body, then the tail; or the original, read again ----------

def test_full_text_is_the_body_then_the_tail(store):
    from app.contacts.enrich.base import body_text, full_text

    with_tail = _message(store, "m/1.eml", tail="-- \nAnn Lee\nDirector")
    without = _message(store, "m/2.eml")
    assert full_text(store, with_tail) == "Thursday works.\n-- \nAnn Lee\nDirector"
    assert full_text(store, without) == body_text(store, without) == "Thursday works."


def test_full_text_reads_an_index_from_before_schema_42(store):
    from app.contacts.enrich.base import full_text

    fid = _message(store, "m/1.eml")
    store.conn.execute("DROP TABLE message_tails")            # measured read-only on an older index
    assert full_text(store, fid) == "Thursday works."


def _eml(path: Path, body: str) -> Path:
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = "ann@example.com", "owner@example.org", "S"
    msg.set_content(body)
    path.write_bytes(msg.as_bytes(policy=email.policy.default))
    return path


def test_read_original_reads_the_whole_eml_again_read_only(store, tmp_path):
    from app.contacts.enrich.base import full_text

    path = _eml(tmp_path / "a.eml", "Thursday works.\n\n-- \nAnn Lee\nDirector\n")
    before = (path.read_bytes(), path.stat().st_mtime_ns)
    fid = _message(store, str(path), quoted_removed=30)
    assert full_text(store, fid) == "Thursday works."              # the switch off: nothing opened
    text = full_text(store, fid, read_original=True)
    assert text.startswith("Thursday works.") and text.rstrip().endswith("Ann Lee\nDirector")
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before   # non-negotiable 10


def test_read_original_is_not_needed_when_the_tail_is_kept_or_nothing_was_cut(store, tmp_path, monkeypatch):
    from app.contacts.enrich.base import full_text
    from app.extract import reread

    opened = []
    monkeypatch.setattr(reread, "original_body", lambda row: opened.append(row) or "NEVER")
    kept = _message(store, str(tmp_path / "k.eml"), tail="-- \nAnn", quoted_removed=10)
    whole = _message(store, str(tmp_path / "w.eml"), quoted_removed=0)
    assert full_text(store, kept, read_original=True) == "Thursday works.\n-- \nAnn"
    assert full_text(store, whole, read_original=True) == "Thursday works."
    assert opened == []


@pytest.mark.parametrize("path, fields", [
    ("account://1/INBOX/r9", {}),                                            # on the server: no network
    ("pst://2019/00000000ABCDEF", {"source_kind": "pst_message", "store_path": "D:/m/2019.pst",
                                    "entry_id": "00000000ABCDEF"}),         # an Outlook EntryID
    ("D:/nowhere/gone.eml", {}),                                             # not on disk any more
    ("D:/m/box.mbox/3", {}),                                                 # inside a mailbox
])
def test_read_original_falls_back_to_the_index_where_it_cannot_open_safely(store, path, fields):
    from app.contacts.enrich.base import full_text

    fid = _message(store, path, quoted_removed=40, **fields)
    assert full_text(store, fid, read_original=True) == "Thursday works."


def test_read_original_takes_a_pst_message_from_where_the_index_says(store, monkeypatch):
    from app.contacts.enrich.base import full_text
    from app.extract import pst_libpff
    from tests.unit.test_pst_libpff import FakeArchive, FakeFolder, FakeMessage

    target = FakeMessage(identifier=2097188, plain="Thursday works.\r\n\r\n-- \r\nAnn Lee\r\nDirector")
    inbox = FakeFolder("Inbox", messages=[FakeMessage(identifier=5), target])
    archive = FakeArchive(FakeFolder("Top of Personal Folders", children=[inbox]))
    module = types.ModuleType("pypff")
    module.file = lambda: archive                                  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "pypff", module)
    monkeypatch.setattr(pst_libpff, "available", lambda: True)
    fid = _message(store, "pst://2019/2097188", quoted_removed=40, source_kind="pst_message",
                   store_path="D:/m/2019.pst", entry_id="2097188",
                   folder_path="Top of Personal Folders/Inbox", folder_index=1)
    text = full_text(store, fid, read_original=True)
    assert text.rstrip().endswith("Ann Lee\nDirector") and archive.closed
