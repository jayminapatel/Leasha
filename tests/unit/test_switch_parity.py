r"""The same query, on any tab, narrowed the same way.

Layer: L1/L5

The owner's report, in full, because every test here is one clause of it:

> *"in the ui the switches Search should have all switches, files should have
> all switches (files, Mail and Code), mail should have mail switches, code same
> switches has files - this is not the case, and the results should be same
> across but only applicable to the tab - this is not the case"*

Both halves had the same cause. Each tab wrote its own filtering, so each tab
had its own idea of which switches existed: Files honoured two of eleven, Code
three - two of which it then ignored anyway - and Mail knew about its five
columns and nothing about the file a message came from. The menus were trimmed
to match, which made the gap look deliberate rather than missing.

`app/storage/filters.py` is now the single definition of what a switch means
against `files`, and every tab composes it. So a tab decides **which rows it is
about** and never what `size:>1mb` means - which is exactly *"same across, but
only applicable to the tab"*.

The tests are written against real stores rather than fakes on purpose: the
thing that was broken was the SQL each tab ended up running, and a fake store
would have agreed with whatever the presenter said while the database disagreed.
"""

from __future__ import annotations

import time

import pytest

from app.search.commands import expand_slashes
from app.search.query import parse_query
from app.storage.sqlite_store import FileStatus, SqliteStore
from app.ui.presenter import mail_filters

NOW = time.time_ns()
#: Deliberately a fixed, long-past instant rather than "now minus N days". The
#: first version of this file used an offset and picked a cutoff that the offset
#: happened to fall the wrong side of - a fixture that would have started
#: failing on a date, which is the worst kind.
LONG_AGO = int(time.mktime((2020, 3, 1, 12, 0, 0, 0, 0, -1))) * 1_000_000_000


def _parse(raw: str):
    return parse_query(expand_slashes(raw))


def _names(rows) -> list[str]:
    return sorted(str(r["path"]).replace("\\", "/").rsplit("/", 1)[-1] for r in rows)


@pytest.fixture()
def store(tmp_path):
    r"""A corpus with something for every switch to bite on."""
    with SqliteStore(tmp_path / "index.db") as store:
        repo_id = store.upsert_repo(r"D:\code\leasha", name="leasha", kind="git")

        def add(path, ext, *, text=None, size=1_000, mtime=NOW, repo=None):
            file_id = store.upsert_file(
                path, size_bytes=size, mtime_ns=mtime, ext=ext,
                status=FileStatus.INDEXED, source_kind="file",
                parent_dir=path.rsplit("\\", 1)[0], repo_id=repo,
            )
            if text:
                store.replace_chunks(file_id, [{"text": text, "ordinal": 0}])
            return file_id

        add(r"D:\projects\leeds\pump report.pdf", "pdf",
            text="Northern pump station commissioning report")
        add(r"D:\projects\leeds\valve notes.docx", "docx",
            text="The pump was replaced during the autumn shutdown")
        add(r"D:\projects\hull\big survey.pdf", "pdf",
            text="unrelated survey text", size=5_000_000, mtime=LONG_AGO)
        add(r"D:\code\leasha\PumpService.cs", "cs",
            text="class PumpService", repo=repo_id)
        add(r"D:\code\leasha\readme.md", "md", text="notes about the pump",
            repo=repo_id)

        message = add(r"D:\mail\2024\pump quote.msg", "msg", text="the pump quote")
        store.set_message(
            message, sender="dave.smith@acme.com", recipients='["priya@x.com"]',
            subject="Pump quote", has_attach=1, sent_at=int(time.time()),
        )
        yield store


# --- Files: every switch, and the same rows the search box would filter -----

@pytest.mark.parametrize("query,expected", [
    ("/type pdf", ["big survey.pdf", "pump report.pdf"]),
    ("/path leeds", ["pump report.pdf", "valve notes.docx"]),
    ("/size >1mb", ["big survey.pdf"]),
    ("/name valve", ["valve notes.docx"]),
    ("/repo leasha", ["PumpService.cs", "readme.md"]),
    ("/from dave", ["pump quote.msg"]),
    ("/to priya", ["pump quote.msg"]),
    ("/subject quote", ["pump quote.msg"]),
    ("/has attachment", ["pump quote.msg"]),
])
def test_files_honours_every_switch(store, query, expected):
    r"""**Eight of these nine did nothing before.**

    `file_query` kept `type:` and the name and dropped the rest one function
    above the store call, so the dropdown offered filters that were parsed
    correctly and then discarded. `/from` here is not a mail search: it is
    *"which files on disk are mail from Dave"*, which is a question about files
    and belongs on the tab about files.
    """
    assert _names(store.browse_files(_parse(query), limit=50)) == expected


