r"""Running the indexer in a process of its own, supervised by the window.

Layer: L3

Work order 0x §2 (decision D1). This module starts `python -m app.cli index
--events jsonl` as a **child process** of the window, reads what it says, and
passes Pause, Resume and Stop to it. To the rest of the window it looks like a
`Pipeline`: `IndexingView.start(...)` is handed a `ChildIndexRun` instead of a
`Pipeline`, and the page, its worker and its handlers do not know the
difference.

**Why a separate process at all.** Python runs one thread at a time inside one
process: a thread must hold the *global interpreter lock* (the GIL) to run
Python code. The window's own thread - the one that draws the screen and
answers the mouse - shares that lock with every indexing thread when they
live in one process, and however carefully the indexing work is split into
threads, each of them holds the lock for a while and the window waits its turn.
HANDOFF records the workarounds that tried to soften this (`lag_monitor`, a
tighter `sys.setswitchinterval`, the pipeline yielding when the window runs
late). A child process has **its own interpreter and its own lock**, so the
indexer can be as busy as it likes and the window's thread never waits for it.
The operating system shares the processor between the two, and the child is
run below normal priority (by the same code `app.cli index` has always used,
`ResourceGovernor.apply_priority`), so the window wins that contest too.

**Why the child's standard input and output.** No network port, no service,
no FastAPI (the owner's decision; HANDOFF §1's one-process, no-services rule
still holds for everything a person uses). A pipe is the one channel every
operating system gives a parent and its child for free, it closes by itself
when either end dies (which is how the child notices the window has gone),
and nothing has to be installed or listening. The language on the pipe is
`app/index/run_events.py`: one JSON object per line.

**Why a blocking read on the page's own worker thread, and not `QProcess`.**
`QProcess` would read the pipe on the window's thread through
`readyReadStandardOutput`, with no extra thread - a good fit in general. Here it
would mean changing `IndexingView`, which owns the run through `IndexWorker`
on its own one-thread pool (and is outside this change). Reusing that thread
means every existing piece keeps working untouched: `is_running`, the external
run watch, `_drain_workers` waiting for the pool at close, the scheduler, the
Pause and Stop buttons. The thread is not an *extra* one - it is the thread
that used to run the whole pipeline, and now only waits. While it waits in
`readline` it does not hold the GIL, so it costs the window nothing; the few
lines a second it parses would otherwise have been parsed on the window's own
thread by `QProcess`. `subprocess.Popen` with an argument list behaves the
same on Windows and macOS.

**What the window keeps.** The run lock is taken by the child itself (it is an
ordinary `app.cli index`, naming itself "the window" on the published record),
so the lock's rules are unchanged: a child that dies has its lock released by
the operating system and leaves its record behind - which is exactly the
evidence 0w's interrupted-run notice reads the next time the page opens.

**Never an orphan.** Three independent guards:

1. Closing the window asks the child to stop, waits a bounded time, then ends
   it (`end_all_children`, called from `MainWindow.closeEvent`).
2. If the window dies without closing (killed from Task Manager, a crash), the
   child's standard input reaches its end; the child takes that as Stop, and
   ends itself if the stop has not finished within `ORPHAN_GRACE_S`
   (`app/cli/index.py`).
3. A write to a pipe nobody reads fails, and the child stops on that too.
"""

from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
import time
import weakref
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Iterable, Optional

from app.core.logging import logger

__all__ = [
    "CHILD_STDERR_NAME", "ChildIndexRun", "LINE_LIMIT_BYTES", "ORPHAN_GRACE_S",
    "STOP_GRACE_S", "TERMINATE_WAIT_S", "child_command", "end_all_children",
    "live_children", "settings_environment",
]

_log = logger.bind(component="index.child")

#: Seconds a closing window waits for the child to stop cleanly before ending
#: it. A clean stop finishes the file in hand, which is seconds for almost
#: every file and can be longer for a large archive; `MainWindow` has already
#: waited `INDEX_SHUTDOWN_GRACE_MS` (30 s) for the run by the time this is
#: asked, so this is the last few seconds, not the whole allowance. Fixed, not
#: a setting (non-negotiable #11): it trades a lingering process against a
#: file read twice, and nobody would tune that.
STOP_GRACE_S = 5.0

