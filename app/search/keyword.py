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

__all__ = ["search", "KEYWORD_LIMIT", "Eligibility", "ELIGIBLE_CAP"]

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

    # **The narrow query first, and only widen if it was not enough.**
    #
    # `AND_TERM_LIMIT = 1` joins multi-word queries with OR, and that value is
    # not a mistake - it was chosen against twenty real sentences, where three
    # left six of them returning nothing at all and four left eleven. Demanding
    # every word of a description is how a search box earns a reputation for
    # finding nothing, and no latency argument is worth that.
    #
    # But OR is what makes the keyword half expensive, and the cost is
    # proportional to matching rows rather than to returned ones. Measured on a
    # 40,000-chunk synthetic corpus with independent terms:
    #
    #     "pump" OR "valve"               22,869 rows      21.2 ms
    #     "pump" AND "valve"               4,877 rows       5.7 ms
    #     three words, OR                 23,077 rows      22.7 ms
    #     three words, AND                    60 rows       1.3 ms
    #
    # At twenty million chunks that ratio is the difference between a search box
    # and a progress bar.
    #
    # So both, in the order that costs least: the AND form is tried first, and
    # the OR form runs **only when AND did not fill the page**. Recall is
    # therefore identical to OR-always - the wide query still runs whenever the
    # narrow one is thin, which is exactly the case the twenty sentences
    # measured - while a query whose terms genuinely co-occur never pays for it.
    #
    # Skipped for a single term, where the two expressions are the same string,
    # and for an explicit `AND`, which is already narrow.
    narrow = _narrow_first(parsed, prefix_last)
    if narrow and narrow != expression:
        rows = _run_match(store, narrow, where, params, limit)
        if len(rows) >= limit:
            return rows

    # **Unfiltered searches go through FTS5's own top-k, and that is H6.**
    #
    # The single-statement form below joins `chunks` and `files` to every
    # matching row *and then* sorts, so BM25 and both joins are paid for every
    # match before `LIMIT` discards them. Measured on a 40,000-chunk synthetic
    # corpus: a two-common-word query took **25.8ms** against **1.4ms** for a
    # rare word - an eighteenfold gap that is entirely the number of rows
    # scored. At the twenty to thirty million chunks this is built for, that
    # extrapolates to seconds per keystroke, and `AND_TERM_LIMIT = 1` makes it
    # worse by turning multi-word queries into OR.
    #
    # FTS5 has a dedicated optimisation for `ORDER BY rank LIMIT n` - it keeps a
    # running top-k instead of materialising every match - but it only fires
    # when the query is against the FTS table alone, with nothing else in the
    # `WHERE` and no joins. So the shape is: pick the survivors first, then join
    # only those.
    #
    # **`rank`, not `bm25(chunks_fts)`.** They give the same ordering; only the
    # bare `rank` column triggers the optimisation.
    #
    # A filtered search cannot use it: the LIMIT would apply before the filter,
    # so the top 100 overall might contain no PDFs at all while thousands
    # match. That case keeps the single statement, where the filter is part of
    # the same query - and its cost is bounded by the filter rather than by the
    # corpus.
    if where:
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
        arguments = [expression, *params, limit]
    else:
        sql = """
            SELECT c.id AS chunk_id, c.file_id, c.text, c.page,
                   c.char_start, c.char_end,
                   f.path, f.ext, f.mtime_ns,
                   top.score AS score
            FROM (
                SELECT rowid AS chunk_id, rank AS score
                FROM chunks_fts
                WHERE chunks_fts MATCH ?
                ORDER BY rank
                LIMIT ?
            ) AS top
            JOIN chunks c ON c.id = top.chunk_id
            JOIN files  f ON f.id = c.file_id
            ORDER BY top.score
        """
        arguments = [expression, limit]

    return _run_match(store, expression, where, params, limit)


def _narrow_first(parsed: ParsedQuery, prefix_last: bool) -> str:
    """The AND-joined form of this query, or "" when there is no narrower one."""
    if parsed.explicit_and:
        return ""
    try:
        return parsed.fts_match(prefix_last=prefix_last, force_and=True)
    except TypeError:                            # an older ParsedQuery
        return ""


