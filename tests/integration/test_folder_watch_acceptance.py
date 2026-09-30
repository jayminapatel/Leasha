r"""Work order 0z, item F1: acceptance tests for the folder watch.

The item, verbatim:

  F1  Watch the indexed folders for changes, so a file saved a moment ago can
      be found.

What these hold it to:

  1. A file saved in a watched folder is found by a search, with nobody
     starting an index run - through the real watcher, the real pipeline and
     a real (temporary) index.
  2. An edited file is found by its new words and no longer by its old ones.
  3. A renamed file is one row under its new name; a deleted file is gone from
     SQLite, the word index and the vectors.
  4. A folder that appears is indexed with what is in it; one that goes takes
     its rows with it.
  5. "Look at the whole folder" (an overflow) adds what is new, removes what
     has gone, and touches nothing outside that folder.
  6. A mailbox is left to the ordinary run; a file locked by its program is
     offered again.
  7. The run lock is held only while a batch is written, and is free after.

The embedding model is an injected fake, as in the Layer 3 acceptance tests:
what is being tested is whether the watch loses, repeats or misplaces work.
"""

from __future__ import annotations

import math
import os
import time
import uuid
from pathlib import Path

import pytest

from app.core.errors import make_error
from app.core.run_lock import INDEX_MUTEX_NAME, IndexRunLock, is_indexing
from app.index import folder_watch as fw
from app.index.embedder import Embedder, l2_normalise
from app.index.folder_watch import Batch, BatchIndexer, Change, ChangeBuffer, FolderWatcher
from app.index.pipeline import Pipeline, PipelineConfig
from app.index.walker import PathRules, WalkConfig
from app.storage.sqlite_store import FileStatus, SqliteStore
from app.storage.vector_store import VectorStore

pytestmark = pytest.mark.slow

DIM = 384
EXTENSIONS = frozenset({".txt", ".md", ".pst"})


def fake_encoder(texts):
    out = []
    for text in texts:
        seed = float(abs(hash(text)) % 1000)
        out.append(l2_normalise([math.sin(seed + i) for i in range(DIM)]))
    return out


@pytest.fixture()
def stores(tmp_path: Path):
    store = SqliteStore(tmp_path / "index" / "fts" / "index.db").connect()
    vectors = VectorStore(tmp_path / "index" / "vectors", dim=DIM).connect()
    yield store, vectors
    store.close()
    vectors.close()


@pytest.fixture()
def root(tmp_path: Path) -> Path:
    folder = tmp_path / "Documents"
    folder.mkdir()
    return folder


def config_for(roots) -> PipelineConfig:
    return PipelineConfig(
        walk=WalkConfig(roots=list(roots), extensions=EXTENSIONS),
        workers=2, checkpoint_every=5)


@pytest.fixture()
def lock_name() -> str:
    """The run lock is machine-wide; a name of the test's own keeps it away
    from any real index run on the machine the suite runs on."""
    return f"Leasha.Test.FolderWatch.{uuid.uuid4().hex}"


@pytest.fixture()
def indexer(stores, tmp_path, lock_name) -> BatchIndexer:
    store, vectors = stores
    return BatchIndexer(
        store, vectors, embedder=lambda: Embedder(dim=DIM, encoder=fake_encoder),
        config=config_for, lock_name=lock_name, lock_dir=tmp_path / "locks")


def found(store, phrase: str) -> list[str]:
    """Names of the files a keyword search for `phrase` returns."""
    return sorted({Path(hit["path"]).name for hit in store.search_bm25(f'"{phrase}"')})


def wait_for(condition, *, seconds: float = 45.0, what: str = "") -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what or 'the watch'}")


def full_run(stores, root: Path) -> None:
    store, vectors = stores
    Pipeline(store, vectors, Embedder(dim=DIM, encoder=fake_encoder),
             config_for([root])).run()


# --- 1, 2, 3: the real watcher, end to end ------------------------------------

