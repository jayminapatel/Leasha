"""Drop-downs that can also be typed: the phone region (flag, name, dialling code) and an address's country."""

from __future__ import annotations

import pytest

from app.ui.presenter.regions import code_of, flag, regions, row_text
from app.ui.widgets.region_box import CountryBox, RegionBox


@pytest.mark.slow
def test_a_row_shows_the_flag_the_name_and_the_dialling_code() -> None:
    uk = next(r for r in regions() if r.code == "GB")
    assert uk.dial == "+44" and uk.name == "United Kingdom"
    assert row_text(uk).endswith("United Kingdom  +44") and flag("GB") in row_text(uk)


def test_a_country_is_found_by_code_name_dialling_code_or_its_row() -> None:
    assert code_of("gb") == "GB" and code_of("United States") == "US" and code_of("+44") == "GB"
    assert code_of("1") == "US" and code_of(row_text(next(r for r in regions() if r.code == "FR"))) == "FR"
    assert code_of("nowhere") is None


def test_the_region_box_reads_and_writes_codes(qtbot) -> None:
    box = RegionBox()
    qtbot.addWidget(box)
    seen = []
    box.editingFinished.connect(lambda: seen.append(box.text()))
    box.setText("us")
    assert box.text() == "US"
    box.setEditText("france")
    box.lineEdit().editingFinished.emit()
    assert box.text() == "FR" and seen[-1] == "FR"
    box.setEditText("gibberish")
    box.lineEdit().editingFinished.emit()
    assert box.text() == "FR"                          # not a country: what was there stays


def test_the_country_box_keeps_any_word_typed(qtbot) -> None:
    box = CountryBox()
    qtbot.addWidget(box)
    changed = []
    box.textChanged.connect(changed.append)
    box.setText("Narnia")
    assert box.text() == "Narnia" and changed[-1] == "Narnia"
    assert box.findText("United Kingdom") >= 0
