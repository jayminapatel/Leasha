r"""The folder watch as a process of its own, supervised by the window.

Layer: L3

Work order 0z, item F1. The window does not watch folders or index what
changes itself: it starts `python -m app.cli watch --events jsonl` as a child
process and reads what it says. This module starts that child, passes its
events on, and ends it.

**Why a child, when the window could run a thread.** The same reasons the
index run got one (`app/index/child_run.py`, work order 0x §2): a process of
its own has its own interpreter lock, so reading and embedding a saved file
never makes the window's thread wait; the meaning model loads in the child,
not in the window; and a fault in a reader costs the watch, which is started
again, not the window. It also means the path the window uses is exactly the
command-line path (non-negotiable 8) - there is one way to watch, and it is
the one that can be run and tested with no window at all.

**The cost is a second process that stays up.** Measured figures are in the
work order's note for F1.

**Never an orphan**, on `child_run`'s three guards: closing the window ends
it (`end_all_watch_children`, from `MainWindow.closeEvent`); the child stops
by itself when its standard input ends, which is what the operating system
does to the pipe if the window dies; and a write to a pipe nobody reads fails,
which the child also takes as the end.

No Qt here. `WatchChild.start` returns at once - the process is started and
read on a thread of this module's own - and `on_event(kind, data)` is called
on that thread; `app/ui/folder_watch.py` turns it into a signal.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import weakref
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from app.core.logging import logger

__all__ = [
    "STDERR_NAME", "STOP_GRACE_S", "WatchChild", "end_all_watch_children",
    "watch_command",
]

_log = logger.bind(component="index.watch_child")

#: Seconds a closing window waits for the watch to stop by itself before
#: ending it. A batch in progress finishes the file in hand first, which is
#: seconds. Fixed (non-negotiable 11), as `child_run.STOP_GRACE_S` is.
STOP_GRACE_S = 5.0

#: Where the child's log lines and last words go, in the log folder. A file
#: rather than a pipe so nothing has to keep reading it.
STDERR_NAME = "watch-process-stderr.log"

#: The longest line read from the child; an event is a few hundred bytes.
_LINE_LIMIT = 1024 * 1024

_LIVE: "weakref.WeakSet[WatchChild]" = weakref.WeakSet()
_LIVE_LOCK = threading.Lock()


def watch_command(roots: Iterable[Any], *, env_file: Any = None,
                  python: Optional[str] = None,
                  extra: Iterable[str] = ()) -> list[str]:
    """The argument list that starts the watch as a child. A list, never a
    string, so no shell ever parses a folder name.

    The folders are named, so the child cannot fall back to a saved list that
    a change made a moment ago has not reached yet; `--live-only` still leaves
    out the ones marked Archive.
    """
    argv = [python or sys.executable, "-m", "app.cli"]
    if env_file:
        argv += ["--env", str(env_file)]
    argv += ["watch", "--events", "jsonl", "--live-only"]
    argv += [str(item) for item in extra]
    argv.append("--")
    argv += [str(Path(root)) for root in roots]
    return argv


def end_all_watch_children(grace_s: float = STOP_GRACE_S) -> int:
    """Stop every watch still running: ask, wait `grace_s`, then end it.

    Called as the window closes. Returns how many had to be ended by force.
    Never raises - this runs in a process that is exiting.
    """
    with _LIVE_LOCK:
        children = list(_LIVE)
    forced = 0
    for child in children:
        try:
            if child.shutdown(grace_s) == "terminated":
                forced += 1
        except Exception as exc:                 # noqa: BLE001 - closing never raises
            _log.warning("could not end the folder watch: {}", exc)
    return forced


class WatchChild:
    """One `app.cli watch --events jsonl` process.

        child = WatchChild(argv, env=env, cwd=project_root, on_event=handler)
        child.start()            # returns at once
        ...
        child.stop()             # returns at once; the process ends shortly
        child.shutdown(5.0)      # waits up to five seconds, then ends it

    `on_event(kind, data)` gets each event the child writes (`kind` as in
    `FolderWatcher`'s `on_event`, plus `"stopped"`), and one last
    `("ended", {"code": exit code, "expected": bool})` when the process has
    gone - `expected` False means nobody asked it to stop.
    """

    def __init__(self, argv: list[str], *, env: Optional[dict[str, str]] = None,
                 cwd: Any = None, stderr_path: Any = None,
                 on_event: Optional[Callable[[str, dict], None]] = None,
                 popen: Callable[..., Any] = subprocess.Popen) -> None:
        self.argv = list(argv)
        self.env = dict(env) if env is not None else None
        self.cwd = str(cwd) if cwd else None
        self.stderr_path = Path(stderr_path) if stderr_path else None
        self._on_event = on_event
        self._popen = popen
        self._lock = threading.Lock()
        self._proc: Any = None
        self._thread: Optional[threading.Thread] = None
        self._stop_asked = False
        self._ended = threading.Event()
        self.returncode: Optional[int] = None

    @property
    def running(self) -> bool:
        return self._thread is not None and not self._ended.is_set()

    def start(self) -> None:
        """Start the process and the thread that reads it. Returns at once."""
        if self._thread is not None:
            return
        with _LIVE_LOCK:
            _LIVE.add(self)
        self._thread = threading.Thread(
            target=self._run, name="folder-watch-child", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        try:
            proc = self._open()
        except Exception as exc:                 # noqa: BLE001 - said as an event
            _log.warning("the folder watch could not be started: {}", exc)
            self._finish(None, reason=str(exc))
            return
        with self._lock:
            self._proc = proc
            stop_first = self._stop_asked
        if stop_first:
            self._send_stop()
        try:
            while True:
                raw = proc.stdout.readline(_LINE_LIMIT)
                if not raw:
                    break
                if not raw.endswith(b"\n"):
                    continue                     # a dying child's half line
                try:
                    event = json.loads(raw.decode("ascii", "replace"))
                except ValueError:
                    continue
                if not isinstance(event, dict) or "kind" not in event:
                    continue
                self._say(str(event.pop("kind")), event)
        except (OSError, ValueError) as exc:
            _log.debug("the folder watch's output could not be read: {}", exc)
        code: Optional[int] = None
        try:
            code = proc.wait(timeout=STOP_GRACE_S)
        except subprocess.TimeoutExpired:
            code = self._force_end(proc)
        self._finish(code)

    def _open(self) -> Any:
        stderr: Any = subprocess.DEVNULL
        handle = None
        if self.stderr_path is not None:
            try:
                self.stderr_path.parent.mkdir(parents=True, exist_ok=True)
                handle = open(self.stderr_path, "wb")      # noqa: SIM115 - closed below
                stderr = handle
            except OSError as exc:
                _log.debug("the folder watch's error log could not be opened: {}", exc)
        _log.info("starting the folder watch: {}", " ".join(self.argv[1:]))
        try:
            return self._popen(
                self.argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=stderr, cwd=self.cwd, env=self.env)
        finally:
            if handle is not None:
                handle.close()                   # the child has its own copy now

    def _finish(self, code: Optional[int], *, reason: str = "") -> None:
        self.returncode = code
        proc = self._proc
        for stream in (getattr(proc, "stdin", None), getattr(proc, "stdout", None)):
            try:
                if stream is not None:
                    stream.close()
            except (OSError, ValueError):
                pass
        self._ended.set()
        with _LIVE_LOCK:
            _LIVE.discard(self)
        data: dict[str, Any] = {"code": code, "expected": self._stop_asked}
        if reason:
            data["reason"] = reason
        self._say("ended", data)

    def _say(self, kind: str, data: dict) -> None:
        if self._on_event is None:
            return
        try:
            self._on_event(kind, data)
        except Exception as exc:                 # noqa: BLE001 - a listener never ends the watch
            _log.debug("a folder watch listener failed on {!r}: {}", kind, exc)

    # -- ending it -----------------------------------------------------------------

    def _send_stop(self) -> None:
        """`stop` down the pipe, then close it. Never raises, never waits."""
        with self._lock:
            proc = self._proc
        stdin = getattr(proc, "stdin", None) if proc is not None else None
        if stdin is None:
            return
        try:
            stdin.write(b"stop\n")
            stdin.flush()
        except (OSError, ValueError):
            pass
        try:
            stdin.close()
        except (OSError, ValueError):
            pass

    def stop(self) -> None:
        """Ask the watch to stop. Returns at once; `ended` follows."""
        with self._lock:
            self._stop_asked = True
        self._send_stop()

    def shutdown(self, grace_s: float = STOP_GRACE_S) -> str:
        """Stop, wait up to `grace_s`, then end it. `"stopped"`, `"terminated"`
        or `"not running"`."""
        if self._thread is None or self._ended.is_set():
            return "not running"
        self.stop()
        if self._ended.wait(max(0.0, float(grace_s))):
            return "stopped"
        proc = self._proc
        if proc is None:
            return "not running"
        _log.warning("the folder watch did not stop within {:.0f}s; ending it", grace_s)
        self._force_end(proc)
        return "terminated"

    def _force_end(self, proc: Any) -> Optional[int]:
        from app.index.child_run import _end_descendants

        _end_descendants(getattr(proc, "pid", None))
        for step in (proc.terminate, proc.kill):
            try:
                step()
            except OSError:
                pass
            try:
                return proc.wait(timeout=STOP_GRACE_S)
            except subprocess.TimeoutExpired:
                continue
        return None
