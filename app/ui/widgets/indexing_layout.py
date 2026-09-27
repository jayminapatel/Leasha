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

from app.ui.presenter import (
    finished_text, index_summary, part_read_rows, progress_for, progress_text,
    resting_headline, unfinished_run_rows, when_text,
)
from app.ui.presenter.activity import timed_notices
from app.ui.widgets.category_nav import CategoryNav
from app.ui.widgets.indexing_controls import PAUSED_DETAIL, PAUSED_HEADLINE
from app.ui.widgets.indexing_headline import show_now
from app.ui.widgets.indexing_workers import IndexingWorkers
from app.ui.widgets.run_log import RunLog
from app.ui.widgets.scroll import scrollable

__all__ = [
    "assemble_pages", "paint_finished", "paint_progress", "paint_resting_headline",
    "paint_run_panels", "paint_totals", "repaint_totals",
]


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
    # The words the view opened with, read off the label rather than copied,
    # so `paint_totals` can tell "no run has been shown yet" without a flag
    # the view would have to maintain (it is at its line guard). See
    # `paint_resting_headline`.
    view._starting_headline = view._resting_headline = view.headline.text()
    # 0x §4b: what the run is doing now, straight under the counts it explains.
    status_layout.addWidget(view.now_line)
    status_layout.addWidget(view.totals)
    status_layout.addWidget(view.stats_box)
    status_layout.addWidget(view.bar)
    status_layout.addWidget(view.detail)
    # 0x §4d: one line per reader, the heartbeat, and the work behind them,
    # under the detail line they add to. Made here rather than in the view,
    # which is at its line guard; `view.workers_panel` is set on the view
    # exactly as if it had been. Hidden until a run has something to show.
    view.workers_panel = IndexingWorkers()
    status_layout.addWidget(view.workers_panel)
    status_layout.addWidget(view.notices)
    # Work order 0w §2b. Made here rather than in the view, which is over its
    # line guard: `view.run_log` is set on the view exactly as if it had been.
    view.run_log = RunLog()
    status_layout.addWidget(view.run_log)
    # 0x §4a: the button row is a widget of its own now
    # (`widgets/indexing_controls.py`), no longer a bare layout.
    status_layout.addWidget(controls)
    # **Tab reaches the log's filter, Copy and the log before the buttons
    # below them**, the order they appear in. The log is made here, after the
    # buttons, so Qt's made-first-comes-first default would put it after them
    # (and after everything else on the page). The log's three are slotted in
    # just before Start; the row keeps its own left-to-right order.
    chain = (controls.start_button.previousInFocusChain(), view.run_log.filter,
             view.run_log.copy_button, view.run_log.view) + _row(controls)
    for first, second in zip(chain, chain[1:]):
        QWidget.setTabOrder(first, second)
    status_layout.addWidget(view.archives)
    status_layout.addWidget(view.skips, stretch=1)
    # **Spare height goes below everything, not between the lines.** While
    # the skips panel has nothing to show it is hidden, and its stretch goes
    # with it; Qt then shared the page's spare height out as gaps between
    # every row, so the log's caption floated a hand's width above its own
    # box (seen in the 0x §4 grabs, and already so before them). This spacer
    # has stretch 0, so while the skips panel is showing it takes nothing and
    # the page looks exactly as it did.
    status_layout.addStretch(0)

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


def _row(controls: Any) -> tuple:
    """The button row's buttons, left to right, for the tab order.

    Read from the row itself (`IndexControls.in_order`) rather than listed
    again here: a second copy of the order went stale when the row was
    reordered, and this chain, set last, silently undid the row's own.
    """
    return tuple(controls.in_order)


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
    # 3c: then any archive a run stopped inside, kept apart from the damage row.
    rows += part_read_rows(payload.get("part_read"), running=running)
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
    paint_resting_headline(view, payload, running=running)
    stats = payload.get("stats") or {}
    try:
        view.totals_shown.emit(int(stats.get("files_total", 0) or 0))
    except (AttributeError, TypeError, ValueError):
        pass


