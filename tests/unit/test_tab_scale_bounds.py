r"""The Files, Mail and Code tabs bound what they read - work of 2026-10-04.

Layer: L1

Measured with `tools/fts_scale_bench.py` on a million chunks, for a word in a
quarter of them: the Files list 611 ms, its count 434, the Code tab's
repositories 378, the Mail list 432. After: 18.5, 16.9, 0.2 and 16.0 ms.

The Files list is ranked, so it scores the newest matches only, as the search
box does. The others must not change their answer, so each new form is checked
here against the old one on the same rows, with the thresholds shrunk so a
small index counts as broad.
"""

from __future__ import annotations

import pytest

from app.storage import sqlite_store
from app.storage.sqlite_store import FileStatus, SqliteStore


@pytest.fixture()
def store(tmp_path):
    with SqliteStore(tmp_path / "index.db") as store:
        first = store.upsert_repo(r"D:\RepoA", name="RepoA", kind="work")
        second = store.upsert_repo(r"D:\RepoB", name="RepoB", kind="work")
        for number in range(20):
            repo = first if number % 2 else second
            file_id = store.upsert_file(
                rf"D:\Repo{'A' if number % 2 else 'B'}\mod{number}.py", size_bytes=1,
                mtime_ns=number, ext="py", status=FileStatus.INDEXED,
                source_kind="file", repo_id=repo)
            store.replace_chunks(file_id, [
                {"ordinal": o, "text": f"pump code {number} {o}"
                 + (" widget" if number % 2 and o == 3 else ""),
                 "page": None, "char_start": 0, "char_end": 1} for o in range(5)])
        for number in range(30):
            file_id = store.upsert_file(
                rf"D:\Mail\a.pst::{number}", size_bytes=1, mtime_ns=number, ext="msg",
                status=FileStatus.INDEXED, source_kind="pst_message")
            with store.write() as conn:
                conn.execute(
                    "INSERT INTO messages (file_id, subject, sender, sent_at) "
                    "VALUES (?, ?, 'a@example.com', ?)",
                    (file_id, f"s{number}", None if number % 9 == 0 else 1_700_000_000 + number))
            store.replace_chunks(file_id, [
                {"ordinal": o, "text": f"pump mail {number} {o}"
                 + (" holiday" if number % 3 == 0 and o == 2 else ""),
                 "page": None, "char_start": 0, "char_end": 1} for o in range(4)])
        yield store


def _both(monkeypatch, name, call):
    """`call()` with the walk/bound off, then on."""
    monkeypatch.setattr(sqlite_store, name, 10**9)
    before = call()
    monkeypatch.setattr(sqlite_store, name, 5)
    after = call()
    return before, after


@pytest.mark.parametrize("words", ["pump", "holiday", "pump holiday"])
@pytest.mark.parametrize("sort", ["", "oldest"])
def test_the_mail_list_walked_by_date_is_the_same_list(store, monkeypatch, words, sort):
    before, after = _both(monkeypatch, "MATCH_WALK_MIN", lambda: [
        r["file_id"] for r in store.browse_messages(words=words, sort=sort, limit=12)])
    assert before and after == before


@pytest.mark.parametrize("words", ["pump", "holiday", "pump holiday", "nothing"])
def test_the_mail_count_probed_is_the_same_number(store, monkeypatch, words):
    """The count must be exact: the probing form against the collecting one."""
    before, after = _both(monkeypatch, "MATCH_PROBE_MIN",
                          lambda: store.count_messages_matching(words=words))
    assert after == before
    assert store.count_messages_matching(words=words, sender="example") == after


def test_the_code_tabs_repositories_walked_are_the_same_repositories(store, monkeypatch):
    from app.search.query import parse_query

    for words in ("pump", "widget", "nothing"):
        before, after = _both(monkeypatch, "MATCH_WALK_MIN",
                              lambda: store.repos_with_matches(parse_query(words)))
        assert after == before, words


def test_the_files_count_is_the_same_number(store):
    from app.search.query import parse_query

    expected = {r[0] for r in store.conn.execute(
        "SELECT DISTINCT c.file_id FROM chunks_fts JOIN chunks c ON c.id = chunks_fts.rowid "
        "JOIN files f ON f.id = c.file_id WHERE chunks_fts MATCH '\"pump\"' "
        "AND f.source_kind = 'file'")}
    assert store.count_browse_files(parse_query("pump")) == len(expected)
    assert store.count_browse_files(parse_query("pump"), cap=5) == 6


def test_the_files_list_scores_the_newest_matches_once_a_word_is_broad(store, monkeypatch):
    from app.search.query import parse_query

    monkeypatch.setattr(sqlite_store, "MATCH_SCORED_MAX", 10**9)
    assert store._match_floor('"pump"') == 0
    monkeypatch.setattr(sqlite_store, "MATCH_SCORED_MAX", 20)
    floor = store._match_floor('"pump"')
    assert floor > 0
    # The newest chunks are all mail, which the Files list leaves out: the
    # bounded read widens until the page fills rather than showing nothing.
    newest_kinds = {r[0] for r in store.conn.execute(
        "SELECT DISTINCT f.source_kind FROM chunks c JOIN files f ON f.id = c.file_id "
        "WHERE c.id > ?", (floor,))}
    assert newest_kinds == {"pst_message"}
    rows = store.browse_files(parse_query("pump"), limit=15)
    assert len(rows) == 15
    assert {r["source_kind"] for r in rows} == {"file"}
