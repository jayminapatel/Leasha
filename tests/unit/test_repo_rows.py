"""Repository formatting and the `repo:` round trip, without a display.

Layer: L5

Split out of `test_code_view.py` because that module needs Qt and this does
not. The quoting test in particular has to run everywhere: it is the one
guarding a filter that would otherwise match a *different* repository, silently,
with nothing on screen to explain why.
"""

from __future__ import annotations

from app.search.query import SCOPES, parse_query
from app.ui.presenter import repo_rows, repo_summary


# --- formatting -------------------------------------------------------------

def test_a_kind_becomes_a_word():
    rows = repo_rows([{"name": "x", "kind": "submodule", "root_path": "D:/x",
                       "files": 1, "last_seen": 0}])
    assert rows[0].kind == "Submodule"


def test_an_unknown_kind_still_reads_as_something():
    rows = repo_rows([{"name": "x", "kind": "", "root_path": "D:/x",
                       "files": 0, "last_seen": 0}])
    assert rows[0].kind == "Repository"


def test_a_repository_with_no_indexed_files_is_still_listed():
    """The store uses a LEFT JOIN so a detected-but-unindexed repository still
    exists. Dropping it here would make the tab disagree with the walker for
    reasons nobody could see."""
    rows = repo_rows([{"name": "empty", "kind": "work", "root_path": "D:/e",
                       "files": 0, "last_seen": 0}])
    assert rows[0].files == "0"
    assert rows[0].file_count == 0


def test_a_missing_name_falls_back_to_the_folder():
    rows = repo_rows([{"name": "", "kind": "work",
                       "root_path": "D:/code/leasha", "files": 3, "last_seen": 0}])
    assert rows[0].name == "leasha"


def test_counts_are_kept_unformatted_for_sorting():
    """"9" must sort below "10", which needs the number, not the text."""
    rows = repo_rows([
        {"name": "a", "kind": "work", "root_path": "D:/a", "files": 9, "last_seen": 0},
        {"name": "b", "kind": "work", "root_path": "D:/b", "files": 10, "last_seen": 0},
    ])
    assert [row.file_count for row in rows] == [9, 10]
    assert [row.files for row in rows] == ["9", "10"]


def test_a_large_count_is_grouped_for_reading():
    rows = repo_rows([{"name": "a", "kind": "work", "root_path": "D:/a",
                       "files": 48301, "last_seen": 0}])
    assert rows[0].files == "48,301"


def test_last_seen_accepts_whatever_the_store_stored():
    """Seconds, nanoseconds or an ISO string - a column read through
    `dict(row)` arrives as whatever SQLite held, and a browser panel is not the
    place to discover a type mismatch."""
    for value in (1756000000, 1756000000 * 1_000_000_000, "2026-08-25T10:00:00"):
        rows = repo_rows([{"name": "a", "kind": "work", "root_path": "D:/a",
                           "files": 1, "last_seen": value}])
        assert rows[0].seen, f"{value!r} produced no date"
        assert rows[0].seen_at > 0


def test_no_last_seen_is_blank_rather_than_1970():
    rows = repo_rows([{"name": "a", "kind": "work", "root_path": "D:/a",
                       "files": 1, "last_seen": None}])
    assert rows[0].seen == ""


def test_the_full_path_is_kept_even_though_the_column_is_shortened():
    """The column elides; the menu and the tooltip need the real thing."""
    long_root = "D:/very/deeply/nested/" + ("folder/" * 12) + "repo"
    rows = repo_rows([{"name": "repo", "kind": "work", "root_path": long_root,
                       "files": 1, "last_seen": 0}])

    assert rows[0].root == long_root
    assert len(rows[0].path) < len(long_root)


def test_the_summary_counts_both_things():
    assert repo_summary(12, 48301) == "12 repositories, 48,301 files indexed"
    assert repo_summary(1, 1) == "1 repository, 1 file indexed"
    assert repo_summary(0, 0) == ""


# --- the bridge's contract with the grammar ---------------------------------

def test_a_repository_name_with_a_space_survives_the_round_trip():
    """U5, and the reason `_search_repo` quotes the name."""
    parsed = parse_query('repo:"my tools" pump station')

    assert parsed.repos == ("my tools",)
    assert "pump" in parsed.text


def test_an_unquoted_two_word_name_ends_at_the_space():
    """What happens if the bridge forgets to quote: a filter that matches a
    different repository, or none, and looks perfectly deliberate."""
    assert parse_query("repo:my tools pump").repos == ("my",)


def test_several_repositories_at_once():
    """Comma-separated, which is the form the `/repo` value hint advertises."""
    assert parse_query("repo:leasha,tools").repos == ("leasha", "tools")


def test_code_is_a_scope_the_engine_knows():
    """The chip's value must be one `scoped()` accepts, or the tab hands the
    search box something it will ignore."""
    assert "code" in SCOPES
    assert parse_query("pump").scoped("code").scope == "code"


def test_scope_and_type_stay_different_questions():
    """`type:code` filters by extension; the `code` scope means "in a
    repository". Both stay available, and conflating them would take one away."""
    parsed = parse_query("type:code pump")

    assert parsed.scope != "code", "type: must not set the scope"
    assert parsed.ext, "type:code still filters by extension"
