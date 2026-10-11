"""A drop-down looks like one, and the default phone or email says so in words.

The owner (2026-10-12): "there is no drop down for the suggested default type of phone ... and the default
signal is not visually clear enough". The stylesheet had taken the native arrow away and drawn none.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.contacts.card import Field
from app.ui import theme


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_every_combo_box_has_an_arrow_that_exists_on_disk(scheme):
    sheet = theme.stylesheet(scheme)
    match = re.search(r"QComboBox::down-arrow \{ image: url\(([^)]+)\)", sheet)
    assert match, "the stylesheet draws no drop-down arrow"
    assert Path(match.group(1)).is_file()


def test_the_default_row_says_default_and_the_others_offer_to_be(qtbot):
    from app.ui.contacts.fields import FieldRow

    first = FieldRow("phone", Field("phone", "+44 7911 123456", "mobile", primary=True))
    second = FieldRow("phone", Field("phone", "+44 161 496 0000", "work"))
    for row in (first, second):
        qtbot.addWidget(row)
    assert first.main.text() == "Default" and second.main.text() == "Set default"
    second.main.setChecked(True)
    assert second.main.text() == "Default"
    assert first.label.isEditable() and first.label.count() >= 4      # suggested types, and your own word
