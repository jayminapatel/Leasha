r"""The process-wide graphics-card gate reaches the picture and speech models too.

`test_gpu_lock_wiring.py` proves `gpu_serialize.gpu_exclusive` reaches the
embedder, OCR and the reranker. The models added since - Florence-2 (photo
tags), insightface (faces), CLIP (picture search) and Whisper (speech) - each
had a lock of their own, which keeps two callers of *that* model apart and
says nothing about OCR's DirectML session on another reader thread. Found on
2026-10-09, diagnosing the index process's death inside ONNX Runtime: the
device test had put faces, photo tags and OCR on the graphics card at once,
and only OCR took the gate. `ocr.py`'s own note records what an ungated
DirectML call beside a gated one cost on 2026-09-12 (261 native faults, then
an access violation).

No GPU here: every "on the card" case is a flag on a fake session, and the
assertion is whether `gpu_serialize._GPU_LOCK` was held while it ran.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import numpy as np
import pytest

from app.core import gpu_serialize
from app.index import backends


def _held() -> bool:
    return gpu_serialize._GPU_LOCK.locked()


class _Session:
    """Records, on every run, whether the gate was held at that moment."""

    def __init__(self, seen: list) -> None:
        self.seen = seen

    def run(self, _outputs, _feeds):
        self.seen.append(_held())
        return [np.zeros((1, 2, 2), dtype=np.float32)]


class _Loaded:
    def __init__(self, seen: list, on_gpu: bool) -> None:
        self.session = _Session(seen)
        self.on_gpu = on_gpu


@pytest.mark.parametrize("on_gpu", [True, False])
def test_florence_graphs_run_inside_the_gate_when_on_the_card(on_gpu) -> None:
    from app.ort.florence import OnnxFlorence

    seen: list = []
    engine = OnnxFlorence.__new__(OnnxFlorence)
    engine.vision = _Loaded(seen, on_gpu)
    engine.embed = _Loaded(seen, on_gpu)
    engine.encoder = _Loaded(seen, on_gpu)

    engine.encode_image(np.zeros((1, 3, 4, 4), dtype=np.float32))
    engine._embed([1, 2])
    engine._encode(np.zeros((1, 2, 2), dtype=np.float32), np.ones((1, 2), dtype=np.int64))

    assert seen == [on_gpu] * 3
    assert not _held()                                   # released after each call


@pytest.mark.parametrize("on_gpu", [True, False])
def test_face_detection_runs_inside_the_gate_when_on_the_card(monkeypatch, on_gpu) -> None:
    from app.extract import face_detect

    seen: list = []

    class _Pack:
        def get(self, _image):
            seen.append(_held())
            return []

    monkeypatch.setitem(sys.modules, "cv2", sys.modules.get("cv2") or types.ModuleType("cv2"))
    monkeypatch.setattr(face_detect, "_engine", _Pack())
    monkeypatch.setattr(face_detect, "_engine_failed", False)
    monkeypatch.setattr(face_detect, "_engine_is_gpu", on_gpu)
    monkeypatch.setattr(face_detect, "_read_bgr",
                        lambda _path, _cv2, _np: np.zeros((4, 4, 3), dtype=np.uint8))

    assert face_detect.detect_faces(Path("photo.jpg")) == []
    assert seen == [on_gpu]
    assert not _held()


@pytest.mark.parametrize("on_gpu", [True, False])
def test_clip_embedding_runs_inside_the_gate_when_on_the_card(on_gpu) -> None:
    from app.index.clip_embedder import ClipImageEmbedder

    seen: list = []

    def encoder(paths):
        seen.append(_held())
        return [[1.0, 0.0] for _ in paths]

    embedder = ClipImageEmbedder(encoder=encoder, dim=2)
    embedder.choice = backends.Choice(
        device=backends.GPU if on_gpu else backends.CPU,
        providers=backends.providers_for(backends.GPU if on_gpu else backends.CPU),
        why="test: forced")
    assert embedder.embed([Path("a.jpg")]) == [[1.0, 0.0]]
    assert seen == [on_gpu]
    assert not _held()


@pytest.mark.parametrize("on_gpu", [True, False])
def test_whisper_encoder_runs_inside_the_gate_when_on_the_card(on_gpu) -> None:
    from app.ort.whisper import OnnxWhisperEngine

    seen: list = []
    engine = OnnxWhisperEngine.__new__(OnnxWhisperEngine)
    engine._encoder = _Session(seen)
    engine._encoder_input = "input_features"
    engine._encoder_output = "last_hidden_state"
    engine._encoder_dtype = np.float32
    engine._encoder_on_gpu = on_gpu

    engine._encode(np.zeros((80, 3000), dtype=np.float32))
    assert seen == [on_gpu]
    assert not _held()
