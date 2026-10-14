r"""The Indexing page's Status shelf as a cockpit: where everything sits, and what paints it.

Layer: L5

The owner, 2026-10-14: *"the index page is not the most user friendly as a
dashboard for indexing, the sizes of boxes change on content etc it is
disturbing for the eye, re think that .. That should be the cockpit for
indexing"*. Top to bottom, every part at a fixed place:

1. **Header** - the state pill, the headline over the "now" line (one line
   each, cut with "…"), and the five buttons on the right; under it the
   funnel's counts in a fixed two-line slot ("keep the counts on top").
2. **Phase stepper** - a chip per step of a run.
3. **Six tiles** - Indexed, Files seen, Skipped, Passages, Speed, Time left.
4. **The bar** and its one detail line.
5. **The lanes table** - one row per reader and per lane (`indexing_workers`).
6. **The resource strip** - processor, memory, graphics card, disk, governor.
7. **The tab panel** - Run log, System log, Skipped, Held for later, This
   index, Notices (`cockpit_tabs`). It takes the height the window has left.

**Nothing on the shelf changes size or place because of what it shows.** The
rows above the tabs have fixed heights; the "now" line keeps its space while it
is hidden; the tab panel's height is the window's, not its contents'.

As with `indexing_layout.py`, every widget the view already had keeps its name
on the view (`view.headline`, `view.workers_panel`, `view.run_log`, ...), and
no user-facing sentence is written here.
"""

from __future__ import annotations

from typing import Any

from PySide6.QtWidgets import QHBoxLayout, QSizePolicy, QVBoxLayout, QWidget

from app.ui.presenter.cockpit import (
    next_state, resources, run_state, step_states, tiles,
)
from app.ui.theme import SPACE_M, SPACE_S, SPACE_XS
from app.ui.widgets.cockpit_parts import MetricTiles, PhaseStepper, ResourceStrip, StatePill
from app.ui.widgets.cockpit_tabs import CockpitTabs
from app.ui.widgets.indexing_controls import force_skip_reader
from app.ui.widgets.indexing_workers import IndexingWorkers
from app.ui.widgets.run_log import RunLog
from app.ui.widgets.status_funnel import StatusFunnel
from app.ui.widgets.timed_out_panel import TimedOutPanel

__all__ = ["build_status_page", "chain_tabs", "cockpit_state", "paint_cockpit",
           "refresh_tabs"]


def _keep_space(widget: QWidget) -> None:
    """Hidden or shown, `widget` keeps its place - nothing below it moves."""
    policy = widget.sizePolicy()
    policy.setRetainSizeWhenHidden(True)
    widget.setSizePolicy(policy)


