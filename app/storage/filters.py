r"""What one `/switch` means, as SQL over the `files` table. One definition.

Layer: L1

**This lives below search on purpose, and it used to live inside it.** The
builder was `keyword._filter_sql`, private to Layer 4, so the only code that
could use it was the generic search. Every other surface that needed to honour a
filter wrote its own smaller version:

  * the Files tab understood `type:` and a name, and silently dropped
    `path:`, `after:`, `before:`, `size:`, `repo:` and every mail field;
  * the Code tab understood `repo:` and `type:`, and dropped `name:` and
    `path:` - both of which its own dropdown offered;
  * Mail understood the five columns `messages` has and nothing about the file
    the message came from.

The result was the owner's report: *"the results should be same across but only
applicable to the tab, this is not the case"*. Three implementations of one
grammar will always drift, and these had - not because anybody was careless, but
because there was no single place for them to agree.

So the meaning of a switch is defined once, here, and every caller composes the
same fragment. A tab decides **which rows it is about** - files, messages,
repository files - and never what `size:>1mb` means.

The fragment is written against the alias `f` for `files`, and is safe to
concatenate: every value is a bound parameter, and no user input reaches the
SQL text.

`parsed` is duck-typed rather than annotated as `ParsedQuery`, which is Layer 4
and cannot be imported from here. It needs the attributes `ParsedQuery` has;
anything carrying them will do, which is also what makes this testable without
building a parser.
"""

from __future__ import annotations

from typing import Any

from app.storage.like import ESCAPE, contains

__all__ = ["file_filter_sql", "epoch_ns", "MAIL_KINDS", "merge_by_date"]

#: `files.source_kind` values that count as mail. **A fact about the table**,
#: which is why it is stated here rather than in the parser that reads it -
#: `app/search/query.py` re-exports it so the older spelling still resolves.
MAIL_KINDS = ("pst_message", "eml")


def _date_clause(op: str) -> str:
    r"""`after:`/`before:` against **the date a file is from**, not its mtime.

    Work order 0f §3a, decision 3: *"EXIF date is THE date for photos."* A
    photograph taken in 2006 and copied to a new drive in 2019 has a 2019
    `mtime_ns` and nothing anyone would call a 2019 document. `files.taken_at_ns`
    holds its EXIF shot date when there is one (see `_v18_photo_taken_at`), and
    is NULL for every file that is not a photograph - which is where `mtime_ns`
    remains exactly the right answer and is used unchanged.

    **Written as an explicit two-branch OR rather than the obvious COALESCE,
    and the reason is measured, not stylistic.**
    `COALESCE(f.taken_at_ns, f.mtime_ns) <= ?` wraps the columns in a function,
    so no index can serve it and every date filter becomes a full scan of
    `files`. On a 200,000-row table with a selective cutoff:

        plain `f.mtime_ns <= ?`   0.37ms   SEARCH ... COVERING INDEX idx_files_mtime
        COALESCE form             5.79ms   SCAN f
        this form                 0.86ms   MULTI-INDEX OR, both indexes

    `files` is aimed at twenty million rows and this runs on every keystroke of
    a filtered search, so the fifteen-fold difference is the whole argument.
    Each branch is a bare column comparison, which lets SQLite seek
    `idx_files_mtime` for the ordinary files and `idx_files_taken_at` for the
    photographs and union the two.

    The branches are mutually exclusive by construction - `taken_at_ns` is
    either NULL or it is not - so no row can match twice and the OR needs no
    DISTINCT. `op` is only ever `>=` or `<=` from the two call sites above; it
    is never user input.
    """
    return (f"((f.taken_at_ns IS NULL AND f.mtime_ns {op} ?) "
            f"OR (f.taken_at_ns IS NOT NULL AND f.taken_at_ns {op} ?))")


