"""The UI audit of 2026-10-12: settings that pinned themselves, and questions Enter could answer.

Layer: L5, offscreen. The owner asked for the recommended fixes "to make it world class and
friendly". Each fault the audit found is held here by a test that failed before its fix.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QMessageBox, QPushButton  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture()
def answer(monkeypatch):
    """Every `QMessageBox` built and `exec`ed is recorded; `press` names the button to click.

    `exec` is replaced so no dialog ever waits for a person. A box nobody clicks returns
    with no clicked button, which is exactly what Escape or the close box leave behind.
    """
    seen: list = []
    state = SimpleNamespace(press=None, seen=seen)

    def fake_exec(box):
        seen.append(box)
        if state.press:
            for button in box.buttons():
                if button.text() == state.press:
                    button.click()
        return 0

    monkeypatch.setattr(QMessageBox, "exec", fake_exec)
    return state


# --- the shared confirmation -------------------------------------------------------

def test_a_destructive_question_defaults_to_cancel_and_names_its_verb(app, answer):
    from app.ui.widgets.confirm import confirm

    assert confirm(None, "Delete a model", "Delete it?", "Delete") is False
    box = answer.seen[-1]
    assert box.defaultButton().text() == "Cancel"
    assert box.escapeButton().text() == "Cancel"
    assert box.icon() == QMessageBox.Icon.Warning
    assert sorted(b.text() for b in box.buttons()) == ["Cancel", "Delete"]
    answer.press = "Delete"
    assert confirm(None, "Delete a model", "Delete it?", "Delete") is True


# --- 1. the wizard's ticks follow the kind of account --------------------------------

def test_going_back_from_imap_to_google_ticks_contacts_again(app):
    from app.ui.accounts.wizard import AddAccountDialog

    dlg = AddAccountDialog(backend=None)
    try:
        dlg.choices["imap"].setChecked(True)
        dlg.next.click()
        assert not dlg.contacts.isChecked() and dlg.mail.isChecked()
        dlg.back.click()
        dlg.choices["google"].setChecked(True)
        dlg.next.click()
        assert dlg.contacts.isChecked(), "Google's contacts stayed unticked from IMAP"
        assert not dlg.mail.isChecked()
        # Back and Next on the same kind keeps what the person ticked.
        dlg.mail.setChecked(True)
        dlg.back.click()
        dlg.next.click()
        assert dlg.mail.isChecked()
    finally:
        dlg.deleteLater()


# --- 2. search behaviour writes one key, and its reset unpins ------------------------

def _behaviour_box():
    from app.ui.widgets.search_behaviour_box import SearchBehaviourBox

    box = SearchBehaviourBox(SimpleNamespace())
    sent: list = []
    box.changed.connect(sent.append)
    return box, sent


def test_one_switch_writes_one_key(app):
    box, sent = _behaviour_box()
    box.controls["auto_chips"].setChecked(False)
    assert sent == [{"SEARCH_AUTO_CHIPS": False}], sent
    box.kinds_shown.setValue(20)
    assert sent[-1] == {"FILE_KINDS_SHOWN": 20}
    box.deleteLater()


def test_the_reset_removes_the_lines_rather_than_writing_the_defaults(app):
    box, sent = _behaviour_box()
    box.controls["recency_blend"].setChecked(False)
    box.offer_recent.setChecked(False)
    sent.clear()
    box.restore_defaults()
    assert len(sent) == 1
    assert set(sent[0].values()) == {None}, sent[0]
    assert {"SEARCH_RECENCY_BLEND", "SEARCH_OFFER_RECENT", "SEARCH_FIX_SPELLING"} <= set(sent[0])
    assert box.controls["recency_blend"].isChecked() and box.offer_recent.isChecked()
    assert "six" not in box.reset.toolTip()
    # The seven behaviours and "Offer what you searched for before": what the button
    # puts back on screen is what it unpins, and the tooltip counts them.
    assert len(sent[0]) == 8 and "all eight" in box.reset.toolTip()
    box.deleteLater()


def test_a_removed_line_applies_its_default_live_not_off(app):
    """`None` is "use the default" in `.env`, and was read as *off* by the live overrides:
    restoring "Offer what you searched for before" switched it off until the next start."""
    from app.ui.controllers.settings_controller import _apply_written_settings

    told: list = []
    window = SimpleNamespace(_settings_overrides={}, _apply_search_preferences=lambda: None,
                             _apply_hotkey=lambda: None, _engine=None, notify=told.append)
    _apply_written_settings(window, {"SEARCH_OFFER_RECENT": None, "MINI_SEARCH_ENABLED": None})
    assert window._settings_overrides["search_offer_recent"] is True
    assert window._settings_overrides["mini_search_enabled"] is True


# --- 3. permanent deletions ask with Cancel first ------------------------------------

def test_deleting_a_conversation_names_the_verb(app, answer):
    from app.ui.widgets.chat_session_list import SessionList

    sessions = SessionList()
    try:
        assert sessions.confirm_delete("Rent") is False
        box = answer.seen[-1]
        assert box.defaultButton().text() == "Cancel"
        assert "Delete" in [b.text() for b in box.buttons()]
    finally:
        sessions.deleteLater()


# --- 4. Enter on the remove-account dialog cancels -----------------------------------

def test_the_remove_account_dialog_defaults_to_cancel(app):
    from app.ui.accounts.panel import _RemoveDialog

    dlg = _RemoveDialog(SimpleNamespace(name="Work"))
    try:
        buttons = {b.text(): b for b in dlg.findChildren(QPushButton)}
        assert buttons["Cancel"].isDefault()
        assert not buttons["Remove account"].isDefault()
        assert not buttons["Remove account"].autoDefault()
    finally:
        dlg.deleteLater()


# --- 5. a typed number is written once, not per keystroke ----------------------------

def test_the_cloud_download_limit_waits_for_the_whole_number(app):
    from app.ui.widgets.storage_box import StorageBox

    box = StorageBox(SimpleNamespace(cloud_content_cap_mb=1024))
    try:
        assert box.cloud_content_cap.keyboardTracking() is False
    finally:
        box.deleteLater()


# --- 6. Restore defaults asks, and is current afterwards -----------------------------

def test_restore_defaults_asks_and_then_says_nothing_is_pinned(app, answer, tmp_path):
    from PySide6.QtWidgets import QCheckBox, QWidget

    from app.ui.widgets.defaults import restore_button

    env = tmp_path / ".env"
    env.write_text("RERANK_ENABLED=true\nRERANK_TOP_N=50\nDATA_PATH=D:/x\n", encoding="utf-8")
    panel = QWidget()
    rerank = QCheckBox("Rerank results", panel)
    rerank.setObjectName("RERANK_ENABLED")
    rerank.setChecked(True)
    sent: list = []
    button = restore_button(panel, env, sent.append)
    try:
        assert "2 settings" in button.toolTip()
        button.click()                              # Cancel: nothing written
        assert sent == []
        box = answer.seen[-1]
        assert box.defaultButton().text() == "Cancel"
        assert "2 settings" in box.text()
        answer.press = "Restore defaults"
        button.click()
        assert sent == [{"RERANK_ENABLED": None, "RERANK_TOP_N": None}]
        assert not button.isEnabled() and "Nothing is pinned" in button.toolTip()
        assert rerank.isChecked() is False, "the control still showed the pinned value"
        # A setting written afterwards pins again, and the button says so.
        button.note_written({"RERANK_TOP_N": 40})
        assert button.isEnabled() and "1 setting " in button.toolTip()
    finally:
        panel.deleteLater()


def test_a_location_offers_no_reset_and_is_never_blanked(app):
    """`env_writer` refuses to remove a location, so a right-click "Reset" on the index
    folder did nothing to `.env` - and showing its default would blank the field."""
    from PySide6.QtWidgets import QLineEdit, QWidget

    from app.ui.widgets.defaults import attach_resets, show_default

    panel = QWidget()
    where = QLineEdit("D:/LeashaIndex/Data", panel)
    where.setObjectName("DATA_PATH")
    try:
        assert attach_resets(panel, lambda values: None) == 0
        assert show_default(panel, ["DATA_PATH"]) == 0
        assert where.text() == "D:/LeashaIndex/Data"
    finally:
        panel.deleteLater()


# --- 7. the file type dialog says what OK does ---------------------------------------

def test_the_file_type_note_says_save_keeps_it(app):
    from PySide6.QtWidgets import QLabel

    from app.ui.widgets.file_types import EditFileTypeDialog

    dlg = EditFileTypeDialog(".pdf", "pdf", ["pdf"], 50 * 2 ** 20, True)
    try:
        words = " ".join(label.text() for label in dlg.findChildren(QLabel))
        assert "Press Save file types to keep it" in words
        assert "it applies from the next index run" in words
    finally:
        dlg.deleteLater()


def test_resetting_file_types_asks_with_cancel_first(app, answer, monkeypatch):
    from app.ui.widgets import file_types

    asked: list = []
    monkeypatch.setattr(file_types, "confirm",
                        lambda *a, **k: asked.append((a, k)) or False)
    editor = SimpleNamespace(_added=set())
    file_types.FileTypesEditor.reset_to_defaults(editor)
    assert asked and asked[0][0][3] == "Reset file types"


# --- 8. Tidy's question names the Recycle Bin on its button --------------------------

def test_tidys_question_says_move_to_recycle_bin(app, answer):
    from app.ui.tidy_view import TidyView

    assert TidyView.ask(None, "Move to the Recycle Bin", "Move 3 files?") is False
    box = answer.seen[-1]
    assert box.defaultButton().text() == "Cancel"
    assert "Move to Recycle Bin" in [b.text() for b in box.buttons()]


# --- 9. Export says where the report went --------------------------------------------

def test_export_says_the_file_it_saved(app, monkeypatch, tmp_path, qtbot):
    from app.ui import reports_view
    from PySide6.QtWidgets import QFileDialog

    target = tmp_path / "space-report.pdf"
    monkeypatch.setattr(QFileDialog, "getSaveFileName",
                        staticmethod(lambda *a, **k: (str(target), "")))
    monkeypatch.setattr(reports_view, "_write_pdf",
                        lambda document, path: target.write_bytes(b"%PDF"))
    view = reports_view.ReportsView(None)
    told: list = []
    view.notice.connect(told.append)
    view._space_document = "# Space"
    view._start_export()
    qtbot.waitUntil(lambda: bool(told), timeout=5_000)
    assert "space-report.pdf" in told[0]
    view.deleteLater()


# --- 10. the Delete button's dialog says Delete --------------------------------------

def test_the_delete_dialog_agrees_with_the_delete_button(app):
    from PySide6.QtWidgets import QDialogButtonBox

    from app.ui.widgets.offline_media_dialogs import DeleteVolumeDialog

    dlg = DeleteVolumeDialog("Holiday disk", 12)
    try:
        assert dlg.buttons.button(QDialogButtonBox.StandardButton.Ok).text() == "Delete"
        assert "Delete" in dlg.windowTitle() and "catalogue" in dlg.windowTitle()
        assert "Forget" not in dlg.windowTitle()
        assert dlg.buttons.button(QDialogButtonBox.StandardButton.Cancel).isDefault()
    finally:
        dlg.deleteLater()
