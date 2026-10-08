r"""Number fields: typed, never arrowed, with one button that puts the default back.

Layer: L5

**The owner, 2026-09-29:** *"in the ui remove the up down controls for numbers
instead have a default button with just logo, they will be entered this is
every where"*. So every `QAbstractSpinBox` in the application - spin boxes, the
daily time - loses Qt's up/down arrows and gains, **in the space the arrows
used to take**, one small icon-only button: back to the default.

**Why inside the field and not beside it.** "Instead" is the owner's word: the
button replaces the arrows, so it sits where they sat. It also means no layout
anywhere changes shape. A spin box keeps its place in its form, its label's
buddy, its row's visibility and its enabled state, and `labelForField`, the
settings filter and `mark_restart_needed` all still find the control they
found before. Wrapping fifty spin boxes in fifty new row widgets would have
changed every one of those relationships at once.

**What stays exactly as it was.** Range, suffix, special value text ("No
limit", "Automatic"), keyboard tracking, validation and the wheel rule in
`no_scroll.py`: a spin box without arrows still takes the Up and Down keys and,
once clicked, the wheel. Only the two arrow buttons go.

**What the button does.** It sets the field to its default through `setValue`
(or `setTime`), which emits the same `valueChanged` a typed number emits once
the person presses Enter or leaves the field, so every save, debounce and
warning already wired to the field runs unchanged. It is greyed while the field
already holds the default, so it also answers "is this the default?" at a
glance.

**Which default.** In order: one passed in by the caller; the settings
registry's default when the field's object name is a registered key (the
convention `test_settings_reachable.py` enforces, so almost every field on the
two settings pages has one); otherwise the value the field held when it was
fitted, which is the value it was built with.

**Applied app-wide, in one call.** `fit_all(root)` runs beside
`protect_all(root)` in `MainWindow`, for the same reason that guard does:
per page, the one somebody forgets is the one that matters. A dialog or menu
built later calls it on itself. `tests/unit/test_number_fields.py` walks every
page and dialog it can build and fails on any field with arrows or without
the button.
"""

from __future__ import annotations

from typing import Any, Optional

from PySide6.QtCore import QEvent, QObject, QSize, Qt, QTime
from PySide6.QtWidgets import (
    QAbstractSpinBox, QDateTimeEdit, QDoubleSpinBox, QLineEdit, QSpinBox, QToolButton,
    QWidget,
)

__all__ = ["fit", "fit_all", "default_of", "reset_button", "RESET_NAME", "RESET_ICON"]

#: The reset button's object name - what the stylesheet and the tests find it by.
RESET_NAME = "numberReset"

#: The same "put it back" glyph every "Restore defaults" button already carries
#: (`buttons.BUTTONS`), so the one picture means the one thing everywhere.
RESET_ICON = "rotate-ccw"

#: The icon's size inside the button, in pixels.
_ICON_PX = 14

#: The Qt property that marks a field as fitted, so a second pass skips it.
_FITTED = "numberFieldFitted"


def _registry_default(field: QAbstractSpinBox) -> Any:
    """The registry's default for this field's key, or None."""
    name = field.objectName()
    if not name:
        return None
    try:
        from app.core.settings_registry import by_key
    # A registry that cannot import (a bare test) means no registry default.
    except Exception:                              # noqa: BLE001 - a lookup, never a failure
        return None
    setting = by_key(name)
    if setting is None:
        return None
    return setting.default


def _as_value(field: QAbstractSpinBox, raw: Any) -> Any:
    """`raw` as the type the field holds, or None when it cannot be."""
    if raw is None:
        return None
    try:
        if isinstance(field, QDateTimeEdit):
            if isinstance(raw, QTime):
                return raw
            parsed = QTime.fromString(str(raw).strip(), "HH:mm")
            return parsed if parsed.isValid() else None
        if isinstance(field, QDoubleSpinBox):
            return float(raw)
        if isinstance(field, QSpinBox):
            return int(raw)
    except (TypeError, ValueError):
        return None
    return None


def _current(field: QAbstractSpinBox) -> Any:
    """The field's value in its own type (a `QTime` for a time field)."""
    if isinstance(field, QDateTimeEdit):
        return field.time()
    if isinstance(field, (QSpinBox, QDoubleSpinBox)):
        return field.value()
    return None


def _clamped(field: QAbstractSpinBox, value: Any) -> Any:
    """What the field would actually hold if set to `value` - its range wins."""
    if isinstance(field, (QSpinBox, QDoubleSpinBox)) and value is not None:
        return min(max(value, field.minimum()), field.maximum())
    return value


def default_of(field: QAbstractSpinBox) -> Any:
    """The default this field's button restores, as the field's own type."""
    return field.property("numberDefault")


def _words(field: QAbstractSpinBox, value: Any) -> str:
    """The default as the field would show it: "30 results", "No limit", "02:00"."""
    if isinstance(field, QDateTimeEdit):
        return value.toString(field.displayFormat() or "HH:mm")
    if isinstance(field, (QSpinBox, QDoubleSpinBox)):
        special = field.specialValueText()
        if special and value == field.minimum():
            return special
        text = field.textFromValue(value)
        return f"{field.prefix()}{text}{field.suffix()}".strip()
    return str(value)


