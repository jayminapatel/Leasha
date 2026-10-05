r"""`open_row_async` - the one open route, for every page. 2026-10-04.

Layer: L5

Offline Media 1b/3a/3c's resolve-then-open was proved first by
`shell._open_volume_result` and copied into `open_row_async` for the Files
page. The owner (2026-10-04): "where ever possible the same code should run for
functions so they are all consistent and standard" - so the copy is gone and
every page's Open and Show in folder is this one route, taking the ROW. These
tests drive it with stand-ins for everything that would start a program:
`open_in_explorer`, the media player, the editor and Outlook are replaced, so
nothing here can open anything on the machine running the suite.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("PySide6")


def _pump(ms: int = 3_000) -> None:
    from PySide6.QtCore import QThreadPool
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    QThreadPool.globalInstance().waitForDone(ms)
    for _ in range(5):
        app.processEvents()


@pytest.fixture()
def launched(monkeypatch):
    """Every launcher the route can reach, recorded instead of run - and the
    window's context put back afterwards, whatever a test set."""
    from app.ui import workers
    from app.ui.widgets import mail_open

    seen: list = []
    monkeypatch.setattr(workers, "_CONTEXT", workers.OpenContext())
    monkeypatch.setattr(workers, "open_in_explorer",
                        lambda path, *, select=True: seen.append(("explorer", path, select)))
    monkeypatch.setattr(workers, "open_media_at",
                        lambda path, seconds: seen.append(("media", path, seconds)))
    monkeypatch.setattr(workers, "open_at_line",
                        lambda path, line, **k: seen.append(("editor", path, line, k["choice"])))
    real = mail_open.open_original
    monkeypatch.setattr(mail_open, "open_original", lambda target, **k: real(
        target, outlook=lambda entry, store: seen.append(("outlook", entry, store)),
        open_file=lambda path: seen.append(("message file", path))))
    return seen


def test_none_row_does_nothing(launched):
    from app.ui import workers

    workers.open_row_async(store=None, row=None)
    workers.open_row_async(None, "")
    _pump()
    assert launched == []


def test_an_ordinary_row_opens_its_file(launched):
    from app.ui import workers

    workers.open_row_async(None, SimpleNamespace(path=r"D:\a\report.pdf", volume_id=None))
    workers.open_row_async(None, SimpleNamespace(path=r"D:\a\report.pdf"), reveal=True)
    _pump()
    assert sorted(launched) == [("explorer", r"D:\a\report.pdf", False),     # two workers:
                                ("explorer", r"D:\a\report.pdf", True)]      # either order


def test_a_volume_row_resolves_before_opening(launched, monkeypatch):
    """A catalogued-volume row never reaches Explorer with its synthetic key."""
    from app.ui import tasks, workers

    resolved = []
    monkeypatch.setattr(tasks, "resolve_open_path",
                        lambda store, row: resolved.append(row) or r"E:\reports\q3.txt")
    row = SimpleNamespace(path="leasha-volume://1/reports/q3.txt", volume_id=1,
                          relative_path="reports/q3.txt")
    errors: list = []
    workers.open_row_async(object(), row, reveal=True, on_error=errors.append)
    _pump()
    assert resolved == [row]
    assert launched == [("explorer", r"E:\reports\q3.txt", True)] and errors == []


def test_a_catalogued_drives_key_alone_resolves_too(launched, monkeypatch):
    """A pin or a chip keeps only the key; the drive and path are read off it."""
    from app.ui import tasks, workers

    resolved = []
    monkeypatch.setattr(tasks, "resolve_open_path",
                        lambda store, row: resolved.append(row) or r"E:\reports\q3.txt")
    workers.open_row_async(object(), "leasha-volume://1/reports/q3.txt")
    _pump()
    assert [(r.volume_id, r.relative_path) for r in resolved] == [(1, "reports/q3.txt")]
    assert launched == [("explorer", r"E:\reports\q3.txt", False)]


