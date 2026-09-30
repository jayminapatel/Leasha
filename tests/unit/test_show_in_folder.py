r""""Show in folder" in the preview pane: only offered when there is a folder.

Layer: L5

**What was wrong (2026-09-30).** The pane enabled the button for any row with
a path. A message inside a mail archive has a made-up address for one -
`pst://archive/E12` - so the button was on for something no file manager can
show. The pane now offers it only for a row that stands for a real file, and
for a message out of an archive once the read says which archive: the button
then shows **the archive file itself**, which is what "where is this" means
for a message.

**Found on the way.** In the Files, Mail and Code tabs the button was enabled
and connected to nothing: `attach_preview` wired it to the list's own
`reveal_requested`, and only the Search list has one. It now works there too.

Nothing here opens a file manager: `open_in_explorer` is replaced throughout.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("PyQt6.QtWidgets", exc_type=ImportError)

from PyQt6.QtCore import pyqtSignal                             # noqa: E402
from PyQt6.QtWidgets import QWidget                             # noqa: E402

from app.ui.presenter.rows import MailRow, file_of_row          # noqa: E402
from tests.unit.test_mail_preview_card import (                 # noqa: E402,F401
    TUESDAY, add_message, preview_now, pump, store,
)


@pytest.fixture()
def shown(monkeypatch) -> list:
    """Every `(path, select)` the window would have handed to the file manager."""
    calls: list = []

    def fake(path, *, select=True):
        calls.append((str(path), select))
        return None

    monkeypatch.setattr("app.ui.workers.open_in_explorer", fake)
    return calls


# --- the rule, without Qt -----------------------------------------------------

def test_a_message_inside_an_archive_is_not_a_file() -> None:
    assert file_of_row(SimpleNamespace(path="pst://Archive/E1")) == ""
    assert file_of_row(SimpleNamespace(path="pst://Archive/E1/attachments/form.pdf")) == ""


def test_an_ordinary_row_is_its_own_file() -> None:
    assert file_of_row(SimpleNamespace(path="C:/docs/report.txt")) == "C:/docs/report.txt"
    assert file_of_row(SimpleNamespace(path="C:/mail/note.eml")) == "C:/mail/note.eml"
    assert file_of_row(SimpleNamespace(path="")) == ""
    assert file_of_row(None) == ""


def test_a_code_row_is_the_file_it_opens_not_the_shortened_column() -> None:
    checkout = SimpleNamespace(path="app/cli.py", full_path="D:/repo/app/cli.py")
    history = SimpleNamespace(path="app/cli.py:12", full_path="")
    assert file_of_row(checkout) == "D:/repo/app/cli.py"
    assert file_of_row(history) == "", "a commit from history has no file on disk"


# --- the pane -----------------------------------------------------------------

def _mail_row(path: str) -> MailRow:
    return MailRow(file_id=1, sender="Dave", recipients="", sent="", subject="Trip",
                   attachment="", size="", path=path, name="Trip")


def test_the_button_is_off_for_a_message_whose_archive_is_not_known(
        _qt_application, shown) -> None:
    from app.ui.widgets.preview import PreviewPane

    pane = PreviewPane()
    pane.show_row(_mail_row("pst://Archive/E1"))
    pane._timer.stop()

    assert not pane.reveal_button.isEnabled()
    assert pane.open_button.isEnabled() and pane.pop_button.isEnabled()
    pane.reveal_button.click()
    pump()
    assert shown == []
    pane.close()


def test_the_button_is_on_for_a_real_file(_qt_application) -> None:
    from app.ui.widgets.preview import PreviewPane

    pane = PreviewPane()
    pane.show_row(SimpleNamespace(path="C:/docs/report.txt", name="report.txt"))
    pane._timer.stop()
    assert pane.reveal_button.isEnabled()
    pane.close()


# --- the Mail tab, end to end -------------------------------------------------

def _mail_view(store):
    from app.ui.mail_view import MailView

    view = MailView(store)
    view._timer.stop()
    pump()
    return view


def test_a_message_from_an_archive_shows_the_archive_file(
        _qt_application, store, shown) -> None:
    add_message(store, path="pst://Archive/E1", subject="School trip",
                sender="dave@acme.com", sent_at=TUESDAY, body="The trip is on Friday.",
                entry_id="E1", store_path="C:/mail/Archive.pst")
    view = _mail_view(store)
    try:
        preview_now(view.preview, view._rows[0])
        pane = view.preview

        assert pane.reveal_button.isEnabled()
        pane.reveal_button.click()
        pump()

        assert shown == [("C:/mail/Archive.pst", True)], (
            "the archive itself, selected in its folder - not the made-up address")
        # "Open in Outlook" is its own button and is as it was.
        assert not pane.original_button.isHidden()
    finally:
        view.shutdown()


def test_a_message_whose_archive_the_index_did_not_record_has_no_folder_to_show(
        _qt_application, store, shown) -> None:
    add_message(store, path="pst://Archive/E1", subject="School trip",
                sender="dave@acme.com", sent_at=TUESDAY, body="The trip is on Friday.")
    view = _mail_view(store)
    try:
        preview_now(view.preview, view._rows[0])

        assert not view.preview.reveal_button.isEnabled()
        view.preview.reveal_button.click()
        pump()
        assert shown == []
    finally:
        view.shutdown()


def test_the_archive_of_one_message_is_not_offered_for_the_next(
        _qt_application, store, shown) -> None:
    add_message(store, path="pst://Archive/E1", subject="Known", sender="a@x.org",
                sent_at=TUESDAY + 60, body="One.", entry_id="E1",
                store_path="C:/mail/Archive.pst")
    add_message(store, path="pst://Other/E2", subject="Unknown", sender="b@x.org",
                sent_at=TUESDAY, body="Two.")
    view = _mail_view(store)
    try:
        known = next(row for row in view._rows if row.subject == "Known")
        unknown = next(row for row in view._rows if row.subject == "Unknown")
        preview_now(view.preview, known)
        assert view.preview.reveal_button.isEnabled()

        view.preview.show_row(unknown)
        assert not view.preview.reveal_button.isEnabled(), "the last message's archive"
        view.preview._timer.stop()
        view.preview._start()
        pump()
        assert not view.preview.reveal_button.isEnabled()
        view.preview.reveal_button.click()
        pump()
        assert shown == []
    finally:
        view.shutdown()


def test_a_message_that_is_a_file_shows_that_file(_qt_application, store, shown) -> None:
    add_message(store, path="C:/mail/note.eml", subject="A note", sender="dave@acme.com",
                sent_at=TUESDAY, body="Hello.", source_kind="file")
    view = _mail_view(store)
    try:
        preview_now(view.preview, view._rows[0])
        assert view.preview.reveal_button.isEnabled()
        view.preview.reveal_button.click()
        pump()
        assert shown == [("C:/mail/note.eml", True)]
    finally:
        view.shutdown()


# --- the tabs where the button was connected to nothing ----------------------

def test_the_files_tab_shows_the_selected_file_in_its_folder(
        _qt_application, store, shown) -> None:
    from app.ui.files_view import FilesView

    done = store.upsert_file("C:/corpus/report.txt", size_bytes=10, mtime_ns=1)
    store.mark_indexed(done)
    view = FilesView(store)
    try:
        pump()
        view.results.selectRow(0)
        view.preview.setVisible(True)
        view.preview.show_row(view.results.current_row())
        view.preview._timer.stop()

        assert view.preview.reveal_button.isEnabled()
        view.preview.reveal_button.click()
        pump()

        assert shown == [("C:/corpus/report.txt", True)]
    finally:
        view.shutdown()


def test_the_code_tab_shows_the_file_and_offers_nothing_for_history(
        _qt_application, shown) -> None:
    from app.ui.widgets.code_results import CodeResults

    results = CodeResults()
    try:
        pane = results.preview
        pane.show_row(SimpleNamespace(name="cli.py", path="app/cli.py",
                                      full_path="D:/repo/app/cli.py"))
        pane._timer.stop()
        assert pane.reveal_button.isEnabled()
        pane.reveal_button.click()
        pump()
        assert shown == [("D:/repo/app/cli.py", True)], "the real path, not the column's"

        pane.show_row(SimpleNamespace(name="cli.py", path="app/cli.py:12", full_path=""))
        pane._timer.stop()
        assert not pane.reveal_button.isEnabled(), "a commit from history has no file"
    finally:
        results.shutdown()


# --- the pinned window --------------------------------------------------------

def test_a_pinned_message_from_an_archive_offers_no_folder(_qt_application, tmp_path) -> None:
    """The pinned window is handed the row and nothing about its archive, so
    for a message with no file of its own the button is off rather than
    asking the file manager for an address."""
    from app.ui.widgets.preview_window import PreviewWindow

    message = PreviewWindow(_mail_row("pst://Archive/E1"), state={},
                            body_provider=lambda _row: "The trip is on Friday.")
    assert not message.reveal_button.isEnabled()
    message.close()

    real = tmp_path / "note.txt"
    real.write_text("hello", encoding="utf-8")
    document = PreviewWindow(SimpleNamespace(path=str(real), name="note.txt", page=0),
                             state={})
    assert document.reveal_button.isEnabled()
    document.close()


# --- a list with a route of its own keeps it (the Search tab) ----------------

class _List(QWidget):
    selected = pyqtSignal(object)
    reveal_requested = pyqtSignal(object)


def test_a_list_with_its_own_route_is_still_asked_for_an_ordinary_file(
        _qt_application, shown) -> None:
    from app.ui.widgets.preview import attach_preview

    results = _List()
    asked: list = []
    results.reveal_requested.connect(asked.append)
    pane, split = attach_preview(results, lambda _row: None, lambda _error: None)
    row = SimpleNamespace(path="C:/docs/report.txt", name="report.txt")
    pane.show_row(row)
    pane._timer.stop()

    pane.reveal_button.click()
    pump()

    assert asked == [row], "the window's own route, as before"
    assert shown == [], "and not a second time from the pane"
    split.close()


def test_in_the_search_list_a_message_shows_its_archive_not_its_address(
        _qt_application, store, shown) -> None:
    from app.ui.widgets.preview import attach_preview

    file_id = add_message(store, path="pst://Archive/E1", subject="School trip",
                          sender="dave@acme.com", sent_at=TUESDAY, body="Friday.",
                          entry_id="E1", store_path="C:/mail/Archive.pst")
    results = _List()
    asked: list = []
    results.reveal_requested.connect(asked.append)
    pane, split = attach_preview(results, lambda _row: None, lambda _error: None,
                                 store=store)
    # A Search row: it has the message's address and id, and no archive on it.
    row = SimpleNamespace(path="pst://Archive/E1", name="School trip", file_id=file_id)
    preview_now(pane, row)

    assert pane.reveal_button.isEnabled()
    pane.reveal_button.click()
    pump()

    assert shown == [("C:/mail/Archive.pst", True)]
    assert asked == [], "the list would have been handed an address that is no file"
    split.close()
