"""Layer 3: the embedder.

Every test here runs with an injected encoder and no model on disk, because the
behaviours worth pinning down are not "does bge work" - it does - but the three
things that fail *silently* around it:

  * a dimension mismatch, which is what changing `EMBED_MODEL` looks like;
  * an un-normalised vector, which raises nothing and ranks wrong forever;
  * a batch that comes back the wrong length, which would pair chunks with other
    chunks' vectors and produce search results that are subtly, unaccountably wrong.
"""

from __future__ import annotations

import math
import threading

import pytest

from app.core.errors import AppErrorException
from app.index.embedder import EMBED_BATCH, Embedder, l2_normalise


def unit(seed: float, dim: int = 384) -> list[float]:
    """A deterministic unit vector of the right width."""
    raw = [math.sin(seed + i) for i in range(dim)]
    return l2_normalise(raw)


def encoder_for(dim: int = 384, *, normalised: bool = True):
    calls: list[list[str]] = []

    def encode(texts):
        calls.append(list(texts))
        out = []
        for index, _text in enumerate(texts):
            vector = unit(float(index), dim)
            out.append(vector if normalised else [v * 7.5 for v in vector])
        return out

    encode.calls = calls  # type: ignore[attr-defined]
    return encode


# --- basics -----------------------------------------------------------------

def test_returns_one_vector_per_text() -> None:
    embedder = Embedder(encoder=encoder_for())
    vectors = embedder.embed(["one", "two", "three"])
    assert len(vectors) == 3
    assert all(len(v) == 384 for v in vectors)


def test_empty_input_does_not_touch_the_model() -> None:
    """A file that produced no chunks must not load a 130MB model to say so."""
    embedder = Embedder()          # no encoder: loading would fail here
    assert embedder.embed([]) == []
    assert not embedder.loaded


def test_spec_batch_size() -> None:
    """**Raised from 64 to 256, and the reason is that 256 was already there.**

    `pipeline.EMBED_BATCH` was 256 and documented as "the single biggest
    throughput lever in the whole pipeline" - and `embed_all` re-split every
    gathered batch back down to this module's 64, so the lever reached the
    model as a quarter of itself. Two constants for one number, disagreeing
    silently, which is exactly what `docs/WORKORDER-terabyte-scale.md` §4 asked
    to be *verified rather than assumed*.

    There is now one definition and the pipeline imports it.
    """
    from app.index.pipeline import EMBED_BATCH as PIPELINE_BATCH

    assert EMBED_BATCH == 256
    assert PIPELINE_BATCH == EMBED_BATCH, "two constants for one number, again"
    assert Embedder(encoder=encoder_for()).batch_size == 256


def test_the_pipelines_batch_actually_reaches_the_model() -> None:
    """The check the constants alone cannot make.

    A caller who sets `PipelineConfig(embed_batch=...)` means the embedding
    batch. Before this, they got a different number of store writes and no
    change at all to the size of an ONNX call.
    """
    from app.index.pipeline import Pipeline, PipelineConfig
    from app.index.walker import WalkConfig

    embedder = Embedder(encoder=encoder_for(), batch_size=64)
    Pipeline(
        store=object(), vectors=object(), embedder=embedder,
        config=PipelineConfig(walk=WalkConfig(roots=[]), embed_batch=512),
    )

    assert embedder.batch_size == 512


def test_invalid_batch_size_is_rejected() -> None:
    with pytest.raises(ValueError):
        Embedder(encoder=encoder_for(), batch_size=0)


# --- batching ---------------------------------------------------------------

def test_embed_all_batches_at_the_configured_size() -> None:
    encoder = encoder_for()
    embedder = Embedder(encoder=encoder, batch_size=10)

    vectors = list(embedder.embed_all([f"chunk {i}" for i in range(25)]))
    assert len(vectors) == 25
    assert [len(call) for call in encoder.calls] == [10, 10, 5]


def test_embed_all_is_lazy() -> None:
    """On a million chunks, materialising every vector before writing one row
    would be several GB of list."""
    encoder = encoder_for()
    embedder = Embedder(encoder=encoder, batch_size=4)

    stream = embedder.embed_all([f"c{i}" for i in range(100)])
    next(stream)
    assert len(encoder.calls) == 1, "only the first batch should have run"


def test_order_is_preserved_across_batches() -> None:
    """A chunk paired with another chunk's vector produces search results that
    are wrong in a way nobody can diagnose."""
    seen: list[str] = []

    def encode(texts):
        seen.extend(texts)
        return [unit(float(hash(t) % 100)) for t in texts]

    embedder = Embedder(encoder=encode, batch_size=3)
    texts = [f"chunk-{i}" for i in range(10)]
    list(embedder.embed_all(texts))
    assert seen == texts


# --- the dimension guard ----------------------------------------------------

