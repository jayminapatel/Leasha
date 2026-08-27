r"""The diagnostics that only matter when something dies.

Every test here exists because on 2026-08-27 Leasha died twice in three
minutes and left **nothing**: no traceback, no exit code, no crash file, a run
log that simply stops mid-line. Three separate holes, all on the same path,
and all of them only open in the configuration that actually ships.

The rule these lock down: *the failure with no console is the one the
diagnostics exist for, so no diagnostic may depend on a console.*
"""

from __future__ import annotations

import faulthandler
import sys

import pytest

from app import main as app_main


class TestTheCrashFileSurvivesNoConsole:
    """`pythonw.exe` has no stderr, and that must not disable the handler."""

    def test_faulthandler_writes_a_file_when_stderr_is_none(
            self, tmp_path, monkeypatch) -> None:
        r"""The regression, exactly.

        `faulthandler.enable()` with no argument raises
        `RuntimeError: sys.stderr is None` under `pythonw.exe`. It used to be
        called *first*, inside the same `try` as the file, so that raise
        skipped the `open()` and the file handler was never installed - the
        crash report was disabled by the absence of a console, which is the
        one condition it was written for.
        """
        monkeypatch.setattr(sys, "stderr", None)
        monkeypatch.setattr(app_main, "_CRASH_FILE", None)
        try:
            app_main._catch_native_crashes(tmp_path)

            written = tmp_path / "crash" / "crash.log"
            assert written.exists(), (
                "no crash file with stderr=None - the pythonw regression")
            assert app_main._CRASH_FILE is not None
            assert faulthandler.is_enabled()
        finally:
            handle = app_main._CRASH_FILE
            app_main._CRASH_FILE = None
            if handle is not None:
                handle.close()
            faulthandler.disable()

    def test_the_stderr_call_alone_really_does_raise(self, monkeypatch) -> None:
        """The premise, measured rather than asserted from memory.

        Without this the test above could pass for the wrong reason forever.
        """
        monkeypatch.setattr(sys, "stderr", None)
        with pytest.raises(RuntimeError):
            faulthandler.enable()

    def test_the_file_lands_in_the_crash_folder(self, tmp_path) -> None:
        """`logs/crash/`, which is what `logs/README.txt` promises.

        It used to be written to `logs/crash.log`, one level up from the
        folder named for it - so the one place somebody looks was the one
        place it was not.
        """
        from app.core.logging import LOG_SUBDIRS

        assert "crash" in LOG_SUBDIRS
        try:
            app_main._catch_native_crashes(tmp_path)
            assert (tmp_path / "crash" / "crash.log").exists()
        finally:
            handle = app_main._CRASH_FILE
            app_main._CRASH_FILE = None
            if handle is not None:
                handle.close()
            faulthandler.disable()

    def test_a_bad_log_directory_does_not_stop_start_up(self, tmp_path) -> None:
        """A diagnostic that prevents start-up is worse than no diagnostic."""
        blocker = tmp_path / "not-a-directory"
        blocker.write_text("", encoding="utf-8")
        app_main._catch_native_crashes(blocker)      # must not raise


class TestAnExceptionIsWrittenDownBeforePyQtAborts:
    r"""PyQt6 calls `qFatal()` when an exception escapes a slot.

    The process is gone in that instant. The traceback goes to
    `sys.excepthook`, whose default writes to stderr - None under
    `pythonw.exe`. So an ordinary `AttributeError` in a button handler looked
    exactly like a segfault, and was diagnosed as one.
    """

    def test_the_hook_is_installed_and_logs(self, monkeypatch) -> None:
        seen: list = []

        class _Log:
            def error(self, _template, text) -> None:
                seen.append(str(text))

        class _Logger:
            def bind(self, **_kw):
                return _Log()

        monkeypatch.setattr("app.core.logging.logger", _Logger())
        original = sys.excepthook
        try:
            app_main._log_every_unhandled_exception()
            assert sys.excepthook is not original, "no hook was installed"

            try:
                raise ValueError("the one that killed it")
            except ValueError:
                sys.excepthook(*sys.exc_info())

            assert seen, "the exception was not logged"
            assert "the one that killed it" in seen[0]
            assert "ValueError" in seen[0]
            assert "test_crash_reporting" in seen[0], "no traceback, just a name"
        finally:
            sys.excepthook = original

    def test_the_hook_survives_a_broken_logger(self, monkeypatch) -> None:
        """The last hook before the process dies may not itself raise."""
        class _Exploding:
            def bind(self, **_kw):
                raise RuntimeError("logging is the thing that broke")

        monkeypatch.setattr("app.core.logging.logger", _Exploding())
        original = sys.excepthook
        try:
            app_main._log_every_unhandled_exception()   # must not raise
        except Exception as exc:                        # noqa: BLE001
            pytest.fail(f"installing the hook raised: {exc}")
        finally:
            sys.excepthook = original

    def test_worker_threads_are_covered_too(self, monkeypatch) -> None:
        """A raise on a worker does not abort, so it is quieter and worse."""
        import threading

        original_sys, original_thread = sys.excepthook, threading.excepthook
        try:
            app_main._log_every_unhandled_exception()
            assert threading.excepthook is not original_thread
        finally:
            sys.excepthook = original_sys
            threading.excepthook = original_thread


class TestQtsOwnMessagesReachTheLog:
    """`QtFatalMsg` is the line immediately before an abort."""

    def test_installing_the_handler_never_raises(self) -> None:
        app_main._log_qt_messages()                  # must not raise

    def test_a_qt_warning_is_logged(self, monkeypatch) -> None:
        pytest.importorskip("PyQt6.QtCore")
        from PyQt6.QtCore import QtMsgType, qInstallMessageHandler

        seen: list = []

        class _Log:
            def __getattr__(self, _name):
                return lambda _template, *args: seen.append(
                    " ".join(str(a) for a in args))

        class _Logger:
            def bind(self, **_kw):
                return _Log()

        monkeypatch.setattr("app.core.logging.logger", _Logger())
        try:
            app_main._log_qt_messages()
            handler = qInstallMessageHandler(None)
            assert handler is not None, "no Qt message handler was installed"
            qInstallMessageHandler(handler)
            handler(QtMsgType.QtWarningMsg, None, "a warning nobody could see")
            assert any("a warning nobody could see" in line for line in seen)
        finally:
            qInstallMessageHandler(None)


class TestTheStartUpPathActuallyCallsThem:
    """A diagnostic nobody installs is a diagnostic nobody has."""

    def test_main_installs_all_three(self) -> None:
        import inspect

        source = inspect.getsource(app_main.main)
        for name in ("_catch_native_crashes", "_log_every_unhandled_exception",
                     "_log_qt_messages"):
            assert name in source, f"main() never calls {name}"

    def test_they_run_before_the_window_is_built(self) -> None:
        """Ordering, because the crash that matters happens during start-up.

        A window that dies inside `MainWindow.__init__` has happened here
        before - the note in `shell.py` records the access violation and the
        faulthandler dump that named it.
        """
        import inspect

        source = inspect.getsource(app_main.main)
        assert (source.index("_log_qt_messages")
                < source.index("_run_window")), "installed after the window"
