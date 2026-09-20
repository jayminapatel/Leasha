r"""Index tuning §6: the speed work, and the evidence each piece rests on.

§6a gates this whole section, and it is built - so each item below can be
asserted rather than believed.

**§6e, chunk dedup, measured on a realistic shape**: twenty documents sharing a
long confidentiality notice produce 80 chunks, of which the model needs to see
22. All 80 vectors are still written. That is arithmetic avoided rather than a
trade-off taken - identical text produces an identical vector - which is why
this needed no quality gate, only a saving to report.

**§6f, bulk FTS, half-built on purpose.** The control changes behaviour today:
`on` merges whatever the run wrote, `off` leaves the segments alone. What is
deliberately absent is dropping the triggers, because the dirty flag that makes
an interrupted bulk run recoverable has to be written *before* they go - and
shipping the fast half without the safe half is how a corpus becomes
unsearchable with nothing to say why. A test pins that it stays absent until
the safe half arrives with it.
"""

from __future__ import annotations

import ast
import math
import tempfile
from pathlib import Path

import pytest

from app.index.embedder import Embedder, l2_normalise
from app.index.pipeline import Pipeline, PipelineConfig
from app.index.walker import WalkConfig
from app.storage.sqlite_store import SqliteStore

ROOT = Path(__file__).resolve().parents[2]

#: Long enough to become a chunk of its own, which is the shape that repeats.
NOTICE = ("This message and any attachments are confidential and intended "
          "solely for the addressee. If you have received it in error please "
          "notify the sender and delete it. ") * 40


class NoVectors:
    def ensure_table(self): pass
    def add(self, **kwargs): return len(kwargs.get("chunk_ids", []))
    def delete_by_file_ids(self, ids): return 0
    def maybe_create_index(self): pass
    def maybe_compact(self, force=False): pass
    def count(self): return 0


class Counting:
    """An encoder that records how many passages the model was shown."""

    def __init__(self) -> None:
        self.seen = 0

    def __call__(self, texts):
        texts = list(texts)
        self.seen += len(texts)
        return [l2_normalise([math.sin(abs(hash(text)) % 100 + index)
                              for index in range(8)]) for text in texts]


@pytest.fixture()
def repetitive() -> Path:
    root = Path(tempfile.mkdtemp()) / "docs"
    root.mkdir(parents=True)
    for number in range(20):
        (root / f"f{number}.txt").write_text(
            f"Report {number} about the pump station. " + NOTICE,
            encoding="utf-8")
    return root


def _run(corpus: Path, tmp_path: Path, name: str, **config):
    store = SqliteStore(tmp_path / f"{name}.db").connect()
    encoder = Counting()
    settings = PipelineConfig(walk=WalkConfig(roots=[corpus]), workers=2,
                              min_free_gb=0, required_free_gb=0, **config)
    stats = Pipeline(store, NoVectors(),
                     Embedder(dim=8, encoder=encoder), settings).run()
    store.close()
    return stats, encoder


# --- §6e: each distinct passage embedded once -------------------------------


def test_repeated_text_is_embedded_once(repetitive: Path, tmp_path: Path) -> None:
    """The measurement §6e asked for, on the shape that actually repeats:
    signatures, disclaimers and letterheads attached to everything."""
    plain, plain_encoder = _run(repetitive, tmp_path, "plain",
                                dedup_chunks=False)
    deduped, dedup_encoder = _run(repetitive, tmp_path, "dedup",
                                  dedup_chunks=True)

    assert plain.chunks == deduped.chunks, "the same corpus, either way"
    assert dedup_encoder.seen < plain_encoder.seen / 2
    assert deduped.chunks_deduped == plain_encoder.seen - dedup_encoder.seen


def test_every_chunk_still_gets_a_vector(repetitive: Path, tmp_path: Path) -> None:
    """**The half that makes it safe.** Sending fewer passages to the model
    must not mean storing fewer vectors: the saving is in the arithmetic, not
    in the coverage."""
    stats, _encoder = _run(repetitive, tmp_path, "coverage", dedup_chunks=True)

    assert stats.vectors == stats.chunks


def test_the_reused_vector_is_the_same_vector(tmp_path: Path) -> None:
    """Identical text produces an identical vector, so reuse changes nothing.
    Asserted directly rather than assumed, because the whole justification for
    having no quality gate rests on it."""
    calls: list = []

    def encode(texts):
        texts = list(texts)
        calls.append(texts)
        return [l2_normalise([float(len(text)), 1.0, 0.0, 0.0]) for text in texts]

    store = SqliteStore(tmp_path / "same.db").connect()
    pipeline = Pipeline(store, NoVectors(), Embedder(dim=4, encoder=encode),
                        PipelineConfig(walk=WalkConfig(roots=[tmp_path]),
                                       dedup_chunks=True))
    got = pipeline._embed_texts(["a", "b", "a", "b", "a"])   # noqa: SLF001

    assert calls == [["a", "b"]], "the model saw each distinct passage once"
    # Order 0b §6c: `_embed_texts` rows are `numpy.ndarray` now (each one
    # a row `Embedder.embed_all` yielded), so `==` on the bare arrays raises
    # ("truth value ... is ambiguous") rather than compares. `.tolist()`
    # first, same fix as every other caller in this codebase.
    got = [row.tolist() for row in got]
    assert got[0] == got[2] == got[4]
    assert got[1] == got[3]
    store.close()


