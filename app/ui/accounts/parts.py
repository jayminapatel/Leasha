"""Small shared widgets of Settings > Accounts: a hint line, a button, a confirm, a schedule row.

Layer: L5
"""

from __future__ import annotations

from typing import Any, Optional

from PySide6.QtCore import QTime, Signal
from PySide6.QtWidgets import (
    QComboBox, QHBoxLayout, QLabel, QMessageBox, QPushButton, QTimeEdit, QWidget,
)

from app.ui.widgets.buttons import action_button, style_button

__all__ = ["hint", "btn", "confirm", "notice", "ScheduleRow", "set_quiet", "restyle_hints", "open_guide"]


def _dim() -> str:
    from app.ui.theme import theme_colours  # noqa: PLC0415
    return theme_colours().get("text_dim", "palette(mid)")


def hint(text: str, parent: Optional[QWidget] = None) -> QLabel:
    """The plain sentence under a value, saying what it does."""
    label = QLabel(text, parent)
    label.setObjectName("accountsHint")
    label.setWordWrap(True)
    label.setStyleSheet(f"color: {_dim()};")
    return label


def restyle_hints(root: QWidget) -> None:
    for label in root.findChildren(QLabel, "accountsHint"):
        label.setStyleSheet(f"color: {_dim()};")


def notice(parent: Optional[QWidget] = None) -> QLabel:
    """A line for the outcome of an action; empty until there is one."""
    label = QLabel("", parent)
    label.setObjectName("noticeBar")
    label.setWordWrap(True)
    label.setVisible(False)
    return label


def btn(text: str, glyph: str, role: str = "secondary", tip: str = "", name: str = "") -> QPushButton:
    b = action_button(text, glyph, role)
    if tip:
        b.setToolTip(tip)
    b.setAccessibleName(name or text)
    return b


def confirm(parent: Optional[QWidget], title: str, text: str, ok: str, *, danger: bool = False) -> bool:
    """A question that states the cost. Tests replace this function."""
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Icon.Warning if danger else QMessageBox.Icon.Question)
    box.setWindowTitle(title)
    box.setText(text)
    yes = box.addButton(ok, QMessageBox.ButtonRole.AcceptRole)
    no = box.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
    style_button(yes, "trash-2" if danger else "check", "danger" if danger else "primary")
    style_button(no, "x", "secondary")
    box.setDefaultButton(no)
    box.exec()
    return box.clickedButton() is yes


def set_quiet(widget: Any, value: Any, setter: str) -> None:
    """Show a stored value without emitting on the way in."""
    blocked = widget.blockSignals(True)
    try:
        getattr(widget, setter)(value)
    finally:
        widget.blockSignals(blocked)


class ScheduleRow(QWidget):
    """One schedule (10g): Manual / Every ... / Daily at a time. Saves at once via `changed`."""

    changed = Signal(object)          # a ScheduleSetting

    def __init__(self, title: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        from app.ui.accounts import logic  # noqa: PLC0415
        from app.ui.widgets.debounce import Debounced  # noqa: PLC0415

        self._logic = logic
        self.title = title
        self.mode = QComboBox()
        self.mode.setAccessibleName(f"{title} schedule")
        for key, text in logic.MODE_CHOICES:
            self.mode.addItem(text, key)
        self.every = QComboBox()
        self.every.setAccessibleName(f"{title} - how often")
        self.daily_at = QTimeEdit()
        self.daily_at.setDisplayFormat("HH:mm")
        self.daily_at.setAccessibleName(f"{title} - time of day")
        self._minutes = 60
        self._fill_every(60)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        for w in (self.mode, self.every, self.daily_at):
            lay.addWidget(w)
        lay.addStretch(1)
        self._save = Debounced(self._emit, parent=self)
        self.mode.currentIndexChanged.connect(self._mode_changed)
        self.every.currentIndexChanged.connect(lambda _i: self._changed())
        self.daily_at.timeChanged.connect(lambda _t: self._save())
        self._rows()

    def _fill_every(self, minutes: int) -> None:
        blocked = self.every.blockSignals(True)
        self.every.clear()
        for m, text in self._logic.interval_items(minutes):
            self.every.addItem(text, m)
        self.every.setCurrentIndex(max(0, self.every.findData(int(minutes))))
        self.every.blockSignals(blocked)

    def _rows(self) -> None:
        mode = self.mode.currentData()
        self.every.setVisible(mode == "interval")
        self.daily_at.setVisible(mode == "daily")

    def _mode_changed(self, _i: int) -> None:
        # D11.3: from Manual to a timed schedule the interval starts at 1 hour.
        if self.mode.currentData() == "interval" and self._was_manual:
            self._fill_every(60)
        self._was_manual = self.mode.currentData() == "manual"
        self._changed()

    _was_manual = True

    def _changed(self) -> None:
        self._rows()
        self._save()

    def value(self) -> Any:
        from app.accounts.store import ScheduleSetting  # noqa: PLC0415
        t = self.daily_at.time()
        return ScheduleSetting(str(self.mode.currentData() or "manual"),
                               int(self.every.currentData() or 60), f"{t.hour():02d}:{t.minute():02d}")

    def _emit(self) -> None:
        self.changed.emit(self.value())

    def set_value(self, sc: Any) -> None:
        self._save.cancel()
        for w in (self.mode, self.every, self.daily_at):
            w.blockSignals(True)
        try:
            self.mode.setCurrentIndex(max(0, self.mode.findData(sc.mode)))
            self._fill_every(int(sc.minutes))
            h, _, m = str(sc.daily_at).partition(":")
            self.daily_at.setTime(QTime(int(h or 2), int(m or 0)))
            self._was_manual = sc.mode == "manual"
        finally:
            for w in (self.mode, self.every, self.daily_at):
                w.blockSignals(False)
        self._rows()

    def flush(self) -> None:
        self._save.flush()


def open_guide(anchor: str) -> bool:
    """Open the user guide at the section for a set-up step (1a). Tests replace this function."""
    from PySide6.QtCore import QUrl  # noqa: PLC0415
    from PySide6.QtGui import QDesktopServices  # noqa: PLC0415

    from app.ui.accounts import logic  # noqa: PLC0415

    path = logic.guide_path()
    if path is None:
        return False
    url = QUrl.fromLocalFile(str(path))
    url.setFragment(anchor)
    return bool(QDesktopServices.openUrl(url))
