"""Logging setup.

Layer: L0

Two sinks:
  console  INFO   what the operator sees while something is running
  file     DEBUG  logs/app_{time}.log, a new file at midnight, 14 days retained

AppError codes are logged as a structured field (`error_code`), so the
skipped-files panel in Layer 5 can group thousands of skips by cause without
parsing message text.
"""

from __future__ import annotations

import sys
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from loguru import logger

from app.core.errors import AppError

__all__ = ["setup_logging", "log_app_error", "logger", "recent_lines",
           "RECENT_LIMIT", "open_log_files"]

#: How many recent lines are kept in memory for the debug pane in Settings.
#:
#: **This exists because the console is going away.** `leasha.cmd` launches the
#: window with `pythonw.exe`, which has no console at all - so the running
#: commentary that used to appear in a terminal has nowhere to go, and a person
#: whose index looks stuck has nothing to look at. The file log has always had
#: everything, but "open the logs folder and find today's file" is not an answer
#: somebody reaches for while wondering whether the application has hung.
#:
#: A few hundred lines is nothing in memory and covers minutes of an index run.
RECENT_LIMIT = 300

_CONSOLE_FORMAT = (
    "<green>{time:HH:mm:ss}</green> "
    "<level>{level: <8}</level> "
    "<cyan>{extra[component]}</cyan> "
    "<level>{message}</level>"
)

_FILE_FORMAT = (
    "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | "
    "{extra[component]} | {extra[error_code]} | "
    "{name}:{function}:{line} | {message}"
)

_configured = False

#: The ring the debug pane reads. Written by a loguru sink, so it holds exactly
#: what the file log holds, formatted the same way.
_recent: deque = deque(maxlen=RECENT_LIMIT)


def _remember(message: Any) -> None:
    """A loguru sink that keeps the line rather than writing it anywhere.

    **Never raises.** A sink that throws takes the logging system with it, and
    losing the log is how a diagnosable problem becomes an undiagnosable one.
    """
    try:
        _recent.append(str(message).rstrip("\n"))
    except Exception:                            # noqa: BLE001
        return


def recent_lines(limit: int = 50) -> list[str]:
    """The last `limit` log lines, oldest first. For the debug pane."""
    if limit <= 0:
        return []
    return list(_recent)[-int(limit):]


#: Everything under logs/ has a fixed home, so troubleshooting is a matter of
#: knowing which folder to look in rather than sifting one flat directory.
LOG_SUBDIRS = {
    "app": "Application logs, one file per day. Start here.",
    "errors": "Errors only, one JSON object per line. Machine-readable.",
    "runs": "One file per command or window session, with its settings and result.",
    "install": "Installer transcripts, one per run.",
    "crash": "Unhandled crash reports.",
    "diagnostics": "Diagnostic bundles produced by 'app.cli diagnose'.",
}


def ensure_log_dirs(log_dir: Path) -> dict[str, Path]:
    """Create the log folder structure and return the paths by name."""
    made: dict[str, Path] = {}
    for name in LOG_SUBDIRS:
        target = Path(log_dir) / name
        target.mkdir(parents=True, exist_ok=True)
        made[name] = target

    lines = [
        "Log folders",
        "===========",
        "",
    ]
    width = max(len(n) for n in LOG_SUBDIRS)
    for name, description in LOG_SUBDIRS.items():
        lines.append(f"  {name.ljust(width)}  {description}")
    lines += [
        "",
        "If something goes wrong, run this and send the resulting zip:",
        "",
        "    venv\\Scripts\\python.exe -m app.cli diagnose",
        "",
        "It gathers the environment report, both store summaries, recent",
        "logs and the installed package versions into one file.",
        "",
        "See docs/TROUBLESHOOTING.md for what each file means.",
        "",
    ]
    wanted = "\n".join(lines)

    # **Rewritten when it no longer matches, not only when it is absent.**
    # It was written once and never again, so adding a folder left a file
    # confidently describing a structure that no longer existed - and a
    # generated document that has quietly gone stale is worse than none,
    # because somebody will act on it. Nothing here is hand-edited: it is
    # produced from `LOG_SUBDIRS`, which is the thing that changes.
    readme = Path(log_dir) / "README.txt"
    try:
        if readme.read_text(encoding="utf-8") != wanted:
            readme.write_text(wanted, encoding="utf-8")
    except OSError:
        try:
            readme.write_text(wanted, encoding="utf-8")
        except OSError:
            pass          # a missing README is never a reason not to log
    return made


