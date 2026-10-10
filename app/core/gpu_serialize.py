r"""One process-wide gate so two GPU sessions are never built or run at once.

Layer: L0 — read by the embedder, the reranker and OCR, the three independent
onnxruntime/DirectML consumers `app/index/backends.py` hands a `Choice` to.

**Why this exists.** `logs/crash/crash.log`'s entry for 2026-09-07 ~22:45 is a
real access-violation crash during indexing: the faulting thread was mid-`Run()`
inside the embedder's ONNX session while a second thread was concurrently
inside onnxruntime's `_create_inference_session`, constructing RapidOCR's
`text_cls` session — two independently-built onnxruntime/DirectML sessions on
the same graphics card at the same moment. `app/core/compute_profile.py` is
detection only and `app/index/backends.py::choose()` picks a device per
subsystem in isolation; nothing before this file coordinated *how many*
subsystems could be inside DirectML at once. Each of the three modules already
carries its own private `threading.Lock` (`embedder.py`'s `_lock`, `ocr.py`'s
`_engine_lock`, `rerank.py`'s `_lock`), but each only guards concurrent callers
*within that one subsystem* — a second, independent subsystem was never in
scope for any of them. This is the missing, broader guard.

**One lock for the whole process, not one per device.** This machine has one
graphics card; a second adapter would be a reason to widen this, not to add a
second ad-hoc lock somewhere else.

**Never held across a whole pipeline stage.** Only around the actual session
construction and the actual inference call — H4's rule that a broken or slow
model degrades to an error, never to a hang, would otherwise be broken by this
very fix: holding the lock across anything wider (a whole batch, a whole
extractor) would let one stuck GPU call block the other two subsystems'
*CPU* fallback paths too, which is the opposite of what this file is for.

**Skipped entirely on the CPU path.** CPU-provider onnxruntime sessions are
safe for concurrent independent use, and there is no reason to slow down a
CPU-only machine, or a machine where only one of the three ever resolves to
the graphics card, by serialising work that was never at risk.

**2026-09-08: also the process-wide "the graphics driver failed" latch.**
`is_transient_gpu_error` classifies a DirectML/DXGI driver event, and
`mark_gpu_unreliable` / `gpu_unreliable` remember that one happened so
`app/index/backends.py::choose()` sends every later session build in this
process to the processor. Both live here rather than in `backends.py` because
the three consumers already import this module for the gate, and the
classifier and the latch are read at the same moment the gate is.
"""

from __future__ import annotations

import os
import re
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import IO, Any, Iterator, Optional

__all__ = [
    "gpu_exclusive",
    "gpu_unreliable",
    "is_transient_gpu_error",
    "mark_gpu_unreliable",
]

#: Substrings that identify a DirectML/DXGI device-removed style event.
#: Lower-cased before comparison, so this list is written in whatever case
#: reads best.
#:
#: **What this exists to catch.** `logs/runs/run-20260908-050751-window.log`
#: (line 121-123): an index run's embedding call failed with
#: `[ONNXRuntimeError] : 1 : FAIL : ...DmlExecutionProvider... 887A0005 The
#: GPU device instance has been suspended. Use GetDeviceRemovedReason to
#: determine the appropriate action.` That HRESULT is `DXGI_ERROR_DEVICE_
#: REMOVED` - the graphics driver reset, or the device was briefly taken away
#: from every process using it (a driver crash-recover, a laptop waking from
#: sleep, a display switch) - not a corrupt model or a bad input. The same
#: log's line 95 shows this had already happened once before that run, more
#: softly, and line 99-100 shows OCR falling back to the processor moments
#: later because "no display adapter was detected", consistent with the
#: device having genuinely dropped out of the system at that point.
#:
#: `887a0006` (`DXGI_ERROR_DEVICE_HUNG`) and `887a0007` (`DXGI_ERROR_DEVICE_
#: RESET`) are included alongside the confirmed `887a0005` because they are
#: the same family of event reported through the same mechanism - a
#: reasonable, documented, **non-exhaustive** set, not a claim that these are
#: the only transient GPU failures that exist.
#:
#: **2026-09-08, later the same day: the hand-picked three were not enough.**
#: `logs/runs/run-20260908-055844-window.log` at 06:29:49: the embedder
#: failed with `...DmlExecutionProvider\src\DmlCommandRecorder.cpp(371)...
#: 887A0020 An internal issue prevented the driver from carrying out the
#: specified operation. The driver's state is probably suspect, and the
#: application should not continue.` That is `DXGI_ERROR_DRIVER_INTERNAL_
#: ERROR` - the same family as the three above, reported through the same
#: mechanism, and this list did not match it, so the recovery path built
#: that morning never fired and the whole index run ended thirty minutes in.
#: Every `887A00xx` HRESULT is the DXGI facility (`_FACDXGI = 0x87a`), and
#: every code in it that reaches an inference call is a driver or device
#: event rather than a model or data problem - so the whole facility is now
#: matched by `_DXGI_HRESULT` below rather than by a hand-picked few, and the
#: DirectML provider's own file names are markers too: a failure whose text
#: names `DmlCommandRecorder` came from the driver path, whatever the code.
_TRANSIENT_GPU_MARKERS: tuple[str, ...] = (
    "device instance has been suspended",
    "getdeviceremovedreason",
    "dxgi_error_device_removed",
    "dxgi_error_device_hung",
    "dxgi_error_device_reset",
    "dxgi_error_driver_internal_error",
    "driver_internal_error",
    "driver's state is probably suspect",
    "dmlexecutionprovider",
    "dmlcommandrecorder",
    "887a0005",
    "887a0006",
    "887a0007",
)

