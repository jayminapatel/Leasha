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

from PyQt6.QtWidgets import (
    QCheckBox, QHBoxLayout, QSplitter, QStackedWidget, QVBoxLayout, QWidget,
)

from app.ui import drag_out
from app.ui.results_view import ResultsView
from app.ui.widgets import pinned_panel as _pinned_mod
from app.ui.widgets import preview_window as _preview_window_mod
from app.ui.widgets import thumbnail_grid as _grid_mod
from app.ui.widgets import timeline_strip as _timeline_mod
from app.ui.widgets.pinned_panel import PANEL_ENABLED_KEY, PinnedPanel
from app.ui.widgets.preview_window import DWG_PREVIEW_ENABLED_KEY
from app.ui.widgets.thumbnail_grid import GRID_ENABLED_KEY, ThumbnailGrid
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
              stack: QStackedWidget, split: QWidget, grid: ThumbnailGrid,
              store: Any) -> QWidget:
    """The off switches §6 requires, in one row above everything else."""

    def toggle_drag(checked: bool) -> None:
        results.set_drag_enabled(checked)
        _write_flag(store, drag_out.DRAG_ENABLED_KEY, checked)

    def toggle_pinned(checked: bool) -> None:
        pinned.setVisible(checked)
        _write_flag(store, PANEL_ENABLED_KEY, checked)

    def toggle_timeline(checked: bool) -> None:
        timeline.set_enabled(checked)
        _write_flag(store, STRIP_ENABLED_KEY, checked)

    def toggle_grid(checked: bool) -> None:
        # Work order 0h §3a: list stays the default; the grid is a view the
        # list swaps out for, never a second copy of it running beside it.
        stack.setCurrentWidget(grid if checked else split)
        _write_flag(store, GRID_ENABLED_KEY, checked)

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

    grid_box = _grid_mod.enabled_checkbox(store, on_toggle=toggle_grid)
    toggle_grid(grid_box.isChecked())

    # Workspace §5c. Nothing to toggle on a widget that is already built -
    # every pop-out reads this key for itself when it opens, so the switch is
    # only ever the write. A drawing already pinned keeps what it was opened
    # with, which is the same promise every other pop-out already makes.
    drawings_box = _preview_window_mod.enabled_checkbox(
        store, on_toggle=lambda checked: _write_flag(
            store, DWG_PREVIEW_ENABLED_KEY, checked))

    row = QHBoxLayout()
    for box in (timeline_box, pinned_box, drag_box, grid_box, drawings_box):
        row.addWidget(box)
    row.addStretch(1)
    holder = QWidget()
    holder.setLayout(row)
    return holder


def _similar_summary(row: Any) -> str:
    from pathlib import Path

    name = Path(str(getattr(row, "path", "") or "")).name or "this result"
    return f"Similar to “{name}”"


def _wire_similar(*, results: ResultsView, grid: ThumbnailGrid, engine: Any,
                  on_error: Any) -> None:
    r"""Work order 0h §2d: "more like this", for a passage or a photo alike.

    **Dispatches to whichever backend a row's kind actually has a vector
    for.** `SearchEngine.similar_to` reads back the vector stored for a
    `chunk_id` against `self.vectors` - the *text* store; `SearchEngine.
    find_similar_images` is its image-table twin, added alongside it once
    work order 0h §2d's backend half landed. For a photo, `row.chunk_id` by
    this point is already a bare int equal to `file_id` (see
    `_result_chunk_id`'s docstring in `app/search/engine.py` - the "img:"
    namespacing that keeps a photo from colliding with a real chunk id
    inside fusion has already done its job before a `SearchResult` ever
    reaches here), so the same value works as either method's argument -
    only which *method* to call differs, and that is decided by `ext`, the
    same field `results_view.image_rows`/`thumbnail_grid` already use to
    tell a photo row from a passage row.
    """
    from PyQt6.QtCore import QThreadPool

    from app.ui.thumbnail_loader import is_image_result
    from app.ui.workers import CallableWorker, run

    def _finished(response: Any, row: Any) -> None:
        found = list(getattr(response, "results", None) or [])
        results.show_results(found, [], summary=_similar_summary(row))

    def _run(row: Any) -> None:
        if engine is None:
            return
        chunk_id = int(getattr(row, "chunk_id", 0) or 0)
        backend = (engine.find_similar_images if is_image_result(getattr(row, "ext", ""))
                  else engine.similar_to)
        worker = CallableWorker(backend, chunk_id, component="ui.similar_to")
        worker.signals.finished.connect(lambda response, r=row: _finished(response, r))
        if on_error is not None:
            worker.signals.failed.connect(on_error)
        run(QThreadPool.globalInstance(), worker)

    results.similar_requested.connect(_run)
    grid.similar_requested.connect(_run)


