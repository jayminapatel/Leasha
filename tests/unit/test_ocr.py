"""OCR: on by default, and therefore obliged to be honest about what it costs.

Layer: L2

The owner overrode "OCR is out of scope for V2" deliberately, knowing it may add
days to a first 100GB run. That is theirs to decide - but it is only a real
decision if the cost is visible, so the counters are tested here alongside the
behaviour.

**Measured on this machine**: about 3.6 seconds for a full page of text, which
is roughly eight times what embedding one passage costs. 10,000 scanned pages is
ten hours. Those numbers belong in a test rather than a conversation, because
they are what somebody will want when deciding whether to leave it on.

Everything below runs with a **fake engine** except where marked, so the whole
module is covered on a machine with no OCR installed.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from app.core.errors import AppErrorException
from app.extract import ocr as module
from app.extract import ocr_ladder
from app.extract.ocr import MIN_CONFIDENCE, OcrExtractor, available, ocr_image

HAS_OCR = importlib.util.find_spec("rapidocr_onnxruntime") is not None
HAS_PIL = importlib.util.find_spec("PIL") is not None


def engine_returning(*rows):
    """A fake in RapidOCR's `(results, timings)` shape."""
    return lambda _source: ([list(row) for row in rows], 0.01)


# ---------------------------------------------------------------------------
# The seam: everything below is tested without OCR installed
# ---------------------------------------------------------------------------

def test_low_confidence_lines_are_dropped_and_the_rest_kept():
    result = ocr_image("x", engine=engine_returning(
        ([[0, 0]], "Leeds safety report", 0.97),
        ([[0, 0]], "|||~~", 0.10),
    ))

    assert result.text == "Leeds safety report"
    assert result.lines == 1


def test_the_confidence_floor_is_low_on_purpose():
    """A wrong word costs one bad search result; a dropped line costs a document
    that can never be found at all. Recall matters more than precision here."""
    assert MIN_CONFIDENCE <= 0.6


def test_an_engine_that_raises_never_takes_the_run_down():
    """One image is one file in a 100GB run, not an emergency."""
    def explode(_source):
        raise RuntimeError("model exploded")

    assert ocr_image("x", engine=explode).empty


@pytest.mark.parametrize("reply", [None, [], (), ([], 0.0), "nonsense", 42])
def test_an_unexpected_reply_shape_is_empty_rather_than_a_crash(reply):
    """RapidOCR's return shape has changed between versions, and an extractor
    that crashes on a new one takes an index worker with it."""
    assert ocr_image("x", engine=lambda _s: reply).empty


def test_malformed_rows_are_skipped_not_fatal():
    result = ocr_image("x", engine=engine_returning(
        ([[0, 0]], "good line", 0.9),
        ("too", "short"),
        ([[0, 0]], "another", "not a number"),
    ))
    assert result.text == "good line"


def test_no_engine_at_all_returns_empty():
    assert ocr_image("x", engine=None if module._load_engine() is None else lambda _s: None).empty


def test_the_cost_is_recorded_so_it_can_be_attributed():
    """"Indexing got slow" with no attribution is a complaint nobody can act
    on. Seconds and confidence ride along on every document."""
    result = ocr_image("x", engine=engine_returning(([[0, 0]], "text", 0.9)))
    assert result.elapsed_s >= 0
    assert result.mean_confidence == pytest.approx(0.9)


# ---------------------------------------------------------------------------
# What it refuses to spend time on
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not HAS_PIL, reason="Pillow is not installed")
def test_an_icon_is_skipped_without_loading_the_model(tmp_path):
    """A document-heavy corpus is full of 16x16 bullets and signature images.
    Each costs a model call and yields nothing, and there are thousands."""
    from PIL import Image

    icon = tmp_path / "bullet.png"
    Image.new("RGB", (24, 24), "white").save(icon)

    assert OcrExtractor()._worth_reading(icon) is False


@pytest.mark.skipif(not HAS_PIL, reason="Pillow is not installed")
def test_a_real_page_is_worth_reading(tmp_path):
    from PIL import Image

    page = tmp_path / "page.png"
    Image.new("RGB", (1240, 1750), "white").save(page)

    assert OcrExtractor()._worth_reading(page) is True


def test_an_unreadable_image_is_attempted_rather_than_assumed_empty(tmp_path):
    """If Pillow cannot open it, OCR may still manage. Guessing costs nothing
    to be wrong about; refusing loses a document."""
    broken = tmp_path / "broken.png"
    broken.write_bytes(b"not a png")

    assert OcrExtractor()._worth_reading(broken) is True


