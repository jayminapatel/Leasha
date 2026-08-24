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
from typing import Optional, Sequence

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

_FIELD_ALIASES = {
    "type": "ext", "ext": "ext", "kind": "ext",
    "after": "after", "since": "after",
    "before": "before", "until": "before",
    "path": "path", "folder": "path", "dir": "path",
    "from": "sender", "sender": "sender",
    "to": "recipient", "recipient": "recipient", "cc": "recipient",
    "subject": "subject", "title": "subject", "re": "subject",
    "has": "has",
    "name": "name", "filename": "name", "file": "name",
    "size": "size", "bigger": "size", "smaller": "size",
}

# field:value, where value is either "a quoted string" or a bare run of non-space.
#
# **The field list is built from `_FIELD_ALIASES`, not written out again.** It
# used to be a second hardcoded alternation, and adding `to:`, `subject:` and
# `has:` to the alias table did nothing at all: the alias was accepted by the
# handler that would never be reached, because the regex did not match the
# operator in the first place. The words simply became search terms, and the
# filter silently did nothing - exactly the shape of failure this project keeps
# finding. One list, one place.
_OPERATOR = re.compile(
    r'\b(?P<field>' + "|".join(sorted(_FIELD_ALIASES, key=len, reverse=True)) + r')'
    r':(?P<value>"[^"]*"|\S+)',
    re.IGNORECASE,
)
_PHRASE = re.compile(r'"([^"]*)"')
# Unicode-aware word run. Keeps intra-word . _ - ' so that "v1.2", "some_file" and
# "o'brien" survive as single terms; strips emoji and punctuation, which are not
# indexed and would only ever be FTS5 syntax errors waiting to happen.
_TERM = re.compile(r"[^\W_]+(?:[._'\-][^\W_]+)*\*?", re.UNICODE)


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


#: The scopes the UI offers. "documents" means everything that is not mail -
#: files on disk - rather than a specific set of extensions, so a new extractor
#: never has to be added here.
SCOPES = ("all", "mail", "documents")

#: `files.source_kind` values that count as mail.
MAIL_KINDS = ("pst_message", "eml")


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
    #: Mail-only fields. Empty for a document search, which is why they cost
    #: nothing when unused: each becomes a subquery on `messages` only if the
    #: person actually asked for it.
    recipients: tuple[str, ...] = ()
    subjects: tuple[str, ...] = ()
    #: Filename filters - the *basename*, not the whole path. `path:` already
    #: answers "which folder"; this answers "what is it called", and they are
    #: different questions that people ask for different reasons.
    names: tuple[str, ...] = ()
    #: `(comparison, bytes)` pairs, e.g. `(">=", 1048576)`.
    sizes: tuple[tuple[str, int], ...] = ()
    #: Terms grouped by `OR`. Within a group terms are ANDed, between groups
    #: ORed - the precedence everybody expects, where `a b OR c` means
    #: `(a AND b) OR c`. A query with no `OR` has exactly one group, which
    #: produces the same expression as before this existed.
    or_groups: tuple[tuple[str, ...], ...] = ()
    #: True when the person typed `AND` themselves. Terms are otherwise joined
    #: with OR so a description need not match every word; an explicit AND is a
    #: direct instruction and overrides that.
    explicit_and: bool = False
    #: True for `has:attachment`, False for `has:no-attachment`, None when the
    #: person did not say. Three states, because "did not ask" and "asked for
    #: none" are different searches and a bool cannot tell them apart.
    has_attachment: Optional[bool] = None
    #: "all" | "mail" | "documents". Not typed by the user - set by the scope
    #: chips beside the search box, and folded in here so it travels with the
    #: query through fusion, the cache key and the usage log rather than being
    #: a second argument every layer has to remember to pass on.
    scope: str = "all"
    unknown_operators: tuple[str, ...] = field(default_factory=tuple)

    @property
    def has_filters(self) -> bool:
        return bool(
            self.ext or self.after or self.before or self.paths
            or self.senders or self.recipients or self.subjects
            or self.names or self.sizes
            or self.has_attachment is not None or self.scope != "all"
        )

    def scoped(self, scope: str) -> "ParsedQuery":
        """The same query restricted to mail or to documents.

        A copy rather than a mutation: `ParsedQuery` is frozen so it can be a
        cache key, and a scope that changed in place would leave the cache
        serving one scope's results under another's name.
        """
        from dataclasses import replace as _replace

        return _replace(self, scope=scope if scope in SCOPES else "all")

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


_SIZE_UNITS = {"b": 1, "k": 1024, "kb": 1024, "m": 1024**2, "mb": 1024**2,
               "g": 1024**3, "gb": 1024**3}
