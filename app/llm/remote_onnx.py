r"""The chat model, as the window sees it: the same methods, the model in another process.

Layer: L1 (`app/llm`; reads nothing from the UI, starts one child process).

`RemoteOnnxLLM` has the methods of `app.ort.llm.OnnxLLM` - the ones the chat
engine, Interpret and Describe call - and forwards the ones that load or run
the model to the host process (`app/ort/llm_host.py`). Building an ONNX
Runtime session holds Python's global lock for the whole load (20.1 s measured
on a 1 GB model), which froze the window and made Windows label it "Not
responding"; here the load happens in the host and the window's lock is never
touched.

**Cheap questions never wait for the host.** The host's own lock is held by a
load in progress, so a question sent to it then would wait as long as the load
- and the caller may be the window. `has_model`, `available_models`,
`serving`, `serves`, `can_think`, `context_window`, `missing_error`, `health`
and `is_loaded` are answered here, from the disk and from what this proxy has
seen the host do. A private `OnnxLLM` that never loads anything does the disk
reads, so the answers are the ones the real class would give.

**The model named in the request.** `set_model` only records the choice here;
every request carries it and the host applies it first, so a pick and the
question that follows it can never arrive in the wrong order (requests run on
threads of their own there).

**A dead host costs the reply in flight.** The window gets `ERR_MODEL_HOST_ENDED`
and the next question starts a fresh host. Nothing here retries a reply: half
an answer repeated is worse than an honest error.
"""

from __future__ import annotations

import atexit
import contextlib
import itertools
import queue
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Optional, Sequence

from app.core import pipewire
from app.core.errors import AppErrorException, make_error
from app.core.logging import logger

log = logger.bind(component="llm.remote_onnx")

__all__ = ["ModelHost", "RemoteOnnxLLM", "START_LIMIT_S"]

#: Seconds the host may take to say it is ready (imports only; no model loads
#: before the first question).
START_LIMIT_S = 60.0
#: How often a waiting caller looks at its Stop.
POLL_S = 0.05
#: Longest `unload` waits for the host; it carries on there regardless.
UNLOAD_WAIT_S = 2.0

ENGINE = "onnx"


def _ended(details: str) -> AppErrorException:
    return AppErrorException(make_error("ERR_MODEL_HOST_ENDED", "llm.remote_onnx",
                                        details=details))


