r"""A warm, reused LibreOffice, so a legacy file does not pay for a cold start.

Layer: L2

**Measured on this machine (work order 202626270114 item 6i, 2026-09-20):**
a cold `soffice --headless --convert-to` costs five to ten seconds a file, and
nearly all of it is starting LibreOffice and building a first-run profile. This
module keeps LibreOffice running and hands it files one at a time.

**How.** For each worker there is one helper - `lo_server.py`, run by the
`python.exe` that ships beside `soffice.exe`, the only interpreter with LibreOffice's
`uno` bridge in it. The helper starts `soffice` against a **persistent profile**
and a private pipe, and then answers one JSON line per file. That is a session:
`start()`, `convert()`, `close()`. A `SessionPool` holds up to N of them and
lends one to each converting thread.

**Why this shape and not another** (each was tried; the numbers are in
work order 202626270114, section 6i):

* *A persistent profile alone* removes first-run profile creation but not the
  process start, which is most of the cost.
* *One soffice, many files on the command line* is fast but needs the caller to
  hand over batches; the pipeline converts one file per worker thread and the
  interface must not change.
* *Driving the CLI while an instance is running* forwards the request and
  returns before the file is written - no completion signal and no error.
* *A UNO connection* is synchronous, reports failures, and lets the parent see
  exactly when a file is stuck. That is this module.

**What makes it safe to leave running.**

* Every child belongs to one Windows **job object** with kill-on-close, so if
  Leasha dies - crash, End Task, power-cut of the parent - the operating system
  removes LibreOffice with it. No orphan `soffice.exe` outlives the process.
* A file that does not finish in its time limit, or takes a runaway amount of
  memory (one 1.4MB `.doc` on this corpus took LibreOffice past 8GB), gets its
  whole process tree killed and a fresh session on the next file.
* A session that dies - `kill -9`, a crash inside a filter - is noticed on the
  next request, replaced, and the file retried once.
* A session is recycled after `RECYCLE_AFTER` files (LibreOffice grows) and
  closed after `IDLE_CLOSE_S` idle, so a finished run leaves nothing behind.
* Every wait polls `should_stop` a few times a second, so pressing Stop or
  closing the window never waits for a conversion, and every thread here is a
  daemon.

**What it does not do.** It never runs a shell. The programs it starts are the
two allowed by name in `converter.ALLOWED_BINARIES`, resolved to absolute paths,
with fixed arguments and the file names passed as data over a pipe.
"""

from __future__ import annotations

import atexit
import contextlib
import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterator, Optional, Sequence

from app.core.logging import logger

__all__ = [
    "ConversionFailed",
    "ConversionStopped",
    "ConversionTimeout",
    "ConversionTooBig",
    "LoSession",
    "SessionCrashed",
    "SessionPool",
    "SessionUnavailable",
    "convert_warm",
    "get_pool",
    "kind_for",
    "notice",
    "quiet_errors",
    "set_stop_check",
    "shutdown",
]

log = logger.bind(component="extract.lo_session")

#: How long a first start may take. A brand-new profile is built and LibreOffice
#: restarts itself once; that measured 8 to 25 seconds on a busy machine.
START_TIMEOUT_S = 120.0

#: **Memory one session may hold before it is killed.** Evidence: a 1.4MB
#: `.doc` on the owner's corpus took a cold LibreOffice to 8GB and past 11GB; the
#: normal figure is 130 to 350MB. 3GB is roughly ten times normal and well
#: under what makes a laptop swap. What would change it: a real file that needs
#: more and converts correctly - then it is a Settings control, not a guess.
MEMORY_LIMIT_MB = 3000

#: Files converted before a session is replaced. LibreOffice's footprint creeps;
#: measured flat over 200 small files here, so this is a backstop, not a fix.
RECYCLE_AFTER = 250

#: A session unused for this long is closed, so the end of a run - which this
#: module is never told about - leaves no LibreOffice running.
IDLE_CLOSE_S = 90.0

#: How often each wait looks at the clock, the stop flag and memory.
POLL_S = 0.2

#: Profile slots tried before giving up and using a throwaway directory.
_PROFILE_SLOTS = 16

