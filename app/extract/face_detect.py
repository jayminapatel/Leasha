r"""Face detection and embedding - the automatic half of the Photo Tagger.

Layer: L2

Work order 0j (`202626270512`) section 1a. `insightface` (ONNX, already
installed on this machine per the item's own text) finds every face in a
photo and returns a fixed-length embedding per face - a vector two faces of
the same person land close together in, and two different people land far
apart in. Nothing here decides *who* - `app.index.face_clustering` groups
the embeddings, and only a person naming a pile ever attaches an identity.
That separation is the guardrails' own principled line, enforced by which
module is allowed to do what: this one returns bounding boxes and numbers,
never a name, because it has no way to know one.

**Switch-gated, always** (section 1a: "only when the switch is on") - the
caller (`app.index.pipeline`) is what reads `PEOPLE_RECOGNITION_ENABLED` and
decides whether this module is even imported for a given run; nothing here
enforces the switch itself; a module that ran unconditionally the moment it
was imported would make "off means no face code runs at all" (the order's
own test list) a matter of trusting every call site rather than one place.

**Lazy, behind a lock, CPU by default.** Mirrors `florence_tagger.py`'s own
`_load()` shape exactly: `available()` never imports or loads anything,
`_load()` is retried a small, bounded number of times and then gives up
until the process restarts, and any failure degrades to "no faces found"
rather than raising into a worker thread.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from app.core.gpu_serialize import gpu_exclusive
from app.core.logging import logger

log = logger.bind(component="extract.face_detect")

__all__ = ["FaceDetection", "available", "detect_faces", "MODEL_PACK"]

#: `buffalo_l` - insightface's own general-purpose detection+recognition
#: pack, the one every quick-start example in the project's own docs uses.
#: Downloaded once to `~/.insightface` on first use; no network after that.
MODEL_PACK = "buffalo_l"
ENGINE_LOAD_ATTEMPTS = 3
#: insightface's own default detector input size. Bigger finds smaller faces
#: at a higher cost per image; this is the package's own suggested default,
#: not separately measured this session - see `face_clustering.py`'s
#: threshold comment for the same honest gap.
DET_SIZE = (640, 640)

_engine: Any = None
_engine_lock = threading.Lock()
_engine_failed = False
_engine_attempts = 0
#: Whether the loaded pack runs on the graphics card (2026-10-09): read by
#: `detect_faces` for the process-wide gate, as `ocr._engine_is_gpu` is.
_engine_is_gpu = False


@dataclass(frozen=True, slots=True)
class FaceDetection:
    """One face, in one photo. `bbox` is `(x, y, w, h)` in pixels - the crop
    the Photo Tagger grid (section 2a) draws its thumbnail from. `embedding`
    is `insightface`'s normalised recognition vector, ready for
    `face_clustering.cosine_similarity` with no further processing."""

    bbox: tuple[float, float, float, float]
    embedding: bytes
    confidence: float


def available() -> bool:
    """Is face detection usable here? Never raises, never loads a model -
    the same contract `florence_tagger.available()` and `ocr.available()`
    already give doctor.py and the Settings switch to check cheaply."""
    try:
        import importlib.util

        return importlib.util.find_spec("insightface") is not None
    except Exception:                              # noqa: BLE001
        return False


def _choice() -> Any:
    """Which processor faces run on - `backends.choose` on this machine's choice
    for faces, as OCR asks for its own. The processor when anything is unclear."""
    from app.index import backends

    try:
        from app.core.compute_profile import detect
        from app.core.config import load_settings
        from app.core.model_devices import device_for

        return backends.choose(detect(), device_for(load_settings(), "faces"))
    except Exception as exc:                        # noqa: BLE001 - the processor is always there
        log.debug("faces stay on the processor: {}", exc)
        return backends.choose(None, backends.CPU)


def _load() -> Optional[Any]:
    """The `FaceAnalysis` app, loaded and prepared once. `None` when it
    cannot be - absent package, no model pack downloaded and no network to
    fetch one, or a corrupted cache. Every case degrades the same way."""
    global _engine, _engine_failed, _engine_attempts, _engine_is_gpu

    if _engine is not None or _engine_failed:
        return _engine

    with _engine_lock:
        if _engine is not None or _engine_failed:
            return _engine
        from app.extract.heif import register_heif

        register_heif()                  # 2026-10-04: HEIC was unreadable here
        try:
            from insightface.app import FaceAnalysis

            started = time.monotonic()
            # 2026-10-04: the processor this machine's choice for faces names
            # (`model_devices`, Indexing > Tuning > Devices); it was always the
            # processor (`ctx_id=-1`). A graphics card that will not build it
            # falls back to the processor here, as every other model does.
            choice = _choice()
            try:
                app = FaceAnalysis(name=MODEL_PACK, providers=list(choice.providers))
                app.prepare(ctx_id=0 if choice.is_gpu else -1, det_size=DET_SIZE)
                on_gpu = bool(choice.is_gpu)
            except Exception as exc:                # noqa: BLE001 - the processor, then
                if not choice.is_gpu:
                    raise
                log.warning("faces would not load on the graphics card ({}); "
                            "using the processor", exc)
                app = FaceAnalysis(name=MODEL_PACK, providers=["CPUExecutionProvider"])
                app.prepare(ctx_id=-1, det_size=DET_SIZE)   # -1: CPU
                on_gpu = False
            _engine_is_gpu = on_gpu
            _engine = app
            log.info("face detection model loaded in {:.1f}s ({})",
                     time.monotonic() - started, MODEL_PACK)
        except Exception as exc:                    # noqa: BLE001 - absence is normal
            _engine_attempts += 1
            _engine_failed = _engine_attempts >= ENGINE_LOAD_ATTEMPTS
            level = log.info if _engine_failed else log.warning
            level("face detection model did not load (attempt {} of {}): {}: {}",
                  _engine_attempts, ENGINE_LOAD_ATTEMPTS, type(exc).__name__, exc)
            return None
    return _engine


def _read_bgr(path: Path, cv2: Any, np: Any) -> Optional[Any]:
    """The picture as OpenCV's BGR array, or `None` when nothing can read it.

    **2026-10-04: OpenCV cannot decode HEIC**, whatever Pillow has had
    registered, so every `.heic` photo - 3,741 of the owner's 15,011 - came
    back "no faces" in no time and was marked as scanned, never to be looked
    at again. When `imdecode` gives up, Pillow (with `pillow-heif`) reads it
    and the channels are reversed to BGR, the order the model expects.
    """
    # 2026-10-05: the decode the photo's other models share (`extract.picture`),
    # upright as `IMREAD_COLOR` made it - one decode per photo, not three.
    from app.extract.picture import decoded

    shared = decoded(path)
    if shared is not None:
        return np.ascontiguousarray(np.asarray(shared)[:, :, ::-1])
    image = cv2.imdecode(np.fromfile(str(path), dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is not None:
        return image
    try:
        from PIL import Image

        from app.extract.heif import register_heif

        register_heif()
        with Image.open(path) as opened:
            rgb = np.asarray(opened.convert("RGB"))
    except Exception as exc:                        # noqa: BLE001 - unreadable, as before
        log.debug("no picture to look for faces in {}: {}", Path(path).name, exc)
        return None
    return np.ascontiguousarray(rgb[:, :, ::-1])


def _bytes_bgr(data: bytes, np: Any) -> Optional[Any]:
    """A picture held in memory as OpenCV's BGR array, upright, or `None`.

    2026-10-07: a picture that arrived attached to a message has no file -
    its key is `pst://.../attachments/name.jpg` - so `_read_bgr` was handed a
    place that does not exist and all 1,188 of the owner's came back "no
    faces" and were marked as scanned. Pillow reads the bytes; the channels
    are reversed to BGR, as `_read_bgr` does for the same reason."""
    import io

    from PIL import Image, ImageOps

    from app.extract.heif import register_heif

    register_heif()
    with Image.open(io.BytesIO(data)) as opened:
        rgb = np.asarray(ImageOps.exif_transpose(opened).convert("RGB"))
    return np.ascontiguousarray(rgb[:, :, ::-1])


def detect_faces(path: Path, *, data: Optional[bytes] = None) -> list[FaceDetection]:
    """Every face `insightface` finds in one image. Never raises - one
    unreadable or face-free photo costs an empty list, not a crashed
    worker, the same contract `florence_tagger.tag_image` already gives.

    `data` (2026-10-07) is the picture's own bytes, for one with no file to
    open; `path` is then only its name, for the log."""
    app = _load()
    if app is None:
        return []

    try:
        import cv2
        import numpy as np

        # insightface's own examples read through `cv2.imread` (BGR, which
        # is what its models were trained expecting) rather than PIL -
        # matched here rather than converting, so channel order is never a
        # silent quality bug nothing would notice on a face-shaped image.
        image = _bytes_bgr(data, np) if data is not None else _read_bgr(path, cv2, np)
        if image is None:
            return []
        # 2026-10-09: the pack's sessions run on the graphics card when the
        # device test chose it (`device_test.json`: faces on the card on the
        # owner's laptop), so they take the process-wide gate as OCR does.
        with gpu_exclusive(_engine_is_gpu):
            faces = app.get(image)
    except Exception as exc:                        # noqa: BLE001 - one image, not the run
        log.debug("face detection failed on {}: {}: {}",
                  path.name, type(exc).__name__, exc)
        return []

    out: list[FaceDetection] = []
    for face in faces:
        embedding = getattr(face, "normed_embedding", None)
        if embedding is None:
            continue                                # detected, not embeddable - skip it
        x1, y1, x2, y2 = (float(v) for v in face.bbox)
        out.append(FaceDetection(
            bbox=(x1, y1, max(0.0, x2 - x1), max(0.0, y2 - y1)),
            embedding=embedding.astype("float32").tobytes(),
            confidence=float(getattr(face, "det_score", 0.0)),
        ))
    return out
