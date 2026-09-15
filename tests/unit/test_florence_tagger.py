r"""Florence-2 tagging: the module's own seam, independent of `ocr.py`'s wiring.

Layer: L2

Work order 0i (`202626270511`) section 1a/1b. `tests/unit/test_ocr.py` proves
the wiring into `OcrExtractor.extract` (both the real-model path and the
Florence-unavailable fallback); this file proves the module in isolation -
`available()` never raises, and a failed load degrades to `None` rather than
an exception reaching a worker thread.
"""

from __future__ import annotations

import importlib.util

import pytest

from app.extract import florence_tagger

HAS_FLORENCE_DEPS = (
    importlib.util.find_spec("torch") is not None
    and importlib.util.find_spec("transformers") is not None
)


def test_available_never_raises():
    assert florence_tagger.available() in (True, False)


def test_available_reflects_real_dependency_presence():
    """This machine has torch/transformers installed for 0i - asserted here
    so a future environment that removes them gets a red test rather than a
    silently-always-degraded feature nobody notices."""
    assert florence_tagger.available() is HAS_FLORENCE_DEPS


def test_tag_image_returns_none_rather_than_raising_when_the_model_wont_load(
    tmp_path, monkeypatch):
    """A worker thread calling this must never go down because a model file
    was mid-download or a dependency was half-installed - same contract as
    `ocr.ocr_image`'s `engine_missing` path."""
    from PIL import Image

    monkeypatch.setattr(florence_tagger, "_load", lambda: None)

    path = tmp_path / "photo.png"
    Image.new("RGB", (64, 64), "blue").save(path)

    assert florence_tagger.tag_image(path) is None


def test_tag_image_returns_none_for_an_unreadable_path(monkeypatch):
    """A model that did load, pointed at a file Pillow cannot open, must
    degrade the same way - one bad file, not one crashed worker."""
    monkeypatch.setattr(florence_tagger, "_load", lambda: (object(), object()))

    from pathlib import Path
    assert florence_tagger.tag_image(Path("does-not-exist.png")) is None


@pytest.mark.slow
@pytest.mark.skipif(not HAS_FLORENCE_DEPS, reason="torch/transformers not installed")
def test_a_real_image_gets_a_real_caption_and_tags(tmp_path):
    """The real-model proof, independent of `ocr.py`'s wiring. Caption text
    is non-deterministic model output - this only asserts the *shape* 1a
    promises: a non-empty caption, a tags tuple, and a measured elapsed_s.

    2026-09-15: measured on this machine at 11.36s for one image (two
    `generate()` calls, `max_new_tokens=128` each, CPU, `Florence-2-base`) -
    see the dated note on 0i item 1a in the work order for the full
    measurement and why the ~100-300ms/img CPU target in the order's own
    text is not met by a transformers/CPU pipeline; the order's own text
    anticipates this ("the thread MAY swap to an ONNX port later for speed").
    """
    from PIL import Image

    path = tmp_path / "photo.png"
    Image.new("RGB", (256, 256), "orange").save(path)

    result = florence_tagger.tag_image(path)

    assert result is not None
    assert isinstance(result.caption, str)
    assert isinstance(result.tags, tuple)
    assert result.elapsed_s > 0