def test_a_page_cap_exists_so_one_manual_cannot_hold_up_a_run():
    """A 400-page scanned manual would otherwise take hours on its own. The
    first pages are where a title and summary live, so a cap loses little."""
    assert 0 < module.MAX_PAGES <= 100


# ---------------------------------------------------------------------------
# Missing package
# ---------------------------------------------------------------------------

def test_a_machine_without_ocr_says_so_precisely(tmp_path, monkeypatch):
    image = tmp_path / "scan.png"
    image.write_bytes(b"x")
    monkeypatch.setattr(module, "available", lambda: False)

    with pytest.raises(AppErrorException) as caught:
        list(OcrExtractor().extract(image))
    assert caught.value.error.code == "ERR_OCR_UNAVAILABLE"


def test_available_never_raises():
    """Asked by doctor and by Settings, both of which want an answer rather
    than an exception."""
    assert available() in (True, False)


# ---------------------------------------------------------------------------
# With OCR actually installed
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not (HAS_OCR and HAS_PIL), reason="OCR is not installed")
def test_real_text_in_a_real_image_comes_back(tmp_path):
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (900, 200), "white")
    ImageDraw.Draw(image).text((20, 20), "LEEDS SAFETY REPORT", fill="black")
    path = tmp_path / "scan.png"
    image.save(path)

    documents = list(OcrExtractor().extract(path))

    assert documents, "nothing was read from a legible image"
    assert "LEEDS" in documents[0].text.upper()
    assert documents[0].meta["format"] == "ocr"
    assert documents[0].meta["ocr_seconds"] > 0


@pytest.mark.skipif(not (HAS_OCR and HAS_PIL), reason="OCR is not installed")
def test_a_blank_page_yields_nothing_when_florence_is_unavailable(tmp_path, monkeypatch):
    """Which becomes ERR_NO_TEXT_LAYER upstream - the honest answer for a
    photograph of a wall, and one that lands in the skip ledger to be counted.

    2026-09-15, work order 0i section 1a/1b: this used to assert `== []` for
    every blank page unconditionally. That is no longer the whole story - a
    "photograph of a wall" is exactly the photo-class case 0i's Florence-2
    pass exists to give words to, and with `torch`/`transformers` installed on
    this machine a truly blank image now *does* get an AI-written caption
    (see `test_a_blank_page_gets_an_ai_description_instead_of_nothing` below,
    the real-model proof of that). This test keeps the original assertion
    alive for the case it actually describes: Florence genuinely unavailable,
    where the pre-0i behaviour must still hold exactly as before.
    """
    from PIL import Image
    from app.extract import florence_tagger

    monkeypatch.setattr(florence_tagger, "available", lambda: False)

    path = tmp_path / "blank.png"
    Image.new("RGB", (600, 200), "white").save(path)

    assert list(OcrExtractor().extract(path)) == []


@pytest.mark.slow
@pytest.mark.skipif(
    not (HAS_OCR and HAS_PIL and importlib.util.find_spec("torch")
         and importlib.util.find_spec("transformers")),
    reason="OCR and/or Florence-2 (torch/transformers) are not installed")
def test_a_blank_page_gets_an_ai_description_instead_of_nothing(tmp_path):
    """0i section 1a/1b, proved against the real model, not a stub.

    A page with no OCR text is photo-class - the ladder's other half of
    "document-class images keep the specialist OCR" - and now gets one
    Florence-2 pass instead of being left unsearchable. The caption is
    non-deterministic model output, so this only asserts the *shape* the
    work order specifies: a labelled "AI description" segment, present in
    both the built document's own segments and its rendered text (so a
    preview can find and show the label), through the ordinary
    `DocumentBuilder` path used everywhere else in this module.
    """
    from PIL import Image

    path = tmp_path / "blank.png"
    Image.new("RGB", (600, 200), "white").save(path)

    documents = list(OcrExtractor().extract(path))

    assert documents, "a blank image with Florence available should still yield a document"
    document = documents[0]
    assert document.meta["format"] == "florence_tags"
    assert document.meta["ai_caption_seconds"] > 0
    # Not prefixed into `document.text` itself - same convention as
    # `ocr.py`'s own "Text read from the image" label just above this branch
    # in the source: the label is carried on the segment for a preview to
    # read structurally, not glued into the indexed words.
    assert any(segment.label == "AI description" for segment in document.segments)


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------

