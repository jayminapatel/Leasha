"""From a question to searches, and whether the searches found enough.

Layer: L8b - no Qt; the rules half needs no network. Work order section 1b.

**This reuses the translator's design, not a copy of it.** `app/search/
translate_rules.read` - the rules half of query translation, which resolves real
sender names and dates against the index - does the first reading. Round one is
therefore instant and needs no model: it is the sentence's meaningful words plus
whatever filters the rules are sure of. Only when that round comes back thin does
a model get asked (`planner_queries`, the model half, in the translator's own
spirit: its answer is a proposal, checked before use).

**Assessing sufficiency** (`assess`) is what keeps the loop honest in both
directions. A question that retrieval answers well should not burn three rounds;
a question the index cannot answer should not be answered with the nearest
irrelevant passage. The test is coverage: does any single retrieved passage
contain at least half of the question's distinctive words - and, when the index
says a word appears *nowhere*, that is stated evidence of absence, not a weak
match to be papered over.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable, Optional, Sequence

from app.chat.aggregate import _TYPES, _scope_words
from app.chat.text import STOPWORDS, content_tokens, stem
from app.core.logging import logger
from app.search.query import parse_query

__all__ = [
    "Plan",
    "Assessment",
    "make_plan",
    "assess",
    "widen",
    "planner_queries",
    "FILLER",
]

log = logger.bind(component="chat.plan")

#: Words that shape a question without saying what to look for.
FILLER = frozenset(STOPWORDS | set("""
tell show find get give look search please anything something documents document files file
thing things stuff info information mention mentions mentioned say says said saying
know remember recall need want like really actually ever
summarise summarize summary overview everything compare comparison differences regarding
explain describe much pull bring fetch open display
""".split()))

#: Words that name a kind of file outright. "documents" and "files" do not: they
#: name everything, and a `type:` filter made from them would exclude most of it.
_EXPLICIT_TYPE_WORDS = frozenset(word for word, entry in _TYPES.items() if not entry[5])

_YEAR = re.compile(r"^(?:19|20)\d{2}$")
_MONTHS = frozenset("""january february march april may june july august september october
november december""".split())


@dataclass
class Plan:
    """What to search for, read from a question."""

    question: str
    #: Filter operators the rules are sure of, spelt as the search grammar.
    filters: list[str] = field(default_factory=list)
    #: The words to look for, as written.
    terms: list[str] = field(default_factory=list)
    #: Queries run so far, in order - shown to the person if nothing is found.
    tried: list[str] = field(default_factory=list)

    def query(self, *, filters: bool = True, terms: Optional[Sequence[str]] = None) -> str:
        parts = list(self.filters) if filters else []
        parts += list(self.terms if terms is None else terms)
        return " ".join(parts).strip()

    def stems(self) -> list[str]:
        return list(dict.fromkeys(content_tokens(" ".join(self.terms))))

    def summary(self) -> str:
        """The plan in plain words, for a narration line."""
        what = " ".join(self.terms)
        try:
            where = _scope_words(parse_query(" ".join(self.filters))) if self.filters else ""
        except Exception:                               # noqa: BLE001
            where = ""
        if not what:
            # Nothing but filters: "emails from priya.n@acme.com", not the question.
            mail = any(f in ("type:mail", "type:eml") or f.startswith(("from:", "to:"))
                       for f in self.filters)
            what = ("emails" if mail else "files") if where or self.filters else \
                self.question.strip().rstrip("?")
        return f"{what} {where}".strip()[:90]


def make_plan(question: str, store: Any = None, *, today: Optional[date] = None) -> Plan:
    """Read a question into filters and terms. **Never raises**, and needs no model."""
    text = str(question or "").strip()
    plan = Plan(question=text)
    claimed: set[str] = set()
    try:
        from app.search.translate_rules import MAIL_VERBS, read

        reading = read(text, store, today=today)
        explicit_type = False
        has_year = bool(re.search(r"\b(?:19|20)\d{2}\b", text)) or "last year" in text.lower() \
            or "this year" in text.lower()
        for chip in reading.chips:
            if chip.field in ("after", "before") and not has_year:
                # "the March audit": which March? The rules assume this year's,
                # and a wrong guess hides the right document. The month stays a
                # search word; ranking does the rest.
                continue
            if chip.field == "type":
                # A file type is kept only when the person actually named one;
                # "the invoice" does not mean "a PDF".
                if not (set(re.findall(r"[a-z]+", chip.source.lower())) & _EXPLICIT_TYPE_WORDS):
                    continue
                explicit_type = True
            plan.filters.append(chip.as_filter())
            claimed.update(re.findall(r"[a-z0-9']+", chip.source.lower()))
        if reading.mail and not explicit_type:
            plan.filters.append("type:mail")
        claimed |= set(MAIL_VERBS)
    except Exception as exc:                            # noqa: BLE001 - the rules are a helper
        log.debug("plan: rules skipped: {}", exc)

    seen: set[str] = set()
    for word in re.findall(r"[A-Za-z0-9][A-Za-z0-9'’\-]*", text):
        low = word.lower().replace("’", "'")
        if low in FILLER or low in claimed or _YEAR.match(low):
            continue
        base = stem(low)
        if base in seen or len(low) < 2:
            continue
        seen.add(base)
        plan.terms.append(word.strip("'-"))
    plan.terms = plan.terms[:8]
    return plan


@dataclass(frozen=True)
class Assessment:
    """Did the retrieval find enough to answer from?"""

    files: int
    coverage: float
    unmatched: tuple[str, ...]
    thin: bool
    reason: str


def assess(results: Sequence[Any], plan: Plan, unmatched: Sequence[str] = ()) -> Assessment:
    """Is this enough? See the module docstring for the rule.

    `unmatched` are the query words the index holds nowhere (the engine already
    computes them: `SearchResponse.unmatched`).
    """
    wanted = set(plan.stems())
    files = len({int(getattr(r, "file_id", 0) or 0) for r in results})
    if not results:
        return Assessment(0, 0.0, tuple(unmatched), True, "nothing matched")
    if not wanted:
        return Assessment(files, 1.0, tuple(unmatched), False, "no distinctive words to check")

    best = 0.0
    for result in results:
        where = str(getattr(result, "path", "")).replace("\\", "/").replace("-", " ").replace("_", " ")
        have = set(content_tokens(f"{getattr(result, 'text', '')} {where}"))
        best = max(best, len(have & wanted) / len(wanted))

    # A word the index holds in no *text* can still be in a folder or file name
    # ("Recipes\dal-tadka.txt"), which the search matches as well.
    in_paths: set[str] = set()
    for result in results:
        in_paths.update(content_tokens(
            str(getattr(result, "path", "")).replace("\\", "/").replace("-", " ").replace("_", " ")))
    missing = [w for w in unmatched if stem(w.lower()) in wanted and stem(w.lower()) not in in_paths]
    if best < 0.5:
        return Assessment(files, best, tuple(unmatched), True,
                          f"no passage contains half of the question's words ({best:.0%} at best)")
    if missing and len(missing) / len(wanted) >= 0.5:
        return Assessment(files, best, tuple(unmatched), True,
                          f"the index has no document containing: {', '.join(missing)}")
    return Assessment(files, best, tuple(unmatched), False, "enough")


def widen(plan: Plan) -> list[str]:
    """Rule-based next searches after a thin round, best first, none repeated."""
    candidates: list[str] = []
    if plan.filters and plan.terms:
        candidates.append(plan.query(filters=False))
    if len(plan.terms) > 3:
        distinctive = sorted(plan.terms, key=len, reverse=True)[:3]
        candidates.append(plan.query(filters=False, terms=distinctive))
    fresh: list[str] = []
    for candidate in candidates:
        if candidate and candidate not in plan.tried and candidate not in fresh:
            fresh.append(candidate)
    return fresh


def planner_queries(llm: Any, plan: Plan, *, timeout: float = 30.0,
                    should_stop: Optional[Callable[[], bool]] = None) -> list[str]:
    """Ask the planner model for other searches. **A proposal, never a command**:
    the reply must be JSON with a `queries` list of plain strings, each is cleaned
    and bounded, and anything else is discarded. Never raises. `should_stop`
    (2026-10-04, code review) is the question's Stop."""
    from app.chat.llm import stop_kwargs
    from app.chat.prompts import planner_prompt

    if llm is None:
        return []
    try:
        reply = llm.generate(planner_prompt(plan.question, plan.tried), json_mode=True,
                             temperature=0.0, timeout=timeout, max_tokens=120,
                             **stop_kwargs(llm, should_stop))
        data = json.loads(str(getattr(reply, "text", reply) or ""))
        raw = data.get("queries", []) if isinstance(data, dict) else []
    except Exception as exc:                            # noqa: BLE001 - a proposal only
        log.debug("planner model gave nothing usable: {}", exc)
        return []
    out: list[str] = []
    for item in raw[:3]:
        if not isinstance(item, str):
            continue
        cleaned = re.sub(r"[^\w\s'’\-]", " ", item)
        cleaned = " ".join(cleaned.split())[:120]
        if cleaned and cleaned not in plan.tried and cleaned not in out:
            out.append(cleaned)
    return out
