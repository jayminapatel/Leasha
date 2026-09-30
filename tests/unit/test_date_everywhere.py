r"""`/date` in every box with a date to filter on. Order "dates" §1c and §1e.

Layer: L5, driving L1 and L4

The order's acceptance sentence: *type `date:2017-03..2017-06` in any box and
see only what is from those months, mail included.* Each box is checked twice,
because they are two different failures: that the `/` menu **offers** `/date`
with its example (a filter nobody is told about delivers nothing), and that the
box then **applies** it - through the real widget, a real `SqliteStore` and the
function the box actually runs, not the parser alone. A menu row that the tab
then ignores is the failure `test_command_subsets.py` exists for.

**Code gets `/date` too**, because its rows carry a date: the "When" column is
the file's modified time, and the index route answers through
`store.browse_files` - the same `file_filter_sql` Files uses.

Qt tests run offscreen here; the owner's machine is where the window opens.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

pytest.importorskip("PyQt6.QtWidgets", exc_type=ImportError)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QThreadPool  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from app.storage.sqlite_store import FileStatus, SqliteStore  # noqa: E402

NS = 1_000_000_000


def _ns(year: int, month: int, day: int) -> int:
    return int(time.mktime((year, month, day, 12, 0, 0, 0, 0, -1))) * NS


def _seconds(year: int, month: int, day: int) -> int:
    return int(time.mktime((year, month, day, 12, 0, 0, 0, 0, -1)))


@pytest.fixture(scope="module")
def qapp():
    yield QApplication.instance() or QApplication([])


def _release(widget) -> None:
    r"""Delete a view's window object now, rather than when the collector
    gets round to it.

    **2026-09-27, order 0x §6c.** A view here takes part in reference cycles
    (its View button's closures, its tables' column-width watcher), so
    letting go of it at the end of a test leaves it to the cyclic collector
    - and a watcher timer can then tick into a closure the collector has
    already cleared. That crashed this file natively, inside
    `test_the_cli_search_applies_date`, depending only on how many objects
    earlier test files had allocated. Deleting the widget here takes its
    timers with it, at a point that is not inside anything else.
    """
    from PyQt6 import sip

    if not sip.isdeleted(widget):
        sip.delete(widget)


def _pump(ms: int = 5_000) -> None:
    app = QApplication.instance()
    QThreadPool.globalInstance().waitForDone(ms)
    for _ in range(10):
        app.processEvents()
        QThreadPool.globalInstance().waitForDone(ms)


@pytest.fixture()
def store(tmp_path: Path):
    r"""Three months on the calendar: March 2017, July 2017 and May 2019.

    Two ordinary files, two in a repository, two messages - each pair with
    one in March 2017 and one outside it, so `/date 2017-03` has something to
    keep and something to drop on every tab.
    """
    with SqliteStore(tmp_path / "index.db") as opened:
        repo = opened.upsert_repo("C:/code/leasha", name="leasha", kind="git")

        def add(path: str, ext: str, mtime: int, text: str, repo_id=None) -> int:
            file_id = opened.upsert_file(
                path, parent_dir=path.rsplit("/", 1)[0], ext=ext, size_bytes=10,
                mtime_ns=mtime, status=FileStatus.INDEXED, source_kind="file",
                repo_id=repo_id)
            opened.replace_chunks(file_id, [{"ordinal": 0, "text": text}])
            return file_id

        add("C:/work/march report.txt", "txt", _ns(2017, 3, 10), "the march report")
        add("C:/work/july report.txt", "txt", _ns(2017, 7, 1), "the july report")
        add("C:/code/leasha/march.py", "py", _ns(2017, 3, 20), "def march(): pass", repo)
        add("C:/code/leasha/later.py", "py", _ns(2019, 5, 1), "def later(): pass", repo)
        # Messages: the file's own date is "now", as a message's container's is;
        # the Mail tab must answer from the sent date and nothing else.
        now = time.time_ns()
        for path, subject, sent in (
            ("C:/mail/march.msg", "March quote", _seconds(2017, 3, 12)),
            ("C:/mail/may.msg", "May quote", _seconds(2019, 5, 2)),
        ):
            message = add(path, "msg", now, f"the {subject.lower()}")
            opened.set_message(message, sender="dave@acme.com", subject=subject,
                               recipients='["me@acme.com"]', sent_at=sent)
        yield opened


@pytest.fixture()
def engine(store):
    from app.search.engine import SearchEngine

    class _NoVectors:
        def search(self, *_a, **_k):
            return []

    class _NoModel:
        def embed(self, _t):
            raise RuntimeError("no model")

        def embed_all(self, _t):
            raise RuntimeError("no model")

    built = SearchEngine(store, _NoVectors(), _NoModel())
    yield built
    built.close()


def _offered(popup) -> list[str]:
    """The `/` menu's rows for `/dat`, as a person would read them."""
    popup.set_prefix("dat")
    model = popup.model()
    return [str(model.index(row, 0).data()) for row in range(model.rowCount())]


