"""Live mail in the index: D13, removal, the page transaction (order 1k 5d-5g, D13).

Layer: L3 and L7 (test)

D13: a message an account holds and the same message in an Outlook archive (matched by its
`Message-ID`) are ONE message. The live copy is searched; the archive copy is taken out of
the index and kept in `message_places`. Whichever is indexed second finds the first, a later
re-read of the archive does not bring it back, and order 1j's `dedupe-mail` is untouched.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from app.accounts.mail_index import MailIndexer, account_prefix, archive_copy_is_covered
from app.accounts.provider import RemoteMessage
from app.index.mail_duplicates import find_mail_duplicates
from app.storage.sqlite_store import SqliteStore
from tests.unit.mail_sync_fakes import raw_message

ARCHIVE = "D:/mail/2019.pst"


@pytest.fixture
def store(tmp_path):
    with SqliteStore(tmp_path / "index.db") as opened:
        opened.conn.execute("INSERT INTO accounts (provider, address, name, colour, added_at) "
                            "VALUES ('google','me@x.com','Home','#fff',0)")
        opened.conn.commit()
        yield opened


def archive_message(store, entry, subject, body, *, message_id="", attachment=None):
    """What `Pipeline._write_one` leaves for an Outlook message (and its attachment)."""
    key = f"pst://2019/{entry}"
    fid = store.upsert_file(key, size_bytes=1, mtime_ns=1, ext="pst", source_kind="pst_message", status="INDEXED",
                            content_hash=f"hash-{entry}")
    store.replace_chunks(fid, [{"text": f"Subject: {subject}\n\n{body}"}])
    store.set_message(fid, store_path=ARCHIVE, entry_id=entry, subject=subject, sender="priya@example.com",
                      sent_at=1_709_640_000)
    if message_id:
        store.set_message_id(fid, message_id)
    if attachment:
        aid = store.upsert_file(f"{key}/attachments/{attachment[0]}", size_bytes=1, mtime_ns=1, ext="txt",
                                source_kind="pst_message", status="INDEXED")
        store.replace_chunks(aid, [{"text": attachment[1]}])
    return fid


def archive_marker(store):
    return store.upsert_file(ARCHIVE, size_bytes=5000, mtime_ns=123456, source_kind="archive",
                             status="INDEXED", content_hash="marker")


def live(indexer, store, remote_id, subject, body, *, message_id="<A.1@Example.com>", folder="INBOX", **kw):
    msg = RemoteMessage(remote_id, raw_message(subject, body, message_id=message_id, **kw), folder=folder,
                        received_at=1_709_640_000)
    prepared = indexer.prepare(1, msg)
    assert prepared is not None
    return indexer.apply_page(1, [prepared], [])


def searched(store, query):
    from app.chat.testing import keyword_engine

    engine = keyword_engine(store)
    try:
        return [r.path for r in engine.search(query, limit=20).results]
    finally:
        engine.close()


# ------------------------------------------------------------- D13, archive first

def test_archive_first_then_live_the_live_copy_is_the_one_searched(store):
    archive_marker(store)
    archive_message(store, "E1", "Budget", "The budget needs the tortoise approval.", message_id="a.1@example.com",
                    attachment=("sheet.txt", "tortoise spreadsheet"))
    assert searched(store, "tortoise") == ["pst://2019/E1"] or "pst://2019/E1" in searched(store, "tortoise")
    indexer = MailIndexer(store)
    result = live(indexer, store, "r1", "Budget", "The budget needs the tortoise approval.")
    assert result.moved_archive_copies == 1
    live_key = "account://1/INBOX/r1"
    assert searched(store, "tortoise") == [live_key]                         # one message, the live one
    assert store.get_file("pst://2019/E1") is None and store.get_file("pst://2019/E1/attachments/sheet.txt") is None
    places = store.message_places(store.get_file(live_key).id)
    assert places == [{"kind": "archive", "ref": "pst://2019/E1", "store_path": ARCHIVE, "entry_id": "E1"}]
    assert store.conn.execute("SELECT COUNT(*) FROM chunks WHERE file_id NOT IN (SELECT id FROM files)").fetchone()[0] == 0


def test_the_message_ids_are_compared_normalised(store):
    archive_message(store, "E1", "S", "Words about a gannet.", message_id="a.1@example.com")
    live(MailIndexer(store), store, "r1", "S", "Words about a gannet.", message_id="  <A.1@EXAMPLE.com> ")
    assert store.get_file("pst://2019/E1") is None


def test_a_different_message_id_is_a_different_message(store):
    archive_message(store, "E1", "S", "Words about a gannet.", message_id="other@example.com")
    live(MailIndexer(store), store, "r1", "S", "Words about a gannet.")
    assert store.get_file("pst://2019/E1") is not None


def test_a_message_with_no_message_id_is_never_matched(store):
    archive_message(store, "E1", "S", "Words about a gannet.")
    live(MailIndexer(store), store, "r1", "S", "Words about a gannet.", message_id="")
    assert store.get_file("pst://2019/E1") is not None
    assert store.conn.execute("SELECT COUNT(*) FROM message_places").fetchone()[0] == 0


# -------------------------------------------------------------- D13, live first

def test_live_first_then_the_archive_copy_is_left_out_and_recorded(store):
    live(MailIndexer(store), store, "r1", "Budget", "Words about a heron.")
    meta = {"message_id": "<a.1@example.com>", "store_path": ARCHIVE, "entry_id": "E1"}
    assert archive_copy_is_covered(store, "pst://2019/E1", meta) is True
    assert store.message_places(store.get_file("account://1/INBOX/r1").id) == [
        {"kind": "archive", "ref": "pst://2019/E1", "store_path": ARCHIVE, "entry_id": "E1"}]
    # its attachments follow it, and a re-read of the archive meets the recorded place
    assert archive_copy_is_covered(store, "pst://2019/E1/attachments/x.txt", {}) is True
    assert archive_copy_is_covered(store, "pst://2019/E1", {"message_id": ""}) is True
    # another message is not covered
    assert archive_copy_is_covered(store, "pst://2019/E2", {"message_id": "<other@x>"}) is False
    assert archive_copy_is_covered(store, "pst://2019/E3", {}) is False
    assert archive_copy_is_covered(store, "pst://2019/E4", {"message_id": "<a.1@example.com>"}, has_text=False) is False


class _Vectors:
    """Enough of the vector store for a pipeline that never embeds in these tests."""

    def delete_by_file_ids(self, ids):
        pass

    def ensure_table(self):
        pass


def _bare_pipeline(store):
    from app.index.embedder import Embedder
    from app.index.pipeline import Pipeline, PipelineConfig
    from app.index.walker import WalkConfig

    return Pipeline(store, _Vectors(), Embedder(dim=4, encoder=lambda texts: [[0.1] * 4 for _ in texts]),
                    PipelineConfig(walk=WalkConfig(roots=[Path(ARCHIVE).parent]), workers=1))


def _archive_item(key, *, message_id, text="Words about a heron."):
    from app.index.pipeline import _Extracted

    candidate = SimpleNamespace(path=Path(ARCHIVE), mtime_ns=1, size_bytes=5000, volume_id=None, relative_path=None)
    return _Extracted(candidate=candidate, content_hash=None, key=key,
                      chunks=[{"ordinal": 0, "text": text}], source_kind="pst_message",
                      meta={"message_id": message_id, "store_path": ARCHIVE, "entry_id": key.rsplit("/", 1)[-1],
                            "subject": "Budget", "sender": "priya@example.com", "sent_at": 1})


def test_the_pipeline_does_not_write_an_archive_copy_of_a_live_message_and_a_reread_does_not_resurrect_it(store):
    live(MailIndexer(store), store, "r1", "Budget", "Words about a heron.")
    pipeline = _bare_pipeline(store)
    item = _archive_item("pst://2019/E1", message_id="<a.1@example.com>")
    for _ in range(2):                                                      # indexed, then the archive read again
        assert pipeline._write_one(item) == []
        assert store.get_file("pst://2019/E1") is None
    assert pipeline._stats_ref.mail_repeats == 2
    # the same message in the archive with no matching live copy is written as always
    other = _archive_item("pst://2019/E9", message_id="<unrelated@example.com>", text="Something else entirely.")
    pipeline._write_one(other)
    assert store.get_file("pst://2019/E9") is not None


def test_the_pipeline_stores_an_archive_messages_message_id(store):
    pipeline = _bare_pipeline(store)
    fid = store.upsert_file("pst://2019/E5", size_bytes=1, mtime_ns=1, source_kind="pst_message")
    pipeline._store_message_meta(fid, {"subject": "S", "sender": "a@b.c", "store_path": ARCHIVE, "entry_id": "E5",
                                       "message_id": " <Mixed.Case@Example.COM> "})
    assert store.get_message(fid)["message_id"] == "mixed.case@example.com"
    # and one without a Message-ID leaves the column empty
    gid = store.upsert_file("pst://2019/E6", size_bytes=1, mtime_ns=1, source_kind="pst_message")
    pipeline._store_message_meta(gid, {"subject": "S", "sender": "a@b.c"})
    assert store.get_message(gid)["message_id"] is None


def test_archive_copies_indexed_after_the_live_one_by_a_real_second_pass_are_one_message(store):
    """Archive indexed -> live arrives -> archive indexed again -> still one searchable message."""
    pipeline = _bare_pipeline(store)
    pipeline._write_one(_archive_item("pst://2019/E1", message_id="<a.1@example.com>"))
    assert searched(store, "heron") == ["pst://2019/E1"]
    live(MailIndexer(store), store, "r1", "Budget", "Words about a heron.")
    pipeline._write_one(_archive_item("pst://2019/E1", message_id="<a.1@example.com>"))
    assert searched(store, "heron") == ["account://1/INBOX/r1"]


# ----------------------------------------------- order 1j's dedupe-mail is unaffected

def test_dedupe_mail_does_not_see_live_messages_or_hidden_copies(store):
    archive_message(store, "E1", "Same", "Duplicated words here.", message_id="")
    archive_message(store, "E2", "Same", "Duplicated words here.", message_id="")
    for entry in ("E1", "E2"):                       # one Outlook item stored twice: same text, subject, sender, time
        store.conn.execute("UPDATE files SET content_hash = 'h' WHERE path = ?", (f"pst://2019/{entry}",))
    before = find_mail_duplicates(store)
    assert len(before.groups) == 1 and len(before.message_ids) == 1
    live(MailIndexer(store), store, "r1", "Live", "Something different about a yak.", message_id="<live@x>")
    after = find_mail_duplicates(store)
    assert after.groups == before.groups and after.attachment_ids == before.attachment_ids
    # a live message that hides an archive copy is not a duplicate group either
    archive_message(store, "E3", "Other", "Words about a kestrel.", message_id="k@x")
    live(MailIndexer(store), store, "r2", "Other", "Words about a kestrel.", message_id="<k@x>")
    assert find_mail_duplicates(store).groups == before.groups


# ------------------------------------------------- the live copy goes: the archive returns

def test_when_the_live_copy_goes_its_archive_copy_can_be_read_again(store):
    archive_marker(store)
    archive_message(store, "E1", "Budget", "Words about a heron.", message_id="a.1@example.com")
    indexer = MailIndexer(store)
    live(indexer, store, "r1", "Budget", "Words about a heron.")
    marker = store.get_file(ARCHIVE)
    assert marker.mtime_ns == 123456
    result = indexer.apply_page(1, [], ["r1"])
    assert result.deleted == 1 and result.released_archives == 1
    assert store.get_file("account://1/INBOX/r1") is None
    assert store.conn.execute("SELECT COUNT(*) FROM message_places").fetchone()[0] == 0         # cascaded away
    marker = store.get_file(ARCHIVE)
    assert marker.mtime_ns == 0 and marker.content_hash is None          # the next index run reads the archive again


def test_removing_the_accounts_mail_also_releases_its_archive_copies(store):
    archive_marker(store)
    archive_message(store, "E1", "Budget", "Words about a heron.", message_id="a.1@example.com")
    indexer = MailIndexer(store)
    live(indexer, store, "r1", "Budget", "Words about a heron.")
    assert indexer.remove_account(1) == 1
    assert store.get_file(ARCHIVE).mtime_ns == 0


# ---------------------------------------------------------------- the page transaction

def test_a_page_is_one_transaction_with_whatever_the_caller_adds_to_it(store):
    indexer = MailIndexer(store)
    msg = RemoteMessage("r1", raw_message("S", "Words about a stoat.", message_id="<s@x>"), folder="INBOX")
    prepared = indexer.prepare(1, msg)

    def boom(conn):
        raise RuntimeError("the token could not be saved")

    with pytest.raises(RuntimeError):
        indexer.apply_page(1, [prepared], [], in_transaction=boom)
    assert store.get_file("account://1/INBOX/r1") is None                 # nothing of the page survived
    indexer.apply_page(1, [prepared], [], in_transaction=lambda conn: conn.execute(
        "INSERT INTO index_state (key, value, updated_at) VALUES ('t', 'saved', 0)"))
    assert store.get_file("account://1/INBOX/r1") is not None and store.get_state("t") == "saved"


def test_a_message_delivered_again_is_updated_not_duplicated(store):
    indexer = MailIndexer(store)
    first = live(indexer, store, "r1", "S", "Words about a stoat.")
    second = live(indexer, store, "r1", "S", "Words about a stoat.")
    assert (first.added, first.changed, second.added, second.changed) == (1, 0, 0, 1)
    assert store.conn.execute("SELECT COUNT(*) FROM files WHERE path LIKE 'account://%'").fetchone()[0] == 1
    assert store.conn.execute("SELECT COUNT(*) FROM messages WHERE account_id = 1").fetchone()[0] == 1


def test_a_message_with_no_text_is_not_indexed(store):
    indexer = MailIndexer(store)
    assert indexer.prepare(1, RemoteMessage("r1", b"Content-Type: text/plain\r\n\r\n", folder="INBOX")) is None


def test_every_row_of_an_account_is_found_by_its_prefix(store):
    indexer = MailIndexer(store)
    live(indexer, store, "r1", "S", "Words about a stoat.")
    assert store.file_ids_under_prefix(account_prefix(1)) == [store.get_file("account://1/INBOX/r1").id]
    assert store.file_ids_under_prefix(account_prefix(2)) == []
    assert store.file_ids_under_prefix(account_prefix(11)) == []          # 1 is not 11


def test_forgetting_deletes_vectors_before_rows(store):
    class Vectors:
        def __init__(self):
            self.seen_rows = None

        def delete_by_file_ids(self, ids):
            self.seen_rows = [store.get_file_by_id(i) is not None for i in ids]

    vectors = Vectors()
    indexer = MailIndexer(store, vectors=vectors)
    live(indexer, store, "r1", "S", "Words about a stoat.")
    indexer.remove_account(1)
    assert vectors.seen_rows == [True]                                    # the rows were still there when the vectors went


def test_a_prefix_only_removes_that_accounts_rows(store):
    store.conn.execute("INSERT INTO accounts (provider, address, name, colour, added_at) "
                       "VALUES ('google','b@x.com','Other','#fff',0)")
    store.conn.commit()
    indexer = MailIndexer(store)
    live(indexer, store, "r1", "S", "Words about a stoat.")
    msg = RemoteMessage("r1", raw_message("S", "Words about a stoat.", message_id="<z@x>"), folder="INBOX")
    indexer.apply_page(2, [indexer.prepare(2, msg)], [])
    assert indexer.remove_account(1) == 1
    assert store.get_file("account://2/INBOX/r1") is not None
