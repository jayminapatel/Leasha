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
from pathlib import Path
from typing import Any, Optional, Sequence

from app.core.branding import NAME
from app.core.errors import AppError, AppErrorException
from app.core.logging import log_app_error, setup_logging


def _fatal(error: AppError) -> int:
    """Show a startup failure in a dialog, falling back to the console.

    A GUI that dies with a traceback into a console nobody opened has, from the
    user's point of view, simply failed to start.

    **It is written to the run log first, and that was missing.** Every failure
    here went to a dialog and nowhere else, so a window that would not open left
    a log ending mid-startup with no reason in it - five identical runs, no
    error, nothing to go on. The dialog is for the person at the screen; the log
    is for working out afterwards what they saw. Both, always.
    """
    try:
        log_app_error(error)
    except Exception:                            # noqa: BLE001 - logging must never mask the failure
        pass

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


#: Kept alive for the process lifetime. `faulthandler` writes to the file
#: descriptor it was given, so letting this be garbage collected closes the file
#: and the crash it was installed to record goes nowhere.
_CRASH_FILE = None


def _catch_native_crashes(log_dir: "Any") -> None:
    """Turn a Qt access violation into a stack trace instead of silence.

    **Half of this application is C++.** A crash inside PyQt - a widget touched
    after its C++ side is gone, a null model, a bad pixmap - kills the process
    where it stands: no Python exception, so no `except` sees it, no traceback,
    and the run log simply stops mid-line with no footer. From the outside the
    window "does not open" and there is nothing whatsoever to work from.

    `faulthandler` installs OS-level handlers for SIGSEGV and friends that print
    the Python stack at the moment of the crash. It costs nothing until
    something dies, and it is the difference between "it does not open" and a
    file name and line number.

    Written to a file as well as stderr, because the failure that matters is the
    one from a double-clicked shortcut, where there is no console to print to.
    """
    global _CRASH_FILE

    import faulthandler

    # **The file first, and that ordering is the whole fix.** This used to call
    # `faulthandler.enable()` for the console before opening the file. Under
    # `pythonw.exe` - which is how the shortcut, the installer and every real
    # run start Leasha - `sys.stderr` is None, and `enable()` with no argument
    # raises `RuntimeError: sys.stderr is None`. The blanket `except` below
    # swallowed it and the *file* handler, three lines later, was never
    # installed. So the diagnostic that exists precisely for the crash with no
    # console was disabled by the absence of a console. Two hard deaths on
    # 2026-08-27 left `logs/` with no crash.log in it at all - not an empty one,
    # none, because the `open()` never ran either.
    try:
        crash_dir = Path(log_dir) / "crash"
        crash_dir.mkdir(parents=True, exist_ok=True)
        _CRASH_FILE = open(crash_dir / "crash.log", "a", buffering=1,
                           encoding="utf-8")
        faulthandler.enable(file=_CRASH_FILE, all_threads=True)
    except Exception:                            # noqa: BLE001
        # A diagnostic that prevents start-up is worse than no diagnostic.
        pass

    # stderr as well, when there is one. Additive: `enable()` replaces the
    # destination, so this runs second and only when it can succeed.
    try:
        if sys.stderr is not None:
            faulthandler.enable()
    except Exception:                            # noqa: BLE001
        pass


def _log_every_unhandled_exception() -> None:
    r"""Write down the exception PyQt is about to kill the process over.

    **PyQt6 aborts on an exception that escapes a slot.** Not "prints and
    continues" - since PyQt 5.5 an unhandled Python exception inside a slot
    invoked from C++ calls `qFatal()`, which calls `abort()`. The process is
    gone in the same instant.

    On the way out PyQt hands the traceback to `sys.excepthook`, and the
    default hook writes to `sys.stderr` - which under `pythonw.exe` is None.
    So an ordinary `AttributeError` in a button handler becomes a window that
    vanishes with an empty log and no exit code, which is indistinguishable
    from a segfault and was diagnosed as one.

    This puts the traceback in the log first. It costs nothing until something
    throws, and it turns "it crashed" into a file and a line number.
    """
    import threading
    import traceback

    def _hook(kind, value, tb) -> None:
        # **The logger is bound here, not at install time.** Binding it up
        # front looked tidier and meant that if logging was itself the broken
        # thing, *installing the crash reporter* raised - from `main`, before
        # the window, turning a diagnostic into the failure. Caught by
        # `test_the_hook_survives_a_broken_logger`.
        try:
            text = "".join(traceback.format_exception(kind, value, tb)).strip()
            from app.core.logging import logger

            logger.bind(component="main").error(
                "unhandled exception - the process may stop here:\n{}", text)
        except Exception:                        # noqa: BLE001 - the last hook
            pass
        try:
            if _CRASH_FILE is not None:
                _CRASH_FILE.write(
                    "".join(traceback.format_exception(kind, value, tb)))
        except Exception:                        # noqa: BLE001 - the last hook
            pass
        # Chain to whatever was there, so a console run still prints.
        try:
            sys.__excepthook__(kind, value, tb)
        except Exception:                        # noqa: BLE001 - no stderr
            pass

    sys.excepthook = _hook
    # **Worker threads too.** A raise on a `CallableWorker` does not abort the
    # process, so it is quieter and lives longer - a feature that silently
    # stopped working with nothing in the log at all.
    try:
        threading.excepthook = lambda args: _hook(
            args.exc_type, args.exc_value, args.exc_traceback)
    except Exception:                            # noqa: BLE001
        pass


