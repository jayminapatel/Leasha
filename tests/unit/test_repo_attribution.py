r"""Undoing a repository, and the accident that showed it could not be undone.

Layer: L1/L3/L5

From `docs/WORKORDER-202626081149-code-tab.md`. The owner reported a UI symptom -
*"in git view i dont see the files"* - and investigating it produced a
data-integrity finding instead.

A copy of this project's own `.git` had been dragged into `D:\SearchData`, a
document archive of PDFs, PSTs and Visio files, with its working tree emptied.
Detection found the `.git` and adopted the folder, so **1,179 of 2,677 indexed
files — 44% of the corpus — were attributed to a repository**. `scope:code` is
`repo_id IS NOT NULL`, so "Code only" matched the whole archive; two of the four
repositories held zero files, so selecting either showed an empty list with no
explanation.

**§2 is the finding: attribution was a one-way door.** Three independent
mechanisms each prevented recovery, and all three had to be fixed:

1. nothing pruned `repos`, so a repository whose `.git` had gone kept its row;
2. nothing ever set `files.repo_id` back to NULL;
3. `repo_id = COALESCE(excluded.repo_id, files.repo_id)` meant a NULL could not
   overwrite an attribution - so even `index --force` would not clear it.

Mechanism 3 is defensible alone; its comment explains that it stops callers
knowing nothing about repositories from blanking a good attribution. Combined
with the other two it became a trap, and **the only route back was deleting the
whole index** - which is what the owner did, for a bookkeeping error.

A7 is written first, as the order asks: *"the whole incident, reduced to a
fixture."*
"""

from __future__ import annotations

import pytest

from app.index.repo_health import (
    BIG_ENOUGH_TO_JUDGE,
    CODE_SHARE_FLOOR,
    code_share,
    describe,
    suspicion,
)
from app.storage.sqlite_store import NO_REPO, FileStatus, SqliteStore


def _generation(store) -> int:
    row = store.conn.execute(
        "SELECT generation FROM index_generation WHERE id = 1").fetchone()
    return int(row["generation"])


@pytest.fixture()
def archive(tmp_path):
    r"""`D:\SearchData` as it was: a document archive wearing a `.git`."""
    with SqliteStore(tmp_path / "index.db") as store:
        repo_id = store.upsert_repo(r"D:\SearchData", name="SearchData", kind="work")
        for number in range(12):
            store.upsert_file(
                rf"D:\SearchData\report {number}.pdf", size_bytes=1_000,
                mtime_ns=1, ext="pdf", status=FileStatus.INDEXED,
                source_kind="file", repo_id=repo_id)
        yield store


# --- A7: the incident, as a fixture -----------------------------------------

def test_a7_an_archive_of_documents_is_reported_rather_than_silently_adopted():
    r"""**1,179 files, almost none of them code.**

    The application knew: `app.cli repos` has printed *"`scope:code` will match
    your whole corpus rather than just code"* since repositories were added. It
    said so in a place nobody was reading and adopted the folder anyway.
    Adopt-and-warn is fine; silent adoption is not.
    """
    reason = suspicion(files=1179, code_files=11)

    assert reason
    assert "1,179" in reason


def test_a7_a_tree_that_reads_as_deleted_is_the_strongest_signal():
    """A checkout nobody has is not a checkout. This is what was actually true
    of the stray `.git`: every tracked file reported as deleted."""
    assert suspicion(files=1179, code_files=900, tree_deleted=True)


def test_an_ordinary_checkout_is_adopted_without_comment():
    """The failure mode to avoid is a warning on every repository, which is how
    the existing one came to be ignored."""
    assert suspicion(files=400, code_files=380) == ""
    assert suspicion(files=400, code_files=380, tree_deleted=False) == ""


def test_a_small_repository_is_not_judged():
    """Four files that happen to be `.md` is not evidence of anything."""
    assert suspicion(files=BIG_ENOUGH_TO_JUDGE - 1, code_files=0) == ""