def test_every_image_extension_is_registered():
    import app.extract  # noqa: F401
    from app.extract.base import REGISTRY

    for extension in (".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp"):
        assert REGISTRY.get(extension) is not None, f"{extension} is unregistered"
        assert REGISTRY[extension].name == "ocr"


def test_the_shipped_config_has_a_smaller_size_cap_for_images():
    """OCR cost scales with pixels, so a 100MB image is minutes of work for a
    scan nobody will read. The cap is per format for exactly this reason."""
    from app.core.formats import load_rules

    rules = load_rules()
    assert rules.max_bytes_for(".png") < rules.default_max_bytes


# ---------------------------------------------------------------------------
# The OCR ladder: `ocr_image` is the one call site every real caller shares
# ---------------------------------------------------------------------------

def test_the_ladder_is_consulted_for_a_real_path(tmp_path, monkeypatch):
    """A `Path` source is routed through the ladder before the engine runs -
    this is the wiring that was previously missing entirely: `route()` was
    never called from outside its own test file."""
    calls = []

    def fake_route(source, **kwargs):
        calls.append(source)
        return ocr_ladder.LadderResult(
            decision=ocr_ladder.RouteDecision.FULL_OCR,
            elapsed_ms=0.1,
            reason="test",
        )

    monkeypatch.setattr(ocr_ladder, "route", fake_route)

    image = tmp_path / "IMG_1234.jpg"
    image.write_bytes(b"x")
    ocr_image(image, engine=lambda _s: None)

    assert calls == [image]


def test_a_fake_string_source_never_touches_the_ladder(monkeypatch):
    """The unit tests above drive `ocr_image` with plain strings to exercise
    the fake `engine` seam without a real file. Those must behave exactly as
    before: no ladder, no routing, straight to the engine."""
    calls = []
    monkeypatch.setattr(ocr_ladder, "route", lambda *a, **k: calls.append(1))

    ocr_image("x", engine=lambda _s: None)

    assert calls == []


def test_a_ladder_settled_no_text_skips_the_engine_entirely(tmp_path, monkeypatch):
    """When the ladder's detection rung has already found zero text boxes,
    recognition never runs - `checked_no_text` is a truthful settled state,
    not a guess, and distinct from an ordinary empty result."""
    monkeypatch.setattr(
        ocr_ladder, "route",
        lambda *a, **k: ocr_ladder.LadderResult(
            decision=ocr_ladder.RouteDecision.NO_TEXT,
            elapsed_ms=42.0,
            reason="test",
        ),
    )

    def engine_that_must_not_run(_source):
        raise AssertionError("recognition ran after the ladder already settled this")

    image = tmp_path / "wall.jpg"
    image.write_bytes(b"x")
    result = ocr_image(image, engine=engine_that_must_not_run)

    assert result.checked_no_text is True
    assert result.empty


def test_ladder_routing_never_takes_an_image_down(tmp_path, monkeypatch):
    """A broken ladder is a smaller problem than a crashed extractor - OCR
    falls back to running the engine as if the ladder were never wired in."""
    def broken_route(*_a, **_k):
        raise RuntimeError("ladder exploded")

    monkeypatch.setattr(ocr_ladder, "route", broken_route)

    image = tmp_path / "IMG_1234.jpg"
    image.write_bytes(b"x")
    result = ocr_image(image, engine=engine_returning(([[0, 0]], "text", 0.9)))

    assert result.text == "text"


def test_bytes_source_is_also_routed(monkeypatch):
    """RAW previews reach `ocr_image` as bytes, not a `Path` - the RAW
    extractor's own path to the ladder, one call site for every caller."""
    calls = []

    def fake_route(source, **kwargs):
        calls.append(source)
        return ocr_ladder.LadderResult(
            decision=ocr_ladder.RouteDecision.FULL_OCR,
            elapsed_ms=0.1,
            reason="test",
        )

    monkeypatch.setattr(ocr_ladder, "route", fake_route)

    preview = b"fake jpeg bytes"
    ocr_image(preview, engine=lambda _s: None)

    assert calls == [preview]


# ---------------------------------------------------------------------------
# OCR_WHITE_PAGE_PERCENT: rung 1's threshold, promoted to a setting
# ---------------------------------------------------------------------------

