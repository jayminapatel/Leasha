r"""One file per run, so "it did something odd" can be read rather than retold.

Layer: L0

Asked for directly: *"for testing create detailed log files which for every run
you can check on the project folder"*. The point is the round trip. A report of
the form *"this seems stuck"* is a good report and it has found real faults here,
but answering it costs a message asking what the console said, another asking
which command was run, and a third asking what the settings were - by which time
the run is over and the console has scrolled.

**What was missing was never the logging.** `logs\app\` already holds everything
at DEBUG. What it does not hold is a *boundary*: one run's lines sit in the same
daily file as the twenty runs around it, interleaved with a window session, and
finding "the run that went wrong" means guessing at timestamps. So this adds no
new logging. It adds a sink pointed at a file named after the run, and a header
and footer around it.

**Four things the daily log cannot tell you**, all of which have cost time here:

*What the settings actually were.* `doctor` once reported its own hardcoded
defaults rather than the loaded configuration, and a measurement was attributed
to the wrong model for a week. The resolved values are written from the same
object the run is using - not from `.env`, and not from the defaults.

*Which threads were still alive at the end.* The window closing without the
process exiting was diagnosed by reasoning about `concurrent.futures`; the
footer names them outright, and marks the non-daemon ones, because those are
the ones the interpreter waits for.

*How many errors, grouped by code.* A run producing four hundred
`ERR_CONVERTER_MISSING` and one `ERR_DB_LOCKED` reads, in a flat log, as four
hundred and one problems. The tally says it is two.

*Where the time went.* `stage()` records a phase; the footer adds them up. A
run that took nine minutes is not a finding. A run that spent eight of them in
one stage is.

**Never the cause of a failure.** Every method swallows everything. A run log
that raises would turn a small fault into a crash, inside the very run somebody
is trying to understand - the rule `debug_recorder` follows, for the reason.

**One handle, one writer.** The header, the footer and every captured log line
go through `_write`, holding one lock. A loguru file sink alongside hand-written
sections would be two processes appending to one file and trusting the order,
which is how a footer ends up in the middle.

**Values are written, not redacted.** This file stays on the owner's machine and
is for the owner; `diagnose` is the command that produces something shareable,
and it does the redacting. But keys whose *names* say they hold a credential are
masked anyway, because "it never leaves the machine" stops being true the moment
somebody attaches one to an email.
"""

from __future__ import annotations

import os
import platform
import sys
import threading
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from loguru import logger

__all__ = ["KEEP_RUNS", "RUNS_SUBDIR", "RunLog", "current", "start_run"]

#: Where run files live. A folder of its own under `logs\`, so the daily logs
#: stay a chronology and this stays a list of runs.
RUNS_SUBDIR = "runs"

#: How many to keep. Enough that the interesting run is still there three runs
#: later; few enough that the folder can be read at a glance.
KEEP_RUNS = 40

#: Key-name fragments that mask a value however local the file is.
_MASK = ("TOKEN", "SECRET", "PASSWORD", "PASSWD", "PWD", "API_KEY", "APIKEY",
         "CREDENTIAL", "PRIVATE")


def _masked(key: str, value: Any) -> Any:
    return "<masked>" if any(f in key.upper() for f in _MASK) else value


def _slug(text: str) -> str:
    """A file-name-safe fragment. Windows rejects most of what a command line
    can contain, and a run log that failed to open is the least useful
    outcome available."""
    kept = [c if (c.isalnum() or c in "-_") else "-" for c in str(text).strip()]
    # 40 characters: with the `run-YYYYMMDD-HHMMSS-` prefix and `.log` the
    # name stays well inside Windows' 260-character path limit even under a
    # deep LOG_PATH, and a command line is recognisable from its first words.
    return ("".join(kept).strip("-") or "run")[:40]


