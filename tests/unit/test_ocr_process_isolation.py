r"""Work order `pictures-process-isolation`: a fault in the text-in-pictures model
costs the picture, never the run.

Layer: L2/L3

The index process died on 2026-10-09 at 12:25 inside ONNX Runtime's DirectML
path, OCR-ing a picture on a reader thread. Order 1e had put the reading
libraries in a child and left the OCR model in the parent. This order moves the
model to a helper process of its own (`app/index/ocr_process.py`), installed
as `ocr.set_engine_process` for a run; a reader child relays a scanned page it
rendered to the parent under the same seam, so no process but the helper ever
loads the engine.

Two kinds of test: a real helper and a real reader child (the proof that the
pipes work, a few seconds each), and a stand-in child for the one thing a real
one cannot do deterministically - die with a picture in flight.
"""

from __future__ import annotations

import contextlib
import io
import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.errors import AppErrorException
from app.extract import ocr, reading
from app.extract.ocr import OcrResult
from app.index import ocr_process, read_process
from app.index.ocr_process import OcrProcess
from app.index.read_process import ReaderProcess


@pytest.fixture(autouse=True)
def _nothing_left_installed():
    yield
    ocr.set_engine_process(None)


def _png(path: Path, text: str = "HELLO WORLD 2026") -> Path:
    """A picture with words in it, drawn large enough for any OCR engine."""
    pytest.importorskip("PIL")
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (640, 160), "white")
    ImageDraw.Draw(image).text((20, 50), text, fill="black")
    image.save(path)
    return path


# --- 2a: a real helper answers, and says what its engine ran on ----------------------------


def test_a_real_helper_answers_a_picture_and_the_parent_never_loads_the_engine(tmp_path, monkeypatch):
    picture = _png(tmp_path / "words.png")
    engine_before = ocr._engine
    helper = OcrProcess(low_priority=False)
    try:
        result = helper.ocr(picture)
        assert isinstance(result, OcrResult)
        assert result.error is None
        # Either the engine is installed here and read the picture, or it is
        # not and the helper said so - never a crash, never a wrong kind.
        assert result.engine_missing or result.text or result.checked_no_text or result.empty
        assert helper.engine_device in ("gpu", "cpu", "")
        assert helper.started == 1
        # The same helper, a second picture, side by side from two threads.
        answers: list = []

        def ask() -> None:
            answers.append(helper.ocr(picture))

        threads = [threading.Thread(target=ask) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=120)
        assert len(answers) == 2 and all(a.error is None for a in answers)
    finally:
        helper.close()
    assert ocr._engine is engine_before, "the parent loaded the OCR engine itself"


def test_a_dead_helper_is_started_again_for_the_next_picture(tmp_path):
    picture = _png(tmp_path / "words.png")
    helper = OcrProcess(low_priority=False)
    try:
        assert helper.wait_ready()
        helper.kill_child()
        for _ in range(100):                      # until Windows has reaped it
            if not helper.alive:
                break
            time.sleep(0.05)
        result = helper.ocr(picture)              # nothing was in flight: a fresh helper, silently
        assert result.error is None
        assert helper.started == 2
    finally:
        helper.close()


# --- 2b: a helper that dies with a picture in hand costs that picture --------------------


class _StandInChild:
    """A child that says "ready", reads one request, and then goes - the pipe
    closes with the picture in flight, which a real child cannot be made to do
    at a deterministic moment."""

    def __init__(self) -> None:
        reader_fd, writer_fd = os.pipe()
        self.stdout = os.fdopen(reader_fd, "rb")
        self._out = os.fdopen(writer_fd, "wb")
        self.stdin = self
        self.pid = 4242
        self._code = None
        self.requests: list = []
        read_process._write(self._out, ("ready", self.pid))

    # the parent writes requests to `stdin`
    def write(self, data: bytes) -> int:
        self.requests.append(data)
        if len(self.requests) >= 1:              # one frame is one request: the picture is in
            self._code = 3
            self._out.close()                    # the child "died": the parent's listener sees EOF
        return len(data)

    def flush(self) -> None:
        pass

    def close(self) -> None:
        pass

    def poll(self):
        return self._code

    def wait(self, timeout=None):
        return self._code if self._code is not None else 0

    def kill(self) -> None:
        self._code = -9
        with contextlib.suppress(Exception):
            self._out.close()


def test_a_helper_that_dies_with_a_picture_in_hand_costs_that_picture(tmp_path):
    child = _StandInChild()
    helper = OcrProcess(low_priority=False, python="python", popen=lambda *a, **k: child)
    result = helper.ocr(tmp_path / "in-hand.png")
    assert result.error is not None
    assert result.error.code == "ERR_OCR_PROCESS_ENDED"
    assert "in-hand.png" in result.error.render()
    assert helper.started == 1 and not helper.alive


def test_the_picture_reader_records_a_dead_helper_by_its_code(tmp_path, monkeypatch):
    """The text pass: `OcrExtractor` turns the result's error into the skip."""
    picture = _png(tmp_path / "words.png")
    monkeypatch.setattr(ocr, "available", lambda: True)
    from app.extract import florence_tagger

    monkeypatch.setattr(florence_tagger, "deferred", lambda: False)
    died = OcrResult(error=ocr_process.OcrProcess._ended_error("words.png", "it exited with code 3"))
    ocr.set_engine_process(lambda source: died)
    with pytest.raises(AppErrorException) as caught:
        list(ocr.OcrExtractor().extract(picture))
    assert caught.value.error.code == "ERR_OCR_PROCESS_ENDED"


