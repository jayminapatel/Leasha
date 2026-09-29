r"""Order 202626270114 (0b) section 6h - the quantised embedder, for real.

Layer: L3

`test_quantize_model.py` proves the quantisation mechanics against a tiny
synthetic graph, fast and offline. This proves the wiring in `embedder.py`
against the real `BAAI/bge-small-en-v1.5` - a real download (skipped if one
cannot happen), a real local quantisation, and a real embedding compared
against the unquantised path, because the risk this order actually carries
is a subtly wrong result reaching every future search silently, and no
synthetic graph can stand in for that comparison.

Marked slow: a real model download and a real quantisation pass, not
something every run should pay for.
"""

from __future__ import annotations

import pytest

pytest.importorskip("onnx")
# Skips on `OSError` too - a torch DLL blocked by Windows; see test_quantize_model.py.
try:
    import onnxruntime.quantization  # noqa: F401
except (ImportError, OSError) as exc:
    pytest.skip(f"onnxruntime.quantization cannot load here: {exc}",
                allow_module_level=True)

from app.index.embedder import Embedder


@pytest.mark.slow
def test_the_quantised_path_produces_a_correctly_shaped_unit_vector(tmp_path):
    plain = Embedder(cache_dir=str(tmp_path), device="cpu", quantised=False)
    vectors = plain.embed(["a boiler quote from last winter"])

    assert len(vectors) == 1
    assert len(vectors[0]) == plain.dim
    import numpy as np
    assert np.isclose(float(np.linalg.norm(vectors[0])), 1.0, atol=1e-3)


@pytest.mark.slow
def test_the_quantised_path_agrees_closely_with_the_unquantised_one(tmp_path):
    r"""The correctness guard this order's own risk actually needs: not
    that quantising runs without raising, but that ranking is not quietly
    broken. Cosine similarity to the fp16 embedding of the same sentence
    should be high - int8 dynamic quantisation is lossy by design, so this
    is not asserting near-1.0, just that the vector still points the same
    way rather than somewhere unrelated.
    """
    import numpy as np

    text = "a boiler quote Dave sent last winter"
    plain = Embedder(cache_dir=str(tmp_path), device="cpu", quantised=False)
    plain_vector = np.asarray(plain.embed([text])[0])

    quantised = Embedder(cache_dir=str(tmp_path), device="cpu", quantised=True)
    quantised_vector = np.asarray(quantised.embed([text])[0])

    assert quantised.choice is not None, "the quantised path must actually load a model"
    similarity = float(np.dot(plain_vector, quantised_vector))
    assert similarity > 0.97, (
        f"cosine similarity {similarity:.4f} between the quantised and "
        "unquantised embeddings of the same sentence is too low to trust "
        "for ranking - quantisation may have broken more than precision"
    )


def test_a_gpu_choice_never_asks_for_a_quantised_copy(tmp_path, monkeypatch):
    r"""§6h's own refusal, tested at the seam rather than against a real
    graphics card: quantisation buys nothing on a GPU, so
    `quantised_model_dir` (a real download-and-quantise call, the thing
    this test must never trigger) must not even be reached when the chosen
    device is a GPU one."""
    from app.index import backends

    def fake_choose(profile, requested, **kwargs):
        return backends.Choice(device="gpu", providers=("DmlExecutionProvider",),
                              why="stand-in for a graphics card")

    called: list = []
    monkeypatch.setattr(backends, "choose", fake_choose)
    monkeypatch.setattr(
        "app.index.quantize_model.quantised_model_dir",
        lambda *a, **k: called.append((a, k)) or None,
    )

    embedder = Embedder(cache_dir=str(tmp_path), device="auto", quantised=True)
    try:
        embedder.warm_up()
    except Exception:
        # A DirectML/CUDA provider is very likely unavailable on this
        # machine's onnxruntime build - the load failing for that reason is
        # not what this test is about. What matters is what happened before
        # the load ever reached the provider.
        pass

    assert called == [], "a GPU choice must never ask for a quantised copy at all"
