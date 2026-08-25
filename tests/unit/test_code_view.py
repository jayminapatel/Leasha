r"""The Code page: one box, one list, two engines.

Layer: L5

**Rebuilt after the owner's correction**: *"the code search page is all wrong it
should be a combined one search box with the git code files in the list"*. What
was here before tested a tree of repositories with a separate git box below it,
and none of that exists any more.

**The decisions are not tested here.** Which engine answers, what the summary
says, what a git result looks like as a row - all of that is in
`presenter.code_route` and its neighbours, and `test_code_route.py` covers it
without a display. This project has shipped UI logic that was verified by
reading and crashed on the first run (`QPdfView()` without its parent), so the
rule is: decisions go where they can be executed, and what is left here is the
wiring - which has to be checked by constructing the thing.

These need a display and are skipped without one. They run on the owner's
machine, which is where the window actually opens.
"""

from __future__ import annotations

import os

import pytest

from app.search.query import SCOPES as QUERY_SCOPES
from app.ui.presenter import code_route

pytest.importorskip("PyQt6.QtWidgets", exc_type=ImportError)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication  # noqa: E402

from app.ui.code_view import COLUMNS, PREFS_KEY, CodeView  # noqa: E402
from app.ui.widgets.search_bar import SCOPES  # noqa: E402

REPOS = [
    {"id": 1, "name": "leasha", "kind": "work",
     "root_path": "D:/SearchProject", "last_seen": 1756000000, "files": 9},
    {"id": 2, "name": "my tools", "kind": "submodule",
     "root_path": "D:/code/my tools", "last_seen": 1756100000, "files": 10},
]

FILES = [
    {"id": 1, "path": "D:/SearchProject/app/cli.py", "ext": "py",
     "size_bytes": 100, "mtime_ns": 2, "status": "INDEXED", "repo": "leasha"},
    {"id": 2, "path": "D:/code/my tools/main.rs", "ext": "rs",
     "size_bytes": 50, "mtime_ns": 1, "status": "INDEXED", "repo": "my tools"},
]


@pytest.fixture(scope="module")
def qapp():
    yield QApplication.instance() or QApplication([])


class FakeStore:
    """Only what the view calls."""

    def __init__(self, repos=None, files=None, files_total: int = 100):
        self._repos = REPOS if repos is None else repos
        self._files = FILES if files is None else files
        self._files_total = files_total
        self.asked: list[tuple] = []

    def repos_list(self):
        return list(self._repos)

    def code_files(self, text="", *, repo="", ext=None, limit=500):
        self.asked.append((text, repo, tuple(ext or ()), limit))
        return list(self._files)

    def distinct_values(self, _kind, *, prefix="", limit=40):
        return []

    def stats(self):
        return {"files_total": self._files_total}

    def get_state(self, _key, default=""):
        return default

    def set_states(self, _values):
        return None

    def all_state(self):
        return {}


@pytest.fixture()
def view(qapp, monkeypatch):
    """A view whose workers run inline.

    **Every query this page makes goes to a worker**, which is the point - a
    store read on the interface thread is the freeze this application has a
    standing rule against. It also means nothing has happened by the time a
    test returns from `_typed()`, so the first version of these tests asserted
    on an empty list and failed for a reason that had nothing to do with the
    feature. Running the worker inline keeps the assertions about behaviour.
    """
    def inline(_pool, worker):
        worker.run()

    monkeypatch.setattr("app.ui.code_view.run", inline)
    widget = CodeView(FakeStore())
    widget.show()               # `isVisible` is False for a widget never shown
    widget._repos_read(widget._store.repos_list())
    return widget


def rows(view) -> list:
    table = view.results.table
    return [table.item(n, 0).text() for n in range(table.rowCount())]


# --- one box ----------------------------------------------------------------

def test_there_is_exactly_one_search_box(view):
    """**The correction.** Two boxes made somebody choose an engine before they
    had a question."""
    from PyQt6.QtWidgets import QLineEdit

    boxes = [child for child in view.findChildren(QLineEdit)
             if child.isVisibleTo(view)]

    assert len(boxes) == 1
    assert boxes[0] is view.input


def test_there_is_exactly_one_results_list(view):
    from app.ui.widgets.result_table import ResultTable

    assert len(view.findChildren(ResultTable)) == 1