def test_a_documentation_heavy_repository_is_still_a_repository():
    r"""The floor is deliberately low. Plenty of real checkouts are more
    documentation than source, and this asks "is there any code here at all",
    not "does this look tidy"."""
    files = 200
    just_over = int(files * CODE_SHARE_FLOOR) + 1

    assert suspicion(files=files, code_files=just_over) == ""


def test_the_warning_names_the_way_out():
    """A warning with no next step is the one that gets ignored."""
    said = describe("SearchData", r"D:\SearchData",
                    suspicion(files=1179, code_files=11))

    assert "repos --forget" in said and r"D:\SearchData" in said


def test_nothing_is_said_when_there_is_nothing_to_say():
    assert describe("leasha", r"D:\code\leasha", "") == ""


def test_the_share_is_measured_from_the_extensions_seen():
    assert code_share(["py", "py", "md", "pdf"], ["py", "cs"]) == 0.5
    assert code_share(["pdf", "docx"], ["py"]) == 0.0


# --- §2.3: an explicit way to mean "no repository" --------------------------

def test_a_caller_with_no_opinion_still_cannot_blank_an_attribution(archive):
    r"""**Mechanism 3, and it is right.** `_record_skip`, the PST path and every
    test pass no `repo_id`; if that blanked the column, an ordinary skip would
    quietly un-attribute a file the indexer had just placed."""
    path = r"D:\SearchData\report 0.pdf"
    archive.upsert_file(path, size_bytes=1_000, mtime_ns=2, ext="pdf",
                        status=FileStatus.INDEXED, source_kind="file")

    row = archive.conn.execute(
        "SELECT repo_id FROM files WHERE path = ?", (path,)).fetchone()
    assert row["repo_id"] is not None


def test_no_repo_clears_it_deliberately(archive):
    r"""**A defensive guard that cannot be overridden is not a guard, it is a
    one-way door.** This is the handle."""
    path = r"D:\SearchData\report 0.pdf"
    archive.upsert_file(path, size_bytes=1_000, mtime_ns=3, ext="pdf",
                        status=FileStatus.INDEXED, source_kind="file",
                        repo_id=NO_REPO)

    row = archive.conn.execute(
        "SELECT repo_id FROM files WHERE path = ?", (path,)).fetchone()
    assert row["repo_id"] is None


def test_the_sentinel_never_reaches_the_foreign_key(tmp_path):
    """`repo_id` references `repos(id)`, so a literal -1 in the column would be
    refused on insert. It travels as a flag and binds as NULL."""
    with SqliteStore(tmp_path / "index.db") as store:
        store.upsert_file(r"D:\loose\a.txt", size_bytes=1, mtime_ns=1,
                          ext="txt", status=FileStatus.INDEXED,
                          source_kind="file", repo_id=NO_REPO)

        row = store.conn.execute(
            "SELECT repo_id FROM files WHERE path = ?",
            (r"D:\loose\a.txt",)).fetchone()
        assert row["repo_id"] is None


# --- A1: forgetting -----------------------------------------------------------

def test_a1_forgetting_releases_the_files_and_keeps_them_indexed(archive):
    r"""**Nothing is deleted and nothing is re-indexed.**

    Every file keeps its row, its chunks and its vectors; what it loses is the
    claim that it is code. That is what makes this a one-line command rather
    than a reset - the expensive half of the accident was the re-index.
    """
    released = archive.forget_repo(r"D:\SearchData")

    assert released == 12
    assert archive.conn.execute("SELECT COUNT(*) AS n FROM files").fetchone()["n"] == 12
    left = archive.conn.execute(
        "SELECT COUNT(*) AS n FROM files WHERE repo_id IS NOT NULL").fetchone()
    assert left["n"] == 0


def test_a1_the_repository_row_goes_too(archive):
    """Mechanism 1: a repository nothing prunes keeps its row for ever, and the
    tree keeps offering a selection that produces an empty list."""
    archive.forget_repo(r"D:\SearchData")

    assert archive.repos_list() == []


def test_a1_the_generation_is_bumped_so_the_cache_cannot_lie(archive):
    r"""**Without this the first search after forgetting still answers from the
    old attribution**, which reads as the command having done nothing."""
    before = _generation(archive)

    archive.forget_repo(r"D:\SearchData")

    assert _generation(archive) > before


