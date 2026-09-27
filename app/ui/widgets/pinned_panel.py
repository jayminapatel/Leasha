r"""The panel results are gathered into. Workspace §3c.

Layer: L5 widget — thin, like every widget here. What is in the set and what
order it is in is `app/ui/pinned.py`; this draws a list and four buttons.

**Adapted from `docs/_superseded/working_set.py`.** The draft's list was a
`QListWidget`, whose own drag-out is Qt's internal item format, not a real
file — `setDragEnabled(True)` alone does not make a `QListWidgetItem` drag as
a file, only as itself. It reads as solved because the line is right there
and the comment beside it says "§3b again" as though 3b already covered it;
it does not, until the model actually hands back file URLs. Swapped for a
`QListView` over `DraggableResultsModel` - the same model `results_view.py`
uses - so "drag the lot into an email" is the same mechanism twice, not two.

**Cleared explicitly, never by a new search.** That is the whole difference
between a working set and a results list, and it is a property of *not*
wiring something rather than of writing something — so a test asserts it.

**Off-able, per §6.** A checkbox in its own header hides the panel; the
choice is a plain `index_state` flag, read before the panel is even built, so
turning it off costs nothing beyond not constructing the widget.
"""

from __future__ import annotations

from typing import Any, Optional

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QStandardItem
from PyQt6.QtWidgets import (
    QAbstractItemView, QCheckBox, QGroupBox, QHBoxLayout, QLabel, QListView,
    QPushButton, QVBoxLayout, QWidget,
)

from app.ui import pinned
from app.ui.result_delegate import ROLE_PAYLOAD
from app.ui.widgets.result_drag_model import DraggableResultsModel
from app.ui.widgets.buttons import style_all

__all__ = ["PinnedPanel", "PANEL_ENABLED_KEY"]

#: Whether the panel shows at all. §6: every new behaviour is off-able.
PANEL_ENABLED_KEY = "ui:pinned_panel_enabled"


class PinnedPanel(QGroupBox):
    """Documents gathered across several searches, and what to do with them."""

    #: `{key: value}` to store — one `index_state` row. §3c: it survives.
    remember = pyqtSignal(dict)
    #: Open every one of these paths.
    open_all = pyqtSignal(list)
    #: The set changed, so a results list can retick its buttons.
    changed = pyqtSignal(tuple)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__("Pinned working set", parent)
        self._pins: tuple = ()

        self._model = DraggableResultsModel(self)
        self.list = QListView()
        self.list.setModel(self._model)
        self.list.setAccessibleName("Pinned working set")
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.list.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        # §3b's own mechanism, reused rather than reimplemented: a gathered
        # document drags out exactly like a fresh result does.
        self.list.setDragEnabled(True)

        self.summary = QLabel("")
        self.summary.setObjectName("resultMeta")

        self.open_button = self._button(
            "Open them all", "Opens every document in this list.",
            self._open_all)
        self.copy_button = self._button(
            "Copy the paths", "Copies every path here to the clipboard, one "
            "per line.", self._copy)
        self.remove_button = self._button(
            "Remove", "Takes the selected documents out of this list. The "
            "files themselves are not touched.", self._remove)
        self.clear_button = self._button(
            "Clear the list", "Empties this list. Nothing else changes, and "
            "no file is touched.", self.clear)

        buttons = QHBoxLayout()
        for button in (self.open_button, self.copy_button,
                       self.remove_button, self.clear_button):
            buttons.addWidget(button)
        buttons.addStretch(1)

        layout = QVBoxLayout(self)
        layout.addWidget(self.summary)
        layout.addWidget(self.list, stretch=1)
        layout.addLayout(buttons)
        self._redraw(remember=False)
        # The button system (widgets/buttons.py): every action button in
        # here gets its icon, its kind and its natural width.
        style_all(self)

    def _button(self, label: str, tip: str, on_click: Any) -> QPushButton:
        button = QPushButton(label)
        button.setToolTip(tip)
        button.clicked.connect(lambda _checked=False: on_click())
        return button

    # -- what is in it --------------------------------------------------------

    @property
    def pins(self) -> tuple:
        return self._pins

    @property
    def paths(self) -> tuple:
        return pinned.paths(self._pins)

    def restore(self, state: Any) -> None:
        """Put back what was gathered last time. Never raises."""
        try:
            self._pins = pinned.decode((state or {}).get(pinned.PINNED_KEY))
        except Exception:                        # noqa: BLE001 - a list
            self._pins = ()
        self._redraw(remember=False)

    def pin(self, row: Any) -> None:
        """Gather one result. Already-there is not an error — see `pinned`."""
        self._pins = pinned.add(self._pins, row)
        self._redraw()

    def clear(self) -> None:
        """**Explicitly, and only explicitly.** See the module docstring."""
        self._pins = ()
        self._redraw()

    def _remove(self) -> None:
        wanted = {index.data(ROLE_PAYLOAD).path
                 for index in self.list.selectedIndexes()
                 if index.data(ROLE_PAYLOAD) is not None}
        for path in wanted:
            self._pins = pinned.remove(self._pins, path)
        self._redraw()

    def _redraw(self, *, remember: bool = True) -> None:
        self._model.clear()
        for pin in self._pins:
            item = QStandardItem()
            item.setEditable(False)
            item.setData(pin, ROLE_PAYLOAD)
            item.setData(pin.label, int(Qt.ItemDataRole.DisplayRole))
            item.setData(pin.path, int(Qt.ItemDataRole.ToolTipRole))
            self._model.appendRow(item)

        self.summary.setText(pinned.summary(self._pins))
        for button in (self.open_button, self.copy_button,
                       self.remove_button, self.clear_button):
            button.setEnabled(bool(self._pins))
        self.changed.emit(self._pins)
        if remember:
            self.remember.emit({pinned.PINNED_KEY: pinned.encode(self._pins)})

    # -- acting together ------------------------------------------------------

    def _open_all(self) -> None:
        found = list(self.paths)
        if found:
            self.open_all.emit(found)

    def _copy(self) -> None:
        r"""Every path, one per line. **Never raises.**

        One per line because that is what pastes usefully into a message, a
        spreadsheet column and a command line alike - and because a path may
        contain any separator anybody would otherwise reach for.
        """
        try:
            from PyQt6.QtWidgets import QApplication

            clipboard = QApplication.clipboard()
            if clipboard is not None:
                clipboard.setText("\n".join(self.paths))
        except Exception:                        # noqa: BLE001 - a copy
            return


def enabled_checkbox(store: Any, *, on_toggle: Any) -> QCheckBox:
    """The "Pinned working set" on/off switch. §6: off-able, tooltip stated.

    Read once, at construction, from `index_state` — the same pattern every
    other view preference in this codebase follows (`view_options.load_prefs`)
    — so a locked or missing database still opens the window, with the panel
    on by default.
    """
    box = QCheckBox("Pinned working set")
    box.setToolTip(
        "Keep a panel beside the results where you can gather documents from "
        "several searches, then open, copy or drag them all together."
    )
    default_on = True
    try:
        raw = store.get_state(PANEL_ENABLED_KEY, None) if store is not None else None
    except Exception:                            # noqa: BLE001 - a preference
        raw = None
    box.setChecked(default_on if raw is None else raw not in ("off", "0", "false"))
    box.toggled.connect(on_toggle)
    return box