def test_the_repository_is_a_column(view):
    """A flat list has to say where a file came from; a tree said it with the
    row it hung under."""
    assert "repo" in [key for key, *_ in COLUMNS]


def test_the_old_tree_and_the_old_git_panel_are_gone():
    """Deleted, not left unreferenced. Dead code that still imports is code
    somebody maintains for nothing."""
    import importlib

    for name in ("app.ui.widgets.repo_tree", "app.ui.widgets.git_search"):
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module(name)


# --- typing searches the index ---------------------------------------------

def test_typing_asks_the_index_and_fills_the_list(view):
    view.input.setText("cli")
    view._typed()

    assert "cli.py" in rows(view)


def test_what_was_typed_reaches_the_query(view):
    view.input.setText("/repo leasha service /type py")
    view._typed()

    text, repo, ext, _limit = view._store.asked[-1]
    assert text == "service"
    assert repo == "leasha"
    assert ext == ("py",)


def test_a_history_query_does_not_run_on_a_keystroke(view):
    """**The first non-negotiable.** `git log -S` diffs every commit it walks.
    Typing must never start one."""
    before = len(view._store.asked)
    view.input.setText("CustomerId /history")
    view._typed()

    assert len(view._store.asked) == before, "typing started a search anyway"
    assert "Enter" in view.summary.text()


def test_the_summary_names_the_switch_that_made_it_slow(view):
    view.input.setText("x /introduced")
    view._typed()

    assert "/introduced" in view.summary.text()


# --- Enter runs the slow one ------------------------------------------------

def test_enter_with_several_repositories_and_no_name_asks_rather_than_guesses(view):
    """Picking the first would be a guess presented as an answer."""
    view.input.setText("x /history")
    view.start()

    assert "/repo" in view.summary.text()


def test_enter_with_a_named_repository_starts_a_git_run(view, monkeypatch):
    """**`/repo` has to survive the git parse.** git is run *inside* a
    checkout, so which one is the caller's business - but a `/repo` left in the
    free text became part of the pattern, and before that the run could not
    find its repository at all and answered "name a repository first" to a line
    that named one."""
    started: dict = {}
    monkeypatch.setattr("app.ui.code_view.run",
                        lambda _pool, worker: started.setdefault("work", worker))
    view.input.setText("/repo leasha CustomerId /history")
    view.start()

    assert "work" in started, "Enter did not start anything"


def test_enter_on_a_plain_query_searches_the_index(view):
    before = len(view._store.asked)
    view.input.setText("cli")
    view.start()

    assert len(view._store.asked) > before


# --- the pieces every other list has ---------------------------------------

def test_the_preview_pane_is_attached(view):
    """Every list in this application has one - the owner's standing rule."""
    assert view.preview is not None
    assert view.results.split is not None


def test_preferences_live_under_their_own_key():
    assert PREFS_KEY == "ui:code"


def test_the_scope_the_search_tab_offers_still_matches_the_engine():
    """The Code chip on the search tab hands `scope="code"` to the query layer.
    A value that layer does not know is a filter that silently does nothing."""
    offered = {value for _label, value in SCOPES}

    assert "code" in offered
    assert offered <= set(QUERY_SCOPES)


def test_shutdown_is_safe_with_work_in_flight(view):
    view.input.setText("anything")
    view._typed()
    view.shutdown()          # must not raise


# --- empty states -----------------------------------------------------------

def test_no_repositories_but_an_index_explains_what_one_is(qapp):
    widget = CodeView(FakeStore(repos=[], files_total=5_000))
    widget.show()
    widget._repos_read([])

    assert widget.empty.isVisible()
    assert not widget.results.isVisible()


def test_nothing_indexed_at_all_offers_the_indexing_page(qapp):
    widget = CodeView(FakeStore(repos=[], files_total=0))
    widget._repos_read([])
    seen: list = []
    widget.indexing_requested.connect(lambda: seen.append(True))

    widget.empty.linkActivated.emit("x")

    assert seen == [True]


def test_repositories_hide_the_empty_state(view):
    assert not view.empty.isVisible()


# --- the layering rule ------------------------------------------------------

def test_the_engine_choice_is_made_outside_this_view():
    """It is `presenter.code_route`, and it is tested without a display. A
    decision made inside a Qt view is one nobody can execute in CI."""
    import inspect

    from app.ui import code_view

    source = inspect.getsource(code_view)

    assert "code_route(" in source
    assert code_route("x /history").engine == "git"
