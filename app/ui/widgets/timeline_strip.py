r"""A thin band showing when the results cluster in time. Workspace §3d.

Layer: L5 widget — the bucketing and the wording are `app/ui/timeline.py`,
Qt-free and tested without a strip; this only draws bars and turns a click
into a signal. No draft existed for this half - `docs/_superseded` held the
Qt-free bucketing only - so this is new, built against `Band`'s own shape.

**A row of buttons, not a painted canvas.** Twelve bars at most (`MAX_BANDS`),
each already a click target with its own tooltip and accessible name for
free - a hand-painted strip would have to build hit-testing and a tooltip
event filter to get the same thing a `QToolButton` already does correctly.

**Clicking composes a filter, never replaces the query.** §3d is explicit:
*no new query semantics.* The strip appends `after:`/`before:` to whatever is
already typed, through the same box everything else in this application
writes into - so it is undoable by editing the text, exactly like every other
filter here.

**Off-able, per §6.** its own checkbox, read from `index_state` before this
widget is even built - see `enabled_checkbox`, the same shape as the pinned
panel's.
"""

from __future__ import annotations

from typing import Any, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QCheckBox, QHBoxLayout, QMenu, QToolButton, QWidget

from app.ui.timeline import bands
from app.ui.qtsip import open_menu

__all__ = ["TimelineStrip", "STRIP_ENABLED_KEY"]

#: Whether the strip shows at all. §6: every new behaviour is off-able.
STRIP_ENABLED_KEY = "ui:timeline_strip_enabled"


class TimelineStrip(QWidget):
    """The band itself: a row of bars, widest where the hits are thickest."""

    #: The filter text a clicked bar stands for - `after:… before:…`.
    filter_chosen = Signal(str)
    #: `(after, before)` - a period's bar was right-clicked and "See everything
    #: from this period" chosen. Opens the Life Timeline there (order 0n 4b);
    #: it does not touch the search, which is what a left click is for.
    browse_requested = Signal(str, str)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setAccessibleName("Result timeline")
        self._layout = QHBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(1)
        self.setFixedHeight(22)
        self.setVisible(False)          # nothing to show until rows arrive
        self._enabled = True
        self._bands: tuple = ()

    def set_enabled(self, enabled: bool) -> None:
        """§6's off switch. The bars already computed are kept, just not drawn -
        so turning the strip back on does not wait for the next search."""
        self._enabled = bool(enabled)
        self._redraw(self._bands if self._enabled else ())

    def set_rows(self, rows: Any) -> None:
        """Recompute the bars from the current result set. Never raises."""
        try:
            self._bands = bands(rows or ())
        except Exception:                # noqa: BLE001 - a decoration, not a result
            self._bands = ()
        self._redraw(self._bands if self._enabled else ())

    def _menu(self, button: QToolButton, point: Any, band: Any) -> None:
        """Right-click on a bar: the other thing you might want from a period."""
        menu = QMenu(button)
        action = menu.addAction("See everything from this period")
        action.setToolTip("Open your timeline on this period - photos, files and mail from "
                          "then, not only the results of this search.")
        action.triggered.connect(lambda: self.browse_requested.emit(band.after, band.before))
        open_menu(menu, button.mapToGlobal(point))

    def _redraw(self, found: tuple) -> None:
        """Rebuild the bars; each is as wide as its share of the hits."""
        while self._layout.count():
            item = self._layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

        self.setVisible(bool(found))
        if not found:
            return

        total = sum(band.count for band in found) or 1
        for band in found:
            button = QToolButton(self)
            button.setText(band.label)
            button.setToolTip(
                f"{band.count} result{'s' if band.count != 1 else ''} "
                f"between {band.after} and {band.before}.\nClick to filter "
                f"the search to this period.\nRight-click to browse everything "
                f"from this period."
            )
            button.setAutoRaise(True)
            button.clicked.connect(
                lambda _checked=False, text=band.filter_text: self.filter_chosen.emit(text))
            button.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            button.customContextMenuRequested.connect(
                lambda point, b=button, band=band: self._menu(b, point, band))
            self._layout.addWidget(button, stretch=max(1, band.count * 100 // total))


def enabled_checkbox(store: Any, *, on_toggle: Any) -> QCheckBox:
    """The "Timeline" on/off switch. §6: off-able, tooltip stated.

    Same shape as `pinned_panel.enabled_checkbox` - on by default, read once
    from `index_state`, never raising against a locked or missing database.
    """
    box = QCheckBox("Timeline")
    box.setToolTip(
        "Show a thin band above the results marking when they cluster in "
        "time. Click a period to add it to the search as after:/before:."
    )
    default_on = True
    try:
        raw = store.get_state(STRIP_ENABLED_KEY, None) if store is not None else None
    except Exception:                            # noqa: BLE001 - a preference
        raw = None
    box.setChecked(default_on if raw is None else raw not in ("off", "0", "false"))
    box.toggled.connect(on_toggle)
    return box
