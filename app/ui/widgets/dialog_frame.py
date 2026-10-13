r"""The one frame every dialog is built in: a title, the body, a button row with the primary on the right.

Layer: L5

**Why this exists** (owner, 2026-10-13, UI phase B): *"every screen ... look and feel like using
different apps not one app"*. Each dialog laid itself out: margins of 9, 11, 12 or 16, rows 6 or
8 apart, and its OK and Cancel wherever the platform's `QDialogButtonBox` put them - on Windows
OK first, so the button that goes ahead sat left of Cancel in one dialog and right of Close in the
next, where a hand-built row put it.

**The frame, in three calls.**

* `frame(dialog, title)` - the window title and the dialog's layout, `MARGINS` round and
  `SPACING` between rows, on the same 8 px rhythm as a page (`page_frame.SPACING`).
* `buttons(dialog, primary)` - the row at the bottom: a `QDialogButtonBox` (so `button()`,
  `buttonRole()` and every caller and test that finds one keeps working), its OK named `primary`
  when that is given, `accepted`/`rejected` wired to the dialog's `accept`/`reject`.
* The **order** is the sheet's (`theme.py`, `button-layout` on `QDialogButtonBox` and
  `QMessageBox`): the other buttons first, then **the primary last, on the right**, in every box -
  this frame's, the contacts tools', Qt's own `QInputDialog` and every `confirm`.

**The keys.** Enter presses the default; Esc is Cancel (Qt's `QDialog.reject`, which a box's
Cancel also calls). The default is the primary, except where the primary destroys something
(`danger=True`): then Cancel is the default and Enter cannot delete - `confirm.py`'s rule.

`confirm.confirm` stays the one *question* asked before something cannot be taken back; this is
the frame for a dialog with a body of its own.
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtWidgets import QDialog, QDialogButtonBox, QVBoxLayout

__all__ = ["MARGINS", "SPACING", "frame", "buttons", "BUTTON_LAYOUT"]

#: left, top, right, bottom.
MARGINS = (16, 16, 16, 16)
#: Between rows.
SPACING = 8
#: Qt's `QDialogButtonBox::GnomeLayout`: help and actions on the left, then Cancel, then the
#: primary on the right (No before Yes). Set for every box by the sheet, `theme.py`.
BUTTON_LAYOUT = 3


def frame(dialog: QDialog, title: str = "", *, spacing: int = SPACING) -> QVBoxLayout:
    """Give `dialog` its title and its layout, framed. Returns the layout to fill.

    An empty `title` leaves the window title the dialog already set.
    """
    if title:
        dialog.setWindowTitle(title)
    layout = dialog.layout()
    if not isinstance(layout, QVBoxLayout):
        layout = QVBoxLayout(dialog)
    layout.setObjectName("dialogFrame")
    layout.setContentsMargins(*MARGINS)
    layout.setSpacing(spacing)
    dialog.setProperty("dialogFrame", True)
    return layout


def buttons(dialog: QDialog, primary: Optional[str] = None, *,
            standard: Optional[QDialogButtonBox.StandardButton] = None,
            danger: bool = False) -> QDialogButtonBox:
    """The dialog's button row. Add it last: `layout.addWidget(buttons(...))`.

    `standard` defaults to OK and Cancel; `primary` renames OK (or Yes) to say what it does.
    With `danger`, Cancel is the default, so Enter cannot go ahead.
    """
    sb = QDialogButtonBox.StandardButton
    box = QDialogButtonBox(standard if standard is not None else (sb.Ok | sb.Cancel), dialog)
    box.setObjectName("dialogButtons")
    go = box.button(sb.Ok) or box.button(sb.Yes)
    if go is not None and primary:
        go.setText(primary)
    box.accepted.connect(dialog.accept)
    box.rejected.connect(dialog.reject)
    cancel = box.button(sb.Cancel) or box.button(sb.No) or box.button(sb.Close)
    if danger and go is not None and cancel is not None:
        go.setAutoDefault(False)
        go.setDefault(False)
        cancel.setDefault(True)
    elif go is not None:
        go.setDefault(True)
    return box