# --- 1a/1b: a reader child renders a scanned page and asks the parent for its text ------


def _scanned_pdf(path: Path) -> Path:
    """A one-page PDF whose only content is a picture of words: no text layer."""
    pymupdf = pytest.importorskip("pymupdf")
    pytest.importorskip("PIL")
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (1200, 400), "white")
    ImageDraw.Draw(image).text((40, 150), "SCANNED PAGE", fill="black")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    document = pymupdf.open()
    page = document.new_page()
    page.insert_image(page.rect, stream=buffer.getvalue())
    document.save(str(path))
    document.close()
    return path


@pytest.fixture
def reader():
    process = ReaderProcess(low_priority=False)
    try:
        yield process
    finally:
        process.close()


def test_on_the_images_pass_a_child_renders_the_page_and_the_parent_reads_it(
        tmp_path, reader, monkeypatch):
    pdf = _scanned_pdf(tmp_path / "scan.pdf")
    monkeypatch.setenv("LEASHA_PDF_OCR_PAGES", "3")      # the child reads it from its environment
    asked: list = []

    def parent_ocr(source, **_kw):
        asked.append(type(source).__name__)
        return OcrResult(text="RELAYED PAGE TEXT from the parent's helper", lines=1)

    monkeypatch.setattr(ocr, "ocr_image", parent_ocr)
    engine_before = ocr._engine
    with reading.reading(images=reading.IMAGES_ONLY):
        documents = list(reader.read(pdf))
    assert asked == ["bytes"], "the child sent the rendered page; the parent read it"
    texts = " ".join(chunk["text"] for _doc, chunks in documents for chunk in chunks)
    assert "RELAYED PAGE TEXT" in texts
    assert ocr._engine is engine_before, "the parent loaded the OCR engine itself"


def test_on_the_text_pass_the_child_still_holds_a_scanned_pdf(tmp_path, reader, monkeypatch):
    pdf = _scanned_pdf(tmp_path / "scan.pdf")
    monkeypatch.setenv("LEASHA_PDF_OCR_PAGES", "3")
    asked: list = []
    monkeypatch.setattr(ocr, "ocr_image", lambda source, **_kw: asked.append(source) or OcrResult())
    with reading.reading(images=reading.IMAGES_HOLD), pytest.raises(AppErrorException) as caught:
        list(reader.read(pdf))
    assert caught.value.error.code == "ERR_NO_TEXT_LAYER"
    assert asked == [], "the text pass holds scanned pages for the pictures pass"


def test_a_dead_helper_during_a_relayed_page_records_the_pdf(tmp_path, reader, monkeypatch):
    pdf = _scanned_pdf(tmp_path / "scan.pdf")
    monkeypatch.setenv("LEASHA_PDF_OCR_PAGES", "3")
    died = OcrResult(error=OcrProcess._ended_error("scan.pdf page", "it exited with code 3"))
    monkeypatch.setattr(ocr, "ocr_image", lambda source, **_kw: died)
    with reading.reading(images=reading.IMAGES_ONLY), pytest.raises(AppErrorException) as caught:
        list(reader.read(pdf))
    assert caught.value.error.code == "ERR_OCR_PROCESS_ENDED"


# --- D2: one switch covers the readers and the helper ------------------------------------


def test_the_pipeline_installs_the_helper_only_when_reader_processes_are_on(monkeypatch):
    from app.index.pipeline import Pipeline

    closed: list = []

    class _FakeHelper:
        def __init__(self, *, low_priority):
            self.low_priority = low_priority

        def ocr(self, source):
            return OcrResult(text="from the helper")

        def close(self):
            closed.append(True)

    monkeypatch.setattr(ocr_process, "OcrProcess", _FakeHelper)
    pipeline = Pipeline.__new__(Pipeline)
    pipeline._log = SimpleNamespace(warning=lambda *a, **k: None)
    pipeline.config = SimpleNamespace(
        read_processes=False, resolved_limits=lambda: SimpleNamespace(low_priority=False))
    pipeline._install_ocr_helper()
    assert ocr.engine_process() is None

    pipeline.config.read_processes = True
    pipeline._install_ocr_helper()
    assert ocr.engine_process() is not None
    assert ocr.ocr_image(Path("any.png")).text == "from the helper"
    pipeline._close_ocr_helper()
    assert ocr.engine_process() is None and closed == [True]


def test_an_injected_engine_bypasses_the_helper():
    """The seam the OCR tests drive with fake sources is untouched."""
    ocr.set_engine_process(lambda source: OcrResult(text="helper"))
    result = ocr.ocr_image("not-a-real-source", engine=lambda _s: ([], 0.0))
    assert result.text != "helper"


def test_the_helper_has_a_crash_file_of_its_own():
    from app.core.crash_guard import CRASH_FILES

    assert CRASH_FILES["ocr"] == "ocr-crash.log"