def test_a_volume_row_that_cannot_resolve_reports_the_error(launched, monkeypatch):
    from app.core.errors import AppErrorException, make_error
    from app.ui import tasks, workers

    def offline(store, row):
        raise AppErrorException(make_error("ERR_FILE_CORRUPT", "ui.open",
                                           suggestion="Plug it in and try again."))

    monkeypatch.setattr(tasks, "resolve_open_path", offline)
    errors: list = []
    workers.open_row_async(object(), SimpleNamespace(
        path="leasha-volume://1/q3.txt", volume_id=1), on_error=errors.append)
    _pump()
    assert [e.code for e in errors] == ["ERR_FILE_CORRUPT"] and launched == []


def test_a_recording_on_a_catalogued_drive_opens_at_its_moment(launched, monkeypatch):
    """Finding 5: the drive was resolved and the moment dropped. Now both."""
    from app.ui import tasks, workers

    monkeypatch.setattr(tasks, "resolve_open_path", lambda store, row: r"E:\v\talk.mp4")
    workers.open_row_async(object(), SimpleNamespace(
        path="leasha-volume://1/v/talk.mp4", volume_id=1, ext="mp4", label="12:41"))
    _pump()
    assert launched == [("media", r"E:\v\talk.mp4", 761)]


def test_a_code_row_opens_at_its_line_with_the_editor_in_force(launched):
    from app.ui import workers
    from app.ui.presenter.opening import Place

    workers.set_open_context(workers.OpenContext(editor=lambda: ("notepadpp", "")))
    workers.open_row_async(None, Place(r"D:\repo\x.py", 9))
    workers.open_row_async(None, SimpleNamespace(
        path="…\\x.py", full_path=r"D:\repo\x.py", line_no=4))      # a Code row
    _pump()
    assert sorted(launched) == [("editor", r"D:\repo\x.py", 4, "notepadpp"),
                                ("editor", r"D:\repo\x.py", 9, "notepadpp")]


def test_a_code_hit_in_search_opens_at_the_line_its_passage_starts(launched, tmp_path):
    """Finding 7: a Search, Chat or mini-search hit on a code file carries
    `char_start` in the indexed text - which has lost its blank-line runs -
    and goes to the editor at that line of the file."""
    from app.extract.base import normalise_whitespace
    from app.ui import workers

    source = tmp_path / "x.py"
    text = "\n\nimport os\n\n\n\ndef main():\n    return 1\n"
    source.write_text(text, encoding="utf-8")
    start = normalise_whitespace(text).index("def main")
    workers.open_row_async(None, SimpleNamespace(
        path=str(source), ext="py", char_start=start, chunk_id=3, file_id=1))
    _pump()
    assert launched == [("editor", str(source), 7, "auto")]


def test_an_attachment_and_a_zip_member_open_from_a_copy_with_a_note(
        launched, monkeypatch, tmp_path):
    from app.ui import tasks, workers

    copy = tmp_path / "report.docx"
    monkeypatch.setattr(tasks, "save_attachment_copy", lambda store, path, cache: copy)
    notes: list = []
    workers.open_row_async(object(), r"D:\Docs\backup.zip/q3/report.docx", on_note=notes.append)
    _pump()
    assert launched == [("explorer", str(copy), False)]
    assert notes == ["Opened a copy of 'report.docx' from the zip. "
                     "Changes to it are not saved back to the zip."]


def test_show_in_folder_on_a_zip_member_shows_the_zip(launched):
    from app.ui import workers

    workers.open_row_async(None, r"D:\Docs\backup.zip/q3/report.docx", reveal=True)
    _pump()
    assert launched == [("explorer", r"D:\Docs\backup.zip", True)]


class _MailStore:
    """A message `pst://s/1` in `D:\\mail\\archive.pst`, and one with no identifier."""

    def __init__(self):
        self.messages = {1: {"file_id": 1, "entry_id": "E1", "store_path": r"D:\mail\archive.pst"},
                         2: {"file_id": 2, "entry_id": "", "store_path": r"D:\mail\archive.pst"}}

    def get_file(self, path):
        number = {"pst://s/1": 1, "pst://s/2": 2}.get(path)
        return SimpleNamespace(id=number) if number else None

    def get_message(self, file_id):
        return self.messages.get(file_id)


