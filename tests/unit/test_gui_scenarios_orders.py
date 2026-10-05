r"""Order 0m section 1b - the journeys the shipped orders promised that no
pytest-qt scenario yet pressed for real, in the assembled `MainWindow`.

Layer: L5. Marked `gui`; offscreen; in the default run.

What was already covered is listed, order by order, in the coverage table in
`docs/WORKORDER-202626270547-test-automation.md`'s 2026-09-20 note. What is here
is the remainder that could be pressed without hardware or the owner's corpus:

* **0a** the `/` menu's *scoped values*: type `/type`, choose it, and the values
  that exist in the index are offered - by the mouse, on the real popup.
* **0k / 0m 1b** Offline Media **Scan, Rescan and Delete, each pressed**. The
  directory chooser is the one native dialog a headless run cannot drive; it is
  replaced by a function returning the fixture folder, and everything else - the
  name dialog, the worker, the store, the tree, the confirmation dialog - is the
  real one. (The old test only asserted `rescan.isEnabled() or delete.isEnabled()`.)
* **0k** the killer case, first half: describe a file from memory, and the result
  says it is on a named drive that is not plugged in.

One window for the module (the harness rule); the scenarios are independent.
"""

from __future__ import annotations

import pytest

pytest.importorskip("PyQt6")

from PyQt6.QtCore import Qt                                          # noqa: E402

from tests.unit.conftest import gui_pump                             # noqa: E402

pytestmark = pytest.mark.gui


# ---------------------------------------------------------------------------
# 0a - the `/` menu's scoped values
# ---------------------------------------------------------------------------

def test_slash_type_then_a_space_offers_the_types_the_index_actually_holds(gui_mainwindow, qtbot):
    app, window, _store, _engine = gui_mainwindow
    view = window.search_view
    view.input.clear()
    gui_pump(app)

    qtbot.keyClicks(view.input, "/type")
    popup = view.commands.popup()
    qtbot.waitUntil(lambda: popup.isVisible() and popup.model().rowCount() > 0, timeout=3000)
    rect = popup.visualRect(popup.model().index(0, 0))
    qtbot.mouseClick(popup.viewport(), Qt.MouseButton.LeftButton, pos=rect.center())
    gui_pump(app)
    assert "type:" in view.input.text().replace("/", "")

    # The value menu lists what is in *this* index: the fixture holds .txt and .pdf.
    def offered() -> list[str]:
        model = popup.model()
        return [str(model.index(i, 0).data(Qt.ItemDataRole.DisplayRole))
                for i in range(model.rowCount())]

    qtbot.waitUntil(lambda: popup.isVisible() and any("pdf" in text for text in offered()),
                    timeout=3000)
    assert any("txt" in text for text in offered())
    assert not any("docx" in text for text in offered()), "offered a type the index does not hold"

    # Choosing one writes it into the box, and the box then finds only that type.
    pdf_row = next(i for i, text in enumerate(offered()) if "pdf" in text)
    rect = popup.visualRect(popup.model().index(pdf_row, 0))
    qtbot.mouseClick(popup.viewport(), Qt.MouseButton.LeftButton, pos=rect.center())
    gui_pump(app)
    assert "pdf" in view.input.text()
    view.search_now()
    def names() -> list[str]:
        return [str(view.results._model.index(i, 0).data(Qt.ItemDataRole.DisplayRole)).lower()
                for i in range(view.results._model.rowCount())]

    qtbot.waitUntil(lambda: any("newcastle-invoice" in n for n in names()), timeout=8000)
    assert not any("barnsley-report" in n for n in names())
    view.input.clear()


# ---------------------------------------------------------------------------
# 0k - the killer case, first half: describe a file from memory and be told it is
# on a named drive that is not plugged in (the second half - reading its text
# without the drive - is unproven, see the note at the end of the test).
# ---------------------------------------------------------------------------

