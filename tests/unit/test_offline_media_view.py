r"""The Offline Media tab. Work order 202626270513 \u00a72a-2d.

Layer: L5 widget.

Offscreen `pytest-qt`, the same three-tier method the rest of this project's
UI carries: a real `QTreeWidget` painted and read back, a real `SqliteStore`
for `refresh()`'s worker round-trip rather than a fake, and one regression
guard for the performance bug `_drive_locked` nearly introduced into the
hot `connected_volumes` path - caught by `widget.grab()` showing the Status
column pushing every other column off screen before this file existed, and
kept here so the fix cannot quietly regress.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("PySide6.QtWidgets", exc_type=ImportError)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication                        # noqa: E402

from app.ui.offline_media_view import COLUMNS, OfflineMediaView  # noqa: E402
from app.ui.presenter import (                                  # noqa: E402
    delete_volume_confirmation, offline_media_run_summary, volume_rows,
)
from app.ui.widgets.offline_media_dialogs import (              # noqa: E402
    DeleteVolumeDialog, RenameSuggestionDialog, ScanNameDialog,
)


@pytest.fixture(scope="module")
def qapp():
    yield QApplication.instance() or QApplication([])


def _settle(qapp, until, tries: int = 100) -> bool:
    for _ in range(tries):
        qapp.processEvents()
        if until():
            return True
        time.sleep(0.02)
    return False


_ROWS = [
    {"id": 1, "kind": "drive", "name": "Projects 2019", "description": "top shelf",
     "status": "ONLINE", "size_bytes": 128_000_000_000, "indexed_files": 48301,
     "last_scanned_at": 1732000000, "last_seen": 1732000000},
    {"id": 2, "kind": "drive", "name": "Old Backups", "description": "",
     "status": "OFFLINE", "size_bytes": 32_000_000_000, "indexed_files": 1200,
     "last_scanned_at": 1700000000, "last_seen": 1700000000},
    {"id": 3, "kind": "drive", "name": "Encrypted Archive", "description": "",
     "status": "LOCKED", "size_bytes": 0, "indexed_files": 0,
     "last_scanned_at": 0, "last_seen": 1732500000},
]
_ONLINE = {1: Path("F:\\")}


# ---------------------------------------------------------------------------
# volume_rows / delete_volume_confirmation / offline_media_run_summary (Qt-free)
# ---------------------------------------------------------------------------

def test_online_status_names_the_current_letter_never_stores_it():
    rows = volume_rows(_ROWS, _ONLINE, now=1732600000)
    online = rows[0]
    assert online.status == "Online as F:"
    assert online.status_code == "ONLINE"


def test_offline_status_names_when_it_was_last_seen():
    rows = volume_rows(_ROWS, _ONLINE, now=1732600000)
    offline = rows[1]
    assert offline.status_code == "OFFLINE"
    assert "Offline" in offline.status


def test_locked_status_is_short_the_explanation_is_not_in_the_column():
    r"""Regression for the layout bug `widget.grab()` found: a status
    sentence long enough to push Size/Files/Scanned off screen. The column
    text must stay short; the explanation belongs in a tooltip."""
    rows = volume_rows(_ROWS, _ONLINE, now=1732600000)
    locked = rows[2]
    assert locked.status_code == "LOCKED"
    assert locked.status == "Locked (BitLocker)"
    assert len(locked.status) < 25


def test_delete_confirmation_states_the_count_and_the_crucial_sentence():
    title, body = delete_volume_confirmation("Projects 2019", 48301)
    assert "48,301" in body
    assert "Projects 2019" in title or "Projects 2019" in body
    assert ("This removes the catalogue from Leasha's index. Nothing on "
            "the drive itself is touched.") in body


def test_run_summary_names_an_amount_for_a_scan():
    class Stats:
        indexed = 120
        seen = 400
        deleted = 0

    text = offline_media_run_summary({"volume_id": 1, "kind": "drive", "stats": Stats()})
    assert "120" in text and "400" in text


def test_run_summary_names_an_amount_for_a_delete():
    text = offline_media_run_summary({"deleted": True, "files": 48301, "name": "Projects 2019"})
    assert "48,301" in text
    assert "Nothing on the drive itself was touched" in text


# ---------------------------------------------------------------------------
# The widget itself
# ---------------------------------------------------------------------------

def test_empty_state_hides_the_tree(qapp):
    # `isHidden()`, not `isVisible()` - a widget nobody called `.show()` on
    # reports `isVisible() == False` regardless of its own `setVisible`
    # flag, because visibility is also gated by every ancestor actually
    # being on screen. `isHidden()` reads the flag this view set itself.
    view = OfflineMediaView()
    view.load([])
    assert not view.empty.isHidden()
    assert view.tree.isHidden()


def test_populated_rows_paint_all_five_columns(qapp):
    view = OfflineMediaView()
    view.load(_ROWS, _ONLINE)
    assert not view.tree.isHidden()
    assert view.empty.isHidden()
    assert view.tree.topLevelItemCount() == 3
    assert tuple(view.tree.headerItem().text(c) for c in range(5)) == COLUMNS
    first = view.tree.topLevelItem(0)
    assert first.text(0) == "Projects 2019"
    assert first.text(1) == "Online as F:"
    assert first.text(3) == "48,301"


def test_rescan_only_enabled_for_an_online_selection(qapp):
    view = OfflineMediaView()
    view.load(_ROWS, _ONLINE)

    view.tree.topLevelItem(0).setSelected(True)
    view._sync_buttons()
    assert view.rescan.isEnabled()
    assert view.delete.isEnabled()

    view.tree.clearSelection()
    view.tree.topLevelItem(1).setSelected(True)
    view._sync_buttons()
    assert not view.rescan.isEnabled()
    assert view.delete.isEnabled()          # 2c: Delete works even offline


def test_nothing_selected_disables_both_action_buttons(qapp):
    view = OfflineMediaView()
    view.load(_ROWS, _ONLINE)
    view.tree.clearSelection()
    view._sync_buttons()
    assert not view.rescan.isEnabled()
    assert not view.delete.isEnabled()


def test_set_busy_disables_every_button_and_shows_the_message(qapp):
    view = OfflineMediaView()
    view.load(_ROWS, _ONLINE)
    view.tree.topLevelItem(0).setSelected(True)
    view.set_busy("Scanning F:\\\u2026")
    assert view.status_line.text() == "Scanning F:\\\u2026"
    assert not view.scan.isEnabled()
    assert not view.rescan.isEnabled()
    assert not view.delete.isEnabled()
    view.set_busy("")
    assert view.scan.isEnabled()


def test_rescan_selected_emits_the_volume_id(qapp):
    view = OfflineMediaView()
    view.load(_ROWS, _ONLINE)
    view.tree.topLevelItem(0).setSelected(True)

    seen = []
    view.rescan_requested.connect(seen.append)
    view._rescan_selected()
    assert seen == [1]


def test_delete_selected_asks_first_then_emits(qapp, monkeypatch):
    view = OfflineMediaView()
    view.load(_ROWS, _ONLINE)
    view.tree.topLevelItem(0).setSelected(True)

    monkeypatch.setattr(DeleteVolumeDialog, "exec",
                        lambda self: DeleteVolumeDialog.DialogCode.Accepted)
    seen = []
    view.delete_requested.connect(seen.append)
    view._delete_selected()
    assert seen == [1]


def test_delete_selected_does_nothing_on_cancel(qapp, monkeypatch):
    view = OfflineMediaView()
    view.load(_ROWS, _ONLINE)
    view.tree.topLevelItem(0).setSelected(True)

    monkeypatch.setattr(DeleteVolumeDialog, "exec",
                        lambda self: DeleteVolumeDialog.DialogCode.Rejected)
    seen = []
    view.delete_requested.connect(seen.append)
    view._delete_selected()
    assert seen == []


def test_choose_and_scan_asks_for_a_folder_then_a_name(qapp, monkeypatch):
    r"""2b: the first Scan asks for a name. Confirms the whole chain -
    folder picker, then `ScanNameDialog` - without opening a real one of
    either."""
    from app.ui import offline_media_view as module

    monkeypatch.setattr(module.QFileDialog, "getExistingDirectory",
                        classmethod(lambda cls, *a, **k: "E:\\"))
    monkeypatch.setattr(ScanNameDialog, "exec",
                        lambda self: ScanNameDialog.DialogCode.Accepted)
    monkeypatch.setattr(ScanNameDialog, "chosen_name", lambda self: "Projects 2019")
    monkeypatch.setattr(ScanNameDialog, "chosen_description", lambda self: "top shelf")

    view = OfflineMediaView()
    seen = []
    view.scan_requested.connect(lambda *a: seen.append(a))
    view._choose_and_scan()
    assert seen == [("E:\\", "Projects 2019", "top shelf")]


def test_scan_name_dialog_refuses_an_empty_name(qapp):
    dialog = ScanNameDialog("E:\\")
    assert not dialog.buttons.button(dialog.buttons.StandardButton.Ok).isEnabled()
    dialog.name.setText("Projects 2019")
    assert dialog.buttons.button(dialog.buttons.StandardButton.Ok).isEnabled()
    assert dialog.chosen_name() == "Projects 2019"


# ---------------------------------------------------------------------------
# refresh() against a real store - the worker round-trip
# ---------------------------------------------------------------------------

def test_refresh_reads_a_real_store_off_a_worker(qapp, tmp_path):
    from app.storage.sqlite_store import SqliteStore

    db_path = tmp_path / "index.db"
    with SqliteStore(db_path) as store:
        store.upsert_volume(
            "test-guid-not-a-real-drive", kind="drive", name="Projects 2019",
            description="top shelf", volume_guid="test-guid-not-a-real-drive",
        )

        view = OfflineMediaView(store)
        view.refresh()
        ok = _settle(qapp, lambda: view.tree.topLevelItemCount() == 1)
        assert ok, "refresh() never painted the row"
        assert view.tree.topLevelItem(0).text(0) == "Projects 2019"
        # A GUID no real machine has is correctly OFFLINE, not ONLINE - the
        # same fixture trick `test_offline_media.py` already uses.
        assert "Offline" in view.tree.topLevelItem(0).text(1)


# ---------------------------------------------------------------------------
# The performance bug `widget.grab()`-adjacent testing caught in review:
# a BitLocker probe (a PowerShell subprocess) must never sit in the hot
# `connected_volumes` path.
# ---------------------------------------------------------------------------

def test_connected_volumes_never_probes_bitlocker(monkeypatch):
    r"""`connected_volumes` sits under `resolve_file_path`, which a search
    result's Open and every pipeline walk goes through - see its own
    docstring. `_drive_locked` (a PowerShell subprocess, whole seconds) was
    briefly wired in here during this order's own build and made an
    18-test file take minutes; it must live only in
    `refresh_volume_statuses`, which 2a scopes to "once, on panel refresh"."""
    from app.index import offline_media as module

    def _boom(*_a, **_k):
        raise AssertionError("connected_volumes() must never call is_bitlocker_locked")

    monkeypatch.setattr("app.core.volumes_win.is_bitlocker_locked", _boom)

    class _FakeStore:
        def list_volumes(self):
            return [{
                "id": 1, "kind": "drive", "volume_guid": "test-guid-not-a-real-drive",
            }]

    # No real drive on this machine will ever have this GUID, so
    # `find_drive_by_guid` correctly returns None and `online` is empty -
    # the point is only that no subprocess is ever attempted getting there.
    result = module.connected_volumes(_FakeStore())
    assert result == {}


# ---------------------------------------------------------------------------
# 202626270514 1a's offer, accepted through the tab: RenameSuggestionDialog
# ---------------------------------------------------------------------------

def test_rename_suggestion_dialog_names_the_suggestion(qapp):
    dialog = RenameSuggestionDialog("Old NAS")
    assert "Old NAS" in dialog.windowTitle()
    yes = dialog.buttons.button(dialog.buttons.StandardButton.Yes)
    no = dialog.buttons.button(dialog.buttons.StandardButton.No)
    assert "Old NAS" in yes.text()
    assert yes.text() != no.text()


def test_rename_suggestion_dialog_yes_accepts_no_rejects(qapp):
    """Qt's own Yes/No roles - proven rather than assumed, since a mapped
    role that silently changed would make `dialog.exec() == Accepted`
    mean the opposite of what the button said."""
    from PySide6.QtWidgets import QDialogButtonBox

    dialog = RenameSuggestionDialog("Old NAS")
    yes = dialog.buttons.button(QDialogButtonBox.StandardButton.Yes)
    no = dialog.buttons.button(QDialogButtonBox.StandardButton.No)
    assert dialog.buttons.buttonRole(yes) == QDialogButtonBox.ButtonRole.YesRole
    assert dialog.buttons.buttonRole(no) == QDialogButtonBox.ButtonRole.NoRole


# ---------------------------------------------------------------------------
# 202626270514 1d + 3b-3: the tab's own help line
# ---------------------------------------------------------------------------

def test_the_tab_shows_its_own_help_line(qapp):
    from app.ui.presenter import offline_media_help_text

    view = OfflineMediaView()
    assert view.help_line.text() == offline_media_help_text()


def test_help_text_names_the_decommission_case():
    from app.ui.presenter import offline_media_help_text

    text = offline_media_help_text().lower()
    assert "switched off" in text or "decommission" in text
    assert "this account could read" in text


def test_help_text_names_the_backup_doctrine():
    from app.ui.presenter import offline_media_help_text

    text = offline_media_help_text().lower()
    assert "veeam" in text
    assert "archived" in text
