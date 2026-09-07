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
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Iterator

__all__ = ["gpu_exclusive"]

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
        yield
