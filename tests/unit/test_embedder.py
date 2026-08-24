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
    assert EMBED_BATCH == 64
    assert Embedder(encoder=encoder_for()).batch_size == 64


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
