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

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QAction, QActionGroup
from PyQt6.QtWidgets import QComboBox, QMenu, QWidget

__all__ = ["ModelPicker", "ModelMenu"]

PICKER_TIP = ("Which model writes the answers. Bigger models are usually better and "
              "slower; \"in Leasha\" models need nothing else running. Applies to "
              "your next question.")
MENU_TITLE = "Interpret with"
MENU_TIP = "Which model Interpret uses to turn a sentence into a search."


class ModelPicker(QComboBox):
    """The Chat tab's model drop-down. `chosen(value)` only when the person picks."""

    chosen = pyqtSignal(str)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("chatModelPicker")
        self.setAccessibleName("Which model answers")
        self.setToolTip(PICKER_TIP)
        # The labels carry a size; the box need not be as wide as the longest.
        self.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.setMinimumContentsLength(16)
        self.setVisible(False)
        self.activated.connect(lambda index: self.chosen.emit(str(self.itemData(index) or "")))

    def set_options(self, options: Sequence[Any], selected: str = "") -> None:
        """Fill the list (`ModelOption`s) and show `selected`, without emitting."""
        self.blockSignals(True)
        self.clear()
        for option in options:
            self.addItem(str(option.label), str(option.value))
            self.setItemData(self.count() - 1, str(option.label), Qt.ItemDataRole.ToolTipRole)
        index = self.findData(selected) if selected else -1
        self.setCurrentIndex(max(index, 0) if self.count() else -1)
        self.blockSignals(False)
        self.setVisible(self.count() >= 2)

    def value(self) -> str:
        return str(self.currentData() or "")


class ModelMenu(QMenu):
    """Interpret's model, as a sub-menu of the Search page's `⋯` menu.

    `follow(action)` keeps it shown only while that action (Interpret) is."""

    chosen = pyqtSignal(str)

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
        for option in options:
            action = QAction(str(option.label), self)
            action.setCheckable(True)
            action.setData(str(option.value))
            action.setChecked(str(option.value) == selected)
            action.triggered.connect(
                lambda _checked=False, v=str(option.value): self.chosen.emit(v))
            self._group.addAction(action)
            self.addAction(action)
        if options and not any(a.isChecked() for a in self._group.actions()):
            self._group.actions()[0].setChecked(True)
        self._count = len(options)
        self._refresh_visible()

    def value(self) -> str:
        checked = self._group.checkedAction()
        return str(checked.data() or "") if checked is not None else ""

    def _refresh_visible(self) -> None:
        shown = self._follows is None or self._follows.isVisible()
        self.menuAction().setVisible(bool(shown and self._count >= 2))
