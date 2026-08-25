"""The Code tree's rules, without a window.

Every decision the tree makes about *what to show* lives in `presenter.py`, so
it can be argued with here. What is left in `widgets/repo_tree.py` is Qt: items,
columns and signals. This file is the reason that split is worth having - none
of it needs a display, and all of it is what actually goes wrong.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from app.ui.presenter import (
    REPO_FILE_LIMIT, RepoFilter, read_repo_files, repo_file_rows, repo_files_summary,
    repo_filter, repo_filter_summary, repo_rows, repo_visibility,
)


# -- repo_file_rows ----------------------------------------------------------


@dataclass(frozen=True)
class _Record:
    path: str
    ext: str = "py"
    size_bytes: int = 2048
    mtime_ns: int = 1_700_000_000_000_000_000


def test_it_takes_dataclasses_and_dictionaries_alike() -> None:
    """Which one arrives is `read_repo_files`' business, not the tree's."""
    from_object = repo_file_rows([_Record(path=r"D:\Code\leasha\app\main.py")])
    from_mapping = repo_file_rows([{
        "path": r"D:\Code\leasha\app\main.py", "ext": "py",
        "size_bytes": 2048, "mtime_ns": 1_700_000_000_000_000_000,
    }])
    assert from_object == from_mapping


def test_the_name_column_is_the_file_name_not_the_path() -> None:
    """A column of forty identical path prefixes tells you nothing."""
    row = repo_file_rows([_Record(path=r"D:\Code\leasha\app\ui\shell.py")])[0]
    assert row.name == "shell.py"
    assert row.full_path == r"D:\Code\leasha\app\ui\shell.py"


def test_a_row_with_no_path_is_dropped_rather_than_shown_blank() -> None:
    assert repo_file_rows([{"ext": "py"}, _Record(path="/a/b.py")]) == repo_file_rows(
        [_Record(path="/a/b.py")])


def test_it_keeps_the_real_values_so_the_column_sorts_correctly() -> None:
    """The bug `SortableItem` exists for: "10 KB" must not sort below "3 KB"."""
    rows = repo_file_rows([
        _Record(path="/a/big.py", size_bytes=10_000),
        _Record(path="/a/small.py", size_bytes=3_000),
    ])
    assert [row.size_bytes for row in rows] == [10_000, 3_000]
    assert sorted(rows, key=lambda r: r.size_bytes)[0].name == "small.py"


def test_the_extension_is_normalised_because_type_has_to_match_it() -> None:
    row = repo_file_rows([_Record(path="/a/B.PY", ext=".PY")])[0]
    assert row.ext == "py"
    assert RepoFilter(exts=("py",)).matches_file(row.ext, "b.py")


# -- read_repo_files ---------------------------------------------------------


class _StoreWithAccessor:
    def __init__(self) -> None:
        self.asked: list[tuple[int, int]] = []

    def repo_files(self, repo_id: int, *, limit: int) -> list[dict]:
        self.asked.append((repo_id, limit))
        return [{"path": f"/r/{n}.py"} for n in range(3)]

    def iter_files(self, **_kwargs):                     # pragma: no cover
        raise AssertionError("the dedicated accessor should have been used")


class _StoreWithoutAccessor:
    def __init__(self, paths: list[str]) -> None:
        self.paths = paths
        self.kwargs: dict = {}

    def iter_files(self, **kwargs):
        self.kwargs = kwargs
        return iter([_Record(path=path) for path in self.paths])


def _repo(**overrides):
    row = {"id": 7, "name": "leasha", "kind": "git",
           "root_path": r"D:\Code\leasha", "files": 3}
    row.update(overrides)
    return repo_rows([row])[0]


def test_it_prefers_the_indexed_column_when_the_store_has_one() -> None:
    store = _StoreWithAccessor()
    found = read_repo_files(store, _repo(), limit=10)
    assert len(found) == 3
    # One over the limit, so the caller can tell "exactly 10" from "more".
    assert store.asked == [(7, 11)]


def test_it_falls_back_to_a_prefix_walk_when_the_store_predates_it() -> None:
    store = _StoreWithoutAccessor([
        r"D:\Code\leasha\app\main.py",
        r"D:\Code\other\thing.py",
        r"D:\Code\leasha\README.md",
    ])
    found = read_repo_files(store, _repo())
    assert [record.path for record in found] == [
        r"D:\Code\leasha\app\main.py", r"D:\Code\leasha\README.md"]


def test_the_fallback_excludes_mail_in_sql_not_in_python() -> None:
    """200,000 messages must not become 200,000 objects to be discarded."""
    store = _StoreWithoutAccessor([])
    read_repo_files(store, _repo())
    assert store.kwargs == {"source_kind": "file"}


def test_the_fallback_matches_paths_windows_spells_two_ways() -> None:
    """`D:\\Code\\Leasha` and `d:/code/leasha` are the same folder."""
    store = _StoreWithoutAccessor(["d:/code/leasha/app/main.py"])
    assert len(read_repo_files(store, _repo(root_path=r"D:\Code\Leasha\\"))) == 1


def test_a_repository_with_no_root_asks_the_store_nothing() -> None:
    store = _StoreWithoutAccessor(["/anything.py"])
    assert read_repo_files(store, _repo(root_path="", name="x")) == []


def test_the_walk_stops_one_past_the_limit() -> None:
    store = _StoreWithoutAccessor([f"/r/{n}.py" for n in range(50)])
    found = read_repo_files(store, _repo(root_path="/r"), limit=10)
    assert len(found) == 11              # 10 to show, 1 to know there are more


def test_the_dedicated_accessor_is_skipped_without_an_id() -> None:
    """A repository row from a store with no id column still lists its files."""
    class Both(_StoreWithoutAccessor):
        def repo_files(self, repo_id, *, limit):         # pragma: no cover
            raise AssertionError("called with no id")

    store = Both([r"D:\Code\leasha\a.py"])
    assert len(read_repo_files(store, _repo(id=0))) == 1


# -- the filter --------------------------------------------------------------


def test_type_is_honoured_now_that_files_are_listed() -> None:
    """It used to be reported as ignored, correctly - there were no files."""
    chosen = repo_filter("/type py")
    assert chosen.exts == ("py",)
    assert "type" not in chosen.ignored


def test_name_is_free_text_because_the_tree_matches_on_the_name_column() -> None:
    assert repo_filter("/name utils").text == repo_filter("utils").text == "utils"


@pytest.mark.parametrize("operator", ["from:dave", "after:2024", "has:attachment"])
def test_operators_a_tree_of_files_cannot_answer_are_named_not_dropped(operator) -> None:
    chosen = repo_filter(operator)
    assert chosen.ignored
    assert chosen.ignored[0] in repo_filter_summary(
        chosen, shown=2, total=2, files=9)


def test_the_summary_explains_why_a_repository_survives_a_type_filter() -> None:
    """Otherwise an expanded repository showing nothing reads as a bug."""
    summary = repo_filter_summary(repo_filter("/type rs"), shown=2, total=2, files=9)
    assert ".rs files only" in summary


# -- repo_visibility ---------------------------------------------------------


_FILES = [("py", "main.py py app/main.py"), ("md", "readme.md md readme.md")]


def test_a_repository_that_matches_shows_all_of_its_files() -> None:
    """Having found it by name, being filtered again is not what was asked."""
    visible, flags = repo_visibility(
        repo_filter("leasha"), "leasha", "leasha git d:/code/leasha", _FILES)
    assert visible and flags == [True, True]


def test_a_repository_that_does_not_match_survives_if_one_of_its_files_does() -> None:
    """This is what makes the tree searchable rather than merely collapsible."""
    visible, flags = repo_visibility(
        repo_filter("readme"), "leasha", "leasha git d:/code/leasha", _FILES)
    assert visible and flags == [False, True]


def test_a_repository_with_no_match_anywhere_goes() -> None:
    visible, flags = repo_visibility(
        repo_filter("nothing"), "leasha", "leasha git d:/code/leasha", _FILES)
    assert not visible and flags == [False, False]


def test_repo_hides_the_whole_subtree() -> None:
    """Naming a repository is a statement about which one you want."""
    visible, flags = repo_visibility(
        repo_filter("repo:other"), "leasha", "leasha git d:/code", _FILES)
    assert not visible and flags == [False, False]


def test_type_narrows_the_children_and_leaves_the_parent_alone() -> None:
    """The tree cannot know what a collapsed repository contains."""
    visible, flags = repo_visibility(
        repo_filter("/type py"), "leasha", "leasha git d:/code", _FILES)
    assert visible and flags == [True, False]


def test_an_empty_filter_shows_everything() -> None:
    visible, flags = repo_visibility(repo_filter(""), "leasha", "leasha", _FILES)
    assert visible and flags == [True, True]


def test_a_collapsed_repository_is_judged_on_its_own_row() -> None:
    """With no children loaded there is nothing else to judge it on."""
    assert repo_visibility(repo_filter("leasha"), "leasha", "leasha git", ())[0]
    assert not repo_visibility(repo_filter("zzz"), "leasha", "leasha git", ())[0]


def test_the_truncation_note_points_at_the_search_box() -> None:
    """A tree is for looking; fifty thousand rows is the search box's job."""
    note = repo_files_summary(48_301)
    assert "48,301" in note and str(REPO_FILE_LIMIT) in note.replace(",", "")