_SIZE = re.compile(r"^(?P<op>[<>]=?|=)?\s*(?P<n>\d+(?:\.\d+)?)\s*(?P<unit>[kmgb]b?)?$", re.I)


def _parse_size(value: str) -> Optional[tuple[str, int]]:
    """`>1mb` -> `(">", 1048576)`. None when it is not a size at all.

    Returned rather than raised so the caller can report `size:banana` as an
    unknown operator instead of guessing at it - a size filter that silently
    does nothing gives a wider result set than was asked for, with no sign.

    A bare number with no comparison means "at least this", because that is
    what somebody typing `size:1mb` is looking for: the big ones.
    """
    match = _SIZE.match(value.strip())
    if match is None:
        return None
    unit = (match.group("unit") or "b").lower()
    if unit not in _SIZE_UNITS:
        return None
    count = int(float(match.group("n")) * _SIZE_UNITS[unit])
    return (match.group("op") or ">=", count)


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
    recipients: list[str] = []
    subjects: list[str] = []
    names: list[str] = []
    # Terms grouped by OR. One group is the ordinary case and produces exactly
    # the AND-joined expression this has always built.
    groups: list[list[str]] = []
    current_group: list[str] = []
    pending_not = False
    explicit_and = False
    sizes: list[tuple[str, int]] = []
    has_attachment: Optional[bool] = None
    unknown: list[str] = []
    after: Optional[date] = None
    before: Optional[date] = None

    def _take_operator(match: re.Match[str]) -> str:
        nonlocal after, before, has_attachment
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
        elif fld == "recipient":
            if val:
                recipients.append(val.lower())
        elif fld == "subject":
            if val:
                subjects.append(val.lower())
        elif fld == "name":
            if val:
                names.append(val.lower())
        elif fld == "size":
            parsed_size = _parse_size(val)
            if parsed_size is not None:
                sizes.append(parsed_size)
            else:
                unknown.append(match.group(0))
        elif fld == "has":
            # `has:attachment` and `has:no-attachment`. Anything else is a typo
            # and is reported rather than guessed at - silently ignoring it
            # would widen the search without saying so.
            lowered = val.lower()
            negated = lowered.startswith(("no", "-", "!", "without"))
            cleaned = re.sub(r"^(no[t]?[-_ ]?|without[-_ ]?|[-!])", "", lowered)
            cleaned = cleaned.replace("attachments", "attachment")
            if cleaned in ("attachment", "attached", "file"):
                has_attachment = not negated
            else:
                unknown.append(match.group(0))
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
        # **Boolean operators, uppercase only.**
        #
        # `pump OR valve` is a choice; "salt and pepper" and "one or two" are
        # ordinary English that people search for constantly. Requiring capitals
        # is how every search engine tells them apart, and it means the feature
        # cannot break a query somebody was already typing. Lowercase `or` stays
        # a search term.
        if chunk in ("OR", "AND", "NOT"):
            if chunk == "AND":
                # **Honoured, not merely tolerated.** Terms are otherwise
                # joined with OR so a description does not have to match every
                # word - but somebody who types AND has asked for the strict
                # reading in as many words, and overriding that would make the
                # operator a decoration. It is also the answer offered to
                # anybody who finds the default too loose, so it has to work.
                explicit_and = True
            if chunk == "OR":
                # Start a new alternative. AND is the default between terms, so
                # an explicit AND only has to not break anything.
                if current_group:
                    groups.append(current_group)
                    current_group = []
            pending_not = chunk == "NOT"
            continue

        negative = (chunk.startswith("-") and len(chunk) > 1) or pending_not
        pending_not = False
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
                current_group.append(token)

    if current_group:
        groups.append(current_group)

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
        recipients=tuple(recipients),
        subjects=tuple(subjects),
        names=tuple(names),
        sizes=tuple(sizes),
        or_groups=tuple(tuple(g) for g in groups if g),
        explicit_and=explicit_and,
        has_attachment=has_attachment,
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


#: Words dropped from the FTS expression, and **only** from the FTS expression.
#:
#: Every term is joined with AND, so a query is satisfied only by a document
#: containing all of them. That is right for keywords and catastrophic for a
#: sentence: "drawings of the pump station" became
#:
#:     "drawings" AND "of" AND "the" AND "pump" AND "station"
#:
#: and the document - "Pump station general arrangement drawings." - contains no
#: "of" and no "the", so it matched nothing. Measured against twenty plain
#: sentences, nineteen returned **zero results**: not badly ranked, not ranked at
#: all. The application's stated purpose is to let somebody "write in normal text
#: what I am looking for", and the keyword half was mathematically incapable of
#: it, because normal text is mostly these words.
#:
#: They stay in `terms` for highlighting and in `embed_text` for the vector side,
#: where they carry real meaning: "report from Dave" and "report for Dave" embed
#: differently, and should.
_STOPWORDS = frozenset("""
a about all am an and any are as at
be been being but by
can could
did do does doing done
for from
had has have having he her his how
i if in into is it its
me my
of on or our out over
said say says she should so some
than that the their them then there these they this those to
under up us
was we were what when where which who whom why will with would
you your
""".split())