#: Any HRESULT in the DXGI facility: `0x887A00xx`. Matched case-insensitively
#: against the lower-cased text, so written in lower case here.
_DXGI_HRESULT = re.compile(r"887a00[0-9a-f]{2}")


def is_transient_gpu_error(exc: BaseException) -> bool:
    """True if `exc` looks like a DirectML/DXGI device-removed style event
    (device suspended, hung, reset, or removed) rather than a genuine model
    or data problem.

    See `_TRANSIENT_GPU_MARKERS` above for exactly what this matches on and
    the real crash (`logs/runs/run-20260908-050751-window.log`, line
    121-123) that it exists to distinguish from an ordinary model failure.

    **2026-09-08, later the same day:** also any `887A00xx` HRESULT - the
    whole DXGI facility, via `_DXGI_HRESULT` - and the DirectML provider's
    own source-file names and driver phrases, after `887A0020`
    (`logs/runs/run-20260908-055844-window.log` at 06:29:49) slipped past
    the hand-picked codes and ended an index run. One facility, not a list
    somebody has to keep extending one crash at a time.

    **This is a best-effort classification of an error STRING, not a
    structured exception type.** onnxruntime does not raise a distinct
    exception class for a DXGI device-removed event - it surfaces as a
    plain `Fail` with the HRESULT and driver text embedded in the message -
    so string matching is the only signal available here. A false negative
    (a transient GPU error whose wording this does not recognise) is
    therefore expected and must stay harmless: callers use this to choose
    better *guidance and recovery*, never as a correctness gate, so an
    unrecognised transient error simply falls back to whatever the generic,
    always-safe handling already was.
    """
    try:
        text = f"{type(exc).__name__}: {exc}".lower()
    except Exception:                            # noqa: BLE001 - a classifier must never itself crash
        # An exception whose own `__str__` raises is exactly the kind of
        # thing this function exists to survive - see the docstring above:
        # this is guidance, never a gate, so an unreadable exception simply
        # classifies as "not recognised" rather than escaping upward.
        return False
    if _DXGI_HRESULT.search(text):
        return True
    return any(marker in text for marker in _TRANSIENT_GPU_MARKERS)


#: Set once the graphics driver has failed mid-inference in this process,
#: and never cleared for the life of the process - see `mark_gpu_unreliable`.
#:
#: **A plain string, replaced whole, no lock.** Assigning a name to an
#: immutable object is a single bytecode under the GIL, so a reader on
#: another thread sees either the old string or the new one, never a torn
#: value; the only race is two subsystems both marking within the same
#: instant, and the outcome of that race is that one of two true reasons
#: wins, which is acceptable. A lock would guard against nothing here.
_GPU_UNRELIABLE_REASON: str = ""

