"""Indexing speed, 2026-09-29: the four measured changes, pinned.

Layer: L2/L3 (tests)

Each test pins one behaviour a measurement on a mixed corpus of 250 documents
and 220 pictures justified (numbers in `CHANGELOG.md`, Unreleased):

1. OCR's rung-2 probe makes the image's one engine call and it is reused -
   a picture with text is no longer decoded and detected twice.
2. A large JPEG reaches the engine already decoded at the engine's own size
   (`draft`), never below it; everything else reaches it unchanged.
3. A scanned PDF's page reaches OCR as uncompressed PNM rather than PNG, and
   a wholly scanned PDF is left for the pictures pass on the text-first pass.
4. A CLIP model that will not load is tried `CLIP_LOAD_ATTEMPTS` times per
   run, not once per picture.

Every test uses a fake engine or encoder: no model, no network.
"""

from __future__ import annotations

import pytest

PIL = pytest.importorskip("PIL")
from PIL import Image  # noqa: E402

from app.extract import ocr  # noqa: E402


class _Engine:
    """RapidOCR-shaped: `(rows, timings)`, `(None, None)` when nothing is found."""

    def __init__(self, rows=None, max_side_len=None):
        self.rows = rows
        self.calls: list[object] = []
        if max_side_len is not None:
            self.max_side_len = max_side_len

    def __call__(self, source, **kwargs):
        self.calls.append(source)
        if not self.rows:
            return None, None
        return [list(r) for r in self.rows], [0.01]


_BOX = [[0, 0], [10, 0], [10, 10], [0, 10]]


@pytest.fixture(autouse=True)
def _cpu_engine(monkeypatch):
    monkeypatch.setattr(ocr, "_engine_is_gpu", False, raising=False)


# -- 1. one engine call per picture -----------------------------------------

def test_a_picture_with_text_costs_one_engine_call(tmp_path):
    """Before: a detection-only call, then the full call - two decodes and two
    detections for every picture the detector found anything in."""
    photo = tmp_path / "kitchen.jpg"          # no rung-0 pattern, not white-heavy
    Image.new("RGB", (300, 200), "navy").save(photo)
    engine = _Engine(rows=[(_BOX, "TOTAL 12.99", 0.95)])

    result = ocr.ocr_image(photo, engine=engine)

    assert result.text == "TOTAL 12.99"
    assert not result.checked_no_text
    assert len(engine.calls) == 1


def test_a_page_rung_one_sends_straight_to_ocr_is_still_one_call(tmp_path):
    page = tmp_path / "letter.png"            # white-heavy: rung 1 decides FULL_OCR
    Image.new("RGB", (300, 400), "white").save(page)
    engine = _Engine(rows=[(_BOX, "Dear Sir", 0.9)])

    result = ocr.ocr_image(page, engine=engine)

    assert result.text == "Dear Sir"
    assert len(engine.calls) == 1


def test_a_textless_picture_is_settled_by_its_one_call(tmp_path):
    photo = tmp_path / "wall.jpg"
    Image.new("RGB", (300, 200), "navy").save(photo)
    engine = _Engine(rows=None)

    result = ocr.ocr_image(photo, engine=engine)

    assert result.empty and result.checked_no_text
    assert len(engine.calls) == 1


# -- 2. large JPEGs decoded at the engine's size ------------------------------

def test_a_large_jpeg_reaches_the_engine_drafted_never_below_its_size(tmp_path):
    photo = tmp_path / "holiday.jpg"
    Image.new("RGB", (2400, 1800), "green").save(photo, quality=80)
    engine = _Engine(rows=None, max_side_len=1000)

    fed = ocr._engine_input(photo, engine)

    assert isinstance(fed, Image.Image), "a large JPEG should be decoded here"
    assert max(fed.size) >= 1000, "draft must never go below the engine's size"
    assert max(fed.size) < 2400, "and must actually be smaller than the original"

    ocr.ocr_image(photo, engine=engine)
    assert isinstance(engine.calls[0], Image.Image)


@pytest.mark.parametrize("kind", ["png", "small_jpeg", "cmyk_jpeg", "garbage"])
def test_everything_else_reaches_the_engine_unchanged(tmp_path, kind):
    path = tmp_path / f"x.{'png' if kind == 'png' else 'jpg'}"
    if kind == "png":
        Image.new("RGB", (2400, 1800), "green").save(path)
    elif kind == "small_jpeg":
        Image.new("RGB", (1500, 1000), "green").save(path)
    elif kind == "cmyk_jpeg":
        Image.new("CMYK", (2400, 1800)).save(path)
    else:
        path.write_bytes(b"not a picture at all")
    engine = _Engine(rows=None, max_side_len=1000)

    assert ocr._engine_input(path, engine) is path


