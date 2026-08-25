"""The desktop application's entry point.

Layer: L5

    venv\\Scripts\\python.exe -m app.main
    venv\\Scripts\\python.exe -m app.main --debug   # record this session to logs\\sessions\\

Startup order matters and is deliberate:

1. **Settings first**, so a disconnected drive fails in a dialog with a fix,
   rather than three minutes into an index run.
2. **The single-instance lock second**, before either store is opened. Two copies
   sharing one SQLite index is the corruption the mutex exists to prevent, and
   the check is worthless if it happens after the file is already open.
3. **Stores, then the window.** Models are warmed on a background thread once the
   window is up, so the app is usable while ONNX loads.
"""

from __future__ import annotations

import sys
from typing import Any, Optional, Sequence

from app.core.branding import NAME
from app.core.errors import AppError, AppErrorException
from app.core.logging import log_app_error, setup_logging


def _fatal(error: AppError) -> int:
    """Show a startup failure in a dialog, falling back to the console.

    A GUI that dies with a traceback into a console nobody opened has, from the
    user's point of view, simply failed to start.
    """
    try:
        from PyQt6.QtWidgets import QApplication, QMessageBox

        _app = QApplication.instance() or QApplication(sys.argv)  # noqa: F841 - must outlive the box
        box = QMessageBox()
        box.setIcon(QMessageBox.Icon.Critical)
        box.setWindowTitle("Cannot start")
        box.setText(error.message)
        box.setInformativeText(error.suggestion)
        if error.details:
            box.setDetailedText(error.details)
        box.exec()
    except Exception:                            # noqa: BLE001 - Qt itself may be the problem
        print(error.render(), file=sys.stderr)
    return 1


#: How often the interpreter is allowed to run while Qt owns the loop. Short
#: enough that Ctrl+C feels instant, long enough that the timer costs nothing.
_SIGINT_POLL_MS = 200


def _make_ctrl_c_work(application: object) -> None:
    """Let Ctrl+C in the terminal close the window.

    Python does not deliver signals from C code. Once `application.exec()` is
    running, the interpreter is inside Qt's C++ event loop and does not come back
    out, so a Ctrl+C sets a flag that is never looked at - the handler runs only
    when Python next executes a bytecode, which is never. From the terminal the
    application appears to ignore the key entirely, and if the window has also
    stopped repainting, End Task is the only remaining option. That is a bad
    place to be for a background indexer people are asked to trust with 100GB.

    Two pieces, and both are needed. The default `SIGINT` handler is replaced
    with one that quits the application cleanly - closing the window, releasing
    the single-instance lock and flushing SQLite - and a `QTimer` that does
    nothing at all fires four times a second purely to hand control back to
    Python often enough for the handler to be noticed.

    Wrapped, because `signal.signal` only works on the main thread and this must
    never be the reason the app fails to start.
    """
    import signal

    from PyQt6.QtCore import QTimer

    try:
        signal.signal(signal.SIGINT, lambda _sig, _frame: application.quit())
    except (ValueError, OSError, AttributeError):     # not the main thread, or no SIGINT
        return

    timer = QTimer()
    timer.start(_SIGINT_POLL_MS)
    timer.timeout.connect(lambda: None)
    # Parented to the application so it is not collected the moment this
    # function returns - a timer nobody holds stops firing immediately, and the
    # symptom is Ctrl+C working in tests and not in the built app.
    timer.setParent(application)


def main(argv: Optional[Sequence[str]] = None) -> int:
    from app.core.config import log_dir_for
    from app.core.runlog import start_run

    arguments = list(argv if argv is not None else sys.argv)
    # Parsed by hand rather than with argparse: this is a GUI entry point, and
    # argparse would exit the process with a usage message on any stray argument
    # Windows decides to pass to a shortcut.
    debug = "--debug" in arguments
    qt_arguments = [a for a in arguments if a != "--debug"]

    # **The window session gets a run log like any command.** It is the one
    # that matters most: this is the interface the owner actually uses, its
    # failures are reported as prose hours later, and its footer names the
    # threads still alive at exit - which is the whole diagnosis of a window
    # that closes without the process ending.
    #
    # Opened before settings load, because a startup that dies in the "Cannot
    # start" dialog leaves nothing else behind at all.
    run = start_run(log_dir_for(), "window", argv=arguments[1:])
    code = 1
    try:
        code = _run_window(run, qt_arguments, debug)
        return code
    except BaseException as exc:
        run.unhandled(exc)
        code = "crash"     # type: ignore[assignment]
        raise
    finally:
        run.finish(code)


