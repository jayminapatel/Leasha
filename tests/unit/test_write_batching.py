"""Grouping writes into one transaction, and the guarantees that must survive it.

Layer: L1 and L3

Indexing one mail message cost **six** commits: `upsert_file`,
`replace_chunks`, `set_message`, two `set_state` calls for the checkpoint, and
`mark_indexed`. Measured here, by counting `COMMIT` statements rather than
counting call sites by eye - the first estimate from reading the code was four.

A commit is roughly fourteen times the cost of the same statement inside an
open transaction (35.2us against 2.6us, WAL, `synchronous = NORMAL`). Six per
document across twenty million messages is over an hour of a run spent
committing.

The risk in batching is not speed, it is atomicity changing shape underneath
code that relies on it. So most of this file is about what must still be true:
a failure rolls the whole group back, a nested `write()` joins rather than
committing early, and one thread's batch never strips the transaction from
another thread's write.
"""

from __future__ import annotations

import threading

import pytest

from app.core.errors import ActionType, AppError
from app.storage.sqlite_store import SqliteStore

TIMEOUT = 10.0


class CommitCounter:
    """Counts real `COMMIT` statements on a connection.

    Counting `store.write()` calls would count intent. This counts what SQLite
    was actually asked to do, which is the thing that costs.
    """

    def __init__(self, conn):
        self.conn = conn
        self.commits = 0
        self.begins = 0

    def __enter__(self):
        self.conn.set_trace_callback(self._saw)
        return self

    def __exit__(self, *_exc):
        self.conn.set_trace_callback(None)

    def _saw(self, statement: str) -> None:
        text = statement.strip().upper()
        if text.startswith("COMMIT"):
            self.commits += 1
        elif text.startswith("BEGIN"):
            self.begins += 1


@pytest.fixture
def store(tmp_path):
    with SqliteStore(tmp_path / "index.db") as opened:
        yield opened


def _index_one_document(store, path="/msg1.eml"):
    """Exactly what `_write_one` does for a mail message, minus LanceDB."""
    with store.batch():
        file_id = store.upsert_file(path=path, size_bytes=100, mtime_ns=1,
                                    source_kind="pst_message")
        store.replace_chunks(file_id, [{"text": f"chunk {n}"} for n in range(8)])
        store.set_message(file_id, subject="S", sender="a@b.c", recipients="[]",
                          sent_at=1)
    return file_id


# --- the count --------------------------------------------------------------

def test_indexing_one_document_is_one_commit_not_three(store):
    with CommitCounter(store.conn) as counted:
        _index_one_document(store)

    assert counted.commits == 1, (
        f"{counted.commits} commits for one document - the batch is not grouping")
    assert counted.begins == 1


def test_the_checkpoint_is_one_commit_not_two(store):
    with CommitCounter(store.conn) as counted:
        store.set_states({"cursor:last_path": "/a.pdf", "cursor:indexed": "1"})

    assert counted.commits == 1
    assert store.get_state("cursor:last_path") == "/a.pdf"
    assert store.get_state("cursor:indexed") == "1"


def test_marking_many_files_indexed_is_one_commit(store):
    ids = [store.upsert_file(path=f"/f{n}.pdf", size_bytes=1, mtime_ns=n,
                             source_kind="file") for n in range(25)]

    with CommitCounter(store.conn) as counted:
        store.mark_indexed_many(ids)

    assert counted.commits == 1, f"{counted.commits} commits for 25 files"
    assert all(store.get_file_by_id(fid).status == "INDEXED" for fid in ids)


def test_mark_indexed_many_of_nothing_writes_nothing(store):
    with CommitCounter(store.conn) as counted:
        store.mark_indexed_many([])

    assert counted.commits == 0


def test_mark_indexed_still_works_for_one_file(store):
    """The single-file call is now a wrapper. It must not have changed."""
    file_id = store.upsert_file(path="/one.pdf", size_bytes=1, mtime_ns=1,
                                source_kind="file")
    store.mark_indexed(file_id)

    record = store.get_file_by_id(file_id)
    assert record.status == "INDEXED"
    assert record.indexed_at is not None
    assert record.skip_code is None


# --- what must still be true ------------------------------------------------

def test_a_failure_rolls_the_whole_batch_back(store):
    """All of it or none. A half-written document is the thing to avoid.

    Chunks without their file row, or a file marked INDEXED with no chunks,
    are both invisible-to-search states that nothing would ever retry.
    """
    with pytest.raises(RuntimeError), store.batch():
        file_id = store.upsert_file(path="/doomed.eml", size_bytes=1,
                                    mtime_ns=1, source_kind="pst_message")
        store.replace_chunks(file_id, [{"text": "orphan"}])
        raise RuntimeError("extraction blew up after the chunks went in")

    assert store.get_file("/doomed.eml") is None
    assert store.conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] == 0


