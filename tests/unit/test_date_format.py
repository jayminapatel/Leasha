r"""The Files and Mail tabs write dates in one chosen format (`app.core.date_format`).

Layer: L0 and L5

2026-10-11, the owner: "all the dates in the files and mail tab should be of
the format yyyy-mm-dd hh:nn and make this configurable have drop down of most
common formats and have a option of a validated custom format too."
"""

from __future__ import annotations

import time

import pytest

from app.core import date_format as dates
from app.ui.presenter.mail import card_from_row, mail_card
from app.ui.presenter.rows import file_rows, mail_rows

#: A fixed local afternoon: 7 March 2026, 14:05:09.
MOMENT = int(time.mktime((2026, 3, 7, 14, 5, 9, 0, 0, -1)))


@pytest.fixture(autouse=True)
def _default_format():
    dates.set_date_format("")
    yield
    dates.set_date_format("")


def test_the_default_is_the_owners_format():
    assert dates.DEFAULT == "yyyy-mm-dd hh:nn"
    assert dates.format_seconds(MOMENT) == "2026-03-07 14:05"
    assert dates.PRESETS[0] == dates.DEFAULT


@pytest.mark.parametrize("pattern, shown", [
    ("dd/mm/yyyy hh:nn", "07/03/2026 14:05"),
    ("mm/dd/yyyy hh:nn", "03/07/2026 14:05"),
    ("dd.mm.yyyy hh:nn", "07.03.2026 14:05"),
    ("dd mmm yyyy hh:nn", "07 Mar 2026 14:05"),
    ("mmm dd, yyyy hh:nn am/pm", "Mar 07, 2026 02:05 PM"),
    ("yyyy-mm-dd", "2026-03-07"),
    ("dddd dd mmmm yy hh:nn:ss", "Saturday 07 March 26 14:05:09"),
])
def test_each_format_writes_the_moment(pattern, shown):
    assert dates.validate(pattern) is None
    assert dates.format_seconds(MOMENT, pattern) == shown


def test_every_preset_is_valid():
    for pattern in dates.PRESETS:
        assert dates.validate(pattern) is None, pattern


@pytest.mark.parametrize("pattern, says", [
    ("", "Type a format"),
    ("yyyy-mm-dd x", '"x" is not part of a date'),
    ("dd/mm/yyyy hh:mm", "Use nn for minutes"),
    ("mm/yyyy", "day of the month"),
    ("dd yyyy", "the month"),
    ("dd mm", "the year"),
    ("yyyy-mm-dd nn", "need the hour"),
    ("yyyy-mm-dd hh:ss", "Seconds need the minutes"),
    ("yyyy-mm-dd am/pm", "am/pm needs the hour"),
    ("yyyy-mm-dd dd", "once"),
    ("dd mm yyyy %H", '"%" is not part of a date'),
])
def test_a_custom_format_that_cannot_be_used_is_refused_with_a_reason(pattern, says):
    problem = dates.validate(pattern)
    assert problem and says in problem, problem


def test_an_invalid_stored_format_falls_back_to_the_default():
    assert dates.set_date_format("dd/mm/yyyy hh:mm") == dates.DEFAULT
    assert dates.set_date_format("dd.mm.yyyy") == "dd.mm.yyyy"


def test_no_date_is_a_blank_not_1970():
    assert dates.format_seconds(0) == ""
    assert dates.format_seconds(None) == ""
    assert dates.format_ns(0) == ""


def test_the_files_tab_shows_the_format_not_an_age():
    rows = file_rows([{"id": 1, "path": r"C:\Docs\report.pdf", "ext": "pdf",
                       "mtime_ns": MOMENT * 1_000_000_000, "size_bytes": 10}],
                     now=MOMENT + 3 * 86_400)
    assert rows[0].modified == "2026-03-07 14:05"
    dates.set_date_format("dd mmm yyyy hh:nn")
    rows = file_rows([{"id": 1, "path": r"C:\Docs\report.pdf", "ext": "pdf",
                       "mtime_ns": MOMENT * 1_000_000_000, "size_bytes": 10}])
    assert rows[0].modified == "07 Mar 2026 14:05"


def test_the_mail_tab_list_and_card_show_the_format():
    message = {"file_id": 7, "sender": "Dave <dave@acme.com>", "subject": "Boiler",
               "sent_at": MOMENT, "recipients": "[]"}
    (row,) = mail_rows([message], now=MOMENT + 86_400)
    assert row.sent == "2026-03-07 14:05"
    assert mail_card(message).date_words == "2026-03-07 14:05"
    dates.set_date_format("dd/mm/yyyy hh:nn")
    (row,) = mail_rows([message])
    assert row.sent == "07/03/2026 14:05"
    card = card_from_row(row)
    if card is not None:                     # the row carries its sent_at
        assert card.date_words in ("07/03/2026 14:05", "")


# ---------------------------------------------------------------------------
# The control, in Settings > Window
# ---------------------------------------------------------------------------

def _box():
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])
    from app.ui.widgets.window_box import CUSTOM_DATE, WindowBox

    box, sent = WindowBox(), []
    box.date_format_changed.connect(sent.append)
    return box, sent, CUSTOM_DATE


def test_the_drop_down_offers_the_common_formats_and_custom():
    box, _sent, custom = _box()
    data = [box.date_format.itemData(i) for i in range(box.date_format.count())]
    assert data == [*dates.PRESETS, custom]
    assert box.date_format.currentData() == dates.DEFAULT
    assert "2026-03-07 14:05" in box.date_format.itemText(0)


def test_picking_a_preset_sends_it():
    box, sent, _custom = _box()
    box.date_format.setCurrentIndex(box.date_format.findData("dd/mm/yyyy hh:nn"))
    assert sent == ["dd/mm/yyyy hh:nn"]


def test_a_custom_format_is_sent_only_once_it_is_valid():
    box, sent, custom = _box()
    box.date_format.setCurrentIndex(box.date_format.findData(custom))
    assert not box.date_custom.isHidden()
    sent.clear()
    box._date_typed("dd/mm/yyyy hh:mm")
    assert sent == [] and "Use nn for minutes" in box.date_note.text()
    box._date_typed("dd-mmm-yy hh:nn")
    assert sent == ["dd-mmm-yy hh:nn"]
    assert box.date_note.text() == "Shows as 07-Mar-26 14:05"


def test_a_stored_custom_format_loads_as_custom_without_sending():
    box, sent, custom = _box()
    box.load(False, False, date_format="dd-mmm-yy hh:nn")
    assert box.date_format.currentData() == custom
    assert box.date_custom.text() == "dd-mmm-yy hh:nn"
    box.load(False, False, date_format="mm/dd/yyyy hh:nn")
    assert box.date_format.currentData() == "mm/dd/yyyy hh:nn"
    assert box.date_custom.isHidden()
    assert sent == []
