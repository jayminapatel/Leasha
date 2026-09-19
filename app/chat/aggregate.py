"""Counting, listing and totalling: questions the index answers, not the model.

Layer: L8b - no Qt, no network, no model. Work order section 1d.

    "how many PDFs did Dave send in 2019"

is a database question. It runs as a real query and **the number in the answer is
the query's result, injected into a template** - there is no step at which a
model is handed a number, and so none at which it can change one. This is the
whole reason chat can promise "aggregate exactness: 100%" (section 4b): the
figure is not the output of a language model that was very careful, it is the
output of `COUNT(*)`.

The filters are the ones search already has. `parse_aggregate` reads a question
into a `ParsedQuery`-compatible set of filters (senders and dates through
`app.search.translate_rules`, file types through a strict table of its own) and
`app.storage.filters.file_filter_sql` turns them into the same SQL the Files tab
runs - one definition of what `from:` and `after:` mean, not a second one.

**File types are strict here, and looser in search.** `translate_rules` maps
"invoice" to `pdf/docx/xlsx` because for a *search* a wrong guess costs a glance.
A count that guesses is simply a wrong number, so an aggregate names a type only
when the person did ("PDFs", "spreadsheets", "emails"); "invoices" is a word to
find in the text, not an extension to assume.

**Every answer states its own edges** (4e-4): the number, and in the same breath
what it was counted over.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field, replace
from datetime import date
from typing import Any, Optional

from app.chat.text import STOPWORDS
from app.core.logging import logger
from app.search.query import ParsedQuery, parse_query
from app.storage.filters import MAIL_KINDS, file_filter_sql
from app.storage.like import ESCAPE, contains

__all__ = [
    "AggregateSpec",
    "AggregateResult",
    "parse_aggregate",
    "run_aggregate",
    "COUNTED_STATUSES",
    "SCOPE_SENTENCE",
]

log = logger.bind(component="chat.aggregate")

#: Which files count as "in the index". Everything findable: read, partly read
#: (keyword-searchable while embedding catches up) and name-only. Skipped and
#: failed files are not in the index in any sense a person would mean.
COUNTED_STATUSES = ("INDEXED", "PARTIAL", "NAME_ONLY")

#: The clause that ends every aggregate answer (4e-4). "Including offline
#: drives" is true because the count is by index status, not by what is mounted.
SCOPE_SENTENCE = "counted across everything Leasha has indexed, offline drives included"

#: Seconds a count may take before it is abandoned. At twenty million files a
#: badly chosen filter is a long scan; the tab's worker must not be held for it.
QUERY_DEADLINE_S = 30.0

#: SQLite virtual-machine steps between two looks at the clock. Small enough to
#: notice a deadline within a fraction of a second, large enough to cost nothing.
PROGRESS_STEPS = 200_000

#: (label, singular label, extensions, mail, has_attachment, generic)
_TYPES: dict[str, tuple[str, str, tuple[str, ...], bool, bool, bool]] = {}


def _type(words: str, label: str, single: str, exts: tuple[str, ...] = (), *,
          mail: bool = False, attach: bool = False, generic: bool = False) -> None:
    for word in words.split():
        _TYPES[word] = (label, single, exts, mail, attach, generic)


_type("pdf pdfs", "PDFs", "PDF", ("pdf",))
_type("spreadsheet spreadsheets workbook workbooks excel", "spreadsheets", "spreadsheet",
      ("xlsx", "xls", "csv", "ods"))
_type("presentation presentations slide slides deck decks powerpoint", "presentations",
      "presentation", ("pptx", "ppt", "odp"))
_type("photo photos picture pictures pic pics jpeg jpegs", "photos", "photo",
      ("jpg", "jpeg", "png", "heic"))
_type("image images", "images", "image",
      ("jpg", "jpeg", "png", "heic", "gif", "webp", "tif", "tiff"))
_type("email emails mail mails message messages", "emails", "email", mail=True)
_type("attachment attachments", "emails with attachments", "email with an attachment",
      mail=True, attach=True)
_type("docx doc docs word", "Word documents", "Word document", ("docx", "doc", "odt"))
_type("txt", "text files", "text file", ("txt",))
_type("document documents", "documents", "document", generic=True)
_type("file files", "files", "file", generic=True)

#: Topical nouns: a thing to find in the text, never an extension to assume.
_TOPIC_NOUNS = frozenset("""
invoice invoices letter letters report reports contract contracts receipt receipts
drawing drawings note notes scan scans
""".split())

#: Words that shape the question but say nothing about what to count.
_FILLER = frozenset("""
how many much number total count list all every give me show tell do does did
have has had are is was were there my our your in on of at by for with from to
archive index folder folders computer pc drive drives stored store saved got
size big large space storage combined please a an the and or any some each
send sent sends email emails mail mails message messages received receive
latest newest oldest earliest recent when last first most attached attachment
indexed indexes overall altogether exist exists existing take takes taken
""".split())

_MONTHS = ("january", "february", "march", "april", "may", "june", "july",
           "august", "september", "october", "november", "december")


@dataclass(frozen=True)
class AggregateSpec:
    """A counting question, read."""

    op: str                                   # count | list | size | newest | oldest
    #: What is being counted, plural, for the answer sentence. "PDFs".
    label: str
    singular: str
    parsed: ParsedQuery
    #: Words that must each appear in the text or the path (all of them).
    terms: tuple[str, ...] = ()
    #: The filters, spelt as the search grammar, for the narration and debug pane.
    filters: str = ""
    #: The scope in plain words: "from dave.smith@acme.com, in 2019".
    scope_words: str = ""

    def summary(self) -> str:
        """One line of what will be counted, for narration."""
        about = f" mentioning {', '.join(self.terms)}" if self.terms else ""
        where = f" {self.scope_words}" if self.scope_words else ""
        return f"{self.label}{about}{where}"


@dataclass(frozen=True)
class AggregateResult:
    """The answer, computed. `count` is the query's own result."""

    spec: AggregateSpec
    count: int
    text: str
    total_bytes: int = 0
    #: For list/newest/oldest: `[(file_id, path, mtime_ns, taken_at_ns), ...]`.
    rows: tuple[tuple[int, str, int, int], ...] = ()
    notes: tuple[str, ...] = field(default_factory=tuple)
    sql: str = ""
    elapsed_s: float = 0.0


