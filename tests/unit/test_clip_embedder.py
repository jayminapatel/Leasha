"""Work order 0h §1a: the CLIP image embedder.

Same three silent failure modes as `test_embedder.py`, mirrored for the image
tower rather than reasserted from scratch: a dimension mismatch (a vision
model swap), an un-normalised vector (ranks wrong forever in cosine space),
and a batch that comes back the wrong length (pairs an image with the wrong
vector). Every test injects an `encoder` and touches no real model or network.
"""

from __future__ import annotations

import math
import threading

import pytest

from app.core.errors import AppErrorException
from app.index.clip_embedder import CLIP_IMAGE_BATCH, CLIP_IMAGE_DIM, ClipImageEmbedder


def unit(seed: float, dim: int = 8) -> list[float]:
    raw = [math.sin(seed + i) for i in range(dim)]
    magnitude = math.sqrt(sum(v * v for v in raw))
    return [v / magnitude for v in raw]


def encoder_for(dim: int = 8, *, normalised: bool = True):
    calls: list[list[str]] = []

    def encode(paths):
        calls.append(list(paths))
        out = []
        for index, _path in enumerate(paths):
            vector = unit(float(index), dim)
            out.append(vector if normalised else [v * 6.0 for v in vector])
        return out

    encode.calls = calls  # type: ignore[attr-defined]
    return encode


# --- basics -------------------------------------------------------------


def test_returns_one_vector_per_image() -> None:
    embedder = ClipImageEmbedder(dim=8, encoder=encoder_for(8))
    vectors = embedder.embed(["a.jpg", "b.jpg", "c.jpg"])
    assert len(vectors) == 3
    assert all(len(v) == 8 for v in vectors)


def test_empty_input_does_not_touch_the_model() -> None:
    embedder = ClipImageEmbedder()          # no encoder: loading would fail here
    assert embedder.embed([]) == []
    assert not embedder.loaded


def test_default_model_and_dimension_are_the_verified_pair() -> None:
    """`Qdrant/clip-ViT-B-32-vision`, 512-dim, MIT - already confirmed available
    via `ImageEmbedding.list_supported_models()`, paired with the text tower
    `app/search/vector.py` uses. Not re-derived here; pinned so a change to
    either constant is a deliberate edit, not an accident."""
    embedder = ClipImageEmbedder()
    assert embedder.model_name == "Qdrant/clip-ViT-B-32-vision"
    assert embedder.dim == CLIP_IMAGE_DIM == 512


def test_default_batch_is_fastembeds_own_default() -> None:
    assert CLIP_IMAGE_BATCH == 16
    assert ClipImageEmbedder(encoder=encoder_for()).batch_size == 16


def test_invalid_batch_size_is_rejected() -> None:
    with pytest.raises(ValueError):
        ClipImageEmbedder(encoder=encoder_for(), batch_size=0)


# --- batching -------------------------------------------------------------


def test_embed_all_batches_at_the_configured_size() -> None:
    encoder = encoder_for()
    embedder = ClipImageEmbedder(dim=8, encoder=encoder, batch_size=4)

    vectors = list(embedder.embed_all([f"img{i}.jpg" for i in range(10)]))
    assert len(vectors) == 10
    assert [len(call) for call in encoder.calls] == [4, 4, 2]


def test_embed_all_is_lazy() -> None:
    encoder = encoder_for()
    embedder = ClipImageEmbedder(dim=8, encoder=encoder, batch_size=4)

    stream = embedder.embed_all([f"i{i}.jpg" for i in range(40)])
    next(stream)
    assert len(encoder.calls) == 1


def test_order_is_preserved_across_batches() -> None:
    seen: list[str] = []

    def encode(paths):
        seen.extend(paths)
        return [unit(float(hash(p) % 100), 8) for p in paths]

    embedder = ClipImageEmbedder(dim=8, encoder=encode, batch_size=3)
    paths = [f"img-{i}.jpg" for i in range(10)]
    list(embedder.embed_all(paths))
    assert seen == paths


