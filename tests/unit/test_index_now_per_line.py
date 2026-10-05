r"""One line, now: "Index now" on a folder, Rescan on an offline source, and the
hardware ID the Offline list never showed.

Layer: L5, and the L3 helper behind the hardware ID.

2026-10-02, the owner: with a lot of archives in the folder list, Start reads
every one of them to bring one up to date - each line should carry its own
button. The same on the Offline list, which should also show each drive's
hardware ID. What is checked here:

* the folder list's line button and its menu entry name **that line's** folder,
  whichever line is selected;
* the Settings page relays it, and the window starts a run over that folder
  only, read in full even when the line says Archive, on the Indexing page;
* each Offline line's Rescan asks for its own source, is off for a source out
  of reach and while something is running;
* the Hardware ID column says the disk's serial, says so when there is none,
  and shows a share's address - and a Rescan asks Windows again for a drive
  whose serial was never read.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from app.ui.presenter.offline import HARDWARE_ID_MISSING, hardware_id_words, volume_rows

# ---------------------------------------------------------------------------
# The hardware ID, in words (no Qt)
# ---------------------------------------------------------------------------

_DRIVE = {"id": 1, "kind": "drive", "name": "Projects 2019", "status": "ONLINE",
          "hardware_serial": "WD-WX41A49H7K2P",
          "volume_guid": "\\\\?\\Volume{4c1b02c1-0000-0000-0000-100000000000}\\",
          "fs_label": "PROJECTS", "identity_key": "guid-1"}
_NO_SERIAL = {"id": 2, "kind": "drive", "name": "Old Backups", "status": "OFFLINE",
              "hardware_serial": None, "volume_guid": "\\\\?\\Volume{aaaa}\\",
              "identity_key": "guid-2"}
_SHARE = {"id": 3, "kind": "network", "name": "Old NAS", "status": "OFFLINE",
          "identity_key": "\\\\nas01\\projects"}


def test_a_drive_shows_the_serial_of_the_disk_itself():
    cell, note = hardware_id_words(_DRIVE)
    assert cell == "WD-WX41A49H7K2P"
    assert "Volume{4c1b02c1" in note, "what it is recognised by is not said"
    assert "PROJECTS" in note


def test_a_drive_with_no_serial_says_so_and_what_to_do():
    cell, note = hardware_id_words(_NO_SERIAL)
    assert cell == HARDWARE_ID_MISSING
    assert "Rescan" in note and "Volume{aaaa}" in note


def test_a_share_shows_its_address_because_it_has_no_hardware():
    cell, note = hardware_id_words(_SHARE)
    assert cell == "\\\\nas01\\projects"
    assert "no hardware" in note


def test_the_rows_carry_the_words_and_the_raw_serial():
    rows = volume_rows([_DRIVE, _NO_SERIAL, _SHARE], {1: Path("F:\\")})
    assert [row.hardware_id for row in rows] == [
        "WD-WX41A49H7K2P", HARDWARE_ID_MISSING, "\\\\nas01\\projects"]
    assert [row.hardware_serial for row in rows] == ["WD-WX41A49H7K2P", "", ""]
    assert all(row.hardware_note for row in rows)


# ---------------------------------------------------------------------------
# A Rescan asks again for a serial that was never read
# ---------------------------------------------------------------------------

class TestARescanAsksForAMissingSerial:

    def _store(self, tmp_path, **volume):
        from app.storage.sqlite_store import SqliteStore

        store = SqliteStore(tmp_path / "index.db").connect()
        volume_id = store.upsert_volume(
            "test-guid", kind=volume.pop("kind", "drive"), name="Old Backups", **volume)
        return store, store.get_volume(volume_id)

    def test_it_is_read_and_kept(self, tmp_path, monkeypatch):
        from app.index.offline_media import remember_hardware_serial

        monkeypatch.setattr("app.core.volumes_win.hardware_serial_for_root",
                            lambda root: "S3RIAL-42")
        store, record = self._store(tmp_path)
        try:
            assert remember_hardware_serial(store, record, Path("F:\\")) == "S3RIAL-42"
            kept = store.get_volume(record.id)
            assert kept.hardware_serial == "S3RIAL-42"
            assert kept.name == "Old Backups", "the name was changed by a lookup"
        finally:
            store.close()

    def test_one_already_stored_is_not_asked_for_again(self, tmp_path, monkeypatch):
        from app.index.offline_media import remember_hardware_serial

        def _boom(root):
            raise AssertionError("Windows was asked for a serial already held")

        monkeypatch.setattr("app.core.volumes_win.hardware_serial_for_root", _boom)
        store, record = self._store(tmp_path, hardware_serial="KEPT-1")
        try:
            assert remember_hardware_serial(store, record, Path("F:\\")) is None
            assert store.get_volume(record.id).hardware_serial == "KEPT-1"
        finally:
            store.close()

    def test_a_share_is_never_asked(self, tmp_path, monkeypatch):
        from app.index.offline_media import remember_hardware_serial

        def _boom(root):
            raise AssertionError("a network share has no disk to ask about")

        monkeypatch.setattr("app.core.volumes_win.hardware_serial_for_root", _boom)
        store, record = self._store(tmp_path, kind="network")
        try:
            assert remember_hardware_serial(store, record, Path("\\\\nas01\\p")) is None
        finally:
            store.close()

    def test_a_lookup_that_fails_does_not_cost_the_rescan(self, tmp_path, monkeypatch):
        from app.index.offline_media import remember_hardware_serial

        def _fails(root):
            raise OSError("the provider did not answer")

        monkeypatch.setattr("app.core.volumes_win.hardware_serial_for_root", _fails)
        store, record = self._store(tmp_path)
        try:
            assert remember_hardware_serial(store, record, Path("F:\\")) is None
        finally:
            store.close()


# ---------------------------------------------------------------------------
# The widgets
# ---------------------------------------------------------------------------

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QPushButton  # noqa: E402


def _box(qtbot, roots):
    from app.ui.widgets.roots_box import RootsBox

    box = RootsBox()
    qtbot.addWidget(box)
    box.set_roots(list(roots))
    return box


def _line_button(tree, row: int, column: int) -> QPushButton:
    cell = tree.itemWidget(tree.topLevelItem(row), column)
    assert cell is not None, f"line {row} has no button in column {column}"
    button = cell.findChild(QPushButton)
    assert button is not None
    return button


@pytest.mark.gui
class TestIndexNowOnAFolderLine:

    def test_every_line_has_its_own_button_after_the_four_columns_that_were_there(self, qtbot):
        box = _box(qtbot, ["/a", "/b", "/c"])
        header = box.tree.headerItem()
        assert [header.text(c) for c in range(box.tree.columnCount())] == [
            "Folder", "How it is indexed", "Cloud content", "Read first", "Index now"]
        for row, root in enumerate(["/a", "/b", "/c"]):
            button = _line_button(box.tree, row, 4)
            # An icon and no words (the owner: "make sure the button has an
            # icon only"). The words are in the heading above, the tooltip,
            # and what a screen reader is given.
            assert button.text() == "" and not button.icon().isNull()
            assert button.toolTip().startswith("Index now")
            assert button.accessibleName() == f"Index this folder now: {root}"

    def test_pressing_it_names_that_line_whichever_line_is_selected(self, qtbot):
        box = _box(qtbot, ["/a", "/b", "/c"])
        heard: list[str] = []
        box.index_requested.connect(heard.append)
        box.tree.setCurrentItem(box.tree.topLevelItem(0))      # /a is selected

        _line_button(box.tree, 2, 4).click()                   # /c is pressed
        _line_button(box.tree, 1, 4).click()
        assert heard == ["/c", "/b"]

    def test_it_changes_nothing_about_the_list(self, qtbot):
        box = _box(qtbot, ["/a", "/b"])
        changed: list = []
        for signal in (box.roots_changed, box.modes_changed, box.first_changed):
            signal.connect(changed.append)
        _line_button(box.tree, 0, 4).click()
        assert changed == [] and box.current_roots() == ["/a", "/b"]

    def test_a_folder_added_later_gets_one_too(self, qtbot):
        box = _box(qtbot, ["/a"])
        box.add_root("/later")
        heard: list[str] = []
        box.index_requested.connect(heard.append)
        _line_button(box.tree, 1, 4).click()
        assert heard == ["/later"]

    def test_the_button_follows_the_button_system(self, qtbot):
        from app.ui.widgets.buttons import BUTTONS, ICON_PX, ROLES

        assert "Index now" in BUTTONS
        box = _box(qtbot, ["/a"])
        button = _line_button(box.tree, 0, 4)
        assert button.property("buttonRole") in ROLES
        assert button.property("buttonIcon") == BUTTONS["Index now"][0]
        assert button.iconSize().height() == ICON_PX
        assert button.property("iconOnly") is True

    def test_an_icon_button_needs_words_the_table_knows(self, qtbot):
        """Its icon and kind come from the table, like every other button's."""
        from app.ui.widgets.buttons import icon_button

        with pytest.raises(ValueError):
            icon_button("Words nobody listed", tooltip="Does a thing.")
        with pytest.raises(ValueError):
            icon_button("Rescan", tooltip="  ")      # nothing would say what it does
        button = icon_button("Rescan", tooltip="Rescan this source.")
        qtbot.addWidget(button)
        assert button.text() == "" and button.accessibleName() == "Rescan"
        assert button.toolTip() == "Rescan this source."

    def test_the_line_is_tall_enough_for_its_button(self, qtbot):
        """Measured before the fix: a 28px button in a 26px cell, its bottom
        edge cut off. A widget set on a row is given the row less the row's
        padding, so the row is told its height (`buttons.put_on_row`)."""
        from app.ui.widgets.buttons import ROW_PAD_Y

        box = _box(qtbot, ["/a", "/b"])
        box.resize(900, 300)
        box.show()
        qtbot.waitExposed(box)
        item = box.tree.topLevelItem(0)
        button = _line_button(box.tree, 0, 4)
        row = box.tree.visualItemRect(item).height()
        assert row >= button.sizeHint().height() + ROW_PAD_Y
        assert button.height() == button.sizeHint().height(), "the button was squeezed"
        cell = box.tree.itemWidget(item, 4)
        assert button.geometry().bottom() <= cell.rect().bottom(), "cut off at the bottom"

    @pytest.mark.parametrize("scheme", ["light", "dark"])
    def test_the_cell_round_the_button_is_the_rows_own_colour(self, qtbot, scheme):
        """Seen 2026-10-04 in the regrabbed Offline page: a grey block behind
        the round Rescan button. The theme paints every plain `QWidget` the
        window colour, and the cell `put_on_row` makes is one, over a white
        row. The cell must show the row through it."""
        from PySide6.QtCore import QPoint

        from app.ui import theme

        box = _box(qtbot, ["/a", "/b"])
        box.setStyleSheet(theme.stylesheet(scheme, detected=scheme, base_pt=9.0))
        box.resize(900, 300)
        box.show()
        qtbot.waitExposed(box)
        tree = box.tree
        image = tree.viewport().grab().toImage()
        item = tree.topLevelItem(1)
        # The row's own colour, read from the row: the right end of its Folder
        # column, past the short name "/b". (Sampling under the rows instead
        # landed on a separator when the suite's font made the rows taller.)
        folder = tree.visualRect(tree.indexFromItem(item, 0))
        row_colour = image.pixel(QPoint(folder.right() - 3, folder.center().y()))
        for column in (2, 4):                      # the Cloud content box, the Index now button
            cell = tree.itemWidget(item, column)
            beside = cell.mapTo(tree.viewport(), QPoint(cell.width() - 2, cell.height() // 2))
            assert image.pixel(beside) == row_colour, (
                f"{scheme}, column {column}: the cell is painted "
                f"{image.pixelColor(beside).name()} over a {image.pixelColor(row_colour).name()} row")

    def test_the_settings_page_relays_it(self, qtbot):
        from app.ui.settings_view import SettingsView

        view = SettingsView(SimpleNamespace(), None)
        qtbot.addWidget(view)
        view.set_roots(["/a", "/b"])
        heard: list[str] = []
        view.index_folder_requested.connect(heard.append)
        _line_button(view.roots_box.tree, 1, 4).click()
        assert heard == ["/b"]


class TestWhatTheWindowDoesWithIt:
    """`IndexController._index_folder_now`, against a stand-in window."""

    def _window(self):
        calls: list = []
        window = SimpleNamespace(
            indexing_view=object(),
            _show=lambda view: calls.append(("show", view)),
            _start_indexing=lambda **kw: calls.append(("start", kw)),
        )
        return window, calls

    def test_one_folder_read_in_full_on_the_indexing_page(self):
        from app.ui.controllers.index_controller import IndexController

        window, calls = self._window()
        IndexController._index_folder_now(SimpleNamespace(_w=window), r"D:\OutlookArchive")
        assert calls == [
            ("show", window.indexing_view),
            ("start", {"roots": [r"D:\OutlookArchive"], "recheck_archives": True}),
        ]

    def test_nothing_is_started_for_no_folder(self):
        from app.ui.controllers.index_controller import IndexController

        window, calls = self._window()
        IndexController._index_folder_now(SimpleNamespace(_w=window), "  ")
        assert calls == []


@pytest.mark.gui
def test_the_real_window_starts_a_run_over_that_one_folder(gui_mainwindow, monkeypatch):
    """Wired, not only built: the line's button on the real Settings page ends
    in `_start_indexing` with that folder and no other."""
    from tests.unit.conftest import gui_pump

    app, window, _store, _engine = gui_mainwindow
    started: list = []
    monkeypatch.setattr(
        window.index_ctl, "_start_indexing",
        lambda **kw: started.append(kw))
    before = window.settings_view.current_roots()
    try:
        window.settings_view.set_roots(["C:/work", "C:/other"])
        _line_button(window.settings_view.roots_box.tree, 1, 4).click()
        gui_pump(app)
        assert started == [{"roots": ["C:/other"], "recheck_archives": True}]
        assert window.rail.currentIndex() == window._tab_index[window.indexing_view]
    finally:
        window.settings_view.set_roots(before)


# ---------------------------------------------------------------------------
# The Offline list
# ---------------------------------------------------------------------------

_ROWS = [
    dict(_DRIVE, size_bytes=128_000_000_000, indexed_files=48301,
         last_scanned_at=1732000000, last_seen=1732000000, description="top shelf"),
    dict(_NO_SERIAL, size_bytes=32_000_000_000, indexed_files=1200,
         last_scanned_at=1700000000, last_seen=1700000000, description=""),
    dict(_SHARE, size_bytes=0, indexed_files=10, last_scanned_at=1700000000,
         last_seen=1700000000, description=""),
]
_ONLINE = {1: Path("F:\\")}


def _offline(qtbot):
    from app.ui.offline_media_view import OfflineMediaView

    view = OfflineMediaView()
    qtbot.addWidget(view)
    view.load(_ROWS, _ONLINE)
    return view


@pytest.mark.gui
class TestTheOfflineList:

    def test_the_two_new_columns_come_after_the_five_that_were_there(self, qtbot):
        from app.ui.offline_media_view import ALL_COLUMNS, COLUMNS

        view = _offline(qtbot)
        header = view.tree.headerItem()
        shown = tuple(header.text(c) for c in range(view.tree.columnCount()))
        assert shown == ALL_COLUMNS == COLUMNS + ("Hardware ID", "Rescan")

    def test_the_hardware_id_is_on_the_line_with_its_sentence_behind_it(self, qtbot):
        view = _offline(qtbot)
        cells = [view.tree.topLevelItem(row).text(5) for row in range(3)]
        assert cells == ["WD-WX41A49H7K2P", HARDWARE_ID_MISSING, "\\\\nas01\\projects"]
        assert all(view.tree.topLevelItem(row).toolTip(5) for row in range(3))

    def test_a_lines_rescan_asks_for_its_own_source(self, qtbot):
        view = _offline(qtbot)
        heard: list[int] = []
        view.rescan_requested.connect(heard.append)
        view.tree.topLevelItem(1).setSelected(True)            # another line is selected
        _line_button(view.tree, 0, 6).click()
        assert heard == [1]

    def test_a_source_out_of_reach_cannot_be_rescanned_and_says_why(self, qtbot):
        view = _offline(qtbot)
        online, away = _line_button(view.tree, 0, 6), _line_button(view.tree, 1, 6)
        assert online.isEnabled() and not away.isEnabled()
        assert "plugged in" in away.toolTip()
        heard: list[int] = []
        view.rescan_requested.connect(heard.append)
        view._rescan_row(2)
        assert heard == []

    def test_nothing_can_be_started_while_something_is_running(self, qtbot):
        view = _offline(qtbot)
        view.set_busy("Rescanning\u2026")
        assert not _line_button(view.tree, 0, 6).isEnabled()
        heard: list[int] = []
        view.rescan_requested.connect(heard.append)
        view._rescan_row(1)
        assert heard == []
        view.set_busy("")
        assert _line_button(view.tree, 0, 6).isEnabled()

    def test_the_hardware_id_can_be_copied(self, qtbot):
        from PySide6.QtWidgets import QApplication

        view = _offline(qtbot)
        assert view.copy_hardware_id(1) is True
        assert QApplication.clipboard().text() == "WD-WX41A49H7K2P"
        assert "Projects 2019" in view.status_line.text()
        assert view.copy_hardware_id(2) is False, "there is no serial to copy"

    def test_the_line_buttons_follow_the_button_system(self, qtbot):
        from app.ui.widgets.buttons import ROLES

        view = _offline(qtbot)
        button = _line_button(view.tree, 0, 6)
        assert button.text() == "", "an icon and no words, like the folder list's"
        assert button.accessibleName() == "Rescan Projects 2019"
        assert button.toolTip().startswith("Rescan")
        assert button.property("buttonRole") in ROLES and not button.icon().isNull()