def test_open_on_a_message_opens_it_in_outlook_from_any_page(launched):
    """The owner's decision (2026-10-04): the same as the preview's button -
    by the row's file id, or by the key alone (a chip, a pinned window)."""
    from app.ui import workers

    store = _MailStore()
    workers.open_row_async(store, SimpleNamespace(path="pst://s/1", file_id=1))
    workers.open_row_async(store, "pst://s/1")
    _pump()
    assert launched == [("outlook", "E1", r"D:\mail\archive.pst")] * 2


def test_a_message_outlook_cannot_show_is_searched_inside(launched):
    from app.ui import workers

    inside: list = []
    workers.open_row_async(_MailStore(), SimpleNamespace(path="pst://s/2", file_id=2),
                           search_inside=inside.append)
    workers.open_row_async(None, "pst://s/9", search_inside=inside.append)   # no store at all
    _pump()
    assert sorted(inside) == ["pst://s/2", "pst://s/9"] and launched == []


def test_show_in_folder_on_a_message_shows_its_archive(launched):
    from app.ui import workers

    workers.open_row_async(_MailStore(), SimpleNamespace(path="pst://s/1", file_id=1),
                           reveal=True)
    _pump()
    assert launched == [("explorer", r"D:\mail\archive.pst", True)]


def test_an_eml_file_on_disk_still_opens_as_a_file(launched):
    from app.ui import workers

    store = SimpleNamespace(get_message=lambda file_id: {"file_id": 5, "entry_id": "",
                                                         "store_path": ""},
                            get_file=lambda path: None)
    workers.open_row_async(store, SimpleNamespace(path=r"D:\mail\note.eml", file_id=5))
    _pump()
    assert launched == [("message file", r"D:\mail\note.eml")]


def test_every_open_of_an_indexed_result_is_recorded_once_off_the_ui_thread(launched):
    """Finding 6: from any page, with the search on screen's id - and a Show in
    folder, a failed open or a row with no passage is not an open."""
    import threading

    from app.ui import workers

    # Dated note, 2026-10-04, code review: the search id is the ROW's own now
    # (`ResultRow.search_id`), not one the window lends - see the test below.
    recorded: list = []
    engine = SimpleNamespace(record_open=lambda search_id, chunk_id: recorded.append(
        (search_id, chunk_id, threading.current_thread() is threading.main_thread())))
    workers.set_open_context(workers.OpenContext(engine=engine))
    workers.open_row_async(None, SimpleNamespace(path=r"D:\a.txt", chunk_id=7, search_id=41))
    workers.open_row_async(None, SimpleNamespace(path=r"D:\a.txt", chunk_id=7, search_id=41),
                           reveal=True)
    workers.open_row_async(None, SimpleNamespace(path=r"D:\b.txt", chunk_id=0, search_id=41))
    _pump()
    assert recorded == [(41, 7, False)]


def test_an_open_is_credited_to_the_rows_own_search_and_never_to_another(launched):
    """2026-10-04, code review: every open was credited to the Search tab's
    current search - a Chat source, a mini-search hit, a window pinned from an
    earlier search. A row carries the search it came from; one with none
    records nothing."""
    from app.ui import workers

    recorded: list = []
    engine = SimpleNamespace(record_open=lambda search_id, chunk_id: recorded.append(
        (search_id, chunk_id)))
    workers.set_open_context(workers.OpenContext(engine=engine))
    assert not hasattr(workers.OpenContext(), "search_id"), "no window-wide search id"
    workers.open_row_async(None, SimpleNamespace(path=r"D:\a.txt", chunk_id=7, search_id=12))
    workers.open_row_async(None, SimpleNamespace(path=r"D:\c.txt", chunk_id=8))   # Chat
    _pump()
    assert recorded == [(12, 7)]


def test_the_search_list_stamps_its_search_on_every_row(qtbot):
    from app.ui.results_view import ResultsView

    view = ResultsView()
    qtbot.addWidget(view)
    hit = SimpleNamespace(chunk_id=3, file_id=1, path=r"D:\a.txt", text="pump", score=1.0,
                          ext="txt", rank=1)
    view.show_results([hit], ["pump"], search_id=55)
    assert [row.search_id for row in view._rows] == [55]
    view.show_results([hit], ["pump"])
    assert [row.search_id for row in view._rows] == [None]