def test_forgetting_something_unknown_is_harmless(archive):
    assert archive.forget_repo(r"D:\not\here") == 0
    assert archive.forget_repo("") == 0


def test_a1_a_forgotten_root_is_remembered_as_ignored(archive):
    r"""**Otherwise the next walk undoes it.** Finding a `.git` is the whole of
    how a repository is registered, so "I have looked at this and it is not a
    checkout" has to be recorded somewhere detection reads."""
    archive.ignore_repo_root(r"D:\SearchData")

    assert archive.ignored_repo_roots() == [r"D:\SearchData"]


def test_ignoring_is_reversible(archive):
    """A setting that can only be added to is how a corpus quietly shrinks."""
    archive.ignore_repo_root(r"D:\SearchData")

    assert archive.unignore_repo_root(r"d:\searchdata") is True
    assert archive.ignored_repo_roots() == []
    assert archive.unignore_repo_root(r"D:\SearchData") is False


def test_ignoring_the_same_root_twice_records_it_once(archive):
    archive.ignore_repo_root(r"D:\SearchData")
    archive.ignore_repo_root(r"D:\SearchData\\")

    assert len(archive.ignored_repo_roots()) == 1


# --- A2: pruning ------------------------------------------------------------

def test_a2_a_repository_whose_git_has_gone_is_pruned(archive):
    r"""Mechanism 1. Deleted files are pruned and repositories were not, so a
    `.git` that was removed or renamed left its row, its name in the tree and
    its `repo_id` on every file - with no way back short of a reset."""
    gone = archive.prune_repos([r"D:\code\somewhere-else"])

    assert gone == [r"D:\SearchData"]
    assert archive.repos_list() == []


def test_a2_a_repository_the_walk_covered_survives(archive):
    r"""**Only roots the walk actually reached can be judged.** A repository on
    an unmounted drive has not disappeared - it is simply not being looked at,
    and pruning it would release every one of its files the moment somebody
    indexed a different folder."""
    gone = archive.prune_repos([r"D:\SearchData"])

    assert gone == []
    assert len(archive.repos_list()) == 1


# --- §4 and §5: saying what is hidden, and which kind of empty ---------------

def test_a4_the_summary_names_the_preset_and_the_arithmetic():
    r"""`DEFAULT_PRESET` is `build`, which excludes `.md`, `.txt`, `.json`,
    `.yml` and `.csv` - so `README.md`, `package.json` and `requirements.txt`
    are filtered out on a fresh install. The grouping is defensible; **the fault
    was that nothing on screen said a filter was active.**"""
    from app.ui.presenter import code_summary

    # The arithmetic is the summary's own: it has the repository totals and the
    # rows that survived, which is everything the subtraction needs. Handing it
    # in meant the view doing a sum, in a module a length guard keeps short.
    said = code_summary([1] * 12, [{"files": 340}], preset="build")

    assert "12 files" in said
    assert "328 hidden" in said
    assert "source, config and build" in said.lower()


def test_a_tab_hiding_nothing_says_nothing_about_filters():
    """A line reading "0 hidden" on every screen is noise, and noise is what
    teaches people to stop reading the summary."""
    from app.ui.presenter import code_summary

    assert "hidden" not in code_summary([1] * 12, [{"files": 12}])


@pytest.mark.parametrize("state,expected", [
    (dict(indexed_files=0, hidden=0, has_query=False), "not be under an indexed root"),
    (dict(indexed_files=340, hidden=340, has_query=False, preset="build"), "0 of 340"),
    (dict(indexed_files=340, hidden=0, has_query=True), "No file matches that query"),
])
def test_a5_each_kind_of_empty_gets_its_own_sentence(state, expected):
    r"""**Three states rendered identically, as nothing.**

    `repo_empty_state` already does this well one level up - for *no
    repositories at all* - and its docstring explains why a generic "no results"
    would waste the answer that matters. The same care had not been applied
    here, and two of these three are configuration problems the person can fix.
    """
    from app.ui.presenter import repo_list_empty

    assert expected in repo_list_empty(**state)
