"""The file-types editor's two reported faults, and the settings it persists.

Layer: L5

Both bugs here were found by using the window, not by a test - which is the
pattern for every UI fault in this project. They are pinned now:

1. *"scrolling moves from the main page into the list without clicking on it"* -
   a scrollable table inside a scrollable page swallows the wheel, so the page
   stops dead and there is no way to scroll past the table at all.
2. *"when you double click the file it does not open the edit box"* - double-click
   did nothing whatsoever, and the size cap could only be changed by finding a
   TOML file on disk.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

pytest.importorskip("PyQt6.QtWidgets", exc_type=ImportError)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QEvent, QPoint, QPointF, Qt  # noqa: E402
from PyQt6.QtGui import QWheelEvent  # noqa: E402
from PyQt6.QtWidgets import QApplication, QTableWidget  # noqa: E402

from app.ui.widgets.file_types import EditFileTypeDialog, FileTypesEditor  # noqa: E402
from app.ui.widgets.no_scroll import protect_view  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    yield QApplication.instance() or QApplication([])


@pytest.fixture()
def editor(qapp, tmp_path, monkeypatch):
    """A real editor over a throwaway data path, so saving cannot touch the
    machine's own overrides."""
    class Settings:
        data_path = tmp_path

    return FileTypesEditor(Settings())


def _wheel(widget) -> QWheelEvent:
    """A scroll-down wheel event over `widget`.

    `QPointF` for the two positions, not `QPoint`: PyQt6 has no overload taking
    integer points and the TypeError names only "argument 1", which is a slow
    way to discover a two-character fix.
    """
    return QWheelEvent(
        QPointF(5.0, 5.0), QPointF(widget.mapToGlobal(QPoint(5, 5))),
        QPoint(0, -120), QPoint(0, -120),
        Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
        Qt.ScrollPhase.NoScrollPhase, False,
    )


class _Focused:
    """Stands in for a view that has focus.

    Real focus needs a shown, activated window, which the offscreen platform
    will not reliably grant - and the thing under test is the guard's *decision*,
    not Qt's focus machinery. Faking the one input it reads keeps the test about
    the rule.
    """

    def hasFocus(self) -> bool:                    # noqa: N802 - Qt's naming
        return True

    def parent(self):
        return None


# --- the scroll-steal bug --------------------------------------------------

def test_an_unfocused_table_lets_the_page_have_the_wheel(qapp):
    """The reported fault: the pointer crossing a table stopped the page."""
    table = QTableWidget(20, 2)
    protect_view(table)

    event = _wheel(table.viewport())
    delivered = QApplication.sendEvent(table.viewport(), event)

    assert delivered, "the filter must consume the event, not decline it"
    assert not event.isAccepted(), (
        "an ignored wheel event is what propagates to the scrolling page"
    )


def test_a_focused_view_keeps_its_own_wheel(qapp):
    """The other half. Somebody who clicked the table meant to scroll it, and
    taking the wheel away from them would be its own annoyance.

    The guard must **decline** the event - returning False leaves Qt's own
    scrolling to run exactly as it always did."""
    from app.ui.widgets.no_scroll import ViewWheelGuard

    table = QTableWidget(20, 2)
    event = _wheel(table.viewport())

    assert ViewWheelGuard().eventFilter(_Focused(), event) is False


def test_focus_on_the_view_counts_when_the_viewport_is_filtered(qapp):
    """The filter is installed on the viewport, whose own focus is always
    False - the view's is the one that means anything. Reading the wrong one
    would make the guard swallow every wheel event, including the deliberate
    ones, and the table would never scroll at all."""
    from app.ui.widgets.no_scroll import ViewWheelGuard

    class Viewport:
        def hasFocus(self):                        # noqa: N802 - Qt's naming
            return False

        def parent(self):
            return _Focused()

    table = QTableWidget(20, 2)
    event = _wheel(table.viewport())

    assert ViewWheelGuard().eventFilter(Viewport(), event) is False


