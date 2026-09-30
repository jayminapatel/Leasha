r"""Drawing an index run that belongs to another process.

Layer: L5

**The window and `app.cli index` are two processes now.** Splitting the run
lock from the window lock is what made indexing from a terminal possible with
Leasha open - see `core/run_lock.py` - and it created a state that had never
existed before: an index genuinely under way, with nothing in this process
knowing about it. Before this, the bar sat at zero and Start stayed enabled
while a run was plainly in progress, and pressing Start produced a lock error
for something the window should simply have been showing.

The run publishes a snapshot of its stats on every checkpoint. This paints it.

Here rather than in `indexing_view.py` for the reason the guard exists: that
view was at 279 code lines against a limit of 250, and every decision in here -
is this record still live, whose run is it, what should the bar read - is
answerable without a widget and already lives in `presenter`. The view keeps one
line that calls this.

**The mutex settles it; the record only supplies the words.** A record whose
lock is free belongs to a process that died, and treating that as a running
index would refuse Start until somebody edited a database by hand. `locked` is
therefore passed in rather than inferred here.
"""

from __future__ import annotations

from typing import Any

from app.ui.presenter import (
    external_is_live,
    external_run_text,
    external_snapshot,
    progress_for,
    start_blocked_reason,
)

__all__ = ["paint_external"]


def paint_external(view: Any, record: Any, *, locked: bool) -> None:
    """Show, or clear, a run belonging to another process.

    Does nothing while this window is running its own index: the live progress
    signal is better than a polled one, and two of them fighting over the same
    bar is a flicker with no upside.
    """
    if getattr(view, "_worker", None) is not None:
        return

    live = bool(locked) and external_is_live(record)
    view._external = dict(record) if live and isinstance(record, dict) else None

    if not live:
        _go_idle(view)
        return

    snapshot = external_snapshot(record)
    value, total = progress_for(
        snapshot, total_estimate=getattr(view, "_total_estimate", 0))
    view.bar.setRange(0, total)
    view.bar.setValue(value)
    view.bar.set_active(True)          # another process's run is going

    headline, detail = external_run_text(record)
    view.headline.setText(headline)
    view.detail.setText(detail)
    view.start_button.setEnabled(False)
    view.start_button.setToolTip(start_blocked_reason(record, locked=True))
    # Stop reaches another process now, so it stays live - but not twice.
    view.stop_button.setEnabled(not getattr(view, "_stopping", False))


def _go_idle(view: Any) -> None:
    """Nothing is running anywhere. **Only repaint if something changed.**

    This is called on a timer, and a window sitting idle for a week would
    otherwise rewrite the same four widgets every tick for no reason. The Start
    button already being enabled is the cheapest available proof that the last
    tick said the same thing.
    """
    if view.start_button.isEnabled():
        return
    view.start_button.setEnabled(True)
    view.start_button.setToolTip("")
    view.stop_button.setEnabled(False)
    view._stopping = False
    view.bar.setRange(0, 1)
    view.bar.setValue(0)
    view.bar.set_active(False)
    view.headline.setText("Nothing is indexing.")
    view.detail.setText("")