def test_wrong_dimension_is_caught_with_both_numbers() -> None:
    """What changing EMBED_MODEL in .env actually looks like."""
    embedder = Embedder(model_name="some/other-model", dim=384, encoder=encoder_for(768))

    with pytest.raises(AppErrorException) as caught:
        embedder.embed(["text"])

    error = caught.value.error
    assert error.code == "ERR_MODEL_LOAD"
    assert "768" in error.details and "384" in error.details
    assert "EMBED_DIM" in error.suggestion


def test_dimension_is_only_checked_once() -> None:
    encoder = encoder_for()
    embedder = Embedder(encoder=encoder)
    embedder.embed(["a"])
    embedder.embed(["b"])
    assert embedder._checked_dim


def test_a_matching_dimension_passes_quietly() -> None:
    Embedder(dim=8, encoder=encoder_for(8)).embed(["x"])


# --- normalisation ----------------------------------------------------------

def test_vectors_come_back_unit_length() -> None:
    """Cosine ranking degrades quietly without this - no error, just slightly
    wrong ordering for the life of the index."""
    embedder = Embedder(encoder=encoder_for(normalised=False))
    for vector in embedder.embed(["a", "b"]):
        assert math.isclose(math.sqrt(sum(v * v for v in vector)), 1.0, abs_tol=1e-9)


def test_already_normalised_vectors_are_left_alone() -> None:
    embedder = Embedder(encoder=encoder_for(normalised=True))
    expected = unit(0.0)
    assert embedder.embed(["a"])[0] == expected


def test_l2_normalise_handles_a_zero_vector() -> None:
    """Dividing by a zero magnitude would be a ZeroDivisionError on an input
    that is merely unusual, not invalid."""
    assert l2_normalise([0.0, 0.0, 0.0]) == [0.0, 0.0, 0.0]


def test_l2_normalise_is_idempotent() -> None:
    once = l2_normalise([3.0, 4.0])
    assert l2_normalise(once) == pytest.approx(once)


# --- failure modes ----------------------------------------------------------

def test_a_short_batch_is_refused_rather_than_misaligned() -> None:
    """Silently accepting fewer vectors than texts would pair chunks with the
    wrong vectors from that point on."""
    embedder = Embedder(encoder=lambda _texts: [unit(0.0)])

    with pytest.raises(AppErrorException) as caught:
        embedder.embed(["a", "b", "c"])
    assert "got 1" in caught.value.error.details


def test_an_encoder_that_raises_becomes_an_apperror() -> None:
    def explode(_texts):
        raise RuntimeError("onnxruntime segfaulted")

    with pytest.raises(AppErrorException) as caught:
        Embedder(encoder=explode).embed(["a"])

    error = caught.value.error
    assert error.code == "ERR_MODEL_LOAD"
    assert "onnxruntime" in error.details
    assert error.suggestion.strip()


def test_missing_fastembed_names_the_install_command(monkeypatch: pytest.MonkeyPatch) -> None:
    import builtins

    real_import = builtins.__import__

    def refuse(name, *args, **kwargs):
        if name == "fastembed":
            raise ImportError("No module named 'fastembed'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", refuse)

    with pytest.raises(AppErrorException) as caught:
        Embedder().warm_up()

    assert caught.value.error.code == "ERR_MODEL_LOAD"
    assert "pip install fastembed" in caught.value.error.suggestion


# --- lazy loading -----------------------------------------------------------

def test_the_model_is_not_loaded_until_it_is_needed() -> None:
    loads = []

    def encode(texts):
        loads.append(1)
        return [unit(0.0) for _ in texts]

    embedder = Embedder(encoder=encode)
    assert not loads
    embedder.embed(["now"])
    assert len(loads) == 1


def test_warm_up_is_idempotent_and_thread_safe() -> None:
    """Layer 4 calls this from a startup thread; two threads racing must load
    the model once, not twice."""
    attempts: list[int] = []
    barrier = threading.Barrier(4)

    class Slow(Embedder):
        def _ensure_encoder(self):
            if self._encoder is None:
                with self._lock:
                    if self._encoder is None:
                        attempts.append(1)
                        self._encoder = lambda texts: [unit(0.0) for _ in texts]
            return self._encoder

    embedder = Slow()

    def worker():
        barrier.wait()
        embedder.warm_up()

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(attempts) == 1
    assert embedder.loaded


# --- P9: the arithmetic moved to numpy, and must not have moved the answers --


