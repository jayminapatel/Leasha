"""The two pieces of `ResultsView` that build model items and the right-click menu.

Layer: L5

`results_view.py` crossed the 250-line guard. What left it is what holds no
decision of its own: turning a payload into a `QStandardItem` (the roles a
delegate, a tooltip and a screen reader each read), and assembling the shared
file menu for the row under the cursor. Each function takes what it needs, so
nothing here reaches into the view's private state.

**Rows are painted, not built.** The list is a `QListView` over a plain model
with `ResultDelegate` painting only what is on screen; an item therefore carries
its payload and a few roles and no widgets, so the cost stops scaling with the
result count.

**Both accessibility roles, or the list is empty to a screen reader.** The
delegate paints from `ROLE_PAYLOAD`, so the item carries no text of its own -
and `QAccessible` reads `AccessibleTextRole`, falling back to `DisplayRole`.
With neither set there was nothing to fall back to and fifty results announced
as fifty blanks. `DisplayRole` is set too and is harmless: the delegate draws
the row itself and never consults it, so nothing appears twice.
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtGui import QStandardItem

from app.core.file_state import explain
from app.ui.presenter import (
    Terminator, accessible_text, explain_switch_on, offline_volume_note,
    result_tooltip, why,
)
from app.ui.result_delegate import ROLE_EXPANDED, ROLE_PAYLOAD
from app.ui.widgets.file_menu import FileActions, show_for, viewport_point
from app.ui.widgets.why_dialog import show_why

__all__ = ["refill", "result_item", "show_result_menu", "terminator_item"]

_ROLE = Qt.ItemDataRole


def result_item(payload: Any, *, expanded: bool, missing: set, volumes: dict,
                placeholders: set, statuses: Any = None) -> QStandardItem:
    item = QStandardItem()
    item.setEditable(False)
    item.setData(payload, ROLE_PAYLOAD)
    item.setData(expanded, ROLE_EXPANDED)
    path = str(getattr(payload, "path", "") or "")
    note = offline_volume_note(volumes.get(int(getattr(payload, "file_id", 0) or 0)))
    tip = result_tooltip(payload, missing=path in missing, volume_note=note,
                         placeholder=(not note) and path in placeholders)
    # The Status word the delegate paints, and its one plain sentence - the
    # hover is where "what does Deferred mean" gets answered.
    word = (statuses or {}).get(int(getattr(payload, "file_id", 0) or 0), "")
    if word:
        tip = "\n\n".join(part for part in (tip, f"Status: {word} — {explain(word)}") if part)
    item.setData(tip, int(_ROLE.ToolTipRole))
    spoken = accessible_text(payload, expanded=expanded)      # item 7a
    item.setData(spoken, int(_ROLE.AccessibleTextRole))
    item.setData(spoken, int(_ROLE.DisplayRole))
    return item


def refill(view: Any, items: list, anchor: Any) -> None:
    """Put `items` in `view`'s model in one insert, then select `anchor` again.

    2026-10-04, code review: `ResultsView._rebuild` appended one row at a time
    and set the current index inside that loop - every rebuild (each tier,
    the details redraw, a chevron, a preference) fired `selected` mid-fill and
    the preview pane started reading a file before the list was whole. Now
    the rows go in at once, `selected` is quiet while they do (`_refilling`),
    and fires once afterwards - only when what the pane would show changed.
    The one function here that reaches into the view: it is the view's fill.
    """
    from app.ui.presenter import row_identity

    before = _shown(view.current_row())
    view._refilling = True
    try:
        view._model.clear()
        if items:
            view._model.invisibleRootItem().appendRows(items)
        for number, item in enumerate(items if anchor is not None else ()):
            if row_identity(item.data(ROLE_PAYLOAD)) == anchor:          # item 5d
                view._list.setCurrentIndex(view._model.index(number, 0))
                break
    finally:
        view._refilling = False
    after = view.current_row()
    if after is not None and _shown(after) != before:
        view.selected.emit(after)


def _shown(row: Any) -> Any:
    """What the preview pane shows of a row - its identity and its words."""
    if row is None:
        return None
    return tuple(getattr(row, name, None)
                 for name in ("chunk_id", "file_id", "path", "name", "folder", "search_id"))


def terminator_item(text: str) -> QStandardItem:
    """Item 5c: not selectable or activatable - a fact, not a result."""
    item = QStandardItem()
    item.setEditable(False)
    item.setData(Terminator(text), ROLE_PAYLOAD)
    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsSelectable & ~Qt.ItemFlag.ItemIsEnabled)
    item.setData(text, int(_ROLE.AccessibleTextRole))
    return item


def show_result_menu(view: Any, point: Any, show: Any = show_for) -> None:
    """One menu, shared with the filename browser - see `widgets/file_menu.py`.

    Viewport coordinates: the signal gives a point relative to the widget and
    `indexAt` wants one relative to the viewport. Getting this wrong is why
    right-click looked broken - it acted a row low and found nothing at all on
    the last row.
    """
    index = view._list.indexAt(viewport_point(view._list, point))
    row = view._row_for(index.data(ROLE_PAYLOAD) if index.isValid() else None)
    if row is None:
        selected = view.selected_rows()
        row = selected[0] if selected else None
    if row is None:
        return
    # **"Why this result?" keeps the explanation reachable.** It came off every
    # row to stop it competing with the name; it must not become unavailable,
    # because being able to ask is where trust comes from.
    terms, prefs = view.explain_context() if view.explain_context else ((), {})
    show(view._list, point, row.path, FileActions(
        # What the list already knows, never a stat here (2026-10-04).
        row=row, missing=row.path in getattr(view, "_missing", ()),
        offline=int(getattr(row, "file_id", 0) or 0) in getattr(view, "_volumes", {}),
        open_file=lambda: view.opened.emit(row),
        reveal=lambda: view.reveal_requested.emit(row),
        reindex=lambda: view.reindex_requested.emit(row),
        pin=lambda: view.pin_requested.emit(row),
        # Offered for every row, photos included (work order 0h section 2d):
        # `similar_requested` carries whichever row was right-clicked, and the
        # handler (`result_tools._run_similar`) knows whether this chunk_id is a
        # real passage or a photo's file_id wearing one.
        similar=lambda: view.similar_requested.emit(row),
        explain=(lambda: show_why(view, row, terms, prefs))
        if explain_switch_on(prefs) else None,
        same_period=lambda: view.period_requested.emit(row),
        copy=[("Why this result?", why(row))],
    ))
