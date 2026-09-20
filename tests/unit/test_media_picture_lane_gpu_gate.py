r"""The picture lane's OCR calls are serialised on the graphics card - all of them.

Layer: L2

**The crash these tests are written from.** A first run of the picture stack
over the owner's `PhotosMaster\2008` (131 photographs) ended with exit 139 - a
native access violation - with two extraction workers, after the OCR engine had
loaded. `app/core/gpu_serialize.py` exists precisely to stop two DirectML
onnxruntime calls overlapping, and `app/extract/ocr.py::ocr_image` has wrapped
its *recognition* call in `gpu_exclusive` since that module was written. The OCR
ladder's rung 2 - the detection-only probe added later - calls the **same three
DirectML sessions** and was never put behind the same gate.

Measured on this machine, 131 real photographs, `EMBED_DEVICE=auto` (DirectML):

  one thread   0 native failures, text read normally (lines=1..5)
  four threads 261 `Unknown C++ exception from OpenCV code` faults, `lines=0`
               on every image, 522 `Windows fatal exception` reports

and `logs/crash/crash.log` for 2026-09-12 shows the same picture reaching a
real crash: three threads inside RapidOCR's text detector at once, two of them
arriving through `ocr.py::_detect`, and a fourth queued at `gpu_exclusive`.

These tests use the `engine` seam, never a real model, so they prove the gating
contract on any machine - with no OCR package, no graphics card and no photos.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from app.extract import ocr


class _CountingEngine:
    """A fake RapidOCR that records how many threads were inside it at once."""

    def __init__(self, hold_s: float = 0.05) -> None:
        self.hold_s = hold_s
        self._lock = threading.Lock()
        self.inside = 0
        self.max_inside = 0
        self.calls = 0
        self.detect_calls = 0

    def __call__(self, source, **kwargs):
        with self._lock:
            self.inside += 1
            self.calls += 1
            if kwargs.get("use_rec") is False:
                self.detect_calls += 1
            self.max_inside = max(self.max_inside, self.inside)
        try:
            time.sleep(self.hold_s)
        finally:
            with self._lock:
                self.inside -= 1
        if kwargs.get("use_rec") is False:
            # Detection found one box, so the ladder falls through to full OCR.
            return [[(0, 0), (10, 0), (10, 10), (0, 10)]], 0.01
        return [[None, "some words", 0.9]], 0.01


@pytest.fixture
def engine_on_the_card(monkeypatch):
    """`ocr_image` believes the loaded engine is a graphics-card session.

    That is the only condition under which either gate does anything, and it is
    a module global rather than an argument because `ocr.py` is a registered
    extractor reached with a path and nothing else.
    """
    monkeypatch.setattr(ocr, "_engine_is_gpu", True, raising=False)
    yield


def test_the_detection_probe_runs_inside_the_graphics_card_gate(
        engine_on_the_card, tmp_path):
    """**The exit-139 regression.** Two workers must never be inside the OCR
    engine at the same moment when it is a DirectML session - not in the
    recognition pass, and not in the ladder's detection probe either, which is
    the same three sessions and was the unguarded half.

    Fails before the fix with `max_inside == 2`: both threads sail into
    `_detect` together, because only the recognition call below it was gated.
    """
    fake = _CountingEngine()
    # A name rung 0 has no opinion about, pointing at nothing, so rung 1's
    # thumbnail read fails and the image reaches rung 2 - the probe under test.
    source = tmp_path / "photo.png"

    def read_one():
        for _ in range(3):
            ocr.ocr_image(source, engine=fake)

    threads = [threading.Thread(target=read_one) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert fake.detect_calls > 0, (
        "the ladder never reached rung 2, so this test proved nothing")
    assert fake.max_inside == 1, (
        f"{fake.max_inside} threads were inside the graphics-card OCR engine at "
        "once - two concurrent DirectML calls is the access violation "
        "app/core/gpu_serialize.py exists to prevent")


def test_the_processor_path_is_not_serialised(monkeypatch, tmp_path):
    """A CPU engine must stay as concurrent as it always was.

    `gpu_serialize`'s own rule: independent CPU-provider sessions are safe for
    concurrent use, and slowing a processor-only machine down to fix a graphics
    card's problem would be a worse bug than the one being fixed. Measured
    above: four threads on the processor over the same 131 photographs gave 0
    failures and read text normally.
    """
    monkeypatch.setattr(ocr, "_engine_is_gpu", False, raising=False)
    fake = _CountingEngine(hold_s=0.08)
    source = tmp_path / "photo.png"

    def read_one():
        ocr.ocr_image(source, engine=fake)

    threads = [threading.Thread(target=read_one) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert fake.max_inside > 1, (
        "the processor path was serialised - a CPU-only machine now pays for a "
        "graphics card's thread-safety problem")


def test_one_unreadable_image_costs_one_image_and_is_said_out_loud_once(
        monkeypatch, tmp_path, caplog):
    """A native fault inside the engine costs that image, never the run - and
    the first one is reported at warning, not swallowed at debug.

    The DirectML fault above failed all 131 photographs this way and the run
    reported success with nothing read. One unreadable image is ordinary; a run
    that cannot read anything is a fact somebody has to be told once.
    """
    monkeypatch.setattr(ocr, "_recognition_failure_reported", False, raising=False)
    monkeypatch.setattr(ocr, "_engine_is_gpu", False, raising=False)

    messages: list[str] = []
    monkeypatch.setattr(
        ocr.log, "warning",
        lambda template, *args: messages.append(str(template).format(*args)))

    def exploding(source, **kwargs):
        raise RuntimeError("Unknown C++ exception from OpenCV code")

    first = ocr.ocr_image(tmp_path / "photo.png", engine=exploding)
    second = ocr.ocr_image(tmp_path / "other.png", engine=exploding)

    assert first.empty and second.empty, "a failed read must not invent text"
    assert not first.engine_missing, (
        "the engine was there and failed; that is not 'OCR is not installed'")
    assert len(messages) == 1, (
        f"expected exactly one warning for the run, got {len(messages)}: "
        f"{messages}")
    assert "OpenCV" in messages[0], (
        "the warning must name what actually went wrong")


def test_a_probe_that_faults_never_becomes_no_text(monkeypatch, tmp_path):
    """A detection probe that dies natively must not be read as "checked, no
    text" - that is a claim about the photograph, and it would put it in the
    bucket the OCR pass never looks at again.

    This is the silent half of the same crash: 131 photographs recorded as
    blank because OpenCV threw inside a corrupted DirectML detector.
    """
    monkeypatch.setattr(ocr, "_engine_is_gpu", False, raising=False)
    calls: list[dict] = []

    def half_broken(source, **kwargs):
        calls.append(kwargs)
        if kwargs.get("use_rec") is False:
            raise RuntimeError("Unknown C++ exception from OpenCV code")
        return [[None, "readable after all", 0.9]], 0.01

    result = ocr.ocr_image(tmp_path / "photo.png", engine=half_broken)

    assert not result.checked_no_text, (
        "a probe that could not run was recorded as 'no text found'")
    assert result.text == "readable after all", (
        "the full recognition pass must still run when the probe fails")
    assert len(calls) == 2, "expected a probe and then a full pass"