def test_bytes_sources_are_drafted_too(tmp_path):
    import io

    buffer = io.BytesIO()
    Image.new("L", (2400, 1800), 128).save(buffer, "JPEG")
    fed = ocr._engine_input(buffer.getvalue(), _Engine(max_side_len=1000))
    assert isinstance(fed, Image.Image) and max(fed.size) >= 1000


# -- 3. scanned PDFs ----------------------------------------------------------

def _scanned_pdf(tmp_path):
    pymupdf = pytest.importorskip("pymupdf")
    document = pymupdf.open()
    page = document.new_page()
    pixmap = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 160, 160))
    pixmap.set_rect(pixmap.irect, (210, 210, 210))
    page.insert_image(pymupdf.Rect(72, 72, 232, 232), pixmap=pixmap)
    path = tmp_path / "scan.pdf"
    document.save(str(path))
    document.close()
    return path


@pytest.fixture
def _fake_page_ocr(monkeypatch):
    from app.extract.pdf import PDF_OCR_PAGES_VAR

    monkeypatch.setenv(PDF_OCR_PAGES_VAR, "3")
    monkeypatch.setattr(ocr, "available", lambda: True)
    calls: list[bytes] = []

    def fake(source):
        calls.append(source)
        return ocr.OcrResult(text="Read from the scanned page.", lines=1)

    monkeypatch.setattr(ocr, "ocr_image", fake)
    return calls


def test_a_scanned_page_reaches_ocr_uncompressed(tmp_path, _fake_page_ocr):
    from app.extract.pdf import PdfExtractor

    documents = list(PdfExtractor().extract(_scanned_pdf(tmp_path)))

    assert documents and "Read from the scanned page." in documents[0].text
    assert _fake_page_ocr and _fake_page_ocr[0][:2] in (b"P6", b"P5"), (
        "a rendered page should be PNM, not a PNG it costs ~200 ms to compress")
    Image.open(__import__("io").BytesIO(_fake_page_ocr[0])).load()  # Pillow reads it


def test_the_text_first_pass_leaves_a_scanned_pdf_for_the_pictures_pass(
        tmp_path, _fake_page_ocr):
    from app.core.errors import AppErrorException
    from app.extract import reading
    from app.extract.pdf import PdfExtractor

    path = _scanned_pdf(tmp_path)
    with reading.reading(images=reading.IMAGES_HOLD):
        with pytest.raises(AppErrorException) as raised:
            list(PdfExtractor().extract(path))

    assert raised.value.error.code == "ERR_NO_TEXT_LAYER", (
        "the queue entry the pictures pass reads its scanned PDFs from")
    assert _fake_page_ocr == [], "no page may be OCR'd on the text-first pass"

    with reading.reading(images=reading.IMAGES_ONLY):
        documents = list(PdfExtractor().extract(path))
    assert documents and len(_fake_page_ocr) == 1, "the pictures pass reads it"


# -- 4. a CLIP model that will not load --------------------------------------

def test_a_clip_model_that_will_not_load_is_not_retried_per_picture(monkeypatch):
    from app.core.errors import AppErrorException, make_error
    from app.index import clip_embedder
    from app.index.clip_embedder import CLIP_LOAD_ATTEMPTS, ClipImageEmbedder

    attempts: list[int] = []

    def refuse(self):
        attempts.append(1)
        raise AppErrorException(make_error(
            "ERR_MODEL_LOAD", "index.clip_embedder", details="no network"))

    monkeypatch.setattr(clip_embedder.ClipImageEmbedder, "_load_encoder", refuse)
    embedder = ClipImageEmbedder()

    for _ in range(20):
        with pytest.raises(AppErrorException) as raised:
            embedder.embed(["photo.jpg"])
        assert raised.value.error.code == "ERR_MODEL_LOAD"

    assert len(attempts) == CLIP_LOAD_ATTEMPTS


def test_a_clip_load_that_fails_once_then_works_still_loads(monkeypatch):
    from app.core.errors import AppErrorException, make_error
    from app.index import clip_embedder
    from app.index.clip_embedder import ClipImageEmbedder

    state = {"n": 0}

    def flaky(self):
        state["n"] += 1
        if state["n"] == 1:
            raise AppErrorException(make_error(
                "ERR_MODEL_LOAD", "index.clip_embedder", details="file busy"))
        self._encoder = lambda paths: [[1.0] + [0.0] * 511 for _ in paths]
        return self._encoder

    monkeypatch.setattr(clip_embedder.ClipImageEmbedder, "_load_encoder", flaky)
    embedder = ClipImageEmbedder()

    with pytest.raises(AppErrorException):
        embedder.embed(["a.jpg"])
    assert len(embedder.embed(["a.jpg"])[0]) == 512
