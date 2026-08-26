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

__all__ = ["file_filter_sql", "epoch_ns", "MAIL_KINDS"]

#: `files.source_kind` values that count as mail. **A fact about the table**,
#: which is why it is stated here rather than in the parser that reads it -
#: `app/search/query.py` re-exports it so the older spelling still resolves.
MAIL_KINDS = ("pst_message", "eml")


def file_filter_sql(parsed: Any) -> tuple[str, list[Any]]:
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
        params.append(epoch_ns(parsed.after))

    if parsed.before is not None:
        clauses.append("f.mtime_ns <= ?")
        params.append(epoch_ns(parsed.before, end_of_day=True))

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