def test_a_file_on_an_unplugged_drive_says_which_drive_it_is_on(gui_mainwindow, qtbot):
    from PyQt6.QtWidgets import QToolButton

    from app.ui.presenter import offline_volume_note
    from app.ui.result_delegate import ROLE_PAYLOAD

    app, window, store, _engine = gui_mainwindow
    view = window.search_view

    volume_id = store.upsert_volume(
        "\\?\\Volume{00000000-0000-0000-0000-0000000000a1}\\", kind="drive",
        name="Projects 2019", description="top shelf, office cupboard",
        volume_guid="\\?\\Volume{00000000-0000-0000-0000-0000000000a1}\\",
        status="OFFLINE", seen_at=1_731_400_000)                     # 12 Nov 2024
    file_id = store.upsert_file(
        "offline://Projects 2019/quay-wall-inspection.txt", parent_dir="offline://Projects 2019",
        ext="txt", size_bytes=1, mtime_ns=1, status="INDEXED", source_kind="file",
        volume_id=volume_id, relative_path="quay-wall-inspection.txt")
    store.replace_chunks(file_id, [{"ordinal": 0,
                                    "text": "quay wall inspection found a hairline crack near bollard nine"}])

    view.input.clear()
    gui_pump(app)
    qtbot.keyClicks(view.input, "hairline crack bollard")
    view.search_now()
    qtbot.waitUntil(lambda: view.results._model.rowCount() > 0, timeout=5000)

    # The volume note arrives with the metadata redraw, a moment after the rows.
    def note_shown() -> bool:
        return bool(getattr(view.results, "_volumes", None))

    qtbot.waitUntil(note_shown, timeout=8000)
    marks = list(view.results._volumes.values())
    assert len(marks) == 1
    note = offline_volume_note(marks[0])
    assert "Projects 2019" in note and "offline" in note.lower()

    payload = view.results._model.index(0, 0).data(ROLE_PAYLOAD)
    assert "quay-wall-inspection" in payload.name.lower()

    # Selecting it shows its text from the index - the drive is not needed.
    view.results._list.setCurrentIndex(view.results._model.index(0, 0))
    toggle = [b for b in window.findChildren(QToolButton) if b.objectName() == "toggle_inspector"][0]
    if not toggle.isChecked():
        qtbot.mouseClick(toggle, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: "quay-wall-inspection" in view.preview.title.text().lower(), timeout=8000)
    # 2026-09-20 (dated note, not an edit of the sentence above): the body IS asserted
    # now. It stayed empty for 15 s because `to_row` never copied `volume_id` onto the
    # row, so the pane read the drive-less synthetic path as an ordinary missing file;
    # fixed in `presenter/results.py`. The index's own text shows with its notice.
    qtbot.waitUntil(lambda: "hairline crack" in view.preview.text.toPlainText().lower(), timeout=8000)
    assert "shown from the index" in view.preview.notice.text().lower()
    if toggle.isChecked():
        qtbot.mouseClick(toggle, Qt.MouseButton.LeftButton)
    view.input.clear()


# ---------------------------------------------------------------------------
# 0k / 0m 1b - Offline Media Scan, Rescan and Delete, each pressed for real
# (the scenario the 2026-09-20 note says was removed unproven: the run hung with
# workers blocked in `SqliteStore._new_connection`)
# ---------------------------------------------------------------------------

def _answer_next_dialog(qtbot, *, type_name: str = "", accept: bool = True) -> None:
    """Arrange for the next modal dialog to be answered the way a person would:
    type a name if asked, then press its OK/Scan (or Cancel) button. Polls, so
    the dialog is answered whenever `exec()` has put it on screen."""
    from PyQt6.QtCore import QTimer
    from PyQt6.QtWidgets import QApplication, QDialog, QDialogButtonBox, QLineEdit

    def poll() -> None:
        dialog = QApplication.activeModalWidget()
        if not isinstance(dialog, QDialog):
            QTimer.singleShot(50, poll)
            return
        if type_name:
            box = dialog.findChild(QLineEdit)
            if box is not None:
                box.setText(type_name)
        buttons = dialog.findChild(QDialogButtonBox)
        which = (QDialogButtonBox.StandardButton.Ok if accept
                 else QDialogButtonBox.StandardButton.Cancel)
        buttons.button(which).click()

    QTimer.singleShot(50, poll)


