"""The four things the UI thread asked the backend for.

Layer: L1

`HANDOFF-ui-to-backend.md`, 2026-08-25. Two live bugs, one accessor, and a
design decision:

  B1  `VectorStore.ensure_table` wraps an already-formed `AppErrorException` in
      `ERR_UNEXPECTED`, destroying the real message. A user saw a DETAIL line
      that described itself.
  B2  Searches returned keyword hits and no vector hits. The UI needs a field
      it can bind to so the window can say so, rather than leaving it in a log.
  B3  `repo_files(repo_id, limit)` - the Code tab works without it by scanning
      the whole `files` table per expansion.
  B4  Branches, history and commits. Answered by measurement, not here.
"""

from __future__ import annotations

import pytest

from app.core.errors import AppErrorException
from app.storage.sqlite_store import SqliteStore
from app.storage.vector_store import VectorStore


@pytest.fixture
def store(tmp_path):
    with SqliteStore(tmp_path / "index.db") as opened:
        yield opened


# --- B1 ---------------------------------------------------------------------

def test_b1_an_unconnected_store_names_the_real_cause():
    """The reported message ended in a description of itself.

        DETAIL: Could not create the 'chunks' table: AppErrorException:
                [ERR_UNEXPECTED] An unexpected error occurred in storage.vectors.

    `self.db` raises `AppErrorException` when the store was never connected;
    the broad handler caught that finished error and wrapped it in a second
    one. `str(AppErrorException)` renders only the headline, so the inner
    message was destroyed rather than nested - and the real cause appeared
    neither in the message nor in the log.
    """
    store = VectorStore.__new__(VectorStore)
    store._db = None
    store._table = None
    store.table_name = "chunks"
    store.dim = 384

    with pytest.raises(AppErrorException) as caught:
        store.ensure_table()

    rendered = caught.value.error.render()
    assert "connect" in rendered, f"the cause is still not in the message:\n{rendered}"
    # ...and it is not a description of itself.
    assert "AppErrorException" not in rendered
    assert rendered.count("ERR_UNEXPECTED") == 1


def test_b1_a_genuine_failure_is_still_wrapped_with_context():
    """The guard must not swallow the case the handler exists for.

    A real pyarrow or LanceDB failure carries no code and no fix, so wrapping
    it is right - that is the difference between the two branches.
    """
    class Exploding:
        def create_table(self, *_args, **_kwargs):
            raise RuntimeError("lancedb said no")

    store = VectorStore.__new__(VectorStore)
    store._db = Exploding()
    store._table = None
    store.table_name = "chunks"
    store.dim = 384

    with pytest.raises(AppErrorException) as caught:
        store.ensure_table()

    rendered = caught.value.error.render()
    assert "lancedb said no" in rendered
    assert "chunks" in rendered


# --- B2 ---------------------------------------------------------------------

@pytest.mark.parametrize("chunks, rows, ready", [
    (100, 100, True),      # healthy
    (100, 96, True),       # 96% - close enough, do not nag
    (100, 94, False),      # below the bar
    (100, 0, False),       # the reported state
    (3355, 154, False),    # the owner's real numbers
    (0, 0, False),         # nothing indexed: not ready, and not a fault
])
def test_b2_vectors_ready_is_the_field_to_bind_to(store, chunks, rows, ready):
    """**Not `rows > 0`.**

    A vector store holding 5% of the corpus is not "ready", and calling it
    ready is exactly how a half-working search goes on looking healthy.
    """
    if chunks:
        file_id = store.upsert_file(path="/a.txt", size_bytes=1, mtime_ns=1,
                                    source_kind="file")
        store.replace_chunks(file_id, [{"text": f"c{n}"} for n in range(chunks)])

    coverage = store.vector_coverage(rows)

    assert coverage["vectors_ready"] is ready
    assert coverage["vector_rows"] == rows
    assert coverage["chunks_total"] == chunks


def test_b2_coverage_is_measured_against_the_total_not_the_flag(store):
    """The trap the CLI's own version documents having fallen into.

    `chunks_embedded` answers "did the write succeed for what we tried", which
    is not the question. On an index where 154 of 3,355 were ever attempted,
    the flag and the row count agree perfectly - and a check comparing those
    two reports everything healthy.
    """
    file_id = store.upsert_file(path="/a.txt", size_bytes=1, mtime_ns=1,
                                source_kind="file")
    store.replace_chunks(file_id, [{"text": f"c{n}"} for n in range(100)])
    store.mark_embedded(range(1, 6))          # only five ever attempted

    coverage = store.vector_coverage(5)

    assert coverage["vectors_ready"] is False
    assert coverage["missing"] == 95


def test_b2_a_missing_vector_store_is_not_a_crash(store):
    """`None` is what the caller has when the table does not exist at all."""
    file_id = store.upsert_file(path="/a.txt", size_bytes=1, mtime_ns=1,
                                source_kind="file")
    store.replace_chunks(file_id, [{"text": "one"}])

    coverage = store.vector_coverage(None)

    assert coverage["vectors_ready"] is False
    assert coverage["vector_rows"] == 0


# --- B3 ---------------------------------------------------------------------

def test_b3_repo_files_returns_one_repositorys_files_newest_first(store):
    repo_id = store.upsert_repo(r"D:\leasha", kind="work")
    other = store.upsert_repo(r"D:\tools", kind="work")
    for n in range(4):
        store.upsert_file(path=rf"D:\leasha\f{n}.py", size_bytes=10 + n,
                          mtime_ns=n, source_kind="file", repo_id=repo_id)
    store.upsert_file(path=r"D:\tools\t.py", size_bytes=1, mtime_ns=99,
                      source_kind="file", repo_id=other)

    rows = store.repo_files(repo_id)

    assert [r["path"] for r in rows] == [
        rf"D:\leasha\f{n}.py" for n in (3, 2, 1, 0)], "not newest first"
    assert all(key in rows[0] for key in ("path", "ext", "size_bytes", "mtime_ns"))


def test_b3_mail_never_appears_in_a_list_of_source_files(store):
    """`source_kind = 'file'` is in the WHERE clause for a reason."""
    repo_id = store.upsert_repo(r"D:\leasha", kind="work")
    store.upsert_file(path=r"D:\leasha\a.py", size_bytes=1, mtime_ns=1,
                      source_kind="file", repo_id=repo_id)
    store.upsert_file(path="pst://2007/1", size_bytes=1, mtime_ns=2,
                      source_kind="pst_message", repo_id=repo_id)

    rows = store.repo_files(repo_id)

    assert [r["path"] for r in rows] == [r"D:\leasha\a.py"]


def test_b3_the_limit_is_not_clamped(store):
    """The caller asks for `limit + 1` to tell "exactly 500" from "more".

    Quietly capping it makes those two indistinguishable, which is the
    difference between a complete list and a truncated one shown as complete.
    """
    repo_id = store.upsert_repo(r"D:\leasha", kind="work")
    for n in range(10):
        store.upsert_file(path=rf"D:\leasha\f{n}.py", size_bytes=1, mtime_ns=n,
                          source_kind="file", repo_id=repo_id)

    assert len(store.repo_files(repo_id, limit=5)) == 5
    assert len(store.repo_files(repo_id, limit=6)) == 6
    assert len(store.repo_files(repo_id, limit=10_000)) == 10


def test_b3_an_unknown_repository_is_empty_not_an_error(store):
    assert store.repo_files(99_999) == []