def _run_match(store: Any, expression: str, where: str, params: list[Any],
               limit: int) -> list[dict[str, Any]]:
    """One FTS5 query, in whichever of the two shapes suits the filters."""
    if where:
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
        arguments = [expression, *params, limit]
    else:
        sql = """
            SELECT c.id AS chunk_id, c.file_id, c.text, c.page,
                   c.char_start, c.char_end,
                   f.path, f.ext, f.mtime_ns,
                   top.score AS score
            FROM (
                SELECT rowid AS chunk_id, rank AS score
                FROM chunks_fts
                WHERE chunks_fts MATCH ?
                ORDER BY rank
                LIMIT ?
            ) AS top
            JOIN chunks c ON c.id = top.chunk_id
            JOIN files  f ON f.id = c.file_id
            ORDER BY top.score
        """
        arguments = [expression, limit]

    try:
        rows = store.conn.execute(sql, arguments).fetchall()
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


#: Eligible file ids listed before the list itself becomes the cost.
#:
#: **Measured on 500,000 files, 2026-08-27.** `type:pdf` matches half of them,
#: and building that set took **383ms and 46MB** - before either retriever
#: started, on the critical path of every filtered search, against a 300ms
#: budget for the whole search. At the ten million this is designed for it is
#: seconds and most of a gigabyte.
#:
#: Nothing downstream wanted the list anyway. `vector.MAX_PREFILTER_IDS` is the
#: point above which the ids stop being pushed down, so a list longer than that
#: is built and then not used; past it the search only ever asks "are these few
#: hundred candidates eligible?", which SQLite answers over a few hundred ids
#: far faster than Python answers it over a quarter of a million it first had
#: to be handed.
#:
#: **So the cap is `MAX_PREFILTER_IDS`, and a test holds the two together.**
#: Stated as a literal because `keyword` importing `vector` would be a cycle,
#: which is exactly the kind of drift the test exists to catch.
ELIGIBLE_CAP = 2_000


class Eligibility:
    """Which files the filters allow, answered at whatever size that is.

    Two shapes behind one question, because a broad filter and a narrow one
    want opposite things:

    * **narrow** - the ids fit, so they are held and pushed down into the ANN
      search, which is the fastest thing available;
    * **broad** - listing them costs more than the search, so they are not
      listed. The filter travels as SQL and is applied to the *candidates*, of
      which there are a few hundred.

    `ids` is None in the broad case, which is deliberately the same thing an
    unfiltered query hands to LanceDB: no pushdown. Correctness does not rest
    on the pushdown - `keeps()` is what enforces the filter, and it is asked in
    both shapes.
    """

    __slots__ = ("ids", "_store", "_where", "_params", "_filtered")

    def __init__(self, store: Any, parsed: ParsedQuery,
                 cap: int = ELIGIBLE_CAP) -> None:
        self._store = store
        self._filtered = bool(parsed.has_filters)
        self.ids: Optional[set[int]] = None
        self._where, self._params = ("", [])
        if not self._filtered:
            return

        self._where, self._params = _filter_sql(parsed)
        rows = store.conn.execute(
            f"SELECT id FROM files f WHERE 1=1{self._where} LIMIT ?",
            [*self._params, cap + 1],
        ).fetchall()
        if len(rows) <= cap:
            self.ids = {int(row["id"]) for row in rows}

    @property
    def filtered(self) -> bool:
        return self._filtered

    @property
    def excludes_everything(self) -> bool:
        """A filter that matched nothing, which is not the same as no filter."""
        return self.ids is not None and not self.ids

    def keeps(self, file_ids: Sequence[int]) -> set[int]:
        """Which of `file_ids` the filters allow. **Bounded by what is asked.**

        The narrow case is a set intersection. The broad case is one query
        against the ids in hand - a few hundred - rather than against the
        quarter of a million the filter matches, which is the whole point.
        """
        if not self._filtered:
            return {int(f) for f in file_ids}
        wanted = {int(f) for f in file_ids}
        if not wanted:
            return set()
        if self.ids is not None:
            return wanted & self.ids
        placeholders = ", ".join("?" for _ in wanted)
        rows = self._store.conn.execute(
            f"SELECT id FROM files f WHERE f.id IN ({placeholders}){self._where}",
            [*wanted, *self._params],
        ).fetchall()
        return {int(row["id"]) for row in rows}


def file_ids_matching(store: Any, parsed: ParsedQuery) -> Optional[set[int]]:
    """File ids the filters allow, or None when there are no filters.

    **Kept for callers that genuinely want the whole list**, and for the tests
    that assert the filter grammar. The search path uses `Eligibility`, which
    refuses to build a list nobody needed - see `ELIGIBLE_CAP`.
    """
    if not parsed.has_filters:
        return None
    where, params = _filter_sql(parsed)
    rows = store.conn.execute(
        f"SELECT id FROM files f WHERE 1=1{where}", params
    ).fetchall()
    return {int(row["id"]) for row in rows}
