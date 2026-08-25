"""The Code tab: a repository browser that hands its selection to search.

Layer: L5

U1-U12 from `docs/WORKORDER-git-search-ui.md` §9.

The tab exists because source code was **already** searchable - twenty
extensions since Layer 2, with snippets and reranking - and the one thing the
application could not do was say which repository a result came from. So the
tests worth writing are about the bridge and about not reinventing search: the
tab has no search box, invents no filter, and hands a repository name to the one
grammar every other tab already speaks.
"""

from __future__ import annotations

import os

import pytest

from app.search.query import SCOPES as QUERY_SCOPES
from app.search.query import parse_query
from app.ui.presenter import repo_rows, repo_summary

# Everything below needs Qt. `search_bar` does too - it holds SCOPES beside the
# combo it builds - so the import has to sit under the skip, not above it.
pytest.importorskip("PyQt6.QtWidgets", exc_type=ImportError)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import Qt  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from app.ui.code_view import COLUMNS, PREFS_KEY, CodeView  # noqa: E402
from app.ui.widgets.repo_tree import ROW_ROLE as _ROW_ROLE  # noqa: E402
from app.ui.widgets.search_bar import (  # noqa: E402
    SCOPES, build_scope, select_scope,
)


@pytest.fixture(scope="module")
def qapp():
    yield QApplication.instance() or QApplication([])


class FakeStore:
    """Only the two calls the view makes."""

    def __init__(self, repos=None, files_total: int = 100):
        self._repos = repos if repos is not None else [
            {"id": 1, "name": "leasha", "kind": "work",
             "root_path": "D:/SearchProject", "last_seen": 1756000000, "files": 9},
            {"id": 2, "name": "my tools", "kind": "submodule",
             "root_path": "D:/code/my tools", "last_seen": 1756100000, "files": 10},
        ]
        self._files_total = files_total
        self.calls = 0

    def repos_list(self):
        self.calls += 1
        return list(self._repos)

    def stats(self):
        return {"files_total": self._files_total}

    def get_state(self, _key, default=""):
        return default

    def set_states(self, _values):
        return None

    def all_state(self):
        return {}


@pytest.fixture()
def view(qapp):
    widget = CodeView(FakeStore())
    widget._show(widget._store.repos_list(), widget._generation)
    return widget


def tops(view) -> list:
    """The repository rows. A tree indexes by item, not by row number."""
    return [view.results.topLevelItem(n)
            for n in range(view.results.topLevelItemCount())]


def visible(view) -> list:
    return [item for item in tops(view) if not item.isHidden()]


def kids(item) -> list:
    return [item.child(n) for n in range(item.childCount())]


# --- U10, U9: the scope chip -----------------------------------------------

def test_code_only_is_offered_and_matches_the_engine():
    """U10. The chip's value has to be one the query layer knows."""
    values = [value for _label, value in SCOPES]

    assert "code" in values
    assert set(values) <= set(QUERY_SCOPES), "a chip the engine cannot honour"


def test_the_scope_labels_read_as_narrowing_not_switching():
    labels = [label for label, _value in SCOPES]
    assert labels[0] == "Everything", "the union is first and is the default"
    assert "Code only" in labels


def test_selecting_a_scope_by_value(qapp):
    """U9, both halves."""
    combo = build_scope(None, lambda _i: None)

    select_scope(combo, "code")
    assert combo.currentData() == "code"

    select_scope(combo, "nonsense")
    assert combo.currentData() == "code", "an unknown value must change nothing"


def test_the_tooltip_explains_the_surprising_part(qapp):
    """"Code only" means *in a repository*, not *looks like code*. Nobody would
    guess that, and the two questions stay separately answerable."""
    tip = build_scope(None, lambda _i: None).toolTip()

    assert "repository" in tip.lower()
    assert "type:code" in tip, "say where the other question is answered"


# --- U7, and the formatting ------------------------------------------------

def test_repositories_sort_numerically_not_lexically(view):
    """U7. Nine below ten. `SortableItem` reads the real count."""
    from app.ui.widgets.sortable_item import SORT_ROLE

    files_column = [key for key, *_ in COLUMNS].index("files")
    values = [item.data(files_column, SORT_ROLE) for item in tops(view)]

    assert values == [9, 10] or values == [10, 9]
    assert all(isinstance(value, int) for value in values)


def test_a_kind_becomes_a_word():
    rows = repo_rows([{"name": "x", "kind": "submodule", "root_path": "D:/x",
                       "files": 1, "last_seen": 0}])
    assert rows[0].kind == "Submodule"


def test_an_unknown_kind_still_reads_as_something():
    rows = repo_rows([{"name": "x", "kind": "", "root_path": "D:/x",
                       "files": 0, "last_seen": 0}])
    assert rows[0].kind == "Repository"


