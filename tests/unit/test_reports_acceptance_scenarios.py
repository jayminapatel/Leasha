r"""Order 202626270602 (0n) - the acceptance sentences, each pressed in the real window.

Layer: L5

The order ends with three sentences (its own words):

1. *a family member who has never seen Leasha can read the printed map and know
   which drawer holds what;*
2. *the owner learns in one glance which drive holds the only copy of anything;*
3. *"June 2015" is a place you can go - every photo, letter and file from that
   month, wherever it lives now.*

The 0m convention (`WORKORDER-CONVENTIONS.md` 5b): a sentence that promises
something clickable is not done until a pytest-qt scenario presses the keys.
Each scenario below opens **the page in the real `MainWindow`** - real store,
real workers - and does what that person would.

Sentence 1 went with Digital Inheritance (order 1q D9, 2026-10-11, at the owner's
word); the Space Report is the "Report" entry of the Tidy page now.
"""

from __future__ import annotations

import os

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QThreadPool, Qt                           # noqa: E402

from tests.unit.conftest import gui_pump                            # noqa: E402
from tests.unit.timeline_env import add_file, add_mail, noon        # noqa: E402

pytestmark = pytest.mark.gui

SECRET = "ACCOUNT-PIN-4471-SECRET"


@pytest.fixture
def family(gui_mainwindow, qtbot):
    """The real window over a small family's index: a computer, a drive in a
    drawer, a tape in a safe."""
    app, window, store, _engine = gui_mainwindow
    drive = store.upsert_volume("acc-projects", kind="drive", name="Projects 2019",
                                description="the old work drive", status="OFFLINE", size_bytes=9_000_000)
    tape = store.upsert_volume("acc-tape", kind="archived", name="LTO tape B-0042",
                               description="everything before 2012",
                               location_note="fire safe, hallway cupboard", status="ARCHIVED",
                               size_bytes=50_000_000)
    ids = {
        "holiday": add_file(store, "C:/family/Photos/holiday.jpg", mtime=noon(2015, 6, 1),
                            content_hash="dup", size=4_000_000),
        "holiday_tape": add_file(store, "leasha-volume://acc/holiday-2011.jpg", mtime=noon(2015, 6, 1),
                                 content_hash="dup", size=4_000_000, volume_id=tape,
                                 relative_path="holiday-2011.jpg"),
        "accounts": add_file(store, "leasha-volume://acc/Accounts/2014.xlsx", mtime=noon(2015, 6, 1),
                             content_hash="only-here", size=700_000, volume_id=drive,
                             relative_path="Accounts/2014.xlsx"),
        "photo": add_file(store, "C:/family/Photos/lake.jpg", mtime=noon(2019, 3, 1),
                          taken=noon(2015, 6, 10)),
        "letter": add_file(store, "C:/family/Letters/to-the-bank.docx", mtime=noon(2015, 6, 20)),
        "beach": add_file(store, "leasha-volume://acc/Holiday/beach.jpg", mtime=noon(2020, 1, 1),
                          taken=noon(2015, 6, 15), volume_id=drive, relative_path="Holiday/beach.jpg"),
        "mail": add_mail(store, "Wedding plans", sent=noon(2015, 6, 25), container_mtime=noon(2021, 3, 3)),
    }
    store.replace_chunks(ids["holiday"], [{"ordinal": 0, "text": SECRET}])
    with store.write() as conn:
        conn.execute("UPDATE files SET indexed_at = 1700000000 + id WHERE indexed_at IS NULL")
    store.set_state("ui:roots", "C:/family")
    window.resize(1200, 800)
    window.show()
    window._show(window.tidy_view)
    gui_pump(app)
    window.tidy_view.show_kind("report")
    view = window.tidy_view.report
    view._space_cached_at = None
    view._space_document = ""
    return app, window, view, ids


@pytest.mark.slow
def test_the_owner_sees_in_one_glance_which_drive_holds_the_only_copy(family, qtbot):
    app, _window, view, _ids = family
    view.refresh()
    qtbot.waitUntil(lambda: bool(view._space_document), timeout=20000)
    headline = view.space_table.headline.text().splitlines()
    first = next(line for line in headline if "exist nowhere else" in line or "exists nowhere else" in line)
    assert "Projects 2019" in first, headline                     # the drive in the drawer, first
    tree = view.space_table.trees["only-copy"]
    assert tree.topLevelItem(0).text(0) == "Projects 2019"         # ...and first in its table


def test_june_2015_is_a_place_you_can_go(family, qtbot):
    r"""Browse -> 2015 -> Jun: the photograph, the letter, the drive's photograph
    and the message, wherever each lives now. (Reports -> Browse your timeline
    until order 1i, 2026-10-11, gave the timeline its own rail page.)"""
    app, window, _view, ids = family
    timeline = window.timeline_view
    window._show(timeline)
    assert timeline.isVisibleTo(window)
    timeline.refresh()
    qtbot.waitUntil(lambda: timeline._overview is not None and timeline.picker.year_box.count() > 0,
                    timeout=20000)
    at = timeline.picker.year_box.findData(2015)
    assert at >= 0
    timeline.picker.year_box.setCurrentIndex(at)                    # choose the year
    qtbot.waitUntil(lambda: timeline.picker.month_buttons[6].isEnabled(), timeout=10000)
    qtbot.mouseClick(timeline.picker.month_buttons[6], Qt.MouseButton.LeftButton)   # then June
    qtbot.waitUntil(lambda: timeline.heading.text() == "June 2015" and not timeline._loading
                    and timeline.list.block_count() > 0, timeout=15000)
    shown = [f.head for r in range(timeline.list.block_count()) for f in timeline.list.block_at(r).folds]
    by_id = {e.file_id: e for e in shown}
    assert {ids["photo"], ids["beach"], ids["letter"], ids["mail"]} <= set(by_id)
    assert by_id[ids["photo"]].basis == "taken" and by_id[ids["letter"]].basis == "saved"
    assert by_id[ids["beach"]].source_name == "Projects 2019" and not by_id[ids["beach"]].reachable
    assert [e.when_ns for e in shown] == sorted(e.when_ns for e in shown)
    QThreadPool.globalInstance().waitForDone(5000)
