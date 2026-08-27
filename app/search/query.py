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

from app.core.identifiers import expand_term, has_case_boundary

# Re-exported below. Imported here rather than mid-file so the import block is
# the import block.
from app.search.wildcards import needs_expansion
from app.storage.filters import MAIL_KINDS

__all__ = [
    "ParsedQuery",
    "parse_query",
    "to_fts_match",
    "MAIL_KINDS",
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
    "repo": "repo", "repository": "repo", "project": "repo",
    "from": "sender", "sender": "sender",
    "to": "recipient", "recipient": "recipient", "cc": "recipient",
    "subject": "subject", "title": "subject", "re": "subject",
    "has": "has",
    "name": "name", "filename": "name", "file": "name",
    "size": "size", "bigger": "size", "smaller": "size",
    "sort": "sort", "newest": "sort", "latest": "sort", "oldest": "sort",
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
#
# **The leading `-` or `!` is captured, and losing it was a wrong answer.**
# `\b` sits between `-` and `t` in `-type:pdf`, so the operator matched, the
# minus stayed behind in the free text and was dropped as punctuation - and a
# request to *exclude* PDFs returned nothing but PDFs. The user cannot see that
# from the results; it looks like a search that simply worked.
#
# The lookbehind lives inside the optional group on purpose. It constrains only
# the negated form to a token start, so `some-type:pdf` keeps matching exactly
# as it always did rather than suddenly reading as a negation.
_OPERATOR = re.compile(
    r'(?:(?<!\S)(?P<neg>[-!]))?'
    r'\b(?P<field>' + "|".join(sorted(_FIELD_ALIASES, key=len, reverse=True)) + r')'
    r':(?P<value>"[^"]*"|\S+)',
    re.IGNORECASE,
)
#: Same treatment for a quoted run: `-"annual report"` asked for the phrase to
#: be absent and was read as requiring it.
_PHRASE = re.compile(r'(?:(?<!\S)(?P<pneg>[-!]))?"(?P<body>[^"]*)"')
#: Two or more stars together mean nothing more than one does.
_STAR_RUN = re.compile(r"\*{2,}")
# Unicode-aware word run. Keeps intra-word . _ - ' so that "v1.2", "some_file" and
# "o'brien" survive as single terms; strips emoji and punctuation, which are not
# indexed and would only ever be FTS5 syntax errors waiting to happen.
#
# **Leading and trailing underscores are part of the word.** `[^\W_]` excludes
# underscore and the continuation required a word character *after* each
# separator, so an underscore only survived between two letters:
#
#     DF_1234      -> DF_1234     correct
#     DF_          -> DF          the anchor silently gone
#     __init__.py  -> init, py    both leading underscores lost
#
# From `WORKORDER-202626081059-search-quality.md` F4. It matters more since this
# application went from 34 source types to 405: `__init__`, `_private`, `DF_`
# and `SNAKE_CASE_` prefixes are exactly what somebody types into a code search,
# and the answer they got was a search for something else.
#
# Underscore is a word character here, with a lookahead demanding at least one
# character that is not one. That keeps `__init__.py` whole - wrapping the old
# pattern in `_*` did not, because the trailing `_*` swallowed the underscores
# before `.py` could attach as a continuation, giving `__init__` and `py` as two
# terms - while still refusing `_` and `___`, which are punctuation rather than
# words and would otherwise put separator runs into the term list.
# **Wildcards are part of the term, not punctuation between terms.** `*` and `?`
# used to be dropped on the floor here, so `*voice` reached the expression as
# `"voice"` - a wildcard search that silently became an ordinary one and
# returned plausible results for the wrong question - and `inv?ice` split into
# `inv` and `ice`, two unrelated words. See `app/search/wildcards.py`.
#
# The lookahead still demands one real letter or digit, so `*`, `??` and `___`
# remain punctuation rather than becoming terms that match everything.
_TERM = re.compile(r"(?=[\w?*]*[^\W_])[\w?*]+(?:[.'\-][\w?*]+)*", re.UNICODE)


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
#:
#: **"code" means "in a repository", not "has a code extension".** The two are
#: different questions and both stay available: `type:code` answers the second
#: (see `_EXT_GROUPS`) and is untouched. This scope answers the first, which
#: nothing could ask before, and it is the one that actually separates a work
#: project from the same words in a document. A `.md` file in a repository is
#: in scope; a `.py` file in Downloads is not. The opposite reading is the
#: natural guess, which is why it is written down here.
SCOPES = ("all", "mail", "documents", "code")

#: `files.source_kind` values that count as mail.
#:
#: **Defined in `app/storage/filters.py`** and re-exported here under the name
#: everything already imports. It is a fact about what the `files` table stores
#: rather than about how a query is written, and the SQL builder that needs it
#: now sits below this layer - so defining it here would have meant Layer 1
#: importing Layer 4 to find out what its own column can contain.


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
    #: Repository names or root paths. Matched on either, because people refer
    #: to a project by its name and to a checkout by its path and which one
    #: they reach for is not predictable.
    repos: tuple[str, ...] = ()
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
    #: The negated halves of the filters above - `-type:pdf`, `-from:noreply`,
    #: `-"annual report"`. **Each one used to be read as its opposite**: `\b`
    #: matched the operator with the minus still outside it, the minus was then
    #: dropped as punctuation, and a request to exclude PDFs returned only PDFs.
    #: A wrong answer that looks exactly like a right one.
    #:
    #: `after`, `before`, `size`, `has` and `sort` have no negated form and are
    #: reported in `unknown_operators` instead - `-after:2024` is a confusing
    #: way of writing `before:`, and guessing at it would be worse than saying
    #: it was not understood.
    not_phrases: tuple[str, ...] = ()
    not_ext: tuple[str, ...] = ()
    not_paths: tuple[str, ...] = ()
    not_repos: tuple[str, ...] = ()
    not_senders: tuple[str, ...] = ()
    not_recipients: tuple[str, ...] = ()
    not_subjects: tuple[str, ...] = ()
    not_names: tuple[str, ...] = ()
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
    #: `wildcard term -> the real indexed terms it matched`.
    #:
    #: **Filled after parsing, by `wildcards.expand`, because it needs the
    #: store.** Parsing stays pure - it cannot know what words a corpus
    #: contains - so a `*voice` survives the parse as itself and is replaced by
    #: `("invoic" OR "voic")` when the expression is built. A tuple of pairs
    #: rather than a dict, so `ParsedQuery` stays frozen and hashable.
    expansions: tuple[tuple[str, tuple[str, ...]], ...] = ()

    #: True for `has:attachment`, False for `has:no-attachment`, None when the
    #: person did not say. Three states, because "did not ask" and "asked for
    #: none" are different searches and a bool cannot tell them apart.
    has_attachment: Optional[bool] = None
    #: "all" | "mail" | "documents" | "code". Not typed by the user - set by the scope
    #: chips beside the search box, and folded in here so it travels with the
    #: query through fusion, the cache key and the usage log rather than being
    #: a second argument every layer has to remember to pass on.
    scope: str = "all"
    #: `"newest"`, `"oldest"`, or `""` for relevance order.
    #:
    #: **The one finding in the search-quality work order that was a missing
    #: feature rather than a defect.** The complete filter set narrowed and
    #: nothing sorted, so *"which is the latest"* was unanswerable by any
    #: mechanism the application had - and Interpret could not rescue it
    #: either, because translation may only emit operators that exist. The word
    #: `latest` became a search term and quietly made the results worse.
    sort: str = ""

    unknown_operators: tuple[str, ...] = field(default_factory=tuple)

    @property
    def has_filters(self) -> bool:
        return bool(
            self.ext or self.after or self.before or self.paths
            or self.repos
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
        r"""What to hand the embedding model. Operators are noise to a dense model.

        **Wildcards come out.** `*voice` is not a sentence, and a dense model
        handed a star produces a vector for a star - so the stars go and the
        words stay, which is the closest thing to what the person meant. The
        keyword half is where a wildcard means something.
        """
        from app.search.wildcards import strip_wildcards

        return strip_wildcards(" ".join([*self.phrases, *self.terms])).strip()

    def fts_match(self, *, prefix_last: bool = False,
                  force_and: bool = False) -> str:
        r"""FTS5 MATCH expression. Guaranteed parseable or empty.

        `force_and` joins the terms with AND regardless of `AND_TERM_LIMIT`.
        **Not a way to change the default** - that value was measured and
        stands - but the narrow form `keyword.search` tries first, falling back
        to this expression's ordinary OR shape whenever the narrow one does not
        fill the page. See the measurements there.
        """
        return to_fts_match(self, prefix_last=prefix_last, force_and=force_and)


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


def _end_of(year: int, month: Optional[int] = None) -> date:
    """The last day of a year or a month."""
    if month is None:
        return date(year, 12, 31)
    following = date(year + (month // 12), (month % 12) + 1, 1)
    return following - timedelta(days=1)


def _parse_date(value: str, *, today: Optional[date] = None,
                end: bool = False) -> Optional[date]:
    r"""ISO dates, partial ISO, and plain-English relatives. Never raises.

    **`end` is what makes `before:2024` mean the whole year.** A partial date
    names a *period*, and which edge of it is meant depends entirely on which
    side of the range it is on: `after:2024` is the first moment of 2024 and
    `before:2024` is the last. Both used to resolve to 1 January, so
    `before:2024` excluded the entire year bar one day, and
    `after:2024 before:2024` matched only New Year's Day - a range that reads as
    "everything in 2024" and returned almost nothing.

    Full dates and relatives are unaffected: a day is already a single day, and
    `7d` already means a moment.
    """
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

    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%d-%m-%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(v, fmt).date()
        except ValueError:
            continue

    # The partial forms, which name a period rather than a day.
    for fmt, whole in (("%Y-%m", "month"), ("%Y", "year")):
        try:
            found = datetime.strptime(v, fmt).date()
        except ValueError:
            continue
        if not end:
            return found                       # the first day of the period
        return _end_of(found.year, found.month if whole == "month" else None)
    return None


def parse_query(raw: str, *, today: Optional[date] = None) -> ParsedQuery:
    """Decompose a raw search string. Never raises, whatever is thrown at it."""
    if raw is None:
        raw = ""
    original = raw
    working = raw[:MAX_QUERY_CHARS]

    ext: list[str] = []
    paths: list[str] = []
    repos: list[str] = []
    senders: list[str] = []
    recipients: list[str] = []
    subjects: list[str] = []
    names: list[str] = []
    # The negated halves. Separate lists rather than a sign on each value: every
    # consumer has to build a different SQL clause for them, and a tuple of
    # `(value, negated)` pairs would make every one of those call sites test the
    # flag - which is how one of them ends up not testing it.
    not_ext: list[str] = []
    not_paths: list[str] = []
    not_repos: list[str] = []
    not_senders: list[str] = []
    not_recipients: list[str] = []
    not_subjects: list[str] = []
    not_names: list[str] = []
    # Terms grouped by OR. One group is the ordinary case and produces exactly
    # the AND-joined expression this has always built.
    groups: list[list[str]] = []
    current_group: list[str] = []
    pending_not = False
    explicit_and = False
    sizes: list[tuple[str, int]] = []
    has_attachment: Optional[bool] = None
    unknown: list[str] = []
    sort_order = ""
    after: Optional[date] = None
    before: Optional[date] = None
    # The text each was parsed from, kept only so a reversed range can be
    # re-resolved against the correct edge of its period - see the swap below.
    raw_after = ""
    raw_before = ""

    def _take_operator(match: re.Match[str]) -> str:
        nonlocal after, before, has_attachment, sort_order
        nonlocal raw_after, raw_before
        fld = _FIELD_ALIASES.get(match.group("field").lower())
        val = match.group("value").strip('"')
        negated = bool(match.group("neg"))

        # **Negation is answered first, and only where it means something.**
        # `-after:2024` is not a filter, it is a confusing way to write
        # `before:`, and `-sort:newest` is nothing at all - so those fall
        # through to the positive handling below rather than being silently
        # accepted as an exclusion nobody could have meant.
        if negated:
            target = {
                "ext": not_ext, "path": not_paths, "repo": not_repos,
                "sender": not_senders, "recipient": not_recipients,
                "subject": not_subjects, "name": not_names,
            }.get(fld or "")
            if target is not None:
                if fld == "ext":
                    target.extend(_norm_ext(val))
                elif fld == "repo":
                    target.extend(part.strip().lower()
                                  for part in val.split(",") if part.strip())
                elif val:
                    target.append(val if fld == "path" else val.lower())
                return " "
            # A negation on a field that cannot express one is reported rather
            # than dropped: acting on half of what was typed, silently, is the
            # failure this whole file keeps being corrected for.
            unknown.append(match.group(0))
            return " "

        if fld == "ext":
            ext.extend(_norm_ext(val))
        elif fld == "path":
            if val:
                paths.append(val)
        elif fld == "repo":
            # **Comma-separated, unlike the others.** `repo:leasha,tools` is
            # how somebody asks about two checkouts at once, and it is the
            # form the `/repo` value hint advertises. Lowered here so the
            # match can be case-insensitive without `LOWER()` at query time.
            for part in val.split(","):
                part = part.strip().lower()
                if part:
                    repos.append(part)
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
        elif fld == "sort":
            # **The spelling carries the value**, so `/newest` needs no argument
            # and `/sort date` still works. `newest`, `latest` and `oldest` are
            # all aliases of the same field; which one was typed decides the
            # direction, which is why the raw operator is read rather than the
            # resolved field name.
            typed = match.group("field").lower()
            wanted = "oldest" if typed == "oldest" else (
                "oldest" if val.lower() in ("oldest", "asc", "old") else "newest")
            if typed in ("newest", "latest", "oldest") or val.lower() in (
                    "date", "newest", "latest", "oldest", "asc", "desc", "recent"):
                sort_order = wanted
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
                raw_after = val
            else:
                unknown.append(match.group(0))
        elif fld == "before":
            # `end=True`: `before:2024` means the end of 2024, not its start.
            parsed = _parse_date(val, today=today, end=True)
            if parsed:
                before = parsed
                raw_before = val
            else:
                unknown.append(match.group(0))
        return " "                                  # remove from the free text

    working = _OPERATOR.sub(_take_operator, working)

    # Phrases next, so their contents are not re-split into bare terms.
    phrases: list[str] = []
    not_phrases: list[str] = []
    for found in _PHRASE.finditer(working):
        cleaned = " ".join(found.group("body").split())
        if not cleaned:
            continue
        (not_phrases if found.group("pneg") else phrases).append(cleaned)
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
            # **Runs of stars collapse; two stars in different places do not.**
            # This used to strip every trailing `*` from any token holding more
            # than one, which turned `*a*` into `*a` and `*voice*` into
            # `*voice` - quietly changing a "contains" pattern into an "ends
            # with" one. `**` is still nonsense and still collapses.
            token = _STAR_RUN.sub("*", token)
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
    #
    # **Re-resolved from the raw text, not just exchanged.** A partial date names
    # a period, and which edge of it is meant depends on which side of the range
    # it ends up on. Swapping the two resolved days would turn
    # `after:2025 before:2024` into 31 December 2024 to 1 January 2025 - two
    # days, from a query that plainly means those two whole years. Parsing each
    # raw value again against its new side gives the outer edges.
    if after and before and after > before:
        after = _parse_date(raw_before, today=today) or before
        before = _parse_date(raw_after, today=today, end=True) or after

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
        repos=tuple(dict.fromkeys(repos)),
        senders=tuple(senders),
        recipients=tuple(recipients),
        subjects=tuple(subjects),
        names=tuple(names),
        not_phrases=tuple(not_phrases),
        not_ext=tuple(dict.fromkeys(not_ext)),
        not_paths=tuple(not_paths),
        not_repos=tuple(dict.fromkeys(not_repos)),
        not_senders=tuple(not_senders),
        not_recipients=tuple(not_recipients),
        not_subjects=tuple(not_subjects),
        not_names=tuple(not_names),
        sizes=tuple(sizes),
        or_groups=tuple(tuple(g) for g in groups if g),
        explicit_and=explicit_and,
        has_attachment=has_attachment,
        sort=sort_order,
        unknown_operators=tuple(unknown),
    )


def _fts_quote(value: str) -> str:
    r"""Wrap a token as an FTS5 string literal, preserving a trailing prefix star.

    Everything is quoted, so no token can ever be read as FTS5 syntax. `AND`,
    `NOT`, `*`, `(` and a lone `"` all become ordinary text.

    **A camelCase word also matches its split spelling.** Somebody who types
    `getUserName` means the same thing as `get_user_name`, but the two tokenize
    differently: the second is already three tokens (`unicode61` splits on the
    underscore) and therefore contains no token `getusername` at all. So the
    term becomes `("getUserName" OR ("get" AND "user" AND "name"))`.

    The document side of the same problem is handled by the `symbols` column -
    see `app/core/identifiers.py`. Both halves are needed and neither is
    sufficient: this one finds the underscored spelling, that one finds the
    camelCase spelling, and a word with no capital in the middle of it - which
    is nearly every word anybody ever searches for - takes neither path and is
    quoted exactly as before.
    """
    prefix = value.endswith("*")
    body = value[:-1] if prefix else value

    # A prefix search is left alone: `getUser*` already matches `getUserName`,
    # and expanding it would turn one cheap prefix scan into three.
    if not prefix and has_case_boundary(body):
        return expand_term(body)

    body = body.replace('"', '""')
    if not body:
        return ""
    return f'"{body}"' + ("*" if prefix else "")


def _fts_quote_plain(value: str) -> str:
    r"""One token as an FTS5 string literal, with **no expansion of any kind**.

    For inside a phrase, where `+` joins strings and a parenthesised boolean
    is a syntax error rather than a cleverer match. See the phrase builder in
    `to_fts_match` for the crash this exists to stop.
    """
    body = str(value or "").replace("*", "").replace('"', '""')
    return f'"{body}"' if body else ""


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


#: Words somebody is saying **to** the application, not looking **for**.
#:
#: From `WORKORDER-202626081059-search-quality.md` F1. *"find a project execution
#: plan"* became `"find" OR "project" OR "execution" OR "plan"`, and in an
#: archive of project documents `find` matches thousands of files and drags the
#: ranking with it. The word is the owner talking to the search box and it was
#: being matched against the corpus.
#:
#: **Deliberately a second list rather than more `_STOPWORDS`**, and the reason
#: is in the next function. Every one of these is also a real thing to search
#: for - "latest version", a file named `Search.md`, "the newest drawing" - so
#: they may only be dropped while something else remains to search for. A
#: stopword is never worth searching for alone; one of these frequently is.
#:
#: Kept in `terms` for highlighting and in `embed_text` for the vector half, for
#: the same reason stopwords are: they are noise to BM25 and harmless context to
#: an embedding.
_INSTRUCTION_WORDS = frozenset("""
anything
biggest
find
get
give
latest
looking
need
newest
recent
search
show
want
""".split())


def _content_terms(terms: Sequence[str]) -> list[str]:
    """`terms` without stopwords - unless that would leave nothing.

    A search for "the" alone, or "how to", must still search for what was typed.
    Dropping every word and returning an empty expression would turn a query
    that finds little into one that finds nothing, which is a worse answer to a
    worse question.

    **Instruction words are dropped in a second pass, and only if the first
    pass left something.** `find` is noise in *"find the pump report"* and is the
    entire query in *"find"*; the same word, and the difference is whether
    anything else survived. Doing it in one pass over a combined set would make
    a search for `latest` return everything that mentions a date.
    """
    kept = [term for term in terms if term.lower().rstrip("*") not in _STOPWORDS]
    kept = kept or list(terms)
    without_instructions = [
        term for term in kept
        if term.lower().rstrip("*") not in _INSTRUCTION_WORDS
    ]
    return without_instructions or kept


def to_fts_match(parsed: ParsedQuery, *, prefix_last: bool = False,
                 force_and: bool = False) -> str:
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
        # **Quoted plainly, because `+` joins strings and nothing else.**
        # `_fts_quote` turns a camelCase word into `("getUserName" OR ("get"
        # AND "user" AND "name"))`, which is correct in a term position and
        # not legal in an adjacency chain: FTS5 answers
        # `syntax error near "+"` and the whole search fails. Typing
        # `"getUserName handler"` therefore crashed, in released code, until
        # a pasted-error query built the same shape and hit it.
        #
        # A phrase is a request for these tokens in this order, so the
        # narrower reading is also the right one - and the document side of
        # camelCase is already covered by the `symbols` column.
        tokens = [t for t in (_fts_quote_plain(t) for t in _TERM.findall(phrase)) if t]
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
        expanded = dict(parsed.expansions)
        for term in content:
            # **An expanded wildcard becomes one alternative, not many terms.**
            # Splicing `invoic` and `voic` in as siblings would change what the
            # AND/OR joiner below means - `pump *voice` would start requiring
            # both stems rather than either. Rendered as a parenthesised OR, the
            # group occupies exactly the position the wildcard did.
            matches = expanded.get(term)
            if matches is None and needs_expansion(term):
                # **Dropped, never quietly narrowed to the literal text.** A
                # wildcard nothing expanded is a wildcard whose answer is not
                # known - on the interim tier, or with no store - and searching
                # for `voice` when `*voice` was typed is the whole defect.
                # The caller says so; see `SearchResponse.notices`.
                continue
            if matches is not None:
                quoted_matches = [q for q in (_fts_quote(m) for m in matches) if q]
                if quoted_matches:
                    quoted_terms.append("(" + " OR ".join(quoted_matches) + ")")
                # No matches means no results - never a fallback to the literal
                # text. Quietly searching for something else is the defect this
                # whole feature exists to remove.
                continue
            if prefix_last and term == last_term and not term.endswith("*"):
                term = term + "*"
            quoted = _fts_quote(term)
            if quoted:
                quoted_terms.append(quoted)

        # A description gets OR so BM25 can rank by how much matched; a short
        # keyword query keeps AND, because that is what somebody means by it.
        joiner = (
            " AND " if force_and or parsed.explicit_and
            or len(quoted_terms) <= AND_TERM_LIMIT
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
    # Bare `-word` and `-"quoted phrase"` alike. A phrase is quoted as one
    # token so FTS5 excludes the sequence rather than each word in it.
    negatives = [q for q in (_fts_quote(t) for t in parsed.excluded) if q]
    negatives += [q for q in (_fts_quote(p) for p in parsed.not_phrases) if q]
    if negatives:
        expression = f"({expression}) NOT ({' OR '.join(negatives)})"
    return expression