def merge_by_date(branch_a: list, branch_b: list, *, limit: int,
                   newest_first: bool = True) -> list:
    r"""Two already-`LIMIT`-bounded row lists, merged by shot-date-or-mtime.

    Work order 0f §3a's third clause: "any date display use it" - and that
    includes a "/newest"/"/oldest" browse, which decides *which rows survive
    a LIMIT* at all, not only how the survivors happen to be labelled.

    **Why this is two queries and a merge, not one query with `ORDER BY
    COALESCE(f.taken_at_ns, f.mtime_ns)`.** The COALESCE form was measured
    directly, the same way `_date_clause` measured its own WHERE form:
    against a 200,000-file table (600,000 chunk rows, matching this
    project's own chunks-per-file ratio) with `ORDER BY f.mtime_ns DESC
    LIMIT 20`, the query planner walks `idx_files_mtime` newest-first and
    stops after twenty files - the mechanism `idx_files_mtime`'s own comment
    in `schema.sql` documents and `test_filter_only_browse_neither_scans_
    nor_sorts` pins. Wrapping the sort key in `COALESCE` (or an equivalent
    `CASE`) makes it a computed expression no index can serve, forcing
    SQLite to materialise and sort every matching row before `LIMIT` can
    discard any of them:

        plain `ORDER BY f.mtime_ns DESC`                    0.011 ms
        `ORDER BY COALESCE(f.taken_at_ns, f.mtime_ns) DESC`  39.5  ms
        `ORDER BY CASE ... END DESC`                         42.2  ms
        this function (two queries + Python merge)           0.10  ms

    on the same 200,000-file table. `files` is aimed at twenty million rows,
    where that gap is not a rounding error.

    **The shape.** `taken_at_ns` is NULL for every file that is not a
    photograph, which is nearly all of them (`idx_files_taken_at` is
    partial for the same reason). So the correct order splits cleanly along
    that NULL, and each half is already sorted correctly by its own plain,
    indexed column: rows with no shot date sort by `mtime_ns`, rows with one
    sort by `taken_at_ns`. The caller runs both queries - each
    `ORDER BY <its own column> LIMIT limit`, using `idx_files_mtime` and
    `idx_files_taken_at` exactly as `_date_clause` already does for `WHERE`
    - and this function does the merge.

    **Why `limit` from each side is enough, not `limit` from one and the
    rest from the other.** Any row in the true top-`limit` by effective date
    belongs to exactly one of the two partitions, and within its own
    partition it can have at most `limit - 1` rows ranked ahead of it in the
    combined order - otherwise the combined top-`limit` would not contain it
    at all. So it is always within its own partition's top `limit`, and
    fetching `limit` from each side can never miss it. (A standard property
    of a top-k merge over a partition; it does not depend on `taken_at_ns`
    or `mtime_ns` specifically.)

    Every row must already be a plain `dict` (as every call site already
    produces via `dict(row)`) carrying both `mtime_ns` and `taken_at_ns` -
    `None` or absent reads as "no shot date", the same convention
    `SearchResult.taken_at_ns` uses.
    """
    combined = list(branch_a) + list(branch_b)
    combined.sort(
        key=lambda row: int(row.get("taken_at_ns") or row.get("mtime_ns") or 0),
        reverse=newest_first,
    )
    return combined[: max(0, int(limit))]