class ModelHost:
    """The one host process of this window: started on first use, restarted after a death."""

    def __init__(self, *, env_file: Optional[Path] = None, python: Optional[str] = None,
                 popen: Callable[..., Any] = subprocess.Popen,
                 factory: str = "") -> None:
        from app.core.osbridge.stdio import own_python

        self.env_file = Path(env_file) if env_file else None
        self.python = python or own_python()
        self.factory = factory
        self._popen = popen
        self._proc: Any = None
        self._ready: Optional[threading.Event] = None
        self._lock = threading.Lock()            # starting, and the tables below
        self._write_lock = threading.Lock()      # one writer on the pipe
        self._pending: dict[int, "queue.Queue[tuple]"] = {}
        self._instances: dict[int, tuple] = {}
        self._rids = itertools.count(1)
        self._iids = itertools.count(1)
        #: Hosts started - more than one means one died.
        self.started = 0
        atexit.register(self.close)

    # -- life ----------------------------------------------------------------

    @property
    def alive(self) -> bool:
        proc = self._proc
        return proc is not None and proc.poll() is None

    def _argv(self) -> list[str]:
        argv = [self.python, "-m", "app.ort.llm_host"]
        if self.env_file is not None:
            argv += ["--env", str(self.env_file)]
        return argv

    def ensure(self) -> None:
        """Start the host if there is none, and wait until it is ready. Raises
        `ERR_MODEL_HOST_ENDED` if it cannot be."""
        with self._lock:
            if not self.alive:
                here = Path(__file__).resolve().parents[2]
                proc = self._proc = self._popen(
                    self._argv(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL, cwd=str(here), close_fds=True)
                ready = self._ready = threading.Event()
                self.started += 1
                threading.Thread(target=self._listen, args=(proc, ready),
                                 name="llm-host-listen", daemon=True).start()
                for iid, spec in self._instances.items():
                    self._send_new(iid, spec)
            proc, ready = self._proc, self._ready
        if ready is None or not ready.wait(START_LIMIT_S) or not self.alive:
            self._kill(proc)
            raise _ended("the chat model process did not start")

    def close(self) -> None:
        """Ask the host to leave, and end it if it does not. Never raises."""
        with self._lock:
            proc, self._proc, self._ready = self._proc, None, None
        if proc is None:
            return
        try:
            with self._write_lock:
                pipewire.write(proc.stdin, ("quit",))
            proc.stdin.close()
        except Exception:
            pass
        try:
            proc.wait(timeout=3.0)
        except Exception:
            self._kill(proc)

    @staticmethod
    def _kill(proc: Any) -> None:
        for step in (lambda: proc.kill(), lambda: proc.wait(timeout=3.0)):
            try:
                step()
            except Exception:
                pass

    def kill(self) -> None:
        """End the host now (tests, and a stuck reply). The next call starts another."""
        self._kill(self._proc)

    def _listen(self, proc: Any, ready: threading.Event) -> None:
        """The host's frames, to whoever is waiting for them. On a closed pipe,
        everything still waiting is told the host is gone."""
        try:
            first = pipewire.read(proc.stdout)
        except Exception:
            first = None
        if first and first[0] == "ready":
            ready.set()
            while True:
                try:
                    message = pipewire.read(proc.stdout)
                except Exception:
                    message = None
                if message is None:
                    break
                waiting = self._pending.get(message[1]) if len(message) > 1 else None
                if waiting is not None:
                    waiting.put(message)
        code = None
        try:
            code = proc.wait(timeout=2.0)
        except Exception:
            pass
        with self._lock:
            lost = list(self._pending.values())
            if self._proc is proc:
                self._proc = None
        if lost:
            log.warning("the chat model process ended (exit code {}) with {} request(s) in flight",
                        code, len(lost))
        for waiting in lost:
            waiting.put(("lost", 0, code))
        ready.set()                              # so a starter does not wait out the limit

    # -- clients and requests --------------------------------------------------

    def register(self, spec: tuple) -> int:
        """Make a client here; `spec` is `(cache_dir, model, device, timeout, keep_resident)`.
        Sent to the host now when it runs, and replayed whenever one starts."""
        with self._lock:
            iid = next(self._iids)
            self._instances[iid] = spec
            running = self.alive
        if running:
            self._send_new(iid, spec)
        return iid

    def update(self, iid: int, spec: tuple) -> None:
        with self._lock:
            self._instances[iid] = spec

    def _send_new(self, iid: int, spec: tuple) -> None:
        cache_dir, model, device, timeout, keep = spec
        self.send(("new", iid, self.factory, str(cache_dir) if cache_dir else "", model,
                   device, timeout, keep))

    def send(self, message: tuple) -> None:
        proc = self._proc
        if proc is None:
            raise _ended("the chat model process is not running")
        try:
            with self._write_lock:
                pipewire.write(proc.stdin, message)
        except (OSError, ValueError) as exc:
            raise _ended(f"the chat model process closed ({type(exc).__name__})") from exc

    def request(self, kind: str, iid: int, method: str, args: tuple, kwargs: dict,
                has_stop: bool, model: str) -> tuple[int, "queue.Queue[tuple]"]:
        """Send one call or stream; the frames that answer it arrive on the queue."""
        self.ensure()
        rid = next(self._rids)
        answers: "queue.Queue[tuple]" = queue.Queue()
        with self._lock:
            self._pending[rid] = answers
        try:
            self.send((kind, rid, iid, method, args, dict(kwargs), has_stop, model))
        except AppErrorException:
            self.forget(rid)
            raise
        return rid, answers

    def stop(self, rid: int) -> None:
        with contextlib.suppress(AppErrorException):
            self.send(("stop", rid))

    def forget(self, rid: int) -> None:
        with self._lock:
            self._pending.pop(rid, None)


class RemoteOnnxLLM:
    """`OnnxLLM`'s methods, with the model in the host process."""

    engine = ENGINE
    #: Read by code written for `OllamaClient` (`vision_caption.unavailable_reason`).
    url = "(inside Leasha)"

    def __init__(self, host: ModelHost, cache_dir: Optional[Path], model: str = "", *,
                 device: str = "auto", timeout: float = 120.0) -> None:
        from app.ort import hub
        from app.ort.llm import OnnxLLM

        self._host = host
        #: The model a caller actually named - empty when it named none, so the
        #: catalogue's ranking decides, exactly as `OnnxLLM._asked`.
        self._named = model if model and hub.by_key(model) is not None else ""
        # Never loads anything here: it answers the questions that only read the disk.
        self._local = OnnxLLM(cache_dir, model, device=device, timeout=timeout)
        self.cache_dir = self._local.cache_dir
        self.device = device
        self.timeout = float(timeout)
        self._keep = False
        self._loaded = False
        self._failed = False
        self._iid = host.register(self._spec())

    def _spec(self) -> tuple:
        return (self.cache_dir, self._named, self.device, self.timeout, self._keep)

    # -- which model -------------------------------------------------------------

    @property
    def model(self) -> str:
        return self._local.model

    @model.setter
    def model(self, name: str) -> None:
        self.set_model(name)

    def set_model(self, name: str) -> None:
        """Record the choice; every request carries it, and the host applies it first."""
        before = self._local.model
        self._local.set_model(name)
        if self._local.model != before:
            self._named = self._local.model
            self._loaded = False
            self._failed = False
            self._host.update(self._iid, self._spec())

    @property
    def keep_resident(self) -> bool:
        return self._keep

    @keep_resident.setter
    def keep_resident(self, value: bool) -> None:
        self._keep = bool(value)
        self._host.update(self._iid, self._spec())
        if self._host.alive:
            with contextlib.suppress(AppErrorException):
                self._host.send(("set", self._iid, "keep_resident", self._keep))

    # -- the questions that only read the disk, answered here ----------------------

    def available_models(self) -> list[str]:
        return self._local.available_models()

    def has_model(self) -> bool:
        return self._local.has_model()

    def serving(self) -> str:
        return self._local.serving()

    def serves(self, key: str) -> bool:
        return self._local.serves(key)

    def can_think(self) -> bool:
        return self._local.can_think()

    def context_window(self) -> int:
        return self._local.context_window()

    def missing_error(self) -> AppErrorException:
        return self._local.missing_error()

    def health(self, *, force: bool = False) -> bool:
        """Up means "can answer": downloaded, and the host did not fail to load it."""
        return self.has_model() and not self._failed

    def is_loaded(self) -> bool:
        """What this proxy has seen the host do: a reply or a warm-up succeeded."""
        return self._loaded and self._host.alive

    # -- what runs in the host -------------------------------------------------------

    def _wait(self, rid: int, answers: "queue.Queue[tuple]", should_stop: Optional[Callable[[], bool]],
              limit: Optional[float] = None) -> Iterator[tuple]:
        """The frames that answer `rid`, until its last. Sends Stop once when asked to;
        raises the error the host sent, or `ERR_MODEL_HOST_ENDED` if it went."""
        deadline = None if limit is None else time.monotonic() + limit
        stopped = False
        finished = False
        try:
            while True:
                # Looked at on every pass, not only when nothing arrives: a stream
                # that is producing text never goes quiet long enough to be asked.
                if should_stop is not None and not stopped and should_stop():
                    self._host.stop(rid)
                    stopped = True
                try:
                    item = answers.get(timeout=POLL_S)
                except queue.Empty:
                    if deadline is not None and time.monotonic() > deadline:
                        return
                    continue
                kind = item[0]
                if kind == "lost":
                    finished = True
                    self._loaded = False
                    raise _ended(f"the chat model process ended (exit code {item[2]})")
                if kind == "error":
                    finished = True
                    error = item[2]
                    if getattr(error, "code", "") == "ERR_LOCAL_MODEL_FAILED":
                        self._failed = True
                    raise AppErrorException(error)
                if kind in ("result", "end"):
                    finished = True
                yield item
                if finished:
                    return
        finally:
            if not finished:
                self._host.stop(rid)             # abandoned or timed out: stop the reply
            self._host.forget(rid)

    def _call(self, method: str, args: tuple = (), kwargs: Optional[dict] = None, *,
              should_stop: Optional[Callable[[], bool]] = None) -> Any:
        rid, answers = self._host.request("call", self._iid, method, args, kwargs or {},
                                          should_stop is not None, self._named)
        for item in self._wait(rid, answers, should_stop):
            if item[0] == "result":
                self._loaded = True
                self._failed = False
                return item[2]
        raise _ended("the chat model process gave no answer")

    def _stream(self, method: str, args: tuple, kwargs: dict,
                should_stop: Optional[Callable[[], bool]]) -> Iterator[str]:
        rid, answers = self._host.request("stream", self._iid, method, args, kwargs,
                                          should_stop is not None, self._named)
        for item in self._wait(rid, answers, should_stop):
            if item[0] == "piece":
                self._loaded = True
                self._failed = False
                yield item[2]

    def warm(self, **_kwargs: Any) -> bool:
        """Load the model ahead of the first question. False when it cannot load."""
        try:
            return bool(self._call("warm"))
        except AppErrorException:
            return False

    def unload(self) -> bool:
        """Let the model go. Nothing to do when no host is running. Waits briefly;
        the host finishes the unload on its own after a reply in progress."""
        if not self._host.alive:
            self._loaded = False
            return True
        try:
            rid, answers = self._host.request("call", self._iid, "unload", (), {}, False, self._named)
        except AppErrorException:
            return True
        for item in self._wait(rid, answers, None, limit=UNLOAD_WAIT_S):
            if item[0] == "result":
                self._loaded = False
                return bool(item[2])
        return False

    def drop_idle_prefixes(self, now: Optional[float] = None) -> bool:
        return False                             # the host's own timer does this

    def stream(self, prompt: str, *, should_stop: Optional[Callable[[], bool]] = None,
               **kwargs: Any) -> Iterator[str]:
        yield from self._stream("stream", (prompt,), kwargs, should_stop)

    def chat_stream(self, messages: Sequence[Mapping[str, str]], *,
                    should_stop: Optional[Callable[[], bool]] = None,
                    **kwargs: Any) -> Iterator[str]:
        yield from self._stream("chat_stream", ([dict(m) for m in messages],), kwargs, should_stop)

    def generate(self, prompt: str, *, should_stop: Optional[Callable[[], bool]] = None,
                 **kwargs: Any) -> Any:
        return self._call("generate", (prompt,), kwargs, should_stop=should_stop)

    def chat(self, messages: Sequence[Mapping[str, str]], *,
             should_stop: Optional[Callable[[], bool]] = None, **kwargs: Any) -> Any:
        return self._call("chat", ([dict(m) for m in messages],), kwargs, should_stop=should_stop)
