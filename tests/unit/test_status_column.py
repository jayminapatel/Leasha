r"""A Status column in every results list, and the total behind a capped list.

Layer: L5 (with the L1 counts behind it)

The owner: *"in the results it must show the single word status engine and
this should be visible in the results"* - a Status column in Search, Files,
Mail and Code, each cell one word from `app.core.file_state` with its plain
sentence as the tooltip. And: *"when searching for mails the search displays
maximum 500 but does not tell how much total"* - the Mail summary, and the
Files one that had the same silent cap, now say both numbers.

The word always comes from what the row already carries, or from one batched
read on the worker that builds the rows. Never from the UI thread.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from PyQt6.QtCore import QThreadPool
from PyQt6.QtWidgets import QApplication

from app.core.errors import make_error
from app.core.file_state import explain
from app.search.query import parse_query
from app.storage.sqlite_store import FileStatus, SqliteStore


def _pump(ms: int = 5_000) -> None:
    app = QApplication.instance()
    QThreadPool.globalInstance().waitForDone(ms)
    for _ in range(10):
        app.processEvents()
        QThreadPool.globalInstance().waitForDone(ms)


# 2026-09-30: the `_release(view)` that deleted each view by hand has gone. A
# Files or Mail view that is let go is freed at once now - the cause is fixed
# and pinned in `test_views_are_freed.py`.


@pytest.fixture()
def store(tmp_path: Path):
    s = SqliteStore(tmp_path / "index.db").connect()
    now = time.time_ns()
    done = s.upsert_file("C:/corpus/report.txt", size_bytes=10, mtime_ns=now)
    s.mark_indexed(done)
    held = s.upsert_file("C:/corpus/scan.png", size_bytes=10, mtime_ns=now - 1)
    s.mark_skipped(held, make_error("ERR_OCR_HELD", "test", path="scan.png"))
    s.upsert_file("C:/corpus/film.mp4", size_bytes=10, mtime_ns=now - 2,
                  status=FileStatus.NAME_ONLY)
    for n in range(7):
        message = s.upsert_file(f"pst://box.pst/E{n}", size_bytes=5, mtime_ns=now,
                                source_kind="pst_message")
        s.mark_indexed(message)
        s.set_message(message, subject=f"Quote {n}", sender="dave@acme.com",
                      sent_at=1_700_000_000 + n)
    yield s
    s.close()


def _column(table, key: str, columns) -> int:
    return [k for k, *_ in columns].index(key)


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------

def test_files_shows_a_status_word_with_its_sentence(qapp, store) -> None:
    from app.ui.files_view import COLUMNS, FilesView

    view = FilesView(store)
    try:
        _pump()
        status = _column(view.results, "status", COLUMNS)
        assert view.results.horizontalHeaderItem(status).text() == "Status"
        words = {view.results.item(r, 0).text(): view.results.item(r, status)
                 for r in range(view.results.rowCount())}
        assert words["report.txt"].text() == "Indexed"
        assert words["scan.png"].text() == "Deferred"
        assert words["film.mp4"].text() == "NameOnly"
        assert words["scan.png"].toolTip() == explain("Deferred")
        # Offered in the View menu, and on screen by default.
        assert "status" in view._available
        assert not view.results.isColumnHidden(status)
        # The folder column still stretches: it is still last.
        assert COLUMNS[-1][0] == "folder"
    finally:
        view.shutdown()


def test_files_says_the_total_when_the_page_is_full(qapp, store, monkeypatch) -> None:
    from app.ui import files_view

    # A page of two, so three files are "more than a page".
    real = files_view.browse_files_page
    monkeypatch.setattr(files_view, "browse_files_page",
                        lambda s, parsed, *, limit: real(s, parsed, limit=2))
    view = files_view.FilesView(store)
    try:
        _pump()
        assert view.results.rowCount() == 2
        assert view.summary.text().startswith("Showing 2 of 3 files — narrow it with /type")
    finally:
        view.shutdown()


def test_the_files_worker_counts_only_a_full_page(store) -> None:
    from app.ui.tasks import browse_files_page

    page = browse_files_page(store, parse_query(""), limit=200)
    assert len(page["rows"]) == 3 and page["total"] is None
    page = browse_files_page(store, parse_query(""), limit=1)
    assert page["total"] == 3
    assert page["offline"] == set()


def test_count_browse_files_matches_what_browse_files_returns(store) -> None:
    for query in ("", "report", "/type png", "zz"):
        parsed = parse_query(query)
        rows = store.browse_files(parsed, limit=1000)
        assert store.count_browse_files(parsed) == len(rows), query
    # Bounded: never counts past cap + 1.
    assert store.count_browse_files(parse_query(""), cap=1) == 2


# ---------------------------------------------------------------------------
# Mail
# ---------------------------------------------------------------------------

def test_mail_shows_the_status_word(qapp, store) -> None:
    from app.ui.mail_view import COLUMNS, MailView

    view = MailView(store)
    try:
        _pump()
        status = _column(view.results, "status", COLUMNS)
        assert view.results.horizontalHeaderItem(status).text() == "Status"
        cell = view.results.item(0, status)
        assert cell.text() == "Indexed"
        assert cell.toolTip() == explain("Indexed")
        assert "status" in view._available
    finally:
        view.shutdown()


def test_mail_says_how_many_in_total_when_capped(qapp, store, monkeypatch) -> None:
    """The owner's report, reproduced with a page of five over seven messages."""
    from app.ui import mail_view

    monkeypatch.setattr(mail_view, "PAGE_SIZE", 5)
    view = mail_view.MailView(store)
    try:
        _pump()
        assert view.results.rowCount() == 5
        assert view.summary.text().startswith(
            "Showing 5 of 7 messages — narrow it with /from, /after")
        # Narrowed below a page: no "Showing", just the count.
        view.input.setText("/subject 3")
        view._run()
        _pump()
        assert view.summary.text().startswith("1 message")
    finally:
        view.shutdown()