class _BoxStore:
    """A message read out of an mbox and one out of an `.olm`, by key."""

    keys = {r"D:\mail\home.mbox/123": 11,
            r"D:\mail\work.olm/Accounts/x/Inbox/message_00001.xml": 12}

    def get_file(self, path):
        number = self.keys.get(path)
        return SimpleNamespace(id=number) if number else None

    def get_message(self, file_id):
        return {"file_id": file_id, "entry_id": "", "store_path": ""} if file_id else None


@pytest.mark.parametrize(("key", "archive"), [
    (r"D:\mail\home.mbox/123", r"D:\mail\home.mbox"),
    (r"D:\mail\work.olm/Accounts/x/Inbox/message_00001.xml", r"D:\mail\work.olm"),
])
def test_show_in_folder_on_an_mbox_or_olm_message_shows_the_archive_file(
        launched, key, archive):
    """2026-10-04, code review: only `pst://` was a message to the route; an
    mbox or `.olm` message was revealed as its key and failed "moved or
    deleted". From a row that says it is mail, and from the key alone."""
    from app.ui import workers

    workers.open_row_async(None, SimpleNamespace(path=key, file_id=11, source_kind="eml"),
                           reveal=True)
    workers.open_row_async(None, key, reveal=True)
    _pump()
    assert launched == [("explorer", archive, True)] * 2


def test_open_on_an_mbox_message_given_only_its_key_is_searched_inside(launched):
    """`Place` has no file id: `_message_of` looked a key up only when it had
    `://`, so the key went to Explorer. Now it is found, and - nothing being
    able to show an mbox message - searched inside, as from its row."""
    from app.ui import workers

    inside: list = []
    workers.open_row_async(_BoxStore(), r"D:\mail\home.mbox/123", search_inside=inside.append)
    workers.open_row_async(_BoxStore(), SimpleNamespace(
        path=r"D:\mail\work.olm/Accounts/x/Inbox/message_00001.xml", file_id=12),
        search_inside=inside.append)
    _pump()
    assert sorted(inside) == [r"D:\mail\home.mbox/123",
                              r"D:\mail\work.olm/Accounts/x/Inbox/message_00001.xml"]
    assert launched == []


def test_an_mbox_message_on_a_catalogued_drive_shows_the_archive_on_the_drive(
        launched, monkeypatch):
    from app.ui import tasks, workers

    monkeypatch.setattr(tasks, "resolve_open_path",
                        lambda store, row: r"E:\mail\home.mbox\123")
    workers.open_row_async(object(), SimpleNamespace(
        path="leasha-volume://1/mail/home.mbox/123", volume_id=1,
        relative_path="mail/home.mbox/123"), reveal=True)
    _pump()
    assert launched == [("explorer", r"E:\mail\home.mbox", True)]


def test_plain_files_that_look_numbered_are_not_messages():
    from app.ui.presenter.opening import mail_container, plan_for

    assert mail_container(r"D:\photos\2024/123") == ""
    assert mail_container(SimpleNamespace(path=r"D:\a.mbox/1", source_kind="file")) == ""
    assert mail_container(r"D:\mail\note.eml") == ""
    assert mail_container("pst://s/1") == ""
    assert plan_for(r"D:\photos\2024/123", reveal=True).path == r"D:\photos\2024/123"
    # A row that says it is mail is read by its shape even without the suffix.
    assert mail_container(SimpleNamespace(path=r"D:\Inbox.mbox\mbox/7",
                                          source_kind="eml")) == r"D:\Inbox.mbox\mbox"


def test_a_web_address_goes_to_the_browser_not_to_a_worker(launched, monkeypatch):
    from PySide6.QtGui import QDesktopServices

    from app.ui import workers

    urls: list = []
    monkeypatch.setattr(QDesktopServices, "openUrl", lambda url: urls.append(url.toString()))
    workers.open_row_async(None, "https://example.org/page")
    _pump()
    assert urls == ["https://example.org/page"] and launched == []