def test_the_guard_only_reacts_to_wheel_events(qapp):
    from app.ui.widgets.no_scroll import ViewWheelGuard

    table = QTableWidget(1, 1)
    other = QEvent(QEvent.Type.MouseButtonPress)
    assert ViewWheelGuard().eventFilter(table.viewport(), other) is False


def test_focus_policy_stops_the_wheel_granting_focus(qapp):
    """With WheelFocus the view takes focus on the first notch and then
    legitimately eats every notch after it - the guard would appear to work
    once and then stop, which reads as flakiness rather than policy."""
    table = QTableWidget(1, 1)
    protect_view(table)
    assert table.focusPolicy() == Qt.FocusPolicy.StrongFocus


# --- the edit dialog -------------------------------------------------------

def test_the_dialog_round_trips_megabytes_to_bytes(qapp):
    dialog = EditFileTypeDialog(
        ".pdf", reader="pdf", readers=["pdf", "plaintext"],
        max_bytes=25 * (1 << 20), enabled=True,
    )
    assert dialog.limit.value() == 25

    dialog.limit.setValue(50)
    reader, max_bytes, enabled = dialog.value()

    assert (reader, max_bytes, enabled) == ("pdf", 50 * (1 << 20), True)


def test_raising_an_ocr_cap_says_what_it_costs(qapp):
    """OCR cost scales with pixels, so a cap raised without warning can make an
    index run twenty times longer with nothing announcing it."""
    dialog = EditFileTypeDialog(
        ".png", reader="ocr", readers=["ocr"],
        max_bytes=25 * (1 << 20), enabled=True,
    )
    assert dialog.warning.text() == ""

    dialog.limit.setValue(100)
    assert "OCR cost scales" in dialog.warning.text()
    assert "25MB is the shipped cap" in dialog.warning.text()


def test_a_converted_type_cannot_have_its_reader_changed(qapp):
    """A converter's reader is fixed by what the converter produces. Offering
    to change it would be offering a setting that cannot work."""
    dialog = EditFileTypeDialog(
        ".doc", reader="plaintext", readers=["plaintext", "pdf"],
        max_bytes=1 << 20, enabled=True, editable_reader=False,
    )
    assert not dialog.reader.isEnabled()
    assert "converted first" in dialog.reader.toolTip()


# --- select all / none -----------------------------------------------------

def test_select_none_then_all_switches_every_shown_type(editor):
    total = len(editor._boxes)
    assert total > 10, "the table should list everything the app reads"

    editor.set_all(False)
    assert not any(box.isChecked() for box in editor._boxes.values())

    changed = editor.set_all(True)
    assert changed > 0
    assert all(box.isChecked() for box in editor._boxes.values())


def test_select_all_respects_the_filter(editor):
    """The one that matters. Filtered to `ocr`, "select none" must switch off
    the image types and leave the eighty formats behind the filter alone -
    otherwise one decision silently becomes eighty."""
    editor.set_all(True)

    editor.filter.setText("ocr")
    shown = [
        editor.table.item(row, 1).text().replace(" *", "").strip()
        for row in range(editor.table.rowCount())
        if not editor.table.isRowHidden(row)
    ]
    assert shown, "the filter matched nothing, so this proves nothing"
    assert len(shown) < len(editor._boxes)

    editor.set_all(False)

    for extension, box in editor._boxes.items():
        if extension in shown:
            assert not box.isChecked(), f"{extension} was shown and should be off"
        else:
            assert box.isChecked(), f"{extension} was hidden and must be untouched"


def test_select_all_says_when_a_filter_narrowed_it(editor):
    """A count smaller than the table has to explain itself, or it reads as
    the button having half worked."""
    editor.filter.setText("ocr")
    editor.set_all(False)

    assert "current filter" in editor.status.text()


def test_select_all_enables_saving_only_when_something_changed(editor):
    editor.set_all(True)
    editor.save_button.setEnabled(False)

    editor.set_all(True)                     # already all on
    assert not editor.save_button.isEnabled(), "no change, nothing to save"

    editor.set_all(False)
    assert editor.save_button.isEnabled()


# Persistence is tested in test_formats.py, without Qt: what the dialog writes
# has to be verifiable on a headless machine, where these tests cannot run.
