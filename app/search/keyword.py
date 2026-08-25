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
from app.search.query import MAIL_KINDS, ParsedQuery, _fts_quote

__all__ = ["search", "KEYWORD_LIMIT"]

#: Candidates handed to fusion. From the spec's pipeline diagram.
KEYWORD_LIMIT = 100

_log = logger.bind(component="search.keyword")


def _filter_sql(parsed: ParsedQuery) -> tuple[str, list[Any]]:
    """Build the WHERE fragment for a ParsedQuery's operators."""
    clauses: list[str] = []
    params: list[Any] = []

    if parsed.ext:
        placeholders = ", ".join("?" for _ in parsed.ext)
        clauses.append(f"f.ext IN ({placeholders})")
        params.extend(ext.lstrip(".").lower() for ext in parsed.ext)

    if parsed.after is not None:
        # mtime_ns, because that is what the files table stores. Dates from the
        # query are whole days, so the comparison is against midnight.
        clauses.append("f.mtime_ns >= ?")
        params.append(_epoch_ns(parsed.after))

    if parsed.before is not None:
        clauses.append("f.mtime_ns <= ?")
        params.append(_epoch_ns(parsed.before, end_of_day=True))

    for folder in parsed.paths:
        clauses.append("LOWER(f.path) LIKE ?")
        params.append(f"%{folder.lower()}%")

    for name in parsed.names:
        # The **basename**, not the whole path - `path:` already answers "which
        # folder", and matching the full path here would make `name:leeds` hit
        # every file in a Leeds directory, which is a different and much larger
        # answer than the one asked for.
        #
        # `parent_dir` is stored, so removing it from `path` leaves the name,
        # without any assumption about which slash this platform uses.
        clauses.append("LOWER(REPLACE(f.path, f.parent_dir, '')) LIKE ?")
        params.append(f"%{name.lower()}%")

    for comparison, size in parsed.sizes:
        # The comparison came from `_parse_size`, which only ever returns one of
        # these five - so it can go into the SQL text safely, and never from
        # anything the user typed directly.
        if comparison in ("<", "<=", ">", ">=", "="):
            clauses.append(f"f.size_bytes {comparison} ?")
            params.append(size)

    # Mail or documents, from the scope chips. `source_kind` is on `files` and
    # already indexed, so this costs nothing.
    if parsed.scope == "mail":
        placeholders = ", ".join("?" for _ in MAIL_KINDS)
        clauses.append(f"f.source_kind IN ({placeholders})")
        params.extend(MAIL_KINDS)
    elif parsed.scope == "documents":
        placeholders = ", ".join("?" for _ in MAIL_KINDS)
        clauses.append(f"f.source_kind NOT IN ({placeholders})")
        params.extend(MAIL_KINDS)

    # --- mail fields, all on the `messages` table -------------------------
    #
    # **Substring, not equality.** `from:dave` used `LOWER(sender) IN (...)`,
    # an exact match against the whole address - so it found a message only if
    # somebody had typed `dave.smith@acme.com` in full, and `from:dave` matched
    # nothing at all. Nobody searches that way, and the filter looked broken
    # rather than strict. Every mail field below matches on any part.
    for sender in parsed.senders:
        clauses.append(
            "f.id IN (SELECT file_id FROM messages WHERE LOWER(sender) LIKE ?)"
        )
        params.append(f"%{sender.lower()}%")

    for recipient in parsed.recipients:
        # `recipients` is a JSON array, and matching inside the text is enough:
        # an address is distinctive, and parsing JSON per row to do it properly
        # would cost far more than it could ever save.
        clauses.append(
            "f.id IN (SELECT file_id FROM messages WHERE LOWER(recipients) LIKE ?)"
        )
        params.append(f"%{recipient.lower()}%")

    for subject in parsed.subjects:
        clauses.append(
            "f.id IN (SELECT file_id FROM messages WHERE LOWER(subject) LIKE ?)"
        )
        params.append(f"%{subject.lower()}%")

    if parsed.has_attachment is not None:
        clauses.append(
            "f.id IN (SELECT file_id FROM messages WHERE has_attach = ?)"
        )
        params.append(1 if parsed.has_attachment else 0)

    return (" AND " + " AND ".join(clauses) if clauses else ""), params


def _epoch_ns(day: Any, *, end_of_day: bool = False) -> int:
    from datetime import datetime
    from datetime import time as time_of_day

    moment = datetime.combine(day, time_of_day.max if end_of_day else time_of_day.min)
    return int(moment.timestamp() * 1_000_000_000)


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
