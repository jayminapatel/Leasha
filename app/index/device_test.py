r"""Test this machine: each model on the processor and on the graphics card.

Layer: L3

2026-10-04, the owner: "a test button so this can be tested on this machine
and a new machine for indexing ... so it can get tested and the setting set".
Measured the same night, the case it exists for: Florence-2 took 8.9 s a photo
on this laptop's processor and 3.9 s on its integrated graphics, with the same
description.

**For each model:** build it on the processor and, where this machine can use
one, on the graphics card; give both the same fixed work, generated here the
same way every time (a page of text, a picture, a few sentences), so two
machines - or one machine before and after a driver update - are compared on
the same thing; time it after one warm-up; and compare the answers. **A faster
answer that differs is a fail**: vectors must agree to a cosine of 0.98, text
and descriptions closely, faces in number.

The winner per model is the graphics card only when it worked, agreed, and was
at least `MIN_GAIN` faster; otherwise the processor. Results go to
`model_devices.save_results`, keyed by this machine's fingerprint, and every
model left on Automatic follows them from its next load.

A model that is not downloaded is reported as such and left alone - no
download starts here. Loading each model holds the window still while it loads
(measured 5.5 s for the chat model), so the person is told before it starts.
"""

from __future__ import annotations

import difflib
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Optional

from app.core.logging import logger

__all__ = ["run_device_test", "MIN_GAIN", "SAMPLE_TEXTS"]

_log = logger.bind(component="index.device_test")

#: The graphics card must be at least this much faster to be chosen - a 5%
#: win is noise, and the processor is the one that never has a driver problem.
MIN_GAIN = 0.85

SAMPLE_TEXTS = (
    "The northern pump station was commissioned in March after the valve inspection.",
    "Invoice 2024-117 for the replacement of two pressure gauges, payable in thirty days.",
    "Minutes of the safety meeting: the forklift route past the loading bay is closed.",
    "Grandma's recipe for lemon cake: butter, sugar, three eggs and the zest of two lemons.",
)
QUERY = "pump station valve inspection"


