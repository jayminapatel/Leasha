r"""One way to lay out a settings row, for every box on Settings (UI phase B, 2026-10-13).

Layer: L6 (UI)

**Why one helper.** The owner: "every screen and settings look and feel like using different
apps". Each box laid its rows out its own way - a switch in the field column here and across the
row there, a help sentence spanning the form in Accounts and tucked under the control on Search,
an empty-string label as the way to say "under the row above". The same kind of row looked
different from box to box, which is exactly the feel the owner named.

So a row is built here, in one of three shapes, and nowhere else:

* `add_row(form, control, "Label")` - a label and its control, side by side;
* `add_row(form, switch)` - a control that carries its own words (a tick box, a button), across
  the whole row;
* `add_under(form, widget)` - something that belongs to the row above (a Download row, a note, a
  second button), under that row's control.

`hint=` puts the plain sentence that says what the row does under it, across the row, in the one hint style
(`settingsHint`), or as the label it is given - Accounts keeps its own `accountsHint` labels.

**The restore-default comes with the name, not with a button per row.** A control named after
its registry key (`setObjectName("RERANK_TOP_N")`) gets the right-click "Reset ... to its default"
from `defaults.attach_resets`, which walks the page for those names - so a row built here needs
nothing more, and the boxes that have one keep their own Reset button where it always was. A
second, per-row button would have been new words on every row and a second way to do one thing.

**No words live here.** Every label and hint is passed in by the box that owns it, so the words
stay where `test_pages_reorg.py` reads them, unchanged.
"""

from __future__ import annotations

from typing import Any, Optional, Union

from PySide6.QtWidgets import QFormLayout, QLabel, QWidget

__all__ = ["add_row", "add_under", "hint_label", "HINT_NAME", "ROW_PROPERTY"]

#: The object name the theme styles a hint by (`theme.py`, `#settingsHint`).
HINT_NAME = "settingsHint"

#: Set on every control a row was built for, so "Search all settings" and a test can tell a
#: settings row from any other widget on the page.
ROW_PROPERTY = "settingsRow"


def hint_label(text: str, parent: Optional[QWidget] = None, *, name: str = HINT_NAME) -> QLabel:
    """The plain sentence under a row: wrapped, and styled by the theme as a hint."""
    label = QLabel(text, parent)
    label.setObjectName(name)
    label.setWordWrap(True)
    return label


def add_row(form: QFormLayout, control: QWidget, label: Optional[str] = None, *,
            hint: Union[str, QLabel, None] = None) -> QWidget:
    """One settings row in `form`; returns `control`.

    `label` None is a control that names itself, laid across the row. `hint` is a sentence (or a
    ready-made label) that goes under the row, across its whole width: tried in the field column
    first, the Accounts hints wrapped onto so many more lines that its buttons were squeezed
    below their height (`test_accounts_page`, 2026-10-13).
    """
    if label is None:
        form.addRow(control)
    else:
        form.addRow(label, control)
    control.setProperty(ROW_PROPERTY, True)
    if hint is not None:
        form.addRow(hint if isinstance(hint, QLabel) else hint_label(hint))
    return control


def add_under(form: QFormLayout, widget: Any) -> Any:
    """`widget` under the field of the row above - a Download row, a note, more buttons.

    Never a row of its own with a label: what sits here belongs to the setting above it.
    """
    form.addRow("", widget)
    return widget
