"""Review CS-12 (2026-10-14): a contact the account refuses during a sync can be opened from the Sync window at
once - the live progress row carries its contact id - not only after the window reloads.

Layer: L5
"""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt                                    # noqa: E402

from app.ui.contacts_tools.sync_dialog import SyncDialog         # noqa: E402
from tests.unit.test_contacts_tools_ui_common import open_dialog  # noqa: E402
from tests.unit.test_sync_dialog import ctx  # noqa: E402,F401 - the Sync window's fixture: a fake scheduler


def _opened(c):
    opened = []
    c.service.open_contact = opened.append
    c.service.sync_overview = lambda: []
    return opened


@pytest.mark.slow
def test_a_refusal_in_the_progress_opens_its_contact(ctx, qtbot):      # noqa: F811
    c, sched, _calls = ctx
    opened = _opened(c)
    dlg = open_dialog(qtbot, SyncDialog, c)
    sched.busy = True
    sched.run_started.emit("sync-now")
    sched.progress.emit({"account": "MS Main", "account_id": 2, "phase": "writing", "done": 1, "total": 3,
                         "failed": 1, "failures": [["Bob Bell", "HTTP 400", 41]]})
    item = dlg.problems.topLevelItem(0)
    assert item.text(1) == "Bob Bell" and item.data(0, Qt.ItemDataRole.UserRole) == 41
    assert item.data(1, Qt.ItemDataRole.UserRole) == 2
    dlg._open_problem(item)
    assert opened == [41]


def test_a_refusal_in_the_finished_run_opens_its_contact(ctx, qtbot):  # noqa: F811
    c, sched, _calls = ctx
    opened = _opened(c)
    dlg = open_dialog(qtbot, SyncDialog, c)
    out = SimpleNamespace(account_id=2, account_name="MS Main", status="partial", error=None,
                          plan=SimpleNamespace(add_in=0, create_out=0, merge_in=0, update_out=9, delete_out=0,
                                               deleted_in=0, failed_out=2,
                                               failures=[["Bob Bell", "HTTP 400", 41], ["Cy Cox", "HTTP 400"]]))
    sched.run_finished.emit([out])
    items = {dlg.problems.topLevelItem(i).text(1): dlg.problems.topLevelItem(i)
             for i in range(dlg.problems.topLevelItemCount())}
    assert items["Bob Bell"].data(0, Qt.ItemDataRole.UserRole) == 41
    assert items["Cy Cox"].data(0, Qt.ItemDataRole.UserRole) is None       # an old row without an id still shows
    dlg._open_problem(items["Bob Bell"])
    assert opened == [41]
