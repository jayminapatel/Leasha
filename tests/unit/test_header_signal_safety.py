r"""No Python may run from `sectionResized` while the view is being applied.

Leasha died twice on 2026-08-27 with a Windows access violation. The crash
dump - readable at last, once the reporter was fixed - named:

    _fill -> show_rows -> apply_prefs -> apply_to_table -> resized

with `resized` at an unknown line, because the frame faulted on *entry*,
before one bytecode ran.

`remember_widths` already carries a long note about the identical fault during
window construction, and its conclusion is the thing these tests defend:

    the fault is in *invoking a Python slot from `sectionResized` while Qt is
    mid-layout* - Qt calling into PyQt's glue during a layout pass the header
    has not finished.

That note also records that guarding the *body* of the slot changed nothing,
because a Python bool cannot fault. So an early return is not a fix here and a
test that only checked the flag would pass over the bug. What has to be true
is stronger: **the slot is never called at all.**
"""

from __future__ import annotations

import pytest

pytest.importorskip("PyQt6.QtWidgets")

from PyQt6.QtCore import QCoreApplication                       # noqa: E402
from PyQt6.QtWidgets import QTableWidgetItem                    # noqa: E402

from app.ui.view_options import (                               # noqa: E402
    APPLYING, ViewPreferences, apply_to_table, remember_widths,
)
from app.ui.widgets.result_table import ResultTable             # noqa: E402

COLUMNS = [("name", "Name"), ("size", "Size"), ("when", "Modified")]


class _Button:
    """Stands in for the view button, and records what it was told."""

    def __init__(self) -> None:
        self.saved: list = []

    def remember_width(self, key, width) -> None:
        self.saved.append((key, width))


def _table(_qt_application) -> tuple:
    """A populated table wired exactly as the four real views wire theirs."""
    del _qt_application
    table = ResultTable([h for _k, h in COLUMNS], ranked=True,
                        aligns=["left", "right", "left"])
    button = _Button()
    remember_widths(table, button, COLUMNS)
    # The connection is deferred by one turn of the event loop on purpose -
    # see the note in `remember_widths`. Without this the test would measure a
    # table nothing is listening to, and would pass for the wrong reason.
    QCoreApplication.processEvents()

    table.setRowCount(3)
    for row in range(3):
        for column in range(3):
            table.setItem(row, column,
                          QTableWidgetItem(f"cell {row}{column} wwwwwwwwwwww"))
    table.set_row_objects([object(), object(), object()])
    return table, button


class TestTheSlotIsNotCalledWhileTheViewIsApplied:

    def test_no_section_resize_reaches_python(self, _qt_application) -> None:
        """The regression. It was four invocations before the fix.

        Counted on a spy attached to the same signal, so this measures what
        Qt actually emitted rather than what the code intended.
        """
        table, _button = _table(_qt_application)
        seen: list = []
        table.horizontalHeader().sectionResized.connect(
            lambda index, old, new: seen.append((index, old, new)))

        apply_to_table(table, ViewPreferences(), columns=COLUMNS,
                       available=[key for key, _h in COLUMNS])

        assert seen == [], (
            f"{len(seen)} section resizes reached a Python slot from inside "
            "apply_to_table - this is the access violation of 2026-08-27")

    def test_a_resize_afterwards_still_reaches_python(self, _qt_application) -> None:
        """The other half, without which the fix is just a broken feature.

        Blocking too much would silently disable remembering column widths,
        and nothing else in the suite would notice.
        """
        table, _button = _table(_qt_application)
        apply_to_table(table, ViewPreferences(), columns=COLUMNS,
                       available=[key for key, _h in COLUMNS])

        seen: list = []
        table.horizontalHeader().sectionResized.connect(
            lambda index, old, new: seen.append((index, old, new)))
        table.horizontalHeader().resizeSection(0, 321)

        assert seen, "the header stayed deaf after apply_to_table returned"


class TestTheGuardsAreLeftAsTheyWereFound:

    def test_the_header_is_unblocked_afterwards(self, _qt_application) -> None:
        table, _button = _table(_qt_application)
        apply_to_table(table, ViewPreferences(), columns=COLUMNS,
                       available=[key for key, _h in COLUMNS])
        assert not table.horizontalHeader().signalsBlocked()

    def test_the_applying_flag_is_cleared_afterwards(self, _qt_application) -> None:
        table, _button = _table(_qt_application)
        apply_to_table(table, ViewPreferences(), columns=COLUMNS,
                       available=[key for key, _h in COLUMNS])
        assert table.property(APPLYING) is False

    def test_a_header_already_blocked_is_left_blocked(self, _qt_application) -> None:
        """Restore what was there, not what is convenient.

        A caller that blocked the header for its own reasons must not find it
        unblocked because this ran in the middle.
        """
        table, _button = _table(_qt_application)
        table.horizontalHeader().blockSignals(True)
        try:
            apply_to_table(table, ViewPreferences(), columns=COLUMNS,
                           available=[key for key, _h in COLUMNS])
            assert table.horizontalHeader().signalsBlocked()
        finally:
            table.horizontalHeader().blockSignals(False)

    def test_the_nested_width_pass_does_not_drop_the_flag(self, _qt_application) -> None:
        r"""`_apply_widths` sets the same flag and used to force it False.

        It runs *inside* the span `apply_to_table` now guards, so clearing it
        on the way out would leave the rest of that span unguarded while the
        caller still believed the guard was up.
        """
        from app.ui.view_options import _apply_widths

        table, _button = _table(_qt_application)
        table.setProperty(APPLYING, True)
        _apply_widths(table, ViewPreferences(), [k for k, _h in COLUMNS],
                      [k for k, _h in COLUMNS])
        assert table.property(APPLYING) is True, (
            "the nested pass cleared a flag it did not set")


class TestTheViewIsStillApplied:
    """A fix that stopped the function working would pass everything above."""

    def test_columns_are_still_hidden_and_shown(self, _qt_application) -> None:
        table, _button = _table(_qt_application)
        prefs = ViewPreferences(columns=("name", "when"))
        shown = apply_to_table(table, prefs, columns=COLUMNS,
                               available=[key for key, _h in COLUMNS])

        assert "name" in shown and "when" in shown
        assert not table.isColumnHidden(0)
        assert table.isColumnHidden(1), "the unchosen column is still visible"

    def test_the_hidden_rank_column_stays_hidden(self, _qt_application) -> None:
        """Ranked tables carry a hidden sort column; it is not a real one."""
        table, _button = _table(_qt_application)
        apply_to_table(table, ViewPreferences(), columns=COLUMNS,
                       available=[key for key, _h in COLUMNS])
        assert table.isColumnHidden(table.columnCount() - 1)
