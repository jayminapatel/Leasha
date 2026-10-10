r"""The embedding gap: chunks in SQLite with no vector in LanceDB.

Layer: L3

**The defect these pin was open for months and was diagnosed backwards.** On the
owner's index, 154 of 3,355 passages had a vector - meaning-based search silently
doing a twentieth of its job. Every previous fix was to the *reporting*: `stats`
and `doctor` learned to compare the two stores and say so. Nothing had ever
looked at how the two stores came to disagree.

The cause turned out to be a window, and the window was structural. `_write_one`
committed a file's chunks and deleted that file's old vectors immediately, while
the replacement vectors were deferred to `_embed_pending` - up to 256 chunks and
one lazy ONNX model load later. Every abort inside that window was uncounted
loss.

**What made a batch-sized fault corpus-sized was that it compounded.** A file
whose flush never happened stays `PENDING`, so the next run picks it up, reaches
the delete, and destroys the vectors of everything it re-reaches *before* failing
in the same place. Each run left coverage lower than it found it. `--force` - the
natural thing to try on seeing a gap - put every file on that path.

So the load-bearing test here is not "a flush writes vectors". It is
`test_a_failed_run_does_not_leave_less_coverage_than_it_found`: the ratchet is
what turned this into 95%, and a fix that stops the loss without stopping the
ratchet would look correct on a green first run and rot exactly as before.

These drive the real `Pipeline` against a real `SqliteStore`. The archive work
established why: twenty-nine passing unit tests sat alongside an extractor that
could not run at all, because they called the module rather than the pipeline.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.errors import AppErrorException, make_error
from app.index.embedder import Embedder
from app.index.pipeline import Pipeline, PipelineConfig
from app.index.walker import WalkConfig
from app.storage.sqlite_store import SqliteStore

DIM = 8


def _encode(texts):
    """Deterministic, dependency-free, and never the thing under test."""
    out = []
    for text in texts:
        seed = sum(ord(ch) for ch in text) or 1
        raw = [((seed * (i + 3)) % 17) / 17 for i in range(DIM)]
        length = sum(value * value for value in raw) ** 0.5 or 1.0
        out.append([value / length for value in raw])
    return out


def _embedder(*, on_embed=None, on_warm=None) -> Embedder:
    def encode(texts):
        if on_embed is not None:
            on_embed(texts)
        return _encode(texts)

    made = Embedder(dim=DIM, encoder=encode)
    if on_warm is not None:
        made.warm_up = on_warm            # type: ignore[method-assign]
    return made


class FakeVectors:
    """A vector store that records what it was asked to do, in order.

    The order is the whole subject: `delete` before `add` for a file, with
    nothing in between that can fail, is the property being tested.
    """

    def __init__(self, *, fail_add: bool = False) -> None:
        self.rows: dict[int, int] = {}          # chunk_id -> file_id
        self.calls: list[tuple[str, list[int]]] = []
        self.fail_add = fail_add

    def ensure_table(self) -> None:
        pass

    def delete_by_file_ids(self, file_ids) -> None:
        wanted = [int(one) for one in file_ids]
        self.calls.append(("delete", wanted))
        for chunk_id, file_id in list(self.rows.items()):
            if file_id in wanted:
                del self.rows[chunk_id]

    def add(self, *, chunk_ids, file_ids, vectors) -> int:
        self.calls.append(("add", [int(one) for one in chunk_ids]))
        if self.fail_add:
            raise RuntimeError("LanceDB is unavailable")
        for chunk_id, file_id in zip(chunk_ids, file_ids, strict=True):
            self.rows[int(chunk_id)] = int(file_id)
        return len(list(chunk_ids))

    def maybe_compact(self, **_kwargs) -> bool:
        return False

    def maybe_create_index(self, **_kwargs) -> bool:
        return False

    def count(self) -> int:
        return len(self.rows)


def _corpus(root: Path, files: int = 3) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for index in range(files):
        (root / f"doc{index}.txt").write_text(
            f"Pump station {index} commissioning report for the northern site.",
            encoding="utf-8")
    return root


def _reword(root: Path) -> None:
    """Give every document new words. 2026-10-10: a forced re-read of the
    same words no longer sends a passage to the model - `replace_chunks`
    keeps its row and its vector - so a test about what a re-embedding does
    has to change the text to get one."""
    for path in sorted(root.glob("doc*.txt")):
        path.write_text(path.read_text(encoding="utf-8").replace("northern", "southern"),
                        encoding="utf-8")


def _run(store, root, vectors, embedder, **config):
    pipeline = Pipeline(
        store, vectors, embedder,
        PipelineConfig(walk=WalkConfig(roots=[root]), workers=1, **config),
    )
    return pipeline.run()


# ---------------------------------------------------------------------------
# The ratchet - the reason this was 95% rather than one batch
# ---------------------------------------------------------------------------

def test_a_failed_run_does_not_leave_less_coverage_than_it_found(tmp_path):
    r"""**The load-bearing one.**

    Index successfully, then re-index with the embedder broken. The old vectors
    must survive: a run that cannot embed has nothing better to offer than what
    is already there, and deleting first meant every failed attempt ratcheted
    coverage downwards while reporting only the failure.
    """
    root = _corpus(tmp_path / "docs")
    vectors = FakeVectors()

    with SqliteStore(tmp_path / "index.db") as store:
        first = _run(store, root, vectors, _embedder())
        assert first.chunks and vectors.count() == first.chunks

        before = dict(vectors.rows)

        def explode(_texts):
            raise AppErrorException(make_error(
                "ERR_MODEL_LOAD", "index.embedder", details="deliberate"))

        _reword(root)
        with pytest.raises(AppErrorException):
            _run(store, root, vectors, _embedder(on_embed=explode), force=True)

        assert vectors.rows == before, (
            "a run that could not embed destroyed the vectors it could not replace")


def test_the_delete_happens_in_the_flush_immediately_before_the_add(tmp_path):
    """The ordering, stated as an ordering rather than as an outcome."""
    root = _corpus(tmp_path / "docs", files=2)
    vectors = FakeVectors()

    with SqliteStore(tmp_path / "index.db") as store:
        _run(store, root, vectors, _embedder())
        vectors.calls.clear()
        _reword(root)
        _run(store, root, vectors, _embedder(), force=True)

    kinds = [kind for kind, _ids in vectors.calls]
    assert kinds, "the re-index wrote nothing at all"
    assert kinds == sorted(kinds, key=lambda k: 0 if k == "delete" else 1)[:len(kinds)] \
        or kinds[0] == "delete"
    assert kinds.count("add") == 1, "one flush for a corpus this small"
    assert kinds.index("delete") < kinds.index("add"), "the add must follow the delete"


def test_a_document_that_chunks_to_nothing_takes_its_vectors_with_it(tmp_path):
    r"""The one case the flush cannot clean up after.

    A file that used to chunk and now does not never reaches `pending`, so
    `_embed_pending` never sees it and would never delete its vectors. Orphans
    point at chunk rows that no longer exist: they cost the ANN index its
    accuracy and can resurface content the file no longer holds.

    Driven through `_write_one` rather than through a file, deliberately. A
    whitespace-only `.txt` does **not** reach here - the extractor produces no
    document at all, so it becomes an `ERR_NO_TEXT_LAYER` skip and both stores
    keep what they had, which is consistent and correct (asserted below). The
    gap needs a document that *has* text and chunks to nothing, which is a
    property of the chunker and of archive members rather than of any file this
    test can write.
    """
    from app.index.pipeline import _Extracted
    from app.index.walker import Candidate

    root = _corpus(tmp_path / "docs", files=1)
    target = root / "doc0.txt"
    vectors = FakeVectors()

    with SqliteStore(tmp_path / "index.db") as store:
        _run(store, root, vectors, _embedder())
        assert vectors.count() == 1

        pipeline = Pipeline(
            store, vectors, _embedder(),
            PipelineConfig(walk=WalkConfig(roots=[root]), workers=1))
        stat = target.stat()
        produced = pipeline._write_one(_Extracted(
            candidate=Candidate(path=target, size_bytes=stat.st_size,
                                mtime_ns=stat.st_mtime_ns),
            content_hash="deadbeef",
            chunks=[],
        ))

        assert produced == [], "an empty document must not join the flush"
        assert vectors.count() == 0, "vectors left behind for chunks that are gone"


def test_a_file_nothing_can_extract_leaves_both_stores_alone(tmp_path):
    """The neighbouring case, and it must not be confused with the one above.

    A file emptied on disk becomes a skip. Nothing is rewritten, so the chunks
    and the vectors both stay - stale together, which is what a *retry* is for
    and is not a gap.
    """
    root = _corpus(tmp_path / "docs", files=1)
    vectors = FakeVectors()

    with SqliteStore(tmp_path / "index.db") as store:
        _run(store, root, vectors, _embedder())

        (root / "doc0.txt").write_text("   \n\t \n", encoding="utf-8")
        stats = _run(store, root, vectors, _embedder(), force=True)

        chunks = store.conn.execute("SELECT COUNT(*) AS n FROM chunks").fetchone()
        assert stats.skipped_by_code == {"ERR_NO_TEXT_LAYER": 1}
        assert int(chunks["n"]) == vectors.count() == 1, "the two stores disagree"


# ---------------------------------------------------------------------------
# Saying so - the number whose absence hid this
# ---------------------------------------------------------------------------

def test_the_run_counts_the_vectors_it_wrote(tmp_path):
    root = _corpus(tmp_path / "docs")
    vectors = FakeVectors()

    with SqliteStore(tmp_path / "index.db") as store:
        stats = _run(store, root, vectors, _embedder())

    assert stats.chunks > 0
    assert stats.vectors == stats.chunks
    assert stats.embed_failures == 0
    assert stats.as_dict()["vectors"] == stats.vectors


def test_a_run_that_embedded_nothing_says_so_rather_than_reporting_success(tmp_path):
    r"""**A run wrote 3,355 chunks and 0 vectors and called itself successful.**

    `IndexStats` had a `chunks` counter and no vector counter at all, so the
    only record of the gap was the two stores disagreeing - a comparison made
    afterwards, by a diagnostic nobody runs against a run that said it worked.
    """
    root = _corpus(tmp_path / "docs")
    vectors = FakeVectors(fail_add=True)

    with SqliteStore(tmp_path / "index.db") as store, pytest.raises(RuntimeError):
        _run(store, root, vectors, _embedder())

    from app.cli import _print_vector_coverage

    class _Stats:
        chunks, vectors, embed_failures = 3355, 0, 13

    printed: list[str] = []
    import builtins

    real_print = builtins.print
    builtins.print = lambda *args, **kw: printed.append(" ".join(str(a) for a in args))
    try:
        _print_vector_coverage(_Stats())
    finally:
        builtins.print = real_print

    joined = "\n".join(printed)
    assert "0 of 3,355" in joined
    assert "reembed" in joined, "a gap without a remedy is a gap somebody researches"


def test_a_healthy_run_says_nothing_about_vectors(tmp_path):
    """A line reading "3,355 of 3,355" on every run teaches people to skim."""
    from app.cli import _print_vector_coverage

    class _Stats:
        chunks, vectors, embed_failures = 500, 500, 0

    printed: list[str] = []
    import builtins

    real_print = builtins.print
    builtins.print = lambda *args, **kw: printed.append(" ".join(str(a) for a in args))
    try:
        _print_vector_coverage(_Stats())
    finally:
        builtins.print = real_print

    assert printed == []


# ---------------------------------------------------------------------------
# Failing before anything is written
# ---------------------------------------------------------------------------

def test_a_model_that_cannot_load_costs_nothing_at_all(tmp_path):
    r"""**Not one chunk committed.**

    The model loaded lazily, on the first `embed()` - inside the flush, after a
    few hundred chunks were already committed and their vectors deleted. The
    failure is the same either way; what differs is whether it orphans a batch
    on every attempt for ever.
    """
    root = _corpus(tmp_path / "docs")
    vectors = FakeVectors()

    def refuse():
        raise AppErrorException(make_error(
            "ERR_MODEL_LOAD", "index.embedder", details="deliberate"))

    with SqliteStore(tmp_path / "index.db") as store:
        with pytest.raises(AppErrorException):
            _run(store, root, vectors, _embedder(on_warm=refuse))

        rows = store.conn.execute("SELECT COUNT(*) AS n FROM chunks").fetchone()
        assert int(rows["n"]) == 0, "chunks committed before the model was known good"
        assert vectors.count() == 0


def test_an_embedder_without_warm_up_still_runs(tmp_path):
    """Half the suite passes a double. Requiring the method would break them."""
    root = _corpus(tmp_path / "docs", files=1)
    vectors = FakeVectors()

    plain = Embedder(dim=DIM, encoder=_encode)
    assert hasattr(plain, "warm_up")

    class NoWarmUp:
        dim = DIM
        batch_size = 256

        def embed_all(self, texts):
            return _encode(list(texts))

    with SqliteStore(tmp_path / "index.db") as store:
        stats = _run(store, root, vectors, NoWarmUp())

    assert stats.indexed == 1


# ---------------------------------------------------------------------------
# Reporting must never cost the flush
# ---------------------------------------------------------------------------

def test_a_failing_progress_callback_does_not_cost_the_vectors(tmp_path):
    r"""The CLI's progress line prints a *filename* to a Windows console.

    One path outside cp1252 was a `UnicodeEncodeError` that escaped `_consume`
    before the final flush - so a character in a filename cost the whole pending
    batch its vectors, with the chunks already committed.
    """
    root = _corpus(tmp_path / "docs", files=3)
    vectors = FakeVectors()

    def hostile(_stats):
        raise UnicodeEncodeError("charmap", "x", 0, 1, "undefined")

    with SqliteStore(tmp_path / "index.db") as store:
        pipeline = Pipeline(
            store, vectors, _embedder(),
            PipelineConfig(walk=WalkConfig(roots=[root]), workers=1,
                           checkpoint_every=1),
        )
        stats = pipeline.run(on_progress=hostile)

    assert stats.chunks > 0
    assert vectors.count() == stats.chunks, "reporting cost the batch its vectors"


# ---------------------------------------------------------------------------
# The repair path, and the cached counts
# ---------------------------------------------------------------------------

def test_dropping_the_table_resets_every_cached_count(tmp_path):
    r"""`drop()` reset `_table` and `_indexed_at_rows` and not the other two.

    The run after a `reembed --all` therefore believed an empty table still held
    thousands of rows, which re-enabled the per-document delete that `db17d1c`
    removed - a dataset version per document, on precisely the run that has the
    most documents to write.
    """
    from app.storage.vector_store import VectorStore

    with VectorStore(tmp_path / "vectors", dim=DIM) as store:
        store.ensure_table()
        store.add(chunk_ids=[1, 2], file_ids=[1, 1], vectors=_encode(["a", "b"]))
        assert store._approx_rows == 2

        store.drop()

        assert store._approx_rows == 0
        assert store._since_compact == 0
        assert store._indexed_at_rows == 0