def _log_qt_messages() -> None:
    r"""Qt's own warnings and fatals, into the same log as everything else.

    Qt writes to its own message handler, which by default goes to stderr -
    None under `pythonw.exe`. `QWidget: Must construct a QApplication first`,
    a bad pixmap, a layout warning: all invisible in exactly the runs where
    they matter. A `QtFatalMsg` is the message immediately before an abort, so
    it is the most valuable line the log can hold.
    """
    try:
        from PyQt6.QtCore import (QtMsgType, qInstallMessageHandler)

        from app.core.logging import logger

        log = logger.bind(component="qt")
        levels = {
            QtMsgType.QtDebugMsg: log.debug,
            QtMsgType.QtInfoMsg: log.info,
            QtMsgType.QtWarningMsg: log.warning,
            QtMsgType.QtCriticalMsg: log.error,
            QtMsgType.QtFatalMsg: log.critical,
        }

        def _handler(kind, context, message) -> None:
            try:
                where = ""
                if context is not None and getattr(context, "file", None):
                    where = f" ({context.file}:{context.line})"
                levels.get(kind, log.info)("{}{}", message, where)
            except Exception:                    # noqa: BLE001 - a log line
                pass

        qInstallMessageHandler(_handler)
    except Exception:                            # noqa: BLE001
        pass


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
    _catch_native_crashes(log_dir_for())
    _log_every_unhandled_exception()
    _log_qt_messages()
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


def _exit_fast(code: int) -> None:
    r"""Exit without interpreter teardown.

    §3d: After stores and lock are released, call `os._exit()` to skip the
    slow teardown of native modules (onnxruntime, lance, PyQt6). This is
    standard practice for Python processes with heavyweight C++ libraries.

    **The placement is load-bearing.** This is called *after* the
    `SqliteStore.__exit__` and `VectorStore.__exit__` have run, so it never
    happens while a store is open. The log has been flushed. This is the
    last thing the process does.

    See <https://bugs.python.org/issue42971> for the performance issue this
    works around.

    Tests: `test_exit_placement_is_safe_` asserts this is unreachable while a
    store is open.
    """
    import os

    os._exit(code)  # noqa: B605 - deliberate use of os._exit