def test_the_summary_counts_both_things():
    assert repo_summary(12, 48301) == "12 repositories, 48,301 files indexed"
    assert repo_summary(1, 1) == "1 repository, 1 file indexed"
    assert repo_summary(0, 0) == ""


def test_a_repository_with_no_indexed_files_is_still_listed():
    """The store uses a LEFT JOIN for this; the row must survive formatting."""
    rows = repo_rows([{"name": "empty", "kind": "work", "root_path": "D:/e",
                       "files": 0, "last_seen": 0}])
    assert rows[0].files == "0"


# --- U2: nothing on the interface thread ------------------------------------

def test_loading_happens_on_a_worker(view, monkeypatch):
    """U2. Asserted at the call site, as the other view tests do."""
    import app.ui.code_view as module

    started = {}

    class Recorder:
        def __init__(self, work, *args, **kwargs):
            started["work"] = work
            self.signals = type("S", (), {
                "finished": type("X", (), {"connect": lambda *_a: None})(),
                "failed": type("X", (), {"connect": lambda *_a: None})(),
            })()

    monkeypatch.setattr(module, "CallableWorker", Recorder)
    monkeypatch.setattr(module, "run", lambda _pool, _worker: None)

    view.refresh()

    assert started["work"] == view._store.repos_list, (
        "repos_list must be handed to a worker, not called here"
    )


# --- U3: the bridge ---------------------------------------------------------

def test_enter_asks_to_search_the_selected_repository(view):
    """U3, the keyboard half - a keyboard user must never need the mouse."""
    from PyQt6.QtGui import QKeyEvent

    seen: list[str] = []
    view.search_repo_requested.connect(seen.append)
    view.results.setCurrentItem(tops(view)[0])

    view.results.keyPressEvent(QKeyEvent(
        QKeyEvent.Type.KeyPress, Qt.Key.Key_Return, Qt.KeyboardModifier.NoModifier))

    assert seen and seen[0] in {"leasha", "my tools"}


def test_double_click_asks_for_the_same_thing(view):
    seen: list[str] = []
    view.search_repo_requested.connect(seen.append)

    view.results.itemDoubleClicked.emit(tops(view)[1], 0)

    assert len(seen) == 1


def test_enter_with_nothing_selected_does_nothing(view):
    from PyQt6.QtGui import QKeyEvent

    seen: list[str] = []
    view.search_repo_requested.connect(seen.append)
    view.results.clearSelection()

    view.results.keyPressEvent(QKeyEvent(
        QKeyEvent.Type.KeyPress, Qt.Key.Key_Return, Qt.KeyboardModifier.NoModifier))

    assert not seen


def test_the_name_comes_from_the_row_not_its_position(view):
    """The tree is sortable, so visual row 3 is not `self._rows[3]` after a
    header click - acting on the wrong repository is the bug nobody reports
    because they assume they misclicked."""
    from app.ui.widgets.repo_tree import ROW_ROLE

    view.results.sortByColumn(0, Qt.SortOrder.DescendingOrder)
    first = view.results.topLevelItem(0)
    view.results.setCurrentItem(first)

    assert view.selected_repo() == first.data(0, ROW_ROLE).name


# --- U5: quoting ------------------------------------------------------------

def test_a_repository_name_with_a_space_survives_the_round_trip():
    """U5. Unquoted, the filter would end at the first space and match a
    different repository, or none, with nothing on screen to explain it."""
    parsed = parse_query('repo:"my tools" pump station')

    assert parsed.repos == ("my tools",)
    assert "pump" in parsed.text


def test_an_unquoted_two_word_name_does_not_silently_work():
    """The reason the bridge quotes: this is what happens if it forgets."""
    parsed = parse_query("repo:my tools pump")

    assert parsed.repos == ("my",), "the value ends at the space, as expected"


# --- U6: the empty states ---------------------------------------------------

def test_no_repositories_but_an_index_names_git_and_roots(qapp):
    """U6, the state that matters. A generic "no results" would waste it."""
    widget = CodeView(FakeStore(repos=[], files_total=5000))
    widget._show([], widget._generation)

    text = widget.empty.text()
    assert widget.empty.isVisible()
    assert ".git" in text
    assert "indexed root" in text or "indexed folders" in text


def test_nothing_indexed_at_all_routes_to_indexing(qapp):
    widget = CodeView(FakeStore(repos=[], files_total=0))
    widget._show([], widget._generation)

    seen: list[bool] = []
    widget.indexing_requested.connect(lambda: seen.append(True))

    assert "Nothing is indexed" in widget.empty.text()
    widget.empty.linkActivated.emit("#index")
    assert seen, "the empty state has to be a route, not a dead end"


def test_repositories_hide_the_empty_state(view):
    assert not view.empty.isVisible()
    assert view.results.isVisible()
    assert "2 repositories" in view.summary.text()


