"""The desktop application's entry point.

Layer: L5

    venv\\Scripts\\python.exe -m app.main

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
from typing import Optional, Sequence

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
    from app.core.config import load_settings
    from app.core.single_instance import SingleInstance

    try:
        settings = load_settings()
    except AppErrorException as exc:
        return _fatal(exc.error)

    setup_logging(settings.log_path)

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

    application = QApplication(list(argv or sys.argv))
    application.setApplicationName("Local Knowledge Graph")
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
                                  enabled=settings.rerank_enabled),
            )
            window = MainWindow(settings, store, vectors, engine)
            window.show()
            return application.exec()
    except AppErrorException as exc:
        log_app_error(exc.error)
        return _fatal(exc.error)


if __name__ == "__main__":
    sys.exit(main())
