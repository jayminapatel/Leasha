r"""The keyword index written once per batch: same index, same guarantees.

Layer: L1. Order 0z (`WORKORDER-robust-indexing-and-status.md`), "writing
measured", 2026-09-30.

Inside `SqliteStore.batch()` the index rows of new passages and new messages
are no longer written one at a time by the `chunks_ai` and `messages_ai`
triggers; they wait and are written together at the end of the batch, in the
same transaction (`SqliteStore._deferred` has the measurement and the reason).

That is only allowed if nothing anybody can see changes. So this file is about
what must still be true:

* the same writes give **the same index** as the old way - every row, every
  term, every search, the substring searches `messages_fts` exists for
  included;
* a run **killed at any point** leaves the index and the rows it indexes in
  step, with nothing to repair;
* a failure rolls the batch back and leaves the triggers on;
* every write that does not know about any of this still finds the database
  exactly as it always did.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

import app.storage.sqlite_store as store_module
from app.core.errors import make_error
from app.storage.sqlite_store import SqliteStore

PROJECT_ROOT = Path(__file__).resolve().parents[2]

needs_the_switch = pytest.mark.skipif(
    store_module._TRIGGER_SWITCH is None,                        # noqa: SLF001
    reason="this Python cannot switch a connection's triggers, so the store "
           "writes the index the old way and there is nothing to compare")

WORDS = ("invoice", "survey", "harbour", "quarterly", "budget", "kitchen",
         "holiday", "contract", "renewal", "delivery", "meeting", "minutes",
         "ResetPasswordHandler", "colour", "naïve", "straße", "o'brien")
PEOPLE = ("dave.smith@acme.com", "priya@survey.example.org", "JOSÉ@correo.es",
          "o'brien@harbour.ie", "accounts@acme.com", "x@y.zz")


def _message(n: int, salt: str = "") -> dict:
    """One deterministic mail message. `salt` makes a changed version of it."""
    words = [WORDS[(n * 7 + k * 3) % len(WORDS)] for k in range(6 + n % 5)]
    sender = PEOPLE[n % len(PEOPLE)]
    recipients = [PEOPLE[(n + k + 1) % len(PEOPLE)] for k in range(1 + n % 3)]
    subject = None if n % 17 == 0 else f'{"RE: " if n % 3 else ""}{words[0]} "{words[1]}" no {n}{salt}'
    chunks = [{"text": f"Subject: {subject}\nFrom: {sender}\n\n{' '.join(words)} marker{n}{salt}"}]
    for extra in range(n % 3):
        chunks.append({"text": f"{' '.join(reversed(words))} part{extra} of {n}{salt}"})
    if n % 23 == 0:
        chunks = []                                   # a message with no text at all
    return {
        "key": f"D:\\Mail\\a.pst#{n}", "chunks": chunks,
        "meta": {"subject": subject, "sender": sender,
                 "recipients": json.dumps(recipients), "sent_at": 1_600_000_000 + n,
                 "conversation": f"<thread{n % 9}@acme.com>", "entry_id": str(n),
                 "store_path": "D:\\Mail\\a.pst", "has_attach": n % 2},
    }


def _write_message(store: SqliteStore, n: int, salt: str = "") -> int:
    """The three writes `Pipeline._write_one` makes for one message."""
    item = _message(n, salt)
    with store.batch():
        file_id = store.upsert_file(
            item["key"], size_bytes=100 + n, mtime_ns=1, content_hash=f"h{n}{salt}",
            status="PARTIAL", source_kind="pst_message", parent_dir="D:\\Mail", ext="pst")
        store.replace_chunks(file_id, item["chunks"])
        store.set_message(file_id, **item["meta"])
    return file_id


def _write_the_lot(store: SqliteStore) -> None:
    """Every shape of write a run makes, in a fixed order."""
    # A first index: one group of new messages, the common case.
    with store.batch():
        ids = [_write_message(store, n) for n in range(120)]
        # The same message again in the same group, changed.
        _write_message(store, 5, salt="-second")
        # An ordinary document, which also has a name in `files_fts`.
        doc = store.upsert_file("C:/docs/Invoice 2024.txt", size_bytes=9, mtime_ns=2)
        store.replace_chunks(doc, [{"text": f"invoice page {page} harbour"} for page in range(5)])
        # Writes that know nothing about waiting rows, between documents.
        store.add_caption_chunk(ids[10], "a harbour at dusk")
        store.mark_skipped(ids[11], make_error("ERR_UNEXPECTED", "test", details="x"))
        store.delete_file(ids[12])
        _write_message(store, 130)
    # A later run: some messages changed, some new, one written on its own.
    with store.batch():
        for n in range(20, 30):
            _write_message(store, n, salt="-changed")
        for n in range(200, 220):
            _write_message(store, n)
    _write_message(store, 300)
    store.delete_file(ids[40])
    store.set_message(ids[41], subject="a subject replaced on its own", sender="x@y.zz",
                      recipients='["dave.smith@acme.com"]')
    # One long document, past the point where waiting rows are handed over early.
    with store.batch():
        long_doc = store.upsert_file("C:/docs/long.txt", size_bytes=9, mtime_ns=2)
        store.replace_chunks(long_doc, [
            {"text": f"{WORDS[k % len(WORDS)]} long passage {k}"} for k in range(45)])
        for n in range(400, 410):
            _write_message(store, n)


def _in_step(store: SqliteStore) -> None:
    """Fail unless both indexes agree with the tables they index.

    FTS5's own check, asked to compare against the content table (`rank` 1),
    and a row count beside it: one index row per content row.
    """
    conn = store.conn
    for fts, content in (("chunks_fts", "chunks"), ("messages_fts", "messages")):
        conn.execute(f"INSERT INTO {fts}({fts}, rank) VALUES('integrity-check', 1)")
        indexed = conn.execute(f"SELECT COUNT(*) FROM {fts}_docsize").fetchone()[0]
        rows = conn.execute(f"SELECT COUNT(*) FROM {content}").fetchone()[0]
        assert indexed == rows, f"{fts} holds {indexed} rows for {rows} in {content}"


def _fingerprint(store: SqliteStore) -> dict:
    """Everything a search can be answered from, and a spread of real searches."""
    conn = store.conn
    conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS temp.mail_vocab "
                 "USING fts5vocab('main', 'messages_fts', 'row')")
    out: dict = {
        "chunks": [tuple(r) for r in conn.execute(
            "SELECT id, file_id, ordinal, text, symbols FROM chunks ORDER BY id")],
        "messages": [tuple(r) for r in conn.execute(
            "SELECT file_id, subject, sender, recipients FROM messages ORDER BY file_id")],
        "chunk terms": [tuple(r) for r in conn.execute(
            "SELECT term, doc, cnt FROM chunks_vocab ORDER BY term")],
        "mail terms": [tuple(r) for r in conn.execute(
            "SELECT term, doc, cnt FROM temp.mail_vocab ORDER BY term")],
        "chunk sizes": [tuple(r) for r in conn.execute(
            "SELECT id, hex(sz) FROM chunks_fts_docsize ORDER BY id")],
        "mail sizes": [tuple(r) for r in conn.execute(
            "SELECT id, hex(sz) FROM messages_fts_docsize ORDER BY id")],
    }
    for query in ("invoice", "harbour", "password", "marker5", "second", "changed",
                  "inv*", "quart* budget", '"long passage"', "colour OR kitchen",
                  "dusk", "naïve", "part1"):
        out[f"bm25 {query}"] = [
            (hit["chunk_id"], round(hit["score"], 9))
            for hit in store.search_bm25(query, limit=1000)]
    for column, text in (("sender", "ave.smi"), ("sender", "josé"), ("sender", "@acme"),
                         ("subject", "voice"), ("subject", '"sur'), ("subject", "replaced"),
                         ("recipients", "harbour.ie"), ("recipients", "o'br")):
        phrase = '"' + text.replace('"', '""') + '"'
        out[f"trigram {column} {text}"] = [r[0] for r in conn.execute(
            "SELECT rowid FROM messages_fts WHERE messages_fts MATCH ? ORDER BY rowid",
            (f"{column} : {phrase}",))]
    for kwargs in ({"sender": "ave.smi"}, {"subject": "voice"}, {"recipient": "survey"},
                   {"sender": "acme", "subject": "budget"}):
        out[f"browse {kwargs}"] = [row["file_id"] for row in store.browse_messages(**kwargs)]
    out["by name"] = [row["path"] for row in store.search_files_by_name("voice 20")]
    return out


def _built(path: Path, *, defer: bool, monkeypatch) -> SqliteStore:
    monkeypatch.setattr(store_module, "FTS_DEFER_MAX_ROWS", 40)
    store = SqliteStore(path).connect()
    store.defer_fts = defer
    _write_the_lot(store)
    return store


def _segments(store: SqliteStore, fts: str) -> int:
    """How many separate pieces the index is stored in right now."""
    return int(store.conn.execute(
        f"SELECT COUNT(DISTINCT id >> 37) FROM {fts}_data WHERE id >= (1 << 37)").fetchone()[0])


# --- the same index ---------------------------------------------------------


@needs_the_switch
def test_both_ways_of_writing_give_the_same_index_and_the_same_searches(tmp_path, monkeypatch):
    old = _built(tmp_path / "old.db", defer=False, monkeypatch=monkeypatch)
    new = _built(tmp_path / "new.db", defer=True, monkeypatch=monkeypatch)
    try:
        _in_step(old)
        _in_step(new)
        before, after = _fingerprint(old), _fingerprint(new)
        assert before["bm25 invoice"] and before["trigram sender ave.smi"], (
            "the searches found nothing either way, so comparing them proves nothing")
        for name in before:
            assert after[name] == before[name], f"{name} differs between the two ways"
    finally:
        old.close()
        new.close()


def test_the_check_used_here_notices_an_index_out_of_step(tmp_path):
    """The instrument, tested: a passage with no index row must fail `_in_step`."""
    store = SqliteStore(tmp_path / "t.db").connect()
    try:
        _write_message(store, 1)
        _in_step(store)
        with store.write() as conn:
            conn.execute("DROP TRIGGER chunks_ai")
            conn.execute("INSERT INTO chunks (file_id, ordinal, text) VALUES (1, 99, 'orphan words')")
        with pytest.raises((sqlite3.DatabaseError, AssertionError)):
            _in_step(store)
    finally:
        store.close()


# --- why: one piece per batch, not one per row ------------------------------


@needs_the_switch
def test_a_batch_writes_each_index_once(tmp_path):
    """The cause, pinned. Written by the triggers, every row became its own
    piece of the index before the transaction had even committed; FTS5 then
    spent its time merging them. Waiting rows reach the index as one piece."""
    counts = {}
    for defer in (False, True):
        store = SqliteStore(tmp_path / f"{defer}.db").connect()
        store.defer_fts = defer
        try:
            with store.batch():
                for n in range(1, 60):
                    _write_message(store, n)
                inside = (_segments(store, "chunks_fts"), _segments(store, "messages_fts"))
            counts[defer] = (inside, (_segments(store, "chunks_fts"),
                                      _segments(store, "messages_fts")))
            _in_step(store)
        finally:
            store.close()
    assert counts[True] == ((0, 0), (1, 1)), counts
    assert min(counts[False][0]) > 1, (
        "the triggers no longer write a piece per row, so the reason for "
        f"waiting rows is gone - measure again before keeping them: {counts}")


# --- killed part-way --------------------------------------------------------

KILLED = r"""
import os, sys
sys.path.insert(0, {root!r})
import app.storage.sqlite_store as store_module
from app.storage.sqlite_store import SqliteStore
sys.path.insert(0, {tests!r})
from test_fts_deferred_writes import _write_message

