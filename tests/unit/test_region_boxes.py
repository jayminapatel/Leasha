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


@pytest.mark.slow
def test_the_country_lists_import_no_phone_library() -> None:
    """2026-10-14, the lag monitor: the window held 2 s importing `phonenumbers/geodata` while a
    contact was shown (23:46; 10:45 in the contact editor) - `regions()` worked the list out of
    `phonenumbers` (336 modules, about 117 MB) on the window's thread. It reads a table now.
    A fresh interpreter, so what an earlier test imported does not count."""
    import subprocess
    import sys
    from pathlib import Path

    probe = ("import sys\n"
             "from app.ui.presenter.regions import code_of, country_names, regions\n"
             "assert len(regions()) > 200 and 'United Kingdom' in country_names() and code_of('+44') == 'GB'\n"
             "print(sorted(m for m in sys.modules if m.split('.')[0] == 'phonenumbers'))\n")
    done = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, timeout=120,
                          cwd=str(Path(__file__).resolve().parents[2]))
    assert done.returncode == 0, done.stderr[-2000:]
    assert done.stdout.strip() == "[]", f"the country list imported {done.stdout.strip()[:300]}"


@pytest.mark.slow
def test_regions_table_matches_phonenumbers() -> None:
    """The table is `phonenumbers`' own answer, written out. Red after an upgrade changed a name or a
    code: write `app/ui/presenter/region_table.py` again from `regions._derive()`."""
    from app.ui.presenter.regions import _derive

    assert regions() == _derive()
