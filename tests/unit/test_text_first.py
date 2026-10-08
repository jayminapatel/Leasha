r""""Make text searchable first" (`INDEX_TWO_PHASE`): reading never waits for the model.

Layer: L3

2026-10-08, the owner: "a lot of times threads are waiting for index writer".
The writer was not the slow part. Measured on the owner's 15-hour run: 54,555 s
embedding against 1,568 s writing. The hand-off to the meaning model holds one
batch, so once the model was busy the writer blocked, the readers' queue
filled, and every reader showed "Waiting for the index writer" - the whole run
read at the model's pace. The setting that promised words first was passed to
the pipeline and read by nothing.

Now a batch the model cannot take is **parked**: its files are marked INDEXED
(searchable by their words), only the chunk ids are kept, and the model reads
the text back from SQLite when it is free. A run that ends first loses
nothing - the passages are `embedded = 0`, and the next run parks them again.

Also here: the start-of-run repair cut one file's passages across two batches,
and each batch began by deleting that file's vectors - so a long file left
unembedded kept only its last batch. `SqliteStore.unembedded_by_file` never
cuts a file.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

from app.index.embedder import Embedder
from app.index.pipeline import PHASE_MEANING, Pipeline, PipelineConfig
from app.index.resources import ResourceLimits
from app.index.walker import WalkConfig
from app.storage.sqlite_store import SqliteStore
from app.ui.presenter.indexing import PHASE_WORDS

NO_PACING = ResourceLimits(pause_on_battery=False, cpu_percent=0)


class Vectors:
    """Behaves like the real store where it matters: a delete by file removes
    that file's rows, and a chunk embedded twice is an error."""

    def __init__(self) -> None:
        self.rows: dict[int, int] = {}          # chunk id -> file id
        self.added = 0

    def ensure_table(self) -> None:
        pass

    def delete_by_file_ids(self, file_ids) -> None:
        gone = {int(one) for one in file_ids}
        self.rows = {cid: fid for cid, fid in self.rows.items() if fid not in gone}

    def add(self, *, chunk_ids, file_ids, vectors, **_kw) -> int:
        chunk_ids, file_ids = list(chunk_ids), list(file_ids)
        for cid, fid in zip(chunk_ids, file_ids, strict=True):
            assert int(cid) not in self.rows, f"chunk {cid} was embedded twice"
            self.rows[int(cid)] = int(fid)
        self.added += len(chunk_ids)
        return len(chunk_ids)

    def count(self) -> int:
        return len(self.rows)

    def __getattr__(self, name):
        return lambda *args, **kwargs: False


def _files(root: Path, count: int) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for n in range(count):
        (root / f"f{n:02d}.txt").write_text(
            f"pump station {n} commissioning report", encoding="utf-8")
    return root


def _encoder(calls: list[int]):
    def encode(texts):
        calls.append(len(texts))
        return [[1.0, 0.0, 0.0, 0.0] for _ in texts]
    return encode


def _scalar(store: SqliteStore, sql: str) -> int:
    return int(store.conn.execute(sql).fetchone()[0])


def test_readers_do_not_wait_while_the_model_is_busy(tmp_path):
    """**The fault itself.** The model is held on its first batch; every file is
    still written and searchable by its words before it is let go."""
    root = _files(tmp_path / "docs", 12)
    started, release = threading.Event(), threading.Event()
    calls: list[int] = []

    def held(texts):
        calls.append(len(texts))
        if len(calls) == 1:
            started.set()
            assert release.wait(timeout=10), "the test never released the model"
        return [[1.0, 0.0, 0.0, 0.0] for _ in texts]

    vectors = Vectors()
    store = SqliteStore(tmp_path / "index.db").connect()
    config = PipelineConfig(walk=WalkConfig(roots=[root]), workers=1,
                            embed_batch=2, limits=NO_PACING)
    pipeline = Pipeline(store, vectors, Embedder(dim=4, encoder=held), config)
    runner = threading.Thread(target=pipeline.run, daemon=True)
    runner.start()
    try:
        assert started.wait(timeout=10), "the first batch never reached the model"
        deadline = time.monotonic() + 5
        written = 0
        while time.monotonic() < deadline:
            written = _scalar(store, "SELECT COUNT(*) FROM chunks")
            if written == 12:
                break
            time.sleep(0.02)
        # One batch is in the model's hands and one waits in the hand-off; the
        # other eight files were parked - written, and findable by their words.
        assert written == 12, (
            f"only {written} of 12 files were written while the model was busy - "
            "reading waited for it")
        searchable = _scalar(
            store, "SELECT COUNT(*) FROM files WHERE status IN ('PARTIAL', 'INDEXED')")
        assert searchable == 12
    finally:
        release.set()
        runner.join(timeout=20)
    assert not runner.is_alive(), "the run did not finish"
    # And the parked batches were all given meaning before the run ended.
    assert vectors.count() == 12
    assert _scalar(store, "SELECT COUNT(*) FROM chunks WHERE embedded = 0") == 0
    store.close()