def test_ocr_image_passes_the_settings_threshold_to_the_ladder(tmp_path, monkeypatch):
    """The wiring half of non-negotiable 11: a setting that is declared and
    never read is the same bug as no setting at all. `ocr_image` must hand
    whatever `_white_fraction_threshold` returns straight to `route()`."""
    captured: dict = {}

    def fake_route(source, **kwargs):
        captured.update(kwargs)
        return ocr_ladder.LadderResult(
            decision=ocr_ladder.RouteDecision.FULL_OCR,
            elapsed_ms=0.1,
            reason="test",
        )

    monkeypatch.setattr(ocr_ladder, "route", fake_route)
    monkeypatch.setattr(module, "_white_fraction_threshold", lambda: 0.42)

    image = tmp_path / "IMG_1234.jpg"
    image.write_bytes(b"x")
    ocr_image(image, engine=lambda _s: None)

    assert captured.get("white_fraction_threshold") == 0.42


def test_white_fraction_threshold_reads_the_percent_setting_as_a_fraction(
    monkeypatch,
):
    """`OCR_WHITE_PAGE_PERCENT` is stored as a whole-number percentage (the
    registry has no float kind); this is where it becomes the fraction
    `ocr_ladder.route` compares against."""
    import app.core.config as config_module

    class _Stub:
        ocr_white_page_percent = 55

    monkeypatch.setattr(module, "_SETTINGS_WHITE_FRACTION", None)
    monkeypatch.setattr(config_module, "load_settings", lambda *a, **k: _Stub())

    assert module._white_fraction_threshold() == pytest.approx(0.55)


def test_white_fraction_threshold_falls_back_when_settings_cannot_load(
    monkeypatch,
):
    """A missing or broken `.env` must not take OCR down - it degrades to
    exactly the literal this module used before the setting existed."""
    import app.core.config as config_module

    def _raise(*_a, **_k):
        raise RuntimeError("no .env")

    monkeypatch.setattr(module, "_SETTINGS_WHITE_FRACTION", None)
    monkeypatch.setattr(config_module, "load_settings", _raise)

    assert (module._white_fraction_threshold()
            == ocr_ladder.WHITE_FRACTION_THRESHOLD_DEFAULT)


def test_white_fraction_threshold_is_cached(monkeypatch):
    """Read once, not once per image - the same shape as `app/extract/pdf.py`
    `_pages_from_settings`, and for the same reason: this is asked for on
    every routed image, and re-reading `.env` per file is the shape of cost
    that turns a run into an afternoon."""
    import app.core.config as config_module

    calls = []

    class _Stub:
        ocr_white_page_percent = 40

    def _load_settings(*_a, **_k):
        calls.append(1)
        return _Stub()

    monkeypatch.setattr(module, "_SETTINGS_WHITE_FRACTION", None)
    monkeypatch.setattr(config_module, "load_settings", _load_settings)

    first = module._white_fraction_threshold()
    second = module._white_fraction_threshold()

    assert first == second == pytest.approx(0.40)
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# Rung 2 (2c): the detection-only probe, wired end to end through `_detect_only`
# ---------------------------------------------------------------------------

def _detecting_engine(det_boxes, rec_rows=()):
    """A fake RapidOCR-shaped engine honouring `use_det`/`use_cls`/`use_rec`,
    so rung 2's real wiring (`ocr.py`'s `_detect_only`) can be exercised
    without the real engine installed - the same reason `engine_returning`
    exists for the recognition-only tests above."""
    def _run(source, use_det=None, use_cls=None, use_rec=None):
        if use_rec is False:
            return (det_boxes, [0.05])
        return ([list(row) for row in rec_rows], 0.02)
    return _run


@pytest.mark.skipif(not HAS_PIL, reason="Pillow is not installed")
def test_a_wall_photo_settles_no_text_without_recognition_ever_running(tmp_path):
    """§4 'ladder:' item - end to end through `ocr_image`, not just `route()`
    directly: a photograph with no text at all costs one detection pass and
    never reaches recognition. `checked_no_text` is the truthful settled
    state this proves, distinct from an ordinary empty OCR result."""
    from PIL import Image

    photo = tmp_path / "wall.jpg"  # no rung-0 pattern, not white-heavy
    Image.new("RGB", (256, 256), color="blue").save(photo)

    recognition_calls = []

    def engine(source, use_det=None, use_cls=None, use_rec=None):
        if use_rec is False:
            return (None, None)  # RapidOCR's own shape: detection found nothing
        recognition_calls.append(source)
        raise AssertionError("recognition ran after rung 2 already settled this")

    result = ocr_image(photo, engine=engine)

    assert result.checked_no_text is True
    assert result.empty
    assert recognition_calls == []


