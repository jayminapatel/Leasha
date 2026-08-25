r"""Does a plain-English sentence find the right document? Measured, not assumed.

Layer: L4

The work order's first instruction was to measure before building: type twenty
real sentences, record whether the wanted document came back in the top ten, and
let that number decide whether query translation is worth having. Its stated
expectation, worth checking rather than believing:

> **topic matching will be decent, constraints will be ignored.** It will find
> safety reports about Leeds; it will not reliably honour "from Dave" or "before
> the audit", because embeddings encode meaning, not metadata.

This module is that measurement, made repeatable. A `Question` is a sentence and
the path that should come back; `evaluate` runs each one and reports recall at k,
split by whether the sentence carried a constraint.

**The split is the whole point.** One overall number cannot distinguish "search
is bad" from "search is fine at topics and blind to constraints", and those have
completely different fixes - the second is precisely what Layer 8a addresses. A
harness that reported a single percentage would have hidden the finding it exists
to produce.

**Three modes, because they answer different questions.**

| mode | what runs | answers |
|---|---|---|
| `keyword` | BM25 only | is the text findable at all? |
| `hybrid` | BM25 + embeddings | does plain English work? |
| `translated` | Layer 8a, then hybrid | do constraints get honoured? |

`keyword` needs no model, which matters: it is the half that can be checked
anywhere, including in an environment with no network. `hybrid` and `translated`
need the embedding model, and `translated` additionally needs Ollama - so a
result carries which modes actually ran, and never silently reports a number
from a mode that quietly degraded.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Sequence

__all__ = [
    "Question",
    "Outcome",
    "Report",
    "evaluate",
    "DEFAULT_K",
]

#: **Rank 1, not 10.**
#:
#: Ten is the work order's bar and is right for a real corpus. On the twenty-one
#: document built-in corpus it is half of everything, so it reported **100% on
#: every category** - a benchmark that cannot fail is worse than no benchmark,
#: because it gets believed and then quoted.
#:
#: One is the honest question: did the right document come *first*? On a small
#: corpus that is the only measure with any discrimination left in it, and on a
#: large one it is the number that matches what somebody actually experiences -
#: nobody scrolls to rank nine and calls it a good search.
DEFAULT_K = 1


@dataclass(frozen=True, slots=True)
class Question:
    """One sentence, and the document that should come back for it."""

    sentence: str
    #: A distinctive fragment of the wanted document's path. A fragment rather
    #: than a full path so the same questions work against a corpus laid out
    #: anywhere.
    expects: str
    #: What kind of thing the sentence asks for beyond its topic: "sender",
    #: "date", "type", "recipient", "attachment", or "" for a pure topic
    #: question. **This is what makes the report useful** - it is the axis the
    #: whole measurement exists to split on.
    constraint: str = ""
    #: A note for the reader of the report, not used in scoring.
    why: str = ""

    @property
    def constrained(self) -> bool:
        return bool(self.constraint)


@dataclass(frozen=True, slots=True)
class Outcome:
    """What happened for one question."""

    question: Question
    #: 1-based position of the wanted document, or None if it never appeared.
    rank: Optional[int]
    returned: int
    #: The query actually run - the sentence itself, or what Layer 8a made of it.
    query: str = ""
    note: str = ""

    def hit(self, k: int = DEFAULT_K) -> bool:
        return self.rank is not None and self.rank <= k


@dataclass
class Report:
    """Every outcome, and the split that decides what to do next."""

    mode: str
    k: int = DEFAULT_K
    outcomes: list[Outcome] = field(default_factory=list)
    note: str = ""

    def _rate(self, questions: Sequence[Outcome]) -> float:
        return sum(1 for o in questions if o.hit(self.k)) / len(questions) if questions else 0.0

    @property
    def topic_only(self) -> list[Outcome]:
        return [o for o in self.outcomes if not o.question.constrained]

    @property
    def constrained(self) -> list[Outcome]:
        return [o for o in self.outcomes if o.question.constrained]

    @property
    def overall(self) -> float:
        return self._rate(self.outcomes)

    @property
    def topic_recall(self) -> float:
        return self._rate(self.topic_only)

    @property
    def constrained_recall(self) -> float:
        return self._rate(self.constrained)

    def by_constraint(self) -> dict[str, float]:
        """Recall for each kind of constraint separately.

        `from:` and `before:` fail for different reasons and are worth different
        amounts to fix, so they are not averaged together.
        """
        kinds: dict[str, list[Outcome]] = {}
        for outcome in self.constrained:
            kinds.setdefault(outcome.question.constraint, []).append(outcome)
        return {kind: self._rate(group) for kind, group in sorted(kinds.items())}

    def as_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "k": self.k,
            "questions": len(self.outcomes),
            "overall": round(self.overall, 3),
            "topic_only": round(self.topic_recall, 3),
            "constrained": round(self.constrained_recall, 3),
            "by_constraint": {k: round(v, 3) for k, v in self.by_constraint().items()},
            "note": self.note,
            "misses": [
                {"sentence": o.question.sentence, "expected": o.question.expects,
                 "query": o.query, "constraint": o.question.constraint}
                for o in self.outcomes if not o.hit(self.k)
            ],
        }

    def lines(self) -> list[str]:
        """The report as text. The split first, because it is the finding."""
        out = [
            f"Recall at {self.k}  ({self.mode} mode, {len(self.outcomes)} sentences)",
            "=" * 62,
            f"  overall                {self.overall:>6.0%}",
            f"  topic only             {self.topic_recall:>6.0%}"
            f"   ({len(self.topic_only)} sentences)",
            f"  with a constraint      {self.constrained_recall:>6.0%}"
            f"   ({len(self.constrained)} sentences)",
        ]
        by_kind = self.by_constraint()
        if by_kind:
            out.append("")
            for kind, rate in by_kind.items():
                out.append(f"    {kind:<20} {rate:>6.0%}")

        if self.k > 1:
            out += [
                "",
                f"  Counting a hit anywhere in the top {self.k}. Rank 1 is the honest",
                "  question - on a small corpus a generous k flatters everything.",
            ]

        misses = [o for o in self.outcomes if not o.hit(self.k)]
        if misses:
            out += ["", f"  Missed ({len(misses)}):"]
            for outcome in misses:
                out.append(f"    {outcome.question.sentence}")
                out.append(f"      wanted {outcome.question.expects}"
                           f"   got {outcome.returned} results"
                           f"{f', rank {outcome.rank}' if outcome.rank else ''}")
                if outcome.query and outcome.query != outcome.question.sentence:
                    out.append(f"      searched: {outcome.query}")
        if self.note:
            out += ["", f"  {self.note}"]
        return out


def evaluate(
    questions: Sequence[Question],
    search: Callable[[str], list[str]],
    *,
    mode: str = "keyword",
    k: int = DEFAULT_K,
    translate: Optional[Callable[[str], str]] = None,
    note: str = "",
) -> Report:
    """Run every question and score it.

    `search` takes a query and returns paths, best first - so the harness knows
    nothing about the engine and can be driven by a fake in a test. `translate`
    is Layer 8a when the mode calls for it.

    A question whose document is not in the corpus at all would score zero and
    look like a retrieval failure, so the caller is responsible for ensuring
    every `expects` is present; `app.cli evaluate` checks it and says so.
    """
    report = Report(mode=mode, k=k, note=note)

    for question in questions:
        query = question.sentence
        note_for_this = ""
        if translate is not None:
            translated = translate(question.sentence)
            if translated and translated != question.sentence:
                query = translated
            else:
                note_for_this = "not translated"

        paths = search(query)
        rank = next(
            (index for index, path in enumerate(paths, start=1)
             if question.expects.lower() in path.lower()),
            None,
        )
        report.outcomes.append(Outcome(
            question=question, rank=rank, returned=len(paths),
            query=query, note=note_for_this,
        ))

    return report