# --- U8, U11, U12 -----------------------------------------------------------

def test_preferences_live_under_their_own_key():
    """U8. A shared key would make the Files columns follow the Code ones."""
    assert PREFS_KEY == "ui:code"


def test_shutdown_is_safe_with_a_load_in_flight(view):
    """U11. Closing the window mid-load must raise nothing."""
    view.refresh()
    view.shutdown()

    # A result landing after shutdown is stale and must be dropped.
    before = view.results.topLevelItemCount()
    view._show([{"name": "late", "kind": "work", "root_path": "D:/l",
                 "files": 1, "last_seen": 0}], generation=-1)
    assert view.results.topLevelItemCount() == before


def test_the_presenter_still_imports_no_qt():
    """U12. The whole reason the formatting is testable."""
    from pathlib import Path

    source = Path(__file__).resolve().parents[2] / "app" / "ui" / "presenter.py"
    assert "PyQt6" not in source.read_text(encoding="utf-8")


def test_the_filter_narrows_the_list(qapp, view):
    """§11 refused a search box here. The owner overrode it: Files and Mail
    both filter, and a list you cannot narrow is the odd one out.

    It is not the thing §11 refused. It runs over rows already on screen and
    invents no filter - searching *inside* a repository is still Enter, still
    the one grammar, still the search tab.
    """
    view.input.setText("tools")

    assert len(visible(view)) == 1, "only the matching repository stays"
    assert "1 of 2" in view.summary.text()


def test_the_filter_matches_kind_and_location_too(qapp, view):
    """Somebody typing "sub" may mean a submodule or a folder, and making them
    choose which column they meant is precision that only sounds helpful."""
    view.input.setText("submodule")
    assert len(visible(view)) == 1

    view.input.setText("SearchProject")       # a location
    assert len(visible(view)) == 1


def test_a_filter_matching_nothing_says_so(qapp, view):
    view.input.setText("no-such-repository")

    assert "No repository matches" in view.summary.text()
    assert not visible(view)


def test_clearing_the_filter_restores_the_count(qapp, view):
    view.input.setText("tools")
    view.input.setText("")

    assert "2 repositories" in view.summary.text()
    assert len(visible(view)) == 2


def test_a_refresh_keeps_what_was_typed(qapp, view):
    """Reloading the list must not silently drop a filter somebody is using."""
    view.input.setText("tools")
    view._show(view._store.repos_list(), view._generation)

    assert view.input.text() == "tools"
    assert "1 of 2" in view.summary.text()


def test_the_filter_is_hidden_when_there_is_nothing_to_narrow(qapp):
    """An empty tab with a filter box invites typing into a list that does not
    exist, and the empty state is the thing worth reading."""
    widget = CodeView(FakeStore(repos=[], files_total=5000))
    widget._show([], widget._generation)

    assert not widget.input.isVisible()


# --- the files inside a repository ------------------------------------------

def test_a_repository_with_files_can_be_opened(view):
    """The placeholder child is what draws the expand arrow.

    Without one Qt draws a leaf, there is nothing to click, and the lazy load
    is unreachable - the tab would be a flat list again with extra machinery.
    """
    assert all(item.childCount() == 1 for item in tops(view))


def test_an_empty_repository_has_nothing_to_open(view):
    """No arrow on a row that would open onto nothing."""
    view._show([{"id": 3, "name": "bare", "kind": "work", "root_path": "D:/b",
                 "files": 0, "last_seen": 0}], view._generation)
    assert tops(view)[0].childCount() == 0


def test_expanding_asks_a_worker_for_the_files(view, monkeypatch):
    """U2 again, and it matters more here: this one reads the file table."""
    import app.ui.code_view as module

    started = {}

    class Recorder:
        def __init__(self, work, *args, **kwargs):
            started["work"] = work
            started["args"] = args
            self.signals = type("S", (), {
                "finished": type("X", (), {"connect": lambda *_a: None})(),
                "failed": type("X", (), {"connect": lambda *_a: None})(),
            })()

    monkeypatch.setattr(module, "CallableWorker", Recorder)
    monkeypatch.setattr(module, "run", lambda _pool, _worker: None)

    view.results.expandItem(tops(view)[0])

    assert started["work"] is module.read_repo_files
    assert started["args"][0] is view._store


def test_the_files_arrive_as_children(view):
    from app.ui.presenter import repo_file_rows

    parent = tops(view)[0]
    view.results.set_files(parent.data(0, _ROW_ROLE).name, repo_file_rows([
        {"path": "D:/SearchProject/app/main.py", "ext": "py",
         "size_bytes": 2048, "mtime_ns": 1_700_000_000_000_000_000},
    ]))

    assert [child.text(0) for child in kids(parent)] == ["main.py"]