#: Seconds to wait after ending the child before forcing it (POSIX sends
#: SIGTERM, then SIGKILL; on Windows the first is already final).
TERMINATE_WAIT_S = 5.0

#: Seconds a child that has lost its window gives its own clean stop before it
#: ends itself. Read by `app/cli/index.py`; here so both sides are documented
#: together. Long enough for an embedding batch and a flush to finish; short
#: enough that nobody finds an indexer running with no window.
ORPHAN_GRACE_S = 60.0

#: Seconds to wait for the child's first line. It has to start Python and
#: import the pipeline first, which takes a few seconds on a warm machine and
#: can take much longer on a cold one, or a busy test runner.
FIRST_WORD_S = 180.0

#: Seconds of silence after that before the child is taken to be stuck. It
#: sends a heartbeat every `run_events.HEARTBEAT_S` (half a second) whatever
#: it is doing, paused included, so a minute with nothing at all is not a slow
#: file - it is a child that has stopped answering. 2026-09-29: the first
#: Windows CI run with the child indexer waited ten minutes on a child that
#: never said anything, and the test runner ended the whole suite there.
SILENCE_S = 60.0

#: The longest line read from the child. A progress event is a few kilobytes;
#: this only stops a runaway line (a library dumping binary to stdout) from
#: holding the window's memory. A longer "line" is read in pieces, none of
#: which parse, so it is ignored rather than misread.
LINE_LIMIT_BYTES = 16 * 1024 * 1024

#: The file the child's standard error goes to, in the log folder: its log
#: lines and, if it crashes, Python's last words. **A file rather than a
#: second pipe** so nothing in the window has to keep reading it (a pipe that
#: nobody drains fills up and stalls the child), and so it is still there to
#: open after a crash. Overwritten by each run.
CHILD_STDERR_NAME = "index-process-stderr.log"

#: How many lines from the end of that file go into a crash's details.
_STDERR_TAIL_LINES = 25

#: Every child this process has started and not yet seen end. Weak, so a run
#: object nobody holds any more does not stay alive because of this set.
_LIVE: "weakref.WeakSet[ChildIndexRun]" = weakref.WeakSet()
_LIVE_LOCK = threading.Lock()


def _end_descendants(pid: Optional[int]) -> None:
    """Kill every process below `pid`. Best effort: without psutil, or once
    they have gone, there is nothing to do."""
    if not pid:
        return
    try:
        import psutil

        for child in psutil.Process(pid).children(recursive=True):
            try:
                child.kill()
            except psutil.Error:
                pass
    except Exception:                    # noqa: BLE001 - never block the ending
        pass


def live_children() -> list["ChildIndexRun"]:
    """The child runs still going, for closing and for tests."""
    with _LIVE_LOCK:
        return [run for run in _LIVE if run.running]


def end_all_children(grace_s: float = STOP_GRACE_S) -> int:
    """Stop every child still running: ask, wait `grace_s`, then end it.

    Called as the window closes, after the index pool has been given its own
    grace. Returns how many had to be ended by force (0 on a clean close).
    Never raises - this runs in a process that is exiting.
    """
    forced = 0
    for run in live_children():
        try:
            if run.shutdown(grace_s) != "stopped":
                forced += 1
        except Exception as exc:                 # noqa: BLE001 - closing never raises
            _log.warning("could not end the indexing process: {}", exc)
    return forced


# ---------------------------------------------------------------------------
# Building the command line and the environment
# ---------------------------------------------------------------------------