def file_filter_sql(parsed: Any) -> tuple[str, list[Any]]:
    """Build the WHERE fragment for a ParsedQuery's operators."""
    clauses: list[str] = []
    params: list[Any] = []

    if parsed.ext:
        placeholders = ", ".join("?" for _ in parsed.ext)
        clauses.append(f"f.ext IN ({placeholders})")
        params.extend(ext.lstrip(".").lower() for ext in parsed.ext)

    if parsed.after is not None:
        # Dates from the query are whole days, so the comparison is against
        # midnight. See `_date_clause` for which column is compared.
        clauses.append(_date_clause(">="))
        params.extend([epoch_ns(parsed.after)] * 2)

    if parsed.before is not None:
        clauses.append(_date_clause("<="))
        params.extend([epoch_ns(parsed.before, end_of_day=True)] * 2)

    # **No `LOWER()` on any of the LIKE predicates below.**
    #
    # SQLite's LIKE is already case-insensitive for ASCII, so `LOWER(col) LIKE
    # '%dave%'` and `col LIKE '%dave%'` return the same rows - verified on a
    # deliberately mixed-case fixture, 1,650 rows either way. The wrapper only
    # added a function call per row: measured on 60,000 messages, 11.54ms with
    # it against 4.64ms without, a **2.49x cut** on the dominant cost of every
    # mail filter.
    #
    # This is safe only while LIKE stays case-insensitive.
    # `tests/unit/test_query_plans.py` fails if `PRAGMA case_sensitive_like` is
    # ever turned on, because that would silently make `from:dave` stop
    # matching `Dave@...` rather than break anything loudly.
    #
    # It does **not** make these use an index, and nothing can: a leading `%`
    # means there is no prefix to seek to. The scan is at least a covering one.
    for folder in parsed.paths:
        clauses.append("f.path LIKE ?" + ESCAPE)
        params.append(contains(folder))

    # **Name or root path, and the caller does not have to say which.**
    #
    # People refer to a project by its name and to a checkout by its path, and
    # which one they reach for is not predictable - `repo:leasha` and
    # `repo:D:\SearchProject` are the same question asked two ways. Matching
    # both costs one extra comparison against a table with as many rows as the
    # machine has repositories, which is tens.
    #
    # **ORed, where every other repeated filter ANDs.** That is deliberate and
    # it is not a style choice: a file belongs to exactly one repository, so
    # `repo:a repo:b` under the usual AND matches nothing, ever. The first
    # version of this built one clause per name and `repo:leasha,tools`
    # silently returned zero rows - a filter that looks like it works and
    # cannot. One clause, one subquery, `IN`.
    if parsed.repos:
        conditions = " OR ".join(
            "name = ? COLLATE NOCASE OR root_path LIKE ?" + ESCAPE for _ in parsed.repos
        )
        clauses.append(f"f.repo_id IN (SELECT id FROM repos WHERE {conditions})")
        for repo in parsed.repos:
            params.extend((repo, contains(repo)))

    # Work order 0i section 1c. `file_tags` (schema v19) is Florence-2's tag
    # vocabulary. ORed within the tuple, the same convention `parsed.repos`
    # uses just above and for the same reason: `shows:dog,cat` asks for
    # either, not both, and a file commonly carries several tags so an AND
    # reading of one filter occurrence would be a different (and unasked)
    # question. Unlike `repo_id` there is no single column to `IN (...)`
    # against, so this is a subquery over the join table instead.
    if parsed.shows:
        conditions = " OR ".join("tag = ? COLLATE NOCASE" for _ in parsed.shows)
        clauses.append(f"f.id IN (SELECT file_id FROM file_tags WHERE {conditions})")
        params.extend(parsed.shows)

    for name in parsed.names:
        # The **basename**, not the whole path - `path:` already answers "which
        # folder", and matching the full path here would make `name:leeds` hit
        # every file in a Leeds directory, which is a different and much larger
        # answer than the one asked for.
        #
        # `parent_dir` is stored, so removing it from `path` leaves the name,
        # without any assumption about which slash this platform uses.
        clauses.append("REPLACE(f.path, f.parent_dir, '') LIKE ?" + ESCAPE)
        params.append(contains(name))

    # --- the negated halves -------------------------------------------------
    #
    # **`-type:pdf` used to return only PDFs.** The parser lost the minus, so
    # every exclusion arrived here as its opposite - see `query._OPERATOR`.
    # Each clause below is the mirror of the positive one directly above it,
    # deliberately written out rather than generated: a loop over
    # `(field, negated)` pairs would put the sense of the comparison in a
    # variable, and getting that wrong is exactly the bug being fixed.
    #
    # `NOT LIKE` is NULL-safe here because none of these columns is nullable
    # except `repo_id`, which is handled by its own `IS NULL` below.
    if getattr(parsed, "not_ext", ()):
        placeholders = ", ".join("?" for _ in parsed.not_ext)
        clauses.append(f"f.ext NOT IN ({placeholders})")
        params.extend(ext.lstrip(".").lower() for ext in parsed.not_ext)

    for folder in getattr(parsed, "not_paths", ()):
        clauses.append("f.path NOT LIKE ?" + ESCAPE)
        params.append(contains(folder))

    for name in getattr(parsed, "not_names", ()):
        clauses.append("REPLACE(f.path, f.parent_dir, '') NOT LIKE ?" + ESCAPE)
        params.append(contains(name))

    if getattr(parsed, "not_shows", ()):
        conditions = " OR ".join("tag = ? COLLATE NOCASE" for _ in parsed.not_shows)
        clauses.append(
            f"f.id NOT IN (SELECT file_id FROM file_tags WHERE {conditions})")
        params.extend(parsed.not_shows)

    if getattr(parsed, "not_repos", ()):
        # **`OR f.repo_id IS NULL` is the whole difference.** Most files belong
        # to no repository at all, and `repo_id NOT IN (...)` is NULL - not
        # true - for every one of them. Without it, excluding one checkout
        # would also exclude the entire rest of the corpus, which is a far
        # bigger wrong answer than the one being fixed.
        conditions = " OR ".join(
            "name = ? COLLATE NOCASE OR root_path LIKE ?" + ESCAPE for _ in parsed.not_repos
        )
        clauses.append(
            f"(f.repo_id IS NULL OR f.repo_id NOT IN "
            f"(SELECT id FROM repos WHERE {conditions}))")
        for repo in parsed.not_repos:
            params.extend((repo, contains(repo)))

    for comparison, size in parsed.sizes:
        # The comparison came from `_parse_size`, which only ever returns one of
        # these five - so it can go into the SQL text safely, and never from
        # anything the user typed directly.
        if comparison in ("<", "<=", ">", ">=", "="):
            clauses.append(f"f.size_bytes {comparison} ?")
            params.append(size)

    # Mail or documents, from the scope chips.
    #
    # This comment used to say `source_kind` was "already indexed, so this costs
    # nothing". **There was no such index**, and clicking the Mail chip
    # full-scanned `files` on every keystroke. `idx_files_source_kind` exists
    # now (schema v5) - and the lesson is that a comment asserting a fact about
    # the schema is worth exactly as much as the schema agreeing with it.
    if parsed.scope == "mail":
        placeholders = ", ".join("?" for _ in MAIL_KINDS)
        clauses.append(f"f.source_kind IN ({placeholders})")
        params.extend(MAIL_KINDS)
    elif parsed.scope == "documents":
        placeholders = ", ".join("?" for _ in MAIL_KINDS)
        clauses.append(f"f.source_kind NOT IN ({placeholders})")
        params.extend(MAIL_KINDS)
    elif parsed.scope == "code":
        # **"In a repository", not "has a code extension".** `type:code` is the
        # other question and is untouched; this is the one that separates a
        # work project from the same words in a document. A `.md` file in a
        # repository is in scope, a `.py` file in Downloads is not.
        #
        # Note that `documents` above deliberately still includes repository
        # files. The scope is a narrowing offered to the person searching, not
        # a partition of the corpus - somebody looking through Documents for a
        # connection string they know they wrote must still find it.
        clauses.append("f.repo_id IS NOT NULL")

    # --- mail fields, all on the `messages` table -------------------------
    #
    # **Substring, not equality.** `from:dave` used `LOWER(sender) IN (...)`,
    # an exact match against the whole address - so it found a message only if
    # somebody had typed `dave.smith@acme.com` in full, and `from:dave` matched
    # nothing at all. Nobody searches that way, and the filter looked broken
    # rather than strict. Every mail field below matches on any part.
    for sender in parsed.senders:
        clauses.append(
            "f.id IN (SELECT file_id FROM messages "
            f"WHERE COALESCE(sender_lc, sender) LIKE ?{ESCAPE})"
        )
        params.append(contains(sender))

    for recipient in parsed.recipients:
        # `recipients` is a JSON array, and matching inside the text is enough:
        # an address is distinctive, and parsing JSON per row to do it properly
        # would cost far more than it could ever save.
        clauses.append(
            "f.id IN (SELECT file_id FROM messages "
            f"WHERE COALESCE(recipients_lc, recipients) LIKE ?{ESCAPE})"
        )
        params.append(contains(recipient))

    for subject in parsed.subjects:
        clauses.append(
            "f.id IN (SELECT file_id FROM messages "
            f"WHERE COALESCE(subject_lc, subject) LIKE ?{ESCAPE})"
        )
        params.append(contains(subject))

    # The negated mail fields. **`NOT IN` rather than `IN (... NOT LIKE ...)`**:
    # the second form asks "is there any message row whose sender is not this",
    # which is true for almost every file in the index and excludes nothing.
    # The question is whether *this* file's message matches, so the negation
    # belongs outside the subquery.
    for sender in getattr(parsed, "not_senders", ()):
        clauses.append(
            "f.id NOT IN (SELECT file_id FROM messages "
            f"WHERE COALESCE(sender_lc, sender) LIKE ?{ESCAPE})"
        )
        params.append(contains(sender))

    for recipient in getattr(parsed, "not_recipients", ()):
        clauses.append(
            "f.id NOT IN (SELECT file_id FROM messages "
            f"WHERE COALESCE(recipients_lc, recipients) LIKE ?{ESCAPE})"
        )
        params.append(contains(recipient))

    for subject in getattr(parsed, "not_subjects", ()):
        clauses.append(
            "f.id NOT IN (SELECT file_id FROM messages "
            f"WHERE COALESCE(subject_lc, subject) LIKE ?{ESCAPE})"
        )
        params.append(contains(subject))

    if parsed.has_attachment is not None:
        clauses.append(
            "f.id IN (SELECT file_id FROM messages WHERE has_attach = ?)"
        )
        params.append(1 if parsed.has_attachment else 0)

    return (" AND " + " AND ".join(clauses) if clauses else ""), params


def epoch_ns(day: Any, *, end_of_day: bool = False) -> int:
    from datetime import datetime
    from datetime import time as time_of_day

    moment = datetime.combine(day, time_of_day.max if end_of_day else time_of_day.min)
    return int(moment.timestamp() * 1_000_000_000)