def _wire_lightbox(*, grid: ThumbnailGrid, store: Any, on_error: Any) -> None:
    """Work order 0h §3b: a thumbnail opens straight into the lightbox.

    Self-contained rather than routed through `shell._pin_document` - that
    path exists for the *in-app preview pane's* own pop-out button, and the
    grid has no such pane to pop out from (it is deliberately just thumbnails
    - see `thumbnail_grid.py`'s own docstring). `store` gives it the same
    geometry/on-top persistence `_pin_document` gives every other pop-out;
    `open_async`/`workers.py` gives "Open the real file" and "Show in
    folder" the same worker-thread guarantee every other opener has, without
    needing shell.py's `_open_path` specifically.
    """
    from app.ui.widgets.preview_window import PreviewWindow
    from app.ui.workers import open_async

    open_windows: list = []

    def _state_now() -> dict:
        if store is None:
            return {}
        try:
            return store.all_state()
        except Exception:                        # noqa: BLE001 - opens fresh
            return {}

    def _remember(values: dict) -> None:
        if store is not None:
            try:
                store.set_states(values)
            except Exception:                    # noqa: BLE001 - a preference
                pass

    def _open(row: Any, siblings: Any) -> None:
        sibling_list = list(siblings or ())
        try:
            index = sibling_list.index(row)
        except ValueError:
            index = 0
        window = PreviewWindow(
            row, state=_state_now(), siblings=sibling_list, index=index,
            store=store)
        window.remember.connect(_remember)
        window.open_requested.connect(lambda path: open_async(path, on_error=on_error))
        window.reveal_requested.connect(
            lambda path: open_async(path, reveal=True, on_error=on_error))
        window.closed.connect(
            lambda w: open_windows.remove(w) if w in open_windows else None)
        open_windows.append(window)          # kept alive - see PreviewWindow's own note
        window.show()
        window.raise_()

    grid.opened.connect(_open)


def build_results_pane(*, on_opened: Any, on_reveal: Any, on_reindex: Any, on_error: Any,
                       store: Any = None, search_box: Any = None, on_filter: Any = None,
                       engine: Any = None) -> tuple:
    r"""A `ResultsView`, wired, with its preview pane, pinned set and timeline.

    Returns `(results, preview, split)` - the same three names as before;
    `split` now also carries the switches, the timeline strip, the pinned
    panel and the thumbnail grid (work order 0h §3a), so nothing calling this
    had to change shape, only its arguments. `results` is always the real
    `ResultsView` - the grid sits *beside* it in a `QStackedWidget`, never
    replacing what this function returns, so every existing caller of
    `results.<method>` keeps working exactly as it did.

    `store` persists the pinned set and the switches; without one (a test's
    bare stand-in engine) everything still works, session-only, every switch
    at its own documented default - the same fallback `view_options.load_prefs`
    already uses.

    `search_box` and `on_filter` are how a clicked band on the timeline
    becomes a search: appended to whatever is already typed, then dispatched
    through the same path Enter already uses. Neither is required - without
    them the strip still shows, it just cannot be clicked into a filter.

    `engine` is work order 0h §2d's and §3a/§3b's shared dependency: "more
    like this" (`_wire_similar`) needs `SearchEngine.similar_to`; opening a
    thumbnail into the lightbox (`_wire_lightbox`) does not need it directly
    but is wired alongside for the same reason both features live here -
    `search_view.py` is at its own 250-line guard and has no room left for
    either. `None` (a test's bare stand-in) disables "more like this"
    quietly; the grid and the lightbox still work without it.
    """
    from app.ui.widgets.preview import attach_preview

    results = ResultsView()
    results.opened.connect(on_opened)
    results.reveal_requested.connect(on_reveal)
    results.reindex_requested.connect(on_reindex)
    preview, split = attach_preview(results, on_opened, on_error, store=store)

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

    # Work order 0h §3a. The grid shows the same rows the list already has -
    # `rows_changed` already fires on every search and every federated
    # append (the timeline strip listens to the identical signal above), so
    # the grid needs no search-specific wiring, only a filter down to the
    # photos it already knows how to recognise.
    grid = ThumbnailGrid()
    results.rows_changed.connect(grid.show_rows)
    grid.reveal_requested.connect(on_reveal)
    grid.pin_requested.connect(pinned.pin)
    _wire_similar(results=results, grid=grid, engine=engine, on_error=on_error)
    _wire_lightbox(grid=grid, store=store, on_error=on_error)

    stack = QStackedWidget()
    stack.addWidget(split)          # index 0: list + preview - the default
    stack.addWidget(grid)           # index 1: thumbnails

    left = QWidget()
    left_layout = QVBoxLayout(left)
    left_layout.setContentsMargins(0, 0, 0, 0)
    left_layout.addWidget(_switches(
        results=results, pinned=pinned, timeline=timeline,
        stack=stack, split=split, grid=grid, store=store))
    left_layout.addWidget(timeline)
    left_layout.addWidget(stack, stretch=1)

    outer = QSplitter()
    outer.addWidget(left)
    outer.addWidget(pinned)
    outer.setStretchFactor(0, 4)
    outer.setStretchFactor(1, 1)
    outer.setChildrenCollapsible(False)

    return results, preview, outer
