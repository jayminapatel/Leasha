"""Which model answers: a drop-down on the Chat tab, a menu for Interpret.

Layer: L5 view - thin.

2026-10-04, the owner: "if multiple models are available they should be listed so
they can be changed at chat or search time". The list itself - the models inside
Leasha that are downloaded and the ones Ollama has installed - is made on a worker
(`app.ui.tasks.answer_model_menu`) from `app.chat.roles.answer_options`; these two
controls only show it and say which one was picked. **Shown only with two or more
to choose from**: a one-line list is not a choice.

Nothing here touches the store, the disk or the network.
"""

from __future__ import annotations

from typing import Any, Optional, Sequence

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QAction, QActionGroup
from PySide6.QtWidgets import QComboBox, QLabel, QMenu, QWidget

__all__ = ["ModelPicker", "ModelMenu", "SETTINGS_CHOICE"]

PICKER_TIP = ("Which model writes the answers. Bigger models are usually better and "
              "slower; \"in Leasha\" models need nothing else running. Applies to "
              "your next question.")
MENU_TITLE = "Interpret with"
MENU_TIP = "Which model Interpret uses to turn a sentence into a search."
#: 2026-10-04, code review: shown when the model Settings chose is not in the list
#: (Ollama not answering, say). The list used to fall to its first entry - a model
#: that was not the one answering. Picking it means "the one Settings chose".
SETTINGS_CHOICE = "Settings' choice"
SETTINGS_CHOICE_TIP = "The model chosen in Settings, Models."
WARNING_TIP = "About the model picked in the list beside this."


def _note_for(option: Any) -> str:
    from app.chat.roles import large_model_note

    try:
        return large_model_note(option)
    except Exception:                                    # noqa: BLE001 - a courtesy only
        return ""


class ModelPicker(QComboBox):
    """The Chat tab's model drop-down. `chosen(value)` only when the person picks.

    `warning` (2026-10-04, code review) is a line the view puts beside it: what a
    model bigger than `roles.AFFORDABLE_MAX_B` costs, while one is picked."""

    chosen = Signal(str)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("chatModelPicker")
        self.setAccessibleName("Which model answers")
        self.setToolTip(PICKER_TIP)
        # The labels carry a size; the box need not be as wide as the longest.
        self.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.setMinimumContentsLength(16)
        self.setVisible(False)
        self.warning = QLabel("")
        self.warning.setObjectName("chatModelWarning")
        self.warning.setAccessibleName("About the model picked")
        self.warning.setToolTip(WARNING_TIP)
        self.warning.setWordWrap(True)
        self.warning.setVisible(False)
        self._options: dict[str, Any] = {}
        self._real = 0
        self.activated.connect(lambda index: self.chosen.emit(str(self.itemData(index) or "")))
        self.currentIndexChanged.connect(lambda _index: self._refresh_warning())

    def set_options(self, options: Sequence[Any], selected: str = "") -> None:
        """Fill the list (`ModelOption`s) and show `selected`, without emitting.

        2026-10-04, code review: when `selected` is not among them the list shows
        `SETTINGS_CHOICE` rather than its first entry, which was not answering."""
        self.blockSignals(True)
        self.clear()
        self._options = {str(o.value): o for o in options}
        self._real = len(options)
        for option in options:
            self.addItem(str(option.label), str(option.value))
            self.setItemData(self.count() - 1, str(option.label), Qt.ItemDataRole.ToolTipRole)
        index = self.findData(selected) if selected else -1
        if index < 0 and self.count():
            self.insertItem(0, SETTINGS_CHOICE, "")
            self.setItemData(0, SETTINGS_CHOICE_TIP, Qt.ItemDataRole.ToolTipRole)
            index = 0
        self.setCurrentIndex(index if self.count() else -1)
        self.blockSignals(False)
        self.setVisible(self._real >= 2)
        self._refresh_warning()

    def value(self) -> str:
        return str(self.currentData() or "")

    def warning_text(self) -> str:
        return self.warning.text()

    def _refresh_warning(self) -> None:
        text = _note_for(self._options.get(self.value())) if self.value() else ""
        self.warning.setText(text)
        # Never shown on its own: a label with no window yet would open as one.
        self.warning.setVisible(bool(text) and self._real >= 2
                                and self.warning.parent() is not None)


class ModelMenu(QMenu):
    """Interpret's model, as a sub-menu of the Search page's `⋯` menu.

    `follow(action)` keeps it shown only while that action (Interpret) is."""

    chosen = Signal(str)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(MENU_TITLE, parent)
        self.setObjectName("interpretModelMenu")
        self.setToolTipsVisible(True)
        self.menuAction().setToolTip(MENU_TIP)
        self._group = QActionGroup(self)
        self._group.setExclusive(True)
        self._follows: Optional[QAction] = None
        self._count = 0
        self.menuAction().setVisible(False)

    def follow(self, action: QAction) -> None:
        self._follows = action
        action.changed.connect(self._refresh_visible)
        self._refresh_visible()

    def set_options(self, options: Sequence[Any], selected: str = "") -> None:
        for action in list(self._group.actions()):
            self._group.removeAction(action)
            self.removeAction(action)
            action.deleteLater()
        rows = [(str(o.label), str(o.value), _note_for(o) or str(o.label)) for o in options]
        if options and not any(value == selected for _l, value, _t in rows):
            # 2026-10-04, code review: the first entry was ticked though it was not
            # the model Interpret used.
            rows.insert(0, (SETTINGS_CHOICE, "", SETTINGS_CHOICE_TIP))
        for label, value, tip in rows:
            action = QAction(label, self)
            action.setCheckable(True)
            action.setData(value)
            action.setToolTip(tip)
            action.setChecked(value == selected or (not value and selected not in
                                                    [v for _l, v, _t in rows]))
            action.triggered.connect(lambda _checked=False, v=value: self.chosen.emit(v))
            self._group.addAction(action)
            self.addAction(action)
        self._count = len(options)
        self._refresh_visible()

    def value(self) -> str:
        checked = self._group.checkedAction()
        return str(checked.data() or "") if checked is not None else ""

    def _refresh_visible(self) -> None:
        shown = self._follows is None or self._follows.isVisible()
        self.menuAction().setVisible(bool(shown and self._count >= 2))
