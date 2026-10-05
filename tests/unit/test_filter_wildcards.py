"""A `*` or a `?` typed into a `/` command is a wildcard, on every tab.

2026-10-05, the owner: "/ commands should take wild cards". Until then the
star worked in the words of a search (`tests/unit/test_wildcards.py`) and was
a literal character in every filter: `/name inv*` looked for a file with a
star in its name and found nothing. Every test here fails on that code.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.search.commands import expand_slashes
from app.search.query import parse_query
from app.storage.like import contains, glob, has_wildcard
from app.storage.sqlite_store import SqliteStore
from app.ui.presenter.repos import repo_filter, repo_visibility

#: (folder, name, ext). The names differ in where "invoice" sits.
FILES = (
    ("accounts", "invoice-2024.pdf", "pdf"),
    ("accounts", "invoice-2025.xlsx", "xlsx"),
    ("accounts", "old invoice.xls", "xls"),
    ("projects/leeds", "site_plan.docx", "docx"),
    ("projects/leeds", "100%_final.txt", "txt"),
)


@pytest.fixture()
def store(tmp_path: Path):
    opened = SqliteStore(tmp_path / "knowledge.db").connect()
    for folder, name, ext in FILES:
        parent = (tmp_path / folder).as_posix()
        file_id = opened.upsert_file(
            f"{parent}/{name}", parent_dir=parent, ext=ext, size_bytes=10,
            mtime_ns=1_741_780_800_000_000_000, status="INDEXED", source_kind="file")
        opened.replace_chunks(file_id, [{"ordinal": 0, "text": name}])
    message = opened.upsert_file(
        "pst://msg/renewal", parent_dir="pst://msg", size_bytes=1,
        mtime_ns=1_741_780_800_000_000_000, status="INDEXED", source_kind="pst_message")
    with opened.write() as conn:
        conn.execute(
            "INSERT INTO messages (file_id, subject, sender, recipients, sent_at, has_attach) "
            "VALUES (?, ?, ?, ?, ?, 0)",
            (message, "Licence renewal 2025", "dave.smith@acme.com", '["priya@acme.com"]',
             1_741_780_800))
    yield opened
    opened.close()


def found(store: SqliteStore, typed: str) -> list[str]:
    rows = store.browse_files(parse_query(expand_slashes(typed)), limit=50)
    return sorted(Path(row["path"]).name for row in rows)


@pytest.mark.parametrize("typed, expected", [
    # The whole name, as at a command prompt: starts with, ends with, anywhere.
    ("/name invoice*", ["invoice-2024.pdf", "invoice-2025.xlsx"]),
    ("/name *invoice*", ["invoice-2024.pdf", "invoice-2025.xlsx", "old invoice.xls"]),
    ("/name *.xls", ["old invoice.xls"]),
    ("/name invoice-202?.pdf", ["invoice-2024.pdf"]),
    ("/name site?plan*", ["site_plan.docx"]),
    # No wildcard: still "contains", as it always was.
    ("/name invoice", ["invoice-2024.pdf", "invoice-2025.xlsx", "old invoice.xls"]),
    # A percent sign somebody typed is still a percent sign.
    ("/name 100%*", ["100%_final.txt"]),
    ("/name 1%*", []),
    ("-name:invoice* /type pdf,xlsx,xls", ["old invoice.xls"]),
    ("/type xls*", ["invoice-2025.xlsx", "old invoice.xls"]),
    ("/type xls?", ["invoice-2025.xlsx"]),
    ("-type:xls* /name invoice", ["invoice-2024.pdf"]),
    ("/type pdf,xls*", ["invoice-2024.pdf", "invoice-2025.xlsx", "old invoice.xls"]),
    # A folder is matched anywhere in the path, wildcard or not.
    ("/path proj*/leeds", ["100%_final.txt", "site_plan.docx"]),
    ("/path acc?unts", ["invoice-2024.pdf", "invoice-2025.xlsx", "old invoice.xls"]),
])
def test_a_wildcard_in_a_file_filter(store, typed, expected):
    assert found(store, typed) == expected


@pytest.mark.parametrize("typed, hits", [
    ("/from dav*acme", 1), ("/from d?ve", 1), ("/from zz*", 0),
    ("/to pri*@acme.com", 1), ("/subject lic*renewal", 1), ("/subject renewal*2024", 0),
    ("-from:dav* /type mail", 0),
])
def test_a_wildcard_in_a_mail_filter(store, typed, hits):
    # The Files tab does not list messages, so this asks the one definition
    # every tab and the search itself build their SQL from.
    from app.storage.filters import file_filter_sql

    where, params = file_filter_sql(parse_query(expand_slashes(typed)))
    rows = store.conn.execute(
        f"SELECT f.path FROM files f WHERE f.path LIKE 'pst://%'{where}", params).fetchall()
    assert len(rows) == hits


@pytest.mark.parametrize("given, hits", [
    (dict(sender="dav*acme"), 1), (dict(sender="dave.smith"), 1),
    (dict(subject="lic?nce ren*"), 1), (dict(recipient="pri*"), 1),
    (dict(subject="ren*2024"), 0),
])
def test_the_mail_tabs_own_list_takes_the_same_wildcards(store, given, hits):
    """The Mail tab narrows by header through a trigram phrase when it can; a
    phrase cannot hold a wildcard, so those go to the scan."""
    assert len(store.browse_messages(**given)) == hits
    assert store._header_match("sender", "dav*acme") is None


def test_the_code_tree_takes_the_same_wildcards():
    files = [("md", "readme.md"), ("py", "calc.py")]
    assert repo_visibility(repo_filter("/repo lea*"), "leasha", "leasha", files) == (True, [True, True])
    assert repo_visibility(repo_filter("/repo lea*"), "tools", "tools", files)[0] is False
    assert repo_visibility(repo_filter("read*.md"), "tools", "tools", files) == (True, [True, False])
    assert repo_visibility(repo_filter("/type p?"), "tools", "tools", files) == (True, [False, True])


def test_the_pattern_itself():
    assert has_wildcard("inv*") and has_wildcard("a?c") and not has_wildcard("50%_off")
    assert contains("Dav*") == "%dav%%"
    assert glob("Inv*_202?.pdf") == r"inv%\_202_.pdf"
    assert contains("50%_off") == r"%50\%\_off%"


def test_the_help_says_so():
    from app.search.commands import help_lines

    assert any("/name inv*" in line for line in help_lines())
