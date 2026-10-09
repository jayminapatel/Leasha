r"""The chat model in a process of its own.

Layer: L2 (`app/ort`), beside `llm.py`, whose class it hosts.

**Why.** Building an ONNX Runtime session holds Python's global lock for the
whole load: measured 2026-10-09, a ticker on another thread stopped for 20.1 s
while a 1 GB chat model loaded on a background thread, and the window's own log
shows a 14.2 s stall at the moment the chat model loaded. No thread can work
around that in the window's process, so the window froze whenever any chat
model loaded - and Windows labelled it "Not responding". The model now loads
and runs here, in a child, and the window talks to it through a proxy with the
same methods (`app/llm/remote_onnx.py`). A load can take as long as it likes:
the window's own lock is untouched.

**What runs here.** The real `OnnxLLM`, unchanged, one instance per client the
window made (`engines._shared_onnx` makes one for the model Settings chose and
one for each model picked by name), so the one-at-a-time lock, the kept prompt
starts and the `MAX_RESIDENT` limit all keep their meaning: they are
per-process, and this is the one process that holds a model.

**The protocol** (frames as `app/core/pipewire.py`):

    window -> host    ("new", iid, factory, cache_dir, model, device, timeout, keep_resident)
                      ("set", iid, name, value)            only `keep_resident`
                      ("call", rid, iid, method, args, kwargs, has_stop, model)
                      ("stream", rid, iid, method, args, kwargs, has_stop, model)
                      ("stop", rid)                        the reply's Stop
                      ("quit",)
    host -> window    ("ready", pid)                       once, imports done
                      ("result", rid, value)               a call's answer
                      ("piece", rid, text)                 a stream's next text
                      ("end", rid)                         the stream is finished
                      ("error", rid, AppError)             the call or stream failed

A `should_stop` callable cannot cross a pipe. The window sends `("stop", rid)`
and the request's thread here answers `True` from its own `should_stop`.
Every request runs on a thread of its own, so Interpret, Chat and Describe
may all be in flight, as they were in one process.

**A fault in the model library** ends this process and costs the reply in
flight (the window says so and starts a fresh host for the next question),
not the window. It has its own crash file, `logs/crash/model-host-crash.log`.
"""

from __future__ import annotations

import contextlib
import importlib
import os
import sys
import threading
from pathlib import Path
from typing import Any, Optional

from app.core import pipewire
from app.core.errors import AppErrorException, make_error
from app.core.logging import logger

log = logger.bind(component="ort.llm_host")

__all__ = ["CALLS", "FACTORY", "STREAMS", "main", "serve"]

#: What builds a client, as `module:attribute`. A test names a fake here.
FACTORY = "app.ort.llm:OnnxLLM"

#: The methods the window may call, and which of them stream. Nothing else is
#: forwarded: the proxy is the only caller, and an unknown name is refused.
CALLS = frozenset({
    "has_model", "health", "serving", "serves", "available_models", "can_think",
    "context_window", "is_loaded", "unload", "drop_idle_prefixes", "warm",
    "set_model", "generate", "chat",
})
STREAMS = frozenset({"stream", "chat_stream"})
#: The attributes the window may set.
SETTABLE = frozenset({"keep_resident"})


def _build(factory: str) -> Any:
    module, _, attribute = (factory or FACTORY).partition(":")
    return getattr(importlib.import_module(module), attribute)


def _failure(exc: BaseException) -> Any:
    """The `AppError` to send for whatever went wrong here."""
    if isinstance(exc, AppErrorException):
        return exc.error
    return make_error(
        "ERR_LOCAL_MODEL_FAILED", "ort.llm_host",
        details=f"the chat model process failed: {type(exc).__name__}: {exc}")


