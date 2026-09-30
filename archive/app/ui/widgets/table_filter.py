r"""Narrowing a table that is already on screen.

Layer: L5

**Not a search.** This hides rows that do not match, over data already loaded,
and it exists because every list in this application should behave the same way
when somebody starts typing at it. Files and Mail filter; a list that could not
be narrowed was the odd one out.

The distinction is worth keeping sharp. A *search* asks the index a question and
gets rows back; a *filter* removes rows already fetched. They look alike and
behave differently: a filter cannot find what is not on screen, so it must never
be the only way to look for something - which is why the tabs that filter also
hand their selection to the search box.

**No debounce here, deliberately.** A search debounces because each keystroke
would otherwise cost a query; this walks rows already in memory, and on the
tens-of-rows lists it is used for a timer would only add lag to something that
is already instant. If it is ever pointed at thousands of rows, that changes -
and the comment is here so the change is a decision rather than a discovery.
"""

from __future__ import annotations

from typing import Any, Optional, Sequence

from PyQt6.QtWidgets import QLineEdit, QWidget

__all__ = ["build_filter", "filter_rows"]


def build_filter(
    placeholder: str,
    *,
    accessible_name: str,
    tooltip: str = "",
    on_change: Any = None,
    parent: Optional[QWidget] = None,
) -> QLineEdit:
    """A filter box that looks and behaves like every other one here.

    `accessible_name` is required rather than optional: a text box has no text
    of its own, so without one a screen reader announces nothing at all - and
    `test_accessible_names.py` fails the build for it.
    """
    box = QLineEdit(parent)
    box.setPlaceholderText(placeholder)
    box.setAccessibleName(accessible_name)
    box.setClearButtonEnabled(True)
    if tooltip:
        box.setToolTip(tooltip)
    if on_change is not None:
        box.textChanged.connect(lambda _text: on_change())
    return box


def filter_rows(table: Any, needle: str, columns: Sequence[int]) -> int:
    """Hide rows not matching `needle`. Returns how many are still shown.

    Matches the named columns **joined together**, so somebody typing "sub" is
    not asked whether they meant a kind or a folder. Making them choose which
    column they meant is precision that only sounds helpful.
    """
    wanted = (needle or "").strip().lower()
    shown = 0
    for row in range(table.rowCount()):
        haystack = " ".join(
            (table.item(row, column).text() or "").lower()
            for column in columns
            if table.item(row, column) is not None
        )
        hide = bool(wanted) and wanted not in haystack
        table.setRowHidden(row, hide)
        shown += 0 if hide else 1
    return shown
