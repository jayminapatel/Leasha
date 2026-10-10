r"""Is the window keeping up? Measured, logged, and used.

Layer: L0

Moved from `app/ui/lag_monitor.py` on 2026-10-08: the monitor is pure Python
(threads, a deque, the logger - no Qt), and `app/index/pipeline_bench.py`
measures the window's own monitor, which made it the one place under
`app/index` importing from `app/ui`. The UI module re-exports every name here,
so `window.lag_monitor` and every test import are unchanged. The log component
stays `ui.lag`: that is what the window's responsiveness is, wherever the
code sits.

**"The interface stays responsive" was a claim with no instrument behind it.**
Indexing runs on its own threads, but it shares one process - and one
interpreter lock - with the window, so whether a keystroke is ever delayed is a
question only a measurement can answer. This is that measurement.

Two halves, because one cannot do the other's job:

* **A beat on the UI thread.** A `QTimer` calls `beat()` every `BEAT_MS`. How
  late each call arrives is how late the event loop is, which is exactly what a
  person feels as a delay.
* **A watcher on its own thread.** A stalled UI thread cannot report that it is
  stalled, and by the time it beats again the stack that caused it is gone. The
  watcher notices that the beat is overdue *while it is overdue* and records what
  the UI thread is executing at that moment - the line that is holding it up.

**What it feeds.** `recent_lag_s()` is what `Pipeline._yield_to_ui` reads: when the
window is running late, the indexer yields. The measurement is a control input,
not only a log line.

The core is plain Python with an injectable clock (`LagMonitor.beat`,
`LagMonitor.check`), so every threshold is tested without Qt or a real stall.
`install` is the only part that touches Qt.
"""

from __future__ import annotations

import sys
import threading
import time
import traceback
from collections import deque
from typing import Any, Callable, Deque, Optional

from app.core.logging import logger

__all__ = ["LagMonitor", "install", "tighten_switch_interval",
           "BEAT_MS", "STALL_S", "SWITCH_INTERVAL_S"]

log = logger.bind(component="ui.lag")

#: How often the UI thread beats. Fine enough to see a 100 ms delay, coarse
#: enough that the timer itself costs nothing.
BEAT_MS = 50

#: A beat this much later than expected is a stall worth writing down.
STALL_S = 0.25

#: How far back `recent_lag_s` looks. Long enough to ride out one slow beat,
#: short enough that the indexer stops yielding soon after the window recovers.
RECENT_WINDOW_S = 1.5

#: One stack dump at most this often, however long or often the window stalls.
#: A log that fills with identical dumps is a log nobody reads.
DUMP_INTERVAL_S = 5.0

#: The "it was unresponsive for N ms" line, at most this often.
NOTE_INTERVAL_S = 1.0

#: 2026-10-07: a stall still going is described again as it passes each of
#: these, with every thread's frames - see `_describe_again`.
LONG_STALL_S = (2.0, 5.0, 10.0)

#: Beats kept for the percentile. Twelve thousand is ten minutes at 50 ms.
_KEEP = 12_000


