r"""Order 0m §2d - `file_filter_sql` under fuzz: no injection shape possible,
and the LIKE-escaping it depends on holds for arbitrary input.

Layer: L1

`app/storage/filters.py`'s own docstring states the property already:
*"every value is a bound parameter, and no user input reaches the SQL
text."* These tests do not take that on trust - they generate queries
hypothesis picks, parse them with the real parser, and execute the SQL
`file_filter_sql` builds against a real migrated `SqliteStore` schema (the
same one `app/storage/sqlite_store.py` embeds it into), the same reasoning
`test_query.py` already uses for `to_fts_match`: if it parses and executes
against the real thing, it is real, not merely plausible.
"""

from __future__ import annotations

import re

from hypothesis import given
from hypothesis import strategies as st

from app.search.query import parse_query
from app.storage.filters import file_filter_sql
from app.storage.like import contains, like_escape

#: Words plus the characters that make LIKE and SQL interesting: quotes,
#: percent, underscore, backslash, semicolons - the shapes an injection
#: attempt or an accidental wildcard would take.
_SPICY_WORD = st.text(
    alphabet=st.characters(
        min_codepoint=97, max_codepoint=122
    ) | st.sampled_from(["%", "_", "\\", "'", '"', ";", "-", "."]),
    min_size=1, max_size=16,
)

_OPERATORS = ["path", "repo", "name", "from", "to", "subject", "on", "shows", "who"]


@st.composite
def _filter_query(draw) -> str:
    op = draw(st.sampled_from(_OPERATORS))
    value = draw(_SPICY_WORD)
    # A colon-valued operator, optionally negated and/or quoted - the two
    # axes item 2a already fuzzes for the parser; here the point is what
    # reaches `file_filter_sql`, not the parser itself.
    negated = "-" if draw(st.booleans()) else ""
    quoted = draw(st.booleans())
    rendered = f'"{value}"' if quoted else value
    return f"{negated}{op}:{rendered}"


def _executes_cleanly(where: str, params: list) -> None:
    """Run the fragment against the real schema - a syntax error or a
    mismatched parameter count raises here, which is the test failing."""
    import tempfile
    from pathlib import Path

    from app.storage.sqlite_store import SqliteStore

    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "filters.db"
        with SqliteStore(db_path).connect() as store:
            store.conn.execute(
                f"SELECT f.id FROM files f WHERE 1=1{where}", params
            ).fetchall()


@given(_filter_query())
def test_the_where_fragment_always_executes_against_the_real_schema(raw: str) -> None:
    parsed = parse_query(raw)
    where, params = file_filter_sql(parsed)
    _executes_cleanly(where, params)


@given(_filter_query())
def test_the_placeholder_count_always_matches_the_param_count(raw: str) -> None:
    parsed = parse_query(raw)
    where, params = file_filter_sql(parsed)
    assert where.count("?") == len(params)


@given(_filter_query())
def test_no_raw_filter_value_ever_appears_as_literal_sql_text(raw: str) -> None:
    """The injection-shape property: every value the person typed travels as
    a bound parameter, never concatenated into the SQL string itself. This
    checks the *values*, not the whole query - `raw` also contains the
    operator name and punctuation that legitimately never reaches `where`
    (or reaches it only as part of the fixed clause text, e.g. `LIKE`)."""
    parsed = parse_query(raw)
    where, params = file_filter_sql(parsed)
    candidate_values = [
        *parsed.paths, *parsed.repos, *parsed.names, *parsed.senders,
        *parsed.recipients, *parsed.subjects, *parsed.volumes,
        *parsed.shows, *parsed.who,
        *parsed.not_paths, *parsed.not_repos, *parsed.not_names,
        *parsed.not_senders, *parsed.not_recipients, *parsed.not_subjects,
        *parsed.not_volumes, *parsed.not_shows, *parsed.not_who,
    ]
    for value in candidate_values:
        if not value:
            continue
        # A one- or two-character value can legitimately be a substring of
        # the fixed SQL scaffolding (`f.id`, `AND`, ...) by pure coincidence
        # - the property that matters is about values long enough to be
        # distinctive, matching how `test_sanitise.py` restricts itself to
        # meaningful markers rather than any substring at all.
        if len(value) < 3:
            continue
        assert value not in where, f"{value!r} leaked into the SQL text: {where}"


@given(_SPICY_WORD)
def test_like_escape_never_raises_and_only_grows(value: str) -> None:
    escaped = like_escape(value)
    assert isinstance(escaped, str)
    assert len(escaped) >= len(value)


@given(_SPICY_WORD)
def test_every_percent_and_underscore_in_the_source_is_escaped(value: str) -> None:
    """A literal `%`/`_` in the value must not survive as an unescaped LIKE
    wildcard - each one in the input must be preceded by the escape
    character in the output, for every occurrence, not just the first."""
    escaped = like_escape(value)
    for wildcard in ("%", "_"):
        # Every occurrence of the wildcard in `escaped` must be one this
        # function put there as an escape (`\%` / `\_`) - i.e. immediately
        # preceded by a backslash - never a bare, unescaped one.
        for match in re.finditer(re.escape(wildcard), escaped):
            assert match.start() > 0 and escaped[match.start() - 1] == "\\", (
                f"unescaped {wildcard!r} survived in {escaped!r} (from {value!r})"
            )


@given(_SPICY_WORD)
def test_contains_wraps_an_escaped_lowercase_pattern(value: str) -> None:
    pattern = contains(value)
    assert pattern.startswith("%")
    assert pattern.endswith("%")
    assert pattern[1:-1] == like_escape(value.lower())
