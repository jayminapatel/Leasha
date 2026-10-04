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
from app.storage.filters import epoch_ns, file_filter_sql, merge_by_date

__all__ = ["search", "left_out", "KEYWORD_LIMIT", "Eligibility", "ELIGIBLE_CAP"]

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


# ---------------------------------------------------------------------------
# Bounding the scored set - 2026-10-04, measured with tools/fts_scale_bench.py
# ---------------------------------------------------------------------------
#
# **A search costs what it matches, not what it returns.** FTS5 scores every
# matching chunk before `LIMIT` keeps a hundred, and no index changes that. On a
# million chunks: a word in 166 of them took 1 ms, one in 10,120 took 31 ms, one
# in 244,310 (a quarter) took 302 ms - five times the 60 ms the keyword stage has
# in BUILD_SPEC_V2 - and a filter on top of that 610-870 ms.
#
# So the scored set is bounded, and only when it would otherwise exceed
# `SCORED_MATCHES`: a small index searches exactly as it always did.
#
# * **A word in `COMMON_SHARE` or more of the index is left out** of a query that
#   has other words. Its BM25 weight is close to nothing - it is in a tenth of
#   everything - and it is what made the query expensive.
# * **What is still too broad is scored over the newest chunks only**, as many
#   as hold about `SCORED_MATCHES` matches - a `rowid` range, which FTS5 seeks
#   to in its own index rather than filtering after.
#
# How common a word is comes from counting its matches among the newest
# `COMMON_SAMPLE` chunks, under 1.5 ms for any word at any index size. The
# vocabulary table answers exactly, but costs what the word matches: 50 ms for
# `the` on a million chunks, which is the cost this exists to avoid.

#: A word in at least this share of the index is "common".
COMMON_SHARE = 0.10
#: The newest chunks a word's share is estimated from.
COMMON_SAMPLE = 20_000
#: The most matches one search scores. About 25-80 ms on the laptop.
SCORED_MATCHES = 10_000
#: How many top-ranked chunks a filtered search draws on before it falls back
#: to scoring every match. 655 ms became 286 ms with identical results.
OVERFETCH = 1_000


def _share(store: Any, expression: str, top: int) -> float:
    """The share of the newest `COMMON_SAMPLE` chunks that match `expression`."""
    sample = min(COMMON_SAMPLE, top)
    if sample <= 0 or not expression:
        return 0.0
    try:
        found = store.conn.execute(
            "SELECT count(*) FROM chunks_fts WHERE chunks_fts MATCH ? AND rowid > ?",
            (expression, top - sample)).fetchone()[0]
    except sqlite3.Error:
        return 0.0
    return int(found) / sample


def _exists(store: Any, expression: str) -> bool:
    """Whether any chunk matches `expression`. Never raises."""
    try:
        return store.conn.execute(
            "SELECT 1 FROM chunks_fts WHERE chunks_fts MATCH ? LIMIT 1",
            (expression,)).fetchone() is not None
    except sqlite3.Error:
        return False


def _bounded(store: Any, parsed: ParsedQuery, prefix_last: bool) -> tuple[ParsedQuery, int]:
    """`(query, floor)`: common words left out, and the rowid to score above (0 = all)."""
    bounded, floor, _left_out = _bound(store, parsed, prefix_last)
    return bounded, floor


def left_out(store: Any, parsed: ParsedQuery, *, prefix_last: bool = False) -> tuple[str, ...]:
    """The words a search for `parsed` leaves out as too common - for the notice
    that says so (`engine.NOTICE_LEFT_OUT`). Empty on an index small enough to
    score everything. A few constant-cost counts; never raises."""
    try:
        return _bound(store, parsed, prefix_last)[2]
    except Exception:                                    # noqa: BLE001 - a notice, never a failure
        return ()