# --------------------------------------------------------------------------- reading

def _date_phrase(after: Optional[date], before: Optional[date]) -> str:
    if after and before:
        if after.month == 1 and after.day == 1 and before.month == 12 and before.day == 31 \
                and after.year == before.year:
            return f"in {after.year}"
        if after.year == before.year and after.month == before.month and after.day == 1 \
                and before.day >= 28:
            return f"in {_MONTHS[after.month - 1].capitalize()} {after.year}"
        return f"between {after.isoformat()} and {before.isoformat()}"
    if after:
        return f"after {after.isoformat()}"
    if before:
        return f"before {before.isoformat()}"
    return ""


def _scope_words(parsed: ParsedQuery) -> str:
    parts: list[str] = []
    if parsed.senders:
        parts.append("from " + " or ".join(parsed.senders))
    if parsed.recipients:
        parts.append("sent to " + " or ".join(parsed.recipients))
    when = _date_phrase(parsed.after, parsed.before)
    if when:
        parts.append(when)
    if parsed.has_attachment:
        parts.append("with an attachment")
    return ", ".join(parts)


def parse_aggregate(question: str, store: Any = None, *,
                    today: Optional[date] = None) -> AggregateSpec:
    """Read a counting question into filters. **Never raises.**

    People and dates come from the same rules the search translator uses
    (`translate_rules.read`); the file type from the strict table above.
    """
    text = str(question or "").strip()
    lowered = text.lower()

    # -- what to do ---------------------------------------------------------
    if re.search(r"\bhow\s+(?:much|big|large)\b.*\b(?:space|storage|disk|size)\b|\b(?:total|combined)\s+size\b",
                 lowered):
        op = "size"
    elif re.search(r"\b(?:latest|newest|most\s+recent)\b", lowered) and "how many" not in lowered:
        op = "newest"
    elif re.search(r"\b(?:oldest|earliest)\b", lowered) and "how many" not in lowered:
        op = "oldest"
    elif re.match(r"^\s*(?:please\s+)?(?:list|enumerate|give\s+me\s+a\s+list)", lowered):
        op = "list"
    else:
        op = "count"

    # -- what kind of thing -------------------------------------------------
    words = re.findall(r"[a-z0-9']+", lowered)
    chosen = None
    generic = None
    for word in words:
        entry = _TYPES.get(word)
        if entry is None:
            continue
        if entry[5]:
            generic = generic or entry
        else:
            chosen = entry
            break
    entry = chosen or generic
    label, singular, exts, mail, attach, _ = entry or ("files", "file", (), False, False, True)
    scope = "all"
    if entry is generic and generic is not None and "document" in label:
        scope = "documents"

    # -- who and when, by the search translator's own rules --------------------
    filters: list[str] = []
    claimed: set[str] = set()
    try:
        from app.search.translate_rules import read as read_rules

        reading = read_rules(text, store, today=today)
        for chip in reading.chips:
            if chip.field == "type":
                continue                       # types are strict here - see the docstring
            filters.append(chip.as_filter())
            claimed.update(re.findall(r"[a-z0-9']+", chip.source.lower()))
        if reading.mail and not (exts or mail):
            mail = True
            label, singular = "emails", "email"
    except Exception as exc:                            # noqa: BLE001 - never raises
        log.debug("aggregate: rules skipped: {}", exc)

    if attach:
        filters.append("has:attachment")
    parsed = parse_query(" ".join(filters))
    if exts:
        parsed = replace(parsed, ext=tuple(exts))
    if mail:
        parsed = parsed.scoped("mail")
    elif scope == "documents":
        parsed = parsed.scoped("documents")

    # -- what is left is a topic -------------------------------------------------
    terms: list[str] = []
    for word in words:
        if word in claimed or word in _FILLER or word in STOPWORDS or word in _TYPES:
            continue
        if word.isdigit() and len(word) == 4:
            continue                           # a year was read as a date
        if word in _MONTHS or word in ("year",):
            continue
        if word in ("year", "years", "month", "months", "week", "weeks", "ago", "last", "this"):
            continue
        base = word[:-1] if word in _TOPIC_NOUNS and word.endswith("s") else word
        if base not in terms and len(base) > 2:
            terms.append(base)
    terms = terms[:4]

    return AggregateSpec(
        op=op, label=label, singular=singular, parsed=parsed,
        terms=tuple(terms), filters=" ".join(filters),
        scope_words=_scope_words(parsed),
    )