where = sys.argv[2]
store = SqliteStore(sys.argv[1]).connect()
with store.batch():                          # a group that commits
    for n in range(1, 41):
        _write_message(store, n)
# A page cache of ten pages, so the group below spills into the journal
# before the process dies - the journal then holds uncommitted pages.
store.conn.execute("PRAGMA cache_size = 10")
if where == "some rows already handed to the index":
    store_module.FTS_DEFER_MAX_ROWS = 25
group = store.batch()
group.__enter__()
for n in range(41, 120):
    _write_message(store, n)
if where == "index written, commit not reached":
    store._finish_deferred(store.conn)
print("dying", flush=True)
os._exit(7)
"""


@needs_the_switch
@pytest.mark.parametrize("where", [
    "rows still waiting",
    "some rows already handed to the index",
    "index written, commit not reached",
])
def test_a_run_killed_inside_a_batch_leaves_the_index_in_step(tmp_path, where):
    script = tmp_path / "killed.py"
    script.write_text(KILLED.format(root=str(PROJECT_ROOT),
                                    tests=str(PROJECT_ROOT / "tests" / "unit")),
                      encoding="utf-8")
    db = tmp_path / "killed.db"
    done = subprocess.run([sys.executable, str(script), str(db), where],
                          capture_output=True, text=True, timeout=120, cwd=str(PROJECT_ROOT))
    assert done.returncode == 7 and "dying" in done.stdout, done.stderr

    store = SqliteStore(db).connect()                  # the next start
    try:
        conn = store.conn
        assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 40, (
            "the group that committed is whole and the one that did not is gone")
        _in_step(store)
        assert store.search_bm25("marker40"), "a committed message cannot be found"
        assert not store.search_bm25("marker41"), "a message that never committed can be found"
        assert store.integrity_check()
        # And the next run indexes as usual, inside a batch and outside one.
        with store.batch():
            _write_message(store, 500)
        _write_message(store, 501)
        assert store.search_bm25("marker500") and store.search_bm25("marker501")
        _in_step(store)
    finally:
        store.close()


# --- a failure, and what comes after it -------------------------------------


@needs_the_switch
def test_a_failed_batch_is_rolled_back_whole_and_leaves_the_triggers_on(tmp_path):
    store = SqliteStore(tmp_path / "t.db").connect()
    try:
        _write_message(store, 1)
        with pytest.raises(RuntimeError):
            with store.batch():
                _write_message(store, 2)
                _write_message(store, 3)
                raise RuntimeError("the reader fell over")
        assert store.conn.getconfig(store_module._TRIGGER_SWITCH)    # noqa: SLF001
        assert not store.search_bm25("marker2") and not store.search_bm25("marker3")
        assert store.conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1
        # The very next write is mirrored by its trigger, and a batch works.
        _write_message(store, 4)
        with store.batch():
            _write_message(store, 5)
        assert store.search_bm25("marker4") and store.search_bm25("marker5")
        _in_step(store)
    finally:
        store.close()


@needs_the_switch
def test_an_index_that_cannot_be_written_fails_the_batch_not_the_index(tmp_path):
    """If the waiting rows cannot be written, nothing of the batch may commit."""
    store = SqliteStore(tmp_path / "t.db").connect()
    try:
        with pytest.raises(sqlite3.Error):
            with store.batch():
                _write_message(store, 1)
                # The index is given a row it cannot take: a rowid that is not one.
                store._local.deferred.chunks.append(("not a rowid", "x", ""))   # noqa: SLF001
        assert store.conn.getconfig(store_module._TRIGGER_SWITCH)    # noqa: SLF001
        assert store.conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] == 0
        _in_step(store)
    finally:
        store.close()


@needs_the_switch
def test_the_triggers_are_back_on_after_every_batch(tmp_path):
    store = SqliteStore(tmp_path / "t.db").connect()
    try:
        with store.batch():
            _write_message(store, 1)
            assert not store.conn.getconfig(store_module._TRIGGER_SWITCH)   # noqa: SLF001
        assert store.conn.getconfig(store_module._TRIGGER_SWITCH)           # noqa: SLF001
        # A change outside a batch is mirrored by the triggers, as ever.
        file_id = store.get_file("D:\\Mail\\a.pst#1").id
        store.replace_chunks(file_id, [{"text": "entirely different words zebra"}])
        assert store.search_bm25("zebra") and not store.search_bm25("marker1")
        store.delete_file(file_id)
        assert not store.search_bm25("zebra")
        _in_step(store)
    finally:
        store.close()


@needs_the_switch
def test_a_write_that_knows_nothing_of_this_finds_the_index_complete(tmp_path):
    """Between two documents of a batch, any other store write must see every
    row indexed and the triggers on - here a delete of a file whose rows were
    still waiting, which would otherwise ask the index to forget rows it had
    not been given."""
    store = SqliteStore(tmp_path / "t.db").connect()
    try:
        with store.batch():
            first = _write_message(store, 1)
            second = _write_message(store, 2)
            store.delete_file(first)
            assert store.conn.getconfig(store_module._TRIGGER_SWITCH)       # noqa: SLF001
            store.add_caption_chunk(second, "a lighthouse")
            _write_message(store, 3)
        assert not store.search_bm25("marker1")
        assert store.search_bm25("marker2") and store.search_bm25("lighthouse")
        assert store.search_bm25("marker3")
        _in_step(store)
    finally:
        store.close()


def test_without_the_switch_the_index_is_written_the_old_way(tmp_path, monkeypatch):
    """An older Python has no `setconfig`. Nothing waits; nothing is lost."""
    monkeypatch.setattr(store_module, "_TRIGGER_SWITCH", None)
    store = SqliteStore(tmp_path / "t.db").connect()
    try:
        with store.batch():
            _write_message(store, 1)
            assert getattr(store._local, "deferred", None) is None           # noqa: SLF001
            assert store.search_bm25("marker1"), "the trigger did not write the row"
        _in_step(store)
    finally:
        store.close()


@needs_the_switch
def test_another_thread_sees_nothing_until_the_commit_and_everything_after(tmp_path):
    import threading

    store = SqliteStore(tmp_path / "t.db").connect()
    seen: dict = {}

    def look(label: str) -> None:
        seen[label] = (len(store.search_bm25("marker1")),
                       len(store.browse_messages(sender="priya")))

    try:
        with store.batch():
            _write_message(store, 1)
            thread = threading.Thread(target=look, args=("during",))
            thread.start()
            thread.join(10)
        thread = threading.Thread(target=look, args=("after",))
        thread.start()
        thread.join(10)
        assert seen == {"during": (0, 0), "after": (1, 1)}
    finally:
        store.close()
