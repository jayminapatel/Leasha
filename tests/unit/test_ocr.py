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
def test_a_blank_page_yields_nothing_rather_than_an_empty_document(tmp_path):
    """Which becomes ERR_NO_TEXT_LAYER upstream - the honest answer for a
    photograph of a wall, and one that lands in the skip ledger to be counted."""
    from PIL import Image

    path = tmp_path / "blank.png"
    Image.new("RGB", (600, 200), "white").save(path)

    assert list(OcrExtractor().extract(path)) == []


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
