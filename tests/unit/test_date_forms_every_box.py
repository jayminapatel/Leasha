r"""Every search box, typed into: `/between` and a `date:` range. Order 0x §6c.

Layer: L5, driving L1 and L4

The order asks for proof that each box uses **the same parser for dates** and
offers **the same date forms**. `test_date_everywhere.py` (0w) already proves
`/date` is offered and applied in each box by setting the text and calling the
run method. This file is the §5b half (`docs/WORKORDER-CONVENTIONS.md`): the
keys are *pressed*, one character at a time, through the box's own debounce,
and the filter that reached the query is read back from the box itself.

Two lines per box, and each is chosen so the two cannot both pass by luck:

* `/between 2017-03-01 and 2017-06-30` - keeps March 2017, drops July 2017
  and May 2019.
* `date:2017-07..2019-05` - the other way round: keeps July 2017 and May
  2019, drops March.

**Boxes, and the one that is not a box.**

* Search, Files, Mail, Code and the mini-search each have a line to type in
  and all five run `parse_query(expand_slashes(...))` on it.
* The Timeline has no query line by design (order 0n §4d): it has a From box
  and a To box, each read by the parser's own `_parse_date`. Its scenario
  types the two ends into those boxes and checks they reach the same period,
  and that a whole range typed into one box is refused in plain words rather
  than read as something else.
* Chat has no source search box. Its question goes through
  `translate_rules.read` - covered by `test_between_dates.py`
  (`test_chat_keeps_its_own_rule_about_years`), not here.

Qt runs offscreen here; the owner's machine is where the window opens.
"""

from __future__ import annotations

import os
import time
from datetime import date
from pathlib import Path

import pytest

pytest.importorskip("PyQt6.QtWidgets", exc_type=ImportError)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import Qt, QThreadPool  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from app.storage.sqlite_store import FileStatus, SqliteStore  # noqa: E402

pytestmark = pytest.mark.gui

NS = 1_000_000_000

#: The two lines typed into every box, and the dates each must reach.
BETWEEN = "/between 2017-03-01 and 2017-06-30"
BETWEEN_EDGES = (date(2017, 3, 1), date(2017, 6, 30))
DATE_RANGE = "date:2017-07..2019-05"
DATE_EDGES = (date(2017, 7, 1), date(2019, 5, 31))


def _ns(year: int, month: int, day: int) -> int:
    return int(time.mktime((year, month, day, 12, 0, 0, 0, 0, -1))) * NS


def _seconds(year: int, month: int, day: int) -> int:
    return int(time.mktime((year, month, day, 12, 0, 0, 0, 0, -1)))


@pytest.fixture(scope="module")
def qapp():
    # Module-scoped and held, as `test_date_everywhere.py` does: a
    # QApplication nobody references is collected, and the next widget built
    # in this process aborts. pytest-qt's `qtbot` uses this one.
    yield QApplication.instance() or QApplication([])


def _pump(ms: int = 5_000) -> None:
    """Let the workers finish and their results reach the widgets."""
    app = QApplication.instance()
    QThreadPool.globalInstance().waitForDone(ms)
    for _ in range(10):
        app.processEvents()
        QThreadPool.globalInstance().waitForDone(ms)


