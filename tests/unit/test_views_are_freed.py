r"""A Files, Mail or Code view that is let go is freed at once.

Layer: L5

**What this pins (2026-09-30).** A view nobody holds any more used to stay
alive - not freed by reference count, and not by `gc.collect()` either - until
`view_options._WATCHERS` was cleared by hand. Traced with `gc.get_referrers`,
the path was:

    _WATCHERS -> look (the width watcher's tick) -> header (the table's
    QHeaderView, closed over) -> the view's own `lambda point: self...`
    connected to the header's context menu -> self

So the registry that stops the 2026-09-27 crash (a watcher ticking into a
closure the collector had cleared) pinned the whole view instead: a leak in
place of a crash. Underneath that one path, each view also sat in cycles of
its own - `self`-capturing lambdas handed to its own children - so that even
without the registry only the cyclic collector could end it, at a moment of
its choosing.

Both halves are asserted here **with the collector switched off**, which is
the strict form: the only thing that can free the view is its last reference
going. Nothing in these tests touches `_WATCHERS`.
"""

from __future__ import annotations

import gc
import time
import types
import weakref
from pathlib import Path

import pytest

pytest.importorskip("PyQt6.QtWidgets", exc_type=ImportError)

from PyQt6 import sip                                           # noqa: E402
from PyQt6.QtCore import QPoint, QThreadPool, QTimer            # noqa: E402
from PyQt6.QtWidgets import QApplication                        # noqa: E402

from app.storage.sqlite_store import SqliteStore                # noqa: E402
from app.ui import view_options                                 # noqa: E402


def _pump(ms: int = 5_000) -> None:
    """Let every queued worker finish and its result be delivered."""
    app = QApplication.instance()
    QThreadPool.globalInstance().waitForDone(ms)
    for _ in range(10):
        app.processEvents()
        QThreadPool.globalInstance().waitForDone(ms)


@pytest.fixture()
def store(tmp_path: Path):
    s = SqliteStore(tmp_path / "index.db").connect()
    now = time.time_ns()
    repo = s.upsert_repo("C:/code/leasha", kind="work")
    for n in range(4):
        done = s.upsert_file(f"C:/code/leasha/module{n}.py", size_bytes=10 + n,
                             mtime_ns=now - n, repo_id=repo)
        s.mark_indexed(done)
    for n in range(4):
        message = s.upsert_file(f"pst://box.pst/E{n}", size_bytes=5, mtime_ns=now,
                                source_kind="pst_message")
        s.mark_indexed(message)
        s.set_message(message, subject=f"Quote {n}", sender="dave@acme.com",
                      sent_at=1_700_000_000 + n)
    yield s
    s.close()


def _files(store):
    from app.ui.files_view import FilesView
    return FilesView(store)


def _mail(store):
    from app.ui.mail_view import MailView
    return MailView(store)


def _code(store):
    from app.ui.code_view import CodeView
    view = CodeView(store)
    view.refresh()                 # what a tab switch does; the list fills here
    return view


def _table(view):
    table = view.results
    return getattr(table, "table", table)        # Code keeps its table one down


BUILDERS = {"files": _files, "mail": _mail, "code": _code}


def _used(view) -> None:
    """The view, after somebody has actually used it: filled, typed into,
    a row selected, the View menu's change applied, a watcher tick gone by."""
    _pump()
    table = _table(view)
    assert table.rowCount() > 0, "the list never filled; this would prove nothing"
    table.selectRow(0)
    view.input.setText("module" if hasattr(view, "git_button") else "")
    _pump()
    view._prefs_changed(view.view_button.prefs)
    for timer in table.findChildren(QTimer):
        timer.timeout.emit()
    _pump()


@pytest.fixture()
def no_collector():
    """The cyclic collector off, so only a reference count can free anything."""
    gc.collect()
    was_enabled = gc.isenabled()
    gc.disable()
    try:
        yield
    finally:
        if was_enabled:
            gc.enable()