def test_turning_it_off_restores_the_old_behaviour(
    repetitive: Path, tmp_path: Path
) -> None:
    """For anybody who suspects it and wants to compare."""
    stats, encoder = _run(repetitive, tmp_path, "off", dedup_chunks=False)

    assert stats.chunks_deduped == 0
    assert encoder.seen == stats.chunks


def test_a_batch_with_nothing_repeated_costs_nothing_extra(tmp_path: Path) -> None:
    """The common case on a corpus of distinct documents: the dedup pass must
    not become a tax on the runs it cannot help."""
    calls: list = []

    def encode(texts):
        texts = list(texts)
        calls.append(len(texts))
        return [[1.0, 0.0, 0.0, 0.0] for _ in texts]

    store = SqliteStore(tmp_path / "distinct.db").connect()
    pipeline = Pipeline(store, NoVectors(), Embedder(dim=4, encoder=encode),
                        PipelineConfig(walk=WalkConfig(roots=[tmp_path]),
                                       dedup_chunks=True))
    pipeline._embed_texts(["a", "b", "c"])       # noqa: SLF001

    assert calls == [3], "one call, all three, exactly as before"
    store.close()


def test_a_model_that_returns_the_wrong_count_is_not_trusted(tmp_path: Path
                                                             ) -> None:
    r"""**The one way this could corrupt an index silently.** If the model
    returned a different number of vectors than passages, mapping them back
    onto the chunks would attach the wrong meaning to the wrong text -
    permanently, invisibly, and with every test still green.

    It cannot happen, and this records *why*: `Embedder.embed` already checks
    the count and raises, so the mismatch never reaches the mapping. The guard
    inside `_embed_texts` is belt to that braces - reachable only by an
    injected encoder that bypasses `embed_all` - and it stays because the cost
    of being wrong here is an index nobody can trust and nobody can diagnose.
    """
    from app.core.errors import AppErrorException

    def confused(texts):
        texts = list(texts)
        return [[1.0, 0.0, 0.0, 0.0]] * (len(texts) + 1)

    store = SqliteStore(tmp_path / "confused.db").connect()
    pipeline = Pipeline(store, NoVectors(), Embedder(dim=4, encoder=confused),
                        PipelineConfig(walk=WalkConfig(roots=[tmp_path]),
                                       dedup_chunks=True))

    with pytest.raises(AppErrorException):
        pipeline._embed_texts(["a", "a", "b"])   # noqa: SLF001

    assert pipeline._stats_ref.chunks_deduped == 0   # noqa: SLF001
    store.close()


def test_the_saving_is_reported(repetitive: Path, tmp_path: Path) -> None:
    """Whether repeated text is worth avoiding is a question about somebody's
    corpus, and the run summary is the only place the answer appears."""
    stats, _encoder = _run(repetitive, tmp_path, "reported", dedup_chunks=True)

    assert stats.as_dict()["chunks_deduped"] == stats.chunks_deduped > 0


# --- §6f: the half that is built, and the half that is not ------------------


def test_off_leaves_the_word_index_unmerged(tmp_path: Path) -> None:
    merged: list = []

    class Store(SqliteStore):
        def optimize_fts(self):                  # noqa: D102
            merged.append(True)
            return True

    store = Store(tmp_path / "bulk.db").connect()
    pipeline = Pipeline(store, NoVectors(), Embedder(dim=4, encoder=lambda t: []),
                        PipelineConfig(walk=WalkConfig(roots=[tmp_path]),
                                       bulk_fts="off"))
    from app.index.pipeline import IndexStats

    pipeline._optimise_keyword_index(IndexStats(chunks=10 ** 9))  # noqa: SLF001

    assert merged == []
    store.close()


def test_on_merges_a_run_too_small_to_qualify(tmp_path: Path) -> None:
    merged: list = []

    class Store(SqliteStore):
        def optimize_fts(self):                  # noqa: D102
            merged.append(True)
            return True

    store = Store(tmp_path / "bulk_on.db").connect()
    pipeline = Pipeline(store, NoVectors(), Embedder(dim=4, encoder=lambda t: []),
                        PipelineConfig(walk=WalkConfig(roots=[tmp_path]),
                                       bulk_fts="on"))
    from app.index.pipeline import IndexStats

    pipeline._optimise_keyword_index(IndexStats(chunks=1))       # noqa: SLF001

    assert merged == [True]
    store.close()


