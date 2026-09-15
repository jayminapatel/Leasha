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
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from app.core.logging import logger

log = logger.bind(component="extract.florence_tagger")

__all__ = ["FlorenceResult", "tag_image", "available", "MODEL_ID"]

MODEL_ID = "microsoft/Florence-2-base"
ENGINE_LOAD_ATTEMPTS = 3

_model: Any = None
_processor: Any = None
_engine_lock = threading.Lock()
_engine_failed = False
_engine_attempts = 0


@dataclass(slots=True)
class FlorenceResult:
    """A caption and tags for one image, and what they cost."""

    caption: str
    tags: tuple[str, ...]
    elapsed_s: float


def available() -> bool:
    """Is Florence-2 tagging usable here? Never raises, never loads the model."""
    try:
        import importlib.util

        return (
            importlib.util.find_spec("torch") is not None
            and importlib.util.find_spec("transformers") is not None
        )
    except Exception:                              # noqa: BLE001
        return False


def _load() -> Optional[tuple[Any, Any]]:
    """The model and processor, loaded once. `None` when they cannot be."""
    global _model, _processor, _engine_failed, _engine_attempts

    if _model is not None or _engine_failed:
        return (_model, _processor) if _model is not None else None

    with _engine_lock:
        if _model is not None or _engine_failed:
            return (_model, _processor) if _model is not None else None
        try:
            import torch
            from transformers import AutoModelForCausalLM, AutoProcessor

            started = time.monotonic()
            model = AutoModelForCausalLM.from_pretrained(
                MODEL_ID, trust_remote_code=True, torch_dtype=torch.float32,
            )
            processor = AutoProcessor.from_pretrained(MODEL_ID, trust_remote_code=True)
            model.eval()
            _model, _processor = model, processor
            log.info("Florence-2 tagging model loaded in {:.1f}s ({})",
                     time.monotonic() - started, MODEL_ID)
        except Exception as exc:                    # noqa: BLE001 - absence is normal
            _engine_attempts += 1
            _engine_failed = _engine_attempts >= ENGINE_LOAD_ATTEMPTS
            level = log.info if _engine_failed else log.warning
            level("Florence-2 tagging model did not load (attempt {} of {}): {}: {}",
                  _engine_attempts, ENGINE_LOAD_ATTEMPTS, type(exc).__name__, exc)
            return None
    return _model, _processor


def _run_task(model: Any, processor: Any, image: Any, task: str,
              max_new_tokens: int) -> dict:
    """One Florence-2 `generate()` call for one task prompt."""
    import torch

    inputs = processor(text=task, images=image, return_tensors="pt")
    with torch.no_grad():
        generated_ids = model.generate(
            input_ids=inputs["input_ids"],
            pixel_values=inputs["pixel_values"],
            max_new_tokens=max_new_tokens,
            num_beams=1,
            do_sample=False,
        )
    text = processor.batch_decode(generated_ids, skip_special_tokens=False)[0]
    return processor.post_process_generation(
        text, task=task, image_size=(image.width, image.height))


def tag_image(path: Path) -> Optional[FlorenceResult]:
    """Caption + tags for one photo-class image. Never raises."""
    loaded = _load()
    if loaded is None:
        return None
    model, processor = loaded

    started = time.monotonic()
    try:
        from PIL import Image

        with Image.open(path) as im:
            image = im.convert("RGB")
            caption_out = _run_task(model, processor, image, "<DETAILED_CAPTION>", 128)
            caption = str(caption_out.get("<DETAILED_CAPTION>", "")).strip()

            od_out = _run_task(model, processor, image, "<OD>", 128)
            od = od_out.get("<OD>")
            labels = od.get("labels", []) if isinstance(od, dict) else []
            tags = tuple(dict.fromkeys(
                str(label).strip().lower() for label in labels if str(label).strip()))
    except Exception as exc:                        # noqa: BLE001 - one image, not the run
        log.debug("Florence-2 tagging failed on {}: {}: {}",
                  path.name, type(exc).__name__, exc)
        return None

    return FlorenceResult(caption=caption, tags=tags, elapsed_s=time.monotonic() - started)
