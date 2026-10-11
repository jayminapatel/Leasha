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
    QCheckBox, QComboBox, QFormLayout, QGroupBox, QLabel, QLineEdit, QSpinBox, QVBoxLayout,
    QWidget,
)

__all__ = ["WindowBox", "CUSTOM_DATE"]

#: The drop-down's last entry: type a format of your own.
CUSTOM_DATE = "custom"


class WindowBox(QGroupBox):
    """Minimise and close behaviour, and which colours the window uses."""

    #: (minimise_to_tray, close_to_tray). Both together, because the window
    #: applies them as a pair and installing the tray icon depends on either.
    changed = Signal(bool, bool)
    #: system | light | dark
    theme_changed = Signal(str)
    #: UI Redesign (202626160950 §5c): animate panels, off by default.
    motion_changed = Signal(bool)
    #: The body text size in pixels (2026-10-10). Applied at once, no restart.
    text_size_changed = Signal(int)
    #: The Files and Mail tabs' date format (2026-10-11), only ever a valid one.
    date_format_changed = Signal(str)

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
            # Both values travel together: the window installs or removes the tray
            # icon from the pair, not from one switch at a time.
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

        # **Window state, like the theme beside it** (`ui:text_size`): an Appearance choice,
        # not an `.env` key. Everything - labels, text boxes, lists - is a multiple of this
        # one size, so it is the whole app's text and it changes the moment it is set.
        from app.ui import theme as _theme
        from app.ui.widgets.number_field import fit

        low, high = _theme.TEXT_PX_RANGE
        self.text_size = QSpinBox()
        self.text_size.setObjectName("UI_TEXT_SIZE")
        self.text_size.setAccessibleName("Text size")
        self.text_size.setRange(low, high)
        self.text_size.setSuffix(" px")
        self.text_size.setKeyboardTracking(False)
        self.text_size.setValue(_theme.DEFAULT_TEXT_PX)
        self.text_size.setToolTip(
            "How big the normal text is, everywhere in Leasha: labels, text boxes, lists "
            f"and answers. {_theme.DEFAULT_TEXT_PX} px is the default. The change applies "
            "at once - no restart. Headings and small print follow it."
        )
        fit(self.text_size, default=_theme.DEFAULT_TEXT_PX)
        self.text_size.valueChanged.connect(self.text_size_changed.emit)

        # 2026-10-11, the owner: one date format for the Files and Mail tabs. Window state
        # (`ui:date_format`) like the text size, picked from the common ones or typed and
        # checked as it is typed (`app.core.date_format`); only a valid one is ever sent.
        from app.core import date_format as _dates

        self.date_format = QComboBox()
        self.date_format.setObjectName("UI_DATE_FORMAT")
        self.date_format.setAccessibleName("Date format")
        for pattern in _dates.PRESETS:
            self.date_format.addItem(f"{_dates.example(pattern)}   ({pattern})", pattern)
        self.date_format.addItem("Custom...", CUSTOM_DATE)
        self.date_format.setToolTip(
            "How dates are written in the Files and Mail tabs.\n"
            "mm is the month and nn the minutes, as in Excel.")
        self.date_custom = QLineEdit()
        self.date_custom.setObjectName("UI_DATE_FORMAT_CUSTOM")
        self.date_custom.setAccessibleName("Custom date format")
        self.date_custom.setPlaceholderText(_dates.DEFAULT)
        self.date_custom.setToolTip(
            "yyyy yy mmmm mmm mm dddd ddd dd hh nn ss am/pm,\n"
            "with spaces or - / . , : between them.")
        self.date_note = QLabel()
        self.date_note.setObjectName("UI_DATE_FORMAT_NOTE")
        self.date_note.setWordWrap(True)
        self.date_custom.hide()
        self.date_note.hide()
        self.date_format.currentIndexChanged.connect(lambda _i: self._date_picked())
        self.date_custom.textEdited.connect(self._date_typed)

        appearance = QFormLayout()
        appearance.addRow("Appearance", self.theme)
        appearance.addRow("Text size", self.text_size)
        appearance.addRow("Dates", self.date_format)
        appearance.addRow("", self.date_custom)
        appearance.addRow("", self.date_note)

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
             theme: Optional[Any] = None, motion: Optional[bool] = None,
             text_size: Optional[int] = None, date_format: Optional[str] = None) -> None:
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
        if text_size is not None:
            self.text_size.blockSignals(True)
            self.text_size.setValue(int(text_size))
            self.text_size.blockSignals(False)
        if date_format is not None:
            self.set_date_format(date_format)

    def set_date_format(self, pattern: Any) -> None:
        """Show a stored date format without emitting: its preset, or Custom with its text."""
        from app.core import date_format as _dates

        text = str(pattern or "").strip() or _dates.DEFAULT
        found = self.date_format.findData(text)
        self.date_format.blockSignals(True)
        self.date_custom.blockSignals(True)
        try:
            if found >= 0:
                self.date_format.setCurrentIndex(found)
                self.date_custom.hide()
                self.date_note.hide()
            else:
                self.date_format.setCurrentIndex(self.date_format.findData(CUSTOM_DATE))
                self.date_custom.setText(text)
                self.date_custom.show()
                self._date_say(text)
        finally:
            self.date_format.blockSignals(False)
            self.date_custom.blockSignals(False)

    def _date_picked(self) -> None:
        choice = str(self.date_format.currentData() or "")
        if choice != CUSTOM_DATE:
            self.date_custom.hide()
            self.date_note.hide()
            self.date_format_changed.emit(choice)
            return
        self.date_custom.show()
        self.date_custom.setFocus()
        self._date_typed(self.date_custom.text())

    def _date_typed(self, text: str) -> None:
        """Check the custom format as it is typed; send it only when it can be used."""
        if self._date_say(text):
            self.date_format_changed.emit(str(text).strip())

    def _date_say(self, text: str) -> bool:
        """Show what the format gives, or what is wrong with it. True when it is valid."""
        from app.core import date_format as _dates

        problem = _dates.validate(text)
        self.date_note.setText(problem or f"Shows as {_dates.example(str(text).strip())}")
        self.date_note.setProperty("problem", problem is not None)
        self.date_note.show()
        return problem is None

    def set_theme(self, preference: Any) -> None:
        """Show a stored theme choice without emitting."""
        self.theme.blockSignals(True)
        try:
            found = self.theme.findData(str(preference or "system"))
            self.theme.setCurrentIndex(found if found >= 0 else 0)
        finally:
            self.theme.blockSignals(False)
