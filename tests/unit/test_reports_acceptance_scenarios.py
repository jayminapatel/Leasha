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
Each scenario below opens **Reports in the real `MainWindow`** - real store,
real workers - and does what that person would.
"""

from __future__ import annotations

import os

import pytest

pytest.importorskip("PyQt6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QThreadPool, Qt                           # noqa: E402
from app.ui.widgets.timeline_host import REPORT_KEY  # noqa: E402 - the list's key role
from PyQt6.QtWidgets import QFileDialog                             # noqa: E402

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
    window._show(window.reports_view)
    gui_pump(app)
    view = window.reports_view
    view._space_cached_at = None
    view._space_document = ""
    view.list.setCurrentRow(0)
    return app, window, view, ids


def pick(view, key: str) -> None:
    view.list.setCurrentRow([view.list.item(i).data(REPORT_KEY) for i in range(view.list.count())].index(key))


def test_a_family_member_can_read_the_map_and_print_it_and_know_which_drawer_holds_what(
        family, qtbot, tmp_path, monkeypatch):
    import app.ui.reports_view as reports_view
    from app.ui.widgets.report_export_dialog import SourceSelectionDialog

    app, _window, view, _ids = family
    view.refresh()
    qtbot.waitUntil(lambda: bool(view._sources), timeout=15000)
    pick(view, "inheritance")
    on_screen = view.body.toPlainText()
    for name in ("Projects 2019", "LTO tape B-0042"):
        assert name in on_screen
    assert "fire safe, hallway cupboard" in on_screen and "the old work drive" in on_screen
    assert SECRET not in on_screen

    # Press Export, keep every source, choose where the PDF goes.
    target = tmp_path / "map.pdf"
    monkeypatch.setattr(SourceSelectionDialog, "exec", lambda self: SourceSelectionDialog.DialogCode.Accepted)
    monkeypatch.setattr(QFileDialog, "getSaveFileName",
                        staticmethod(lambda *a, **k: (str(target), "PDF files (*.pdf)")))
    written: list = []
    real = reports_view._write_pdf
    monkeypatch.setattr(reports_view, "_write_pdf",
                        lambda document, path: (written.append(str(document)), real(document, path)))
    qtbot.mouseClick(view.export, Qt.MouseButton.LeftButton)
    QThreadPool.globalInstance().waitForDone(20_000)
    gui_pump(app)
    assert target.is_file() and target.read_bytes()[:4] == b"%PDF"
    document = written[0]
    assert "Projects 2019" in document and "fire safe, hallway cupboard" in document
    assert SECRET not in document                                # names and places, never contents
    # (the printed page itself is read back in test_reports_fixtures.py, in a process with fonts)


def test_the_owner_sees_in_one_glance_which_drive_holds_the_only_copy(family, qtbot):
    app, _window, view, _ids = family
    view.refresh()
    qtbot.waitUntil(lambda: bool(view._space_document), timeout=20000)
    pick(view, "space")
    headline = view.space_table.headline.text().splitlines()
    first = next(line for line in headline if "exist nowhere else" in line or "exists nowhere else" in line)
    assert "Projects 2019" in first, headline                     # the drive in the drawer, first
    tree = view.space_table.trees["only-copy"]
    assert tree.topLevelItem(0).text(0) == "Projects 2019"         # ...and first in its table


def test_june_2015_is_a_place_you_can_go(family, qtbot):
    r"""Reports -> Browse your timeline -> 2015 -> Jun: the photograph, the
    letter, the drive's photograph and the message, wherever each lives now."""
    app, _window, view, ids = family
    pick(view, "timeline")
    timeline = view.timeline
    assert timeline.isVisibleTo(view)
    view.refresh()
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