# ---------------------------------------------------------------------------
# Across processes (order 1h section 1, 2026-10-10)
# ---------------------------------------------------------------------------
#
# **Why.** Since 2026-10-09 Leasha's graphics-card work runs in three processes:
# the indexer (meaning model, CLIP, faces), the OCR helper (`ocr_process.py`) and
# the vision host (Florence, the CLIP text tower). `_GPU_LOCK` and the latch above
# are per process, so two DirectML sessions could run at once again - the crash
# this file was written for - and a driver fault seen by the OCR helper left the
# indexer asking the same suspect driver. Both now reach every process of one
# Leasha session: the gate through a file lock (`osbridge.filelock`, released by
# the operating system if its holder dies), the latch through a small file.
#
# **Every hand-off to another process happens before the gate is taken**
# (`ocr.ocr_image` sends to the helper first; the window's Florence and CLIP calls
# go to the host, which takes the gate itself), so no process holds the file lock
# while it waits on another that needs it.

#: Inherited by every process the first one starts (the index child copies
#: `os.environ`; the helpers and hosts inherit it), so they share one latch file.
#: A Leasha started separately - a second command line - has its own.
SESSION_ENV = "LEASHA_GPU_SESSION"
#: Where the lock and latch files live. Tests point it at a folder of their own.
DIR_ENV = "LEASHA_GPU_LOCK_DIR"
#: How long a process waits for another's turn on the card before it goes on
#: without the cross-process gate (warned once). The longest single hold measured
#: is a Florence-2 session build, about 12 s; this is ten times that. Waiting for
#: ever would turn a stuck driver call in one process into a hang in all three.
MACHINE_WAIT_S = 120.0

os.environ.setdefault(SESSION_ENV, f"{os.getpid()}-{int(time.time())}")

_machine_handle: Optional[IO[Any]] = None
_machine_broken = False          # this file system cannot lock: thread lock only
_machine_warned = False


def _lock_dir() -> Path:
    return Path(os.environ.get(DIR_ENV) or tempfile.gettempdir())


def _latch_path() -> Path:
    session = re.sub(r"[^0-9A-Za-z_-]", "_", os.environ.get(SESSION_ENV, "none"))
    return _lock_dir() / f"leasha-gpu-unreliable-{session}.txt"


def _share_latch(reason: str) -> None:
    """Write the latch for the other processes of this session. Never raises."""
    try:
        path = _latch_path()
        if not path.exists():
            path.write_text(reason, encoding="utf-8")
        cutoff = time.time() - 2 * 86_400       # old sessions' files, tidied in passing
        for old in path.parent.glob("leasha-gpu-unreliable-*.txt"):
            if old != path and old.stat().st_mtime < cutoff:
                old.unlink(missing_ok=True)
    except OSError:
        pass


def _read_shared_latch() -> str:
    try:
        return _latch_path().read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def mark_gpu_unreliable(reason: str) -> None:
    """Record that the graphics driver failed this session, so every later
    `backends.choose()` lands on the processor.

    **Why a process-wide latch and not a per-subsystem retry.** The driver's
    own words in `logs/runs/run-20260908-055844-window.log` were "the
    driver's state is probably suspect, and the application should not
    continue" - and `backends.choose()` had no memory that anything had
    happened, so the embedder's rebuild would have asked for the same
    suspect driver again, and OCR and the reranker each would have had to
    learn the same lesson separately. One flag, read at the one seam every
    GPU consumer goes through, means the first subsystem to be hit moves
    the whole process to the processor, and nothing needs per-subsystem
    code to follow it.

    Sticky until the process ends: a driver that has said "do not continue"
    does not get a second chance from a stored setting, and searches, OCR
    and indexing all still work on the processor. The first reason recorded
    is kept; a later one does not overwrite it, because the first is the
    one that explains everything after it.
    """
    global _GPU_UNRELIABLE_REASON
    text = str(reason or "").strip() or "the graphics driver failed"
    if not _GPU_UNRELIABLE_REASON:
        _GPU_UNRELIABLE_REASON = text
        _share_latch(text)