class LagMonitor:
    """How late the event loop is, and what it was doing when it was late."""

    def __init__(
        self, *, beat_s: float = BEAT_MS / 1000, stall_s: float = STALL_S,
        clock: Callable[[], float] = time.monotonic,
        ui_thread_id: Optional[int] = None,
    ) -> None:
        """Set the thresholds and the injectable clock; `beat` must come from the UI thread."""
        self._beat_s = beat_s
        self._stall_s = stall_s
        self._clock = clock
        self._ui_id = ui_thread_id or threading.get_ident()
        self._last_beat = clock()
        #: `(when, lateness_s)` for each beat; lateness is beyond the interval.
        self._late: Deque[tuple[float, float]] = deque(maxlen=_KEEP)
        self._lock = threading.Lock()
        #: Set while a stall is being watched, so one stall is one report.
        self._reported_for = 0.0
        self._last_dump = -DUMP_INTERVAL_S
        self._last_note = -NOTE_INTERVAL_S
        #: 2026-10-10: when the watcher last ran, and how late that run was - see `check`.
        self._last_check: Optional[float] = None
        self._watcher_gap_s = 0.0
        self.stalls = 0
        self.worst_s = 0.0
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # -- the UI thread ------------------------------------------------------

    def beat(self) -> None:
        """Called on the UI thread by its timer. Cheap: a clock read and an append."""
        now = self._clock()
        late = max(0.0, (now - self._last_beat) - self._beat_s)
        self._last_beat = now
        with self._lock:
            self._late.append((now, late))
            if late > self.worst_s:
                self.worst_s = late
            if late >= self._stall_s:
                self.stalls += 1
        if late >= self._stall_s and now - self._last_note >= NOTE_INTERVAL_S:
            self._last_note = now
            log.warning("the window was unresponsive for {:.0f} ms", late * 1000)

    # -- the reading side ---------------------------------------------------

    def recent_lag_s(self) -> float:
        """The worst lateness in the last `RECENT_WINDOW_S`, or 0.0.

        **Includes a stall still in progress.** A beat that has not arrived yet
        is not in the record, and the moment somebody most needs the answer is
        while it is missing - so a beat overdue *now* counts.
        """
        now = self._clock()
        overdue = max(0.0, (now - self._last_beat) - self._beat_s)
        cutoff = now - RECENT_WINDOW_S
        with self._lock:
            worst = max((late for when, late in self._late if when >= cutoff),
                        default=0.0)
        return max(worst, overdue)

    def percentile(self, fraction: float) -> float:
        """The lateness at `fraction` of the recorded beats, in seconds."""
        with self._lock:
            values = sorted(late for _when, late in self._late)
        if not values:
            return 0.0
        index = min(len(values) - 1, max(0, int(round(fraction * (len(values) - 1)))))
        return values[index]

    def summary(self) -> dict[str, Any]:
        """Beats, p50, p99, worst and stall count, for the log at close."""
        with self._lock:
            count = len(self._late)
        return {
            "beats": count,
            "p50_ms": round(self.percentile(0.50) * 1000),
            "p99_ms": round(self.percentile(0.99) * 1000),
            "worst_ms": round(self.worst_s * 1000),
            "stalls": self.stalls,
        }

    # -- the watcher --------------------------------------------------------

    def check(self) -> Optional[str]:
        """One pass of the watcher. Returns the report it wrote, or None.

        Public so a test can drive it with a fake clock. Rate-limited by
        `DUMP_INTERVAL_S`, and one report per stall: a stall that lasts a
        minute is one dump, not one every 100 ms.
        """
        now = self._clock()
        # 2026-10-10, A5: how long since this watcher last ran. It is meant to
        # run every beat interval; when it could not - the 1309 ms stall at
        # 09:42:43 that day was reported at 1309 ms, not at ~300 ms, by a watcher
        # that checks every 50 ms - the whole interpreter was held, not only the
        # window, and the report says so (see `_describe`).
        previous = self._last_check
        self._last_check = now
        self._watcher_gap_s = 0.0 if previous is None else max(0.0, now - previous - self._beat_s)
        overdue = (now - self._last_beat) - self._beat_s
        if overdue < self._stall_s:
            return None
        if self._reported_for == self._last_beat:
            # this stall has been described - once, unless it is a long one
            return self._describe_again(overdue)
        if now - self._last_dump < DUMP_INTERVAL_S:
            return None
        self._reported_for = self._last_beat
        self._reported_overdue = overdue
        self._last_dump = now
        report = self._describe(overdue)
        log.warning("{}", report)
        return report

    def _describe(self, overdue: float) -> str:
        """What the UI thread is running now, and who else is."""
        frames = sys._current_frames()                          # noqa: SLF001
        names = {t.ident: t.name for t in threading.enumerate()}
        ui = frames.get(self._ui_id)
        where = ("".join(traceback.format_stack(ui)[-8:]).rstrip()
                 if ui is not None else "(no frame)")
        others = ", ".join(sorted(
            names.get(ident, str(ident)) for ident in frames if ident != self._ui_id))
        report = (f"the window has not responded for {overdue * 1000:.0f} ms - "
                  f"it is executing:\n{where}\nother threads: {others}")
        # 2026-10-10, A5: what the first report could not tell apart. Of the
        # stalls over a second in the logs of 2026-10-10 (00:57:13 1215 ms and
        # 00:57:17 1216 ms in logs/app/app_2026-10-10.log; 07:24:49 606 ms and
        # 09:42:43 1309 ms in logs/runs/run-20261010-072434-window.log and
        # run-20261010-094227-window.log) the window's innermost Python line was
        # `application.exec()` - Qt's own code, or a call from Qt waiting for the
        # interpreter lock before its first Python line - and the other threads
        # were named only. Two things are now added, each cheap: one line per
        # other thread saying where it is (whichever is inside a long call that
        # keeps the lock is the one the window waits for), and a note when the
        # watcher itself was held up, which means the whole interpreter was.
        lines = []
        for ident, frame in frames.items():
            if ident in (self._ui_id, threading.get_ident()):
                continue
            try:
                code = frame.f_code
                lines.append(f"  {names.get(ident, str(ident))}: {code.co_filename}:"
                             f"{frame.f_lineno} in {code.co_name}")
            except Exception:                                   # noqa: BLE001 - a hint only
                continue
        # `_run_window` (app/main.py) is the frame that calls `application.exec()`:
        # when it is the innermost one, no Python code of Leasha's is running there.
        if ui is not None and ui.f_code.co_name == "_run_window":
            report += ("\nthe window is in Qt's own code, not in a line of Leasha's "
                       "(painting, layout, or a call from Qt waiting for the interpreter)")
        if lines:
            report += "\nwhere the other threads are:\n" + "\n".join(sorted(lines))
        gap = self._watcher_gap_s
        if gap >= self._stall_s:
            report += (f"\nthis watcher itself could not run for {gap * 1000:.0f} ms before "
                       "this report: the whole interpreter was held (a thread inside a call "
                       "that keeps the interpreter lock, or the process paused), so the "
                       "window's line above is where it resumed, not necessarily the cause")
        return report

    def _describe_again(self, overdue: float) -> Optional[str]:
        """A stall still going as it passes 2, 5 and 10 seconds: every thread.

        2026-10-07. A 14.6 s stall at start-up was described once, at 294 ms,
        while the window happened to be filling the Mail list - and was read as
        "the Mail list froze the window". The chat model was loading on another
        thread for the same 14.8 s. One early sample cannot tell those apart;
        what each thread is running a few seconds in can. The window's own
        frames, then the other threads' - whichever of them holds the
        interpreter is the one the window is waiting for."""
        step = next((s for s in LONG_STALL_S
                     if getattr(self, "_reported_overdue", 0.0) < s <= overdue), None)
        if step is None:
            return None
        self._reported_overdue = overdue
        frames = sys._current_frames()                          # noqa: SLF001
        names = {t.ident: t.name for t in threading.enumerate()}
        parts = [f"the window has still not responded after {overdue * 1000:.0f} ms - "
                 "every thread, the window's first:"]
        for ident in sorted(frames, key=lambda i: i != self._ui_id):
            if ident == threading.get_ident():
                continue                                        # this watcher
            title = "the window" if ident == self._ui_id else names.get(ident, str(ident))
            parts.append(f"-- {title}\n"
                         + "".join(traceback.format_stack(frames[ident])[-6:]).rstrip())
        report = "\n".join(parts)
        log.warning("{}", report)
        return report

    def _watch(self) -> None:
        """The watcher thread's loop: one `check` per beat interval until stopped."""
        while not self._stop.wait(self._beat_s):
            try:
                self.check()
            except Exception as exc:                            # noqa: BLE001
                log.debug("the lag watcher failed once: {}", exc)

    def start_watcher(self) -> None:
        """Start the watcher thread once; a daemon, so it never keeps the process alive."""
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._watch, name="lag-watcher", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()