def _offers_date(popup) -> None:
    rows = [row for row in _offered(popup) if row.startswith("/date")]
    assert rows, "the / menu does not offer /date"
    # The row's example is its value hint, cut to fit; the range has to
    # survive the cut, because `A..B` is the form nobody would guess.
    assert "2017-03, 2017-01..2017-06" in rows[0]
    assert "between two dates" in rows[0]                  # what it does


def _table_names(table) -> set[str]:
    return {table.item(row, 0).text() for row in range(table.rowCount())}


# --- Search ------------------------------------------------------------------

def test_search_offers_date_and_applies_it(qapp, engine):
    from app.ui.search_view import SearchView

    view = SearchView(engine)
    try:
        _offers_date(view.commands)
        view.input.setText("report /date 2017-03")
        # The typing debounce would fire mid-wait on a slow machine and race
        # this search with an interim one; Enter is what is being tested.
        view._interim_timer.stop()
        view._full_timer.stop()
        view.search_now()
        _pump()
        assert [Path(row.path).name for row in view.results._rows] == ["march report.txt"]
    finally:
        view.shutdown()
        _release(view)


# --- Files -------------------------------------------------------------------

def test_files_offers_date_and_applies_it(qapp, store):
    from app.ui.files_view import FilesView

    view = FilesView(store)
    try:
        _offers_date(view._popup)
        view.input.setText("/date 2017-03")
        view._run()
        _pump()
        assert _table_names(view.results) == {"march report.txt", "march.py"}
        # The acceptance sentence's range, and a whole year.
        view.input.setText("date:2017-03..2017-07")
        view._run()
        _pump()
        assert _table_names(view.results) == {
            "march report.txt", "july report.txt", "march.py"}
    finally:
        view.shutdown()
        _release(view)


# --- Mail --------------------------------------------------------------------

def test_mail_offers_date_and_applies_it_to_the_sent_date(qapp, store):
    from app.ui.mail_view import MailView

    view = MailView(store)
    try:
        _offers_date(view._popup)
        view.input.setText("/date 2017-03")
        view._run()
        _pump()
        assert [row.subject for row in view._rows] == ["March quote"]
        view.input.setText("date:2017-03..2017-06")
        view._run()
        _pump()
        assert [row.subject for row in view._rows] == ["March quote"]
        view.input.setText("date:2019..")
        view._run()
        _pump()
        assert [row.subject for row in view._rows] == ["May quote"]
    finally:
        view.shutdown()
        _release(view)


# --- Code --------------------------------------------------------------------

def test_code_offers_date_and_applies_it_to_repository_files(qapp, store):
    r"""**Code gets `/date` because its rows carry a date** - the "When"
    column is the file's modified time - and the index route filters on it
    through `browse_files`, the same definition Files uses. Only repository
    files, which is what keeps it the Code tab."""
    from app.ui.code_view import CodeView

    view = CodeView(store)
    try:
        _offers_date(view._popup)
        view.input.setText("/date 2017-03")
        view._typed()
        _pump()
        assert _table_names(view.results.table) == {"march.py"}
    finally:
        view.shutdown()
        _release(view)