def _apply_pending_move(settings: Any) -> Any:
    """Carry out an index move recorded by Settings, if one is waiting.

    Returns the settings to use - reloaded when a move happened, unchanged
    otherwise. Raises `AppErrorException` if the move fails, because starting
    against a half-moved index is worse than not starting.

    **The pending record is cleared before the move, not after.** A move that
    fails with the record still in place would retry on every launch, and the
    second attempt finds a destination that is partly populated and refuses -
    so the application would never start again without someone deleting a file
    they do not know about. Cleared first, a failure is one bad start, the old
    index is untouched, and the error says what to do.
    """
    from app.core.config import load_settings
    from app.core.index_move import clear_pending, perform_move, read_pending

    pending = read_pending(settings.project_path)
    if pending is None:
        return settings

    action, destination = pending
    clear_pending(settings.project_path)

    from app.core.logging import logger

    log = logger.bind(component="main")
    log.info("applying pending index move", action=action, destination=str(destination))

    perform_move(
        settings.data_path, destination, action, settings.env_file,
        on_progress=lambda line: log.info(line),
    )
    return load_settings()


def _run_window(run: Any, qt_arguments: list[str], debug: bool) -> int:
    from app.core.config import load_settings
    from app.core.single_instance import SingleInstance

    try:
        settings = load_settings()
    except AppErrorException as exc:
        return _fatal(exc.error)

    run.settings(settings)
    setup_logging(settings.log_path)

    # **A pending index move runs here and nowhere else**: after logging is up,
    # before a single store is opened. That is the only moment nothing holds the
    # files. Settings records the decision; this keeps it, moving the folders and
    # rewriting `.env` as one operation - the split between those two is what
    # used to leave an index orphaned. Settings are reloaded afterwards so the
    # window opens against the new location rather than the one on this object.
    try:
        settings = _apply_pending_move(settings)
    except AppErrorException as exc:
        return _fatal(exc.error)
    run.settings(settings)

    try:
        from PyQt6.QtWidgets import QApplication

        from app.index.embedder import Embedder
        from app.search.engine import SearchEngine
        from app.search.rerank import Reranker
        from app.storage.sqlite_store import SqliteStore
        from app.storage.vector_store import VectorStore
        from app.ui.shell import MainWindow
    except ImportError as exc:
        from app.core.errors import make_error

        return _fatal(make_error(
            "ERR_CONFIG_INVALID", "main", key="dependencies",
            reason=f"a required package is missing: {exc}",
            suggestion="Re-run the installer: run-install.cmd",
        ))

    application = QApplication(qt_arguments)
    application.setApplicationName(NAME)
    # Before any window exists, so the taskbar entry is right from the first
    # frame rather than flickering from a default. A missing file is logged and
    # ignored - refusing to start over an icon would be absurd.
    from app.ui.tray import install_window_icon

    if not install_window_icon(application):
        pass  # cosmetic only; the app is fully usable without it
    _make_ctrl_c_work(application)

    try:
        with SingleInstance(), \
                SqliteStore(settings.fts_db) as store, \
                VectorStore(settings.vector_path, dim=settings.embed_dim) as vectors:
            engine = SearchEngine(
                store, vectors,
                Embedder(settings.embed_model, dim=settings.embed_dim,
                         cache_dir=str(settings.model_cache)),
                reranker=Reranker(settings.rerank_model,
                                  cache_dir=str(settings.model_cache),
                                  enabled=settings.rerank_enabled,
                                  top_n=settings.rerank_top_n,
                                  window_chars=settings.rerank_window_chars),
            )
            window = MainWindow(settings, store, vectors, engine, debug=debug)
            window.show()
            return application.exec()
    except AppErrorException as exc:
        log_app_error(exc.error)
        return _fatal(exc.error)


if __name__ == "__main__":
    sys.exit(main())