def test_numpy_normalisation_agrees_with_the_python_it_replaced() -> None:
    """The old path is the specification; numpy is an implementation of it.

    Written because the first version used float32 and changed two answers -
    unit length came back as 1.0000000015 against a 1e-9 tolerance, and a
    vector the code promises to leave alone came back altered in the eighth
    decimal. A speed-up that changes results is not a speed-up.
    """
    import math

    import numpy as np

    from app.index.embedder import Embedder, l2_normalise

    rng = np.random.default_rng(11)
    raw = [rng.random(384, dtype=np.float32) for _ in range(64)]

    def the_old_way() -> list[list[float]]:
        out = []
        for vector in raw:
            widened = list(map(float, vector))
            magnitude = math.sqrt(sum(v * v for v in widened))
            out.append(widened if abs(magnitude - 1.0) <= 1e-3
                       else l2_normalise(widened))
        return out

    embedder = Embedder(encoder=lambda _texts: raw)
    got = embedder.embed(["x"] * len(raw))

    assert np.allclose(np.asarray(got), np.asarray(the_old_way()), atol=1e-12)


def test_an_already_unit_vector_is_returned_bit_for_bit() -> None:
    """"Left alone" has to mean untouched, or the guard is a rounding step."""
    import numpy as np

    from app.index.embedder import Embedder

    vector = np.zeros(384, dtype=np.float64)
    vector[0] = 1.0
    vector[1] = 0.0
    embedder = Embedder(encoder=lambda _texts: [vector])

    assert embedder.embed(["x"])[0] == list(vector)


def test_a_zero_vector_is_not_divided_by(tmp_path) -> None:
    """`l2_normalise` promises this and numpy would happily return nan."""
    import numpy as np

    from app.index.embedder import Embedder

    zeros = np.zeros(384, dtype=np.float64)
    embedder = Embedder(encoder=lambda _texts: [zeros])

    got = embedder.embed(["x"])[0]
    assert got == [0.0] * 384
    assert not any(g != g for g in got), "nan reached the index"


# --- §1c: download progress reaches the splash, not just the model cache ---


def test_on_progress_reports_a_simulated_download(tmp_path, monkeypatch) -> None:
    """fastembed itself has no progress hook this application can reach -
    `TextEmbedding.__init__` calls `download_model(...)` with a fixed
    argument list that drops any extra kwargs before they would reach
    huggingface_hub's `tqdm_class` - so progress is measured from the
    outside: how big the cache directory has grown, polled on a background
    thread while the (here, fake) constructor call is in flight.
    """
    import time
    from pathlib import Path

    class FakeTextEmbedding:
        """Simulates a slow download by writing to the cache dir over time."""

        def __init__(self, model_name, cache_dir=None, **_kwargs):
            target = Path(cache_dir)
            target.mkdir(parents=True, exist_ok=True)
            # Three writes with real pauses between them, so the watcher's
            # 0.3s poll has more than one opportunity to see growth.
            for chunk in range(3):
                (target / f"part{chunk}.bin").write_bytes(b"x" * 4_000_000)
                time.sleep(0.35)

        def embed(self, texts):
            return [[0.0] * 384 for _ in texts]

    import app.index.embedder as embedder_module

    monkeypatch.setattr("fastembed.TextEmbedding", FakeTextEmbedding)
    monkeypatch.setitem(embedder_module._APPROX_MODEL_BYTES, "fake/model", 12_000_000)

    progress: list[float] = []
    embedder = Embedder(
        "fake/model", cache_dir=str(tmp_path), on_progress=progress.append)

    embedder.warm_up()

    assert progress, "on_progress must be called at least once during a download"
    assert progress[-1] == 100.0, "the final call must report completion"
    assert progress[0] < 100.0, "progress must not start already at 100%"
    # Monotonic: the directory only grows during this fake download, so
    # reported progress must never go backwards.
    assert progress == sorted(progress)


def test_on_progress_is_not_called_for_an_already_cached_model(tmp_path) -> None:
    """An ordinary warm-up of a cached model is not a download - no spurious
    progress bar should appear for it."""
    from pathlib import Path as _Path

    model_dir = _Path(tmp_path)
    (model_dir / "already-here.bin").write_bytes(b"x" * 200_000_000)

    progress: list[float] = []
    embedder = Embedder(
        "BAAI/bge-small-en-v1.5", cache_dir=str(tmp_path),
        encoder=lambda texts: [[0.0] * 384 for _ in texts],
        on_progress=progress.append,
    )
    embedder.warm_up()

    assert progress == [], \
        "an injected encoder never reaches the watcher, but a real cached " \
        "load must not report progress either"


def test_on_progress_is_called_even_if_the_load_fails(monkeypatch, tmp_path) -> None:
    """A failed download must still report 100% - "downloading" cannot be
    the status a splash screen is stuck on after loading has already given
    up and raised."""
    class FailingTextEmbedding:
        def __init__(self, *_args, **_kwargs):
            raise RuntimeError("simulated ONNX load failure")

    monkeypatch.setattr("fastembed.TextEmbedding", FailingTextEmbedding)

    progress: list[float] = []
    embedder = Embedder(
        "BAAI/bge-small-en-v1.5", cache_dir=str(tmp_path),
        on_progress=progress.append)

    with pytest.raises(AppErrorException):
        embedder.warm_up()

    assert progress and progress[-1] == 100.0