@pytest.fixture()
def store(tmp_path: Path):
    r"""Three dates on the calendar - 10 March 2017, 1 July 2017, 1 May 2019 -
    for ordinary files, repository files and messages alike."""
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
        add("C:/work/may report.txt", "txt", _ns(2019, 5, 1), "the may report")
        add("C:/code/leasha/march.py", "py", _ns(2017, 3, 20), "def march(): pass", repo)
        add("C:/code/leasha/later.py", "py", _ns(2019, 5, 1), "def later(): pass", repo)
        # Messages: the file's own date is "now", as a message container's
        # is. The Mail tab must answer from the sent date and nothing else.
        now = time.time_ns()
        for path, subject, sent in (
            ("C:/mail/march.msg", "March quote", _seconds(2017, 3, 12)),
            ("C:/mail/july.msg", "July quote", _seconds(2017, 7, 2)),
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


def _release(widget) -> None:
    r"""Delete a tab's window object now, not when the collector runs.

    Files, Mail and Code views sit in reference cycles (their column-width
    watchers among them), so a view merely let go of is freed by the cyclic
    collector at an arbitrary moment - and a watcher can tick into a closure
    the collector has already cleared, which is a native crash in whichever
    test happens to be running. `test_date_everywhere._release` has the same
    note; deleting here takes the timers with the widget, deterministically.

    **2026-09-30: needed for the Search view only** - see the same dated note
    in `test_date_everywhere._release`. Files, Mail and Code are freed at once.
    """
    from PyQt6 import sip

    if not sip.isdeleted(widget):
        sip.delete(widget)


def _spy_on(engine) -> list:
    """Every `ParsedQuery` the engine answers, in order - the applied filter,
    read from what actually ran rather than from the box's text."""
    seen: list = []

    def spy(real):
        def answer(*args, **kwargs):
            response = real(*args, **kwargs)
            seen.append(getattr(response, "parsed", None))
            return response
        return answer

    # Both tiers: typing gets the quick one first and the full one after.
    engine.search = spy(engine.search)
    engine.interim = spy(engine.interim)
    return seen


def _type(qtbot, box, text: str) -> None:
    """Clear the box and press the keys, one at a time, as a person would.

    Then close the `/` menu the typing opened: it is a separate window, and a
    leftover one would take the next test's keys.
    """
    box.clear()
    qtbot.keyClicks(box, text)
    completer = box.completer()
    if completer is not None:
        completer.popup().hide()


def _edges(parsed) -> tuple:
    return (getattr(parsed, "after", None), getattr(parsed, "before", None))


def _table_names(table) -> set[str]:
    return {table.item(row, 0).text() for row in range(table.rowCount())}


# --- Search ------------------------------------------------------------------

def test_search_box_applies_between_and_a_date_range(qapp, qtbot, engine):
    from app.ui.search_view import SearchView

    seen = _spy_on(engine)
    view = SearchView(engine)
    try:
        _type(qtbot, view.input, f"report {BETWEEN}")
        # The typing debounce runs the search; nothing is called by hand.
        qtbot.waitUntil(lambda: any(_edges(p) == BETWEEN_EDGES for p in seen), timeout=5000)
        _pump()
        qtbot.waitUntil(lambda: bool(view.results._rows), timeout=5000)
        assert [Path(r.path).name for r in view.results._rows] == ["march report.txt"]
        # One chip for the whole range, under the box.
        assert "between: 2017-03-01 and 2017-06-30" in view.chips.labels()

        seen.clear()
        _type(qtbot, view.input, f"report {DATE_RANGE}")
        qtbot.waitUntil(lambda: any(_edges(p) == DATE_EDGES for p in seen), timeout=5000)
        _pump()
        qtbot.waitUntil(
            lambda: {Path(r.path).name for r in view.results._rows}
            == {"july report.txt", "may report.txt"}, timeout=5000)
    finally:
        view.shutdown()
        _release(view)


# --- Files -------------------------------------------------------------------

def test_files_box_applies_between_and_a_date_range(qapp, qtbot, store):
    from app.ui.files_view import FilesView

    view = FilesView(store)
    try:
        _type(qtbot, view.input, BETWEEN)
        qtbot.waitUntil(lambda: _edges(getattr(view, "_parsed", None)) == BETWEEN_EDGES,
                        timeout=5000)
        _pump()
        assert _table_names(view.results) == {"march report.txt", "march.py"}

        _type(qtbot, view.input, DATE_RANGE)
        qtbot.waitUntil(lambda: _edges(view._parsed) == DATE_EDGES, timeout=5000)
        _pump()
        assert _table_names(view.results) == {
            "july report.txt", "may report.txt", "later.py"}
    finally:
        view.shutdown()


# --- Mail --------------------------------------------------------------------

def test_mail_box_applies_between_and_a_date_range_to_the_sent_date(qapp, qtbot, store):
    from app.ui.mail_view import MailView

    view = MailView(store)
    try:
        _type(qtbot, view.input, BETWEEN)
        qtbot.waitUntil(lambda: _edges(getattr(view, "_parsed", None)) == BETWEEN_EDGES,
                        timeout=5000)
        _pump()
        assert [row.subject for row in view._rows] == ["March quote"]

        _type(qtbot, view.input, DATE_RANGE)
        qtbot.waitUntil(lambda: _edges(view._parsed) == DATE_EDGES, timeout=5000)
        _pump()
        assert [row.subject for row in view._rows] == ["July quote"]
    finally:
        view.shutdown()


# --- Code (the index meaning) -------------------------------------------------

def test_code_box_applies_between_and_a_date_range_to_repository_files(qapp, qtbot, store):
    r"""**`/between` goes to the index here, not to git.** It used to be a
    spelling of git's `/range`; since D2 it is `/date`, and `/date` in Code
    means the index (HANDOFF §3). The route must say "index"."""
    from app.ui.code_view import CodeView
    from app.ui.presenter import code_route

    view = CodeView(store)
    try:
        _type(qtbot, view.input, BETWEEN)
        qtbot.waitUntil(lambda: _edges(getattr(view, "_parsed", None)) == BETWEEN_EDGES,
                        timeout=5000)
        _pump()
        assert code_route(view.input.text()).engine == "index"
        assert _table_names(view.results.table) == {"march.py"}

        _type(qtbot, view.input, DATE_RANGE)
        qtbot.waitUntil(lambda: _edges(view._parsed) == DATE_EDGES, timeout=5000)
        _pump()
        assert _table_names(view.results.table) == {"later.py"}
    finally:
        view.shutdown()


# --- The mini-search ---------------------------------------------------------

def test_mini_search_applies_between_and_a_date_range(qapp, qtbot, engine):
    from app.ui.widgets.mini_search import MiniSearch, row_label

    seen = _spy_on(engine)
    box = MiniSearch(engine)
    try:
        box.summon()
        _type(qtbot, box.box, f"report {BETWEEN}")
        qtbot.waitUntil(lambda: any(_edges(p) == BETWEEN_EDGES for p in seen), timeout=5000)
        _pump()
        qtbot.waitUntil(lambda: bool(box._rows), timeout=5000)
        labels = [row_label(group) for group in box._rows]
        assert len(labels) == 1 and labels[0].startswith("march report.txt")

        seen.clear()
        _type(qtbot, box.box, f"report {DATE_RANGE}")
        qtbot.waitUntil(lambda: any(_edges(p) == DATE_EDGES for p in seen), timeout=5000)
        _pump()
        qtbot.waitUntil(lambda: len(box._rows) == 2, timeout=5000)
        labels = sorted(row_label(group) for group in box._rows)
        assert labels[0].startswith("july report.txt")
        assert labels[1].startswith("may report.txt")
    finally:
        box.dismiss()


# --- The Timeline ------------------------------------------------------------

def test_timeline_from_and_to_reach_the_same_period_as_a_typed_range(qapp, qtbot):
    r"""The Timeline has a From box and a To box instead of a query line.
    Typing the two ends of each range there must give exactly the dates the
    search boxes give for `/between` and `date:`.

    **A gap, recorded rather than closed:** a whole range typed into one box -
    `/between A and B` or `date:A..B` - is not read there. 0w decided the
    boxes take single values because they are already a From/To pair. What
    this checks is that it is *said* (the plain-words sentence) rather than
    quietly read as some other period.
    """
    from app.search.commands import expand_slashes
    from app.search.query import parse_query
    from app.ui.widgets.timeline_picker import TimelinePicker

    picker = TimelinePicker()
    chosen, refused = [], []
    picker.period_chosen.connect(chosen.append)
    picker.bad_date.connect(refused.append)

    for typed, first, last in ((BETWEEN, "2017-03-01", "2017-06-30"),
                               (DATE_RANGE, "2017-07", "2019-05")):
        chosen.clear()
        picker.range_from.clear()
        picker.range_to.clear()
        qtbot.keyClicks(picker.range_from, first)
        qtbot.keyClicks(picker.range_to, last)
        qtbot.keyClick(picker.range_to, Qt.Key.Key_Return)
        assert len(chosen) == 1, typed
        parsed = parse_query(expand_slashes(typed))
        assert (chosen[0].after, chosen[0].before) == (parsed.after, parsed.before), typed

    for whole in (BETWEEN, DATE_RANGE):
        chosen.clear()
        refused.clear()
        picker.range_from.clear()
        picker.range_to.clear()
        qtbot.keyClicks(picker.range_from, whole)
        qtbot.keyClick(picker.range_from, Qt.Key.Key_Return)
        assert chosen == [] and len(refused) == 1, whole
        assert refused[0].strip(), whole