def _env_text(value: Any) -> Optional[str]:
    """One setting as the text `config.load_settings` reads back, or None."""
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def settings_environment(settings: Any) -> dict[str, str]:
    r"""The window's settings, as environment variables for the child.

    **Why not just the `.env` file.** The child is told which `.env` to read
    (`--env`), but the window's live `Settings` can differ from the file: the
    Tuning shelf applies a change to the window's copy the moment it is made
    (`_limits_changed`), and the in-process run has always used that copy.
    `config.load_settings` lets an environment variable override the file for
    every key it knows (`SETTING_KEYS`), so passing each one here makes the
    child's settings the window's settings, value for value - the "same
    settings as the in-process run" the order asks for.
    """
    from app.core.config import SETTING_KEYS

    out: dict[str, str] = {}
    for key in SETTING_KEYS:
        text = _env_text(getattr(settings, key.lower(), None))
        if text is not None:
            out[key] = text
    return out


def child_command(roots: Iterable[Any], *, env_file: Any = None,
                  python: Optional[str] = None, prune: bool = True,
                  recheck_archives: bool = False, workers: int = 0,
                  cloud_content_keys: Iterable[str] = (),
                  first: Iterable[Any] = (),
                  extra: Iterable[str] = ()) -> list[str]:
    r"""The argument list that starts the indexer as a child. A list, never a
    string, so no shell ever parses a folder name (Windows and macOS alike).

    `python` defaults to the interpreter running the window (`sys.executable`,
    which is `pythonw.exe` for the window on Windows - it has no console, so
    no console window flashes up for the child). Each option mirrors something
    `IndexController._index_resolved` puts into the in-process `Pipeline`:

    * `roots` - the folders the window chose, named explicitly so the child
      cannot fall back to a different saved list;
    * `prune=False` -> `--no-prune`, for a run over some folders only;
    * `recheck_archives` -> `--recheck-archives` ("Rescan archived folders now");
    * `workers` -> `--workers`, the number `resolve_for_run` already chose;
    * `cloud_content_keys` -> `--cloud-content-key`, the per-folder cloud
      opt-ins exactly as the window stores them (already normalised);
    * `first` -> `--first`, once per folder marked "Index this folder first",
      **in order** - the order is the setting (2026-09-29).
    """
    from app.core.osbridge.stdio import own_python

    argv = [python or own_python(), "-m", "app.cli", "index",
            "--events", "jsonl", "--run-owner", "window"]
    if env_file:
        argv += ["--env", str(env_file)]
    if not prune:
        argv.append("--no-prune")
    if recheck_archives:
        argv.append("--recheck-archives")
    if workers and int(workers) > 0:
        argv += ["--workers", str(int(workers))]
    for key in sorted(str(k) for k in cloud_content_keys):
        argv += ["--cloud-content-key", key]
    for folder in first:
        argv += ["--first", str(Path(folder))]
    argv += [str(item) for item in extra]
    # `--` so a folder whose name starts with a dash is read as a folder.
    argv.append("--")
    argv += [str(Path(root)) for root in roots]
    return argv


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------