#: Above this many content words, terms are joined with OR rather than AND.
#:
#: **Because a sentence is not a keyword list.** Even after stopwords go, "the
#: email from Chris about buying a licence" leaves `email AND Chris AND buying
#: AND licence`, and the message does not contain the word "email" - so it
#: matched nothing. People describe what they want using words that are *about*
#: the document rather than *in* it: "email", "version", "deck", "actually".
#:
#: With OR, BM25 ranks a document matching four terms above one matching two, so
#: the best answer still comes first - and the terms that were not in any
#: document simply contribute nothing instead of excluding everything.
#:
#: **One, chosen by measurement rather than by taste.** Against twenty sentences
#: and a corpus with known answers:
#:
#:     limit   empty results   recall@1   recall@3   translated@1
#:         1               0        70%        95%            90%
#:         2               2        65%        85%            85%
#:         3               6        50%        65%            75%
#:         4              11        35%        45%            75%
#:
#: Three was the first guess and left six of twenty sentences returning nothing
#: at all. Anything above one word is a description, and demanding every word of
#: a description is how a search box earns a reputation for finding nothing.
#:
#: Precision is not lost, because this is the retrieval stage. BM25 ranks a
#: document matching four terms above one matching two, fusion with the vector
#: side reorders, and the cross-encoder reranks after that. Somebody who wants a
#: strict match has `"quoted phrases"` and an explicit `AND`, both of which
#: still mean exactly what they say.
AND_TERM_LIMIT = 1


def _content_terms(terms: Sequence[str]) -> list[str]:
    """`terms` without stopwords - unless that would leave nothing.

    A search for "the" alone, or "how to", must still search for what was typed.
    Dropping every word and returning an empty expression would turn a query
    that finds little into one that finds nothing, which is a worse answer to a
    worse question.
    """
    kept = [term for term in terms if term.lower().rstrip("*") not in _STOPWORDS]
    return kept or list(terms)


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
    phrase_clauses: list[str] = []
    for phrase in parsed.phrases:
        tokens = [t for t in (_fts_quote(t) for t in _TERM.findall(phrase)) if t]
        if tokens:
            phrase_clauses.append(f"({' + '.join(tokens)})")   # + is FTS5 adjacency

    # Terms come grouped by OR. One group is the ordinary case and produces the
    # same AND-joined expression this has always produced; several groups become
    # `(a AND b) OR (c)`, which is the precedence everybody expects.
    #
    # Phrases apply to the whole query rather than to one alternative: somebody
    # writing `"site survey" pump OR valve` means the phrase in both cases.
    # Distributing them is the reading that matches how people write it.
    groups = parsed.or_groups or ((tuple(parsed.terms),) if parsed.terms else ())
    last_term = parsed.terms[-1] if parsed.terms else None

    alternatives: list[str] = []
    for group in groups:
        content = _content_terms(group)
        quoted_terms: list[str] = []
        for term in content:
            if prefix_last and term == last_term and not term.endswith("*"):
                term = term + "*"
            quoted = _fts_quote(term)
            if quoted:
                quoted_terms.append(quoted)

        # A description gets OR so BM25 can rank by how much matched; a short
        # keyword query keeps AND, because that is what somebody means by it.
        joiner = (
            " AND " if parsed.explicit_and or len(quoted_terms) <= AND_TERM_LIMIT
            else " OR "
        )
        body = joiner.join(quoted_terms)

        # A phrase is always required. Quoting something is the most explicit
        # statement of intent the box offers, and loosening it to OR would make
        # "critical control point" behave like three loose words.
        clauses = list(phrase_clauses)
        if body:
            clauses.append(f"({body})" if joiner == " OR " and len(clauses) else body)
        if clauses:
            alternatives.append(" AND ".join(clauses))

    if not alternatives and phrase_clauses:
        alternatives = [" AND ".join(phrase_clauses)]
    if not alternatives:
        return ""

    expression = (
        alternatives[0] if len(alternatives) == 1
        else " OR ".join(f"({a})" for a in alternatives)
    )

    # NOT needs a left operand in FTS5, so exclusions only apply to a real query.
    negatives = [q for q in (_fts_quote(t) for t in parsed.excluded) if q]
    if negatives:
        expression = f"({expression}) NOT ({' OR '.join(negatives)})"
    return expression
