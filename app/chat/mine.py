"""The owner's own questions, asked of the real index and scored.

Layer: L8b - no Qt. The engine is handed in.

Order `smarter-answers` 6a (owner, 2026-10-12: "the questions is the last once the
full index is done"). The built-in fixture proves the machinery on 46 invented
documents; only the owner's questions over the owner's files say whether Leasha
is smart *for them*. One question per line, the same shape `evaluate --questions`
already reads for search, with the fact the answer must state added:

    # question | part of the file's path | words the answer must contain
    What did we agree with the landlord about the deposit? | landlord-deposit | 950
    When is the next MOT due? | mot-certificate | 2026

A question is right when the answer cites a file whose path contains the second
part, and - when a third part is given - says those words. Both are checked from
the finished turn, the one the person would read.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

__all__ = ["OwnerQuestion", "OwnerResult", "OwnerReport", "read_owner_questions",
           "score", "run_owner_questions"]


@dataclass(frozen=True)
class OwnerQuestion:
    question: str
    expects: str            # part of the wanted file's path, any case, either slash
    fact: str = ""          # words the answer must contain; "" = citing the file is enough
    line: int = 0


@dataclass
class OwnerResult:
    q: OwnerQuestion
    cited: bool
    stated: Optional[bool]          # None when no fact was asked for
    kind: str
    seconds: float
    answer: str = ""

    @property
    def right(self) -> bool:
        return self.cited and self.stated is not False


@dataclass
class OwnerReport:
    results: list[OwnerResult] = field(default_factory=list)
    model: str = ""

    def share(self, test: Callable[[OwnerResult], bool], of: Sequence[OwnerResult]) -> float:
        return sum(1 for r in of if test(r)) / len(of) if of else 0.0

    def as_dict(self) -> dict[str, Any]:
        with_fact = [r for r in self.results if r.stated is not None]
        times = sorted(r.seconds for r in self.results)
        return {
            "model": self.model,
            "questions": len(self.results),
            "right": round(self.share(lambda r: r.right, self.results), 4),
            "cited_the_file": round(self.share(lambda r: r.cited, self.results), 4),
            "stated_the_fact": round(self.share(lambda r: bool(r.stated), with_fact), 4),
            "seconds_p50": round(times[len(times) // 2], 2) if times else 0.0,
            "seconds_max": round(times[-1], 2) if times else 0.0,
            "wrong": [{"line": r.q.line, "question": r.q.question, "kind": r.kind,
                       "cited": r.cited, "stated": r.stated, "answer": r.answer[:300]}
                      for r in self.results if not r.right],
        }

    def lines(self) -> list[str]:
        d = self.as_dict()
        out = ["", "Your questions", "=" * 62,
               f"  model            {d['model']}",
               f"  questions        {d['questions']}",
               f"  right            {d['right']:.1%}   (cited the file, and said the fact when one was given)",
               f"  cited the file   {d['cited_the_file']:.1%}",
               f"  said the fact    {d['stated_the_fact']:.1%}",
               f"  seconds          p50 {d['seconds_p50']}   max {d['seconds_max']}"]
        if d["wrong"]:
            out += ["", f"  Not right ({len(d['wrong'])}):"]
            for wrong in d["wrong"]:
                out.append(f"    line {wrong['line']}: {wrong['question']}")
                out.append(f"          kind={wrong['kind']} cited={wrong['cited']} "
                           f"stated={wrong['stated']}")
        return out


def read_owner_questions(path: Path) -> list[OwnerQuestion]:
    """`question | path part | fact` per line; `#` starts a comment; blank lines skipped.
    A line with no path part is reported, not guessed at."""
    out: list[OwnerQuestion] = []
    for number, raw in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 2 or not parts[0] or not parts[1]:
            raise ValueError(f"line {number}: write it as  question | part of the file's path"
                             f" | words the answer must contain")
        out.append(OwnerQuestion(parts[0], parts[1], parts[2] if len(parts) > 2 else "", number))
    return out


def _norm(text: str) -> str:
    return str(text or "").replace("\\", "/").casefold()


def score(turn: Any, q: OwnerQuestion, seconds: float = 0.0) -> OwnerResult:
    """How one finished turn did against one question."""
    want = _norm(q.expects)
    paths = [_norm(getattr(r, "path", "")) for r in (getattr(turn, "receipts", None) or [])]
    paths += [_norm(getattr(r, "path", "")) for r in (getattr(turn, "result_set", None) or [])]
    text = str(getattr(turn, "text", "") or "")
    stated = None if not q.fact else q.fact.casefold() in text.casefold()
    return OwnerResult(q, any(want in p for p in paths), stated,
                       str(getattr(turn, "kind", "") or ""), seconds, text)


def run_owner_questions(chat: Any, questions: Sequence[OwnerQuestion], *, model: str = "",
                        on_result: Optional[Callable[[OwnerResult], None]] = None) -> OwnerReport:
    """Ask each question as the start of a conversation, and score it."""
    report = OwnerReport(model=model)
    for q in questions:
        started = time.perf_counter()
        turn = chat.ask(q.question, [], lambda _event: None, lambda: False)
        result = score(turn, q, time.perf_counter() - started)
        report.results.append(result)
        if on_result is not None:
            on_result(result)
    return report
