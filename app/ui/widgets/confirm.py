r"""The one question asked before something cannot be taken back.

Layer: L5

**Cancel is the default, and the other button names what it does.** Qt's
`QMessageBox.question` gives Yes and No and leaves the default to the
platform, and on Windows that is Yes - so Enter, pressed to get a dialog out
of the way, deleted a model from the disk. "Yes" also asks somebody to re-read
the question to find out what they are agreeing to; "Delete" says it on the
button they are about to press.

The Reset index confirmation (`index_controller._reset_index`) has always been
built this way. This is that shape, once, so the next destructive action does
not have to remember it. `accounts/parts.confirm` is the same function under
the name the Accounts page's tests replace.

**A question waits for its answer; an error does not.** `confirm` uses `exec()`
because the action it guards cannot go on until somebody decides, and it is only
ever asked from a click. The window's error box (`error_box.show_error`) is the
other kind: it is opened with `open()` and never waited for, because most errors
arrive from a worker or a timer, where a nested event loop is a hang.
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtWidgets import QMessageBox, QWidget

from app.ui.widgets.buttons import style_button

__all__ = ["confirm", "CANCEL"]

CANCEL = "Cancel"


def confirm(parent: Optional[QWidget], title: str, text: str, verb: str, *,
            informative: str = "", danger: bool = True) -> bool:
    """Ask, with `verb` on the button that goes ahead and Cancel as the default.

    True only when the `verb` button was pressed: Escape, the close box and
    Enter all land on Cancel. `informative` is the smaller second paragraph -
    what is and is not at risk - for a question whose first line is the
    decision itself.
    """
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Icon.Warning if danger else QMessageBox.Icon.Question)
    box.setWindowTitle(title)
    box.setText(text)
    if informative:
        box.setInformativeText(informative)
    go = box.addButton(verb, QMessageBox.ButtonRole.AcceptRole)
    cancel = box.addButton(CANCEL, QMessageBox.ButtonRole.RejectRole)
    style_button(go, "trash-2" if danger else "check", "danger" if danger else "primary")
    style_button(cancel, "x", "secondary")
    box.setDefaultButton(cancel)
    box.setEscapeButton(cancel)
    box.exec()
    return box.clickedButton() is go