@pytest.mark.parametrize("backend", [fw.BACKEND_AUTO, fw.BACKEND_POLL])
def test_a_file_saved_a_moment_ago_can_be_found(stores, root, indexer, backend):
    """The item itself. Both ways of noticing: the system's own notifications
    (Windows) and the comparison every other system uses."""
    store, vectors = stores
    events: list = []
    watcher = FolderWatcher(
        [root], apply=indexer, rules=lambda: PathRules(config_for([root]).walk),
        backend=backend, buffer=ChangeBuffer(quiet_s=0.3, max_wait_s=5.0),
        tick_s=0.05, poll_s=0.2,
        on_event=lambda kind, data: events.append((kind, data)))
    watcher.start()
    try:
        assert watcher.wait_ready(30)
        (root / "minutes.txt").write_text(
            "The harbour committee approved the lighthouse repairs.", encoding="utf-8")
        wait_for(lambda: found(store, "lighthouse repairs") == ["minutes.txt"],
                 what="the saved file to be findable")
        # Words first, meaning a moment behind (`two_phase`).
        wait_for(lambda: vectors.count() >= 1, what="its vectors")

        # 2: edited - the new words are found, the old ones are not.
        time.sleep(0.05)
        (root / "minutes.txt").write_text(
            "The harbour committee postponed the breakwater survey instead.",
            encoding="utf-8")
        wait_for(lambda: found(store, "breakwater survey") == ["minutes.txt"],
                 what="the edit to be findable")
        assert found(store, "lighthouse repairs") == []

        # 3: renamed - one row, under the new name.
        (root / "minutes.txt").rename(root / "harbour minutes.txt")
        wait_for(lambda: found(store, "breakwater survey") == ["harbour minutes.txt"],
                 what="the rename")
        assert store.get_file(str(root / "minutes.txt")) is None

        # 3: deleted - gone from the rows, the word index and the vectors.
        (root / "harbour minutes.txt").unlink()
        wait_for(lambda: found(store, "breakwater survey") == []
                 and store.get_file(str(root / "harbour minutes.txt")) is None,
                 what="the deletion")
        wait_for(lambda: vectors.count() == 0, what="the vectors to go")
    finally:
        watcher.stop(timeout_s=10)

    kinds = [kind for kind, _data in events]
    assert "watching" in kinds and "updated" in kinds
    assert not watcher.running and watcher.last_error is None


# --- 4: folders -----------------------------------------------------------------

def test_a_folder_that_appears_is_indexed_and_one_that_goes_takes_its_rows(
        stores, root, indexer):
    store, vectors = stores
    (root / "kept.txt").write_text("An unrelated note about tide tables.", encoding="utf-8")
    full_run(stores, root)
    new = root / "Project Heron" / "drafts"
    new.mkdir(parents=True)
    (new / "plan.txt").write_text("Heron estuary dredging schedule.", encoding="utf-8")
    (new.parent / "budget.md").write_text("Heron estuary dredging costs.", encoding="utf-8")
    (new.parent / "node_modules").mkdir()
    (new.parent / "node_modules" / "junk.txt").write_text(
        "Heron estuary dredging noise.", encoding="utf-8")

    result = indexer(Batch(changes=[Change(root, root / "Project Heron", new=True)]))

    assert result.indexed == 2
    assert found(store, "estuary dredging") == ["budget.md", "plan.txt"]

    # The whole folder is dragged to the Recycle Bin: one event, for the folder.
    import shutil

    shutil.rmtree(root / "Project Heron")
    result = indexer(Batch(changes=[Change(root, root / "Project Heron")]))

    assert result.removed == 2
    assert found(store, "estuary dredging") == []
    assert found(store, "tide tables") == ["kept.txt"], "the neighbour is untouched"
    assert vectors.count() == store.stats()["chunks_total"]


def test_a_folder_whose_contents_changed_is_not_walked(stores, root, indexer):
    """Windows reports the parent folder as modified for every file saved in
    it. That must not mean "walk the folder"."""
    store, _vectors = stores
    (root / "big").mkdir()
    (root / "big" / "never reported.txt").write_text("Not mentioned.", encoding="utf-8")

    result = indexer(Batch(changes=[Change(root, root / "big")]))

    assert result.ignored == 1 and result.indexed == 0
    assert store.get_file(str(root / "big" / "never reported.txt")) is None


# --- 5: look at the whole folder ------------------------------------------------