def gpu_unreliable() -> str:
    """Why the graphics driver is not to be used again this session, or
    `""` while nothing has gone wrong.

    2026-10-10: also what another process of this session recorded - a fault
    the OCR helper saw moves the indexer and the vision host to the processor
    too. Read from the file only until this process has a reason of its own.
    """
    global _GPU_UNRELIABLE_REASON
    if not _GPU_UNRELIABLE_REASON:
        shared = _read_shared_latch()
        if shared:
            _GPU_UNRELIABLE_REASON = shared
    return _GPU_UNRELIABLE_REASON


def _reset_for_tests() -> None:
    """Clear the latch. Tests only - the process never clears it itself."""
    global _GPU_UNRELIABLE_REASON, _machine_handle, _machine_broken, _machine_warned
    _GPU_UNRELIABLE_REASON = ""
    try:
        _latch_path().unlink(missing_ok=True)
    except OSError:
        pass
    if _machine_handle is not None:
        try:
            _machine_handle.close()
        except OSError:
            pass
    _machine_handle = None
    _machine_broken = False
    _machine_warned = False

#: The one process-wide gate. A plain `Lock`, not an `RLock`: every call path
#: into this file was read before choosing — `embedder._ensure_encoder`,
#: `ocr._load_engine`, `rerank._ensure_scorer` and each subsystem's own
#: inference call — and none of them re-enters this lock from a thread that
#: already holds it, and none of the three ever calls into another while
#: holding it. An `RLock` would silently paper over a future call path doing
#: that by mistake; a plain `Lock` deadlocks instead, which is the honest
#: failure for a bug this file did not anticipate.
_GPU_LOCK = threading.Lock()


@contextmanager
def gpu_exclusive(active: bool) -> Iterator[None]:
    """Hold the process-wide graphics-card gate only when `active` is true.

    Wrap the actual session-construction call and the actual inference call
    with this — nothing wider — whenever the caller's own `backends.Choice`
    resolved to a non-CPU provider. When `active` is false (the processor was
    chosen, or the choice is not yet known) this is a no-op: nothing is
    acquired, and independent CPU sessions run exactly as concurrently as they
    always have.

        with gpu_exclusive(choice.is_gpu):
            model = build(choice.providers)

    This exists because two of the three GPU consumers building or running an
    onnxruntime/DirectML session **at the same moment** is exactly the
    access-violation confirmed in `logs/crash/crash.log` on 2026-09-07 — not
    unnecessary caution, a fix for a crash that already happened once.
    """
    if not active:
        yield
        return
    with _GPU_LOCK:
        held = _take_machine_lock()
        try:
            yield
        finally:
            if held is not None:
                from app.core.osbridge.filelock import unlock

                unlock(held)


def _machine_lock_handle() -> Optional[IO[Any]]:
    """The open lock file, opened once per process; `None` if it cannot be."""
    global _machine_handle, _machine_broken
    if _machine_handle is None and not _machine_broken:
        try:
            path = _lock_dir() / "leasha-gpu.lock"
            _machine_handle = open(path, "a+b")     # noqa: SIM115 - held for the process
        except OSError:
            _machine_broken = True
    return _machine_handle


def _take_machine_lock() -> Optional[IO[Any]]:
    """Wait for this computer's graphics-card turn; the handle to unlock, or `None`.

    Called with `_GPU_LOCK` held, so one thread of this process uses the handle at
    a time. `None` means "go on with the thread lock only", as before 2026-10-10:
    a file system that cannot lock, or another process holding the card for
    longer than `MACHINE_WAIT_S`.
    """
    global _machine_broken, _machine_warned
    handle = _machine_lock_handle()
    if handle is None:
        return None
    from app.core.osbridge.filelock import try_lock

    deadline = time.monotonic() + MACHINE_WAIT_S
    pause = 0.005
    while True:
        try:
            if try_lock(handle):
                return handle
        except OSError:
            _machine_broken = True
            return None
        if time.monotonic() >= deadline:
            if not _machine_warned:
                _machine_warned = True
                from app.core.logging import logger

                logger.warning(
                    "another Leasha process has held the graphics card for over {:.0f} s; "
                    "going on without waiting for it", MACHINE_WAIT_S)
            return None
        time.sleep(pause)
        pause = min(pause * 2, 0.05)
