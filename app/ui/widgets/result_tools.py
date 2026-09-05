r"""Wiring the results pane to its preview, its pinned set and its timeline.

Layer: L5 widget — construction, not behaviour, the same reason
`search_bar.build_controls` and `widgets/preview.attach_preview` live outside
a `*_view.py`: both `results_view.py` and `search_view.py` sit at the 250-line
view guard (`test_every_qt_view_keeps_its_logic_in_the_presenter`), and every
one of workspace §3's three items needed somewhere else to live.

`build_results_pane` moved here from `results_view.py` unchanged in its own
behaviour - the four lines it used to be are still exactly what happens -
and grew the three items around it: dragging results out (§3b) is already on
the model `results_view.py` builds; what is new here is the pinned panel
(§3c) and the timeline strip (§3d), plus the row of switches §6 requires for
all three.
"""

from __future__ import annotations

from typing import Any, NamedTuple

from PyQt6.QtWidgets import QCheckBox, QHBoxLayout, QSplitter, QVBoxLayout, QWidget

from app.ui import drag_out
from app.ui.results_view import ResultsView
from app.ui.widgets import pinned_panel as _pinned_mod
from app.ui.widgets import timeline_strip as _timeline_mod
from app.ui.widgets.pinned_panel import PANEL_ENABLED_KEY, PinnedPanel
from app.ui.widgets.timeline_strip import STRIP_ENABLED_KEY, TimelineStrip

__all__ = ["build_results_pane"]


class _PinnedRow(NamedTuple):
    """Just enough of a result row for "open" to work on a pinned path.

    `chunk_id=0` because a pin is not a search hit - there is no chunk to
    record a click against, and `record_open_async` needs the attribute to
    exist, not to mean anything here.
    """

    path: str
    chunk_id: int = 0


def _read_flag(store: Any, key: str, *, default: bool) -> bool:
    """One `index_state` boolean. Never raises - see `view_options.load_prefs`
    for the same fallback, read the same way, for the same reason."""
    if store is None:
        return default
    try:
        raw = store.get_state(key, None)
    except Exception:                            # noqa: BLE001 - a preference
        return default
    if raw is None:
        return default
    return str(raw).strip().lower() not in ("off", "0", "false", "no")


def _write_flag(store: Any, key: str, value: bool) -> None:
    if store is None:
        return
    try:
        store.set_state(key, "on" if value else "off")
    except Exception:                            # noqa: BLE001 - a preference
        pass


def _switches(*, results: ResultsView, pinned: PinnedPanel, timeline: TimelineStrip,
              store: Any) -> QWidget:
    """The three off switches §6 requires, in one row above everything else."""

    def toggle_drag(checked: bool) -> None:
        results.set_drag_enabled(checked)
        _write_flag(store, drag_out.DRAG_ENABLED_KEY, checked)

    def toggle_pinned(checked: bool) -> None:
        pinned.setVisible(checked)
        _write_flag(store, PANEL_ENABLED_KEY, checked)

    def toggle_timeline(checked: bool) -> None:
        timeline.set_enabled(checked)
        _write_flag(store, STRIP_ENABLED_KEY, checked)

    drag_box = QCheckBox("Drag results out")
    drag_box.setToolTip(
        "Drag a result straight into Explorer, an email or anywhere else that "
        "takes a file, instead of finding it again in a folder."
    )
    drag_box.setChecked(_read_flag(store, drag_out.DRAG_ENABLED_KEY, default=True))
    toggle_drag(drag_box.isChecked())
    drag_box.toggled.connect(toggle_drag)

    pinned_box = _pinned_mod.enabled_checkbox(store, on_toggle=toggle_pinned)
    toggle_pinned(pinned_box.isChecked())

    timeline_box = _timeline_mod.enabled_checkbox(store, on_toggle=toggle_timeline)
    toggle_timeline(timeline_box.isChecked())

    row = QHBoxLayout()
    for box in (timeline_box, pinned_box, drag_box):
        row.addWidget(box)
    row.addStretch(1)
    holder = QWidget()
    holder.setLayout(row)
    return holder


def build_results_pane(*, on_opened: Any, on_reveal: Any, on_reindex: Any, on_error: Any,
                       store: Any = None, search_box: Any = None, on_filter: Any = None) -> tuple:
    r"""A `ResultsView`, wired, with its preview pane, pinned set and timeline.

    Returns `(results, preview, split)` - the same three names as before;
    `split` now also carries the switches, the timeline strip and the pinned
    panel, so nothing calling this had to change shape, only its arguments.

    `store` persists the pinned set and the three switches; without one (a
    test's bare stand-in engine) everything still works, session-only, every
    switch on - the same fallback `view_options.load_prefs` already uses.

    `search_box` and `on_filter` are how a clicked band on the timeline
    becomes a search: appended to whatever is already typed, then dispatched
    through the same path Enter already uses. Neither is required - without
    them the strip still shows, it just cannot be clicked into a filter.
    """
    from app.ui.widgets.preview import attach_preview

    results = ResultsView()
    results.opened.connect(on_opened)
    results.reveal_requested.connect(on_reveal)
    results.reindex_requested.connect(on_reindex)
    preview, split = attach_preview(results, on_opened, on_error)

    def remember(values: dict) -> None:
        if store is not None:
            try:
                store.set_states(values)
            except Exception:                    # noqa: BLE001 - a preference
                pass

    def open_all(paths: Any) -> None:
        for path in paths:
            on_opened(_PinnedRow(path))

    pinned = PinnedPanel()
    state: dict = {}
    if store is not None:
        try:
            state = store.all_state()
        except Exception:                        # noqa: BLE001 - opens empty
            state = {}
    pinned.restore(state)
    pinned.remember.connect(remember)
    results.pin_requested.connect(pinned.pin)
    pinned.open_all.connect(open_all)

    timeline = TimelineStrip()
    results.rows_changed.connect(timeline.set_rows)
    if search_box is not None and on_filter is not None:
        def _apply(text: str) -> None:
            current = search_box.text().strip()
            search_box.setText(f"{current} {text}".strip())
            on_filter()
        timeline.filter_chosen.connect(_apply)

    left = QWidget()
    left_layout = QVBoxLayout(left)
    left_layout.setContentsMargins(0, 0, 0, 0)
    left_layout.addWidget(_switches(results=results, pinned=pinned, timeline=timeline, store=store))
    left_layout.addWidget(timeline)
    left_layout.addWidget(split, stretch=1)

    outer = QSplitter()
    outer.addWidget(left)
    outer.addWidget(pinned)
    outer.setStretchFactor(0, 4)
    outer.setStretchFactor(1, 1)
    outer.setChildrenCollapsible(False)

    return results, preview, outer
