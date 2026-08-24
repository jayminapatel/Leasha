"""Query parsing: typed operators, phrases, exclusions, and a crash-proof FTS5 sanitiser.

Layer: L4

Two jobs, both pure and both free.

**Sanitising.** `search_bm25()` currently swallows `sqlite3.OperationalError` and
returns [] on a malformed query. That keeps the app alive but tells the user their
search "found nothing" when what really happened is that they typed an unbalanced
quote. This module builds a MATCH expression that *cannot* be malformed, so an
empty result set means empty, not broken.

**Operators.** `type:pdf after:2024 "site survey" -draft` typed straight into the
search bar. The UI has filter chips, but chips need a mouse, and the spec requires
keyboard-only operation end to end. Parsing costs microseconds and never touches a
model, so it is free against the <300ms budget - unlike LLM query rewriting, which
would put a model in the hot path and is rejected in BUILD_SPEC_V2.md for that reason.

Nothing here does I/O, imports a model, or reaches Layers 2-3. It is a pure
string -> dataclass transform and is fully tested today.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Optional

__all__ = [
    "ParsedQuery",
    "parse_query",
    "to_fts_match",
    "MAX_QUERY_CHARS",
    "MAX_TERMS",
]

# A search box accepts a paste of anything. Both caps are performance guards:
# the first bounds the regex work, the second bounds how much FTS5 is asked to do.
MAX_QUERY_CHARS = 4096
MAX_TERMS = 64

# field:value, where value is either "a quoted string" or a bare run of non-space.
_OPERATOR = re.compile(
    r'\b(?P<field>type|ext|kind|after|since|before|until|path|folder|dir|from|sender)'
    r':(?P<value>"[^"]*"|\S+)',
    re.IGNORECASE,
)
_PHRASE = re.compile(r'"([^"]*)"')
# Unicode-aware word run. Keeps intra-word . _ - ' so that "v1.2", "some_file" and
# "o'brien" survive as single terms; strips emoji and punctuation, which are not
# indexed and would only ever be FTS5 syntax errors waiting to happen.
_TERM = re.compile(r"[^\W_]+(?:[._'\-][^\W_]+)*\*?", re.UNICODE)

_FIELD_ALIASES = {
    "type": "ext", "ext": "ext", "kind": "ext",
    "after": "after", "since": "after",
    "before": "before", "until": "before",
    "path": "path", "folder": "path", "dir": "path",
    "from": "sender", "sender": "sender",
}

# type:doc should find .doc and .docx; the user is naming a kind, not an extension.
_EXT_GROUPS = {
    "doc": ("doc", "docx"),
    "word": ("doc", "docx"),
    "xls": ("xls", "xlsx", "xlsm"),
    "excel": ("xls", "xlsx", "xlsm"),
    "sheet": ("xls", "xlsx", "xlsm", "csv"),
    "ppt": ("ppt", "pptx"),
    "slides": ("ppt", "pptx"),
    "powerpoint": ("ppt", "pptx"),
    "mail": ("msg", "eml", "pst"),
    "email": ("msg", "eml", "pst"),
    "text": ("txt", "md", "log"),
    "code": ("py", "js", "ts", "sql", "ps1", "cs", "java"),
}

_RELATIVE_DAYS = {
    "today": 0,
    "yesterday": 1,
    "week": 7,
    "fortnight": 14,
    "month": 31,
    "quarter": 92,
    "year": 365,
}
_RELATIVE_SPAN = re.compile(r"^(\d+)\s*(d|day|days|w|week|weeks|m|month|months|y|year|years)$", re.I)
_SPAN_DAYS = {"d": 1, "day": 1, "days": 1, "w": 7, "week": 7, "weeks": 7,
              "m": 31, "month": 31, "months": 31, "y": 365, "year": 365, "years": 365}


@dataclass(frozen=True)
class ParsedQuery:
    """One user query, decomposed. `raw` is always preserved for the cache key."""

    raw: str
    text: str = ""                                   # free text, operators removed
    phrases: tuple[str, ...] = ()                    # "quoted runs"
    terms: tuple[str, ...] = ()                      # bare words, deduped, order kept
    excluded: tuple[str, ...] = ()                   # -word
    ext: tuple[str, ...] = ()                        # normalised, no leading dot
    after: Optional[date] = None
    before: Optional[date] = None
    paths: tuple[str, ...] = ()
    senders: tuple[str, ...] = ()
    unknown_operators: tuple[str, ...] = field(default_factory=tuple)

    @property
    def has_filters(self) -> bool:
        return bool(self.ext or self.after or self.before or self.paths or self.senders)

    @property
    def has_text(self) -> bool:
        """True when there is something to actually search for.

        A query of only filters (`type:pdf after:2024`) is legitimate - it means
        'list these' - but it must go down the filter-scan path, not BM25/ANN.
        """
        return bool(self.phrases or self.terms)

    @property
    def embed_text(self) -> str:
        """What to hand the embedding model. Operators are noise to a dense model."""
        return " ".join([*self.phrases, *self.terms]).strip()

    def fts_match(self, *, prefix_last: bool = False) -> str:
        """FTS5 MATCH expression. Guaranteed parseable or empty."""
        return to_fts_match(self, prefix_last=prefix_last)


def _norm_ext(value: str) -> tuple[str, ...]:
    v = value.strip().lstrip(".").lower()
    if not v:
        return ()
    if v in _EXT_GROUPS:
        return _EXT_GROUPS[v]
    # type:pdf,docx
    parts = [p.strip().lstrip(".") for p in v.split(",")]
    return tuple(p for p in parts if p.isalnum())


def _parse_date(value: str, *, today: Optional[date] = None) -> Optional[date]:
    """ISO dates, partial ISO, and plain-English relatives. Never raises."""
    today = today or date.today()
    v = value.strip().strip('"').lower()
    if not v:
        return None

    if v in _RELATIVE_DAYS:
        return today - timedelta(days=_RELATIVE_DAYS[v])
    if v.startswith("last"):                       # last-week, last month
        key = v.replace("last", "", 1).strip(" -_")
        if key in _RELATIVE_DAYS:
            return today - timedelta(days=_RELATIVE_DAYS[key])

    span = _RELATIVE_SPAN.match(v)                 # 7d, 3 months, 2y
    if span:
        return today - timedelta(days=int(span.group(1)) * _SPAN_DAYS[span.group(2).lower()])

    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%d-%m-%Y", "%d/%m/%Y", "%Y-%m", "%Y"):
        try:
            return datetime.strptime(v, fmt).date()
        except ValueError:
            continue
    return None


def parse_query(raw: str, *, today: Optional[date] = None) -> ParsedQuery:
    """Decompose a raw search string. Never raises, whatever is thrown at it."""
    if raw is None:
        raw = ""
    original = raw
    working = raw[:MAX_QUERY_CHARS]

    ext: list[str] = []
    paths: list[str] = []
    senders: list[str] = []
    unknown: list[str] = []
    after: Optional[date] = None
    before: Optional[date] = None

    def _take_operator(match: re.Match[str]) -> str:
        nonlocal after, before
        fld = _FIELD_ALIASES.get(match.group("field").lower())
        val = match.group("value").strip('"')
        if fld == "ext":
            ext.extend(_norm_ext(val))
        elif fld == "path":
            if val:
                paths.append(val)
        elif fld == "sender":
            if val:
                senders.append(val.lower())
        elif fld == "after":
            parsed = _parse_date(val, today=today)
            if parsed:
                after = parsed
            else:
                unknown.append(match.group(0))
        elif fld == "before":
            parsed = _parse_date(val, today=today)
            if parsed:
                before = parsed
            else:
                unknown.append(match.group(0))
        return " "                                  # remove from the free text

    working = _OPERATOR.sub(_take_operator, working)

    # Phrases next, so their contents are not re-split into bare terms.
    phrases: list[str] = []
    for body in _PHRASE.findall(working):
        cleaned = " ".join(body.split())
        if cleaned:
            phrases.append(cleaned)
    working = _PHRASE.sub(" ", working)

    # An odd number of quotes leaves a dangling one. Drop it rather than trip on it.
    working = working.replace('"', " ")

    terms: list[str] = []
    excluded: list[str] = []
    seen: set[str] = set()
    seen_excluded: set[str] = set()
    for chunk in working.split():
        negative = chunk.startswith("-") and len(chunk) > 1
        for token in _TERM.findall(chunk):
            token = token.rstrip("*") if token.count("*") > 1 else token
            key = token.lower()
            if not key or key.strip("*") == "":
                continue
            if negative:
                if key not in seen_excluded:
                    seen_excluded.add(key)
                    excluded.append(token)
            elif key not in seen and len(terms) < MAX_TERMS:
                seen.add(key)
                terms.append(token)

    # after:2025 before:2024 is a typo, not an intent. Swap rather than return nothing.
    if after and before and after > before:
        after, before = before, after

    text = " ".join([*phrases, *terms]).strip()

    return ParsedQuery(
        raw=original,
        text=text,
        phrases=tuple(phrases),
        terms=tuple(terms),
        excluded=tuple(excluded[:MAX_TERMS]),
        ext=tuple(dict.fromkeys(ext)),
        after=after,
        before=before,
        paths=tuple(paths),
        senders=tuple(senders),
        unknown_operators=tuple(unknown),
    )


def _fts_quote(value: str) -> str:
    """Wrap a token as an FTS5 string literal, preserving a trailing prefix star.

    Everything is quoted, so no token can ever be read as FTS5 syntax. `AND`,
    `NOT`, `*`, `(` and a lone `"` all become ordinary text.
    """
    prefix = value.endswith("*")
    body = value[:-1] if prefix else value
    body = body.replace('"', '""')
    if not body:
        return ""
    return f'"{body}"' + ("*" if prefix else "")


def to_fts_match(parsed: ParsedQuery, *, prefix_last: bool = False) -> str:
    """Build a MATCH expression that SQLite will always parse.

    Returns "" when there is nothing searchable, which the caller must treat as
    'skip BM25', not as 'search for nothing'.

    `prefix_last=True` treats the final term as a prefix, which is what
    as-you-type needs and what the interim tier uses. Without it, someone typing
    "pump st" searches for the literal word "st", which matches nothing - so the
    live results stay empty until the exact moment they finish a word, and the
    tier that exists to feel instant instead feels broken.

    Only the *last* term, and only when the caller asks: turning every term into
    a prefix would make "cat" match "catastrophe" in a committed search, which is
    not what anyone means when they press Enter.
    """
    clauses: list[str] = []
    for phrase in parsed.phrases:
        tokens = [t for t in (_fts_quote(t) for t in _TERM.findall(phrase)) if t]
        if tokens:
            clauses.append(f"({' + '.join(tokens)})")     # + is FTS5 phrase adjacency
    for index, term in enumerate(parsed.terms):
        is_last = index == len(parsed.terms) - 1
        if prefix_last and is_last and not term.endswith("*"):
            term = term + "*"
        quoted = _fts_quote(term)
        if quoted:
            clauses.append(quoted)

    if not clauses:
        return ""

    expression = " AND ".join(clauses)

    # NOT needs a left operand in FTS5, so exclusions only apply to a real query.
    negatives = [q for q in (_fts_quote(t) for t in parsed.excluded) if q]
    if negatives:
        expression = f"({expression}) NOT ({' OR '.join(negatives)})"
    return expression
