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
from app.core.format_health import Requirement
from app.core.gpu_serialize import (
    gpu_exclusive,
    is_transient_gpu_error,
    mark_gpu_unreliable,
)
from app.core.logging import logger
from app.extract import ocr_ladder
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

_SETTINGS_WHITE_FRACTION: object = None


def _white_fraction_threshold() -> float:
    """Rung 1's confidence threshold (`OCR_WHITE_PAGE_PERCENT`), as a fraction.

    Cached, and never raises - a settings failure must not take OCR down.
    Same shape as `app/extract/pdf.py::_pages_from_settings`: read once, kept
    for the life of the process, so a value asked for on every routed image
    does not re-read `.env` per file.

    Stored as a whole-number percentage because `settings_registry.Setting`
    has no float kind; divided by 100 here, once, so `ocr_ladder.route` keeps
    working in fractions exactly as it always has.
    """
    global _SETTINGS_WHITE_FRACTION
    if _SETTINGS_WHITE_FRACTION is None:
        try:
            from app.core.config import load_settings

            settings = load_settings(create_dirs=False, check_writable=False)
            percent = int(getattr(settings, "ocr_white_page_percent", 70) or 70)
            _SETTINGS_WHITE_FRACTION = percent / 100.0
        except Exception:                        # noqa: BLE001 - never blocks OCR
            _SETTINGS_WHITE_FRACTION = ocr_ladder.WHITE_FRACTION_THRESHOLD_DEFAULT
    return float(_SETTINGS_WHITE_FRACTION)


_engine: Any = None
_engine_lock = threading.Lock()
_engine_failed = False

#: Whether the loaded `_engine` is running on the graphics card. Recorded
#: alongside `_engine`, not recomputed per image: `ocr_image()`'s recognition
#: call needs this to gate `gpu_exclusive` and has no `Choice` of its own to
#: ask - it is a bare `path -> OcrResult` seam called from extraction workers.
#: `False` until an engine has actually loaded on the graphics card, which is
#: also the safe default: no engine yet means no GPU session exists to guard.
_engine_is_gpu = False

#: Whether a transient-GPU inference failure has already been warned about
#: for the *current* engine. Reset to `False` every time `_load_engine()`
#: successfully builds an engine (see there), so a fresh session that later
#: hits its own hardware trouble is still reported once - this is "once per
#: loaded engine", not "once ever for the life of the process". Follows the
#: same warn-once shape as `rerank.py`'s `_warn_once`, the established
#: convention in this codebase for "say it once, not on every item".
_warned_transient_gpu = False


class OcrResult:
    """Text read from one image, with what it cost and how sure it was."""

    __slots__ = (
        "text", "lines", "elapsed_s", "mean_confidence", "engine_missing",
        "checked_no_text",
    )

    def __init__(
        self,
        text: str = "",
        lines: int = 0,
        elapsed_s: float = 0.0,
        mean_confidence: float = 0.0,
        engine_missing: bool = False,
        checked_no_text: bool = False,
    ) -> None:
        #: **The difference between "nothing to read" and "nothing read it".**
        #: An empty result means a blank image; this means the engine never
        #: ran, and the two must not share a skip code - one is a fact about
        #: the file and the other is a fact about this machine.
        self.engine_missing = engine_missing
        #: **Settled by the ladder, not merely empty.** True when the OCR
        #: ladder (`app.extract.ocr_ladder`) found no text boxes at its
        #: detection rung and the recognition pass never ran at all - a
        #: truthful "checked, nothing there" rather than a guess. Distinct
        #: from an ordinary empty result, where recognition ran and simply
        #: found nothing to keep.
        self.checked_no_text = checked_no_text
        self.text = text
        self.lines = lines
        self.elapsed_s = elapsed_s
        self.mean_confidence = mean_confidence

    @property
    def empty(self) -> bool:
        return not self.text.strip()


#: How many times a failing engine load is retried before OCR is treated as
#: absent for this run. Small: the common cause is a package that is not
#: installed, and paying an import per image for that would be absurd. Larger
#: than one, because the *other* cause is transient and used to cost the whole
#: corpus.
ENGINE_LOAD_ATTEMPTS = 3