def test_the_callers_error_route_is_kept_and_the_windows_is_the_fallback(launched, monkeypatch):
    """Finding 10: `open_async` used to drop `on_error` when it rerouted."""
    from app.core.errors import make_error
    from app.ui import workers

    boom = make_error("ERR_FILE_CORRUPT", "ui.open", path="x")
    monkeypatch.setattr(workers, "open_in_explorer", lambda path, *, select=True: boom)
    mine, window = [], []
    workers.set_open_context(workers.OpenContext(on_error=window.append))
    workers.open_async(r"D:\gone.txt", on_error=mine.append)
    workers.open_async(r"D:\gone.txt")
    _pump()
    assert mine == [boom] and window == [boom]


def test_the_photo_grids_open_reaches_the_results_open_and_its_lightbox_is_a_pop_out(
        monkeypatch):
    """Finding 8 and 4: the grid's menu Open goes where the list's Open goes;
    a double-click builds the lightbox with `pop_out`, as a pinned document is."""
    from app.ui.widgets import preview_window, result_tools
    from app.ui.widgets.thumbnail_grid import ThumbnailGrid

    built: list = []
    monkeypatch.setattr(preview_window, "pop_out",
                        lambda row, **kw: built.append((row, kw["siblings"], kw["store"])) or row)
    opened: list = []
    _results, _preview, outer = result_tools.build_results_pane(
        on_opened=opened.append, on_reveal=lambda _r: None, on_reindex=lambda _r: None,
        on_error=lambda _e: None, store=None)
    grid = outer.findChild(ThumbnailGrid)
    photo = SimpleNamespace(path=r"D:\p\beach.jpg", ext="jpg", chunk_id=1, file_id=1)
    grid.file_requested.emit(photo)
    grid.opened.emit(photo, [photo])
    assert opened == [photo] and built == [(photo, [photo], None)]


def test_the_window_pins_through_pop_out_with_its_store_and_error_box(gui_mainwindow, monkeypatch):
    from app.ui.widgets import preview_window

    _app, window, store, _engine = gui_mainwindow
    asked: list = []
    monkeypatch.setattr(preview_window, "pop_out", lambda row, **kw: asked.append((row, kw)) or row)
    row = SimpleNamespace(path=r"D:\a.txt", name="a.txt")
    window._pin_document(row, provider="the provider")
    assert asked[0][0] is row and asked[0][1]["store"] is store
    assert asked[0][1]["body_provider"] == "the provider"
    assert asked[0][1]["on_error"] == window._show_error
    window._pinned.remove(row)


def test_a_zip_member_or_an_mbox_message_is_not_marked_missing(tmp_path):
    """Search offered "File is missing - re-index" for each: `missing_paths`
    statted the member's path, which is never a file of its own."""
    from app.ui.tasks import missing_paths

    archive = tmp_path / "backup.zip"
    archive.write_bytes(b"PK")
    mailbox = tmp_path / "inbox.mbox"
    mailbox.write_text("From x", encoding="utf-8")
    gone = tmp_path / "gone.docx"
    gone_zip_member = f"{tmp_path / 'gone.zip'}/a.txt"
    paths = [f"{archive}/q3/report.docx", f"{mailbox}/0042", str(gone), gone_zip_member,
             "leasha-volume://3/a.jpg"]
    assert missing_paths(paths) == {str(gone), gone_zip_member}


@pytest.mark.parametrize("text,start_of,line", [
    ("a\nb\nc", "c", 3),
    ("\n\n\nfirst\n\n\n\nsecond\n", "second", 8),
    ("  \n\t\nx = 1\r\n\r\n\r\ny = 2", "y = 2", 6),
])
def test_a_line_is_found_in_the_file_from_an_offset_in_the_indexed_text(text, start_of, line):
    from app.extract.base import line_in_text, normalise_whitespace

    assert line_in_text(text, normalise_whitespace(text).index(start_of)) == line
    assert line_in_text(text, 10_000) == 0
