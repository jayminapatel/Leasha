"""Order 1s section 1: `contacts enrich --measure`, the funnel as a command.

Layer: L7

The acceptance instrument for the whole order, so what it must never do is pinned
as hard as what it counts: it opens the index read-only (the file's bytes are the
same afterwards), never through `SqliteStore.connect()` (which migrates), and never
opens a socket.
"""

from __future__ import annotations

import csv
import hashlib
import json
import socket
import sqlite3
import time
from calendar import timegm
from datetime import date

import pytest

from app.contacts.card import Card, Field
from app.contacts.enrich.measure import ReadOnlyStore, measure, window_start, write_sample
from app.contacts.hub import ContactHub
from app.storage.sqlite_store import SqliteStore

SIGNED = """Hi Bob,

Please find the figures attached.

Kind regards,

Ann Lee
Senior Engineer
Acme Widgets Ltd
T: 020 7946 0018 | M: 07700 900123
www.acme-widgets.co.uk
"""

UNSIGNED = "Thanks, see you Tuesday."


def _epoch(y, m, d):
    return timegm((y, m, d, 12, 0, 0))


def _person(hub, given, email=None):
    fields = [Field("email", email, "work", primary=True)] if email else []
    return hub.create(Card(scalars={"given": given, "family": "Lee"}, fields=fields))


_n = [0]


def _mail(store, sender, body, sent):
    _n[0] += 1
    fid = store.upsert_file(f"mail/{_n[0]}.eml", parent_dir="mail", ext="eml", size_bytes=10, mtime_ns=_n[0],
                            status="INDEXED", source_kind="eml")
    store.set_message(fid, subject=f"Note {_n[0]}", sender=sender, recipients=json.dumps(["me@home.example"]),
                      sent_at=sent)
    store.replace_chunks(fid, [{"ordinal": 0, "text": "Subject: x\nFrom: y", "label": "Headers"},
                               {"ordinal": 1, "text": body, "label": "Body"}])


@pytest.fixture()
def book(tmp_path):
    """An index with five people, written the way the indexer writes, then closed."""
    path = tmp_path / "knowledge.db"
    store = SqliteStore(path).connect()
    hub = ContactHub(store)
    recent = int(time.time()) - 10 * 86400
    _person(hub, "Ann", "ann@acme-widgets.co.uk")
    _mail(store, "Ann Lee <ann@acme-widgets.co.uk>", SIGNED, recent)
    _person(hub, "Bob", "bob@acme-widgets.co.uk")
    _mail(store, "bob@acme-widgets.co.uk", UNSIGNED, recent)
    _person(hub, "Cat", "cat@acme-widgets.co.uk")                    # no mail at all
    _person(hub, "Dan")                                                # no address
    _person(hub, "Eve", "eve@acme-widgets.co.uk")
    _mail(store, "eve@acme-widgets.co.uk", SIGNED.replace("Ann", "Eve"), _epoch(2019, 1, 1))   # years old
    store.close()
    return path


def _run(path, months=0, **kw):
    store = ReadOnlyStore(path)
    try:
        return measure(store, ContactHub(store), None, months=months, **kw)
    finally:
        store.close()


def test_the_funnel_counts_each_step_like_the_orders_table(book):
    fun = _run(book)
    assert (fun.live, fun.with_email, fun.with_mail, fun.block_found, fun.read, fun.proposed) == (5, 4, 3, 2, 2, 2)
    assert fun.sources["signature"] == 2
    assert fun.fields["phone"] == 4 and fun.fields["title"] == 2
    assert fun.seconds >= 0 and fun.per_contact() >= 0


def test_the_age_window_is_a_hard_cut(book):
    fun = _run(book, months=6)
    assert fun.with_mail == 2 and fun.proposed == 1          # Eve's 2019 mail is outside the window
    assert fun.since is not None


def test_measuring_leaves_the_index_byte_for_byte_the_same(book):
    before = hashlib.sha256(book.read_bytes()).hexdigest()
    _run(book, keep_samples=True)
    assert hashlib.sha256(book.read_bytes()).hexdigest() == before


def test_the_read_only_store_refuses_to_write(book):
    store = ReadOnlyStore(book)
    try:
        with pytest.raises(PermissionError):
            store.set_state("enrich:seen:gravatar:x", "1")
        with pytest.raises(sqlite3.OperationalError):
            store.conn.execute("DELETE FROM contacts")
    finally:
        store.close()


def test_measuring_opens_no_socket(book, monkeypatch):
    def refuse(*_a, **_k):
        raise AssertionError("the measuring command opened a socket")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    fun = _run(book)
    assert fun.sources["gravatar: would ask"] == 4


@pytest.mark.parametrize("months, today, expected", [
    (6, date(2026, 10, 12), date(2026, 4, 12)),
    (12, date(2026, 10, 12), date(2025, 10, 12)),
    (1, date(2026, 3, 31), date(2026, 2, 28)),
    (24, date(2026, 1, 15), date(2024, 1, 15)),
])
def test_the_window_starts_whole_calendar_months_back(months, today, expected):
    start = window_start(months, today)
    assert time.strftime("%Y-%m-%d", time.gmtime(start)) == expected.isoformat()
    assert window_start(0, today) is None


def test_a_sample_is_written_with_its_signature_block_and_no_address_in_the_name(book, tmp_path):
    fun = _run(book, keep_samples=True)
    out = tmp_path / "sample.csv"
    n = write_sample(fun, 3, out)
    rows = list(csv.DictReader(out.open(encoding="utf-8")))
    assert n == len(rows) == 3
    assert all("Kind regards" not in r["signature_block"] and r["signature_block"] for r in rows)
    assert all(r["receipt"] and r["suggested"] for r in rows)


def test_the_command_never_migrates_the_index(book, tmp_path, capsys):
    """The 2026-10-12 trap: a branch's CLI moved the owner's index to v40. Measuring must not touch the schema."""
    from app.cli import main

    with sqlite3.connect(book) as conn:
        conn.execute("DELETE FROM schema_version WHERE version = (SELECT MAX(version) FROM schema_version)")
        older = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
    before = hashlib.sha256(book.read_bytes()).hexdigest()
    env = tmp_path / "scratch.env"
    env.write_text(f"DATA_PATH={tmp_path}\nFTS_DB={book}\n", encoding="utf-8")
    code = main(["contacts", "enrich", "--measure", "--any-age", "--env", str(env)])
    assert code == 0
    assert "Live contacts" in capsys.readouterr().out
    assert hashlib.sha256(book.read_bytes()).hexdigest() == before
    with sqlite3.connect(f"{book.as_uri()}?mode=ro", uri=True) as conn:
        assert conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == older


def test_the_local_sources_are_counted_and_a_failing_one_says_so(book, monkeypatch):
    """Order 1s section 5's sources reach the funnel; one that fails on the read-only index is counted as
    failing - a zero must mean found nothing, never could not look."""
    from app.contacts.enrich import invitations, vcards

    monkeypatch.setattr(vcards, "vcard_proposals", lambda *a, **k: ["a proposal"])

    def broken(*_a, **_k):
        raise RuntimeError("no such table")

    monkeypatch.setattr(invitations, "calendar_proposals", broken)
    fun = _run(book)
    assert fun.sources["vcard file"] == 5
    assert fun.sources["calendar invitation: could not be read"] == 5
    assert "linkedin export" not in fun.sources                      # no export chosen
