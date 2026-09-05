"""Workspace §7: "suffix sets consistent between indexer and preview".

Layer: L5

**The `.tiff` class of drift, as a test.** `.tif`/`.tiff` were once accepted
by the indexer's OCR extractor and absent from the preview's own image list -
a scanner's output indexed happily and previewed as "no preview for this
type", which is the kind of gap nobody notices until someone reports it.

This pins the two lists equal rather than merely overlapping, because a
one-way check ("everything OCR reads, the preview also shows") would still
let the preview grow a fourth image type OCR knows nothing about and call
that consistent.
"""

from __future__ import annotations

from app.extract.ocr import OcrExtractor
from app.ui.preview_loader import _IMAGE_SUFFIXES


def test_image_suffixes_match_the_indexers_ocr_extensions():
    assert _IMAGE_SUFFIXES == OcrExtractor.extensions, (
        "the preview's image suffixes and the indexer's OCR extensions have "
        "drifted apart - a type one side accepts and the other does not is "
        "exactly the '.tiff' bug workspace §4a's note describes"
    )
