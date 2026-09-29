r"""Order 0y §1: the Code tab's fixes.

Layer: L0 / L4 / L5

* **1a** - git is started with no console window on Windows.
* **1b** - a history search can be stopped: the git process ends within half
  a second, the result says it was stopped, and the button reads Stop while
  it runs and "Search history" after.
* **1c** - "Ignore this repository" on the row menu, and an Undo that puts back
  exactly the same repository and files with no index run in between.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time

import pytest

from app.core import osbridge
from app.core.osbridge import launch
from app.search import gitsearch
from app.search.gitsearch import STOPPED_EXIT, StopFlag
from app.storage.sqlite_store import FileStatus, SqliteStore

# --- 1a ---------------------------------------------------------------------


def test_git_gets_no_console_window_on_windows(monkeypatch) -> None:
    monkeypatch.setattr(launch, "is_windows", lambda: True)
    assert osbridge.hidden_console_flags() == getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    monkeypatch.setattr(launch, "is_windows", lambda: False)
    assert osbridge.hidden_console_flags() == 0


def test_every_git_call_passes_the_flag(monkeypatch) -> None:
    seen: dict = {}

    class FakeProc:
        returncode = 0

        def __init__(self, args, **kwargs):
            seen.update(kwargs)

        def communicate(self, timeout=None):
            return "", ""

    monkeypatch.setattr(gitsearch.subprocess, "Popen", FakeProc)
    monkeypatch.setattr(gitsearch, "hidden_console_flags", lambda: 0x08000000)
    assert gitsearch._run(["git", "--version"], None, 5.0) == (0, "", "")
    assert seen["creationflags"] == 0x08000000


# --- 1b ---------------------------------------------------------------------


def test_a_running_search_stops_within_half_a_second() -> None:
    stop = StopFlag()
    forever = [sys.executable, "-c", "import time; time.sleep(60)"]
    threading.Timer(0.2, stop.stop).start()
    started = time.monotonic()
    code, _out, err = gitsearch._run(forever, None, 120.0, stop=stop)
    assert code == STOPPED_EXIT and err == "stopped"
    assert time.monotonic() - started < 0.2 + 0.5


def test_a_search_that_is_not_stopped_still_answers() -> None:
    code, out, _err = gitsearch._run(
        [sys.executable, "-c", "print('found')"], None, 30.0, stop=StopFlag())
    assert code == 0 and out.strip() == "found"


def test_a_stopped_query_says_so_and_has_no_rows(tmp_path) -> None:
    from app.search.gitquery import parse_git_query

    stop = StopFlag()
    stop.stop()
    found = gitsearch.run_query(tmp_path, parse_git_query("CustomerId /history"),
                                runner=lambda *_a: (0, "abc\n", ""), stop=stop)
    assert found.stopped and not found.ok and found.rows == []
    assert found.as_dict()["stopped"] is True


# --- 1c: the store half ------------------------------------------------------


@pytest.fixture()
def archive(tmp_path):
    with SqliteStore(tmp_path / "index.db") as store:
        repo_id = store.upsert_repo(r"D:\SearchData", name="SearchData", kind="work")
        for number in range(5):
            store.upsert_file(
                rf"D:\SearchData\report {number}.pdf", size_bytes=1_000,
                mtime_ns=1, ext="pdf", status=FileStatus.INDEXED,
                source_kind="file", repo_id=repo_id)
        yield store


def _attributed(store) -> int:
    return int(store.conn.execute(
        "SELECT COUNT(*) FROM files WHERE repo_id IS NOT NULL AND repo_id > 0").fetchone()[0])


def test_undo_puts_back_exactly_the_same_repository(archive) -> None:
    record = archive.repo_undo_record(r"D:\SearchData")
    assert record and len(record["file_ids"]) == 5 and record["kind"] == "work"

    archive.forget_repo(r"D:\SearchData")
    archive.ignore_repo_root(r"D:\SearchData")
    assert _attributed(archive) == 0 and archive.repos_list() == []

    assert archive.restore_repo(record) == 5
    assert _attributed(archive) == 5
    names = [row["name"] for row in archive.repos_list()]
    assert names == ["SearchData"]
    assert archive.ignored_repo_roots() == [], "undo must stop ignoring it too"


def test_undo_never_takes_a_file_another_repository_claimed(archive) -> None:
    record = archive.repo_undo_record(r"D:\SearchData")
    archive.forget_repo(r"D:\SearchData")
    other = archive.upsert_repo(r"D:\Other", name="Other", kind="work")
    archive.conn.execute("UPDATE files SET repo_id = ? WHERE id = ?",
                         (other, record["file_ids"][0]))
    archive.conn.commit()
    assert archive.restore_repo(record) == 4


def test_an_unknown_root_has_no_undo_record(archive) -> None:
    assert archive.repo_undo_record(r"D:\not\here") is None
    assert archive.repo_undo_record("") is None


# --- 1b and 1c: the window ---------------------------------------------------

pytest.importorskip("PyQt6.QtWidgets", exc_type=ImportError)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication  # noqa: E402

from app.ui.code_view import CodeView  # noqa: E402
from app.ui.widgets import file_menu  # noqa: E402
from tests.unit.test_code_view import FakeStore  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    yield QApplication.instance() or QApplication([])


@pytest.fixture()
def view(qapp, monkeypatch):
    monkeypatch.setattr("app.ui.code_view.run", lambda _pool, worker: worker.run())
    widget = CodeView(FakeStore())
    widget.show()
    widget._repos_read(widget._store.repos_list())
    return widget


def test_the_button_reads_stop_while_git_runs_and_stops_it(view, monkeypatch) -> None:
    held: dict = {}
    monkeypatch.setattr("app.ui.widgets.git_tree.run",
                        lambda _pool, worker: held.setdefault("worker", worker))
    view.input.setText("/repo leasha CustomerId /history")
    view.start()
    assert view.run_button.text() == "Stop"
    assert view._git_escape.isEnabled(), "Esc stops a running search"

    view.start()                                   # the same button, pressed again
    assert view._git_stop.stopped

    stopped = gitsearch.GitSearchResult(ok=False, stopped=True, error="stopped")
    view._show_git(stopped, view._generation)
    assert view.run_button.text() == "Search history"
    assert not view._git_escape.isEnabled()
    assert "stopped" in view.summary.text().lower()


def test_the_row_menu_offers_ignore_for_a_repository_row(view, monkeypatch) -> None:
    captured: dict = {}
    monkeypatch.setattr("app.ui.widgets.repo_ignore.confirm_ignore", lambda _p, _n: False)
    monkeypatch.setattr("app.ui.widgets.code_results.show_for",
                        lambda _w, _p, path, actions: captured.update(actions=actions))
    view._typed()
    view.results.table.selectRow(0)
    asked: list = []
    view.results.ignore_repo_requested.connect(asked.append)
    view.results._on_context_menu(view.results.table.visualItemRect(
        view.results.table.item(0, 0)).center())

    extra = captured["actions"].extra
    assert [label for label, _tip, _run in extra] == ["Ignore this repository"]
    extra[0][2]()
    assert asked == [view.results.table.current_row().repo]
    labels = [a.text() for a in file_menu.build_menu(view, "", captured["actions"]).actions()]
    assert "Ignore this repository" in labels


def test_ignoring_asks_first_then_offers_undo(view, monkeypatch) -> None:
    calls: list = []

    class Store(FakeStore):
        def repo_undo_record(self, root):
            calls.append(("record", root))
            return {"root_path": root, "name": "leasha", "kind": "work", "file_ids": [1, 2, 3]}

        def forget_repo(self, root):
            calls.append(("forget", root))
            return 3

        def ignore_repo_root(self, root):
            calls.append(("ignore", root))

        def restore_repo(self, record):
            calls.append(("restore", record["root_path"]))
            return 3

    monkeypatch.setattr("app.ui.widgets.repo_ignore.run", lambda _pool, worker: worker.run())
    view._store = Store()
    from app.ui.widgets.repo_ignore import ignore_repository

    ignore_repository(view, "leasha", confirm=lambda _p, _n: False)
    assert calls == [], "cancelling the question changes nothing"

    ignore_repository(view, "leasha", confirm=lambda _p, _n: True)
    assert [c[0] for c in calls] == ["record", "forget", "ignore"]
    note = view._ignore_note
    assert note.isVisible() and "Undo" in note.text() and "3" in note.text()

    note.linkActivated.emit("undo")
    assert calls[-1] == ("restore", "D:/SearchProject")
    assert not note.isVisible()