def _samples(folder: Path) -> dict[str, Path]:
    """A page of text and a picture, drawn the same way on every machine."""
    from PIL import Image, ImageDraw, ImageFont

    folder.mkdir(parents=True, exist_ok=True)
    page = Image.new("RGB", (1240, 1754), "white")
    draw = ImageDraw.Draw(page)
    try:
        font = ImageFont.load_default(size=36)
    except TypeError:                               # an older Pillow
        font = ImageFont.load_default()
    for row, line in enumerate(("Leasha device test", *SAMPLE_TEXTS)):
        draw.text((90, 120 + row * 90), line[:60], fill="black", font=font)
    photo = Image.new("RGB", (1024, 768))
    pixels = photo.load()
    for x in range(1024):
        for y in range(768):
            pixels[x, y] = (x * 255 // 1024, y * 255 // 768, 140)
    ImageDraw.Draw(photo).ellipse((300, 200, 700, 600), fill=(230, 190, 60))
    paths = {"page": folder / "page.png", "photo": folder / "photo.png"}
    page.save(paths["page"])
    photo.save(paths["photo"])
    return paths


def _timed(work: Callable[[], Any], repeat: int = 3) -> tuple[float, Any]:
    """Seconds per call after one warm-up, and the last answer."""
    answer = work()
    started = time.perf_counter()
    for _ in range(repeat):
        answer = work()
    return (time.perf_counter() - started) / repeat, answer


# --- one runner per model: build on `device`, return (seconds, answer) ------------
#
# Each runner builds its model afresh for the device asked, so the processor
# and the graphics card are never the same session with a different label.
# They raise freely: `run_device_test` turns a failure into that model's note.

def _meaning(settings: Any, device: str, samples: dict) -> tuple[float, Any]:
    from app.index.embedder import Embedder

    model = Embedder.from_settings(settings, device=device)
    return _timed(lambda: model.embed(list(SAMPLE_TEXTS)))


def _rerank(settings: Any, device: str, samples: dict) -> tuple[float, Any]:
    from app.search.rerank import Reranker

    model = Reranker.from_settings(settings, device=device, enabled=True)
    hits = [{"text": text, "chunk_id": n} for n, text in enumerate(SAMPLE_TEXTS)]
    return _timed(lambda: [h["chunk_id"] for h in model.rerank(QUERY, list(hits))])


def _ocr(settings: Any, device: str, samples: dict) -> tuple[float, Any]:
    from app.extract import ocr

    # The OCR engine is a module-level singleton with no per-device constructor,
    # so the test swaps its state for the device under test and puts it back in
    # `finally`, whatever happened - the run that follows must find the engine
    # exactly as the settings left it.
    saved = (ocr._engine, ocr._engine_failed, ocr._device)
    try:
        ocr._engine, ocr._engine_failed = None, False
        ocr.configure_device(device)
        return _timed(lambda: ocr.ocr_image(samples["page"]).text, repeat=2)
    finally:
        ocr._engine, ocr._engine_failed, ocr._device = saved


def _faces(settings: Any, device: str, samples: dict) -> tuple[float, Any]:
    import cv2
    import numpy as np
    from insightface.app import FaceAnalysis

    from app.extract.face_detect import DET_SIZE, MODEL_PACK
    from app.index import backends

    gpu = device == backends.GPU
    model = FaceAnalysis(name=MODEL_PACK, providers=list(backends.providers_for(device)))
    model.prepare(ctx_id=0 if gpu else -1, det_size=DET_SIZE)
    image = cv2.imdecode(np.fromfile(str(samples["photo"]), dtype=np.uint8),
                         cv2.IMREAD_COLOR)
    return _timed(lambda: len(model.get(image)))


def _pictures(settings: Any, device: str, samples: dict) -> tuple[float, Any]:
    from app.index.clip_embedder import ClipImageEmbedder

    model = ClipImageEmbedder.from_settings(settings, device=device)
    paths = [str(samples["photo"]), str(samples["page"])]
    return _timed(lambda: model.embed(paths))


def _describe(settings: Any, device: str, samples: dict) -> tuple[float, Any]:
    from PIL import Image

    from app.ort.florence import OnnxFlorence

    model = OnnxFlorence.from_cache(Path(settings.model_cache), device=device)
    with Image.open(samples["photo"]) as opened:
        picture = opened.convert("RGB")
    return _timed(lambda: model.caption_and_tags(picture)[0], repeat=1)


RUNNERS: dict[str, Callable[[Any, str, dict], tuple[float, Any]]] = {
    "meaning": _meaning, "rerank": _rerank, "ocr": _ocr,
    "faces": _faces, "pictures": _pictures, "describe": _describe,
}


# --- comparing answers ----------------------------------------------------------------

def agree(model: str, first: Any, second: Any) -> bool:
    """Whether the graphics card's answer is the processor's answer."""
    if model in ("meaning", "pictures"):
        import numpy as np

        a, b = np.asarray(first, dtype="float32"), np.asarray(second, dtype="float32")
        if a.shape != b.shape:
            return False
        a = a / (np.linalg.norm(a, axis=-1, keepdims=True) + 1e-9)
        b = b / (np.linalg.norm(b, axis=-1, keepdims=True) + 1e-9)
        return float(np.min(np.sum(a * b, axis=-1))) >= 0.98
    if model in ("ocr", "describe"):
        return difflib.SequenceMatcher(None, str(first), str(second)).ratio() >= 0.85
    return first == second                          # reranker order, face count


def winner(entry: dict) -> str:
    """`"gpu"` only when it ran, agreed, and was `MIN_GAIN` faster; else `"cpu"`."""
    gpu_s, cpu_s = entry.get("gpu_s"), entry.get("cpu_s")
    if entry.get("gpu_ok") and entry.get("agree") and gpu_s and cpu_s \
            and gpu_s <= cpu_s * MIN_GAIN:
        return "gpu"
    return "cpu"


def gpu_usable() -> tuple[bool, str]:
    """Whether this machine can try the graphics card, and the sentence why not."""
    try:
        from app.core.compute_profile import detect
        from app.index import backends

        choice = backends.choose(detect(), backends.GPU)
        return choice.is_gpu, choice.why
    except Exception as exc:                        # noqa: BLE001
        return False, f"the graphics card could not be checked ({type(exc).__name__})"


def run_device_test(settings: Any, models: Optional[list[str]] = None,
                    *, progress: Optional[Callable[[str], None]] = None,
                    runners: Optional[dict] = None, save: bool = True) -> dict:
    """Test the models named (all by default), save, and return the results.

    `runners` and `save` are for the tests. Never raises: a model that cannot
    be built on a processor says so in its entry and keeps the processor.
    """
    from app.core.model_devices import (MODELS, load_results, machine_fingerprint,
                                        save_results)

    runners = runners or RUNNERS
    wanted = [m for m, _key, _label in MODELS if models is None or m in models]
    labels = {m: label for m, _key, label in MODELS}
    can_gpu, why = gpu_usable()
    previous = load_results(settings)
    same_machine = previous.get("fingerprint") == machine_fingerprint()
    results = {
        "fingerprint": machine_fingerprint(),
        "tested_at": int(time.time()),
        "gpu": "usable" if can_gpu else why,
        "models": dict(previous.get("models") or {}) if same_machine else {},
    }
    with tempfile.TemporaryDirectory(prefix="leasha-device-test-") as scratch:
        samples = _samples(Path(scratch))
        for model in wanted:
            if progress is not None:
                progress(f"Testing {labels[model]}…")
            entry: dict[str, Any] = {"cpu_s": None, "gpu_s": None, "gpu_ok": False,
                                     "agree": False, "note": ""}
            try:
                entry["cpu_s"], cpu_answer = runners[model](settings, "cpu", samples)
            except Exception as exc:                # noqa: BLE001 - one model, not the test
                entry["note"] = f"not available here ({type(exc).__name__}: {exc})"[:200]
                entry["winner"] = "cpu"
                results["models"][model] = entry
                _log.info("device test: {} - {}", model, entry["note"])
                continue
            if can_gpu:
                try:
                    entry["gpu_s"], gpu_answer = runners[model](settings, "gpu", samples)
                    entry["gpu_ok"] = True
                    entry["agree"] = agree(model, cpu_answer, gpu_answer)
                    if not entry["agree"]:
                        entry["note"] = "the graphics card gave a different answer"
                except Exception as exc:            # noqa: BLE001
                    entry["note"] = f"the graphics card failed ({type(exc).__name__})"
            else:
                entry["note"] = why
            entry["winner"] = winner(entry)
            results["models"][model] = entry
            _log.info("device test: {} processor {} s, graphics card {} s, uses {}",
                      model, entry["cpu_s"], entry["gpu_s"], entry["winner"])
    if save:
        save_results(settings, results)
    return results