def test_a_second_expansion_does_not_ask_again(view, monkeypatch):
    """Loaded once, kept. Re-reading on every collapse would be a stutter."""
    from app.ui.presenter import repo_file_rows

    parent = tops(view)[0]
    name = parent.data(0, _ROW_ROLE).name
    view.results.set_files(name, repo_file_rows([{"path": "D:/a.py", "ext": "py"}]))

    asked: list = []
    view.results.files_requested.connect(asked.append)
    view.results.expandItem(parent)

    assert not asked


def test_a_repository_with_no_indexed_files_says_so_rather_than_nothing(view):
    """An arrow that opens onto an empty space is indistinguishable from a bug."""
    parent = tops(view)[0]
    view.results.set_files(parent.data(0, _ROW_ROLE).name, [])

    assert "No indexed files" in kids(parent)[0].text(0)


def test_a_failure_to_list_files_is_shown_in_the_row_that_has_none(view):
    parent = tops(view)[0]
    view.results.failed(parent.data(0, _ROW_ROLE).name, "disk gone")

    assert "disk gone" in kids(parent)[0].text(0)


def test_a_truncated_repository_points_at_the_search_box(view):
    """Past the cap the tree stops and says where the rest is."""
    from app.ui.presenter import repo_file_rows

    parent = tops(view)[0]
    view.results.set_files(
        parent.data(0, _ROW_ROLE).name,
        repo_file_rows([{"path": f"D:/a{n}.py", "ext": "py"} for n in range(3)]),
        truncated=48_301,
    )

    note = kids(parent)[-1]
    assert "48,301" in note.text(0)
    assert not (note.flags() & Qt.ItemFlag.ItemIsSelectable), "a note, not a row"


def test_activating_a_file_asks_to_open_it_not_to_search(view):
    """The obvious next thing differs by level, so one key does both."""
    from app.ui.presenter import repo_file_rows

    parent = tops(view)[0]
    view.results.set_files(parent.data(0, _ROW_ROLE).name, repo_file_rows([
        {"path": "D:/SearchProject/app/main.py", "ext": "py"}]))

    opened: list[str] = []
    searched: list[str] = []
    view.open_requested.connect(opened.append)
    view.search_repo_requested.connect(searched.append)

    view.results.itemDoubleClicked.emit(kids(parent)[0], 0)

    assert opened == ["D:/SearchProject/app/main.py"]
    assert not searched


def test_a_file_still_knows_which_repository_it_is_in(view):
    """So a file's menu can offer to search the repository holding it."""
    from app.ui.presenter import repo_file_rows

    parent = tops(view)[0]
    name = parent.data(0, _ROW_ROLE).name
    view.results.set_files(name, repo_file_rows([{"path": "D:/a.py", "ext": "py"}]))
    view.results.setCurrentItem(kids(parent)[0])

    assert view.selected_repo() == name


def test_a_filter_reaches_the_files_and_keeps_their_parent(view):
    """Typing "readme" must find the README, and hiding its parent hides it."""
    from app.ui.presenter import repo_file_rows

    parent = tops(view)[0]
    view.results.set_files(parent.data(0, _ROW_ROLE).name, repo_file_rows([
        {"path": "D:/SearchProject/README.md", "ext": "md"},
        {"path": "D:/SearchProject/app/main.py", "ext": "py"},
    ]))

    view.input.setText("readme")

    assert parent in visible(view)
    assert [child.isHidden() for child in kids(parent)] == [False, True]


def test_children_arriving_late_obey_what_was_already_typed(view):
    """The filter was applied before the worker came back; it must still hold."""
    from app.ui.presenter import repo_file_rows

    parent = tops(view)[0]
    view.input.setText("readme")
    view._show_files(
        parent.data(0, _ROW_ROLE),
        [{"path": "D:/SearchProject/app/main.py", "ext": "py"}],
        view._generation,
    )

    assert all(child.isHidden() for child in kids(parent) if child.data(0, _ROW_ROLE))


def test_files_arriving_after_a_refresh_are_dropped(view):
    """Stale by a generation, exactly as a stale repository list is."""
    parent = tops(view)[0]
    view._show_files(parent.data(0, _ROW_ROLE), [{"path": "D:/late.py"}], -1)

    assert parent.childCount() == 1                  # still the placeholder


def test_the_slash_menu_offers_what_this_tab_can_honour(view):
    """It offered nothing at all first, then everything. Both were wrong."""
    from app.ui.presenter import CODE_COMMANDS

    view._popup.set_prefix("")
    assert {c.name for c in view._popup._matches} == set(CODE_COMMANDS)


def test_no_repository_management_controls(qapp, view):
    """Also §11: repositories are found by the walk, not registered."""
    from PyQt6.QtWidgets import QPushButton

    labels = [b.text().lower() for b in view.findChildren(QPushButton)]
    for forbidden in ("add", "remove", "browse"):
        assert not any(forbidden in label for label in labels), labels