@pytest.mark.skipif(not HAS_PIL, reason="Pillow is not installed")
def test_a_receipt_photo_reaches_full_ocr_end_to_end(tmp_path):
    """§4 'ladder:' item, the routes-not-rejects proof: a document that is a
    photograph rather than a scan - generic filename, not white-heavy - still
    reaches full recognition the moment rung 2's detector finds text on it."""
    from PIL import Image

    photo = tmp_path / "counter_photo.jpg"
    Image.new("RGB", (256, 256), color="blue").save(photo)

    box = [[0, 0], [10, 0], [10, 10], [0, 10]]
    engine = _detecting_engine(
        det_boxes=[box],
        rec_rows=[(box, "TOTAL 12.99", 0.95)],
    )

    result = ocr_image(photo, engine=engine)

    assert result.checked_no_text is False
    assert result.text == "TOTAL 12.99"


# ---------------------------------------------------------------------------
# Transient GPU device-removed recovery (2026-09-08)
#
# `logs/runs/run-20260908-050751-window.log` (line 121-123): the same DXGI
# device-removed event that broke the embedder also reaches OCR's inference
# call - and before this, it was logged at DEBUG (invisible) and the module-
# global `_engine`/`_engine_is_gpu` were never invalidated, so every image
# after the first for the rest of the run silently got no OCR text.
# ---------------------------------------------------------------------------

_TRANSIENT_MESSAGE = (
    "Fail: [ONNXRuntimeError] : 1 : FAIL : ...DmlExecutionProvider... "
    "887A0005 The GPU device instance has been suspended. Use "
    "GetDeviceRemovedReason to determine the appropriate action."
)


class _FakeLog:
    """Records warning/debug calls without touching the real loguru sink."""

    def __init__(self) -> None:
        self.warnings: list[str] = []
        self.debugs: list[str] = []

    def warning(self, template, *args) -> None:
        self.warnings.append(template.format(*args) if args else template)

    def debug(self, template, *args) -> None:
        self.debugs.append(template.format(*args) if args else template)

    def info(self, *_args, **_kwargs) -> None:
        pass


def test_transient_gpu_inference_failure_invalidates_the_engine(monkeypatch) -> None:
    def device_removed(_source):
        raise RuntimeError(_TRANSIENT_MESSAGE)

    fake_log = _FakeLog()
    monkeypatch.setattr(module, "log", fake_log)
    monkeypatch.setattr(module, "_engine", "a live but now-dead session")
    monkeypatch.setattr(module, "_engine_is_gpu", True)
    monkeypatch.setattr(module, "_warned_transient_gpu", False)

    result = ocr_image("x", engine=device_removed)

    assert result.empty, "one image's trouble must not raise out of ocr_image"
    assert module._engine is None, "the dead session must be cleared"
    assert module._engine_is_gpu is False
    assert fake_log.warnings, "a real hardware event must be visible, not silent"
    assert fake_log.debugs == [], "this must not also log at debug"


def test_transient_gpu_inference_failure_warns_once_not_per_image(monkeypatch) -> None:
    def device_removed(_source):
        raise RuntimeError(_TRANSIENT_MESSAGE)

    fake_log = _FakeLog()
    monkeypatch.setattr(module, "log", fake_log)
    monkeypatch.setattr(module, "_engine", "session-1")
    monkeypatch.setattr(module, "_warned_transient_gpu", False)

    ocr_image("x", engine=device_removed)
    # A second image hitting the same trouble again (engine still None from
    # the first failure, but the *injected* engine bypasses `_load_engine`,
    # so this simulates "still broken" rather than "reloaded").
    monkeypatch.setattr(module, "_engine", "session-1")   # pretend it reloaded
    ocr_image("x", engine=device_removed)

    assert len(fake_log.warnings) == 1, "must warn once, not on every image"


def test_a_non_transient_ocr_failure_still_logs_at_debug_only(monkeypatch) -> None:
    """Only the classified failure class gets the visible warning and the
    engine invalidation - an ordinary one-off OCR failure keeps behaving
    exactly as before: quiet, and the engine untouched."""
    def explode(_source):
        raise RuntimeError("some unrelated OCR failure")

    fake_log = _FakeLog()
    monkeypatch.setattr(module, "log", fake_log)
    monkeypatch.setattr(module, "_engine", "a perfectly healthy session")
    monkeypatch.setattr(module, "_engine_is_gpu", True)

    result = ocr_image("x", engine=explode)

    assert result.empty
    assert fake_log.warnings == []
    assert fake_log.debugs, "an ordinary failure must still be noted at debug"
    assert module._engine == "a perfectly healthy session", \
        "a non-transient failure must not invalidate the engine"
    assert module._engine_is_gpu is True