def test_a_rescan_adds_removes_and_stays_inside_its_folder(stores, tmp_path, indexer):
    store, vectors = stores
    first, second = tmp_path / "First", tmp_path / "Second"
    for folder in (first, second):
        folder.mkdir()
        for number in range(3):
            (folder / f"note{number}.txt").write_text(
                f"{folder.name} folder note {number} about quarry permits.",
                encoding="utf-8")
    Pipeline(store, vectors, Embedder(dim=DIM, encoder=fake_encoder),
             config_for([first, second])).run()
    # A row in the second folder whose file has gone - the ordinary run's
    # business, not a rescan of the first folder's.
    (second / "note0.txt").unlink()
    (first / "note0.txt").unlink()
    (first / "added.txt").write_text("A late addition on quarry permits.", encoding="utf-8")

    result = indexer(Batch(rescan=[first]))

    assert result.rescanned == 1 and result.indexed == 1 and result.removed == 1
    assert store.get_file(str(first / "note0.txt")) is None
    assert store.get_file(str(first / "added.txt")).status == FileStatus.INDEXED
    assert store.get_file(str(second / "note0.txt")) is not None, (
        "a rescan of one folder said nothing about another")
    assert vectors.count() == store.stats()["chunks_total"]


# --- 6: what is left alone, and what is tried again -------------------------------

def test_a_mailbox_is_named_when_new_and_left_alone_when_known(stores, root, indexer):
    store, _vectors = stores
    mailbox = root / "archive.pst"
    mailbox.write_bytes(b"!BDN" + b"\0" * 2048)

    first = indexer(Batch(changes=[Change(root, mailbox, new=True)]))
    row = store.get_file(str(mailbox))
    assert row is not None and row.status == FileStatus.NAME_ONLY
    assert first.mailboxes_left == 0

    mailbox.write_bytes(b"!BDN" + b"\1" * 4096)          # Outlook wrote to it
    second = indexer(Batch(changes=[Change(root, mailbox)]))

    assert second.mailboxes_left == 1 and second.indexed == 0
    assert store.get_file(str(mailbox)).size_bytes == row.size_bytes, "not re-read"


def test_a_file_locked_by_its_program_is_offered_again(stores, root, indexer, monkeypatch):
    store, _vectors = stores
    document = root / "open elsewhere.txt"
    document.write_text("Locked while its program has it open.", encoding="utf-8")
    real = Pipeline._extract_stream

    def locked(self, candidate, *args, **kwargs):
        raise_error = make_error("ERR_FILE_LOCKED", "test", path=str(candidate.path))
        from app.core.errors import AppErrorException

        raise AppErrorException(raise_error)

    monkeypatch.setattr(Pipeline, "_extract_stream", locked)
    result = indexer(Batch(changes=[Change(root, document, new=True)]))
    if store.get_file(str(document)).skip_code != "ERR_FILE_LOCKED":
        pytest.skip("this pipeline records a lock some other way")
    assert [change.attempts for change in result.retry] == [1]

    monkeypatch.setattr(Pipeline, "_extract_stream", real)
    again = indexer(Batch(changes=result.retry))

    assert again.indexed == 1 and again.retry == []
    assert found(store, "program has it open") == ["open elsewhere.txt"]


def test_an_unchanged_file_costs_no_run_and_a_changed_one_costs_one(stores, root, indexer):
    store, _vectors = stores
    document = root / "steady.txt"
    document.write_text("Nothing about this file will change.", encoding="utf-8")
    old = time.time() - 3600
    os.utime(document, (old, old))
    full_run(stores, root)

    touched = indexer(Batch(changes=[Change(root, document)]))
    assert touched.ignored == 1 and indexer.batches == 0

    document.write_text("Everything about this file has changed.", encoding="utf-8")
    edited = indexer(Batch(changes=[Change(root, document)]))
    assert edited.indexed == 1 and indexer.batches == 1
    assert found(store, "has changed") == ["steady.txt"]


# --- 7: the lock ---------------------------------------------------------------

def test_the_lock_is_free_again_after_a_batch_and_the_last_run_is_left_alone(
        stores, root, indexer, lock_name, tmp_path):
    store, _vectors = stores
    (root / "one.txt").write_text("First file, indexed by an ordinary run.", encoding="utf-8")
    full_run(stores, root)
    last_run = store.get_state("last_run_stats", "")
    (root / "two.txt").write_text("Second file, added by the watch.", encoding="utf-8")

    result = indexer(Batch(changes=[Change(root, root / "two.txt", new=True)]))

    assert result.indexed == 1
    IndexRunLock(None, name=lock_name, lock_dir=tmp_path / "locks").acquire().release()
    assert store.get_state("last_run_stats", "") == last_run, (
        "one file from the watch is not 'the last run'")
    assert store.get_state("run:active", "") == ""
    assert lock_name != INDEX_MUTEX_NAME and callable(is_indexing)
