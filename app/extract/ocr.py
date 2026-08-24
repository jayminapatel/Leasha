r"""OCR: images, and PDFs that turned out to be pictures of text.

Layer: L2

**On by default, by the project owner's explicit decision**, taken knowing that
on a corpus with many images it can dominate an entire index run. That is a
choice somebody is entitled to make about their own machine - but it is only a
real choice if the cost is visible, so the counters below are not decoration.
`IndexStats` reports images read and seconds spent separately from everything
else, and `app.cli embed-bench` measures throughput, because "indexing got slow"
with no attribution is a complaint nobody can act on.

**RapidOCR rather than Tesseract.** It is a pip install with the models inside
the wheel: no separate binary, no `TESSDATA_PREFIX`, nothing for an installer to
get wrong on a machine nobody can log into. Tesseract remains reachable as a
Tier 2 converter for anybody who prefers it.

**The engine is a seam.** It loads lazily, once, behind a lock, and everything
above it is tested with a fake - so every path here (no package, model failure,
low confidence, a PDF whose pages are half text) is covered without OCR being
installed anywhere.

**A page that already has text is never OCR'd.** Running OCR over a searchable
PDF costs seconds per page to produce a worse copy of text already extracted.
Only pages with no text layer go through it, which on a mixed document is
usually a handful out of dozens.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from app.core.errors import raise_error
from app.core.logging import logger
from app.extract.base import Document, DocumentBuilder, SourceKind, register

__all__ = [
    "OcrExtractor",
    "OcrResult",
    "ocr_image",
    "available",
    "MIN_CONFIDENCE",
    "MAX_PAGES",
]

log = logger.bind(component="extract.ocr")

#: Below this, a line is more likely noise than words. Kept low on purpose:
#: a wrong word costs one bad search result, and a dropped line costs a document
#: that can never be found at all. Recall matters more than precision here.
MIN_CONFIDENCE = 0.5

#: Pages OCR'd from a single PDF. A 400-page scanned manual would otherwise
#: hold up a whole index run on its own; the first pages are also where a title
#: and summary live, so a cap loses less than it appears to.
MAX_PAGES = 50

#: Images smaller than this are icons, bullets, spacers and signature blocks.
#: OCR on them yields nothing and costs a model call each, and a document-heavy
#: corpus is full of them.
MIN_PIXELS = 64 * 64

_engine: Any = None
_engine_lock = threading.Lock()
_engine_failed = False


class OcrResult:
    """Text read from one image, with what it cost and how sure it was."""

    __slots__ = ("text", "lines", "elapsed_s", "mean_confidence")

    def __init__(
        self,
        text: str = "",
        lines: int = 0,
        elapsed_s: float = 0.0,
        mean_confidence: float = 0.0,
    ) -> None:
        self.text = text
        self.lines = lines
        self.elapsed_s = elapsed_s
        self.mean_confidence = mean_confidence

    @property
    def empty(self) -> bool:
        return not self.text.strip()


def available() -> bool:
    """Is OCR usable here? Never raises, never loads the engine.

    Asked by `doctor` and by Settings, both of which want an answer rather than
    an exception on a machine where the package was never installed.
    """
    try:
        import importlib.util

        return importlib.util.find_spec("rapidocr_onnxruntime") is not None
    except Exception:                            # noqa: BLE001
        return False


def _load_engine() -> Any:
    """The OCR engine, loaded once. None when it cannot be.

    Behind a lock because extraction runs on a pool of worker threads and two
    of them loading an ONNX model at once wastes memory and time. `_engine_failed`
    stops a machine without the package paying the import cost on every image in
    the corpus.
    """
    global _engine, _engine_failed

    if _engine is not None or _engine_failed:
        return _engine

    with _engine_lock:
        if _engine is not None or _engine_failed:
            return _engine
        try:
            from rapidocr_onnxruntime import RapidOCR

            started = time.monotonic()
            _engine = RapidOCR()
            log.info("OCR engine loaded in {:.1f}s", time.monotonic() - started)
        except Exception as exc:                 # noqa: BLE001 - absence is normal
            _engine_failed = True
            log.info("OCR unavailable: {}: {}", type(exc).__name__, exc)
    return _engine


def ocr_image(
    source: Any,
    *,
    engine: Optional[Callable[[Any], Any]] = None,
    min_confidence: float = MIN_CONFIDENCE,
) -> OcrResult:
    """Read text from one image. Never raises.

    `source` is a path or raw bytes. `engine` is the seam: a callable returning
    RapidOCR's `(results, elapsed)` shape, so every path here is testable with
    no OCR installed.
    """
    run = engine or _load_engine()
    if run is None:
        return OcrResult()

    started = time.monotonic()
    try:
        raw = run(source if isinstance(source, (str, bytes)) else str(source))
    except Exception as exc:                     # noqa: BLE001 - one image, not the run
        log.debug("OCR failed on an image: {}: {}", type(exc).__name__, exc)
        return OcrResult(elapsed_s=time.monotonic() - started)

    # RapidOCR returns `(results, timings)`; results is a list of
    # `(box, text, confidence)`. A version returning only results is handled by
    # taking the first element only when it looks like a pair.
    results = raw[0] if isinstance(raw, tuple) and len(raw) == 2 else raw
    if not isinstance(results, (list, tuple)) or not results:
        # **Type-checked, not merely truth-checked.** RapidOCR's return shape
        # has changed between versions, and a future one returning a number or
        # an object would sail past `if not results` and then fail on iteration
        # - taking an index worker with it. A test feeds it `42` for this.
        if results:
            log.debug("OCR returned an unexpected shape: {}", type(results).__name__)
        return OcrResult(elapsed_s=time.monotonic() - started)

    kept: list[str] = []
    scores: list[float] = []
    for entry in results:
        try:
            _box, text, confidence = entry[0], entry[1], float(entry[2])
        except (TypeError, IndexError, ValueError):
            continue
        if not str(text).strip():
            continue
        scores.append(confidence)
        if confidence >= min_confidence:
            kept.append(str(text).strip())

    return OcrResult(
        text="\n".join(kept),
        lines=len(kept),
        elapsed_s=time.monotonic() - started,
        mean_confidence=sum(scores) / len(scores) if scores else 0.0,
    )


class OcrExtractor:
    """Images, read as text."""

    name = "ocr"
    extensions = frozenset({
        ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp", ".gif",
    })
    reads_externally = False

    def supports(self, path: Path) -> bool:
        return path.suffix.lower() in self.extensions

    def extract(self, path: Path) -> Iterable[Document]:
        if not available():
            raise_error(
                "ERR_OCR_UNAVAILABLE", "extract.ocr", path=str(path),
                details="rapidocr-onnxruntime is not installed",
            )
            return

        if not self._worth_reading(path):
            return

        result = ocr_image(path)
        if result.empty:
            # Nothing readable. `base.extract` turns an empty yield into
            # ERR_NO_TEXT_LAYER, which is the honest answer for a photograph of
            # a wall - and it lands in the skip ledger where it can be counted.
            log.debug("no text in {} after {:.1f}s", path.name, result.elapsed_s)
            return

        builder = DocumentBuilder(path, source_kind=SourceKind.FILE)
        builder.add(result.text, label="Text read from the image")
        builder.meta.update({
            "format": "ocr",
            "ocr_lines": result.lines,
            # Recorded per document so the cost is attributable. "Indexing got
            # slow" with no attribution is a complaint nobody can act on.
            "ocr_seconds": round(result.elapsed_s, 2),
            "ocr_confidence": round(result.mean_confidence, 3),
        })
        yield builder.build()

    def _worth_reading(self, path: Path) -> bool:
        """Skip icons and spacers without loading the OCR model for them.

        A document-heavy corpus is full of 16x16 bullets and signature images.
        Each costs a model call and yields nothing, and there are thousands.
        """
        try:
            from PIL import Image

            with Image.open(path) as image:
                width, height = image.size
        except Exception:                        # noqa: BLE001 - let OCR try anyway
            return True

        if width * height < MIN_PIXELS:
            log.debug("skipped {}: {}x{} is too small to hold text", path.name, width, height)
            return False
        return True


register(OcrExtractor())
