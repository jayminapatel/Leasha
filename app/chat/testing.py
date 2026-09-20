"""Test doubles for the chat engine and the chat tab: a model that follows its
prompt, and one that lies.

Layer: L8b - importable without Qt or a network; used by both engineers' tests and
by `evaluate --chat` when no real model is reachable.

**`FakeLLM` reads the prompts the engine actually sends** (`app/chat/prompts.py`
defines the format and the way to take it apart), so it is a deterministic model
in the ways that matter: given numbered sources and a question, it answers from
them. Its modes:

    extractive       the truth: the best-matching sentence(s) of the sources,
                     each ending in its marker. Deterministic and verifiable, so
                     the engine's whole path can be exercised with no Ollama.
    hallucinating    the opposite, on purpose. Every sentence it writes is one
                     the verifier must kill: a fabricated figure, an invented
                     name, a misquote, a marker for a source that does not exist,
                     a sentence with no marker at all, the source's own sentence
                     with its meaning reversed. **The guarantee test asserts
                     that none of them reaches a `ChatTurn`.** (A collage of the
                     document's own words is not in this list: on a two-sentence
                     document any collage is "near" enough to pass a lexical
                     proxy, which is a limit of the proxy, stated in
                     `app/chat/verify.py`, and tested on a longer passage in
                     `test_chat_verify.py`.)
    mixed            one true sentence, then the hallucinations - to show a
                     lie is dropped while the truth beside it is kept.
    not_found        the model declines ("NOT FOUND").
    empty            says nothing.
    scripted         replies from a list (or a callable), for one-off cases.

**Conversation** (`chat_stream` / `chat`, the `/api/chat` shape): the double reads the
system message the engine sends - `prompts.chat_kind` says whether it is a plain
conversation, an answer from numbered sources or the short general fill - and answers
accordingly. In an archive conversation it answers from the sources exactly as the
modes above do; in a plain conversation it answers from `chat_replies` (a list, or a
callable taking the messages) or, failing that, a short deterministic reply that
quotes the last thing said, so a test can see the *memory* arrive. Every conversation
call is recorded in `chat_calls` as `(kind, messages)`.

It streams (`stream`) in small pieces so the sentence-at-a-time verification and
the Stop button can be tested, and it can be told to be "down" (`up=False`) or to
fail part-way (`fail_after`).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Callable, Iterator, Optional, Sequence

from app.chat import prompts
from app.chat.text import content_tokens, sentence_spans
from app.core.errors import AppErrorException, make_error

__all__ = ["FakeLLM", "FakeReply", "hallucinations_for", "keyword_engine"]


@dataclass(frozen=True)
class FakeReply:
    text: str
    model: str = "fake"
    elapsed_s: float = 0.0


def _sentences_of(passage: str) -> list[str]:
    out = []
    for start, end in sentence_spans(passage):
        out.append(passage[start:end])
    return out


_NUMBER_WORDS = re.compile(r"\b(?:one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|"
                           r"twenty|thirty|forty|fifty|hundred|thousand)\b", re.I)
_MONTHS_OR_DAYS = re.compile(r"\b(?:january|february|march|april|may|june|july|august|september|"
                             r"october|november|december|monday|tuesday|wednesday|thursday|friday|"
                             r"saturday|sunday)\b", re.I)


_FIGURE_WORDS = re.compile(
    r"\b(?:number|amount|price|cost|total|rate|excess|premium|rent|fee|charge|mileage|balance)\b")


def _needs(question: str) -> str:
    """What kind of thing the question wants: "number", "date", "name" or "".

    A model does this without being told; a stub has to be. "How much..." wants a
    figure, so a sentence with no figure in it cannot be the answer.
    """
    q = re.sub(r"^\s*(?:and|also|so|then)\s+", "", question.strip().lower())
    q = q.split("(")[0]
    if re.match(r"(?:how\s+(?:much|many|long|often|old|far))", q) or _FIGURE_WORDS.search(q):
        return "number"
    if q.startswith("when"):
        return "date"
    if re.match(r"(?:who|whom|whose)\b", q):
        return "name"
    return ""


def _has_evidence(kind: str, sentence: str, asked: set[str] = frozenset()) -> bool:
    if kind == "number":
        return bool(re.search(r"\d", sentence) or _NUMBER_WORDS.search(sentence))
    if kind == "date":
        return bool(re.search(r"\d", sentence) or _MONTHS_OR_DAYS.search(sentence))
    if kind == "name":
        # A capitalised word the question did not itself contain.
        from app.chat.text import stem

        return any(stem(w.lower()) not in asked
                   for w in re.findall(r"(?<=[a-z,] )[A-Z][a-z]{2,}", sentence))
    return True


def _true_sentences(prompt: str, limit: int = 2) -> list[tuple[int, str]]:
    """The best-matching sentences of the sources, `(n, sentence)`, best first.

    Overlap with the question, weighted by whether the sentence has the kind of
    thing the question asks for (a figure for "how much"), and nudged by the
    source's file name - a model sees `safety-report-final.pdf` in the prompt and
    it matters to "the final report". Words in a trailing parenthesis (the context
    a follow-up carries) count for less than the question's own.
    """
    from app.chat.plan import FILLER
    from app.chat.text import stem

    sources = prompts.parse_sources(prompt)
    names = prompts.parse_names(prompt)
    text = prompts.parse_question(prompt)
    filler = {stem(w) for w in FILLER}
    main, _, context = text.partition("(")
    weights: dict[str, float] = {}
    for token in content_tokens(context.rstrip(") ")):
        if token not in filler:
            weights[token] = 0.4
    for token in content_tokens(main):
        if token not in filler:
            weights[token] = 1.0
    total = sum(weights.values())
    wants = _needs(text)
    asked = set(weights)
    scored: list[tuple[float, int, int, str]] = []
    for n, passage in sources.items():
        name_tokens = set(content_tokens(re.sub(r"[-_.]", " ", names.get(n, ""))))
        name_bonus = 0.3 * sum(weights.get(t, 0.0) for t in name_tokens) / (total or 1)
        for index, sentence in enumerate(_sentences_of(passage)):
            if re.match(r"^(?:subject|from|to|cc)\s*:", sentence, re.I):
                continue                                # a mail header is not an answer
            if wants in ("number", "date") and not _has_evidence(wants, sentence):
                continue
            tokens = set(content_tokens(sentence))
            if not tokens or not total:
                continue
            overlap = sum(weights.get(t, 0.0) for t in tokens) / total
            score = overlap
            if wants and _has_evidence(wants, sentence, asked):
                score += 0.35
            score += name_bonus
            # A sentence sharing no word with the question needs the document's
            # own name to vouch for it.
            if overlap <= 0 and score < 0.5:
                continue
            if overlap <= 0 and wants not in ("number", "date"):
                continue
            scored.append((score, n, index, sentence))
    scored.sort(key=lambda row: (-row[0], row[1], row[2]))
    if not scored or scored[0][0] < 0.3:
        return []
    best = scored[0][0]
    top = scored[0][1]
    return [(n, sentence) for score, n, _i, sentence in scored[:limit]
            if score >= 0.3 and score >= (0.6 if n == top else 0.95) * best]


def hallucinations_for(prompt: str) -> list[str]:
    """Sentences a lying model would write about these sources. Each one must be
    rejected by the verifier; each is rejected for a different reason."""
    sources = prompts.parse_sources(prompt)
    numbers = sorted(sources)
    first = numbers[0] if numbers else 1
    passage = sources.get(first, "")
    real = [s for s in _sentences_of(passage) if len(s.split()) >= 4]
    lies = [
        # a fabricated figure
        f"The total came to 7,341 pounds [{first}].",
        # an invented person
        f"Everything was settled verbally with Jonathan Featherstonehaugh [{first}].",
        # a misquote: quotation marks around words the source never says
        f'The document states "all payments are final and strictly non-refundable" [{first}].',
        # a marker for a source that was never shown
        "The matter was closed in full last spring [9].",
        # no marker at all
        "This is an important matter that deserves careful attention.",
        # a vague claim with a real marker but nothing behind it
        f"Nothing further was ever discussed about the arrangement afterwards [{first}].",
    ]
    if real:
        sentence = real[0].rstrip(".!? ")
        reversed_ = re.sub(r"\b(will|is|was|are|were|has|have|had|can)\b", r"\1 not", sentence, count=1)
        if reversed_ == sentence:
            reversed_ = f"It is not true that {sentence[0].lower() + sentence[1:]}"
        lies.append(f"{reversed_} [{first}].")
    return lies


class FakeLLM:
    """A deterministic stand-in for a language model. See the module docstring."""

    def __init__(
        self,
        mode: str = "extractive",
        *,
        model: str = "fake-1b",
        up: bool = True,
        installed: bool = True,
        script: Any = None,
        router_answer: str = "LOOKUP",
        planner_queries: Optional[list[str]] = None,
        rewrite: str = "",
        window: int = 4096,
        chunk: int = 9,
        fail_after: Optional[int] = None,
        models: Optional[list[str]] = None,
        chat_replies: Any = None,
        thinks: bool = False,
    ) -> None:
        self.mode = mode
        self.model = model
        self.up = up
        self.installed = installed
        self._script = list(script) if isinstance(script, (list, tuple)) else script
        self.router_answer = router_answer
        self.planner_queries = list(planner_queries or [])
        self.rewrite = rewrite
        self.window = window
        self.chunk = max(1, chunk)
        self.fail_after = fail_after
        self._models = list(models) if models is not None else [model]
        self._chat_replies = list(chat_replies) if isinstance(chat_replies, (list, tuple)) else chat_replies
        self.thinks = thinks
        #: `[(kind, messages), ...]` of every conversation call, for assertions.
        self.chat_calls: list[tuple[str, list[dict]]] = []
        #: The `think=` each conversation call asked for.
        self.think_asked: list[Optional[str]] = []
        #: The temperature each conversation call asked for.
        self.temperatures: list[float] = []
        #: `[(kind, prompt), ...]` of every prompt received, for assertions.
        self.calls: list[tuple[str, str]] = []

    # -- the four things the engine asks of a model ---------------------------------

    def health(self, *, force: bool = False) -> bool:
        return self.up

    def has_model(self) -> bool:
        return self.up and self.installed

    def available_models(self) -> list[str]:
        return list(self._models) if self.up else []

    def set_model(self, name: str) -> None:
        self.model = str(name or self.model)

    def context_window(self) -> int:
        return self.window

    def warm(self, **_kwargs: Any) -> bool:
        return self.up

    def _down(self) -> AppErrorException:
        return AppErrorException(make_error(
            "ERR_OLLAMA_DOWN", "chat.testing", details="the fake model is switched off",
            action_payload="ollama serve"))

    def generate(self, prompt: str, *, json_mode: bool = False, temperature: float = 0.0,
                 timeout: Optional[float] = None, max_tokens: Optional[int] = None,
                 stop: Optional[list[str]] = None, **_kwargs: Any) -> FakeReply:
        if not self.up:
            raise self._down()
        text = self._reply(prompt)
        if stop and "\n" in stop:
            text = text.split("\n")[0]
        return FakeReply(text=text, model=self.model)

    def stream(self, prompt: str, *, temperature: float = 0.0, timeout: Optional[float] = None,
               max_tokens: Optional[int] = None, stop: Optional[list[str]] = None,
               should_stop: Optional[Callable[[], bool]] = None) -> Iterator[str]:
        if not self.up:
            raise self._down()
        text = self._reply(prompt)
        sent = 0
        for at in range(0, len(text), self.chunk):
            if should_stop is not None and should_stop():
                return
            if self.fail_after is not None and sent >= self.fail_after:
                raise self._down()
            sent += 1
            yield text[at:at + self.chunk]

    def can_think(self) -> bool:
        return self.thinks

    def chat_stream(self, messages: Sequence[dict], *, temperature: float = 0.4,
                    timeout: Optional[float] = None, max_tokens: Optional[int] = None,
                    stop: Optional[list[str]] = None,
                    should_stop: Optional[Callable[[], bool]] = None,
                    think: Optional[str] = None) -> Iterator[str]:
        if not self.up:
            raise self._down()
        self.think_asked.append(think)
        self.temperatures.append(temperature)
        text = self._chat_reply(messages)
        sent = 0
        for at in range(0, len(text), self.chunk):
            if should_stop is not None and should_stop():
                return
            if self.fail_after is not None and sent >= self.fail_after:
                raise self._down()
            sent += 1
            yield text[at:at + self.chunk]

    def chat(self, messages: Sequence[dict], **kwargs: Any) -> FakeReply:
        return FakeReply(text="".join(self.chat_stream(messages, **kwargs)), model=self.model)

    def _chat_reply(self, messages: Sequence[dict]) -> str:
        kind = prompts.chat_kind(messages)
        self.chat_calls.append((kind, [dict(m) for m in messages]))
        self.calls.append(({"archive": "answer"}.get(kind, kind),
                           "\n\n".join(f"{m.get('role')}: {m.get('content')}" for m in messages)))
        replies = self._chat_replies
        if callable(replies):
            return str(replies(messages))
        if isinstance(replies, list) and replies:
            return str(replies.pop(0))
        last = next((str(m.get("content", "")) for m in reversed(messages)
                     if m.get("role") == "user"), "")
        if kind in ("archive", "web"):
            system = str(messages[0].get("content", ""))
            question = prompts.parse_question(system) or last
            text = self._answer(f"{system}\n\nQuestion: {question}\nAnswer:")
            return "From the web: " + text if kind == "web" and text != prompts.NOT_FOUND else text
        if kind == "general":
            return "In short, that is a general-knowledge answer from the fake model."
        if self.mode in ("empty",):
            return ""
        earlier = sum(1 for m in messages if m.get("role") == "user") - 1
        return f"Fake reply to: {last.strip()[:80]} (I can see {earlier} earlier message{'s' if earlier != 1 else ''})."

    # -- what it says -------------------------------------------------------------------

    def _reply(self, prompt: str) -> str:
        kind = prompts.kind_of(prompt)
        self.calls.append((kind, prompt))
        if callable(self._script):
            return str(self._script(prompt))
        if isinstance(self._script, list) and self._script:
            return str(self._script.pop(0))
        if kind == "router":
            return self.router_answer
        if kind == "planner":
            return json.dumps({"queries": self.planner_queries})
        if kind == "rewrite":
            return self.rewrite
        if kind == "translate":
            return prompts.parse_question(prompt) or ""
        if kind in ("answer", "extract", "combine"):
            return self._answer(prompt)
        return ""

    def _answer(self, prompt: str) -> str:
        mode = self.mode
        if mode == "empty":
            return ""
        if mode == "not_found":
            return prompts.NOT_FOUND
        truth = " ".join(f"{s.rstrip('.!? ')} [{n}]." for n, s in _true_sentences(prompt))
        if mode == "extractive":
            return truth or prompts.NOT_FOUND
        lies = " ".join(hallucinations_for(prompt))
        if mode == "hallucinating":
            return lies
        if mode == "mixed":
            return (truth + " " + lies).strip()
        return truth


def keyword_engine(store: Any) -> Any:
    """A real `SearchEngine` over `store` with **no embedding model and no
    vector store**: keyword search only, so a test needs nothing but SQLite and
    the ranking is fully deterministic. Chat's own behaviour is what the tests
    are about, not the embedder's."""
    from app.search.engine import SearchEngine

    class _NoVectors:
        def search(self, *_a: Any, **_k: Any) -> list:
            return []

    class _NoModel:
        def embed(self, _text: str) -> Any:
            return [0.0, 0.0, 0.0, 0.0]                 # a vector, so the semantic lane is quiet

        def embed_all(self, _texts: Sequence[str]) -> Any:
            raise RuntimeError("no embedding model in chat tests")

        def warm_up(self) -> None:
            return None

    return SearchEngine(store, _NoVectors(), _NoModel(), log_usage=False)
