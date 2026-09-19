"""The Chat group in Settings: the few things about chat a person may change.

Layer: L5 view

Work order 202626270611 3e and 4d. Which model answers is a setting; how many
search rounds it may take and how strict the checking is are tuning, and stay
out of sight unless somebody goes looking (the Manual mode).

**Driven by the registry, not by a list here.** The engine declares its chat
settings in `app/core/settings_registry.py` (keys starting `CHAT_`); this group
builds one control per such key from the declaration - a tick box for a bool, a
number box for an int, a drop-down for a choice, a text box otherwise - and
gives each `setObjectName(<the key>)`, which is what
`tests/unit/test_settings_reachable.py` and the settings filter look controls
up by. A key added to the registry gets its control with no edit here.

Sends `{KEY: value}` on `changed`, wired by `SettingsView` to its
`settings_changed` signal like every other group, so the window writes `.env`.
"""

from __future__ import annotations

from typing import Any, Optional

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QFormLayout, QGroupBox, QLineEdit, QSpinBox, QWidget,
)

from app.core import settings_registry as reg

__all__ = ["ChatBox", "chat_settings"]

PREFIX = "CHAT_"


def chat_settings() -> list[Any]:
    """The registry entries this group is responsible for."""
    return [s for s in reg.SETTINGS if s.key.startswith(PREFIX) or s.group == "Chat"]


class ChatBox(QGroupBox):
    changed = pyqtSignal(dict)

    def __init__(self, settings: Any = None, parent: Optional[QWidget] = None) -> None:
        super().__init__("Chat", parent)
        self.controls: dict[str, QWidget] = {}
        form = QFormLayout(self)
        found = chat_settings()
        for setting in found:
            control = self._make(setting)
            control.setObjectName(setting.key)
            control.setToolTip(setting.help or setting.label)
            control.setAccessibleName(setting.label)
            self.controls[setting.key] = control
            if isinstance(control, QCheckBox):
                form.addRow(control)
            else:
                form.addRow(setting.label, control)
        self.setVisible(bool(found))
        if settings is not None:
            self.load(settings)

    def _make(self, setting: Any) -> QWidget:
        if setting.kind == "bool":
            box = QCheckBox(setting.label)
            box.setToolTip(setting.help or setting.label)
            box.toggled.connect(lambda _on, s=setting: self._emit(s.key))
            return box
        if setting.kind == "int":
            spin = QSpinBox()
            spin.setToolTip(setting.help or setting.label)
            spin.setRange(int(setting.minimum or 0), int(setting.maximum or 1_000_000))
            if setting.unit:
                spin.setSuffix(f" {setting.unit}")
            spin.setKeyboardTracking(False)
            spin.valueChanged.connect(lambda _v, s=setting: self._emit(s.key))
            return spin
        if setting.kind == "choice":
            combo = QComboBox()
            combo.setToolTip(setting.help or setting.label)
            for choice in setting.choices:
                combo.addItem(choice, choice)
            combo.currentIndexChanged.connect(lambda _i, s=setting: self._emit(s.key))
            return combo
        line = QLineEdit()
        line.setToolTip(setting.help or setting.label)
        line.setAccessibleName(setting.label)
        line.setPlaceholderText(str(setting.default or ""))
        line.editingFinished.connect(lambda s=setting: self._emit(s.key))
        return line

    @staticmethod
    def _read(control: QWidget) -> Any:
        if isinstance(control, QCheckBox):
            return control.isChecked()
        if isinstance(control, QSpinBox):
            return control.value()
        if isinstance(control, QComboBox):
            return control.currentData()
        return control.text().strip()

    def values(self) -> dict:
        return {key: self._read(control) for key, control in self.controls.items()}

    def _emit(self, key: str) -> None:
        self.changed.emit({key: self._read(self.controls[key])})

    def load(self, settings: Any) -> None:
        """Fill from Settings without emitting - see `EditorBox.load` for why."""
        for setting in chat_settings():
            control = self.controls.get(setting.key)
            if control is None:
                continue
            value = getattr(settings, setting.key.lower(), setting.default)
            control.blockSignals(True)
            try:
                if isinstance(control, QCheckBox):
                    control.setChecked(bool(value))
                elif isinstance(control, QSpinBox):
                    control.setValue(int(value))
                elif isinstance(control, QComboBox):
                    index = control.findData(value)
                    control.setCurrentIndex(index if index >= 0 else 0)
                else:
                    control.setText(str(value or ""))
            except (TypeError, ValueError):
                pass
            finally:
                control.blockSignals(False)
