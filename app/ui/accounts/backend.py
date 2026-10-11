"""The UI thread's door to `AccountService`: every call runs on a worker (rule 5).

Layer: L5

Two pools. Reads and small writes (a setting, an account's name) go through one
**serial** pool, so two quick changes never race on the settings document and a
read always sees the write before it. Long work (sign-in, test connection, sync,
undo) goes on the shared pool. Callbacks run on the UI thread.
"""

from __future__ import annotations

import itertools
from typing import Any, Callable, Optional

from PySide6.QtCore import QObject, QThreadPool, Signal

from app.core.logging import logger

__all__ = ["Backend"]

_log = logger.bind(component="ui.accounts")


class Backend(QObject):
    #: Something was saved or finished: the page should read its state again.
    changed = Signal()
    #: An AppError nobody asked to handle.
    error = Signal(object)

    def __init__(self, factory: Callable[[], Any], parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self.factory = factory
        self._serial = QThreadPool()
        self._serial.setMaxThreadCount(1)
        self._ids = itertools.count(1)
        self._callbacks: dict[int, tuple] = {}

    def service(self) -> Any:
        """Only from a worker: the first call builds the service."""
        return self.factory()

    def call(self, fn: Callable[..., Any], *args: Any, done: Optional[Callable] = None,
             fail: Optional[Callable] = None, progress: Optional[Callable] = None,
             serial: bool = False, refresh: bool = False, **kwargs: Any) -> None:
        from app.ui.workers import CallableWorker, run  # noqa: PLC0415

        token = next(self._ids)
        self._callbacks[token] = (done, fail, progress, refresh)
        worker = CallableWorker(fn, *args, component="ui.accounts", report_progress=progress is not None,
                                **kwargs)
        worker.signals.finished.connect(lambda result, t=token: self._finished(t, result))
        worker.signals.failed.connect(lambda err, t=token: self._failed(t, err))
        if progress is not None:
            worker.signals.progress.connect(lambda p, t=token: self._progress(t, p))
        run(self._serial if serial else QThreadPool.globalInstance(), worker)

    # -- results arrive on the UI thread
    def _finished(self, token: int, result: Any) -> None:
        done, _fail, _progress, refresh = self._callbacks.pop(token, (None, None, None, False))
        try:
            if done is not None:
                done(result)
        except RuntimeError as exc:                        # the widget went away first
            _log.debug("accounts callback dropped: {}", exc)
        if refresh:
            self.changed.emit()

    def _failed(self, token: int, err: Any) -> None:
        _done, fail, _progress, refresh = self._callbacks.pop(token, (None, None, None, False))
        try:
            if fail is not None:
                fail(err)
            else:
                self.error.emit(err)
        except RuntimeError as exc:
            _log.debug("accounts callback dropped: {}", exc)
        if refresh:
            self.changed.emit()

    def _progress(self, token: int, payload: Any) -> None:
        cb = self._callbacks.get(token)
        if cb and cb[2]:
            cb[2](payload)

    # -- saving at once (rule 11)
    def save_settings(self, done: Optional[Callable] = None, **values: Any) -> None:
        self.call(_save_settings, self.factory, values, serial=True, done=done, refresh=True)

    def save_account(self, account_id: int, done: Optional[Callable] = None, **values: Any) -> None:
        self.call(_save_account, self.factory, account_id, values, serial=True, done=done, refresh=True)


def _save_settings(factory: Callable[[], Any], values: dict) -> Any:
    return factory().accounts.update_settings(**values)


def _save_account(factory: Callable[[], Any], account_id: int, values: dict) -> None:
    factory().accounts.update(account_id, **values)
