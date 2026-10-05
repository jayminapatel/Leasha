r"""How the window behaves when you are not using it.

Layer: L5

Two checkboxes, and they were both unreachable. `ui:tray_minimise` and
`ui:tray_close` were read at startup and written by **nothing**, so the tray
code could never run: off by default, with no way to turn either on.

Off by default is still right. An application that vanishes from the taskbar
when you did not ask it to is alarming - you close a window, it disappears, and
there is no obvious way back. But "off unless asked" and "no way to ask" are
different things, and only one of them was implemented.

Its own group because window behaviour is not indexing behaviour and not a
search preference, and because `settings_view.py` is at its length limit - the
rule that keeps views short being the rule that keeps logic out of them.

**Appearance lives here now.** It spent a while on the indexing panel, between
the memory ceiling and the low-priority switch, where it was neither an
indexing setting nor findable by anybody looking for one. Whether the window
follows the Windows light/dark setting is window behaviour, and this is the
window group; §4c-4 of the index-tuning order settled it so nobody has to
settle it again. The signal keeps its name, so nothing that listens changed.
"""

from __future__ import annotations

from typing import Any, Optional

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFormLayout, QGroupBox, QVBoxLayout, QWidget,
)

__all__ = ["WindowBox"]


class WindowBox(QGroupBox):
    """Minimise and close behaviour, and which colours the window uses."""

    #: (minimise_to_tray, close_to_tray). Both together, because the window
    #: applies them as a pair and installing the tray icon depends on either.
    changed = Signal(bool, bool)
    #: system | light | dark
    theme_changed = Signal(str)
    #: UI Redesign (202626160950 §5c): animate panels, off by default.
    motion_changed = Signal(bool)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__("Window", parent)

        self.minimise_to_tray = QCheckBox("Minimise to the notification area")
        self.minimise_to_tray.setToolTip(
            "Minimising hides the window to the tray icon rather than the "
            "taskbar. The application keeps running either way."
        )

        self.close_to_tray = QCheckBox("Closing the window keeps it running there")
        self.close_to_tray.setToolTip(
            "The window closes but the application keeps running, so searching "
            "is instant when you come back. Quit properly from the tray icon."
        )

        for box in (self.minimise_to_tray, self.close_to_tray):
            box.stateChanged.connect(lambda _s: self.changed.emit(
                self.minimise_to_tray.isChecked(), self.close_to_tray.isChecked()))

        self.theme = QComboBox()
        self.theme.addItem("Follow Windows", "system")
        self.theme.addItem("Always light", "light")
        self.theme.addItem("Always dark", "dark")
        self.theme.setToolTip(
            "The app follows your Windows light/dark setting by default,\n"
            "and switches immediately when you change it."
        )
        self.theme.currentIndexChanged.connect(
            lambda _i: self.theme_changed.emit(
                str(self.theme.currentData() or "system"))
        )

        appearance = QFormLayout()
        appearance.addRow("Appearance", self.theme)

        # §5c. **Off by default**, and the only perceivable motion the redesign
        # adds (the preview pane sliding open) is gated on it - the standing
        # rule that every new behaviour is off-able, applied before it ships.
        # Stored as `ui:motion` keyed state beside `ui:theme`, the same home
        # every other Appearance switch has; the registry is for `.env` keys.
        self.motion = QCheckBox("Animate panels when they open and close")
        self.motion.setObjectName("UI_MOTION")
        self.motion.setToolTip(
            "Slide the preview pane open and closed instead of showing it at "
            "once. Off by default; leave it off if motion bothers you."
        )
        self.motion.setChecked(False)
        self.motion.toggled.connect(self.motion_changed.emit)

        layout = QVBoxLayout(self)
        layout.addWidget(self.minimise_to_tray)
        layout.addWidget(self.close_to_tray)
        layout.addLayout(appearance)
        layout.addWidget(self.motion)

    def load(self, minimise: bool, close: bool,
             theme: Optional[Any] = None, motion: Optional[bool] = None) -> None:
        """Show the stored preferences without emitting on the way in."""
        for box, value in ((self.minimise_to_tray, minimise),
                           (self.close_to_tray, close)):
            box.blockSignals(True)
            box.setChecked(bool(value))
            box.blockSignals(False)
        if theme is not None:
            self.set_theme(theme)
        if motion is not None:
            self.motion.blockSignals(True)
            self.motion.setChecked(bool(motion))
            self.motion.blockSignals(False)

    def set_theme(self, preference: Any) -> None:
        """Show a stored theme choice without emitting."""
        self.theme.blockSignals(True)
        try:
            found = self.theme.findData(str(preference or "system"))
            self.theme.setCurrentIndex(found if found >= 0 else 0)
        finally:
            self.theme.blockSignals(False)
