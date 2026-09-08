r"""`app.core.gpu_serialize`: the one gate the three GPU consumers share.

This is the primitive, tested on its own with invented callers - `threading`
and a shared counter, not a mock that only asserts a mock was called. The
wiring into `embedder.py`/`ocr.py`/`rerank.py` is proved separately in
`test_gpu_lock_wiring.py`, because a passing test here says the gate itself
works and says nothing about whether any of the three actually reach for it.
"""

from __future__ import annotations

import threading
import time

import pytest

from app.core import gpu_serialize
from app.core.gpu_serialize import gpu_exclusive, is_transient_gpu_error


class _FakeLock:
    """Records every acquire/release, since a real `threading.Lock` instance
    accepts no monkeypatched attributes - it is a C type with no `__dict__`."""

    def __init__(self) -> None:
        self.acquires = 0
        self._real = threading.Lock()

    def __enter__(self):
        self.acquires += 1
        self._real.acquire()
        return self

    def __exit__(self, *exc):
        self._real.release()
        return False


# --- 1: no two GPU-gated sections run at once --------------------------------


def test_two_gpu_holders_never_overlap() -> None:
    """The crash this file exists to prevent: two onnxruntime/DirectML
    sections active on the graphics card at the same moment. Four threads,
    real `threading.Thread`s, a shared counter - not a mock."""
    concurrent = 0
    peak = 0
    guard = threading.Lock()

    def worker() -> None:
        nonlocal concurrent, peak
        with gpu_exclusive(True):
            with guard:
                concurrent += 1
                peak = max(peak, concurrent)
            time.sleep(0.05)
            with guard:
                concurrent -= 1

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert peak == 1, "two gpu_exclusive(True) sections ran concurrently"


# --- 2: the CPU path never touches the lock ----------------------------------


def test_the_cpu_path_never_acquires_the_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    """`gpu_exclusive(False)` must be a pure no-op - no acquire, no release."""
    fake = _FakeLock()
    monkeypatch.setattr(gpu_serialize, "_GPU_LOCK", fake)

    with gpu_exclusive(False):
        pass

    assert fake.acquires == 0, "the CPU path must never touch the GPU gate"


def test_the_gpu_path_does_acquire_the_same_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    """The other half of the same proof: `active=True` does use `_GPU_LOCK`,
    so the CPU-path test above is not passing by accident."""
    fake = _FakeLock()
    monkeypatch.setattr(gpu_serialize, "_GPU_LOCK", fake)

    with gpu_exclusive(True):
        pass

    assert fake.acquires == 1


def test_two_cpu_paths_actually_overlap() -> None:
    """The honest, unmockable half: two `gpu_exclusive(False)` callers must be
    able to be inside the guard **at the same time** - proof there is no
    serialisation at all on the path that must stay fast."""
    barrier = threading.Barrier(2, timeout=2.0)
    reached: list[str] = []

    def worker(label: str) -> None:
        with gpu_exclusive(False):
            barrier.wait()          # only passes if both threads are inside together
            reached.append(label)

    threads = [threading.Thread(target=worker, args=(label,))
               for label in ("a", "b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(reached) == ["a", "b"]


# --- 3: a broken construction still releases the lock (H4) ------------------


def test_a_broken_section_still_releases_the_lock() -> None:
    """`with`, not manual acquire/release, so this is automatic - proved
    anyway, because H4 says a broken model must degrade, never hang, and a
    lock left held by a raised exception would hang every later caller."""
    with pytest.raises(RuntimeError):
        with gpu_exclusive(True):
            raise RuntimeError("construction failed")

    # If the lock were still held, this would block forever - so bound it and
    # fail loudly rather than hang the suite if this regresses.
    acquired = gpu_serialize._GPU_LOCK.acquire(timeout=2.0)
    try:
        assert acquired, "the lock was left held after an exception escaped it"
    finally:
        if acquired:
            gpu_serialize._GPU_LOCK.release()


def test_a_broken_section_does_not_block_a_second_caller() -> None:
    """The same guarantee, proved with a second thread rather than a direct
    acquire - closer to how a real second subsystem would discover it."""
    released = threading.Event()

    def first() -> None:
        try:
            with gpu_exclusive(True):
                raise RuntimeError("boom")
        except RuntimeError:
            pass
        finally:
            released.set()

    t1 = threading.Thread(target=first)
    t1.start()
    t1.join()
    assert released.is_set()

    entered = threading.Event()

    def second() -> None:
        with gpu_exclusive(True):
            entered.set()

    t2 = threading.Thread(target=second)
    t2.start()
    t2.join(timeout=2.0)
    assert entered.is_set(), "the lock stayed held after the first caller raised"


# --- 4: is_transient_gpu_error - the real crash text, and unrelated ones ----
#
# `logs/runs/run-20260908-050751-window.log` (line 121-123) is the real crash
# this classifier exists to recognise: a DXGI device-removed event surfacing
# through onnxruntime's DirectML provider as a plain `Fail`, no structured
# exception type - so this is exercised against the actual wording rather
# than an invented one.


def test_the_real_device_removed_error_is_recognised() -> None:
    exc = RuntimeError(
        "Fail: [ONNXRuntimeError] : 1 : FAIL : Non-zero status code returned "
        "while running ... DmlExecutionProvider ... 887A0005 The GPU device "
        "instance has been suspended. Use GetDeviceRemovedReason to "
        "determine the appropriate action."
    )
    assert is_transient_gpu_error(exc)


@pytest.mark.parametrize("text", [
    "DXGI_ERROR_DEVICE_HUNG",
    "dxgi_error_device_reset",
    "887a0006",
    "887A0007",
    "some prefix then 887a0005 then a suffix",
])
def test_each_documented_marker_is_recognised(text: str) -> None:
    assert is_transient_gpu_error(RuntimeError(text))


@pytest.mark.parametrize("exc", [
    ValueError("bad input"),
    FileNotFoundError("model.onnx not found"),
    RuntimeError("unsupported model format"),
    ImportError("No module named 'fastembed'"),
])
def test_unrelated_exceptions_are_not_classified_as_transient(exc: BaseException) -> None:
    assert not is_transient_gpu_error(exc)


def test_classification_never_raises_on_an_odd_exception() -> None:
    """A best-effort STRING classification, never a gate that can itself
    take anything down - an exception whose `str()` misbehaves must still
    resolve to a plain boolean."""
    class Odd(Exception):
        def __str__(self) -> str:
            raise RuntimeError("str() itself is broken")

    # `is_transient_gpu_error` builds its text from `type(exc).__name__` and
    # `exc` via an f-string, which calls `str(exc)` - so a broken `__str__`
    # is exactly the edge this test is checking does not escape upward.
    try:
        is_transient_gpu_error(Odd())
    except RuntimeError:
        pytest.fail("is_transient_gpu_error must not let an exception's own "
                    "__str__ escape - callers rely on this never raising")