def test_the_triggers_are_not_dropped_until_the_dirty_flag_exists() -> None:
    r"""**The safe half of §6f, pinned so the fast half cannot lose it.**

    Dropping the chunk FTS triggers is the fast half. The safe half is a flag
    written *before* they go, so an interrupted bulk run knows on resume that
    the word index is missing everything the run wrote. Without it, a run
    killed at hour forty leaves a corpus that is silently unsearchable - and
    nothing anywhere says why.

    2026-09-20: the flag has landed (`SqliteStore.drop_fts_triggers` sets
    `fts_dirty` first; `Pipeline.run` calls `check_and_rebuild_fts_if_dirty`), so
    as this test promised, it changed with it: it no longer bans dropping, it
    pins the *order*. The behavioural half - including the resume that used to
    leave the triggers off - is `test_fts_bulk_recovery.py`.
    """
    store_source = (ROOT / "app" / "storage" / "sqlite_store.py").read_text(encoding="utf-8")
    body = store_source.split("def drop_fts_triggers", 1)[1].split("\n    def ", 1)[0]
    assert 'set_state("fts_dirty", "1")' in body and "_suspend_content_triggers(" in body
    assert body.index('set_state("fts_dirty", "1")') < body.index("_suspend_content_triggers("), (
        "the FTS triggers are dropped before the dirty flag is written - an "
        "interrupted run would lose the word index silently")

    pipeline_source = (ROOT / "app" / "index" / "pipeline.py").read_text(encoding="utf-8")
    assert "check_and_rebuild_fts_if_dirty" in pipeline_source, (
        "a run no longer checks for an interrupted bulk run before it starts")


# --- the thread count reaches the model -------------------------------------


def test_the_onnx_thread_count_reaches_the_session() -> None:
    """Left alone, onnxruntime takes every core - right for a benchmark, wrong
    during a run where the extraction workers already hold several and the two
    multiply into a machine slower than it started."""
    built: dict = {}

    class FakeModel:
        def __init__(self, **kwargs):
            built.update(kwargs)

        def embed(self, texts):
            return [[1.0, 0.0, 0.0, 0.0] for _ in texts]

    import sys
    import types

    module = types.ModuleType("fastembed")
    module.TextEmbedding = FakeModel
    sys.modules["fastembed"] = module
    try:
        Embedder(dim=4, threads=3).warm_up()
        assert built.get("threads") == 3
        built.clear()
        Embedder(dim=4).warm_up()
        assert "threads" not in built, "no default of our own"
    finally:
        sys.modules.pop("fastembed", None)


def test_the_tuning_mode_reaches_the_run() -> None:
    r"""**The gap that made the mode switch cosmetic.** The panel resolved `0`
    to `Auto (4)` for display and the run read the literal `0`, so switching
    modes changed what was shown and nothing about what happened."""
    from app.core.compute_profile import ComputeProfile
    from app.index.resolve import resolve_for_run

    machine = ComputeProfile(logical_processors=12, physical_cores=10,
                             performance_cores=2, efficiency_cores=8,
                             ram_mb=32 * 1024)

    class Settings:
        index_tuning_mode = "defaults"
        index_workers = 9
        onnx_intra_op_threads = 0
        embed_batch = 0

    defaults = resolve_for_run(Settings(), None, machine)
    Settings.index_tuning_mode = "manual"
    manual = resolve_for_run(Settings(), None, machine)

    assert defaults.workers == 4, "the envelope decides"
    assert manual.workers == 9, "and Manual keeps what was typed"
    assert defaults.embed_batch > 0, "never a literal zero into a run"


def test_a_machine_that_cannot_be_examined_falls_back_to_the_stored_value(
) -> None:
    r"""Detection is allowed to fail - a locked-down machine, no PowerShell.

    **A machine nothing is known about is not a machine with one core.** The
    envelope reads missing fields as zero and hands back a ceiling of 1, so
    without this somebody's six workers became one on any machine that could
    not be examined. That is the same bug the tuning screen had, in the code
    path that actually runs the index.
    """
    from app.core.compute_profile import ComputeProfile
    from app.index.resolve import resolve_for_run

    class Settings:
        index_tuning_mode = "manual"
        index_workers = 6
        onnx_intra_op_threads = 2
        embed_batch = 64

    # An empty profile is what detection returns when it could not look. It is
    # deliberately *not* `None` here: `None` means "go and detect", and on a
    # machine that answers, being clamped to its real core count is correct.
    found = resolve_for_run(Settings(), None, profile=ComputeProfile())

    assert (found.workers, found.onnx_threads, found.embed_batch) == (6, 2, 64)


def test_the_resolution_the_screen_shows_is_the_one_the_run_uses() -> None:
    """One function, so the two cannot drift - which is the only reason the
    footer's numbers can be trusted against the run's."""
    source = (ROOT / "app" / "index" / "resolve.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = {
        f"{node.module}.{alias.name}"
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
        for alias in node.names
    }

    assert "app.core.envelope" in imported
    assert "envelope.for_setting" in source, (
        "the run must ask the same function the screen asks")