def test_a_write_inside_a_batch_joins_it_rather_than_committing_early(store):
    """SQLite has no nested transactions.

    If `write()` opened its own, the batch would be committed by its first
    member and the grouping would be a lie that never failed. The rollback
    below is the only way to tell the difference.
    """
    with CommitCounter(store.conn) as counted, pytest.raises(RuntimeError), store.batch():
        store.upsert_file(path="/a.pdf", size_bytes=1, mtime_ns=1,
                          source_kind="file")   # a write() internally
        store.set_state("k", "v")               # and another
        raise RuntimeError("nope")

    assert counted.commits == 0, "something committed inside the batch"
    assert store.get_file("/a.pdf") is None
    assert store.get_state("k") is None


def test_nested_batches_commit_once_at_the_outermost(store):
    """`_write_one` batches, and a caller may want to batch several of those."""
    with CommitCounter(store.conn) as counted, store.batch():
        _index_one_document(store, "/one.eml")
        _index_one_document(store, "/two.eml")

    assert counted.commits == 1
    assert store.get_file("/one.eml") is not None
    assert store.get_file("/two.eml") is not None


def test_an_inner_batch_failing_rolls_back_the_outer_one(store):
    with pytest.raises(RuntimeError), store.batch():
        _index_one_document(store, "/kept.eml")
        with store.batch():
            raise RuntimeError("inner")

    assert store.get_file("/kept.eml") is None


def test_writes_still_commit_normally_outside_a_batch(store):
    """The depth counter must not leak past the block."""
    with store.batch():
        _index_one_document(store, "/batched.eml")

    with CommitCounter(store.conn) as counted:
        store.set_state("after", "yes")

    assert counted.commits == 1
    assert store.get_state("after") == "yes"


def test_the_depth_counter_is_cleared_even_when_the_batch_fails(store):
    with pytest.raises(RuntimeError), store.batch():
        raise RuntimeError("boom")

    with CommitCounter(store.conn) as counted:
        store.set_state("k", "v")

    assert counted.commits == 1, "a failed batch left the store thinking one is open"


def test_another_threads_write_waits_for_the_batch_rather_than_joining_it(store):
    """The depth counter is per-thread, and this is what that buys.

    A process-wide counter would make every write on the UI thread silently
    non-transactional for as long as indexing held a batch open - committed by
    somebody else's `COMMIT`, or lost to somebody else's `ROLLBACK`.

    Instead the other thread **waits**, because a batch holds the write lock.
    That is the existing serialisation model rather than something new, and it
    is the reason slow work must stay outside a batch: extraction or a LanceDB
    call inside one would hold this lock for as long as it took. `_write_one`
    keeps three SQLite statements in the block and the vector delete outside.

    The first version of this test asserted the other thread finished promptly
    and hung for ten seconds. The test was wrong, not the code.
    """
    inside = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def indexer():
        with store.batch():
            store.upsert_file(path="/indexing.eml", size_bytes=1, mtime_ns=1,
                              source_kind="pst_message")
            inside.set()
            release.wait(TIMEOUT)

    def other_thread_write():
        store.set_state("from_other_thread", "yes")
        finished.set()

    indexing = threading.Thread(target=indexer, daemon=True)
    indexing.start()
    assert inside.wait(TIMEOUT)

    writer = threading.Thread(target=other_thread_write, daemon=True)
    writer.start()

    # It must NOT have got in while the batch was open.
    assert not finished.wait(0.3), "another thread wrote inside the batch's transaction"
    assert store.get_state("from_other_thread") is None

    release.set()
    indexing.join(TIMEOUT)

    # ...and it must get in promptly once the batch closes.
    assert finished.wait(TIMEOUT), "the write never completed after the batch closed"
    writer.join(TIMEOUT)
    assert store.get_state("from_other_thread") == "yes"


def test_a_batch_on_one_thread_leaves_another_threads_depth_alone(store):
    """The counter itself, without the lock in the way.

    Sequenced rather than concurrent: the point is that the second thread's
    `batch_depth` starts at zero even though the first thread used one.
    """
    with store.batch():
        store.upsert_file(path="/a.eml", size_bytes=1, mtime_ns=1,
                          source_kind="pst_message")

    seen: dict = {}

    def other():
        with CommitCounter(store.conn) as counted:
            store.set_state("k", "v")
        seen["commits"] = counted.commits

    thread = threading.Thread(target=other, daemon=True)
    thread.start()
    thread.join(TIMEOUT)

    assert seen["commits"] == 1, "a worker's write did not open its own transaction"


def test_a_skip_records_the_file_and_the_reason_together(store):
    """`_record_skip` batches two writes. Both, or neither."""
    # SKIP_CONTINUE is what makes it a skip rather than a failure - `is_fatal`
    # is "anything that is not SKIP_CONTINUE", so NONE lands it in FAILED.
    error = AppError(
        code="ERR_NO_TEXT_LAYER", component="extract.pdf",
        message="no text layer", suggestion="run OCR",
        action_type=ActionType.SKIP_CONTINUE,
    )

    with CommitCounter(store.conn) as counted, store.batch():
        file_id = store.upsert_file(path="/scanned.pdf", size_bytes=1,
                                    mtime_ns=1, source_kind="file")
        store.mark_skipped(file_id, error)

    assert counted.commits == 1
    record = store.get_file("/scanned.pdf")
    assert record.status == "SKIPPED"
    assert record.skip_code == "ERR_NO_TEXT_LAYER"