def _bound(store: Any, parsed: ParsedQuery,
           prefix_last: bool) -> tuple[ParsedQuery, int, tuple[str, ...]]:
    """`_bounded`, and which words it left out."""
    dropped: tuple[str, ...] = ()
    try:
        top = int(store.conn.execute("SELECT max(id) FROM chunks").fetchone()[0] or 0)
    except (sqlite3.Error, AttributeError, TypeError):
        return parsed, 0, ()
    if top <= SCORED_MATCHES:
        return parsed, 0, ()

    terms = list(parsed.terms)
    if len(terms) > 1 and not parsed.phrases and len(parsed.or_groups) <= 1 \
            and not parsed.expansions:
        from app.search.query import _STOPWORDS, PREFIX_MIN_CHARS

        typing = terms[-1] if prefix_last else None
        common = []
        for term in terms:
            if term == typing:
                continue
            share = _share(store, _fts_quote(term), top)
            if share >= COMMON_SHARE and share * top > SCORED_MATCHES:
                common.append(term)
        # **Only a word that is searched counts as one that remains.** A
        # half-typed last word under the prefix minimum is not searched, and a
        # stopword is searched only when nothing else is left - leaving `pump`
        # out of `pump v` or `pump of` searched for `v` or `of` (measured: no
        # results for `pump v` on the bench).
        searched = [t for t in terms if t not in common
                    and not (t == typing and len(t) < PREFIX_MIN_CHARS)
                    and t.lower().rstrip("*") not in _STOPWORDS]
        # And only a word the index holds: `pump petrrabigh` with `pump` left
        # out searched for a word in nothing and found nothing, where it used
        # to find the pumps and say "Not in the index: petrrabigh". The first
        # match of a rare or missing word is a short read.
        if common:
            searched = [t for t in searched if _exists(store, _fts_quote(t))]
        if common and searched:
            from app.search.query import with_terms

            _log.debug("left out of the search as too common: {}", common)
            parsed = with_terms(parsed, [t for t in terms if t not in common])
            dropped = tuple(common)

    share = _share(store, parsed.fts_match(prefix_last=prefix_last), top)
    if share * top <= SCORED_MATCHES:
        return parsed, 0, dropped
    return parsed, max(0, top - int(SCORED_MATCHES / share)), dropped


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
    where, params = _filter_sql(parsed)
    if not parsed.fts_match(prefix_last=prefix_last):
        if not parsed.has_filters:
            return []
        return _filter_only(store, where, params, limit)

    # 2026-10-04: bounded before anything is scored - see `_bounded`.
    parsed, floor = _bounded(store, parsed, prefix_last)
    expression = parsed.fts_match(prefix_last=prefix_last)

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
        rows = _run_match(store, narrow, where, params, limit, floor)
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
    #
    # *Note, 2026-10-04: both shapes now live in `_run_match`, and a filtered
    # search draws on the top-k first - the paragraph above is kept as written.
    # The two statements that stood here were built and never run.*
    return _run_match(store, expression, where, params, limit, floor)


def _narrow_first(parsed: ParsedQuery, prefix_last: bool) -> str:
    """The AND-joined form of this query, or "" when there is no narrower one."""
    if parsed.explicit_and:
        return ""
    try:
        return parsed.fts_match(prefix_last=prefix_last, force_and=True)
    except TypeError:                            # an older ParsedQuery
        return ""


def _widening(store: Any, floor: int) -> list[int]:
    """`floor`, then floors each holding four times as many chunks, then 0."""
    if not floor:
        return [0]
    try:
        top = int(store.conn.execute("SELECT max(id) FROM chunks").fetchone()[0] or 0)
    except (sqlite3.Error, TypeError):
        return [floor, 0]
    floors, lower = [], floor
    while lower > 0:
        floors.append(lower)
        lower = max(0, top - 4 * (top - lower))
    return floors + [0]