#: **The restart policy.** LibreOffice 26.8 was observed to crash (`soffice.bin`
#: APPCRASH, exception 0x0) converting some real decks and documents. A session
#: that restarts after every crash forever is a crash loop with Windows error
#: boxes on somebody's desktop, so: a crashed file is tried once more on a fresh
#: session; a file that crashes it twice is that file's fault and is skipped;
#: and after `CRASH_LIMIT` files in a row that crashed LibreOffice, nothing is
#: sent to it for `COOLDOWN_S`, and each waiting file is skipped with a plain
#: sentence instead. Any success ends the streak.
CRASH_LIMIT = 4
COOLDOWN_S = 600.0

#: A restarted session waits this long, times the crashes in a row, before it
#: starts (capped) - so even inside the limit a bad patch is not hammered.
RESTART_BACKOFF_S = 2.0
RESTART_BACKOFF_MAX_S = 10.0

_NO_WINDOW = 0x08000000                          # CREATE_NO_WINDOW


class SessionUnavailable(Exception):
    """A session cannot be run at all here (no LibreOffice, no `uno`, no start).

    Not the file's fault, so the caller falls back to the cold command.
    """


class ConversionFailed(Exception):
    """This file did not convert. The session itself is fine unless it says so."""


class ConversionTimeout(ConversionFailed):
    pass


class ConversionTooBig(ConversionFailed):
    pass


class ConversionStopped(ConversionFailed):
    pass


class SessionCrashed(ConversionFailed):
    """The process died under the request. Worth one retry on a fresh session."""


# ---------------------------------------------------------------------------
# Killing, and the job object
# ---------------------------------------------------------------------------

_job_handle: Any = None
_job_lock = threading.Lock()


