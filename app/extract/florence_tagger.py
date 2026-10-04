r"""Florence-2 tagging: tags and a brief caption for photo-class images.

Layer: L2

Work order 0i (`202626270511`) section 1. The OCR ladder
(`app.extract.ocr_ladder`) already separates document-class images (routed to
full OCR) from photo-class ones (no text boxes found at all). A photo-class
image has never had anything written about it - a wall photo, a birthday, a
dog in a garden - and a search for "dog and man" finds nothing because there
is nothing to find. This is the fast pass that gives it words.

**One model, one pass per photo-class image.** Microsoft's Florence-2 is MIT
licensed - checked directly against the model card
(https://huggingface.co/microsoft/Florence-2-base, "License: mit"), not
assumed - so there is no conflict with Leasha being a sold product. Item 1a's
"tags + brief caption in a single call" is read here as *one model, loaded
once, run against one image* - `<DETAILED_CAPTION>` and `<OD>` are two
`generate()` calls against that one already-loaded model and processor, not
two separate models. Florence-2 has no task token that returns a caption and
a discrete label list from one `generate()` call, so a literal single
`generate()` is not the model's own grammar; the spec's actual concern - no
second model, one pass over each image - is what this satisfies.

**AI-written text is always labelled.** The result becomes a segment tagged
"AI description", the parallel of `ocr.py`'s "Text read from the image" - so
a preview can say, plainly, which words are the camera's subject and which
are a model's guess.

**Lazy, behind a lock, CPU only.** Mirrors `app/extract/ocr.py::_load_engine`:
loaded once, a small retry budget, and absence (`torch`/`transformers` not
installed) is normal and never fatal - `available()` answers without loading
anything, exactly like `ocr.available()`. No DirectML path yet: the work
order allows an ONNX port later "for speed"; this is the CPU baseline it
asks to be measured against first.

**2026-09-29 - on ONNX Runtime, not torch** (owner: "all should be onnx by
default"). Windows' Smart App Control blocked torch's unsigned DLL on the
owner's laptop, and would on any machine where it is on. The same model now
runs from the `onnx-community/Florence-2-base` export through `app/ort/
florence.py`, int8, on the processor. Measured on the owner's laptop against
the torch path on four photos: identical tags, captions of the same quality
in slightly different words, 11-14 s a photo either way, and 4.4 s to load
against 22.4 s. The "CPU only" and "No DirectML path yet" paragraphs above
describe the torch path; see `app/ort/session.py` for why the int8 graphs
still run on the processor. `available()` now means "ONNX Runtime is here and
the model is downloaded"; nothing is fetched while indexing.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from app.core.logging import logger

log = logger.bind(component="extract.florence_tagger")

__all__ = ["FlorenceResult", "tag_image", "describe", "available", "MODEL_ID"]

MODEL_ID = "microsoft/Florence-2-base"
ENGINE_LOAD_ATTEMPTS = 3

_engine: Any = None
_engine_lock = threading.Lock()
_engine_failed = False
_engine_attempts = 0


@dataclass(slots=True)
class FlorenceResult:
    """A caption and tags for one image, and what they cost."""

    caption: str
    tags: tuple[str, ...]
    elapsed_s: float


def _settings() -> tuple[Optional[Path], str]:
    """The model cache and `EMBED_DEVICE`. An extractor is handed a path and
    nothing else, so it reads them itself - the way `ocr.py` does."""
    try:
        from app.core.config import load_settings

        settings = load_settings(create_dirs=False, check_writable=False)
        cache = getattr(settings, "model_cache", None)
        return (Path(cache) if cache else None), str(getattr(settings, "embed_device", "auto"))
    except Exception:                              # noqa: BLE001 - never blocks indexing
        return None, "auto"


def available() -> bool:
    """Is Florence-2 tagging usable here? Never raises, never loads the model.

    2026-09-29: ONNX Runtime and the downloaded `onnx-community/Florence-2-base`
    graphs, not torch and transformers - see the module note.
    """
    try:
        import importlib.util

        from app.ort import hub

        if importlib.util.find_spec("onnxruntime") is None:
            return False
        cache, _device = _settings()
        from app.ort import catalogue

        return catalogue.best("photo", cache) is not None
    except Exception:                              # noqa: BLE001
        return False


def _load() -> Any:
    """The ONNX Florence-2, loaded once. `None` when it cannot be."""
    global _engine, _engine_failed, _engine_attempts

    if _engine is not None or _engine_failed:
        return _engine
    with _engine_lock:
        if _engine is not None or _engine_failed:
            return _engine
        from app.extract.heif import register_heif

        register_heif()                  # 2026-10-04: HEIC was unreadable here
        try:
            from app.ort.florence import OnnxFlorence

            started = time.monotonic()
            cache, device = _settings()
            engine = OnnxFlorence.from_cache(cache, device=device)
            if engine is None:
                raise FileNotFoundError(
                    "the Florence-2 ONNX model is not downloaded - Settings, Models, "
                    "photo tags, Download")
            _engine = engine
            log.info("Florence-2 tagging model loaded in {:.1f}s (ONNX, {})",
                     time.monotonic() - started, "graphics card" if engine.on_gpu else "processor")
        except Exception as exc:                    # noqa: BLE001 - absence is normal
            _engine_attempts += 1
            _engine_failed = _engine_attempts >= ENGINE_LOAD_ATTEMPTS
            level = log.info if _engine_failed else log.warning
            level("Florence-2 tagging model did not load (attempt {} of {}): {}: {}",
                  _engine_attempts, ENGINE_LOAD_ATTEMPTS, type(exc).__name__, exc)
            return None
    return _engine


def reset() -> None:
    """Forget the loaded model and any remembered failure (after a download)."""
    global _engine, _engine_failed, _engine_attempts
    with _engine_lock:
        _engine, _engine_failed, _engine_attempts = None, False, 0


def tag_image(path: Path) -> Optional[FlorenceResult]:
    """Caption + tags for one photo-class image. Never raises."""
    engine = _load()
    if engine is None:
        return None

    started = time.monotonic()
    try:
        from PIL import Image

        with Image.open(path) as im:
            caption, tags = engine.caption_and_tags(im.convert("RGB"))
    except Exception as exc:                        # noqa: BLE001 - one image, not the run
        log.debug("Florence-2 tagging failed on {}: {}: {}",
                  Path(path).name, type(exc).__name__, exc)
        return None

    return FlorenceResult(caption=caption, tags=tags, elapsed_s=time.monotonic() - started)


def describe(path: Path) -> Optional[str]:
    """A paragraph about one picture - Describe, when the chat engine is ONNX.
    Never raises; `None` when the model is absent or the image unreadable."""
    engine = _load()
    if engine is None:
        return None
    try:
        from PIL import Image

        with Image.open(path) as im:
            return engine.describe(im.convert("RGB")) or None
    except Exception as exc:                        # noqa: BLE001
        log.debug("Florence-2 describe failed on {}: {}: {}",
                  Path(path).name, type(exc).__name__, exc)
        return None