def _run_match(store: Any, expression: str, where: str, params: list[Any],
               limit: int, floor: int = 0) -> list[dict[str, Any]]:
    """One FTS5 query, in whichever of the two shapes suits the filters.

    `floor` is the rowid scoring starts above (`_bounded`); 0 scores everything.

    2026-10-04: **a filtered search draws on the top `OVERFETCH` first.** Those
    are the best-ranked chunks overall, so the first `limit` of them that pass
    the filter *are* the filtered top `limit` - exactly the rows the full
    statement returns, at FTS5's top-k cost rather than at the cost of scoring
    and joining every match. Only when they do not fill the page (a filter few
    rows pass) does the full statement run, as before.
    """
    columns = """c.id AS chunk_id, c.file_id, c.text, c.page, c.label,
                   c.char_start, c.char_end,
                   f.path, f.ext, f.mtime_ns, f.taken_at_ns, f.content_hash,
                   f.volume_id, f.relative_path"""
    bound = " AND rowid > ?" if floor else ""
    bound_args = [floor] if floor else []
    top_sql = f"""
            SELECT {columns},
                   top.score AS score
            FROM (
                SELECT rowid AS chunk_id, rank AS score
                FROM chunks_fts
                WHERE chunks_fts MATCH ?{bound}
                ORDER BY rank
                LIMIT ?
            ) AS top
            JOIN chunks c ON c.id = top.chunk_id
            JOIN files  f ON f.id = c.file_id
            WHERE 1=1{where}
            ORDER BY top.score
            LIMIT ?
        """
    full_sql = f"""
            SELECT {columns},
                   bm25(chunks_fts) AS score
            FROM chunks_fts
            JOIN chunks c ON c.id = chunks_fts.rowid
            JOIN files  f ON f.id = c.file_id
            WHERE chunks_fts MATCH ? AND chunks_fts.rowid > ?{where}
            ORDER BY score
            LIMIT ?
        """
    attempts: list[tuple[str, list[Any]]] = []
    if where:
        # 2026-10-04: **a filter can empty the newest slice** - the newest
        # chunks may hold no PDFs at all - so a bounded filtered search that
        # does not fill the page widens four-fold at a time down to the whole
        # index. Any match that exists is still found; only a filter few rows
        # pass pays for the wider reads.
        for lower in _widening(store, floor):
            if lower == 0:
                attempts.append((top_sql.replace(" AND rowid > ?", ""),
                                 [expression, max(OVERFETCH, limit * 10), *params, limit]))
            attempts.append((full_sql, [expression, lower, *params, limit]))
    else:
        attempts.append((top_sql, [expression, *bound_args, limit, limit]))

    rows: list[Any] = []
    for sql, arguments in attempts:
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
        if len(rows) >= limit:
            break

    return [dict(row) for row in rows]


#: The columns every `_filter_only` branch below projects - identical in
#: both, so `merge_by_date` is comparing rows of the same shape either way.
_FILTER_ONLY_COLUMNS = """c.id AS chunk_id, c.file_id, c.text, c.page, c.label,
               c.char_start, c.char_end,
               f.path, f.ext, f.mtime_ns, f.taken_at_ns, f.content_hash,
                   f.volume_id, f.relative_path,
               0.0 AS score"""


#: **The bounded newest-first walk `_filter_only` tries first.** The first
#: window is `max(4 x limit, 400)` files, the widest it will grow to is
#: `max(24 x limit, 2400)`. Measured 2026-09-20 on 200,000 files, limit 100 -
#: see `_filter_only`.
_BROWSE_FIRST_WINDOW = (4, 400)
_BROWSE_WIDEST_WINDOW = (24, 2400)