@pytest.mark.parametrize("which", sorted(BUILDERS))
def test_a_view_that_is_let_go_is_freed_by_its_reference_count(
        _qt_application, store, no_collector, which) -> None:
    view = BUILDERS[which](store)
    _used(view)
    table = _table(view)
    timers = table.findChildren(QTimer)
    alive = weakref.ref(view)
    del table

    del view

    assert alive() is None, (
        f"the {which} view is still alive after its last reference went, with "
        f"the collector off: something it owns refers back to it")
    # Its window went with it, and so did the width watcher on its table.
    assert timers and all(sip.isdeleted(timer) for timer in timers)


def test_a_code_view_that_ran_a_history_search_is_still_freed(
        _qt_application, store, no_collector, monkeypatch) -> None:
    """The history search adds an Esc shortcut the view holds, and a worker
    whose slots draw into the view. Neither may hold the view back - and the
    worker is kept here on purpose: a finished worker is not freed until the
    collector passes, so its slots are exactly what would keep a view alive."""
    held: dict = {}
    monkeypatch.setattr("app.ui.widgets.git_tree.run",
                        lambda _pool, worker: held.update(worker=worker))
    view = _code(store)
    _pump()
    view.input.setText("module /history")
    view.start()
    assert view.run_button.text() == "Stop", "the history search never started"
    held["worker"].signals.done.emit()
    assert view.run_button.text() == "Search history"
    alive = weakref.ref(view)

    del view

    assert alive() is None
    held["worker"].signals.done.emit()           # into a view that has gone: nothing


@pytest.mark.parametrize("which", sorted(BUILDERS))
def test_the_watcher_registry_does_not_hold_a_view(
        _qt_application, store, no_collector, which) -> None:
    """The registry keeps the watcher's tick reachable - and nothing of the view.

    Looked for from the registry's side, so that this still fails for the right
    reason if a view is one day kept alive by something else as well.
    """
    before = set(view_options._WATCHERS)
    view = BUILDERS[which](store)
    _pump()
    mine = [entry for key, entry in view_options._WATCHERS.items() if key not in before]
    assert mine, "the view's table started no width watcher"

    def reaches(start, target) -> bool:
        """Is `target` among what `start` keeps alive? Stops at classes and at
        modules' globals: every function reaches its module, and from there
        everything reaches everything, which would answer a different question."""
        seen, queue = set(), [start]
        while queue:
            thing = queue.pop()
            if thing is target:
                return True
            if (id(thing) in seen or isinstance(thing, (type, types.ModuleType))
                    or (isinstance(thing, dict) and "__builtins__" in thing)):
                continue
            seen.add(id(thing))
            assert len(seen) < 200_000, "the walk escaped into the whole process"
            queue.extend(gc.get_referents(thing))
        return False

    assert not any(reaches(look, view) for _timer, look in mine), (
        f"the {which} view is reachable from view_options._WATCHERS")
    assert not any(reaches(look, _table(view)) for _timer, look in mine)


@pytest.mark.parametrize("which", sorted(BUILDERS))
def test_a_dead_view_leaves_nothing_in_the_registry_after_the_next_view(
        _qt_application, store, no_collector, which) -> None:
    before = set(view_options._WATCHERS)
    view = BUILDERS[which](store)
    _pump()
    mine = set(view_options._WATCHERS) - before
    del view

    other = BUILDERS[which](store)          # registering prunes the dead ones
    _pump()
    assert not (mine & set(view_options._WATCHERS))
    del other


def test_the_header_menu_still_opens_the_view_menu(_qt_application, store) -> None:
    """What the `self`-capturing lambdas did must still happen: a right-click
    on a column heading asks the View button for its menu, at that point."""
    for which in ("files", "mail", "code"):
        view = BUILDERS[which](store)
        _pump()
        asked: list = []
        view.view_button.show_menu = asked.append
        header = _table(view).horizontalHeader()

        header.customContextMenuRequested.emit(QPoint(3, 4))

        assert asked == [header.mapToGlobal(QPoint(3, 4))], which
        del view


def test_typing_still_starts_the_debounce(_qt_application, store) -> None:
    for which in ("files", "mail", "code"):
        view = BUILDERS[which](store)
        _pump()
        view._timer.stop()

        view.input.setText("quote")

        assert view._timer.isActive(), which
        view._timer.stop()
        del view