# --- The mini-search ---------------------------------------------------------

def test_the_mini_search_offers_date_and_applies_it(qapp, engine):
    r"""It had no `/` menu at all, and it passed the box to the engine without
    `expand_slashes` - so `/after 2017` there searched for the words "after"
    and "2017". Both are the Search tab's behaviour now."""
    from app.ui.widgets.mini_search import MiniSearch, row_label

    box = MiniSearch(engine)
    try:
        _offers_date(box._popup)
        box.box.setText("report /date 2017-03")
        box._search()
        _pump()
        labels = [row_label(group) for group in box._rows]
        assert len(labels) == 1 and labels[0].startswith("march report.txt")
    finally:
        box.dismiss()


def test_enter_on_the_mini_search_menu_chooses_the_row_and_keeps_the_box(qapp, engine):
    r"""Enter on a `/` menu row reaches the box as `returnPressed` *before*
    the row is inserted. In the mini-search that opened the highlighted
    result and closed the box - on the keystroke that chose a filter."""
    from PyQt6.QtCore import Qt
    from PyQt6.QtTest import QTest

    from app.ui.widgets.mini_search import MiniSearch

    box = MiniSearch(engine)
    chosen, expanded = [], []
    box.chosen.connect(chosen.append)
    box.expanded.connect(expanded.append)
    try:
        box.summon()
        QTest.keyClicks(box.box, "/dat")
        qapp.processEvents()
        assert box._popup.popup().isVisible()
        QTest.keyClick(box._popup.popup(), Qt.Key.Key_Down)
        QTest.keyClick(box._popup.popup(), Qt.Key.Key_Return)
        qapp.processEvents()
        assert box.box.text() == "date:"
        assert chosen == [] and expanded == []
        assert box.isVisible()
    finally:
        box.dismiss()


# --- The value menu ------------------------------------------------------------

def test_the_date_values_say_the_period_they_cover():
    r"""`/after 30d` says "(since …)"; a `/date` value names its period -
    `today` is today, not everything since this morning."""
    from datetime import date

    from app.ui.presenter import resolved_period, value_rows, value_suggestions

    today = date(2026, 9, 27)
    rows = value_rows("date", value_suggestions(None, "date", ""), today=today)
    assert any(row.startswith("today") and "(27 Sep 2026)" in row for row in rows)
    assert any(row.startswith("30d") and "(since 28 Aug 2026)" in row for row in rows)
    assert resolved_period("2017-03", today=today) == "(1 Mar 2017 to 31 Mar 2017)"
    assert resolved_period("..2017", today=today) == "(up to 31 Dec 2017)"
    assert resolved_period("2017-03-14", today=today) == "(14 Mar 2017)"
    assert resolved_period("2017-13", today=today) == ""


# --- The command line (non-negotiable #8) ------------------------------------

def test_the_cli_search_applies_date(tmp_path, capsys, monkeypatch):
    r"""`app.cli search "date:2017"` - the same parse, the same filter, and the
    applied range in the JSON, so a headless check sees what the window does.

    2026-09-30: the model cache here is temporary and empty, so on a machine
    with a network `search` fetched the meaning model on every run, and hung
    when the network dropped. The fetch is refused, as in `test_cli_wiring`;
    the date filter under test is on the keyword side."""
    import json

    from app import cli
    from tests.unit.test_cli_wiring import env_file, parser_for, refuse_model_fetch

    refuse_model_fetch(monkeypatch)

    env = env_file(tmp_path)
    cli.cmd_init(parser_for(["init", "--env", env]))
    with SqliteStore(tmp_path / "data" / "index.db") as opened:
        for path, mtime in (("C:/work/march report.txt", _ns(2017, 3, 10)),
                            ("C:/work/may report.txt", _ns(2019, 5, 1))):
            file_id = opened.upsert_file(
                path, parent_dir="C:/work", ext="txt", size_bytes=10, mtime_ns=mtime,
                status=FileStatus.INDEXED, source_kind="file")
            opened.replace_chunks(file_id, [{"ordinal": 0, "text": "the report"}])
    capsys.readouterr()

    code = cli.cmd_search(parser_for(
        ["search", "date:2017", "--json", "--no-rerank", "--env", env]))
    payload = json.loads(capsys.readouterr().out)

    assert code == cli.EXIT_OK, payload
    assert [Path(hit["path"]).name for hit in payload["results"]] == [
        "march report.txt"], payload
    assert payload["dates"] == {"after": "2017-01-01", "before": "2017-12-31"}