def serve(inbound: Any, outbound: Any) -> None:
    """The loop: say "ready", then answer requests until "quit" or the pipe closes."""
    lock = threading.Lock()                 # one writer at a time on the pipe
    clients: dict[int, Any] = {}
    stops: dict[int, threading.Event] = {}

    def send(message: Any) -> None:
        with lock:
            pipewire.write(outbound, message)

    def client_of(iid: int) -> Any:
        found = clients.get(iid)
        if found is None:
            raise AppErrorException(make_error(
                "ERR_LOCAL_MODEL_FAILED", "ort.llm_host",
                details=f"the chat model process has no client number {iid}"))
        return found

    def keyword(kwargs: dict, rid: int, has_stop: bool) -> dict:
        given = dict(kwargs or {})
        if has_stop:
            event = stops.setdefault(rid, threading.Event())
            given["should_stop"] = event.is_set
        return given

    def run_call(rid: int, iid: int, method: str, args: tuple, kwargs: dict,
                 has_stop: bool, model: str) -> None:
        try:
            if method not in CALLS:
                raise AppErrorException(make_error(
                    "ERR_LOCAL_MODEL_FAILED", "ort.llm_host",
                    details=f"the chat model process does not run '{method}'"))
            client = client_of(iid)
            if model:
                client.set_model(model)          # the choice travels with the request
            value = getattr(client, method)(*args, **keyword(kwargs, rid, has_stop))
            send(("result", rid, value))
        except Exception as exc:                           # sent to the window as an error
            with contextlib.suppress(Exception):
                send(("error", rid, _failure(exc)))
        finally:
            stops.pop(rid, None)

    def run_stream(rid: int, iid: int, method: str, args: tuple, kwargs: dict,
                   has_stop: bool, model: str) -> None:
        try:
            if method not in STREAMS:
                raise AppErrorException(make_error(
                    "ERR_LOCAL_MODEL_FAILED", "ort.llm_host",
                    details=f"the chat model process does not stream '{method}'"))
            client = client_of(iid)
            if model:
                client.set_model(model)
            for piece in getattr(client, method)(*args, **keyword(kwargs, rid, has_stop)):
                send(("piece", rid, piece))
            send(("end", rid))
        except Exception as exc:                           # sent to the window as an error
            with contextlib.suppress(Exception):
                send(("error", rid, _failure(exc)))
        finally:
            stops.pop(rid, None)

    def start(target: Any, *parts: Any) -> None:
        threading.Thread(target=target, args=parts, daemon=True,
                         name=f"llm-host-{parts[0]}").start()

    send(("ready", os.getpid()))
    while True:
        request = pipewire.read(inbound)
        if request is None or request[0] == "quit":
            return
        kind = request[0]
        if kind == "new":
            _, iid, factory, cache_dir, model, device, timeout, keep = request
            try:
                client = _build(factory)(Path(cache_dir) if cache_dir else None, model,
                                         device=device, timeout=float(timeout or 120.0))
                client.keep_resident = bool(keep)
                clients[iid] = client
            except Exception as exc:                       # reported when first used
                log.warning("could not build client {}: {}", iid, exc)
        elif kind == "set":
            _, iid, name, value = request
            if name in SETTABLE and iid in clients:
                setattr(clients[iid], name, value)
        elif kind == "call":
            _, rid, iid, method, args, kwargs, has_stop, model = request
            if has_stop:
                stops[rid] = threading.Event()
            start(run_call, rid, iid, method, tuple(args), kwargs, bool(has_stop), model)
        elif kind == "stream":
            _, rid, iid, method, args, kwargs, has_stop, model = request
            if has_stop:
                stops[rid] = threading.Event()
            start(run_stream, rid, iid, method, tuple(args), kwargs, bool(has_stop), model)
        elif kind == "stop":
            event = stops.get(request[1])
            if event is not None:
                event.set()


def _warm() -> None:
    """Import the native libraries here, on the main thread, before "ready".

    The same trap as the picture-text helper: a library's first import on a
    worker thread, while the main thread blocks reading the pipe, hung for ever
    inside numpy's C extension (2026-10-09, `faulthandler`). Never raises."""
    for name in ("numpy", "tokenizers", "onnxruntime"):
        with contextlib.suppress(Exception):               # absence is the load's to report
            importlib.import_module(name)
    with contextlib.suppress(Exception):
        importlib.import_module("app.ort.llm")


def main(argv: Optional[list[str]] = None) -> int:
    """The host process: serve the window until it says quit or goes away."""
    argv = list(sys.argv[1:] if argv is None else argv)
    outbound = os.fdopen(os.dup(sys.stdout.fileno()), "wb")
    try:
        os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    except (OSError, AttributeError, ValueError):
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
    sys.stdout = sys.stderr if sys.stderr is not None else Path(os.devnull).open("w")  # noqa: SIM115

    env_file: Optional[Path] = None
    if "--env" in argv and argv.index("--env") + 1 < len(argv):
        env_file = Path(argv[argv.index("--env") + 1])
    with contextlib.suppress(Exception):                   # a diagnostic, not the job
        from app.core.config import log_dir_for
        from app.core.crash_guard import catch_native_crashes

        catch_native_crashes(log_dir_for(env_file), "model-host")

    _warm()
    try:
        serve(sys.stdin.buffer, outbound)
    except (BrokenPipeError, OSError):
        return 0                                           # the window went first
    return 0


if __name__ == "__main__":
    sys.exit(main())