def test_engine_reloads_and_succeeds_after_a_transient_gpu_failure(monkeypatch) -> None:
    """The invalidation actually matters: the *next* image must reach a
    freshly loaded engine (via `_load_engine`) and get real text back,
    proving recovery rather than a reset nobody reads again."""
    calls = {"loads": 0}

    class _FlakyEngine:
        def __init__(self) -> None:
            self.broken = calls["loads"] == 0
            calls["loads"] += 1

        def __call__(self, _source):
            if self.broken:
                raise RuntimeError(_TRANSIENT_MESSAGE)
            box = [[0, 0], [10, 0], [10, 10], [0, 10]]
            return ([(box, "recovered text", 0.95)], 0.01)

    def fake_load_engine():
        if module._engine is None:
            module._engine = _FlakyEngine()
            module._engine_is_gpu = False
        return module._engine

    monkeypatch.setattr(module, "log", _FakeLog())
    monkeypatch.setattr(module, "_engine", None)
    monkeypatch.setattr(module, "_load_engine", fake_load_engine)

    first = ocr_image("x")
    assert first.empty
    assert module._engine is None, "the broken engine must be cleared"

    second = ocr_image("x")
    assert second.text == "recovered text"
    assert calls["loads"] == 2, "recovery must reload, not reuse the broken engine"


# --- 2026-09-08, later: the reload lands on the processor -------------------


def test_transient_gpu_failure_latches_the_driver_so_the_reload_chooses_the_cpu(monkeypatch) -> None:
    """`_load_engine` goes through `backends.choose`, which reads the
    process-wide latch - so after one transient failure the next engine is
    built without DirectML, with no OCR-specific code deciding that."""
    from app.core.gpu_serialize import gpu_unreliable
    from app.index import backends

    def device_removed(_source):
        raise RuntimeError(_TRANSIENT_MESSAGE)

    monkeypatch.setattr(module, "log", _FakeLog())
    monkeypatch.setattr(module, "_engine", "a live but now-dead session")
    monkeypatch.setattr(module, "_engine_is_gpu", True)
    monkeypatch.setattr(module, "_warned_transient_gpu", False)

    ocr_image("x", engine=device_removed)

    assert gpu_unreliable(), "one transient OCR failure must set the process-wide latch"

    class _Card:
        gpus = ("Iris Xe",)
        directml_available = True

    assert not backends.choose(_Card(), "auto").is_gpu, \
        "the next _load_engine() must choose the processor"


def test_a_cpu_reload_after_a_transient_failure_costs_nothing_from_the_budget(monkeypatch) -> None:
    """`_engine_attempts` counts *failed constructions* only. A rebuild on
    the processor that succeeds must leave the budget as it was."""
    class _Card:
        gpus = ("Iris Xe",)
        directml_available = True

    built: list[dict] = []

    class _FakeRapidOCR:
        def __init__(self, **kwargs):
            built.append(kwargs)

        def __call__(self, _source):
            return ([], 0.0)

    import types
    fake_pkg = types.SimpleNamespace(RapidOCR=_FakeRapidOCR)
    monkeypatch.setitem(__import__("sys").modules, "rapidocr_onnxruntime", fake_pkg)
    monkeypatch.setattr(module, "log", _FakeLog())
    monkeypatch.setattr(module, "_profile", lambda: _Card())
    monkeypatch.setattr(module, "_engine", None)
    monkeypatch.setattr(module, "_engine_failed", False)
    monkeypatch.setattr(module, "_engine_attempts", 0)
    monkeypatch.setattr(module, "_engine_is_gpu", False)
    monkeypatch.setattr(module, "_warned_transient_gpu", False)
    monkeypatch.setattr(module, "_device", "auto")

    first = module._load_engine()
    assert first is not None and built[0].get("det_use_dml") is True, \
        "the setup must start on the graphics card or the test proves nothing"

    def device_removed(_source):
        raise RuntimeError(_TRANSIENT_MESSAGE)

    monkeypatch.setattr(module, "_engine_is_gpu", True)
    ocr_image("x", engine=device_removed)
    assert module._engine is None

    second = module._load_engine()
    assert second is not None and second is not first
    assert built[1] == {}, "the reload must be built without DirectML"
    assert module._engine_attempts == 0, "a reload that works must not spend the budget"
    assert module._engine_failed is False