# --- §1d: a mistyped date is said where each box already says things --------

BAD = "date:2017-13"
SAID = ("date:2017-13 isn't a date — there is no month 13. "
        "Try date:2017-12 or date:2017-01..2017-06")


def test_search_says_what_was_wrong_on_the_notice_bar(qapp, engine):
    r"""The notice bar is where the Search tab already says a search was not
    what it looked like. Not only the status line's `Ignored: date:2017-13`,
    which says what happened and not why - and is only shown when there are
    results at all."""
    from app.ui.search_view import SearchView

    view = SearchView(engine)
    try:
        view.input.setText(f"report {BAD}")
        # The typing debounce would fire mid-wait on a slow machine and race
        # this search with an interim one; Enter is what is being tested.
        view._interim_timer.stop()
        view._full_timer.stop()
        view.search_now()
        _pump()
        assert SAID in view.notices.label.text()
    finally:
        view.shutdown()
        _release(view)


@pytest.mark.parametrize("tab", ["files", "mail", "code"])
def test_the_other_tabs_say_it_on_their_summary_line(qapp, store, tab):
    from app.ui.code_view import CodeView
    from app.ui.files_view import FilesView
    from app.ui.mail_view import MailView

    view = {"files": FilesView, "mail": MailView, "code": CodeView}[tab](store)
    try:
        if tab == "code":
            view.refresh()                   # the summary needs the repositories
            _pump()
        view.input.setText(BAD)
        (view._typed if tab == "code" else view._run)()
        _pump()
        assert view.summary.text().startswith(f"⚠ {SAID}")
    finally:
        view.shutdown()
        _release(view)


def test_the_notice_comes_first_and_cannot_inject_markup():
    r"""The bar draws rich text and the sentence quotes what was typed, so the
    value is escaped; and the date comes first, because a filter that is not
    there is the whole explanation for the list beneath it."""
    from app.search.query import parse_query
    from app.ui.presenter import NOTICE_DATE_PROBLEM, window_notices, with_date_problems

    parsed = parse_query("report type:pdf date:<b>soon</b>")
    (hint, *_rest) = window_notices("report type:pdf date:<b>soon</b>", parsed)
    assert hint.code == NOTICE_DATE_PROBLEM
    assert "<b>" not in hint.message and "&lt;b&gt;" in hint.message
    assert with_date_problems("3 files", parse_query(BAD)) == f"⚠ {SAID}  ·  3 files"
    assert with_date_problems("3 files", parse_query("date:2017")) == "3 files"


def test_the_cli_says_it_too(tmp_path, capsys, monkeypatch):
    from app import cli
    from tests.unit.test_cli_wiring import env_file, parser_for, refuse_model_fetch

    refuse_model_fetch(monkeypatch)      # as above: an empty temporary model cache
    env = env_file(tmp_path)
    cli.cmd_init(parser_for(["init", "--env", env]))
    capsys.readouterr()
    cli.cmd_search(parser_for(["search", "report", BAD, "--no-rerank", "--env", env]))
    out = capsys.readouterr().out
    assert f"  ! {SAID}" in out
    assert "(ignored: date:2017-13)" in out
