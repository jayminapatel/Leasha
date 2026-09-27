"""The Indexing page's layout, and the painting that is only assignment.

Layer: L5

`indexing_view.py` reached the 250-line guard, and the guard is right about what
it is for: a view that keeps growing is where logic starts to live. What left it
is the part that holds none - assembling the three shelves into a category
sidebar, and copying a payload's fields onto widgets. **No user-facing string is
here**; every label and tooltip stays in `indexing_view.py`, where
`test_pages_reorg.py` reads it against the commit before the reorganisation.

Each function takes the view and sets or reads the same attributes the view
always had, so nothing that reaches into `view.bar`, `view.stats_box` or
`view._nav` changes.
"""

from __future__ import annotations

from typing import Any

from PyQt6.QtWidgets import QVBoxLayout, QWidget

from app.ui.presenter import index_summary, unfinished_run_rows, when_text
from app.ui.presenter.activity import timed_notices
from app.ui.widgets.category_nav import CategoryNav
from app.ui.widgets.run_log import RunLog
from app.ui.widgets.scroll import scrollable

__all__ = ["assemble_pages", "paint_run_panels", "paint_totals", "repaint_totals"]


def assemble_pages(view: QWidget, controls: Any, names: tuple[str, str, str]) -> CategoryNav:
    """Three shelves, one sidebar (§2a; see the view's module docstring for §2b).

    **Status keeps its old, unwrapped shape.** `view.skips` already scrolls its
    own contents (`stretch=1`, exactly as before) and was never the reported
    fault - wrapping it in a second scroll area would only reintroduce the
    two-scrollbars problem `widgets/scroll.py` warns about. Schedule and Tuning
    are the two shelves that pushed the old single page past its height with
    nothing to scroll it, so they are the two that get `scrollable()`.

    `names` is `(status, schedule, tuning)`, in display order. Returns the
    sidebar; the outer layout is set on `view` here.
    """
    status_name, schedule_name, tuning_name = names

    status_page = QWidget()
    status_layout = QVBoxLayout(status_page)
    status_layout.setContentsMargins(0, 0, 0, 0)
    status_layout.setSpacing(8)
    status_layout.addWidget(view.headline)
    status_layout.addWidget(view.totals)
    status_layout.addWidget(view.stats_box)
    status_layout.addWidget(view.bar)
    status_layout.addWidget(view.detail)
    status_layout.addWidget(view.notices)
    # Work order 0w §2b. Made here rather than in the view, which is over its
    # line guard: `view.run_log` is set on the view exactly as if it had been.
    view.run_log = RunLog()
    status_layout.addWidget(view.run_log)
    status_layout.addLayout(controls)
    status_layout.addWidget(view.archives)
    status_layout.addWidget(view.skips, stretch=1)

    schedule_page = QWidget()
    schedule_layout = QVBoxLayout(schedule_page)
    schedule_layout.setContentsMargins(0, 0, 0, 0)
    schedule_layout.addWidget(view.schedule_box)
    schedule_layout.addStretch(1)

    tuning_page = QWidget()
    tuning_layout = QVBoxLayout(tuning_page)
    tuning_layout.setContentsMargins(0, 0, 0, 0)
    tuning_layout.addWidget(view.tuning)
    tuning_layout.addStretch(1)

    nav = CategoryNav()
    nav.add_category(status_name, status_page)
    nav.add_category(schedule_name, scrollable(schedule_page))
    nav.add_category(tuning_name, scrollable(tuning_page))

    layout = QVBoxLayout(view)
    # 9, the margin every other page gets from Qt by default. This was 0, so the
    # stats card, the progress bar and "Reset index..." ran flush to the window's
    # right edge and the buttons sat 5px from the bottom (seen on the real window
    # at 125%, 2026-09-20).
    layout.setContentsMargins(9, 9, 9, 9)
    layout.addWidget(nav)
    return nav


def paint_totals(view: Any, payload: dict) -> None:
    """Paint the index summary from a worker's payload. UI thread, no I/O."""
    # Kept, so `repaint_totals` can redraw without a second read - see there.
    view._totals_payload = payload
    # Work order `dates-live-log-and-interrupted-runs` 3a. First, because it
    # is the one row that answers "what happened while I was away" - and it
    # is dropped while any run is going, since that run is the carrying on.
    running = getattr(view, "_worker", None) is not None or bool(
        getattr(view, "_external", None))
    rows = unfinished_run_rows(payload.get("unfinished"), running=running)
    rows += index_summary(
        payload.get("stats"),
        payload.get("vectors"),
        data_path=payload.get("data_path", ""),
        disk_bytes=payload.get("disk_bytes"),
        last_run=when_text(payload.get("last_run") or ""),
        next_run=view._next_run_text,
        error=payload.get("error", ""),
        warned=payload.get("warned"),
    )
    view.stats_box.show_rows(rows)
    stats = payload.get("stats") or {}
    try:
        view.totals_shown.emit(int(stats.get("files_total", 0) or 0))
    except (AttributeError, TypeError, ValueError):
        pass


def repaint_totals(view: Any) -> None:
    """Redraw the summary from the last payload, with no store read.

    Called as a run starts. The summary was read before the run took the lock,
    so a "did not finish" row painted then would otherwise stay on screen for
    the whole of the run that is carrying on - and the next read, at the end
    of the run, is hours away. Nothing to do before the first read.
    """
    payload = getattr(view, "_totals_payload", None)
    if isinstance(payload, dict):
        paint_totals(view, payload)


def paint_run_panels(view: Any, stats: Any) -> None:
    """What a progress tick and a finished run both draw beneath the bar: the
    skip summary, the archived folders, the run's notices and its log.

    The log appends only what arrived since it was last painted, so a tick
    `paint_due` dropped loses nothing - its lines come with the next one.
    """
    view.skips.show_skips(stats.skipped_by_code)
    view.archives.show_roots(getattr(stats, "skipped_roots", ()))
    # Work order 0w §2c: each notice with the time it was said, in front of
    # its unchanged words. `show_notices` itself is untouched.
    view.show_notices(timed_notices(stats))
    run_log = getattr(view, "run_log", None)
    if run_log is not None:
        run_log.show_activity(getattr(stats, "activity", None))


def paint_due(view: Any, stats: Any, now: float, min_interval_s: float) -> bool:
    """Whether this progress tick should repaint the page; records it if so.

    **At most a few repaints a second.** A fast run checkpoints every fifty
    documents, which is several times a second, and each tick rebuilds the skip
    summary, the archived-folder list and the notices on the one thread the
    person is typing on. A tick dropped here is replaced by the next, and the
    finished handler always paints the last state. A change in whether the run is
    paused is never dropped - that is the tick that explains why the bar stopped -
    and neither is any tick while a stop is being carried out. Nor is a change
    of phase: the pipeline announces one with a single tick, often a few
    milliseconds after the last, and it may be the only tick for minutes.
    """
    paused = bool(getattr(stats, "paused", False))
    phase = str(getattr(stats, "phase", "") or "")
    if (now - view._last_paint < min_interval_s
            and paused == view._last_paused and not view._stopping
            and phase == getattr(view, "_last_phase", "")):
        return False
    view._last_paint = now
    view._last_paused = paused
    view._last_phase = phase
    return True