def _newest_matching(store: Any, where: str, params: list[Any], limit: int,
                     *, photos: bool) -> list[dict[str, Any]]:
    """The `limit` newest matching first-chunks of one half of `files`.

    `photos=False` is the files with no shot date, newest by `mtime_ns`;
    `photos=True` is the ones with a shot date, newest by `taken_at_ns`. See
    `_filter_only` for why they are two queries.

    **Adaptive, and correct however the guess turns out.** SQLite plans a
    filter-only browse as one of two things and neither is right for every
    filter: walk the ext index and sort every match (`type:pdf`, half the
    files: 177 ms at 200,000), or walk `idx_files_mtime` newest-first and stop
    at `LIMIT` (0.6 ms for pdf, but **2.9 s** for a type with no matches,
    because it visits every file looking for one). So this walks the newest
    files through a *window* first - `limit` rows out of the newest N files is
    exactly the answer, since anything outside the window is older than all of
    it - and only when the window is too short does it fall back to the plain
    statement, which lets SQLite use the ext index. The projection below is
    only a decision about whether the wider window is worth trying; what is
    returned is never taken from a window unless it is full or exhaustive.
    """
    if photos:
        window_from = ("SELECT * FROM files WHERE taken_at_ns IS NOT NULL "
                       "ORDER BY taken_at_ns DESC LIMIT ?")
        gate, order = "f.taken_at_ns IS NOT NULL", "f.taken_at_ns"
        size_probe = "SELECT 1 FROM files WHERE taken_at_ns IS NOT NULL LIMIT ?"
    else:
        window_from = "SELECT * FROM files ORDER BY mtime_ns DESC LIMIT ?"
        gate, order = "f.taken_at_ns IS NULL", "f.mtime_ns"
        size_probe = "SELECT 1 FROM files LIMIT ?"

    # **The same gate `_filter_only` puts on the whole browse, per half.**
    # Since schema v27 every message carries its sent date in `taken_at_ns`,
    # so neither half is reliably small any more, and a half with no match for
    # the filter at all - `type:pdf` among the dated rows, `type:mail` among
    # the undated ones - walked its whole date index looking for one before
    # this. Measured 2026-09-27 on 200,000 files (60,000 messages), limit 100:
    # `type:mail` 17 ms before v27, 176 ms after it without this gate; `type:
    # pdf` 31 ms and 77 ms. Asking "are there `limit` of these in this half"
    # without an ORDER BY lets SQLite use the filter's own index and stop at
    # `limit`, so a sparse half is read directly and never walked.
    few = [int(row[0]) for row in store.conn.execute(
        f"SELECT f.id FROM files f WHERE {gate}{where} LIMIT ?",
        [*params, limit]).fetchall()]
    if len(few) < limit:
        if not few:
            return []
        listed = ", ".join(str(n) for n in few)      # ids from the database
        rows = store.conn.execute(f"""
            SELECT {_FILTER_ONLY_COLUMNS}
            FROM chunks c
            JOIN files f ON f.id = c.file_id
            WHERE c.ordinal = 0 AND f.id IN ({listed})
            ORDER BY {order} DESC
        """).fetchall()
        return [dict(row) for row in rows]

    def window(size: int) -> list[dict[str, Any]]:
        rows = store.conn.execute(f"""
            SELECT {_FILTER_ONLY_COLUMNS}
            FROM ({window_from}) f
            JOIN chunks c ON c.file_id = f.id AND c.ordinal = 0
            WHERE {gate}{where}
            ORDER BY {order} DESC
            LIMIT ?
        """, [size, *params, limit]).fetchall()
        return [dict(row) for row in rows]

    def whole_branch_fits(size: int) -> bool:
        """True when every file of this half is inside a window of `size`, so
        a short window is the complete answer and there is nothing to fall
        back for (most test databases, and the photo half of nearly every
        real one)."""
        row = store.conn.execute(
            f"SELECT COUNT(*) AS n FROM ({size_probe})", [size + 1]).fetchone()
        return int(row[0]) <= size

    first = max(_BROWSE_FIRST_WINDOW[0] * limit, _BROWSE_FIRST_WINDOW[1])
    widest = max(_BROWSE_WIDEST_WINDOW[0] * limit, _BROWSE_WIDEST_WINDOW[1])

    rows = window(first)
    if len(rows) >= limit or whole_branch_fits(first):
        return rows
    if rows:
        # A rough guess at how far down the list `limit` matches would be. Only
        # a decision about whether to try; it does not touch what is returned.
        # Tried only with a 1.5x margin, so a filter sitting on the edge of what
        # the widest window can reach goes straight to the plain statement
        # instead of paying for a wide walk that then falls short.
        projected = -(-limit * first // len(rows))
        if projected * 3 // 2 <= widest:
            wider = projected * 3 // 2 + 1
            rows = window(wider)
            if len(rows) >= limit or whole_branch_fits(wider):
                return rows
    return _plain_filter_only(store, where, params, limit, photos=photos)


def _plain_filter_only(store: Any, where: str, params: list[Any], limit: int,
                       *, photos: bool) -> list[dict[str, Any]]:
    """The statement with no window: SQLite chooses its own plan (the ext index
    for `type:`, the mtime index for a date) and it is the answer for every
    filter that matches few files."""
    gate, order = (("f.taken_at_ns IS NOT NULL", "f.taken_at_ns") if photos
                   else ("f.taken_at_ns IS NULL", "f.mtime_ns"))
    rows = store.conn.execute(f"""
        SELECT {_FILTER_ONLY_COLUMNS}
        FROM chunks c
        JOIN files f ON f.id = c.file_id
        WHERE c.ordinal = 0 AND {gate}{where}
        ORDER BY {order} DESC
        LIMIT ?
    """, [*params, limit]).fetchall()
    return [dict(row) for row in rows]


def _filter_only(store: Any, where: str, params: list[Any], limit: int) -> list[dict[str, Any]]:
    """`type:pdf after:2024` with no search terms - list what matches.

    Ordered newest first, because a query that is purely a filter is a browse,
    and recency is the only ranking signal available without a search term -
    work order 0f section 3a's third clause, "any date display use it", applies
    here too: this ordering decides *which* files survive `LIMIT` before
    fusion ever sees them, not only how the survivors get labelled.

    **Two queries and a merge, not `ORDER BY COALESCE(f.taken_at_ns,
    f.mtime_ns) DESC`.** See `app.storage.filters.merge_by_date` for the
    measurement: the COALESCE form is not sargable, so it stops SQLite
    walking `idx_files_mtime` newest-first and looking up each file's first
    chunk through `idx_chunks_file_ord` - the mechanism `idx_files_mtime`'s
    own comment in `schema.sql` describes and `test_filter_only_browse_
    neither_scans_nor_sorts` pins the plan for. Splitting on `taken_at_ns
    IS [NOT] NULL` keeps each half on its own indexed column, so both still
    walk their index in order and stop at `LIMIT`.

    **2026-09-20: and each half walks a bounded window first**
    (`_newest_matching`). Measured on 200,000 files (50% pdf, 15% txt, 4% xlsx,
    1% dwg), limit 100, best of 5, on a machine other work was running on:

        filter       before      after
        type:pdf     265 ms      see the test's docstring
        type:zzz     0.2 ms      (no matches: never leaves the ext index)

    The gate below is what keeps the no-match case where it was: it asks for
    up to `limit` matching files by id. Fewer than `limit` means that is
    *every* match, so they are read directly (one pass, no walk, no second
    scan for a filter the planner cannot index) - which for `type:` with no
    matches is a seek on the ext index and nothing else.
    """
    if limit <= 0:
        return []
    matching = [int(row[0]) for row in store.conn.execute(
        f"SELECT f.id FROM files f WHERE 1=1{where} LIMIT ?",
        [*params, limit]).fetchall()]
    if len(matching) < limit:
        if not matching:
            return []
        # Ids straight from the database, so inlining them is not an injection
        # route and avoids a bound-variable count that grows with `limit`.
        listed = ", ".join(str(n) for n in matching)
        rows = [dict(row) for row in store.conn.execute(f"""
            SELECT {_FILTER_ONLY_COLUMNS}
            FROM chunks c
            JOIN files f ON f.id = c.file_id
            WHERE c.ordinal = 0 AND f.id IN ({listed})
        """).fetchall()]
        return merge_by_date(
            [row for row in rows if row.get("taken_at_ns") is None],
            [row for row in rows if row.get("taken_at_ns") is not None],
            limit=limit, newest_first=True)
    return merge_by_date(
        _newest_matching(store, where, params, limit, photos=False),
        _newest_matching(store, where, params, limit, photos=True),
        limit=limit, newest_first=True)


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
