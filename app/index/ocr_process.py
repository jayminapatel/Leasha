r"""The text-in-pictures model in a process of its own.

Layer: L3 (`app/index`), beside `read_process.py`, whose pipe protocol it shares.

Order `pictures-process-isolation` (2026-10-09). The index process died at 12:25
that day inside ONNX Runtime's DirectML path on reader thread 39460, OCR-ing a
picture; order 1e had put the *reading libraries* in a child and left the OCR
model in the parent, because the model, its memory and the process-local
graphics-card gate belonged there. A native fault in the model cannot be
caught; it can only be kept in a process of its own, so that a fault costs the
picture and the run goes on. This module is that process.

**One helper for the index process (D1).** OCR was already one-at-a-time on the
graphics card behind `gpu_serialize.gpu_exclusive`, so one helper loses no
concurrency there; on the processor the helper runs `HELPER_THREADS` pictures
side by side, as the reader threads did in the parent, so nothing is lost
there either. Four helpers would hold four engines and four graphics-card
sessions - the very thing `gpu_serialize` exists to prevent.

**One switch (D2).** `INDEX_READ_PROCESSES` ("Read files in separate
processes") covers the readers and this helper: one control meaning "a fault
never ends the run". The pipeline installs the helper as `ocr.set_engine_process`
for the run and clears it after; a reader child installs a *relay* to the
parent under the same seam (`read_process._serve`), so a scanned page it
renders is read by this helper too, and no process but this one ever loads
the engine during a run.

**The protocol.** Pickled frames, length-prefixed, the same `_read`/`_write` as
the reader process. Parent to child: `("ocr", sequence, payload)` where the
payload is a path string or the picture's bytes, and `("quit",)`. Child to
parent: `("ready", pid)` once, then `("result", sequence, OcrResult, device)`
in whatever order pictures finish - the parent matches by sequence, so several
threads may have a picture in flight at once. `device` is what the engine
loaded on ("gpu", "cpu", or "" while it has not), so the parent's run log can
say what ran rather than what was asked for (`backends.record_provider`).

**What a death costs.** The parent's reader thread sees the pipe close and
fails every picture in flight with `ERR_OCR_PROCESS_ENDED`; the next picture
starts a fresh helper. A picture that takes longer than `REQUEST_LIMIT_S` is
treated the same way, with the helper ended, so a hung model cannot hold a
reader thread for ever. The helper has its own crash file
(`logs/crash/ocr-crash.log`, `app.core.crash_guard`).
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from app.core.errors import make_error
from app.core.logging import logger
from app.index.read_process import _CLOSE_WAIT_S, START_LIMIT_S, _read, _write

log = logger.bind(component="index.ocr_process")

__all__ = ["HELPER_THREADS", "REQUEST_LIMIT_S", "OcrProcess", "main"]

#: Seconds one picture may take in the helper before it is given up on and the
#: helper ended. OCR runs at seconds a page; ten minutes is a hung model, not
#: a slow one.
REQUEST_LIMIT_S = 600.0
#: Pictures the helper reads side by side on the processor - what four reader
#: threads did in the parent. On the graphics card its own gate serialises them.
HELPER_THREADS = 4


class _Pending:
    __slots__ = ("done", "name", "result")

    def __init__(self, name: str) -> None:
        self.done = threading.Event()
        self.result: Any = None
        self.name = name


class OcrProcess:
    """The parent's handle on the helper: start it, ask it, end it."""

    def __init__(self, *, low_priority: bool = True, python: str | None = None,
                 popen: Callable[..., Any] = subprocess.Popen,
                 env_file: Path | None = None) -> None:
        self.low_priority = bool(low_priority)
        from app.core.osbridge.stdio import own_python

        self.python = python or own_python()
        self.env_file = Path(env_file) if env_file else None
        self._popen = popen
        self._proc: Any = None
        self._ready: threading.Event | None = None
        self._ready_ok = False
        self._lock = threading.Lock()            # the pipe's writer, and `_pending`
        self._pending: dict[int, _Pending] = {}
        self._sequence = 0
        #: How many helpers this handle has started - more than one means one
        #: ended part-way (a fault, or a picture past its limit).
        self.started = 0
        #: What the helper's engine loaded on, once it has said: "gpu" or "cpu".
        self.engine_device = ""
        self._device_recorded = False

    # -- life ----------------------------------------------------------------

    def argv(self) -> list[str]:
        argv = [self.python, "-m", "app.index.ocr_process"]
        if self.low_priority:
            argv.append("--low-priority")
        if self.env_file is not None:
            argv += ["--env", str(self.env_file)]
        return argv

    @property
    def alive(self) -> bool:
        proc = self._proc
        return proc is not None and proc.poll() is None

    def start(self) -> None:
        """Launch the helper if there is none. Never waits for it."""
        if self.alive:
            return
        here = Path(__file__).resolve().parents[2]
        proc = self._proc = self._popen(
            self.argv(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, cwd=str(here), close_fds=True,
        )
        ready = self._ready = threading.Event()
        self._ready_ok = False
        self.started += 1
        threading.Thread(target=self._listen, args=(proc, ready),
                         name="ocr-process-listen", daemon=True).start()

    def wait_ready(self) -> bool:
        """Start a helper if there is none and wait until it says it is ready.
        False, with the helper ended, when it is not ready in `START_LIMIT_S`."""
        with self._lock:
            self.start()
            proc, ready = self._proc, self._ready
        if ready is not None and ready.wait(START_LIMIT_S) and self._ready_ok and proc.poll() is None:
            return True
        log.warning("the text-in-pictures helper was not ready in {:.0f}s; it was ended",
                    START_LIMIT_S)
        self._abandon(proc)
        return False

    def close(self) -> None:
        """Ask the helper to leave, and end it if it does not. Never raises."""
        with self._lock:
            proc, self._proc = self._proc, None
            self._ready = None
        if proc is None:
            return
        try:
            _write(proc.stdin, ("quit",))
            proc.stdin.close()
        except Exception:                              # - already gone
            pass
        try:
            proc.wait(timeout=_CLOSE_WAIT_S)
        except Exception:
            self._kill(proc)
        self._close_pipes(proc)

    def kill_child(self) -> None:
        """End the helper now, from any thread. Never raises, never waits."""
        proc = self._proc
        if proc is not None:
            with contextlib.suppress(Exception):       # already gone
                proc.kill()

    def _abandon(self, proc: Any) -> None:
        with self._lock:
            if self._proc is proc:
                self._proc = None
                self._ready = None
        if proc is not None:
            self._kill(proc)
            self._close_pipes(proc)

    @staticmethod
    def _kill(proc: Any) -> None:
        try:
            proc.kill()
            proc.wait(timeout=_CLOSE_WAIT_S)
        except Exception:
            pass

    @staticmethod
    def _close_pipes(proc: Any) -> None:
        for pipe in (getattr(proc, "stdin", None), getattr(proc, "stdout", None)):
            try:
                if pipe is not None:
                    pipe.close()
            except Exception:
                pass

    # -- one picture ----------------------------------------------------------

    def ocr(self, source: Any) -> Any:
        """The `OcrResult` for one picture (a path or its bytes). Never raises:
        a helper that dies or hangs on it answers with `error` set to
        `ERR_OCR_PROCESS_ENDED`, and the next picture gets a fresh helper."""
        from app.extract.ocr import OcrResult

        name = Path(source).name if isinstance(source, (str, Path)) else "a picture"
        payload = str(source) if isinstance(source, Path) else source
        if not self.wait_ready():
            return OcrResult(error=self._ended_error(name, "it was not ready in time"))
        with self._lock:
            proc = self._proc
            if proc is None:
                return OcrResult(error=self._ended_error(name, "it had already ended"))
            self._sequence += 1
            sequence = self._sequence
            pending = self._pending[sequence] = _Pending(name)
            try:
                _write(proc.stdin, ("ocr", sequence, payload))
            except (OSError, ValueError):
                self._pending.pop(sequence, None)
                self._abandon_locked(proc)
                return OcrResult(error=self._ended_error(name, "it ended before the picture was sent"))
        if not pending.done.wait(REQUEST_LIMIT_S):
            log.warning("the text-in-pictures helper took more than {:.0f}s on {}; it was ended",
                        REQUEST_LIMIT_S, name)
            self._abandon(proc)
            pending.done.wait(_CLOSE_WAIT_S)       # `_listen` fails it as the pipe closes
            with self._lock:
                self._pending.pop(sequence, None)
            return pending.result if pending.result is not None else OcrResult(
                error=self._ended_error(name, f"it took more than {REQUEST_LIMIT_S:.0f} seconds"))
        return pending.result

    def _abandon_locked(self, proc: Any) -> None:
        if self._proc is proc:
            self._proc = None
            self._ready = None
        self._kill(proc)
        self._close_pipes(proc)

    @staticmethod
    def _ended_error(name: str, how: str) -> Any:
        return make_error("ERR_OCR_PROCESS_ENDED", "index.ocr_process", path=name,
                          details=f"The text-in-pictures helper process ended: {how}.")

    def _listen(self, proc: Any, ready: threading.Event) -> None:
        """The helper's answers, on a thread of their own, matched by sequence.
        Ends when the pipe closes - the helper ended, or was ended - and fails
        every picture still in flight with `ERR_OCR_PROCESS_ENDED`."""
        try:
            first = _read(proc.stdout)
        except Exception:                              # - the pipe went
            first = None
        self._ready_ok = bool(first) and first[0] == "ready"
        ready.set()
        if self._ready_ok:
            while True:
                try:
                    message = _read(proc.stdout)
                except Exception:                      # - the pipe went
                    message = None
                if message is None:
                    break
                if message[0] == "result":
                    _, sequence, result, device = message
                    self._note_device(device)
                    with self._lock:
                        pending = self._pending.pop(sequence, None)
                    if pending is not None:
                        pending.result = result
                        pending.done.set()
        code = None
        with contextlib.suppress(Exception):
            code = proc.wait(timeout=_CLOSE_WAIT_S)
        with self._lock:
            lost = list(self._pending.values())
            self._pending.clear()
            if self._proc is proc:
                self._proc = None
                self._ready = None
        if lost:
            log.warning("the text-in-pictures helper ended (exit code {}) with {} picture(s) "
                        "in hand: {}", code, len(lost), ", ".join(p.name for p in lost))
        from app.extract.ocr import OcrResult

        for pending in lost:
            pending.result = OcrResult(error=self._ended_error(
                pending.name, f"it exited with code {code}"))
            pending.done.set()

    def _note_device(self, device: str) -> None:
        """What the engine ran on, said once in the log and the run log."""
        if not device or self._device_recorded:
            return
        self._device_recorded = True
        self.engine_device = device
        log.info("the text-in-pictures helper loaded its engine on the {}",
                 "graphics card" if device == "gpu" else "processor")
        try:
            from app.index import backends

            choice = backends.Choice(
                device=backends.GPU if device == "gpu" else backends.CPU,
                providers=backends.providers_for(backends.GPU if device == "gpu" else backends.CPU),
                why="in the text-in-pictures helper process")
            backends.record_provider("OCR", choice)
        except Exception:                              # - a record, not the run
            pass


# --- the child -----------------------------------------------------------------


def _serve(inbound: Any, outbound: Any) -> None:
    """The helper's loop: say "ready", then answer each `("ocr", ...)` with a
    `("result", ...)`, pictures side by side on a small pool. Returns on
    `("quit",)` or when the parent's pipe closes."""
    from concurrent.futures import ThreadPoolExecutor

    from app.extract import ocr

    lock = threading.Lock()

    def send(message: Any) -> None:
        with lock:
            _write(outbound, message)

    def one(sequence: int, payload: Any) -> None:
        try:
            source = Path(payload) if isinstance(payload, str) else payload
            result = ocr.ocr_image(source)
        except Exception as exc:                       # - `ocr_image` never raises; belt and braces
            result = ocr.OcrResult(error=make_error(
                "ERR_OCR_PROCESS_ENDED", "index.ocr_process",
                path=Path(payload).name if isinstance(payload, str) else "a picture",
                details=f"{type(exc).__name__}: {exc}"))
        device = ("gpu" if ocr._engine_is_gpu else "cpu") if ocr._engine is not None else ""
        with contextlib.suppress(Exception):           # the parent went
            send(("result", sequence, result, device))

    send(("ready", os.getpid()))
    pool = ThreadPoolExecutor(max_workers=HELPER_THREADS, thread_name_prefix="ocr")
    try:
        while True:
            request = _read(inbound)
            if request is None or request[0] == "quit":
                return
            if request[0] == "ocr":
                pool.submit(one, request[1], request[2])
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


def main(argv: list[str] | None = None) -> int:
    """The helper: read the pictures the parent sends until it says stop."""
    argv = list(sys.argv[1:] if argv is None else argv)
    outbound = os.fdopen(os.dup(sys.stdout.fileno()), "wb")
    try:
        os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    except (OSError, AttributeError, ValueError):
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
    sys.stdout = sys.stderr if sys.stderr is not None else Path(os.devnull).open("w")  # noqa: SIM115

    env_file: Path | None = None
    if "--env" in argv:
        at = argv.index("--env")
        if at + 1 < len(argv):
            env_file = Path(argv[at + 1])

    # A native fault here leaves the Python stack of its thread in
    # `logs/crash/ocr-crash.log`, as every Leasha process does now.
    try:
        from app.core.config import log_dir_for
        from app.core.crash_guard import catch_native_crashes

        catch_native_crashes(log_dir_for(env_file), "ocr")
    except Exception:                                  # - a diagnostic, not the job
        pass

    if "--low-priority" in argv:
        try:
            import psutil

            from app.core.osbridge.priority import lower_process_priority
            lower_process_priority(psutil)
        except Exception:                              # - politeness, not correctness
            pass

    # The processor the owner's choice for OCR names, as `app.cli` and the
    # window tell the engine (`_common._load`, `main.py`).
    try:
        from app.core.config import load_settings
        from app.core.model_devices import device_for
        from app.extract import ocr

        ocr.configure_device(device_for(load_settings(env_file), "ocr"))
    except Exception as exc:                           # - "auto" then
        log.debug("the helper could not read the OCR device setting: {}", exc)

    _size_engine()
    _warm()
    try:
        _serve(sys.stdin.buffer, outbound)
    except (BrokenPipeError, OSError):
        return 0                                       # the parent went first
    return 0


def _size_engine() -> None:
    """Tell OCR it is called from `HELPER_THREADS` threads at once. Before
    `_warm`, which is what loads the engine. Never raises.

    2026-10-10, work order model-sequencing item 1d. This helper calls its one
    engine from a pool of `HELPER_THREADS` threads, and an onnxruntime
    session's intra-op pool is shared by every call on it while each calling
    thread works inside its own call too - so the engine's thread count is
    sized for that (`envelope.picture_model_threads`'s `callers`), rather than
    four pictures each asking for a pool the size of the machine. The count
    itself comes from the envelope's Auto answers on this machine: a helper
    cannot see the run's resolved numbers without resolving a run of its own,
    which would test the hardware a second time.
    """
    try:
        from app.extract import ocr

        ocr.configure_callers(HELPER_THREADS)
    except Exception as exc:                           # - the library's own count then
        log.debug("the helper could not size the OCR engine's threads: {}", exc)


def _warm() -> None:
    """Load the engine and the libraries it leans on here, on the main thread,
    before "ready" is sent.

    **Measured, 2026-10-09:** a fresh child whose first picture arrived on a
    pool thread hung for ever inside the import of numpy's C extension
    (`faulthandler` stack: `create_module` of `_multiarray_umath`) while the
    main thread sat in a blocking read of the pipe; the same loop with numpy
    already imported answered in 5 s. So the first import of every native
    library happens here, and the engine with it - which also makes the first
    picture as fast as the hundredth. Never raises: a machine without the
    engine still gets a helper that answers `engine_missing`."""
    for name in ("numpy", "PIL.Image", "cv2"):
        with contextlib.suppress(Exception):           # absence is the engine's to report
            __import__(name)
    try:
        from app.extract import ocr

        ocr._load_engine()
    except Exception as exc:
        log.debug("the helper could not load the OCR engine up front: {}", exc)


if __name__ == "__main__":
    sys.exit(main())