def _job() -> Any:
    """One kill-on-close job object for every child this module starts.

    Windows only, and best effort: if anything about it fails, the explicit
    tree-kill in `kill_tree` still runs. The job is the guarantee for the case
    that code never gets to run.
    """
    global _job_handle
    if os.name != "nt":
        return None
    with _job_lock:
        if _job_handle is not None:
            return _job_handle or None
        try:
            import ctypes
            from ctypes import wintypes

            class BasicLimits(ctypes.Structure):
                _fields_ = [
                    ("PerProcessUserTimeLimit", ctypes.c_int64),
                    ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD),
                    ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t),
                    ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t),
                    ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD),
                ]

            class IoCounters(ctypes.Structure):
                _fields_ = [(name, ctypes.c_uint64) for name in (
                    "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                    "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

            class ExtendedLimits(ctypes.Structure):
                _fields_ = [
                    ("BasicLimitInformation", BasicLimits),
                    ("IoInfo", IoCounters),
                    ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t),
                ]

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CreateJobObjectW.restype = wintypes.HANDLE
            handle = kernel32.CreateJobObjectW(None, None)
            if not handle:
                raise OSError(ctypes.get_last_error(), "CreateJobObjectW")
            info = ExtendedLimits()
            # KILL_ON_JOB_CLOSE (0x2000) and PROCESS_MEMORY (0x100): the second
            # is the operating system's own backstop behind the polling
            # watchdog, at half as much again as the watchdog allows.
            info.BasicLimitInformation.LimitFlags = 0x2000 | 0x100
            info.ProcessMemoryLimit = int(MEMORY_LIMIT_MB * 1.5) * 1024 * 1024
            ok = kernel32.SetInformationJobObject(
                handle, 9, ctypes.byref(info), ctypes.sizeof(info))
            if not ok:
                raise OSError(ctypes.get_last_error(), "SetInformationJobObject")
            _job_handle = handle
            return handle
        except Exception as exc:                          # noqa: BLE001 - best effort
            log.debug("no job object; relying on explicit kills: {}", exc)
            _job_handle = 0
            return None


def _assign_to_job(proc: "subprocess.Popen[Any]") -> None:
    handle = _job()
    if not handle:
        return
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        if not kernel32.AssignProcessToJobObject(handle, int(proc._handle)):  # type: ignore[attr-defined]
            log.debug("could not assign pid {} to the job: {}", proc.pid,
                      ctypes.get_last_error())
    except Exception as exc:                              # noqa: BLE001
        log.debug("could not assign pid {} to the job: {}", proc.pid, exc)


def _tree_pids(pid: int) -> list[int]:
    try:
        import psutil

        return [child.pid for child in psutil.Process(pid).children(recursive=True)]
    except Exception:                                     # noqa: BLE001 - gone already
        return []


def kill_tree(proc: "subprocess.Popen[Any]", extra_pids: Sequence[int] = ()) -> None:
    """Kill a process and everything it started. Never raises."""
    victims = _tree_pids(proc.pid) if proc.poll() is None else []
    try:
        import psutil

        for pid in extra_pids:
            try:
                target = psutil.Process(pid)
                # Only ever something that is still LibreOffice: a pid can be
                # reused by an unrelated program once its owner has gone.
                if target.name().lower().startswith("soffice"):
                    victims.extend(_tree_pids(pid))
                    victims.append(pid)
            except psutil.Error:
                continue
        for pid in dict.fromkeys(victims):
            try:
                psutil.Process(pid).kill()
            except psutil.Error:
                continue
    except ImportError:                                   # pragma: no cover
        pass
    try:
        proc.kill()
    except OSError:
        pass
    try:
        proc.wait(timeout=5)
    except Exception:                                     # noqa: BLE001
        pass


def _tree_rss_mb(pid: int) -> float:
    try:
        import psutil

        process = psutil.Process(pid)
        total = process.memory_info().rss
        for child in process.children(recursive=True):
            try:
                total += child.memory_info().rss
            except psutil.Error:
                continue
        return total / (1024 * 1024)
    except Exception:                                     # noqa: BLE001
        return 0.0


# ---------------------------------------------------------------------------
# Never an operating-system error box
# ---------------------------------------------------------------------------

_error_mode_lock = threading.Lock()

#: SEM_FAILCRITICALERRORS | SEM_NOGPFAULTERRORBOX | SEM_NOOPENFILEERRORBOX.
_QUIET = 0x0001 | 0x0002 | 0x8000


@contextlib.contextmanager
def quiet_errors() -> Iterator[None]:
    """Start children so a crash of theirs is silent to the person at the desk.

    **This is the fix for a real incident (2026-09-20).** `soffice.bin` crashed
    on some documents, and each crash raised Windows' "has stopped working" box
    on the owner's desktop - a program that is meant to run invisibly, showing
    an error, repeatedly. A child inherits its parent's *error mode*, so the mode
    is set around `CreateProcess` and put back straight after: the child (and the
    `soffice.bin` it starts) never shows the crash dialog, and this process's own
    behaviour is unchanged. What the person sees instead is the file being
    skipped with a sentence saying why.
    """
    if os.name != "nt":
        yield
        return
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    with _error_mode_lock:
        previous = kernel32.SetErrorMode(_QUIET)
        try:
            yield
        finally:
            kernel32.SetErrorMode(previous)


# ---------------------------------------------------------------------------
# The profile a session keeps between files and between runs
# ---------------------------------------------------------------------------


def _profile_root() -> Path:
    """A private folder in the application's own data, never shared.

    Under the index's cache folder when Settings are available - that is
    Leasha's, it is not synced or virtualised, and it is what a person deletes to
    reclaim space. `LOCALAPPDATA` only as a fallback: inside a packaged (MSIX)
    process it is silently redirected to a per-package cache, which is where a
    profile must not end up.
    """
    try:
        from app.core.config import load_settings

        settings = load_settings(create_dirs=False, check_writable=False)
        return Path(settings.cache_path) / "lo-profile"
    except Exception:                                     # noqa: BLE001 - no .env in tests
        pass
    base = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
    return Path(base) / "Leasha" / "lo-profile"


class _ProfileLease:
    """An exclusive claim on one profile directory, held for a session's life.

    Two processes must never share a LibreOffice profile - they fight over its
    lock and one can end up showing a dialog. A lock *file* per slot is claimed
    non-blockingly, so a second Leasha (or a test run beside a real one) simply
    takes the next slot. If every slot is taken the lease is a throwaway
    directory, deleted on release.
    """

    def __init__(self, root: Path, slots: int = _PROFILE_SLOTS) -> None:
        self.path: Path
        self._handle: Any = None
        self._throwaway = False
        root.mkdir(parents=True, exist_ok=True)
        for slot in range(slots):
            lock = root / f"slot-{slot}.lock"
            try:
                handle = open(lock, "a+b")
                _lock_file(handle)
            except OSError:
                try:
                    handle.close()                        # type: ignore[possibly-undefined]
                except Exception:                         # noqa: BLE001
                    pass
                continue
            self._handle = handle
            self.path = root / f"slot-{slot}"
            self.path.mkdir(exist_ok=True)
            return
        self._throwaway = True
        self.path = Path(tempfile.mkdtemp(prefix="leasha-lo-"))

    @property
    def url(self) -> str:
        return "file:///" + str(self.path.resolve()).replace("\\", "/")

    def reset(self) -> None:
        """Delete the profile's contents: for a profile LibreOffice cannot start on."""
        shutil.rmtree(self.path, ignore_errors=True)
        self.path.mkdir(parents=True, exist_ok=True)

    def release(self) -> None:
        if self._handle is not None:
            try:
                _unlock_file(self._handle)
                self._handle.close()
            except OSError:
                pass
            self._handle = None
        if self._throwaway:
            shutil.rmtree(self.path, ignore_errors=True)


def _lock_file(handle: Any) -> None:
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:                                                 # pragma: no cover - not shipped
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_file(handle: Any) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:                                                 # pragma: no cover
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


# ---------------------------------------------------------------------------
# One session
# ---------------------------------------------------------------------------

#: `command_factory(profile_url) -> argv`. Injected by tests; the default builds
#: the real one from the two allowed programs.
CommandFactory = Callable[[str], Sequence[str]]


def default_command(profile_url: str) -> list[str]:
    """The real helper command, or `SessionUnavailable` naming what is missing."""
    from app.extract.converter import resolve_binary

    soffice = resolve_binary("soffice")
    if not soffice:
        raise SessionUnavailable("LibreOffice (soffice) was not found")
    python = resolve_binary("libreoffice-python")
    if not python:
        raise SessionUnavailable("the Python that ships with LibreOffice was not found")
    if Path(python).parent != Path(soffice).parent:
        # A real install keeps the two side by side. Anything else - a stand-in
        # `soffice` on PATH, a test's fake - is not a LibreOffice this module
        # can drive, so the cold command handles it.
        raise SessionUnavailable("soffice and LibreOffice's Python are not in the same folder")
    server = Path(__file__).with_name("lo_server.py")
    # `-P`: do not put this folder first on sys.path. It holds modules named
    # `doc`, `ppt` and `base`, which must never shadow anything LibreOffice's
    # Python imports.
    return [python, "-P", str(server), soffice, profile_url]


def kind_for(command: Sequence[str]) -> Optional[str]:
    """What LibreOffice should write, from a rule's `--convert-to` argument.

    `txt:Text` -> `txt`, `pptx` -> `pptx`, `csv` -> `csv`. None when the rule has
    no `--convert-to` or asks for a kind the helper has no filter for - the
    caller then uses the cold command, which knows every filter LibreOffice does.
    """
    try:
        spec = command[list(command).index("--convert-to") + 1]
    except (ValueError, IndexError):
        return None
    kind = spec.split(":", 1)[0].strip().lower()
    from app.extract.lo_server import FILTERS

    return kind if kind in FILTERS else None


class LoSession:
    """One LibreOffice, kept running between files. Use one thread at a time."""

    def __init__(
        self,
        *,
        command_factory: CommandFactory = default_command,
        profile_root: Optional[Path] = None,
        memory_limit_mb: float = MEMORY_LIMIT_MB,
        recycle_after: int = RECYCLE_AFTER,
        start_timeout_s: float = START_TIMEOUT_S,
        name: str = "lo",
    ) -> None:
        self.name = name
        self._factory = command_factory
        self._profile_root = profile_root
        self._memory_limit_mb = memory_limit_mb
        self._recycle_after = recycle_after
        self._start_timeout_s = start_timeout_s
        self._lock = threading.RLock()
        #: Set by `abort()`: the application is closing, so nothing waits.
        self._abort = False
        self._proc: Optional["subprocess.Popen[str]"] = None
        self._lines: "queue.Queue[Optional[str]]" = queue.Queue()
        self._lease: Optional[_ProfileLease] = None
        self._soffice_pid = 0
        self._next_id = 0
        #: Files converted by the current process; the recycle counter.
        self.converted = 0
        #: Sessions that died under a request, in a row. Any success clears it.
        self.crash_streak = 0
        #: Times a process was started - a test's window on "did it restart".
        self.starts = 0
        self.last_used = time.monotonic()

    # -- lifecycle -----------------------------------------------------------

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    @property
    def pid(self) -> int:
        return self._proc.pid if self._proc is not None else 0

    def start(self, should_stop: Optional[Callable[[], bool]] = None) -> None:
        with self._lock:
            if self.alive:
                return
            self._teardown(kill=True)
            if self.crash_streak:
                # Backoff, honouring Stop: a session that just died is not
                # restarted in the same breath.
                wait = min(RESTART_BACKOFF_MAX_S, RESTART_BACKOFF_S * self.crash_streak)
                until = time.monotonic() + wait
                while time.monotonic() < until:
                    if should_stop is not None and should_stop():
                        raise ConversionStopped("stopped")
                    time.sleep(min(POLL_S, max(0.0, until - time.monotonic())))
            for attempt in (1, 2):
                try:
                    self._start_once(should_stop)
                    return
                except SessionUnavailable:
                    self._teardown(kill=True)
                    if attempt == 2 or self._lease is None or self._lease._throwaway:
                        raise
                    # A profile LibreOffice cannot start on is the commonest
                    # cause of a session that never comes up. Start clean once.
                    log.debug("{}: resetting the profile and trying once more", self.name)
                    self._lease.reset()

    def _start_once(self, should_stop: Optional[Callable[[], bool]]) -> None:
        if self._lease is None:
            self._lease = _ProfileLease(self._profile_root or _profile_root())
        argv = list(self._factory(self._lease.url))
        try:
            with quiet_errors():
                proc = subprocess.Popen(
                    argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL, text=True, encoding="utf-8",
                    errors="replace", bufsize=1, shell=False,
                    creationflags=_NO_WINDOW if os.name == "nt" else 0,
                )
        except OSError as exc:
            raise SessionUnavailable(f"could not start the helper: {exc}") from exc
        _assign_to_job(proc)
        self._proc = proc
        self._lines = queue.Queue()
        threading.Thread(target=self._read, args=(proc, self._lines),
                         name=f"{self.name}-reader", daemon=True).start()

        deadline = time.monotonic() + self._start_timeout_s
        while True:
            if should_stop is not None and should_stop():
                self._teardown(kill=True)
                raise ConversionStopped("stopped while LibreOffice was starting")
            try:
                line = self._lines.get(timeout=POLL_S)
            except queue.Empty:
                if proc.poll() is not None:
                    raise SessionUnavailable("the helper exited while starting")
                if time.monotonic() > deadline:
                    raise SessionUnavailable(
                        f"LibreOffice was not ready within {self._start_timeout_s:.0f}s")
                continue
            if line is None:
                raise SessionUnavailable("the helper closed before it was ready")
            message = _parse(line)
            if message.get("ready"):
                self._soffice_pid = int(message.get("soffice_pid") or 0)
                self.starts += 1
                self.converted = 0
                self.last_used = time.monotonic()
                log.debug("{}: session up (pid {}, soffice {})", self.name, proc.pid,
                          self._soffice_pid)
                return
            if message.get("fatal"):
                raise SessionUnavailable(str(message["fatal"]))

    @staticmethod
    def _read(proc: "subprocess.Popen[str]", out: "queue.Queue[Optional[str]]") -> None:
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                out.put(line)
        except Exception:                                 # noqa: BLE001 - pipe closed
            pass
        finally:
            out.put(None)

    def _teardown(self, kill: bool) -> None:
        proc, self._proc = self._proc, None
        if proc is not None:
            if not kill and proc.poll() is None:
                try:
                    assert proc.stdin is not None
                    proc.stdin.write('{"quit": true}\n')
                    proc.stdin.flush()
                    proc.wait(timeout=10)
                except Exception:                         # noqa: BLE001 - fall through to kill
                    pass
            kill_tree(proc, extra_pids=[self._soffice_pid] if self._soffice_pid else ())
        self._soffice_pid = 0

    def abort(self) -> None:
        """Kill this session's process tree **without waiting for its lock**.

        `close()` waits for a conversion in progress (it holds the lock), which
        is right for tidying but wrong when the application is closing. `abort`
        takes the process down from any thread at once; the thread that was
        waiting on it sees the process gone and stops with `ConversionStopped`.
        """
        self._abort = True
        proc = self._proc
        if proc is not None:
            kill_tree(proc, extra_pids=[self._soffice_pid] if self._soffice_pid else ())

    def close(self, graceful: bool = True) -> None:
        """Stop LibreOffice and give the profile back. Safe to call twice."""
        with self._lock:
            self._abort = False
            self._teardown(kill=not graceful)
            if self._lease is not None:
                self._lease.release()
                self._lease = None

    # -- work ----------------------------------------------------------------

    def convert(
        self,
        source: Path,
        target: Path,
        kind: str,
        *,
        timeout_s: float,
        should_stop: Optional[Callable[[], bool]] = None,
    ) -> float:
        """Convert one file, returning the seconds it took inside LibreOffice.

        Raises `ConversionFailed` (or a subclass) for this file, and
        `SessionUnavailable` when no session can be had at all.
        """
        try:
            return self._convert(source, target, kind, timeout_s=timeout_s,
                                 should_stop=should_stop)
        except SessionCrashed:
            self.crash_streak += 1
            raise

    def _convert(
        self,
        source: Path,
        target: Path,
        kind: str,
        *,
        timeout_s: float,
        should_stop: Optional[Callable[[], bool]] = None,
    ) -> float:
        with self._lock:
            if self.alive and self.converted >= self._recycle_after:
                log.debug("{}: recycling after {} files", self.name, self.converted)
                self._teardown(kill=False)
            if not self.alive:
                self.start(should_stop)
            assert self._proc is not None and self._proc.stdin is not None
            proc = self._proc
            self._next_id += 1
            request_id = self._next_id
            payload = json.dumps({"id": request_id, "in": str(Path(source).resolve()),
                                  "out": str(Path(target).resolve()), "filter": kind})
            started = time.monotonic()
            try:
                proc.stdin.write(payload + "\n")
                proc.stdin.flush()
            except OSError as exc:
                self._teardown(kill=True)
                raise SessionCrashed(f"LibreOffice went away: {exc}") from exc

            deadline = started + max(1.0, float(timeout_s))
            next_memory_check = started + 1.0
            while True:
                if self._abort or (should_stop is not None and should_stop()):
                    self._teardown(kill=True)
                    raise ConversionStopped("stopped")
                try:
                    line = self._lines.get(timeout=POLL_S)
                except queue.Empty:
                    now = time.monotonic()
                    if proc.poll() is not None:
                        self._teardown(kill=True)
                        raise SessionCrashed("LibreOffice exited while converting")
                    if now > deadline:
                        self._teardown(kill=True)
                        raise ConversionTimeout(f"did not finish within {timeout_s:.0f}s")
                    if now >= next_memory_check:
                        next_memory_check = now + 1.0
                        used = _tree_rss_mb(proc.pid)
                        if used > self._memory_limit_mb:
                            self._teardown(kill=True)
                            raise ConversionTooBig(
                                f"LibreOffice needed more than "
                                f"{self._memory_limit_mb:,.0f} MB ({used:,.0f} MB) "
                                f"for this file")
                    continue
                if line is None:
                    self._teardown(kill=True)
                    raise SessionCrashed("LibreOffice closed the connection")
                message = _parse(line)
                if message.get("fatal"):
                    self._teardown(kill=True)
                    raise SessionCrashed(str(message["fatal"]))
                if message.get("id") != request_id:
                    continue                              # a stale answer to an earlier file
                self.converted += 1
                self.last_used = time.monotonic()
                if message.get("ok"):
                    self.crash_streak = 0
                    if not Path(target).is_file():
                        raise ConversionFailed("LibreOffice reported success but wrote nothing")
                    return float(message.get("s") or (time.monotonic() - started))
                error = str(message.get("error") or "conversion failed")
                if "DisposedException" in error or "ConnectException" in error:
                    # The bridge to LibreOffice is gone: the session's fault,
                    # so it is replaced and the file tried again.
                    self._teardown(kill=True)
                    raise SessionCrashed(error)
                raise ConversionFailed(error)


def _parse(line: str) -> dict[str, Any]:
    try:
        value = json.loads(line)
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


# ---------------------------------------------------------------------------
# A pool of sessions
# ---------------------------------------------------------------------------

_StopCheck = Optional[Callable[[], bool]]

_stop_check: _StopCheck = None


def set_stop_check(check: _StopCheck) -> None:
    """Give the sessions the run's stop flag: `set_stop_check(self._stop.is_set)`.

    Optional. Without it a conversion is still bounded by its time limit and
    the process's exit; with it, Stop ends the wait within a fifth of a second.
    """
    global _stop_check
    _stop_check = check


def _stopped() -> bool:
    check = _stop_check
    try:
        return bool(check()) if check is not None else False
    except Exception:                                     # noqa: BLE001 - a broken flag must not hang
        return False


#: What a person is told when the circuit is open. One sentence, no jargon.
CIRCUIT_NOTICE = (
    "LibreOffice keeps stopping on this computer, so files that need it are being "
    "skipped for a few minutes. They are still findable by their names.")


class SessionPool:
    """Up to `size` sessions, each lent to one thread at a time."""

    def __init__(
        self,
        size: int,
        *,
        session_factory: Optional[Callable[[int], LoSession]] = None,
        idle_close_s: float = IDLE_CLOSE_S,
        cooldown_s: float = COOLDOWN_S,
    ) -> None:
        self.size = max(1, int(size))
        self._cooldown_s = cooldown_s
        self._crashed_files = 0
        self._open_until = 0.0
        self._factory = session_factory or (lambda n: LoSession(name=f"lo-{n}"))
        self._idle_close_s = idle_close_s
        self._cond = threading.Condition()
        self._idle: list[LoSession] = []
        self._all: list[LoSession] = []
        self._closed = False
        self._reaper: Optional[threading.Thread] = None

    def _acquire(self, should_stop: _StopCheck) -> LoSession:
        with self._cond:
            while True:
                if self._closed:
                    raise ConversionStopped("the converter is shutting down")
                if should_stop is not None and should_stop():
                    raise ConversionStopped("stopped")
                if self._idle:
                    return self._idle.pop()
                if len(self._all) < self.size:
                    session = self._factory(len(self._all))
                    self._all.append(session)
                    self._ensure_reaper()
                    return session
                self._cond.wait(timeout=POLL_S)

    def _release(self, session: LoSession) -> None:
        with self._cond:
            if not self._closed and session in self._all:
                self._idle.append(session)
            self._cond.notify()

    def convert(
        self,
        source: Path,
        target: Path,
        kind: str,
        *,
        timeout_s: float,
        should_stop: _StopCheck = None,
    ) -> float:
        def stopping() -> bool:
            return bool((should_stop and should_stop()) or _stopped())

        self._check_circuit()
        session = self._acquire(stopping)
        try:
            try:
                result = session.convert(source, target, kind, timeout_s=timeout_s,
                                        should_stop=stopping)
            except SessionCrashed as first:
                # Something outside the file - a kill, a crash in a filter -
                # took the process. One retry on a fresh one; a second death is
                # the file's, and it is skipped rather than tried a third time.
                log.debug("{}: {} - retrying once on a new session", session.name, first)
                try:
                    result = session.convert(source, target, kind, timeout_s=timeout_s,
                                             should_stop=stopping)
                except SessionCrashed as second:
                    self._file_crashed()
                    raise ConversionFailed(
                        "LibreOffice stopped unexpectedly twice on this file, "
                        "so it was skipped") from second
            with self._cond:
                self._crashed_files = 0
            return result
        finally:
            self._release(session)

    # -- the crash circuit ---------------------------------------------------

    def _check_circuit(self) -> None:
        with self._cond:
            if time.monotonic() < self._open_until:
                raise ConversionFailed(CIRCUIT_NOTICE)

    def _file_crashed(self) -> None:
        with self._cond:
            self._crashed_files += 1
            if self._crashed_files >= CRASH_LIMIT:
                self._open_until = time.monotonic() + self._cooldown_s
                self._crashed_files = 0
                log.warning(
                    "LibreOffice stopped unexpectedly on {} files in a row; "
                    "not sending it any more for {:.0f}s", CRASH_LIMIT, self._cooldown_s)

    @property
    def circuit_open(self) -> bool:
        with self._cond:
            return time.monotonic() < self._open_until

    def close(self) -> None:
        with self._cond:
            self._closed = True
            sessions = list(self._all)
            self._cond.notify_all()
        for session in sessions:
            try:
                # A session mid-file is killed, not waited for: closing must
                # never block the application.
                session.abort()
                session.close(graceful=False)
            except Exception as exc:                      # noqa: BLE001
                log.debug("closing {}: {}", session.name, exc)

    def live_pids(self) -> list[int]:
        return [s.pid for s in list(self._all) if s.alive]

    # -- idle reaper ---------------------------------------------------------

    def _ensure_reaper(self) -> None:
        if self._reaper is None or not self._reaper.is_alive():
            self._reaper = threading.Thread(target=self._reap, name="lo-reaper", daemon=True)
            self._reaper.start()

    def _reap(self) -> None:
        interval = max(0.05, min(5.0, self._idle_close_s / 3))
        while True:
            time.sleep(interval)
            with self._cond:
                if self._closed:
                    return
                stale = [s for s in self._idle
                         if s.alive and time.monotonic() - s.last_used > self._idle_close_s]
                for session in stale:
                    self._idle.remove(session)
            for session in stale:
                # Only sessions that were idle *and are now removed from the
                # idle list* are closed, so no thread can be inside one. Asked
                # to quit rather than killed, so the profile is left tidy.
                session.close(graceful=True)
                with self._cond:
                    if not self._closed:
                        self._idle.append(session)
                        self._cond.notify()


# ---------------------------------------------------------------------------
# The shared pool, configured from Settings
# ---------------------------------------------------------------------------

_pool: Optional[SessionPool] = None
_pool_key: Optional[tuple[int, int]] = None
_pool_lock = threading.Lock()
_wanted: Optional[tuple[float, tuple[int, int]]] = None

#: Settings are re-read at most this often.
_SETTINGS_TTL_S = 20.0


def auto_workers() -> int:
    """One session, or two on a machine with the memory to hold a second.

    Legacy Office files are the *fallback* for what the in-process readers
    decline, so they arrive one at a time and rarely; a second session only
    matters for a folder of old `.pub` or `.wpd` files.
    """
    try:
        import psutil

        total_gb = psutil.virtual_memory().total / (1024 ** 3)
    except Exception:                                     # noqa: BLE001
        return 1
    return 2 if total_gb >= 15 else 1


def _read_settings() -> tuple[int, int]:
    """`(sessions, seconds per file)` from Settings, defaults on any trouble."""
    workers, timeout = 0, 120
    try:
        from app.core.config import load_settings

        settings = load_settings(create_dirs=False, check_writable=False)
        workers = int(getattr(settings, "converter_workers", 0) or 0)
        timeout = int(getattr(settings, "converter_timeout_s", 120) or 120)
    except Exception:                                     # noqa: BLE001 - no .env is normal in tests
        pass
    workers = auto_workers() if workers <= 0 else max(1, min(4, workers))
    return workers, max(20, min(300, timeout))


def wanted() -> tuple[int, int]:
    """`(sessions, seconds per file)` now in force."""
    global _wanted
    now = time.monotonic()
    if _wanted is None or now - _wanted[0] > _SETTINGS_TTL_S:
        _wanted = (now, _read_settings())
    return _wanted[1]


def get_pool() -> SessionPool:
    """The shared pool, rebuilt if the pool size setting has changed."""
    global _pool, _pool_key
    size, _timeout = wanted()
    with _pool_lock:
        if _pool is not None and _pool_key != (size, 0):
            old, _pool = _pool, None
            old.close()
        if _pool is None:
            _pool = SessionPool(size)
            _pool_key = (size, 0)
            atexit.register(shutdown)
        return _pool


def shutdown() -> None:
    """Close every session now. Idempotent, and what `atexit` runs."""
    global _pool, _pool_key
    with _pool_lock:
        pool, _pool, _pool_key = _pool, None, None
    if pool is not None:
        pool.close()


#: After a session cannot be had at all, do not try again for this long. Without
#: it every legacy file would spend up to `START_TIMEOUT_S` failing to start
#: LibreOffice before falling back to the cold command.
_RETRY_AFTER_S = 300.0
_disabled_until = 0.0


def convert_warm(source: Path, target: Path, kind: str, *, timeout_s: float,
                 should_stop: _StopCheck = None) -> float:
    """Convert through the shared pool, or raise `SessionUnavailable`.

    `SessionUnavailable` is the caller's cue to use the cold command; the
    other exceptions are about the file and are final.
    """
    global _disabled_until
    if time.monotonic() < _disabled_until:
        raise SessionUnavailable("a warm session could not be started a moment ago")
    try:
        return get_pool().convert(source, target, kind, timeout_s=timeout_s,
                                  should_stop=should_stop)
    except SessionUnavailable:
        _disabled_until = time.monotonic() + _RETRY_AFTER_S
        raise


def notice() -> str:
    """A sentence for the app to show while LibreOffice is being left alone, else ''."""
    pool = _pool
    return CIRCUIT_NOTICE if pool is not None and pool.circuit_open else ""


def reset_settings_cache() -> None:
    """For tests: forget the cached Settings read."""
    global _wanted, _disabled_until
    _wanted = None
    _disabled_until = 0.0