def _acquire_gui_lock_responsively(
    gui_lock: Any,
    wait_s: float,
    application: Any,
) -> Any:
    r"""Acquire the single-instance lock without freezing the splash (§2c).

    **`SingleInstance.acquire(wait_s=...)` blocks the calling thread in its own
    retry loop** (`time.sleep` between attempts), and at this point in startup
    `application.exec()` has not been called yet - there is no Qt event loop
    running to pump. The splash's status was already being set to the honest
    handover message (`"Waiting for the previous Leasha to finish closing…"`)
    before this call, but with nothing pumping events during a call that can
    block for up to `HANDOVER_WAIT_S` (12s), the widget never actually repaints
    to show it, its case-rotation timer never fires, and Windows may mark the
    window Not Responding. That is the exact "busy cursor and nothing else"
    symptom from the work order's problem statement - still present underneath
    a splash that was built specifically to explain it.

    This polls instead: one non-blocking attempt (`wait_s=0.0`) at a time, with
    `application.processEvents()` between attempts, so the splash keeps
    genuinely repainting and rotating cases for the whole wait. It changes
    nothing about `SingleInstance`'s own locking semantics or its error
    contract - a timeout still raises the same `AppErrorException`
    `acquire(wait_s=...)` would have raised, from the same deadline.
    """
    import time as _time

    deadline = _time.monotonic() + max(0.0, float(wait_s))
    poll_s = 0.1
    while True:
        try:
            return gui_lock.acquire(wait_s=0.0)
        except AppErrorException:
            if _time.monotonic() >= deadline:
                raise
            application.processEvents()
            _time.sleep(poll_s)


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
    from app.core.run_lock import GUI_MUTEX_NAME, request_front
    from app.core.single_instance import HANDOVER_WAIT_S, SingleInstance

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
        moved_to = _apply_pending_move(settings)
    except AppErrorException as exc:
        return _fatal(exc.error)
    if moved_to is not settings:
        # Only re-record when a move actually happened. Calling this
        # unconditionally printed the whole settings block twice into every run
        # log, which is noise in the one file somebody reads when the window
        # will not open - and it made a normal start look like a repeat.
        settings = moved_to
        run.settings(settings)

    try:
        from PyQt6.QtWidgets import QApplication
    except ImportError as exc:
        from app.core.errors import make_error

        return _fatal(make_error(
            "ERR_CONFIG_INVALID", "main", key="dependencies",
            reason=f"a required package is missing: {exc}",
            suggestion="Re-run the installer: run-install.cmd",
        ))

    # Before QApplication, so the taskbar has this process's own identity
    # from the first frame rather than showing pythonw.exe's generic icon -
    # see `set_app_user_model_id`'s docstring for why that happens when
    # running unpackaged, which is every run until L9 ships a real .exe.
    from app.ui.tray import install_window_icon, set_app_user_model_id

    set_app_user_model_id()  # cosmetic only; failure is not fatal

    application = QApplication(qt_arguments)
    application.setApplicationName(NAME)
    # Before any window exists, so the taskbar entry is right from the first
    # frame rather than flickering from a default. A missing file is logged and
    # ignored - refusing to start over an icon would be absurd.
    if not install_window_icon(application):
        pass  # cosmetic only; the app is fully usable without it
    _make_ctrl_c_work(application)

    # **The splash is shown immediately** (<300ms), hiding the wait for
    # single-instance handover and model loading. It reports progress through
    # startup breadcrumbs and optionally a download progress bar.
    #
    # **Only stdlib, Qt and the small app.core/app.ui.splash-family modules
    # precede this point** - see the 2026-09-07 note under work order §4's
    # pytest-qt item (`tests/unit/test_startup_import_order.py`). Before that
    # fix, the heavier `app.index`, `app.search`, `app.storage` and the whole
    # of `app.ui.shell` (plus `app.extract.ocr`'s device configuration) were
    # all imported above this line - contradicting this exact comment and
    # this module's own docstring ("Only stdlib and Qt are imported before it
    # is shown"). They are imported below instead, now that the splash is
    # already on screen.
    from app.ui.splash import SplashScreen, StatusReporter, get_splash_status_text
    from app.ui.startup_timing import CloseTimer, StartupTimer

    startup_timer = StartupTimer()
    splash = SplashScreen()
    splash.show()
    startup_timer.record_splash_visible()
    application.processEvents()  # Ensure splash is painted

    # Status reporter forwards startup log messages to the splash
    status_reporter = StatusReporter(splash)

    # **Everything past this point may cost real import time - and that is
    # fine, because the splash is already visible.** This used to run before
    # `QApplication` was even constructed; see the comment above `splash.show()`.
    try:
        from app.index.embedder import Embedder
        from app.search import vector
        from app.search.engine import SearchEngine
        from app.search.rerank import Reranker
        from app.storage.sqlite_store import SqliteStore
        from app.storage.vector_store import ImageVectorStore, VectorStore
        from app.ui.shell import MainWindow
    except ImportError as exc:
        from app.core.errors import make_error

        splash.widget.close()  # H4: no dialog is worth a splash stuck on screen
        return _fatal(make_error(
            "ERR_CONFIG_INVALID", "main", key="dependencies",
            reason=f"a required package is missing: {exc}",
            suggestion="Re-run the installer: run-install.cmd",
        ))

    # OCR is a registered extractor with no settings object in reach, so the
    # device choice is pushed to it here - the same call `cli._load` makes, so
    # the window and the command line run the models on the same processor.
    # Moved here (was before splash-show) for the same reason as the import
    # block just above: it does not need to happen before the splash exists,
    # only before extraction/indexing does.
    from app.extract import ocr as _ocr

    _ocr.configure_device(settings.embed_device)

    # **A breadcrumb before each stage that can block.**
    #
    # A window that would not open left a run log ending after the settings
    # block with nothing after it: no error, no footer, five identical runs. A
    # missing footer means the process never came back from here, so it was
    # hanging rather than failing - and there was no way to tell *where*.
    #
    # The expensive stages are the two model loads. `fastembed` downloads about
    # 130MB on first use and writes it into MODEL_CACHE, so anything that empties
    # that folder - moving the index, re-staging over it - turns the next start
    # into a silent download with no window and no progress. One line each turns
    # that from "it does not open" into "it stopped at the embedding model".
    from app.core.logging import logger

    log = logger.bind(component="main")

    import time

    code = 1
    try:
        log.info("startup: acquiring the single-instance lock")
        status_reporter("Waiting for the previous Leasha to finish closing…")
        application.processEvents()
        # **Waited for, not asked about once.** Closing Leasha holds this lock
        # for as long as the stores stay open, which is seconds - and the window
        # has already vanished from the screen by then, so relaunching
        # immediately is exactly what somebody does. See `HANDOVER_WAIT_S`.
        gui_lock = SingleInstance(GUI_MUTEX_NAME)
        # **The window lock, and only the window.** This used to be the same
        # mutex `app.cli index` takes, so having Leasha open made indexing from
        # a terminal impossible - a refusal that protected nothing, because a
        # window that is merely open is a reader. A second *window* is still
        # refused; see `core/run_lock.py` for the split.
        #
        # §2c: acquired responsively rather than via a single blocking call, so
        # the splash's handover status genuinely repaints (and its case
        # rotation genuinely runs) for the whole wait instead of freezing on
        # whatever was painted last. See `_acquire_gui_lock_responsively`.
        try:
            _acquire_gui_lock_responsively(gui_lock, HANDOVER_WAIT_S, application)
        except AppErrorException:
            # **Not a handover anymore - somebody is already here.**
            # `HANDOVER_WAIT_S` covers a closing copy releasing the lock, which
            # takes seconds; past the whole wait, the far more likely story is
            # a second double-click on a copy that was never closing. Reported
            # live: this used to sit behind a splash for the whole wait and
            # then fail with a fatal-error dialog, and the window that was
            # already open never came forward - "the box is hard to get to."
            # There is no error here from where the person is standing, just
            # one window they now have to find - so ask it to come forward,
            # via the same `index_state` flag `request_stop` uses, and leave
            # quietly instead of raising past this into `_fatal`.
            log.info("startup: the lock is already held - asking that "
                      "copy to come to the front instead of waiting further")
            status_reporter("Leasha is already open…")
            application.processEvents()
            try:
                with SqliteStore(settings.fts_db) as front_store:
                    request_front(front_store)
            except Exception as exc:             # noqa: BLE001 - see comment above
                log.warning("startup: could not ask the running copy to "
                            "come to the front: {}", exc)
            splash.hide_and_close()
            return 0

        status_reporter("Opening your index…")
        application.processEvents()
        with gui_lock, \
                SqliteStore(settings.fts_db) as store, \
                VectorStore(settings.vector_path, dim=settings.embed_dim) as vectors, \
                ImageVectorStore(settings.vector_path) as image_vectors:
            log.info("startup: stores open, loading the embedding model",
                     model=settings.embed_model, cache=str(settings.model_cache))
            status_reporter("Loading the search engine…")
            application.processEvents()
            # §1c: fastembed's own download has no progress hook this
            # application can reach - TextEmbedding.__init__ drops any extra
            # kwargs before they would reach huggingface_hub's tqdm_class -
            # so Embedder measures progress from the outside (the cache
            # directory's growth) and calls this from its own background
            # thread. Splash's status text becomes the plain-words message
            # already used elsewhere for a first-run download, now paired
            # with a real, moving number instead of a static line.
            embedder = Embedder.from_settings(
                settings,
                on_progress=lambda pct: status_reporter(
                    get_splash_status_text("model_download"), pct),
            )

            log.info("startup: loading the reranker",
                     model=settings.rerank_model, enabled=settings.rerank_enabled)
            reranker = Reranker.from_settings(settings)

            # Work order 0h §1c. **Not loaded here, not warmed here.**
            # `clip_text_embedder_from_settings` builds a plain `Embedder`
            # around the CLIP text tower exactly the way `Embedder.
            # from_settings` builds one for the meaning model above it - but
            # neither the model download nor the ONNX session happens at
            # construction (see `Embedder`'s own docstring); it loads lazily
            # on the first search that reaches `vector.search_images`. The
            # startup budget (work order 0r) has room for one eager load
            # (the meaning model above), not two.
            clip_text_embedder = vector.clip_text_embedder_from_settings(settings)

            log.info("startup: building the search engine")
            engine = SearchEngine(
                store, vectors, embedder, reranker=reranker,
                image_vectors=image_vectors, clip_text_embedder=clip_text_embedder,
            )

            log.info("startup: constructing the window")
            window = MainWindow(settings, store, vectors, engine,
                                image_vectors=image_vectors, debug=debug)

            # **Armed here, and only here.** `closeEvent` runs in the test
            # suite too, and a 300-second `os._exit` timer started by a test
            # would end a long pytest session. See `app/ui/exit_watchdog.py`
            # for why a closed window needs a backstop at all.
            from app.ui import exit_watchdog
            window.after_close = exit_watchdog.arm

            log.info("startup: showing the window")
            window.show()
            startup_timer.record_window_visible()
            application.processEvents()  # Ensure window is painted

            log.info("startup: warm-up complete")
            status_reporter("Ready")
            startup_timer.record_warm_up_complete()
            # §2a: measured numbers, logged so every run captures them without
            # anyone adding print statements by hand. See the work order §2a
            # for the numbers recorded from this on the owner's machine.
            #
            # **Interpolated into the message, not passed as kwargs.** The
            # file/console log format strings only render `{message}` plus
            # `component`/`error_code` from `extra` - arbitrary `**kwargs`
            # land in the record's `extra` dict but never appear in the
            # rendered line, which is how the existing `model=...`/`cache=...`
            # kwargs a few lines above this one have been silently invisible
            # in every run log all along.
            _timings = startup_timer.summary()
            log.info(
                "startup: timings - splash {}ms, window {}ms, ready {}ms",
                _timings["process_start_to_splash_ms"],
                _timings["process_start_to_window_ms"],
                _timings["process_start_to_ready_ms"],
            )
            # **Nothing closed the splash.** It showed, reported every stage
            # correctly, and then sat on screen rotating cases for the entire
            # life of the process - `hide_and_close` existed and was never
            # called. It honours its own documented minimum hold time, so
            # calling it here rather than the instant the window is ready
            # never cuts a fast startup's one rotation short.
            splash.hide_and_close()

            log.info("startup: entering the event loop")
            code = application.exec()
            log.info("shutdown: the event loop returned", code=code)

            # §3a: time the tail. The stage log used to end at "engine" close
            # (see `MainWindow.closeEvent`'s own staged teardown) with nothing
            # after it - a shutdown that took twenty seconds and one that took
            # two looked identical from here on, and the single-instance lock
            # is held for exactly this stretch, so a slow tail is what a
            # relaunch runs into.
            #
            # **Closed explicitly here, ahead of the `with` block's own exit.**
            # `SqliteStore.close()`, `VectorStore.close()` and
            # `SingleInstance.release()` are all documented safe to call more
            # than once, so the `with` statement's automatic exit below runs
            # immediately afterwards as a harmless no-op - this does not change
            # what gets closed or when, only names each stage as it happens.
            # (kwargs are interpolated into the message, not passed as extra
            # fields - see the §2a comment above on why: they render invisibly
            # otherwise.)
            close_timer = CloseTimer()
            t0 = time.perf_counter()
            store.close()
            log.info("shutdown: sqlite store closed - {}s",
                     round(time.perf_counter() - t0, 3))

            t0 = time.perf_counter()
            vectors.close()
            log.info("shutdown: vector store closed - {}s",
                     round(time.perf_counter() - t0, 3))
            close_timer.record_stores_closed()

            t0 = time.perf_counter()
            gui_lock.release()
            close_timer.record_lock_released()
            log.info("shutdown: lock released - {}s",
                     round(time.perf_counter() - t0, 3))
        # **After the context exits, stores and lock are released.** §3d: Skip
        # interpreter teardown of heavyweight native modules (onnxruntime, lance,
        # PyQt6) with os._exit(). This is the standard remedy for slow native
        # unload on Windows. The placement after store close is load-bearing -
        # it ensures SQLite and LanceDB __exit__ have run.
        _close_summary = close_timer.summary()
        log.info(
            "shutdown: stores and lock released, exiting - "
            "stores {}ms, lock {}ms since event loop returned",
            _close_summary["close_to_stores_closed_ms"],
            _close_summary["close_to_lock_released_ms"],
        )
        close_timer.record_process_exiting()
        _exit_fast(code)
        # Never reached, but return for type checking
        return code
    except AppErrorException as exc:
        log_app_error(exc.error)
        return _fatal(exc.error)


if __name__ == "__main__":
    sys.exit(main())
