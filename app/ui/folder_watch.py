r"""The window's side of the folder watch: a switch, a process, and a sentence.

Layer: L5

Work order 0z, item F1. "Index files as soon as they are saved" on the
Indexing page starts `app.cli watch` as a child process
(`app/index/watch_child.py`) and stops it; this module is what connects the
switch to the process and the process to the line under the switch.

**Nothing here waits.** Starting and stopping the process return at once - it
is started, read and ended on a thread that belongs to `WatchChild` - and
what it reports arrives through `_event`, a Qt signal, so every change to a
widget happens on the window's thread (non-negotiable 5). The one bounded
wait is `end_all_watch_children`, called as the window closes.

**When the process is restarted:** the switch is turned on; the list of
folders, or which of them are archives, changes; or it ends when nobody asked
it to (three times at most, a minute apart, then it says so and stays off
until the switch is used again).
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, Optional

from PySide6.QtCore import QObject, QTimer, Signal

from app.core.logging import logger
from app.ui.presenter.watch_words import NO_FOLDERS, OFF, STARTING, watch_status

__all__ = ["FolderWatchControl"]

_log = logger.bind(component="ui.folder_watch")

#: How many times a watch that ended by itself is started again, and the
#: wait before each. Fixed: enough to ride out one bad moment, few enough
#: that a watch which cannot run does not start a process a minute for ever.
RESTARTS = 3
RESTART_MS = 60_000
#: The wait after the folder list changes before the watch is restarted, so
#: adding three folders in a row is one restart.
SETTLE_MS = 1_500
#: The least time between two refreshes of the window's file count.
REFRESH_S = 10.0


class FolderWatchControl(QObject):
    """One window's folder watch. Parented to the window, like the controllers."""

    #: `(kind, data)` from the child's reading thread, delivered on the
    #: window's thread because this object lives there.
    _event = Signal(str, dict)

    def __init__(self, window: Any) -> None:
        super().__init__(window)
        self._w = window
        self._child: Any = None
        self._generation = 0
        self._folders = 0
        self._restarts_left = RESTARTS
        self._last_refresh = 0.0
        self._event.connect(self._on_event)
        self._settle = QTimer(self)
        self._settle.setSingleShot(True)
        self._settle.setInterval(SETTLE_MS)
        self._settle.timeout.connect(self.apply)
        self._again = QTimer(self)
        self._again.setSingleShot(True)
        self._again.setInterval(RESTART_MS)
        self._again.timeout.connect(self.apply)

    # -- what the window calls --------------------------------------------------

    @property
    def enabled(self) -> bool:
        return bool(getattr(self._w._settings, "index_watch_folders", False))

    @property
    def running(self) -> bool:
        return self._child is not None and self._child.running

    def toggled(self, on: bool) -> None:
        """The switch was used: save it, and start or stop the watch now."""
        self._w._settings_changed({"INDEX_WATCH_FOLDERS": bool(on)})
        self._w._settings = self._w._settings.model_copy(
            update={"index_watch_folders": bool(on)})
        self._restarts_left = RESTARTS
        self.apply()

    def folders_changed(self, *_args: Any) -> None:
        """The folder list, or which folders are archives, changed."""
        if self.enabled:
            self._settle.start()

    def apply(self) -> None:
        """Make what is running match the switch and the folder list."""
        self._settle.stop()
        self._again.stop()
        self._stop_child()
        if not self.enabled:
            self._status(OFF)
            return
        roots = self._roots()
        if not roots:
            self._status(NO_FOLDERS)
            return
        self._folders = len(roots)
        self._status(STARTING)
        try:
            self._child = self._build(roots)
            self._child.start()
        except Exception as exc:                 # noqa: BLE001 - said, never raised
            _log.warning("the folder watch could not be started: {}", exc)
            self._child = None
            self._status(f"The folder watch could not be started: {exc}")

    def shutdown(self) -> None:
        """The window is closing. The wait is `end_all_watch_children`'s."""
        self._settle.stop()
        self._again.stop()
        self._stop_child()

    # -- the process -----------------------------------------------------------------

    def _roots(self) -> list[str]:
        view = getattr(self._w, "settings_view", None)
        read = getattr(view, "current_roots", None)
        if read is None:
            return []
        try:
            return [str(root) for root in read() if str(root).strip()]
        except Exception as exc:                 # noqa: BLE001
            _log.debug("the folders to watch could not be read: {}", exc)
            return []

    def _build(self, roots: list[str]) -> Any:
        """A `WatchChild` with this window's live settings. Touches nothing on
        disk: it builds a command and an environment."""
        from app.core.config import project_root
        from app.index.child_run import settings_environment
        from app.index.watch_child import STDERR_NAME, WatchChild, watch_command

        settings = self._w._settings
        env = dict(os.environ)
        env.update(settings_environment(settings))
        log_path = getattr(settings, "log_path", None)
        # Each process's events carry its number, so a late word from one
        # that was replaced is not taken for the one running now.
        self._generation += 1
        number = self._generation
        return WatchChild(
            watch_command(roots, env_file=getattr(settings, "env_file", None)),
            env=env, cwd=project_root(),
            stderr_path=(Path(log_path) / STDERR_NAME) if log_path else None,
            on_event=lambda kind, data: self._event.emit(
                kind, {**data, "_process": number}))

    def _stop_child(self) -> None:
        child, self._child = self._child, None
        if child is not None:
            child.stop()

    # -- what it reports ----------------------------------------------------------------

    def _on_event(self, kind: str, data: dict) -> None:
        if data.get("_process") != self._generation or self._child is None:
            return                               # from a process since replaced or stopped
        if kind == "ended":
            self._child = None
            if data.get("expected") or not self.enabled:
                return
            if self._restarts_left <= 0:
                self._status("The folder watch keeps stopping, so it has been "
                             "left off. Switch it off and on to try again; the "
                             "log folder has the reason.")
                return
            self._restarts_left -= 1
            self._again.start()
        if kind == "ready":
            self._restarts_left = RESTARTS
        text = watch_status(kind, data, folders=self._folders)
        if text is not None:
            self._status(text)
        if kind == "updated" and (data.get("indexed") or data.get("removed")):
            self._refresh_counts()

    def _status(self, text: str) -> None:
        view = getattr(self._w, "indexing_view", None)
        box = getattr(view, "schedule_box", None)
        if box is not None:
            box.set_watch_status(text)

    def _refresh_counts(self) -> None:
        """The status bar's file count, at most every `REFRESH_S`."""
        now = time.monotonic()
        if now - self._last_refresh < REFRESH_S:
            return
        self._last_refresh = now
        try:
            self._w._refresh_status()
        except Exception as exc:                 # noqa: BLE001 - a count, not the watch
            _log.debug("the file count was not refreshed: {}", exc)