#: Failed load attempts so far. See `_load_engine`.
_engine_attempts = 0

#: `EMBED_DEVICE`, as this module sees it.
#:
#: **Set rather than read**, because `ocr.py` is a registered extractor with no
#: settings object anywhere near it - it is called from extraction workers with
#: a path and nothing else. `configure_device` is called once at start-up by
#: whichever entry point loaded the settings, and the default is `auto`, which
#: is also the setting's default: an entry point that forgot to call it gets
#: the behaviour somebody who never touched the control would get, rather than
#: a third one nobody chose.
_device = "auto"


def configure_device(device: str) -> None:
    """Tell OCR which processor `EMBED_DEVICE` asked for.

    Idempotent, and takes effect on the next engine load. Changing it after the
    engine exists does nothing on purpose: the setting is marked `restart`, and
    silently reloading three ONNX sessions mid-run to honour it would be a
    worse surprise than not honouring it until asked to.
    """
    global _device
    _device = str(device or "auto").strip().lower() or "auto"


def _profile() -> Any:
    """This machine, for the backend decision. Never raises."""
    try:
        from app.core.compute_profile import detect

        return detect()
    except Exception:                            # noqa: BLE001
        return object()


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
    global _engine, _engine_failed, _engine_is_gpu, _warned_transient_gpu

    if _engine is not None or _engine_failed:
        return _engine

    with _engine_lock:
        if _engine is not None or _engine_failed:
            return _engine
        try:
            from rapidocr_onnxruntime import RapidOCR

            from app.index import backends

            started = time.monotonic()
            # **The third consumer of the one seam.** RapidOCR runs three ONNX
            # sessions - detect, classify, recognise - and takes a `use_dml`
            # flag per session rather than a provider list, so the choice is
            # translated here rather than the seam being bent to fit it.
            #
            # Its own `_check_dml` re-checks Windows version and the available
            # providers and logs its reason before falling back, so this asks
            # for the graphics card and lets the library refuse it. That is one
            # more fallback than `with_fallback` would give, not one fewer.
            choice = backends.choose(_profile(), _device)
            # **Only the three sessions' construction, gated on what was
            # asked for.** A second subsystem building its own ONNX/DirectML
            # session at the same moment is the access-violation in
            # `logs/crash/crash.log` (2026-09-07); see `gpu_serialize`.
            with gpu_exclusive(choice.is_gpu):
                _engine = RapidOCR(**({"det_use_dml": True, "cls_use_dml": True,
                                       "rec_use_dml": True} if choice.is_gpu else {}))
            _engine_is_gpu = choice.is_gpu
            # A fresh engine deserves its own first warning if it later hits
            # transient GPU trouble - see `_warned_transient_gpu` above.
            _warned_transient_gpu = False
            log.info("OCR engine loaded in {:.1f}s on the {}",
                     time.monotonic() - started,
                     "graphics card" if choice.is_gpu else "processor")
            backends.record_provider("OCR", choice)
        except Exception as exc:                 # noqa: BLE001 - absence is normal
            # **A retry budget, not a permanent latch.**
            #
            # This set `_engine_failed = True` on the first failure and never
            # cleared it, so one transient load - a locked model file mid-copy,
            # a moment of memory pressure, an antivirus scan holding the ONNX
            # blob - turned OCR off for the whole run. Every image after that
            # was recorded `ERR_NO_TEXT_LAYER`, which is not "the engine is
            # broken", it is *"this is a photograph of a wall"*. A corpus of
            # scanned documents came back as thousands of blank pages, and the
            # skip ledger agreed with itself all the way down.
            #
            # `_engine_attempts` lets the next few images try again, and after
            # the budget the absence is treated as settled - which is the right
            # answer when the package genuinely is not installed, and is what
            # keeps a missing dependency from costing an import per image.
            global _engine_attempts
            _engine_attempts += 1
            _engine_failed = _engine_attempts >= ENGINE_LOAD_ATTEMPTS
            level = log.info if _engine_failed else log.warning
            level("OCR engine did not load (attempt {} of {}): {}: {}",
                  _engine_attempts, ENGINE_LOAD_ATTEMPTS,
                  type(exc).__name__, exc)
    return _engine


