"""BM25 over FTS5 — the half of hybrid search that knows what words mean literally.

Layer: L4

Keyword search is not the poor relation here. On a corpus of technical documents
and fifteen years of email it is often the *better* retriever: part numbers,
error codes, project names and people's surnames are exactly what a dense model
smooths away, and exactly what someone actually types.

**The expression always comes from `query.py`.** Raw user input never reaches
`MATCH`. `search_bm25()` still catches `OperationalError` as a backstop, but with
the sanitiser in front of it that path should be unreachable — so if it ever
fires, that is a bug in the sanitiser and is logged as one rather than silently
returning "no results" and letting a user conclude their documents are missing.

**Filters are applied in SQL, not in Python.** Fetching 100 rows and discarding
90 of them to honour `type:pdf` means the 10 that survive are the 10 best of the
wrong set. Filtering in the query means the top 100 are the top 100 *that match
the filter*, which is the only version that gives correct results.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Optional, Sequence

from app.core.logging import logger
from app.search.query import ParsedQuery, _fts_quote
from app.storage.filters import epoch_ns, file_filter_sql

__all__ = ["search", "KEYWORD_LIMIT"]

#: Candidates handed to fusion. From the spec's pipeline diagram.
KEYWORD_LIMIT = 100

_log = logger.bind(component="search.keyword")


#: **The filter builder moved to `app/storage/filters.py`.** Three surfaces
#: needed it and only this one could reach it, so the Files, Code and Mail tabs
#: each grew a smaller, divergent copy - which is what the owner saw as
#: *"the results should be same across but only applicable to the tab, this is
#: not the case"*. Aliased rather than renamed at every call site: these names
#: are what the query-plan tests and the repository acceptance suite import,
#: and those tests are about the SQL, which has not changed.
_filter_sql = file_filter_sql
_epoch_ns = epoch_ns


def search(
    store: Any,
    parsed: ParsedQuery,
    *,
    limit: int = KEYWORD_LIMIT,
    prefix_last: bool = False,
) -> list[dict[str, Any]]:
    """BM25 hits for a parsed query, best first.

    Returns [] when there is nothing searchable - a query of only filters, or one
    whose every token was punctuation. That is not an error: the caller decides
    whether a filter-only query should list matching files instead.
    """
    expression = parsed.fts_match(prefix_last=prefix_last)
    where, params = _filter_sql(parsed)

    if not expression:
        if not parsed.has_filters:
            return []
        return _filter_only(store, where, params, limit)

    sql = f"""
        SELECT c.id AS chunk_id, c.file_id, c.text, c.page,
               c.char_start, c.char_end,
               f.path, f.ext, f.mtime_ns,
               bm25(chunks_fts) AS score
        FROM chunks_fts
        JOIN chunks c ON c.id = chunks_fts.rowid
        JOIN files  f ON f.id = c.file_id
        WHERE chunks_fts MATCH ?{where}
        ORDER BY score
        LIMIT ?
    """
    try:
        rows = store.conn.execute(sql, [expression, *params, limit]).fetchall()
    except sqlite3.OperationalError as exc:
        # Unreachable if query.py did its job. Loud, because it means the
        # sanitiser has a hole - not something to swallow as "no results".
        _log.error(
            "FTS5 rejected a sanitised expression - this is a sanitiser bug, not a bad query. "
            "expression={!r} error={}", expression, exc,
        )
        return []

    return [dict(row) for row in rows]


def _filter_only(store: Any, where: str, params: list[Any], limit: int) -> list[dict[str, Any]]:
    """`type:pdf after:2024` with no search terms - list what matches.

    Ordered newest first, because a query that is purely a filter is a browse,
    and recency is the only ranking signal available without a search term.
    """
    sql = f"""
        SELECT c.id AS chunk_id, c.file_id, c.text, c.page,
               c.char_start, c.char_end,
               f.path, f.ext, f.mtime_ns,
               0.0 AS score
        FROM chunks c
        JOIN files f ON f.id = c.file_id
        WHERE c.ordinal = 0{where}
        ORDER BY f.mtime_ns DESC
        LIMIT ?
    """
    rows = store.conn.execute(sql, [*params, limit]).fetchall()
    return [dict(row) for row in rows]


def unmatched_terms(store: Any, terms: Sequence[str], *, limit: int = 6) -> tuple[str, ...]:
    """Which of these words appear nowhere in the index.

    **The bug this exists for.** A search for "a project file for petrrabigh
    project" returned twenty confident-looking results, none of which had
    anything to do with Petro Rabigh - because that word is not in the corpus at
    all, terms are ORed, and every message containing "project" or "file"
    therefore matched. The ranking was correct. The *query* had collapsed to its
    two most useless words, and nothing on screen said so.

    A word that matches nothing is almost always the whole answer: a typo, a
    name spelled differently in the documents, or something simply not indexed
    yet. Saying which word it was turns a mystifying result list into a
    one-second diagnosis.

    One indexed lookup per word with `LIMIT 1`, capped at `limit` words - cheap
    enough for the full tier, deliberately not run on every keystroke.
    """
    missing: list[str] = []
    for term in list(terms)[:limit]:
        cleaned = (term or "").strip().rstrip("*")
        if len(cleaned) < 3:
            # One and two-letter words match by accident or not at all, and
            # reporting them as "not found" would be noise on every query.
            continue
        expression = _fts_quote(cleaned)
        if not expression:
            continue
        try:
            row = store.conn.execute(
                "SELECT 1 FROM chunks_fts WHERE chunks_fts MATCH ? LIMIT 1",
                (expression,),
            ).fetchone()
        except sqlite3.OperationalError:
            continue                             # never fail a search over a hint
        if row is None:
            missing.append(cleaned)
    return tuple(missing)


def file_ids_matching(store: Any, parsed: ParsedQuery) -> Optional[set[int]]:
    """File ids the filters allow, or None when there are no filters.

    The vector side needs this: LanceDB cannot join to `files`, so a filtered
    ANN search has to be told which file ids are eligible. None means "no
    restriction", which is different from an empty set, and conflating the two
    would turn every unfiltered search into one that returns nothing.
    """
    if not parsed.has_filters:
        return None
    where, params = _filter_sql(parsed)
    rows = store.conn.execute(
        f"SELECT id FROM files f WHERE 1=1{where}", params
    ).fetchall()
    return {int(row["id"]) for row in rows}