def test_the_end_of_a_run_says_it_is_finishing_the_meaning(tmp_path):
    root = _files(tmp_path / "docs", 6)

    def slow(texts):
        time.sleep(0.2)
        return [[1.0, 0.0, 0.0, 0.0] for _ in texts]

    phases: list[str] = []
    store = SqliteStore(tmp_path / "index.db").connect()
    config = PipelineConfig(walk=WalkConfig(roots=[root]), workers=1,
                            embed_batch=1, limits=NO_PACING)
    pipeline = Pipeline(store, Vectors(), Embedder(dim=4, encoder=slow), config)
    pipeline.run(on_progress=lambda stats: phases.append(stats.phase))
    store.close()

    assert PHASE_MEANING in phases, phases
    assert PHASE_WORDS[PHASE_MEANING], "the window has nothing to say for it"


def test_a_stop_while_meaning_catches_up_loses_nothing_and_embeds_nothing_twice(tmp_path):
    root = _files(tmp_path / "docs", 10)
    vectors = Vectors()
    db = tmp_path / "index.db"

    class StopOnFirst:
        def __init__(self) -> None:
            self.pipeline: "Pipeline | None" = None
            self.calls = 0

        def encode(self, texts):
            self.calls += 1
            if self.calls == 1:
                time.sleep(0.3)                 # long enough for the rest to park
                self.pipeline.request_stop()
            return [[1.0, 0.0, 0.0, 0.0] for _ in texts]

    model = StopOnFirst()
    with SqliteStore(db) as store:
        config = PipelineConfig(walk=WalkConfig(roots=[root]), workers=1,
                                embed_batch=1, limits=NO_PACING)
        pipeline = Pipeline(store, vectors, Embedder(dim=4, encoder=model.encode), config)
        model.pipeline = pipeline
        pipeline.run()
        left = _scalar(store, "SELECT COUNT(*) FROM chunks WHERE embedded = 0")
    assert left > 0, "the stop came too late to leave anything for the next run"

    calls: list[int] = []
    with SqliteStore(db) as store:
        config = PipelineConfig(walk=WalkConfig(roots=[root]), workers=1,
                                embed_batch=1, limits=NO_PACING)
        second = Pipeline(store, vectors, Embedder(dim=4, encoder=_encoder(calls)),
                          config).run()
        assert _scalar(store, "SELECT COUNT(*) FROM chunks") == 10
        assert _scalar(store, "SELECT COUNT(*) FROM chunks WHERE embedded = 0") == 0
        assert _scalar(store, "SELECT COUNT(*) FROM files WHERE status = 'INDEXED'") == 10, (
            "a file whose vectors were filled in was not promoted from PARTIAL")
    assert second.indexed == 0, "a file the first run had written was read again"
    assert vectors.count() == 10, "a passage lost its vector"
    assert vectors.added == 10, "a passage was embedded twice across the two runs"
    assert second.vectors_repaired >= 1, second.vectors_repaired


def test_a_long_file_left_unembedded_is_repaired_whole(tmp_path):
    r"""**The repair bug, classic mode.** One file with many passages, left with
    no vectors, embed batch smaller than the file: every passage must end up
    with a vector, not only the last batch's."""
    root = tmp_path / "docs"
    root.mkdir()
    paragraph = ("The pump station commissioning report covers flow tests, "
                 "pressure tests and the alarm checks done on site. ") * 12
    (root / "long.txt").write_text("\n\n".join(paragraph for _ in range(40)),
                                   encoding="utf-8")
    vectors = Vectors()
    db = tmp_path / "index.db"
    with SqliteStore(db) as store:
        config = PipelineConfig(walk=WalkConfig(roots=[root]), workers=1,
                                limits=NO_PACING, two_phase=False)
        Pipeline(store, vectors, Embedder(dim=4, encoder=_encoder([])), config).run()
        chunks = _scalar(store, "SELECT COUNT(*) FROM chunks")
        assert chunks >= 6, f"the file made only {chunks} passages - make it longer"
        store.mark_all_unembedded()
    vectors.rows.clear()

    with SqliteStore(db) as store:
        config = PipelineConfig(walk=WalkConfig(roots=[root]), workers=1,
                                embed_batch=2, limits=NO_PACING, two_phase=False)
        Pipeline(store, vectors, Embedder(dim=4, encoder=_encoder([])), config).run()
    assert vectors.count() == chunks, (
        f"{vectors.count()} of {chunks} passages have a vector - a later batch of "
        "the same file deleted what an earlier one wrote")


def test_a_backlog_batch_never_cuts_a_file(tmp_path):
    with SqliteStore(tmp_path / "index.db") as store:
        rows = [(1, 5), (2, 5), (3, 7), (4, 7), (5, 7), (6, 9)]
        store.conn.execute("PRAGMA foreign_keys = OFF")
        for cid, fid in rows:
            store.conn.execute(
                "INSERT INTO chunks (id, file_id, ordinal, text) VALUES (?, ?, ?, 'x')",
                (cid, fid, cid))
        store.conn.commit()
        batches = store.unembedded_by_file(batch_size=2)
    files_seen: set[int] = set()
    for batch in batches:
        here = {fid for _cid, fid in batch}
        assert not (here & files_seen), f"a file was cut across batches: {batches}"
        files_seen |= here
    assert sum(len(batch) for batch in batches) == len(rows)
    assert [len(batch) for batch in batches] == [2, 3, 1]