#: How long one Python thread may hold the interpreter lock before another
#: waiting thread can take it. The default is 5 ms, and the window waits behind
#: every running indexer thread in turn - four extraction threads and the consumer
#: is up to 25 ms per handover, which is most of a frame. One millisecond cuts the
#: worst wait roughly fivefold; the cost is more switching between CPU-bound
#: threads, which is small next to model inference and disk. `LEASHA_SWITCH_
#: INTERVAL_MS` overrides it, so it can be measured against the default.
SWITCH_INTERVAL_S = 0.001


def tighten_switch_interval(interval_s: Optional[float] = None) -> float:
    """Set the interpreter's thread switch interval. Returns the value now in force."""
    import os

    if interval_s is None:
        try:
            interval_s = float(os.environ.get("LEASHA_SWITCH_INTERVAL_MS", "")) / 1000
        except ValueError:
            interval_s = SWITCH_INTERVAL_S
        if interval_s <= 0:
            interval_s = SWITCH_INTERVAL_S
    try:
        sys.setswitchinterval(interval_s)
    except Exception as exc:                                    # noqa: BLE001
        log.debug("could not set the switch interval: {}", exc)
    return sys.getswitchinterval()


def install(application: Any) -> LagMonitor:
    """Start monitoring a running `QApplication`. Call from the UI thread.

    The timer is parented to the application, so it lives as long as the loop
    does - a timer nobody holds stops firing at once.
    """
    from PySide6.QtCore import QTimer

    monitor = LagMonitor()
    timer = QTimer(application)
    timer.setInterval(BEAT_MS)
    timer.timeout.connect(monitor.beat)
    timer.start()
    monitor.start_watcher()
    application.aboutToQuit.connect(monitor.stop)
    return monitor
