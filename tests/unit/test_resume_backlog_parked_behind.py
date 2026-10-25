r"""A restart lists the meaning backlog beside the scan, not before it.

Layer: L3

2026-10-25, the owner: "when you stop and start index the resume is too slow".
On his restart that morning the catch-up phase spent 27 s listing 751,948
passages for the meaning model on the run's own thread before the scan could
start. `Pipeline._park_backlog_behind` now lists them on a thread of its own and
the run waits only until that read has begun. These pin the guarantees the old
order gave for free: the backlog is the one the run started with, it goes to the
front of the queue, and the end of the run waits for it.
"""

from __future__ import annotations

import collections
import threading
from pathlib import Path
from types import SimpleNamespace

from app.index import pipeline as module
from app.storage.sqlite_store import SqliteStore
import pytest


def _file_with_passages(store, tmp_path, name, count):
    path = tmp_path / name
    file_id = store.upsert_file(str(path), size_bytes=1, mtime_ns=1, ext="txt",
                                parent_dir=str(tmp_path), status="PARTIAL")
    store.replace_chunks(file_id, [{"ordinal": n, "text": f"{name} passage {n}"}
                                   for n in range(count)])
    return file_id


def test_the_backlog_is_the_database_as_the_read_began(tmp_path):
    """A passage written after the read began is not in it - even one written
    before a single row has been fetched."""
    with SqliteStore(tmp_path / "index.db") as store:
        old = _file_with_passages(store, tmp_path, "old.txt", 3)
        seen = []

        def write_meanwhile():
            # Another thread, as the run's writer is.
            worker = threading.Thread(
                target=lambda: _file_with_passages(store, tmp_path, "new.txt", 2))
            worker.start()
            worker.join()
            seen.append(True)

        batches = store.unembedded_by_file(batch_size=10, on_snapshot=write_meanwhile)

        assert seen == [True], "on_snapshot is called once"
        assert {fid for batch in batches for _cid, fid in batch} == {old}
        # The new passages are there for the next reader.
        later = store.unembedded_by_file(batch_size=10)
        assert len({fid for batch in later for _cid, fid in batch}) == 2


def test_on_snapshot_is_called_for_an_empty_backlog(tmp_path):
    with SqliteStore(tmp_path / "index.db") as store:
        called = []
        assert store.unembedded_by_file(on_snapshot=lambda: called.append(1)) == []
        assert called == [1]


class _SlowStore:
    """`unembedded_by_file` that begins its read, then waits to be let go."""

    def __init__(self, batches):
        self.batches = batches
        self.release = threading.Event()

    def unembedded_by_file(self, *, batch_size, first_folders, on_snapshot):
        on_snapshot()
        assert self.release.wait(10), "never released"
        return self.batches


def _bare(store):
    built = module.Pipeline.__new__(module.Pipeline)
    built.store = store
    built.config = SimpleNamespace(embed_batch=64, walk=SimpleNamespace(priority_roots=[]),
                                   read_order="newest")
    built._stop = threading.Event()
    built._interrupted = False
    built._text_first = lambda: True
    built._log = __import__("app.core.logging", fromlist=["logger"]).logger
    built._parked = collections.deque()
    built._parked_lock = threading.Lock()
    built._parked_busy = False
    built._parked_refused = False
    built._feeder_queue = SimpleNamespace(unfinished_tasks=0)
    built._embedding_now = threading.Event()
    built._stats_ref = SimpleNamespace(embed_batches=0, walk_complete=True)
    return built


def test_the_run_goes_on_while_the_backlog_is_listed_and_waits_for_it_at_the_end():
    store = _SlowStore([[(1, 10), (2, 10)], [(3, 11)]])
    built = _bare(store)
    stats = SimpleNamespace(enrichment_counts={})

    built._drain_unembedded(stats)               # returns at once: the read has begun

    assert stats.enrichment_counts["unembedded_chunk"] == 0
    assert built._parker_alive()
    assert not built._parked_idle(), "a backlog still being listed is parked work"
    # This run parks a batch of its own meanwhile.
    built._parked.append(([(9, 99)], False))

    store.release.set()
    built._await_backlog_parker()

    assert not built._parker_alive()
    assert list(built._parked) == [([(1, 10), (2, 10)], True), ([(3, 11)], True),
                                   ([(9, 99)], False)], "the backlog goes first, in order"
    assert built._stats_ref.embed_batches == 2


def test_a_stop_does_not_wait_for_the_listing():
    store = _SlowStore([[(1, 10)]])
    built = _bare(store)
    built._drain_unembedded(SimpleNamespace(enrichment_counts={}))
    built._stop.set()
    done = threading.Event()
    threading.Thread(target=lambda: (built._await_backlog_parker(), done.set())).start()
    assert done.wait(2), "a Stop waited for the backlog to be listed"
    store.release.set()


def test_catch_up_waits_for_the_listing_before_it_looks(monkeypatch):
    store = _SlowStore([[(1, 10)]])
    built = _bare(store)
    built._drain_unembedded(SimpleNamespace(enrichment_counts={}))
    looked = []
    built._start_pictures_beside_meaning = lambda stats: looked.append(
        list(built._parked)) or None
    built._parked_idle = lambda: True
    built._raise_if_feeder_failed = lambda: None
    threading.Timer(0.3, store.release.set).start()

    built._catch_up_meaning(SimpleNamespace(), None)

    assert looked == [[([(1, 10)], True)]]


def test_a_failed_listing_is_the_old_warning_and_the_run_goes_on():
    class _Broken:
        def unembedded_by_file(self, **_kw):
            raise RuntimeError("disk on fire")

    built = _bare(_Broken())
    said = []
    built._log = SimpleNamespace(warning=lambda *a: said.append(a[0]), info=lambda *a: None)
    built._drain_unembedded(SimpleNamespace(enrichment_counts={}))
    built._await_backlog_parker()
    assert said == ["could not check for unembedded chunks: {}"]
    assert not built._parked


def test_parked_work_waits_while_the_newest_first_scan_lists_files():
    built = _bare(SimpleNamespace())
    built._person_paused = lambda: False
    built.governor = SimpleNamespace(wait_while_throttled=lambda **_k: SimpleNamespace(
        action="run"))
    built._parked.append(([(1, 10)], True))

    built._stats_ref.walk_complete = False
    assert built._next_parked() is None, "the model took parked work during the scan"
    assert list(built._parked), "and it is still parked"

    built.config.read_order = "found"
    assert built._next_parked() == ([(1, 10)], True), "the 'as found' order reads as it walks"

    built.config.read_order = "newest"
    built._parked.append(([(2, 20)], True))
    built._stats_ref.walk_complete = True
    assert built._next_parked() == ([(2, 20)], True)


@pytest.mark.slow
def test_a_real_restart_still_fills_every_vector(tmp_path):
    """End to end: every passage an earlier run left is embedded once."""
    from tests.unit.test_enrichment_backlog import FakeVectors, _corpus, _embedder, _run

    root = _corpus(tmp_path / "docs")
    for n in range(1, 6):
        (root / f"doc{n}.txt").write_text(f"Valve {n} inspection notes " * 40,
                                          encoding="utf-8")
    vectors = FakeVectors()
    with SqliteStore(tmp_path / "index.db") as store:
        first = _run(store, root, vectors, _embedder())
        assert first.chunks and vectors.count() == first.chunks
        store.mark_all_unembedded()
        vectors.rows.clear()

        second = _run(store, root, vectors, _embedder(), force=False)

        assert second.vectors_repaired == first.chunks
        assert vectors.count() == first.chunks
        assert store.unembedded_by_file() == []
        assert Path(root).is_dir()