# --------------------------------------------------------------------------- SQL

def _where(spec: AggregateSpec) -> tuple[str, list[Any]]:
    marks = ", ".join("?" for _ in COUNTED_STATUSES)
    where = f" AND f.status IN ({marks})"
    params: list[Any] = list(COUNTED_STATUSES)

    fragment, filter_params = file_filter_sql(spec.parsed)
    where += fragment
    params += filter_params

    for term in spec.terms:
        # A topic word is in the text (stemmed, by FTS) or in the file's own
        # path - a folder of Diwali photos has no text to search.
        expression = parse_query(term).fts_match()
        clauses = ["f.path LIKE ?" + ESCAPE]
        params.append(contains(term))
        if expression:
            clauses.append(
                "f.id IN (SELECT c.file_id FROM chunks_fts "
                "JOIN chunks c ON c.id = chunks_fts.rowid WHERE chunks_fts MATCH ?)")
            params.append(expression)
        where += " AND (" + " OR ".join(clauses) + ")"
    return where, params


def _plural(n: int, spec: AggregateSpec) -> str:
    return spec.singular if n == 1 else spec.label


def _describe(spec: AggregateSpec, n: int) -> str:
    about = f" mentioning {', '.join(spec.terms)}" if spec.terms else ""
    where = f" {spec.scope_words}" if spec.scope_words else ""
    return f"{_plural(n, spec)}{about}{where}"