def test_files_dates_narrow_by_when_the_file_changed(store):
    assert _names(store.browse_files(_parse("/before 2021-01-01"), limit=50)) == [
        "big survey.pdf"]
    assert "big survey.pdf" not in _names(
        store.browse_files(_parse("/after 2021-01-01"), limit=50))


def test_free_text_matches_names_and_contents(store):
    r"""Asked for directly: *"names, folders and contents"*.

    `valve notes.docx` has no "pump" in its name and says it in its text; before
    this the Files tab could not find it, while the search box could - two
    answers to one query, which is the complaint.
    """
    found = _names(store.browse_files(_parse("pump"), limit=50))

    assert "pump report.pdf" in found          # by name
    assert "valve notes.docx" in found         # by content


def test_the_name_switch_narrows_to_filenames(store):
    r"""*"create a switch for name... that way only names and folders are
    matched. I think we have this switch"* - and we did.

    It needs no mode flag and gets none. `/name` is an ordinary conjunctive
    filter on the basename, so a row that matched only on its contents cannot
    satisfy it, and the content half falls away on its own.
    """
    # Three files have "pump" in their *name*; `valve notes.docx` says it only
    # in its text, and is the one that must fall away.
    found = _names(store.browse_files(_parse("pump /name pump"), limit=50))

    assert "valve notes.docx" not in found
    assert found == ["PumpService.cs", "pump quote.msg", "pump report.pdf"]


def test_switches_compose_rather_than_replace(store):
    assert _names(store.browse_files(_parse("pump /type docx"), limit=50)) == [
        "valve notes.docx"]


def test_an_empty_box_still_lists_everything(store):
    assert len(store.browse_files(_parse(""), limit=50)) == 6


def test_one_or_two_characters_still_mean_nothing(store):
    """They match nearly every file, and a screenful of arbitrary rows looks
    like a search that worked and gave the wrong answer."""
    assert store.browse_files(_parse("pu"), limit=50) == []


def test_a_file_matching_both_halves_appears_once(store):
    """`readme.md` says "pump" in its text and `PumpService.cs` in its name; a
    file that matched by name *and* by content must not be listed twice."""
    found = _names(store.browse_files(_parse("pump"), limit=50))

    assert len(found) == len(set(found))


# --- Code: the same, narrowed to repositories -------------------------------

def test_code_sees_only_repository_files(store):
    r"""The scope is what makes it the Code tab.

    Everything else about the query is identical to Files - which is the point.
    `f.repo_id IS NOT NULL` is the same narrowing the search box's Code chip
    applies, so the two cannot drift.
    """
    assert _names(store.browse_files(_parse("pump").scoped("code"), limit=50)) == [
        "PumpService.cs", "readme.md"]


def test_code_honours_a_switch_files_honours(store):
    assert _names(store.browse_files(
        _parse("/type md").scoped("code"), limit=50)) == ["readme.md"]


def test_the_same_switch_gives_the_same_rows_on_both_tabs(store):
    r"""**The sentence this file exists for**: *"the results should be same
    across but only applicable to the tab"*.

    Code's rows are exactly Files' rows that live in a repository. Not a
    similar set produced by a similar query - the same set, because it is the
    same filter.
    """
    everywhere = set(_names(store.browse_files(_parse("/name pump"), limit=50)))
    in_repos = set(_names(store.browse_files(
        _parse("/name pump").scoped("code"), limit=50)))

    assert in_repos <= everywhere
    assert in_repos == {"PumpService.cs"}


# --- Mail: its own columns, plus the file the message came from -------------

@pytest.mark.parametrize("query,expected", [
    ("/from dave", ["pump quote.msg"]),
    ("/type msg", ["pump quote.msg"]),
    ("/path 2024", ["pump quote.msg"]),
    ("/name quote", ["pump quote.msg"]),
])
def test_mail_honours_both_halves(store, query, expected):
    """The last three are new: a message is a row joined to the file it came
    from, so its name, folder, type and size were always answerable here."""
    rows = store.browse_messages(**mail_filters(_parse(query)))

    assert _names(rows) == expected


def test_a_file_level_switch_can_exclude_a_message(store):
    """Proof the fragment is applied rather than merely built - a filter that
    only ever passes is indistinguishable from one that is ignored."""
    assert store.browse_messages(**mail_filters(_parse("/type pdf"))) == []


def test_mail_dates_still_mean_the_date_it_was_sent(store):
    r"""**Not the file's mtime, and the difference is not academic.**

    A PST is one file holding two hundred thousand messages, so its mtime is
    when the archive last changed - identical for every message inside it.
    Routing `/after` through the file-level fragment would make a date filter
    either match all of them or none, which is worse than not offering it.
    """
    filters = mail_filters(_parse("/after 2024-01-01 /type msg"))

    assert "after" in filters
    assert "mtime_ns" not in filters.get("file_where", "")