# --- the dimension guard ----------------------------------------------------


def test_wrong_dimension_is_caught_with_both_numbers() -> None:
    """What pointing CLIP_IMAGE_MODEL at a different vision tower looks like."""
    embedder = ClipImageEmbedder(
        model_name="some/other-vision-model", dim=512, encoder=encoder_for(768))

    with pytest.raises(AppErrorException) as caught:
        embedder.embed(["a.jpg"])

    error = caught.value.error
    assert error.code == "ERR_MODEL_LOAD"
    assert "768" in error.details and "512" in error.details


def test_dimension_is_only_checked_once() -> None:
    embedder = ClipImageEmbedder(dim=8, encoder=encoder_for(8))
    embedder.embed(["a.jpg"])
    embedder.embed(["b.jpg"])
    assert embedder._checked_dim


# --- normalisation ----------------------------------------------------------


def test_vectors_come_back_unit_length() -> None:
    embedder = ClipImageEmbedder(dim=8, encoder=encoder_for(8, normalised=False))
    for vector in embedder.embed(["a.jpg", "b.jpg"]):
        assert math.isclose(math.sqrt(sum(v * v for v in vector)), 1.0, abs_tol=1e-9)


def test_already_normalised_vectors_are_left_alone() -> None:
    embedder = ClipImageEmbedder(dim=8, encoder=encoder_for(8, normalised=True))
    expected = unit(0.0, 8)
    assert embedder.embed(["a.jpg"])[0] == expected


# --- failure modes -----------------------------------------------------------


def test_a_short_batch_is_refused_rather_than_misaligned() -> None:
    embedder = ClipImageEmbedder(dim=8, encoder=lambda _paths: [unit(0.0, 8)])

    with pytest.raises(AppErrorException) as caught:
        embedder.embed(["a.jpg", "b.jpg", "c.jpg"])
    assert "got 1" in caught.value.error.details


def test_an_encoder_that_raises_becomes_an_apperror() -> None:
    def explode(_paths):
        raise RuntimeError("corrupt image / onnxruntime failure")

    with pytest.raises(AppErrorException) as caught:
        ClipImageEmbedder(dim=8, encoder=explode).embed(["a.jpg"])

    error = caught.value.error
    assert error.code == "ERR_MODEL_LOAD"
    assert "corrupt image" in error.details
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
        ClipImageEmbedder().warm_up()

    assert caught.value.error.code == "ERR_MODEL_LOAD"
    assert "pip install fastembed" in caught.value.error.suggestion


# --- lazy loading -------------------------------------------------------------


def test_the_model_is_not_loaded_until_it_is_needed() -> None:
    loads = []

    def encode(paths):
        loads.append(1)
        return [unit(0.0, 8) for _ in paths]

    embedder = ClipImageEmbedder(dim=8, encoder=encode)
    assert not loads
    embedder.embed(["now.jpg"])
    assert len(loads) == 1


def test_warm_up_is_idempotent_and_thread_safe() -> None:
    attempts: list[int] = []
    barrier = threading.Barrier(4)

    class Slow(ClipImageEmbedder):
        def _ensure_encoder(self):
            if self._encoder is None:
                with self._lock:
                    if self._encoder is None:
                        attempts.append(1)
                        self._encoder = lambda paths: [unit(0.0, 8) for _ in paths]
            return self._encoder

    embedder = Slow(dim=8)

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


# --- from_settings ------------------------------------------------------------


def test_from_settings_reads_the_shared_model_cache() -> None:
    class FakeSettings:
        model_cache = r"D:\Leasha\Data\models"

    embedder = ClipImageEmbedder.from_settings(FakeSettings())
    assert embedder.cache_dir == r"D:\Leasha\Data\models"
    assert embedder.model_name == "Qdrant/clip-ViT-B-32-vision"