def setup_logging(
    log_dir: Path,
    *,
    console_level: str = "INFO",
    file_level: str = "DEBUG",
    retention: str = "14 days",
    rotation: str = "00:00",
    force: bool = False,
) -> Path:
    """Configure the sinks. Returns the application log file pattern.

    Idempotent: calling it twice does not double every line, which matters
    because both the CLI and the UI entry point call it.

    **`rotation` is a time of day, not a size (2026-10-05).** It was "10 MB".
    The window and its indexing process write the same `app_<day>.log`, and at
    10 MB whichever got there first tried to rename the file out of the way -
    which Windows refuses while the other has it open. From then on every line
    that process logged failed the same way and was lost: on the day it was
    found the file held 37 lines after 00:48, and the indexing process's
    stderr was a wall of "Logging error in Loguru Handler ... PermissionError:
    [WinError 32]". Measured with a second handle on the file: size rotation
    kept 21 of 301 lines, midnight rotation 301 of 301. At midnight the new
    file has a new name, so nothing is renamed and nothing can be refused; a
    day's file is as large as the day was busy, and `retention` still removes
    old ones.
    """
    global _configured
    if _configured and not force:
        return Path(log_dir) / "app" / "app_{time}.log"

    logger.remove()

    # `component` and `error_code` are referenced by both formats, so they must
    # always exist. Binding defaults here means no call site has to remember.
    logger.configure(extra={"component": "app", "error_code": "-"})

    # **`sys.stderr` is `None` under `pythonw.exe`.** The window is launched
    # with it now, so there is no console to write to - and `logger.add(None)`
    # is not a no-op, it is a failure that takes the whole logging setup with
    # it. Guarded rather than removed, because the CLI still has a console and
    # the running commentary is most of what makes it usable.
    if sys.stderr is not None:
        logger.add(
            sys.stderr,
            level=console_level,
            format=_CONSOLE_FORMAT,
            colorize=True,
            backtrace=False,   # the AppError carries the detail; keep the console readable
            diagnose=False,    # never print local variables to a console: they leak file contents
        )

    # The in-memory ring the debug pane reads. Added before the file sink so a
    # failure to create the log directory still leaves something to look at -
    # which is exactly the situation somebody would be trying to diagnose.
    logger.add(
        _remember,
        level=file_level,
        format=_CONSOLE_FORMAT,
        colorize=False,
        backtrace=False,
        diagnose=False,
    )

    dirs = ensure_log_dirs(log_dir)

    pattern = dirs["app"] / "app_{time:YYYY-MM-DD}.log"
    logger.add(
        str(pattern),
        level=file_level,
        format=_FILE_FORMAT,
        rotation=rotation,
        retention=retention,
        encoding="utf-8",
        enqueue=True,      # safe when several worker threads log at once
        backtrace=True,
        diagnose=False,
    )

    # A separate errors-only sink in JSON Lines. One object per line means it
    # can be filtered, counted and grouped without parsing prose - which is how
    # you find "the same failure 400 times" in a 100GB run.
    logger.add(
        str(dirs["errors"] / "errors_{time:YYYY-MM-DD}.jsonl"),
        level="WARNING",
        format="{message}",
        serialize=True,
        rotation=rotation,
        retention=retention,
        encoding="utf-8",
        enqueue=True,
        backtrace=False,
        diagnose=False,
    )

    # **`logger.remove()` above cleared every handler, including the run log's.**
    # A run log opens before its command loads settings - it has to, because a
    # run that fails at configuration is the one most worth having a file for -
    # so this always runs second and would silently truncate it to a header and
    # a footer. Imported here rather than at module scope to keep the dependency
    # one-way.
    try:
        from app.core.runlog import current as _current_run

        run = _current_run()
        if run is not None:
            run.reattach()
    except Exception:  # noqa: BLE001 - logging must not fail over a log file
        pass

    _configured = True
    return pattern


def release_log_files() -> None:
    """Flush and close every log file this process has open, and forget the setup.

    Order 0x (2026-09-27). **Windows will not delete a file that is still open**,
    and loguru keeps its file sinks open for as long as the process lives. A
    caller that pointed logging at a temporary folder - the indexing benchmark
    does, so its own run log does not mix with the owner's - must let go of
    those files before it can remove the folder. Found when the benchmark's
    throwaway folder survived every run on the Windows CI while vanishing on
    Linux, which deletes open files without complaint.

    `logger.complete()` first, because the file sinks use `enqueue=True`: lines
    still waiting in the queue are written before the files close. Everything
    is removed, not just the files, and `_configured` is reset, so the next
    `setup_logging` call builds a fresh, complete setup rather than returning
    early with no sinks at all.
    """
    global _configured
    try:
        logger.complete()
    except Exception:  # noqa: BLE001 - tidying up must never raise
        pass
    logger.remove()
    _configured = False


def open_log_files() -> list[Path]:
    """The log files this process is writing to right now.

    **Asked, not reconstructed.** The Clear-logs button needs to leave these
    alone, and working out which they are from the folder listing would be wrong
    in two ordinary cases: the run log's name carries a timestamp and a process
    id rather than today's date, and a session started before midnight is still
    writing into yesterday's application log. A rule based on the date would
    delete the current run's own evidence on one side of midnight and keep a
    stale file on the other.

    Loguru does not publish its sinks' paths, so the two dated ones are derived
    from the same `ensure_log_dirs` layout that created them and the run log is
    taken from `runlog.current()`, which knows its own file. Returns whatever
    can be established; a file missed here is a file that will not delete, which
    `clear_logs` already reports rather than treats as a fault.
    """
    found: list[Path] = []
    try:
        from app.core.runlog import current as _current_run

        run = _current_run()
        if run is not None and getattr(run, "path", None):
            found.append(Path(run.path))
    except Exception:                            # noqa: BLE001
        pass

    try:
        from app.core.config import log_dir_for

        root = Path(log_dir_for())
        stamp = datetime.now().strftime("%Y-%m-%d")
        found.append(root / "app" / f"app_{stamp}.log")
        found.append(root / "errors" / f"errors_{stamp}.jsonl")
    except Exception:                            # noqa: BLE001
        pass

    return [item for item in found if item]


def component_logger(component: str) -> Any:
    """A logger bound to one component, e.g. 'indexer.pst'."""
    return logger.bind(component=component)


def log_app_error(error: AppError, *, level: Optional[str] = None) -> None:
    """Log an AppError with its code as a structured field.

    SKIP_CONTINUE errors default to WARNING: they are expected in a 100GB run
    and thousands of them are normal. Everything else defaults to ERROR.
    """
    if level is None:
        level = "WARNING" if not error.is_fatal else "ERROR"

    bound = logger.bind(component=error.component, error_code=error.code)
    bound.log(level, "{} | fix: {}", error.message, error.suggestion or "-")
    if error.details:
        bound.debug("detail for {}: {}", error.code, error.details)