class RunLog:
    """One run: a file, a sink feeding it, and a footer written on the way out.

    Usable as a context manager or explicitly via `finish()`. Both are safe to
    call twice - `closeEvent` in Qt fires more than once, and a CLI command that
    raises must not leave the sink attached for whatever runs next.
    """

    def __init__(self, path: Path, command: str) -> None:
        self.path = Path(path)
        self.command = command
        self.started_at = time.perf_counter()
        self.started_wall = datetime.now()
        self.errors: Counter[str] = Counter()
        self.stages: list[tuple[str, float]] = []
        self._handle: Any = None
        self._lock = threading.Lock()
        self._sink_id: Optional[int] = None
        self._finished = False
        #: Whether the "Log" heading has been written yet. It cannot go in the
        #: header, because the settings section arrives *after* the run opens -
        #: a command has to load its configuration before it has one to report.
        #: So the heading is written by whichever comes first, and exactly once.
        self._log_heading = False

    # -- writing -------------------------------------------------------------

    def _write(self, text: str = "") -> None:
        """The only thing that touches the file. Never raises."""
        try:
            with self._lock:
                if self._handle is None:
                    return
                self._handle.write(text + "\n")
        except Exception:  # noqa: BLE001 - a run log must never fail a run
            pass

    def note(self, key: str, value: Any) -> None:
        """Record one fact the run learned rather than loaded - a row count, a
        model that was substituted, a path that was skipped."""
        self._write(f"  {key!s:<26} {_masked(str(key), value)}")

    def section(self, title: str) -> None:
        """An underlined heading, preceded by a blank line. Never raises."""
        self._write()
        self._write(title)
        self._write("-" * len(title))

    def stage(self, name: str, seconds: float) -> None:
        """Record one timed phase for the footer's Timings table. Never raises:
        a bad value is dropped rather than allowed to end the run."""
        try:
            self.stages.append((str(name), float(seconds)))
        except Exception:  # noqa: BLE001
            pass

    def timer(self, name: str) -> _Stage:
        """`with run.timer("embed"):` - the same thing, measured for you."""
        return _Stage(self, name)

    def settings(self, settings: Any) -> None:
        """The configuration in force, written from the object the run is using.

        Called after loading rather than before, because the whole value of the
        section is that it is not the defaults.
        """
        try:
            described = settings.describe()
        except Exception:  # noqa: BLE001
            described = {}
        if not described:
            return
        self.section("Settings in force")
        for key in sorted(described):
            self.note(key, described[key])

    def _begin_log(self) -> None:
        """Written by the first line that arrives, never in advance. An empty
        heading reads as a section that failed to fill."""
        if not self._log_heading:
            self._log_heading = True
            self.section("Log")

    # -- lifecycle -----------------------------------------------------------

    def open(self, *, argv: Any = None) -> RunLog:
        """Create the file, write the header, start capturing. Returns self."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # Line buffered: the run worth reading is the one that ended badly,
            # and a buffered log loses exactly the last few lines that mattered.
            self._handle = self.path.open("a", encoding="utf-8", buffering=1)
        except Exception:  # noqa: BLE001
            self._handle = None
            return self

        self._write(f"run      : {self.command}")
        self._write(f"started  : {self.started_wall:%Y-%m-%d %H:%M:%S}")
        self._write(f"pid      : {os.getpid()}")

        self.section("Versions")
        try:
            from app.core.version import version

            self.note("app", version())
        except Exception:  # noqa: BLE001
            self.note("app", "unreadable")
        self.note("python", sys.version.split()[0])
        self.note("platform", platform.platform())
        self.note("executable", sys.executable)
        self.note("in_venv", sys.prefix != getattr(sys, "base_prefix", sys.prefix))

        if argv is not None:
            self.section("Command line")
            self._write(f"  {' '.join(str(a) for a in argv)}")

        self._attach()
        return self

    def _attach(self) -> None:
        """One sink, feeding `_write`, tallying as it goes.

        **The line is built from the record, not from a format string.** A
        format naming `{extra[component]}` raises `KeyError` on any line logged
        before `logger.configure` has supplied the defaults - and this sink is
        attached deliberately *early*, so that is not a corner case, it is the
        first part of every run. Loguru reports the failure to stderr and drops
        the line, which is a log that loses exactly the entries written before
        anything was set up.

        The tally reads `extra[error_code]`, never the message text - the code
        is a structured field precisely so nothing has to parse prose to group
        by cause.
        """
        def sink(message: Any) -> None:
            self._begin_log()
            record = getattr(message, "record", None)
            if record is None:
                self._write(str(message).rstrip("\n"))
                return
            try:
                extra = record.get("extra") or {}
                level = record["level"].name
                if record["level"].no >= 30:            # WARNING and above
                    code = extra.get("error_code") or "-"
                    self.errors[code if code != "-" else "(uncoded)"] += 1
                line = (f"{record['time']:%H:%M:%S.%f}"[:-3] + " | "
                        f"{level:<8} | {extra.get('component', 'app')} | "
                        f"{extra.get('error_code', '-')} | {record['message']}")
                self._write(line)
                if record.get("exception"):
                    import traceback

                    exception = record["exception"]
                    self._write("".join(traceback.format_exception(
                        exception.type, exception.value,
                        exception.traceback)).rstrip())
            except Exception:  # noqa: BLE001
                # Losing the shape of a line is not a reason to lose the line.
                try:
                    self._write(str(message).rstrip("\n"))
                except Exception:  # noqa: BLE001
                    pass

        try:
            self._sink_id = logger.add(sink, level="DEBUG", format="{message}",
                                       backtrace=True, diagnose=False)
        except Exception:  # noqa: BLE001
            self._sink_id = None

    def reattach(self) -> None:
        """Put the sink back after somebody has cleared every handler.

        **`setup_logging` calls `logger.remove()`**, which takes this sink with
        it - and it runs *after* the run log opens, because a command has to
        load its settings before it knows where logs live. Without this the
        file would contain a header, a footer, and none of the run: a failure
        that looks exactly like a working feature until you open one.
        """
        if self._finished or self._handle is None:
            return
        if self._sink_id is not None:
            try:
                logger.remove(self._sink_id)
            except Exception:  # noqa: BLE001
                pass
            self._sink_id = None
        self._attach()

    def unhandled(self, exc: BaseException) -> None:
        """Record an exception nobody caught. The sink never sees these."""
        self._write()
        self._write(f"UNHANDLED {type(exc).__name__}: {exc}")

    def finish(self, exit_code: Any = None) -> Path:
        """Write the footer, detach, prune. Safe to call more than once."""
        if self._finished:
            return self.path
        self._finished = True

        if self._sink_id is not None:
            try:
                logger.remove(self._sink_id)
            except Exception:  # noqa: BLE001
                pass
            self._sink_id = None

        elapsed = time.perf_counter() - self.started_at

        if self.stages:
            self.section("Timings")
            for name, seconds in self.stages:
                self._write(f"  {name:<26} {seconds:9.3f}s")
            total = sum(seconds for _name, seconds in self.stages)
            self._write(f"  {'(stages total)':<26} {total:9.3f}s of "
                        f"{elapsed:.3f}s wall")

        self.section("Errors by code")
        if self.errors:
            for code, count in self.errors.most_common():
                self._write(f"  {code:<30} {count}")
            self._write()
            self._write("  A large count against one code is one problem, not "
                        "that many. Read the codes, not the total.")
        else:
            self._write("  none")

        # **The threads still running.** A non-daemon thread here is the entire
        # explanation for a process that will not exit after its window closes.
        self.section("Threads at exit")
        try:
            here = threading.current_thread()
            alive = [t for t in threading.enumerate() if t is not here]
            for thread in sorted(alive, key=lambda t: t.name):
                flag = "daemon" if thread.daemon else "NON-DAEMON"
                self._write(f"  {thread.name:<30} {flag:<11} "
                            f"alive={thread.is_alive()}")
            lingering = [t for t in alive
                         if not t.daemon and t is not threading.main_thread()]
            if lingering:
                self._write()
                self._write("  NON-DAEMON threads are still running. The "
                            "interpreter joins every one of them before it "
                            "exits, which is what a process that hangs after "
                            "its window has closed looks like from here.")
        except Exception:  # noqa: BLE001
            self._write("  unreadable")

        self.section("Result")
        self._write(f"  {'exit code':<26} {exit_code}")
        self._write(f"  {'elapsed':<26} {elapsed:.3f}s")
        self._write(f"  {'finished':<26} {datetime.now():%Y-%m-%d %H:%M:%S}")

        try:
            with self._lock:
                if self._handle is not None:
                    self._handle.close()
                    self._handle = None
        except Exception:  # noqa: BLE001
            pass

        global _current
        if _current is self:
            _current = None

        _prune(self.path.parent)
        return self.path

    # -- context manager -----------------------------------------------------

    def __enter__(self) -> RunLog:
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        if exc is not None:
            self._write()
            self._write(f"UNHANDLED {exc_type.__name__}: {exc}")
        self.finish("exception" if exc is not None else None)
        return False   # never swallow


class _Stage:
    """What `RunLog.timer` returns. Times its block, records it, and lets
    anything raised pass through - a timing is not a reason to hide a fault.
    A failed stage is recorded as one, because how long something took before
    it broke is usually the question."""

    def __init__(self, run: RunLog, name: str) -> None:
        self._run = run
        self._name = name
        self._started = 0.0

    def __enter__(self) -> _Stage:
        self._started = time.perf_counter()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        name = self._name if exc is None else f"{self._name} (failed)"
        self._run.stage(name, time.perf_counter() - self._started)
        return False


def _prune(folder: Path, keep: int = KEEP_RUNS) -> None:
    """Delete all but the newest `keep`. Housekeeping never raises: a folder
    that cannot be tidied is not a reason to fail the run that filled it."""
    try:
        files = sorted(Path(folder).glob("run-*.log"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
        for stale in files[keep:]:
            try:
                stale.unlink()
            except OSError:
                pass
    except Exception:  # noqa: BLE001
        pass


#: The run in progress, or None. A run log is process-wide by nature, and the
#: alternative is threading one object through five layers to let the indexer
#: record a stage timing.
_current: Optional[RunLog] = None


def current() -> Optional[RunLog]:
    """The open run log, or None. Call sites are expected to check."""
    return _current


def start_run(log_dir: Path, command: str, *, argv: Any = None) -> RunLog:
    """Open a run log under `log_dir\\runs\\`. The one entry point.

    Named `run-YYYYMMDD-HHMMSS-<command>.log`, so the folder sorts by time and
    reads as a list of what was done.
    """
    global _current
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = Path(log_dir) / RUNS_SUBDIR / f"run-{stamp}-{_slug(command)}.log"
    run = RunLog(path, command).open(argv=argv)
    _current = run
    return run