def build_status_page(view: Any, controls: Any) -> QWidget:
    """Make the Status shelf; sets `view.funnel`, `view.workers_panel`,
    `view.run_log`, `view.timed_out`, `view.system_log` and the cockpit's parts."""
    page = QWidget()
    page.setObjectName("indexCockpit")
    layout = QVBoxLayout(page)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(SPACE_S)

    # 1. The header: state, headline over "now", the buttons.
    view.pill = StatePill()
    header = QHBoxLayout()
    header.setSpacing(SPACE_M)
    header.addWidget(view.pill)
    words = QVBoxLayout()
    words.setSpacing(0)
    words.addWidget(view.headline)
    # The words the view opened with, read off the label (see
    # `indexing_layout.paint_resting_headline`).
    view._starting_headline = view._resting_headline = view.headline.text()
    # 0x §4b: what the run is doing now, straight under the headline. Its space
    # is kept while it has nothing to say.
    words.addWidget(view.now_line)
    _keep_space(view.now_line)
    header.addLayout(words, 1)
    header.addWidget(controls)
    layout.addLayout(header)

    # 1b. The counts, straight under the header (2026-10-14, the owner: "keep
    # the counts on top, rest in tabs is fine"): the funnel (2026-09-29), in a
    # fixed two-line slot that is never hidden - see `widgets/status_funnel.py`.
    view.funnel = StatusFunnel()
    layout.addWidget(view.funnel)

    # 2-3. The steps of a run, and the six tiles.
    view.stepper = PhaseStepper()
    layout.addWidget(view.stepper)
    view.tiles = MetricTiles()
    layout.addWidget(view.tiles)

    # 4. The bar and its one line.
    view.bar.setTextVisible(True)
    view.bar.setRange(0, 1)
    view.bar.setValue(0)
    bar = QVBoxLayout()
    bar.setSpacing(SPACE_XS)
    bar.addWidget(view.bar)
    bar.addWidget(view.detail)
    layout.addLayout(bar)

    # 5. One row per thread. 0z lane B: each reader's Force skip.
    view.workers_panel = IndexingWorkers()
    view.workers_panel.forceSkip.connect(lambda reader: force_skip_reader(view, reader))
    layout.addWidget(view.workers_panel)

    # 6. What the machine has to give.
    view.resources = ResourceStrip()
    layout.addWidget(view.resources)

    # 7. The tabs. The run log (0w §2b) and the timed-out panel (0z F3) are
    # made here, as they were in the old layout.
    view.run_log = RunLog()
    view.timed_out = TimedOutPanel()
    view.cockpit_tabs = CockpitTabs(view)
    view.cockpit_tabs.setSizePolicy(QSizePolicy.Policy.Preferred,
                                    QSizePolicy.Policy.Expanding)
    view.cockpit_tabs.setMinimumHeight(view.fontMetrics().height() * 8)
    layout.addWidget(view.cockpit_tabs, 1)

    # The pill follows the buttons' own state changes and every painted tick.
    controls.stateChanged.connect(lambda event: cockpit_state(view, event))
    view.progressed.connect(
        lambda state, _i, _v, _t, paused, _s, _e: _progressed(view, state, paused))
    view.workers_panel.rowsChanged.connect(lambda: chain_tabs(view))
    chain_tabs(view)
    refresh_tabs(view)
    return page


def chain_tabs(view: Any) -> None:
    """Tab order: the buttons left to right, each reader's skip button, the tabs.

    Set again whenever reader rows are added (`rowsChanged`), so a skip button
    made mid-run is reached from the keyboard in reader order, not last.
    """
    panel = view.workers_panel
    skips = tuple(panel.skip_buttons[key] for key in panel.readers)
    tabs = view.cockpit_tabs
    log = view.run_log
    chain = (tuple(view.controls.in_order) + skips
             + (tabs.tabBar(), log.filter, log.copy_button, log.view))
    for first, second in zip(chain, chain[1:]):
        QWidget.setTabOrder(first, second)


def cockpit_state(view: Any, event: str) -> None:
    """Move the state pill on `event` - see `presenter.cockpit.next_state`."""
    pill = getattr(view, "pill", None)
    if pill is not None:
        pill.set_state(next_state(pill.state, event))


def _progressed(view: Any, state: str, paused: bool) -> None:
    """A painted tick, a finished run or a failure, as the rail's pill gets it."""
    if state == "failed":
        cockpit_state(view, "failed")
    elif state == "finished":
        cockpit_state(view, "idle")
    else:
        cockpit_state(view, run_state(
            paused=paused, held=bool(view.controls.paused),
            stopping=bool(getattr(view, "_stopping", False))))


def paint_cockpit(view: Any, stats: Any, *, finished: bool = False,
                  whole: bool = False) -> None:
    """Steps, tiles and resources for one snapshot. UI thread, no I/O.

    `finished` is a run that has ended; `whole` one that reached its end
    (every step done) rather than one stopped part-way.
    """
    if getattr(view, "tiles", None) is None:
        return
    view.tiles.show_values(tiles(
        stats, total_estimate=getattr(view, "_total_estimate", 0), finished=finished))
    view.stepper.show_steps(step_states(stats, finished=whole))
    view.resources.show_values(resources(stats))


def refresh_tabs(view: Any) -> None:
    """The tab counts and empty states, after the panels in them were painted."""
    tabs = getattr(view, "cockpit_tabs", None)
    if tabs is not None:
        tabs.refresh(view)
