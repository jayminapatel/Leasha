"""Order 1s 1c: a person's mail is found by an indexed address, not a scan of every message.

Layer: L1

`sender_lc` could not simply be compared with `=`: on the owner's index it holds
"Name <address>" for about one message in eleven. Schema 41 keeps the bare address
(`sender_addr`), filled by the migration and by `set_message`, with the same rule as
`enrich.base.address_of` - which these tests hold the two to.
"""

from __future__ import annotations

import json

import pytest

from app.contacts.enrich import base
from app.storage import migrations
from app.storage.sqlite_store import SqliteStore, _sender_address

SENDERS = [
    "ann@acme.co.uk", "Ann Lee <ANN@Acme.co.uk>", '"Lee, Ann" <ann@acme.co.uk>', "  ann@acme.co.uk  ",
    "Ann Lee", "/O=EXCHANGELABS/OU=EXCHANGE ADMINISTRATIVE GROUP/CN=RECIPIENTS/CN=ANN", "", None,
    "joann@acme.co.uk", "Ann <ann@acme.co.uk>, Bob <bob@acme.co.uk>",
]


@pytest.mark.parametrize("sender", SENDERS)
def test_storage_and_enrichment_read_an_address_the_same_way(sender):
    assert (_sender_address(sender) or "") == base.address_of(sender or "")


@pytest.fixture()
def store(tmp_path):
    s = SqliteStore(tmp_path / "k.db").connect()
    yield s
    s.close()


def _mail(store, n, sender, sent):
    fid = store.upsert_file(f"m/{n}.eml", parent_dir="m", ext="eml", size_bytes=1, mtime_ns=n,
                            status="INDEXED", source_kind="eml")
    store.set_message(fid, subject=f"s{n}", sender=sender, recipients=json.dumps(["me@x.com"]), sent_at=sent)
    return fid


def test_set_message_keeps_the_bare_address(store):
    fid = _mail(store, 1, "Ann Lee <ANN@Acme.co.uk>", 100)
    assert store.conn.execute("SELECT sender_addr FROM messages WHERE file_id = ?", (fid,)).fetchone()[0] \
        == "ann@acme.co.uk"


def test_the_migration_fills_messages_written_before_it(store):
    fid = _mail(store, 1, "Ann Lee <ann@acme.co.uk>", 100)
    store.conn.execute("UPDATE messages SET sender_addr = NULL")
    store.conn.execute("DROP INDEX idx_messages_sender_addr")
    migrations._v41_enrichment_that_finds(store.conn)
    assert store.conn.execute("SELECT sender_addr FROM messages WHERE file_id = ?", (fid,)).fetchone()[0] \
        == "ann@acme.co.uk"
    names = {r[1] for r in store.conn.execute("PRAGMA index_list(messages)")}
    assert "idx_messages_sender_addr" in names


def test_mail_from_asks_the_index_not_every_message(store):
    plan = " ".join(str(r[3]) for r in store.conn.execute(
        "EXPLAIN QUERY PLAN SELECT file_id FROM messages WHERE sender_addr = ? ORDER BY sent_at DESC LIMIT 80",
        ("ann@acme.co.uk",)))
    assert "idx_messages_sender_addr" in plan and "SCAN" not in plan


@pytest.mark.slow
def test_the_indexed_and_the_old_way_find_the_same_messages(store):
    for n, (sender, sent) in enumerate([("Ann Lee <ann@acme.co.uk>", 300), ("ann@acme.co.uk", 200),
                                        ("joann@acme.co.uk", 250), ("Bob <bob@acme.co.uk>", 400),
                                        ("ann@acme.co.uk", None)], start=1):
        _mail(store, n, sender, sent)
    new = base.mail_from(store, ["ann@acme.co.uk"], limit=10)
    store._enrich_sender_addr = False                      # an index measured read-only before schema 41
    old = base.mail_from(store, ["ann@acme.co.uk"], limit=10)
    assert [m.file_id for m in new] == [m.file_id for m in old]
    assert [m.sent_at for m in new] == [300, 200, None]