def _size_words(n: int) -> str:
    size = float(n)
    for unit in ("bytes", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{int(size)} bytes" if unit == "bytes" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{n} bytes"


def run_aggregate(store: Any, spec: AggregateSpec, *, limit: int = 50) -> AggregateResult:
    """Run the query and phrase its result. **The number is `COUNT(*)`.**

    Raises nothing but `TimeoutError` (the deadline) and whatever the store
    raises; `ChatEngine` turns both into plain-words errors.
    """
    where, params = _where(spec)
    conn = store.conn
    started = time.monotonic()

    def guard() -> int:
        return 1 if time.monotonic() - started > QUERY_DEADLINE_S else 0

    conn.set_progress_handler(guard, PROGRESS_STEPS)
    try:
        try:
            count = int(conn.execute(
                f"SELECT COUNT(*) FROM files f WHERE 1=1{where}", params).fetchone()[0])
            total_bytes = 0
            rows: tuple = ()
            if spec.op == "size":
                total_bytes = int(conn.execute(
                    f"SELECT COALESCE(SUM(f.size_bytes), 0) FROM files f WHERE 1=1{where}",
                    params).fetchone()[0])
            elif spec.op in ("list", "newest", "oldest") and count:
                order = "DESC" if spec.op != "oldest" else "ASC"
                take = 1 if spec.op in ("newest", "oldest") else max(1, int(limit))
                fetched = conn.execute(
                    f"SELECT f.id, f.path, f.mtime_ns, COALESCE(f.taken_at_ns, 0) "
                    f"FROM files f WHERE 1=1{where} "
                    f"ORDER BY COALESCE(f.taken_at_ns, f.mtime_ns) {order}, f.id LIMIT ?",
                    [*params, take]).fetchall()
                rows = tuple((int(r[0]), str(r[1]), int(r[2]), int(r[3])) for r in fetched)
        except Exception as exc:
            if "interrupted" in str(exc).lower():
                raise TimeoutError(
                    f"counting took longer than {QUERY_DEADLINE_S:.0f} seconds") from exc
            raise
    finally:
        conn.set_progress_handler(None, 0)

    text, notes = _phrase(spec, count, total_bytes, rows, limit)
    return AggregateResult(spec=spec, count=count, text=text, total_bytes=total_bytes,
                           rows=rows, notes=notes, sql=f"files WHERE 1=1{where}",
                           elapsed_s=time.monotonic() - started)


def _phrase(spec: AggregateSpec, count: int, total_bytes: int, rows: tuple,
            limit: int) -> tuple[str, tuple[str, ...]]:
    """Fill the template. The number is inserted, never written by a model."""
    notes: list[str] = []
    edges = SCOPE_SENTENCE
    if spec.op == "size":
        text = (f"{_size_words(total_bytes)} in {count:,} {_describe(spec, count)} - "
                f"{edges}.")
    elif spec.op in ("newest", "oldest"):
        which = "newest" if spec.op == "newest" else "oldest"
        if not count:
            text = f"There are 0 {spec.label} matching that - {edges}."
        else:
            _fid, path, mtime_ns, taken_ns = rows[0]
            when = time.strftime("%d %B %Y", time.localtime((taken_ns or mtime_ns) / 1e9))
            name = path.replace("\\", "/").rsplit("/", 1)[-1]
            text = (f"The {which} of the {count:,} {_describe(spec, count)} is {name}, "
                    f"dated {when} - {edges}.")
    elif spec.op == "list":
        shown = min(len(rows), limit)
        if count > shown:
            text = (f"{count:,} {_describe(spec, count)} - {edges}. "
                    f"Here are the newest {shown:,}.")
        else:
            text = f"{count:,} {_describe(spec, count)} - {edges}. That's all {count:,}."
    else:
        text = f"{count:,} {_describe(spec, count)} - {edges}."
    if count == 0:
        notes.append("Zero is what the index holds. A folder or drive that has not been "
                     "scanned yet is not counted.")
    return text, tuple(notes)
