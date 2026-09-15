r"""One question, start to finish: route, retrieve, answer.

Layer: L8b — ties §1a (the router), §1b (the loop), §1d (aggregate), §1e
(absence) and `answer.py` (generation + verification) into the single call
a caller actually needs to make. Nothing here is new logic - every decision
already lives in the module that owns it; this is composition, in the
order the sections themselves are numbered.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, Protocol, Sequence

from app.chat.absence import AbsenceAnswer, answer_absence, is_empty
from app.chat.aggregate import AggregateAnswer, answer_aggregate
from app.chat.answer import Answer, answer_question
from app.chat.loop import LoopResult, run_loop
from app.chat.router import QuestionClass, RouteDecision, classify

__all__ = ["AskResult", "ask"]


class _Engine(Protocol):
    def search(self, raw: str, **kwargs: Any) -> Any: ...


@dataclass(frozen=True, slots=True)
class AskResult:
    """Exactly one of `answer` / `aggregate` / `absence` / `find_results`
    is populated, named by `kind` - a caller switches on `kind` once rather
    than checking every field for `None`.
    """

    question: str
    route: RouteDecision
    kind: str = "absence"           # "answer" | "aggregate" | "absence" | "find"
    answer: Optional[Answer] = None
    aggregate: Optional[AggregateAnswer] = None
    absence: Optional[AbsenceAnswer] = None
    #: `SearchResult`-shaped objects, for a FIND-routed question - the
    #: order's own §1: "return results, not prose."
    find_results: tuple = field(default_factory=tuple)


def ask(
    question: str,
    *,
    engine: _Engine,
    store: Any,
    translator: Any = None,
    client: Any = None,
    embedder: Any = None,
    history: Optional[Sequence[Any]] = None,
    roots: Sequence[str] = (),
    policy: Any = None,
    context_length: Optional[int] = None,
) -> AskResult:
    """Classify, then answer the way that class calls for.

    **AGGREGATE never retrieves at all** - `count_matching` is a direct
    query, and running the loop first would cost a search whose results
    are then thrown away. Every other class retrieves first, because
    whether *anything* was found is what decides FIND/LOOKUP/SYNTHESIS from
    the honest-absence case, not the question's own wording.
    """
    raw = (question or "").strip()
    route = classify(raw, client=client, history=history)

    if route.question_class == QuestionClass.AGGREGATE:
        aggregate = answer_aggregate(raw, store=store, translator=translator, client=client)
        return AskResult(question=raw, route=route, kind="aggregate", aggregate=aggregate)

    loop_result = run_loop(raw, engine=engine, client=client, translator=translator, policy=policy)

    if is_empty(loop_result):
        absence = answer_absence(loop_result, roots=roots)
        return AskResult(question=raw, route=route, kind="absence", absence=absence)

    if route.question_class == QuestionClass.FIND:
        return AskResult(question=raw, route=route, kind="find",
                         find_results=tuple(loop_result.all_results))

    passages = [getattr(result, "text", "") for result in loop_result.all_results]
    answer = answer_question(
        raw, passages, embedder=embedder, client=client,
        context_length=context_length, terms=_terms(loop_result),
    )
    if answer.thin:
        absence = answer_absence(loop_result, roots=roots)
        return AskResult(question=raw, route=route, kind="absence", absence=absence)
    return AskResult(question=raw, route=route, kind="answer", answer=answer)


def _terms(result: LoopResult) -> tuple[str, ...]:
    """Words to centre `build_context`'s windows on - the query actually
    run, split crudely rather than reusing the parser: this only feeds a
    "which part of the passage is worth keeping" heuristic, not a search,
    so a rough split costs nothing a real parse would have bought back."""
    if not result.queries:
        return ()
    return tuple(result.queries[-1].split())