def test_the_mail_count_uses_the_same_filters_as_the_list(store) -> None:
    for filters in ({}, {"sender": "dave"}, {"subject": "Quote 4"}, {"sender": "nobody"},
                    {"after": 1_700_000_003}):
        rows = store.browse_messages(limit=1000, **filters)
        assert store.count_messages_matching(**filters) == len(rows), filters
    assert store.count_messages_matching(cap=3) == 4, "bounded at cap + 1"


def test_mail_summary_wording() -> None:
    from app.ui.presenter import mail_summary

    assert mail_summary(500, page_size=500, total=12_431).startswith(
        "Showing 500 of 12,431 messages — narrow it with /from, /after")
    assert "more than 100,000" in mail_summary(500, page_size=500, total=100_001)
    # Unknown total: the sentence it always said.
    assert "showing the newest 500" in mail_summary(500, page_size=500)
    assert mail_summary(12, page_size=500, total=12) == "12 messages"


# ---------------------------------------------------------------------------
# Code
# ---------------------------------------------------------------------------

def test_code_rows_carry_the_word_not_the_store_value() -> None:
    from app.ui.presenter.code import repo_file_rows

    rows = repo_file_rows([
        {"path": "C:/repo/a.py", "status": "INDEXED"},
        {"path": "C:/repo/b.py", "status": "SKIPPED", "skip_code": "ERR_FILE_TIMEOUT"},
    ])
    assert [row.status for row in rows] == ["Indexed", "TimedOut"]


def test_code_results_gives_the_status_cell_its_sentence(qtbot) -> None:
    from app.ui.presenter.code import repo_file_rows
    from app.ui.view_options import ViewPreferences
    from app.ui.widgets.code_results import COLUMNS, CodeResults

    results = CodeResults()
    qtbot.addWidget(results)
    results.show_rows(repo_file_rows([{"path": "C:/repo/a.py", "status": "PENDING"}]),
                      ViewPreferences())
    cell = results.table.item(0, _column(results.table, "status", COLUMNS))
    assert cell.text() == "Queued"
    assert cell.toolTip() == explain("Queued")
    assert results.table.item(0, 0).toolTip() == "C:/repo/a.py"


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

def test_search_words_come_from_one_read_on_the_decorating_worker(store) -> None:
    from types import SimpleNamespace

    from app.ui.tasks import decorate_results

    ids = {record["path"]: record["id"]
           for record in store.browse_files(parse_query(""), limit=10)}
    results = [SimpleNamespace(file_id=ids["C:/corpus/scan.png"], path="C:/corpus/scan.png",
                               volume_id=None),
               SimpleNamespace(file_id=ids["C:/corpus/report.txt"],
                               path="C:/corpus/report.txt", volume_id=None)]
    calls = []
    real = store.file_states
    store.file_states = lambda wanted: calls.append(list(wanted)) or real(wanted)
    found = decorate_results(store, results)
    assert len(calls) == 1, "one read for the page, not one per row"
    assert found["statuses"] == {ids["C:/corpus/scan.png"]: "Deferred",
                                 ids["C:/corpus/report.txt"]: "Indexed"}


def test_the_search_list_paints_and_explains_the_word(qtbot) -> None:
    from PyQt6.QtCore import Qt

    from app.search.engine import SearchResult
    from app.ui.results_view import ResultsView

    view = ResultsView()
    qtbot.addWidget(view)
    view.resize(600, 400)
    hits = [SearchResult(chunk_id=1, file_id=7, path=r"D:\Archive\plan.pdf",
                         text="the pump station plan", score=0.9, rank=0, ext="pdf",
                         mtime_ns=1_700_000_000_000_000_000)]
    view.show_results(hits, ["pump"])
    assert view._delegate.statuses == {}, "nothing until the worker lands"
    view.show_results(hits, ["pump"], keep_scroll=True, statuses={7: "Deferred"})
    assert view._delegate.statuses == {7: "Deferred"}
    tip = view._model.item(0).data(Qt.ItemDataRole.ToolTipRole)
    assert f"Status: Deferred — {explain('Deferred')}" in tip
    view.show()
    qtbot.waitExposed(view)
    view.grab()          # paints the row with the word in it - must not raise


def test_redraw_with_details_passes_the_words_on() -> None:
    from types import SimpleNamespace

    from app.ui.widgets.result_table import redraw_with_details

    seen = {}

    class Results:
        def show_results(self, *_a, **kwargs):
            seen.update(kwargs)

    redraw_with_details(Results(), SimpleNamespace(results=[]), [], "",
                        {"statuses": {3: "Indexed"}})
    assert seen["statuses"] == {3: "Indexed"}
