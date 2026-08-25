"""Logging setup.

Layer: L0

Two sinks:
  console  INFO   what the operator sees while something is running
  file     DEBUG  logs/app_{time}.log, 10MB rotation, 14 days retained

AppError codes are logged as a structured field (`error_code`), so the
skipped-files panel in Layer 5 can group thousands of skips by cause without
parsing message text.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Optional

from loguru import logger

from app.core.errors import AppError

__all__ = ["setup_logging", "log_app_error", "logger"]

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
    rotation: str = "10 MB",
    force: bool = False,
) -> Path:
    """Configure the sinks. Returns the application log file pattern.

    Idempotent: calling it twice does not double every line, which matters
    because both the CLI and the UI entry point call it.
    """
    global _configured
    if _configured and not force:
        return Path(log_dir) / "app" / "app_{time}.log"

    logger.remove()

    # `component` and `error_code` are referenced by both formats, so they must
    # always exist. Binding defaults here means no call site has to remember.
    logger.configure(extra={"component": "app", "error_code": "-"})

    logger.add(
        sys.stderr,
        level=console_level,
        format=_CONSOLE_FORMAT,
        colorize=True,
        backtrace=False,   # the AppError carries the detail; keep the console readable
        diagnose=False,    # never print local variables to a console: they leak file contents
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