class ChildIndexRun:
    r"""An index run in a child process, shaped like the `Pipeline` it replaces.

    `IndexWorker` and `IndexingView` use a pipeline through a handful of names,
    and this answers each of them:

    * `run(on_progress=...)` - start the child, turn each `progress` event into
      an `IndexStats` and hand it to `on_progress`, and return the final
      `IndexStats` (or raise the child's `AppError`, or a plain-words one if
      it died). Blocks for the length of the run, on `IndexWorker`'s thread.
    * `request_stop()`, `pause()`, `resume()` - one word each on the child's
      standard input. Called from the window's thread; a pipe write of a few
      bytes, which never waits (the child reads its input continuously).
    * `config.limits.low_priority`, `store`, `thread_priority_only`,
      `run_owner`, `ui_lag` - attributes the window reads or sets on a
      pipeline. They are accepted and mostly ignored: priority, the run lock
      and the run's owner are the child's business now (see the module
      docstring), and the window's lateness is no longer the indexer's
      concern - the two no longer share a lock for it to yield.
    * `takes_its_own_run_lock` - True, which tells `IndexWorker` not to take
      the lock itself: the child must be able to take it.
    """

    #: See the class docstring: the child takes the run lock, not the window.
    takes_its_own_run_lock = True

    def __init__(self, argv: list[str], *, env: Optional[dict[str, str]] = None,
                 cwd: Any = None, stderr_path: Any = None,
                 low_priority: bool = True,
                 popen: Callable[..., Any] = subprocess.Popen) -> None:
        self.argv = list(argv)
        self.env = dict(env) if env is not None else None
        self.cwd = str(cwd) if cwd else None
        self.stderr_path = Path(stderr_path) if stderr_path else None
        # The attributes a window reads or sets on a pipeline - see above.
        self.config = SimpleNamespace(limits=SimpleNamespace(low_priority=bool(low_priority)))
        self.store = None
        #: The window's own store, for **reading** while the child writes: the
        #: Indexing page's status counts refresh from it during the run (order
        #: 0z A3). Kept apart from `store`, which stays None so that nothing
        #: in the window takes this run for one it may write through or lock.
        self.read_store: Any = None
        self.thread_priority_only = False
        self.run_owner: Any = None
        self.ui_lag: Any = None

        self._popen = popen
        self._proc: Any = None
        self._lock = threading.Lock()
        self._stop_requested = False
        self._paused = False
        self._ended = threading.Event()
        self._last_stats: Any = None
        #: `time.monotonic()` of the last line of any kind from the child.
        self.last_heard: Optional[float] = None
        #: Set when the child was ended for saying nothing: how long it was
        #: silent. Named in the error, so "it crashed" and "it hung" differ.
        self.silent_s: Optional[float] = None
        self.returncode: Optional[int] = None

    # -- what the window asks ---------------------------------------------------

    @property
    def running(self) -> bool:
        """Has the child been started, and not yet been seen to end?"""
        return self._proc is not None and not self._ended.is_set()

    @property
    def pid(self) -> Optional[int]:
        """The child's process id, or None before it has started (the bench
        samples the child's memory by it)."""
        proc = self._proc
        return getattr(proc, "pid", None) if proc is not None else None

    @property
    def paused_by_person(self) -> bool:
        """Whether Pause was the last of Pause/Resume sent. The page reads the
        child's own answer from each progress event; this is for callers that
        ask the pipeline directly."""
        return self._paused

    def request_stop(self) -> None:
        """Ask the child to finish the file in hand and end, keeping everything.

        Remembered as well as sent: a Stop pressed in the instant before the
        child exists is sent the moment it does (see `run`).
        """
        with self._lock:
            self._stop_requested = True
            self._paused = False
        self._send("stop")

    def pause(self) -> None:
        """Hold the child's run where it is. Nothing ends and nothing is lost."""
        with self._lock:
            self._paused = True
        self._send("pause")

    def resume(self) -> None:
        """Let the child's run carry on. The machine's own ceilings still apply."""
        with self._lock:
            self._paused = False
        self._send("resume")

    def force_skip(self, slot_id: Any) -> bool:
        """Work order 0z lane B: Force skip reader `slot_id`'s current file.

        `skip <reader>` on the child's standard input; the child's
        `Pipeline.force_skip` does the rest. True when the line was sent - the
        child alone knows whether that reader still had a file, and the next
        progress tick shows it either way.
        """
        try:
            number = int(str(slot_id).strip())
        except (TypeError, ValueError):
            return False
        if not self.running:
            return False
        self._send(f"skip {number}")
        return True

    def _send(self, word: str) -> None:
        """One command line to the child. **Never raises and never waits.**

        A child that has already gone has a closed pipe; the write fails, and
        `run` is about to report how it ended anyway, so the failure is only
        logged.
        """
        with self._lock:
            proc = self._proc
            stdin = getattr(proc, "stdin", None) if proc is not None else None
            if stdin is None:
                return
            try:
                stdin.write((word + "\n").encode("ascii"))
                stdin.flush()
            except (OSError, ValueError) as exc:
                _log.debug("could not send {!r} to the indexing process: {}", word, exc)

    # -- the run ----------------------------------------------------------------

    def run(self, on_progress: Optional[Callable[[Any], None]] = None) -> Any:
        """Start the child, relay its progress, and return its final stats.

        Raises `AppErrorException` with the child's own error when it reports
        one, and with `ERR_INDEX_PROCESS_ENDED` when it ends without a
        `finished` line - naming the file it was reading if a progress event
        had said.
        """
        from app.core.errors import AppErrorException
        from app.index.run_events import (
            EVENT_FINISHED, EVENT_HELLO, EVENT_PROGRESS, PROTOCOL,
            StatsRebuilder, parse_line,
        )

        rebuilder = StatsRebuilder()
        finished: Optional[dict] = None
        self._start()
        proc = self._proc
        # **Read on a thread of its own, so silence has a limit.** A pipe read
        # cannot time out on Windows, so the lines are handed over through a
        # queue and this loop waits on the queue instead. A child that says
        # nothing for `FIRST_WORD_S` at the start, or `SILENCE_S` after that,
        # is ended and reported exactly as a crash is - with its error output -
        # rather than waited on for ever.
        lines: "queue.Queue[bytes]" = queue.Queue()

        def pump() -> None:
            """Reader thread: every line of the child's output onto `lines`,
            then one empty bytes so the loop below knows the pipe closed."""
            try:
                while True:
                    line = proc.stdout.readline(LINE_LIMIT_BYTES)
                    lines.put(line)
                    if not line:
                        return
            except (OSError, ValueError):
                lines.put(b"")

        threading.Thread(target=pump, name="index-child-reader", daemon=True).start()
        wait_s = FIRST_WORD_S
        try:
            while True:
                try:
                    raw = lines.get(timeout=wait_s)
                except queue.Empty:
                    self.silent_s = wait_s
                    _log.warning("the indexing process sent nothing for {:.0f}s; ending it",
                                 wait_s)
                    self._force_end(proc)
                    break
                if not raw:
                    break                                   # the pipe closed
                wait_s = SILENCE_S
                self.last_heard = time.monotonic()
                if not raw.endswith(b"\n"):
                    # **Only complete lines are events.** A line without its
                    # end is the last thing a dying child wrote, or a piece of
                    # an over-long one; either way it is not to be trusted.
                    _log.debug("ignored an incomplete line from the indexing process")
                    continue
                event = parse_line(raw)
                if event is None:
                    _log.debug("ignored a non-event line from the indexing process: {}",
                               raw[:200])
                    continue
                kind = event.get("event")
                if kind == EVENT_HELLO and event.get("protocol") != PROTOCOL:
                    _log.warning("the indexing process speaks protocol {}, this window {}",
                                 event.get("protocol"), PROTOCOL)
                elif kind == EVENT_PROGRESS:
                    stats = rebuilder.rebuild(event.get("stats"), sent_at=event.get("t"),
                                              received_at=self.last_heard)
                    self._last_stats = stats
                    if on_progress is not None:
                        on_progress(stats)
                elif kind == EVENT_FINISHED:
                    finished = event
                    if isinstance(event.get("stats"), dict):
                        self._last_stats = rebuilder.rebuild(
                            event.get("stats"), sent_at=event.get("t"),
                            received_at=self.last_heard)
        finally:
            self.returncode = self._wait_for_exit()
            self._ended.set()
            with _LIVE_LOCK:
                _LIVE.discard(self)

        if finished is not None:
            error = finished.get("error")
            if error is not None:
                from app.index.run_events import from_json_safe

                rebuilt = from_json_safe(error)
                raise AppErrorException(rebuilt if hasattr(rebuilt, "code")
                                        else self._ended_error())
            if self._last_stats is not None:
                return self._last_stats
        raise AppErrorException(self._ended_error())

    def _start(self) -> None:
        """Start the child. Its standard error goes to a file (see
        `CHILD_STDERR_NAME`); without one, to nowhere."""
        stderr: Any = subprocess.DEVNULL
        handle = None
        if self.stderr_path is not None:
            try:
                self.stderr_path.parent.mkdir(parents=True, exist_ok=True)
                handle = open(self.stderr_path, "wb")      # noqa: SIM115 - closed below
                stderr = handle
            except OSError as exc:
                _log.debug("the indexing process's error log could not be opened: {}", exc)
        _log.info("starting the indexing process: {}", " ".join(self.argv[1:]))
        try:
            proc = self._popen(
                self.argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=stderr, cwd=self.cwd, env=self.env)
        finally:
            if handle is not None:
                handle.close()           # the child has its own copy now
        with self._lock:
            self._proc = proc
            stop_first = self._stop_requested
            pause_first = self._paused
        with _LIVE_LOCK:
            _LIVE.add(self)
        # A Stop or Pause pressed while the child was being started had no
        # pipe to go down; it goes now.
        if stop_first:
            self._send("stop")
        elif pause_first:
            self._send("pause")

    def _wait_for_exit(self) -> Optional[int]:
        """The child's exit code, once its output has closed. Bounded: a child
        that closed its output but will not exit is ended."""
        proc = self._proc
        if proc is None:
            return None
        try:
            return proc.wait(timeout=TERMINATE_WAIT_S)
        except subprocess.TimeoutExpired:
            _log.warning("the indexing process closed its output but did not exit; ending it")
            return self._force_end(proc)
        finally:
            for stream in (getattr(proc, "stdin", None), getattr(proc, "stdout", None)):
                try:
                    if stream is not None:
                        stream.close()
                except (OSError, ValueError):
                    pass

    def _force_end(self, proc: Any) -> Optional[int]:
        """Terminate, wait, then kill. Returns the exit code if one arrived.

        2026-09-29: the child's own processes go too. Windows CI ended its job
        with two indexers still running that no test held any more; ending
        only the process we started can leave what it started (a reader
        process, or the real interpreter behind a venv's launcher) alive and
        still writing to our pipe.
        """
        _end_descendants(getattr(proc, "pid", None))
        for step in (proc.terminate, proc.kill):
            try:
                step()
            except OSError:
                pass
            try:
                return proc.wait(timeout=TERMINATE_WAIT_S)
            except subprocess.TimeoutExpired:
                continue
        return None

    def shutdown(self, grace_s: float = STOP_GRACE_S) -> str:
        r"""The window is closing: stop cleanly if possible, end it if not.

        Returns `"stopped"` (it ended by itself within `grace_s`),
        `"terminated"` (it had to be ended), or `"not running"`. Waits on the
        child process, not on `run`'s thread, so it works whether or not that
        thread is still reading.
        """
        proc = self._proc
        if proc is None or self._ended.is_set() or proc.poll() is not None:
            return "not running"
        self.request_stop()
        try:
            proc.wait(timeout=max(0.0, float(grace_s)))
            return "stopped"
        except subprocess.TimeoutExpired:
            pass
        _log.warning("the indexing process did not stop within {:.0f}s of the window "
                     "closing; ending it", grace_s)
        self._force_end(proc)
        return "terminated"

    # -- saying what happened -----------------------------------------------------

    def _ended_error(self) -> Any:
        """`ERR_INDEX_PROCESS_ENDED`, with the file it was reading if known."""
        from app.core.errors import make_error

        current = str(getattr(self._last_stats, "current", "") or "").strip()
        where = f" while reading {current}" if current else ""
        code = self.returncode
        details = f"exit code {code}" if code is not None else "exit code unknown"
        if self.silent_s is not None:
            details = (f"it sent nothing for {self.silent_s:.0f}s and was ended; "
                       + details)
        if code is not None and code < 0:
            # POSIX reports "ended by a signal" as a negative code: -9 is a kill.
            details += f" (ended by signal {-code})"
        tail = self._stderr_tail()
        if tail:
            details += "\nlast lines of its error output:\n" + tail
        return make_error("ERR_INDEX_PROCESS_ENDED", "index.child", where=where,
                          file=current, details=details)

    def _stderr_tail(self) -> str:
        if self.stderr_path is None:
            return ""
        try:
            with open(self.stderr_path, "rb") as handle:
                handle.seek(0, os.SEEK_END)
                size = handle.tell()
                handle.seek(max(0, size - 16_384))
                text = handle.read().decode("utf-8", errors="replace")
        except OSError:
            return ""
        return "\n".join(text.splitlines()[-_STDERR_TAIL_LINES:])