class _ResetButton(QToolButton):
    """The icon-only button that sits where the arrows were."""

    def __init__(self, field: QAbstractSpinBox) -> None:
        super().__init__(field)
        self._field = field
        self.setObjectName(RESET_NAME)
        self.setAutoRaise(True)
        self.setCursor(Qt.CursorShape.ArrowCursor)
        # Reachable by Tab, so the keyboard can reset too; never takes focus
        # from a click, so pressing it does not end an edit in the field.
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.setIconSize(QSize(_ICON_PX, _ICON_PX))
        self.clicked.connect(lambda _c=False: self.reset())
        self._paint()

    def reset(self) -> None:
        """Put the default back through the field's own setter, so every save fires as usual."""
        field, value = self._field, default_of(self._field)
        if value is None:
            return
        if isinstance(field, QDateTimeEdit):
            field.setTime(value)
        else:
            field.setValue(value)
        self.refresh()

    def refresh(self) -> None:
        """Grey when the field already holds its default; say which default."""
        field, value = self._field, default_of(self._field)
        if value is None:
            self.setEnabled(False)
            return
        words = _words(field, _clamped(field, value))
        label = f"Back to the default ({words})"
        # Only what changed: this also runs on every repaint of the field.
        if self.toolTip() != label:
            self.setToolTip(label)
            self.setAccessibleName(label)
        wanted = _current(field) != _clamped(field, value)
        if self.isEnabledTo(field) != wanted:
            self.setEnabled(wanted)
        if self.isHidden() != field.isReadOnly():
            self.setHidden(field.isReadOnly())

    def _paint(self) -> None:
        try:
            from app.ui.theme import theme_colours
            from app.ui.widgets.icons import icon

            colours = theme_colours()
            self.setIcon(icon(RESET_ICON, colours.get("text_dim", "#888888")))
        except Exception:                          # noqa: BLE001 - a picture, never a failure
            # No icon is a visible bug, not a broken field: the tooltip and
            # accessible name still say what the button does.
            pass

    def changeEvent(self, event: Any) -> None:     # noqa: N802 - Qt's naming
        # A theme change restyles every widget; the icon is a picture and does
        # not follow the stylesheet, so it is drawn again in the new colour.
        if event.type() in (QEvent.Type.StyleChange, QEvent.Type.PaletteChange):
            self._paint()
        super().changeEvent(event)


class _Placer(QObject):
    """Keeps each button at its field's right-hand edge as the field resizes."""

    def eventFilter(self, watched: Any, event: Any) -> bool:   # noqa: N802 - Qt's naming
        if event.type() == QEvent.Type.Paint and isinstance(watched, QLineEdit):
            # **A load changes the value with every signal blocked** - the
            # field's by the page, and the text box's by Qt itself - so no
            # signal says the number moved. The text box repaints whenever it
            # does, which is also the only time the button can be seen.
            button = reset_button(watched.parentWidget())
            if button is not None:
                button.refresh()
            return False
        if event.type() in (QEvent.Type.Resize, QEvent.Type.Show):
            button = reset_button(watched)
            if button is not None:
                _place(watched, button)
                if event.type() == QEvent.Type.Show:
                    button.refresh()
        return False


#: Shared and kept for the life of the process, like `no_scroll._GUARD`: an
#: event filter that is garbage collected stops filtering, silently.
_PLACER = _Placer()


def _place(field: QAbstractSpinBox, button: QToolButton) -> None:
    """Put the button inside the field's right edge, a little smaller than the field."""
    side = max(12, field.height() - 6)
    button.setFixedSize(side, side)
    button.move(field.width() - side - 3, (field.height() - side) // 2)
    button.raise_()


def reset_button(field: Any) -> Optional[QToolButton]:
    """The field's reset button, or None if it has not been fitted."""
    if not isinstance(field, QAbstractSpinBox):
        return None
    return field.findChild(QToolButton, RESET_NAME,
                           Qt.FindChildOption.FindDirectChildrenOnly)


def fit(field: QAbstractSpinBox, default: Any = None) -> QAbstractSpinBox:
    """No arrows, and a back-to-default button. Returns the field. Safe to call twice.

    `default`, when given, wins over the registry and over the field's current
    value - for a field whose built value is a saved one rather than a default.
    """
    if default is not None or field.property("numberDefault") is None:
        resolved = _as_value(field, default)
        if resolved is None:
            resolved = _as_value(field, _registry_default(field))
        if resolved is None:
            resolved = _current(field)
        field.setProperty("numberDefault", resolved)
    if field.property(_FITTED):
        button = reset_button(field)
        if button is not None:
            button.refresh()
        return field

    field.setProperty(_FITTED, True)
    # Room for the button, taken back from the arrows it replaces: measured
    # after the arrows are gone, so the field is no wider than it was.
    field.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
    button = _ResetButton(field)
    room = max(12, field.sizeHint().height() - 6) + 4
    field.setMinimumWidth(field.sizeHint().width() + room)
    edit = field.findChild(QLineEdit)
    if edit is not None:
        edit.setTextMargins(0, 0, room, 0)
        edit.textChanged.connect(lambda _t, b=button: b.refresh())
        edit.installEventFilter(_PLACER)
    if isinstance(field, QDateTimeEdit):
        field.timeChanged.connect(lambda _t, b=button: b.refresh())
    elif isinstance(field, (QSpinBox, QDoubleSpinBox)):
        field.valueChanged.connect(lambda _v, b=button: b.refresh())
    field.installEventFilter(_PLACER)
    _place(field, button)
    # Shown explicitly: a child made after its parent is on screen otherwise
    # stays hidden until something shows it.
    button.setVisible(not field.isReadOnly())
    button.refresh()
    return field


def fit_all(root: QWidget) -> int:
    """Fit every number field under `root`. Returns how many were new."""
    fitted = 0
    for field in root.findChildren(QAbstractSpinBox):
        if not field.property(_FITTED):
            fitted += 1
        fit(field)
    return fitted