def paint_resting_headline(view: Any, payload: dict, *, running: bool) -> None:
    """Make the headline agree with the counts, until a run takes it over.

    Order 0x section 9, review finding 10: "Nothing indexed yet." stayed above
    "Documents 17" whenever the page was opened with no run this session,
    because only a run ever changed it. The words come from
    `presenter.resting_headline`.

    **Only a headline this module put there is replaced** - the view's
    starting one, or the one this function last wrote. Anything a run, a
    Stop, a failure or another process's run wrote is left alone: those say
    something more specific than a count. Nothing changes while a run is going,
    or when the read failed (a failed read is not evidence the index is empty,
    and the stats panel already says it failed).
    """
    starting = getattr(view, "_starting_headline", None)
    if starting is None or running or payload.get("error"):
        return
    try:
        current = view.headline.text()
    except RuntimeError:                         # the C++ side has gone
        return
    if current not in (starting, getattr(view, "_resting_headline", starting)):
        return
    text = resting_headline(payload.get("stats")) or starting
    if text != current:
        view.headline.setText(text)
    view._resting_headline = text


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


def paint_progress(view: Any, stats: Any) -> None:
    """Draw one progress tick that `paint_due` let through. UI thread, no I/O.

    Moved out of `IndexingView._on_progress` by 0x §4a, unchanged except for
    the two §4 additions marked below; the throttle check itself stays in the
    view, where the interval is read at call time.
    """
    # See `presenter.progress_for` for both bugs this has had: the numerator
    # once left out `indexed`, so a fresh corpus sat near zero for hours;
    # then the denominator was `seen`, which a bounded queue keeps close to
    # the numerator, so it read 100% within seconds of starting.
    # `(0, 0)` is Qt's indeterminate range - a moving barber pole - and it
    # is what `progress_for` returns while the size of the job is genuinely
    # unknown.
    value, total = progress_for(stats, total_estimate=view._total_estimate)
    # 0x §4c: slide there rather than jump, unless the total changed - see
    # `widgets/indexing_bar.py`. A total of 0 is still the busy bar.
    view.bar.glide_to(value, total)
    view.progressed.emit("running", int(getattr(stats, "indexed", 0) or 0),
                         int(value), int(total),
                         bool(getattr(stats, "paused", False)), False, "")

    headline, detail = progress_text(
        stats, total_estimate=view._total_estimate, stopping=view._stopping
    )
    if getattr(stats, "paused_by_person", False):
        # The presenter cannot tell the two pauses apart from `paused`
        # alone, and its sentence - "waiting for the machine" - is the
        # wrong one here: this run is waiting for the person.
        headline, detail = PAUSED_HEADLINE, PAUSED_DETAIL
    view.headline.setText(headline)
    view.detail.setText(detail)
    # 0x §4b: the "what is happening now" sentence.
    show_now(view, stats, stopping=view._stopping)
    # 0x §4d: the per-reader lines. `show_now(view, None)` clears them.
    panel = getattr(view, "workers_panel", None)
    if panel is not None:
        panel.show_live(stats, stopping=view._stopping)
    paint_run_panels(view, stats)


def paint_finished(view: Any, stats: Any) -> None:
    """Draw a run that has ended (normally, or after Stop). UI thread, no I/O.

    Moved out of `IndexingView._on_finished` by 0x §4a; the view still emits
    its `finished` signal itself, after this.
    """
    # **Only a run that reached the end is full.** `pipeline.run` returns
    # normally after a Stop and after the governor aborts, so this painted a
    # complete green bar over a run stopped at 3% - next to a headline
    # saying it had been stopped, so the panel contradicted itself. The same
    # fault was found and fixed in `_on_failed` and not here.
    finished_whole = not view._stopping and not getattr(stats, "stopped_early", None)
    view.bar.setRange(0, 1)
    view.bar.setValue(1 if finished_whole else 0)
    view.progressed.emit("finished", int(getattr(stats, "indexed", 0) or 0),
                         1, 1, False, not finished_whole, "")
    headline, detail = finished_text(stats)
    view.headline.setText(headline)
    view.detail.setText(detail)
    show_now(view, None)
    paint_run_panels(view, stats)
