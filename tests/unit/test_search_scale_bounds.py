r"""Keyword search bounds what it scores - work of 2026-10-04.

Layer: L4

Measured with `tools/fts_scale_bench.py` on a million chunks: a search costs
what it matches, so a common word took 302 ms against the keyword stage's 60 ms,
a filter on top 610-870 ms, and a one-letter prefix 2,372 ms. These pin the four
changes that followed, at a size a unit test can build by shrinking the
constants:

1. a last word shorter than `PREFIX_MIN_CHARS` is no prefix while typing;
2. a word in a tenth of the index is left out when other words remain, and a
   search still too broad is scored over the newest chunks only;
3. a filtered search draws on FTS5's top-k first, with the same answer;
4. every connection has the larger page cache and memory-mapped reads.

And `fts_stem` works on every thread, not only the first that asked.
"""

from __future__ import annotations

import threading

import pytest

from app.search import keyword
from app.search.query import PREFIX_MIN_CHARS, parse_query
from app.storage import sqlite_store
from app.storage.sqlite_store import FileStatus, SqliteStore


# --- 1. the prefix ----------------------------------------------------------

def test_a_short_last_word_is_left_out_while_others_are_typed():
    assert PREFIX_MIN_CHARS == 3
    assert parse_query("pump v").fts_match(prefix_last=True) == '"pump"'
    assert parse_query("pump va").fts_match(prefix_last=True) == '"pump"'
    assert parse_query("pump val").fts_match(prefix_last=True) == '"pump" OR "val"*'


def test_a_short_word_on_its_own_is_searched_as_typed():
    assert parse_query("ba").fts_match(prefix_last=True) == '"ba"'
    assert parse_query("bar").fts_match(prefix_last=True) == '"bar"*'


def test_a_committed_search_is_untouched():
    assert parse_query("pump v").fts_match() == parse_query("pump v").fts_match(prefix_last=False)
    assert '"v"' in parse_query("pump v").fts_match()


# --- 2 and 3. bounding, on an index made to be "large" ----------------------

@pytest.fixture()
def big(tmp_path, monkeypatch):
    """300 chunks: `pump` in every one, `report` in every tenth, `pdf` files a
    third of them and `dwg` one file. The constants shrink so 300 is large."""
    monkeypatch.setattr(keyword, "SCORED_MATCHES", 50)
    monkeypatch.setattr(keyword, "COMMON_SAMPLE", 200)
    monkeypatch.setattr(keyword, "OVERFETCH", 30)
    with SqliteStore(tmp_path / "index.db") as store:
        for number in range(30):
            ext = "dwg" if number == 0 else ("pdf" if number % 3 == 0 else "txt")
            file_id = store.upsert_file(
                rf"D:\Docs\file{number}.{ext}", size_bytes=1, mtime_ns=number,
                ext=ext, status=FileStatus.INDEXED, source_kind="file")
            store.replace_chunks(file_id, [
                {"ordinal": ordinal,
                 "text": f"pump station note {number} {ordinal}"
                         + (" report" if (number * 10 + ordinal) % 10 == 0 else "")
                         + " pump" * (ordinal % 4),
                 "page": None, "char_start": 0, "char_end": 10}
                for ordinal in range(10)])
        yield store


def test_a_common_word_is_left_out_when_others_remain(big):
    bounded, _floor = keyword._bounded(big, parse_query("pump report"), False)
    assert bounded.fts_match() == '"report"'


def test_a_half_typed_word_does_not_count_as_one_that_remains(big):
    """Measured on the bench: `pump v` found nothing. `v` is not searched while
    it is shorter than the prefix minimum, so leaving `pump` out left nothing."""
    bounded, _floor = keyword._bounded(big, parse_query("pump v"), True)
    assert bounded.fts_match(prefix_last=True) == '"pump"'
    assert keyword.search(big, parse_query("pump v"), limit=20, prefix_last=True)


def test_the_words_left_out_are_reported_for_the_notice(big, monkeypatch):
    assert keyword.left_out(big, parse_query("pump report")) == ("pump",)
    assert keyword.left_out(big, parse_query("report")) == ()
    monkeypatch.setattr(keyword, "SCORED_MATCHES", 10_000)
    assert keyword.left_out(big, parse_query("pump report")) == ()


def test_a_stopword_alone_does_not_count_as_one_that_remains(big):
    bounded, _floor = keyword._bounded(big, parse_query("pump of"), False)
    assert '"pump"' in bounded.fts_match()


def test_a_query_of_only_common_words_is_scored_over_the_newest(big):
    bounded, floor = keyword._bounded(big, parse_query("pump"), False)
    assert bounded.fts_match() == '"pump"'
    assert floor > 0
    hits = keyword.search(big, parse_query("pump"), limit=20)
    assert len(hits) == 20
    assert all(hit["chunk_id"] > floor for hit in hits)


def test_a_filter_the_newest_chunks_miss_still_finds_its_matches(big):
    """The one `dwg` file is the oldest, outside the newest slice: the bounded
    search widens rather than answering "nothing"."""
    _bounded, floor = keyword._bounded(big, parse_query("pump"), False)
    assert floor > 0
    hits = keyword.search(big, parse_query("pump type:dwg"), limit=5)
    assert hits and all(hit["ext"] == "dwg" for hit in hits)


def test_a_small_index_is_searched_exactly_as_before(big, monkeypatch):
    monkeypatch.setattr(keyword, "SCORED_MATCHES", 10_000)
    query = parse_query("pump report")
    assert keyword._bounded(big, query, False) == (query, 0)


def _every_match(store, expression, where, params, limit):
    """The statement a filtered search ran before 2026-10-04: score everything."""
    return [row["chunk_id"] for row in store.conn.execute(f"""
        SELECT c.id AS chunk_id, bm25(chunks_fts) AS score
        FROM chunks_fts JOIN chunks c ON c.id = chunks_fts.rowid
        JOIN files f ON f.id = c.file_id
        WHERE chunks_fts MATCH ?{where} ORDER BY score LIMIT ?""",
        [expression, *params, limit])]


@pytest.mark.parametrize("raw", ["station type:pdf", "station type:dwg", "note type:txt"])
def test_a_filtered_search_gives_the_answer_scoring_everything_gave(big, raw):
    parsed = parse_query(raw)
    where, params = keyword._filter_sql(parsed)
    expression = parsed.fts_match()
    got = [row["chunk_id"] for row in keyword._run_match(big, expression, where, params, 10)]
    assert got == _every_match(big, expression, where, params, 10)


# --- 4. the connection ------------------------------------------------------

def test_every_connection_has_the_search_cache_and_mmap(tmp_path):
    with SqliteStore(tmp_path / "index.db") as store:
        seen = {}

        def read():
            seen["cache"] = store.conn.execute("PRAGMA cache_size").fetchone()[0]
            seen["mmap"] = store.conn.execute("PRAGMA mmap_size").fetchone()[0]

        worker = threading.Thread(target=read)
        worker.start()
        worker.join()
        assert seen == {"cache": -sqlite_store.SEARCH_CACHE_KIB,
                        "mmap": sqlite_store.SEARCH_MMAP_BYTES}


# --- fts_stem on every thread -------------------------------------------------

def test_stemming_works_on_a_second_thread(tmp_path):
    with SqliteStore(tmp_path / "index.db") as store:
        assert store.fts_stem("pumps") == "pump"
        seen = {}
        worker = threading.Thread(target=lambda: seen.setdefault("v", store.fts_stem("valves")))
        worker.start()
        worker.join()
        assert seen["v"] == "valv"
