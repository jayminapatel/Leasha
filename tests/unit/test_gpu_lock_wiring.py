r"""Proof that the shared GPU gate actually reaches the three subsystems.

`test_gpu_serialize.py` proves the primitive works in isolation; this proves
`embedder.py`, `ocr.py` and `rerank.py` actually reach for it when their own
`backends.Choice` resolved to the graphics card, and leave each other alone
when it resolved to the processor. No GPU exists in this sandbox, so every
"gpu" case below is an injected `Choice` (exactly `test_backends.py`'s own
`GPU_READY`/`NO_GPU` pattern) paired with an injected encoder/scorer/engine -
never real onnxruntime, never real hardware.
"""

from __future__ import annotations

import threading
import time

from app.extract import ocr as ocr_module
from app.index import backends
from app.index.embedder import Embedder
from app.search.rerank import Reranker


def _gpu_choice() -> backends.Choice:
    return backends.Choice(
        device=backends.GPU, providers=backends.providers_for(backends.GPU),
        why="test: forced gpu",
    )


def _cpu_choice() -> backends.Choice:
    return backends.Choice(
        device=backends.CPU, providers=backends.providers_for(backends.CPU),
        why="test: forced cpu",
    )


class _Overlap:
    """A shared counter that records the peak number of simultaneous holders."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.concurrent = 0
        self.peak = 0

    def enter(self) -> None:
        with self._lock:
            self.concurrent += 1
            self.peak = max(self.peak, self.concurrent)

    def leave(self) -> None:
        with self._lock:
            self.concurrent -= 1


# --- the embedder and the reranker never overlap on the graphics card -------


def test_embedder_and_reranker_gpu_sections_never_overlap() -> None:
    overlap = _Overlap()

    def slow_encode(texts):
        overlap.enter()
        time.sleep(0.05)
        overlap.leave()
        return [[0.0] * 384 for _ in texts]

    def slow_score(_query, passages):
        overlap.enter()
        time.sleep(0.05)
        overlap.leave()
        return [0.0 for _ in passages]

    embedder = Embedder(encoder=slow_encode)
    embedder.choice = _gpu_choice()          # what _ensure_encoder would have set

    reranker = Reranker(scorer=slow_score)
    reranker.choice = _gpu_choice()          # what _ensure_scorer would have set

    barrier = threading.Barrier(2, timeout=2.0)

    def run_embed() -> None:
        barrier.wait()
        embedder.embed(["one", "two"])

    def run_rerank() -> None:
        barrier.wait()
        reranker.rerank("query", [{"text": "one"}, {"text": "two"}])

    t1 = threading.Thread(target=run_embed)
    t2 = threading.Thread(target=run_rerank)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert overlap.peak == 1, (
        "the embedder's and the reranker's GPU-gated inference calls ran "
        "concurrently - the exact shape of the 2026-09-07 crash"
    )


def test_embedder_and_reranker_cpu_sections_do_overlap() -> None:
    """The other half: on the processor, nothing should serialise them at all
    - a regression that locked the CPU path too would cost real throughput
    for no safety benefit, since independent CPU sessions are safe."""
    barrier = threading.Barrier(2, timeout=2.0)
    reached: list[str] = []

    def slow_encode(texts):
        barrier.wait()
        reached.append("embed")
        return [[0.0] * 384 for _ in texts]

    def slow_score(_query, passages):
        barrier.wait()
        reached.append("rerank")
        return [0.0 for _ in passages]

    embedder = Embedder(encoder=slow_encode)
    embedder.choice = _cpu_choice()

    reranker = Reranker(scorer=slow_score)
    reranker.choice = _cpu_choice()

    t1 = threading.Thread(target=lambda: embedder.embed(["one"]))
    t2 = threading.Thread(target=lambda: reranker.rerank("q", [{"text": "one"}]))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert sorted(reached) == ["embed", "rerank"], (
        "both CPU-path calls must be able to be inside their section at the "
        "same moment - if this hangs or times out, the CPU path is being "
        "serialised when it must not be"
    )


# --- OCR's engine construction and recognition call are gated the same way --


def test_ocr_engine_construction_is_gated_on_gpu(monkeypatch) -> None:
    """`_load_engine` must hold the gate while building the three RapidOCR
    sessions when `backends.choose` resolves to the graphics card."""
    monkeypatch.setattr(ocr_module, "_engine", None)
    monkeypatch.setattr(ocr_module, "_engine_failed", False)
    monkeypatch.setattr(ocr_module, "_engine_attempts", 0)
    monkeypatch.setattr(ocr_module, "_engine_is_gpu", False)
    monkeypatch.setattr(backends, "choose", lambda *a, **k: _gpu_choice())

    from app.core import gpu_serialize

    held_during_construction = {}

    class FakeRapidOCR:
        def __init__(self, **_kwargs):
            held_during_construction["locked"] = gpu_serialize._GPU_LOCK.locked()

    import sys
    import types

    fake_module = types.ModuleType("rapidocr_onnxruntime")
    fake_module.RapidOCR = FakeRapidOCR
    monkeypatch.setitem(sys.modules, "rapidocr_onnxruntime", fake_module)

    engine = ocr_module._load_engine()

    assert engine is not None
    assert held_during_construction["locked"] is True
    assert ocr_module._engine_is_gpu is True
    # Released once construction returns.
    assert not gpu_serialize._GPU_LOCK.locked()


def test_ocr_recognition_call_is_gated_on_the_loaded_engines_device(monkeypatch) -> None:
    from app.core import gpu_serialize

    monkeypatch.setattr(ocr_module, "_engine_is_gpu", True)

    seen_locked = {}

    def fake_run(_source):
        seen_locked["locked"] = gpu_serialize._GPU_LOCK.locked()
        return []

    result = ocr_module.ocr_image("not-a-path-or-bytes", engine=fake_run)

    assert seen_locked["locked"] is True
    assert result.empty
    assert not gpu_serialize._GPU_LOCK.locked()


def test_ocr_recognition_call_is_not_gated_on_cpu(monkeypatch) -> None:
    from app.core import gpu_serialize

    monkeypatch.setattr(ocr_module, "_engine_is_gpu", False)

    seen_locked = {}

    def fake_run(_source):
        seen_locked["locked"] = gpu_serialize._GPU_LOCK.locked()
        return []

    ocr_module.ocr_image("not-a-path-or-bytes", engine=fake_run)

    assert seen_locked["locked"] is False