def _detect_only(run: Callable[..., Any]) -> Callable[[Any], list]:
    """Rung 2's detection-only probe (2c), closed over the already-loaded engine.

    Calls `run` with `use_det=True, use_cls=False, use_rec=False` - RapidOCR's
    own detection stage alone, no recognition - so a genuinely textless photo
    costs one ~50-150ms detection pass rather than the ~3.6s full recognition
    pass it would otherwise pay to learn the exact same thing. Reuses whatever
    engine `ocr_image` already loaded rather than a second model instance.

    Returns the boxes RapidOCR's detection stage found (possibly empty).
    Raises through to `ocr_ladder.route`'s own try/except on any failure - a
    fake `engine` from the unit tests below that does not accept these kwargs
    included - which treats "the probe could not run" as "not yet decided",
    never as "no text".
    """
    def _detect(source: Any) -> list:
        raw = run(source, use_det=True, use_cls=False, use_rec=False)
        boxes = raw[0] if isinstance(raw, tuple) and len(raw) == 2 else raw
        return list(boxes) if boxes else []
    return _detect


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

    **Every real image pays the ladder once, here** - the one call site both
    `OcrExtractor` and `RawExtractor` (RAW previews) go through, so "one
    implementation in the OCR layer, every caller inherits it" is literally
    true rather than a docstring's aspiration. A `source` that is not a `Path`
    or `bytes` (the fake string sources the unit tests below use to drive the
    fake `engine` seam) skips routing entirely and behaves exactly as before.
    """
    global _engine, _engine_is_gpu, _warned_transient_gpu

    run = engine or _load_engine()
    if run is None:
        return OcrResult(engine_missing=True)

    if isinstance(source, (Path, bytes)):
        try:
            routed = ocr_ladder.route(
                source, detect=_detect_only(run),
                white_fraction_threshold=_white_fraction_threshold(),
            )
        except Exception as exc:                 # noqa: BLE001 - the ladder must never take an image down
            log.debug("ocr ladder routing failed: {}: {}", type(exc).__name__, exc)
            routed = None

        if routed is not None and routed.decision is ocr_ladder.RouteDecision.NO_TEXT:
            # The detection rung already found zero text boxes - recognition
            # would spend the expensive pass to confirm what is already known.
            return OcrResult(
                elapsed_s=routed.elapsed_ms / 1000.0,
                checked_no_text=True,
            )

    started = time.monotonic()
    try:
        # Gated on the engine actually loaded, not on `_device` - a fallen-
        # back-to-processor engine must not keep paying the cross-subsystem
        # lock it no longer needs; see `gpu_serialize`.
        with gpu_exclusive(_engine_is_gpu):
            raw = run(source if isinstance(source, (str, bytes)) else str(source))
    except Exception as exc:                     # noqa: BLE001 - one image, not the run
        if is_transient_gpu_error(exc):
            # **A previously-working engine just had a transient hardware
            # hiccup mid-run - distinct from `_engine_failed` above, which is
            # about construction never having succeeded at all.** Left alone,
            # `_engine`/`_engine_is_gpu` would keep pointing at the same dead
            # session for every remaining image in the corpus, with nothing
            # but a DEBUG line - invisible to anyone - to say so. Warn once
            # (see `_warned_transient_gpu`) and clear the engine so the next
            # image's `_load_engine()` call rebuilds fresh, reusing the
            # construction-retry-budget machinery already there rather than
            # the `_engine_failed` latch, which stays untouched: the package
            # is not missing, the hardware just blinked.
            #
            # 2026-09-08, later: the rebuild now lands on the processor, not
            # back on the same suspect driver - `mark_gpu_unreliable` is the
            # process-wide latch `backends.choose()` reads, so the next
            # `_load_engine()` chooses the CPU with no code here to say so.
            # That reload is a *construction* attempt like any other and
            # counts against `_engine_attempts` only if it fails, exactly
            # as before; a CPU engine that builds costs nothing from the
            # budget.
            mark_gpu_unreliable(f"{type(exc).__name__}: {exc}"[:200])
            with _engine_lock:
                if not _warned_transient_gpu:
                    _warned_transient_gpu = True
                    log.warning(
                        "OCR's graphics-card session was reported unavailable "
                        "mid-run (a driver reset, heavy system load, or the "
                        "machine waking from sleep can cause this) - not a "
                        "problem with the OCR package or your images. "
                        "Leasha will reload it and try again on the next "
                        "image. Cause: {}: {}", type(exc).__name__, exc)
                _engine = None
                _engine_is_gpu = False
        else:
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
        ".heic", ".heif", ".svg",
    })
    reads_externally = False
    #: Declared so Settings and `doctor` can say "images are indexed by name
    #: only because RapidOCR is missing" instead of leaving somebody to work it
    #: out from an empty result set. `hard`: without it there is no text at all.
    requires = (
        Requirement("rapidocr_onnxruntime", "rapidocr-onnxruntime",
                    provides="text inside images", hard=True),
        Requirement("PIL", "pillow",
                    provides="image loading for formats ONNX cannot open",
                    hard=False),
        Requirement("pillow_heif", "pillow-heif",
                    provides="HEIC/HEIF image support (Apple Photos)",
                    hard=False),
        # Work order 0i section 1a. Soft: without torch/transformers, a
        # photo-class image (the ladder found no text) is simply not tagged -
        # exactly today's behaviour - rather than being blocked. available()
        # checks both modules; either one missing reads as "not available".
        Requirement("transformers", "transformers",
                    provides="AI tags and a caption for photos with no text "
                             "(Florence-2)", hard=False),
    )

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
        if result.engine_missing:
            # **`ERR_OCR_UNAVAILABLE`, never `ERR_NO_TEXT_LAYER`.** The engine
            # could not run, so nothing was read and nothing is known about
            # this image. Recording it as "no text" is a claim about the file -
            # and a wrong one that puts it in the same bucket as genuinely
            # blank photographs, where the OCR pass will never look at it
            # again.
            raise_error(
                "ERR_OCR_UNAVAILABLE", "extract.ocr", path=str(path),
                details="the OCR engine could not be loaded on this run",
            )
            return

        if result.empty:
            # Work order 0i section 1a/1b: a photo-class image (nothing OCR
            # could read - the other half of the ladder's document-class/
            # photo-class split) gets one Florence-2 pass instead of being
            # left unsearchable. Soft dependency: absent torch/transformers
            # falls straight through to the unchanged behaviour below
            # (ERR_NO_TEXT_LAYER via the skip ledger).
            from app.extract.florence_tagger import available as florence_available
            from app.extract.florence_tagger import tag_image as florence_tag_image

            if florence_available():
                tagged = florence_tag_image(path)
                if tagged is not None and (tagged.caption or tagged.tags):
                    tag_builder = DocumentBuilder(path, source_kind=SourceKind.FILE)
                    body = tagged.caption
                    if tagged.tags:
                        tag_line = "Tags: " + ", ".join(tagged.tags)
                        body = body + chr(10) + tag_line if body else tag_line
                    tag_builder.add(body, label="AI description")

                    from app.extract.exif import read_datetime
                    exif_date = read_datetime(path)
                    if exif_date is not None:
                        tag_builder.date = exif_date

                    tag_builder.meta.update({
                        "format": "florence_tags",
                        "ai_tags": list(tagged.tags),
                        "ai_caption_seconds": round(tagged.elapsed_s, 2),
                    })
                    yield tag_builder.build()
                    return

            # Nothing readable, and no AI tags either. base.extract turns an
            # empty yield into ERR_NO_TEXT_LAYER, which is the honest answer
            # for a photograph of a wall - and it lands in the skip ledger
            # where it can be counted.
            log.debug("no text in {} after {:.1f}s", path.name, result.elapsed_s)
            return

        builder = DocumentBuilder(path, source_kind=SourceKind.FILE)
        builder.add(result.text, label="Text read from the image")

        # **EXIF date is THE date for photos.** File mtime lies after 20 years of
        # drive-to-drive copies; EXIF DateTimeOriginal survives them. If present,
        # use it; otherwise fall back to file mtime.
        from app.extract.exif import read_datetime
        exif_date = read_datetime(path)
        if exif_date is not None:
            builder.date = exif_date

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