@pytest.fixture()
def offline_source(gui_mainwindow, tmp_path, monkeypatch):
    """A fixture folder presented to the real Scan path as a drive.

    Faked - and only these: the Windows volume API (a nested folder is
    correctly refused by `identify_root`), the native directory chooser, the
    embedding model, and the machine-wide run-lock mutex (with the real one,
    any other index run on the machine makes the scan fail with
    ERR_INDEX_RUNNING, correctly). The name dialog, the worker, the store, the
    pipeline, the tree and the confirmation dialog are the real ones."""
    import app.index.offline_media as offline_media_module
    from app.index.embedder import Embedder, l2_normalise
    from tests.unit.test_offline_media import _NullRunLock

    guid = r"\?\Volume{00000000-0000-0000-0000-0000000000b7}"
    folder = tmp_path / "usb-drive"
    (folder / "reports").mkdir(parents=True)
    (folder / "reports" / "harbour-survey.txt").write_text(
        "Harbour survey. The quay wall has a hairline crack near bollard nine.",
        encoding="utf-8")

    monkeypatch.setattr(offline_media_module, "identify_source",
                        lambda path: ("drive", {"identity_key": guid, "volume_guid": guid,
                                                "fs_label": "USBDRIVE"}))
    monkeypatch.setattr("app.core.volumes_win.hardware_serial_for_root", lambda *_a, **_k: None)
    monkeypatch.setattr("app.core.volumes_win.find_drive_by_guid",
                        lambda g, *_a, **_k: folder if g == guid else None)
    monkeypatch.setattr("app.core.run_lock.IndexRunLock", _NullRunLock)
    monkeypatch.setattr(
        Embedder, "from_settings",
        classmethod(lambda cls, settings, **kw: Embedder(
            dim=384, encoder=lambda texts: [l2_normalise([1.0] + [0.0] * 383) for _ in texts])))
    monkeypatch.setattr("PyQt6.QtWidgets.QFileDialog.getExistingDirectory",
                        staticmethod(lambda *_a, **_k: str(folder)))
    return folder


def _row_named(view, name: str):
    for index in range(view.tree.topLevelItemCount()):
        item = view.tree.topLevelItem(index)
        if item.text(0) == name:
            return item
    return None


# 2026-10-05: Rescan needs the source to read as plugged in, and telling that
# is Windows-only today (`core/volumes_win.py`). On macOS the scan worked and
# Rescan never became available - a gap in Offline, not in this test.
@pytest.mark.windows
def test_offline_media_scan_rescan_and_delete_pressed_for_real(gui_mainwindow, offline_source, qtbot):
    from PyQt6.QtWidgets import QToolButton

    app, window, store, _engine = gui_mainwindow
    view = window.offline_media_view
    name = "USB drive 2019"

    # -- Scan: press the button, name the source in the real dialog -----------
    _answer_next_dialog(qtbot, type_name=name)
    qtbot.mouseClick(view.scan, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: _row_named(view, name) is not None, timeout=60_000)
    assert view.status_line.text() == ""                   # no longer "Scanning..."
    assert view.scan.isEnabled()

    volume = next(v for v in store.list_volumes() if v["name"] == name)
    files = list(store.iter_files(volume_id=int(volume["id"]), source_kind="file"))
    assert len(files) == 1 and files[0].path.endswith("harbour-survey.txt")

    # -- Rescan: pick the row, press Rescan; a new file on the drive appears --
    (offline_source / "reports" / "second-note.txt").write_text(
        "Second note about the harbour crane.", encoding="utf-8")
    _row_named(view, name).setSelected(True)
    qtbot.waitUntil(view.rescan.isEnabled, timeout=5000)
    qtbot.mouseClick(view.rescan, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: len(list(store.iter_files(
        volume_id=int(volume["id"]), source_kind="file"))) == 2, timeout=60_000)
    qtbot.waitUntil(view.scan.isEnabled, timeout=10_000)

    # -- the indexed text of an offline file shows in the preview body --------
    search = window.search_view
    search.input.clear()
    gui_pump(app)
    qtbot.keyClicks(search.input, "hairline crack bollard")
    search.search_now()
    qtbot.waitUntil(lambda: search.results._model.rowCount() > 0, timeout=8000)
    search.results._list.setCurrentIndex(search.results._model.index(0, 0))
    toggle = [b for b in window.findChildren(QToolButton) if b.objectName() == "toggle_inspector"][0]
    if not toggle.isChecked():
        qtbot.mouseClick(toggle, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: "hairline crack" in search.preview.text.toPlainText().lower(), timeout=8000)
    if toggle.isChecked():
        qtbot.mouseClick(toggle, Qt.MouseButton.LeftButton)
    search.input.clear()

    # -- Delete: press it, confirm in the real dialog -------------------------
    _row_named(view, name).setSelected(True)
    qtbot.waitUntil(view.delete.isEnabled, timeout=5000)
    _answer_next_dialog(qtbot)
    qtbot.mouseClick(view.delete, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: _row_named(view, name) is None, timeout=30_000)
    assert not [v for v in store.list_volumes() if v["name"] == name]
    assert (offline_source / "reports" / "harbour-survey.txt").exists()   # the drive is never touched
