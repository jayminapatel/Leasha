"""`app/ort/florence.py` - Florence-2 on ONNX Runtime.

Layer: L2. The pure parts always; the real model when it is downloaded.

2026-09-29, measured on the owner's laptop against the torch path it replaces,
four Windows wallpaper photos: identical tags, captions of the same quality in
different words, 11-14 s a photo on the processor either way, 4.4 s to load
against 22.4 s. The int8 graphs on DirectML produced nonsense, which is why
`app/ort/session.py` keeps quantised graphs on the processor.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from app.ort import florence, hub
from app.ort.session import is_quantised


def test_pixel_values_match_the_image_processor():
    from PIL import Image

    image = Image.new("RGB", (300, 200), (255, 0, 0))
    pixels = florence.pixel_values(image)
    assert pixels.shape == (1, 3, 768, 768) and pixels.dtype == np.float32
    # pure red: (1 - 0.485) / 0.229 in the red plane, (0 - mean) / std elsewhere
    assert np.allclose(pixels[0, 0].mean(), (1 - 0.485) / 0.229, atol=1e-3)
    assert np.allclose(pixels[0, 1].mean(), (0 - 0.456) / 0.224, atol=1e-3)


def test_special_tokens_are_removed_from_a_caption():
    assert florence.clean_text("<s>A dog on a beach.</s><pad>") == "A dog on a beach."


def test_object_labels_are_read_from_an_od_answer():
    raw = ("</s><s>dog<loc_12><loc_40><loc_300><loc_500>person<loc_1><loc_2><loc_3><loc_4>"
           "Dog<loc_5><loc_6><loc_7><loc_8></s>")
    assert florence.od_labels(raw) == ("dog", "person")


def test_labels_with_spaces_and_brackets_survive():
    raw = "<s>statue (sculpture)<loc_1><loc_2><loc_3><loc_4></s>"
    assert florence.od_labels(raw) == ("statue (sculpture)",)


def test_quantised_graphs_are_recognised_by_name():
    assert is_quantised(Path("onnx/decoder_model_merged_int8.onnx"))
    assert not is_quantised(Path("onnx/decoder_model_merged.onnx"))


def _cache() -> Path | None:
    try:
        from app.core.config import load_settings

        return Path(load_settings(create_dirs=False, check_writable=False).model_cache)
    except Exception:                                  # noqa: BLE001
        return None


@pytest.mark.slow
@pytest.mark.skipif(hub.resolve(hub.FLORENCE, _cache()) is None,
                    reason="the Florence-2 ONNX model is not downloaded")
def test_the_real_model_captions_and_tags_a_picture():
    """Shape, not wording: a non-empty caption with no special tokens left in
    it, tags as a tuple of lowercase strings."""
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (640, 480), "white")
    ImageDraw.Draw(image).ellipse((200, 120, 440, 360), fill="red")
    engine = florence.OnnxFlorence.from_cache(_cache(), device="cpu")
    caption, tags = engine.caption_and_tags(image, max_new_tokens=48)
    assert caption and "<" not in caption
    assert isinstance(tags, tuple) and all(t == t.lower() for t in tags)
