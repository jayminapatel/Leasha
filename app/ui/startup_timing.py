"""Track and record startup timing.

Layer: L5

The spec (§2a, §2b, §3a, §3e) requires recording actual timings so the next
iteration can measure whether changes helped. This module tracks the startup
pipeline and close tail.

**Startup moments measured:**
1. process_start → splash_visible (currently assumed <300ms)
2. splash_visible → window_visible
3. window_visible → warm_up_complete (model loading)

**Close moments measured:**
1. close_requested → window_hidden (perceived instant close)
2. window_hidden → stores_closed (SQLite optimize already moved to idle)
3. stores_closed → lock_released (so relaunch wait is clear)
4. lock_released → process_exit (os._exit call)

The owner's machine is the reference for all numbers. Record measured timings
in the work order as they arrive.
"""

from __future__ import annotations

import time
from typing import Optional

__all__ = ["StartupTimer", "CloseTimer"]


class StartupTimer:
    """Track startup pipeline stages and record timings."""

    def __init__(self) -> None:
        """Mark the process start; the other moments are recorded as they happen."""
        self.process_start = time.perf_counter()
        self.splash_visible: Optional[float] = None
        self.window_visible: Optional[float] = None
        self.warm_up_complete: Optional[float] = None

    def record_splash_visible(self) -> None:
        """Called when splash is shown (after QApplication, before lock wait)."""
        self.splash_visible = time.perf_counter()
        elapsed_ms = int((self.splash_visible - self.process_start) * 1000)
        # Log this, but do not print to console
        # (splash may not be showing messages yet)

    def record_window_visible(self) -> None:
        """Called when MainWindow.show() completes."""
        self.window_visible = time.perf_counter()

    def record_warm_up_complete(self) -> None:
        """Called when model loading is done (embedder + reranker ready)."""
        self.warm_up_complete = time.perf_counter()

    @property
    def elapsed_to_splash(self) -> float:
        """Time from process start to splash visible, in seconds."""
        if self.splash_visible is None:
            return 0.0
        return self.splash_visible - self.process_start

    @property
    def elapsed_to_window(self) -> float:
        """Time from process start to window visible, in seconds."""
        if self.window_visible is None:
            return 0.0
        return self.window_visible - self.process_start

    @property
    def elapsed_to_ready(self) -> float:
        """Time from process start to warm-up complete, in seconds."""
        if self.warm_up_complete is None:
            return 0.0
        return self.warm_up_complete - self.process_start

    def summary(self) -> dict[str, float]:
        """Return a dict of all timing measurements (in seconds)."""
        return {
            "process_start_to_splash_ms": int(self.elapsed_to_splash * 1000),
            "process_start_to_window_ms": int(self.elapsed_to_window * 1000),
            "process_start_to_ready_ms": int(self.elapsed_to_ready * 1000),
        }


class CloseTimer:
    """Track close pipeline stages."""

    def __init__(self) -> None:
        """Mark the close request; the other moments are recorded as they happen."""
        self.close_requested = time.perf_counter()
        self.window_hidden: Optional[float] = None
        self.stores_closed: Optional[float] = None
        self.lock_released: Optional[float] = None
        self.process_exiting: Optional[float] = None

    def record_window_hidden(self) -> None:
        """Called when window.hide() completes (perceived instant close)."""
        self.window_hidden = time.perf_counter()

    def record_stores_closed(self) -> None:
        """Called when SqliteStore and VectorStore exit (pragma optimize already moved)."""
        self.stores_closed = time.perf_counter()

    def record_lock_released(self) -> None:
        """Called when the GUI lock is released (relaunch can proceed)."""
        self.lock_released = time.perf_counter()

    def record_process_exiting(self) -> None:
        """Called just before os._exit (should be last thing)."""
        self.process_exiting = time.perf_counter()

    @property
    def elapsed_to_hidden(self) -> float:
        """Seconds from the close request, or 0.0 while not reached."""
        if self.window_hidden is None:
            return 0.0
        return self.window_hidden - self.close_requested

    @property
    def elapsed_to_stores_closed(self) -> float:
        """Seconds from the close request, or 0.0 while not reached."""
        if self.stores_closed is None:
            return 0.0
        return self.stores_closed - self.close_requested

    @property
    def elapsed_to_lock_released(self) -> float:
        """Seconds from the close request, or 0.0 while not reached."""
        if self.lock_released is None:
            return 0.0
        return self.lock_released - self.close_requested

    @property
    def elapsed_to_exit(self) -> float:
        """Seconds from the close request, or 0.0 while not reached."""
        if self.process_exiting is None:
            return 0.0
        return self.process_exiting - self.close_requested

    def summary(self) -> dict[str, float]:
        """Return a dict of all timing measurements (in seconds)."""
        return {
            "close_to_hidden_ms": int(self.elapsed_to_hidden * 1000),
            "close_to_stores_closed_ms": int(self.elapsed_to_stores_closed * 1000),
            "close_to_lock_released_ms": int(self.elapsed_to_lock_released * 1000),
            "close_to_exit_ms": int(self.elapsed_to_exit * 1000),
        }
