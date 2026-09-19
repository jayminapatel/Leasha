"""`evaluate --chat`: does Chat answer well, and does it ever say something it cannot back?

Layer: L8b - no Qt. Work order section 4 (4a scoring, 4b floors, 4c latency).

The search harness (`app/search/evaluate.py`) asks "did the right document come
back". This asks the questions the Chat tab makes different:

    citation validity     of every sentence shown, how many have a receipt that
                          checks out - the marker points at a real source, the
                          quoted words are in that source's stored text, and the
                          sentence's meaningful words are in it too. (Floor 98%.)
    extractive            for a question about what a document says, is the fact in
                          the answer and does a receipt name the right document.
                          (Floor 85%.)
    aggregate exactness   is the number shown the number a plain-Python count of the
                          fixture gives. (Floor 100% - they are queries.)
    absence honesty       does every "nothing found" answer scope itself to the
                          index, show what was searched, and never claim the thing
                          does not exist in the world. (Floor 100%.)
    refusal               thin evidence -> says so, instead of guessing.
    router                did the question go to the right machine.
    find                  did the results include the documents that should be there,
                          and does the grid end by saying "that's all N".
    synthesis             measured, **no floor at v1** (work order 4b).
    latency               first narration line, first verified answer text, total.

**Two ways to run it, and the report says which.** With a real local model
(Ollama reachable) the numbers describe that model on this machine; without one the
deterministic `FakeLLM` stands in, and the numbers describe the engine, the router,
the verification and the corpus - **not any language model**. Both are useful and
they must never be confused, so the report's first line names the model and whether
it was real.

**The floors are recorded, not chosen.** `FLOORS` are the order's ship floors
(4b). What a given model actually scored is in the work order's measurement record
and pinned, with margin, in `tests/unit/test_chat_evaluate.py`.
"""

from __future__ import annotations

import os
import platform
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Sequence

from app.chat.absence import WORLD_CLAIMS
from app.chat.text import content_tokens, fold_quotes, normalise_space, sentence_spans
from app.chat.types import ChatTurn, NarrationEvent, ShelfEvent, TokenEvent
from app.chat.verify import audit_turn, normalise_marker_placement, split_markers

__all__ = [
    "CaseResult",
    "ChatReport",
    "run_chat_eval",
    "score_case",
    "FLOORS",
    "machine_description",
]

#: The order's ship floors (section 4b). A model below any of them does not ship.
FLOORS = {
    "citation_validity": 0.98,
    "aggregate_exactness": 1.0,
    "absence_honesty": 1.0,
    "extractive": 0.85,
}

_SIZE_UNITS = ("bytes", "KB", "MB", "GB", "TB")


def machine_description() -> str:
    """Where the numbers came from: OS, CPU count, memory. No host name."""
    ram = ""
    try:
        import psutil                                    # noqa: PLC0415 - optional

        ram = f", {psutil.virtual_memory().total / 1024 ** 3:.0f} GB RAM"
    except Exception:                                    # noqa: BLE001
        pass
    return f"{platform.platform()}, {os.cpu_count()} logical CPUs{ram}"


@dataclass
class CaseResult:
    """One question, run and scored."""

    qa: Any
    turn: ChatTurn
    routed: str = ""
    router_ok: bool = False
    kind_ok: bool = False
    correct: bool = False
    detail: str = ""
    sentences: int = 0
    valid_sentences: int = 0
    unreceipted: int = 0
    honest: Optional[bool] = None
    terminated: Optional[bool] = None
    first_narration_s: Optional[float] = None
    first_token_s: Optional[float] = None
    total_s: float = 0.0
    events: int = 0


def _norm(text: str) -> str:
    return normalise_space(fold_quotes(text)).lower()


def _first_count(text: str) -> Optional[int]:
    """The count an aggregate answer states: its first number that is not a size."""
    for match in re.finditer(r"\d[\d,]*(?:\.\d+)?", text):
        after = text[match.end():match.end() + 8].lstrip()
        if any(after.startswith(unit) for unit in _SIZE_UNITS):
            continue
        digits = match.group(0).replace(",", "")
        if "." in digits:
            continue
        return int(digits)
    return None


def _chunk_text(store: Any, chunk_id: Optional[int]) -> str:
    if not chunk_id:
        return ""
    try:
        row = store.conn.execute("SELECT text FROM chunks WHERE id = ?", (chunk_id,)).fetchone()
        return str(row[0]) if row else ""
    except Exception:                                    # noqa: BLE001
        return ""


def _citation_validity(turn: ChatTurn, store: Any) -> tuple[int, int, int]:
    """`(sentences, valid, unreceipted)` for an answer turn.

    A sentence is valid only if its marker points at a receipt, that receipt's
    quote is a verbatim slice of the stored chunk, and the sentence's meaningful
    words are found in that chunk. Checked against the *database*, not against
    anything the engine kept, so it is an independent test of the guarantee.
    """
    if turn.kind != "answer":
        return 0, 0, 0
    text = normalise_marker_placement(turn.text or "")
    unreceipted = len(audit_turn(turn))
    total = valid = 0
    for start, end in sentence_spans(text):
        sentence = text[start:end]
        body, numbers = split_markers(sentence)
        total += 1
        if not numbers or any(n < 1 or n > len(turn.receipts) for n in numbers):
            continue
        ok = True
        wanted = set(content_tokens(body))
        for n in numbers:
            receipt = turn.receipts[n - 1]
            chunk = _chunk_text(store, receipt.chunk_id)
            quote = _norm(receipt.quote)
            if not quote or quote not in _norm(chunk):
                ok = False
                break
            have = set(content_tokens(chunk))
            if wanted and len(wanted & have) / len(wanted) < 0.6:
                ok = False
                break
        if ok:
            valid += 1
    return total, valid, unreceipted


def _honest_absence(turn: ChatTurn) -> bool:
    """Does an absence answer scope itself to the index, show its working, and
    avoid claiming the thing does not exist?"""
    text = _norm(turn.text)
    if "index" not in text or "searched" not in text:
        return False
    if any(claim in text for claim in WORLD_CLAIMS):
        return False
    if not any(note.startswith("Searched for:") for note in turn.notes):
        return False
    return True


def score_case(qa: Any, turn: ChatTurn, store: Any) -> CaseResult:
    """Score one turn against one question. Pure apart from reading the store."""
    result = CaseResult(qa=qa, turn=turn)
    route = (turn.debug or {}).get("route", {})
    result.routed = str(route.get("kind", ""))
    result.router_ok = result.routed == qa.cls
    result.kind_ok = turn.kind == qa.outcome
    text = _norm(turn.text)
    paths = [r.path.replace("\\", "/").lower() for r in turn.receipts]

    result.sentences, result.valid_sentences, result.unreceipted = _citation_validity(turn, store)

    if qa.outcome == "answer":
        missing = [w for w in qa.expect_all if _norm(w) not in text]
        any_ok = (not qa.expect_any) or any(_norm(w) in text for w in qa.expect_any)
        cited = (not qa.cite) or any(qa.cite.lower() in p for p in paths)
        forbidden = [w for w in qa.forbid if _norm(w) in text]
        result.correct = (turn.kind == "answer" and not missing and any_ok and cited
                          and not forbidden)
        if not result.correct:
            result.detail = (f"kind={turn.kind}" + (f" missing={missing}" if missing else "")
                             + ("" if any_ok else " none of expect_any")
                             + ("" if cited else f" not cited: {qa.cite}")
                             + (f" forbidden present={forbidden}" if forbidden else ""))
    elif qa.outcome == "aggregate":
        stated = _first_count(turn.text)
        missing = [w for w in qa.expect_all if _norm(w) not in text]
        result.correct = (turn.kind == "aggregate" and stated == qa.count and not missing
                          and "counted across everything" in text)
        if not result.correct:
            result.detail = f"stated {stated}, expected {qa.count}" + (
                f" missing={missing}" if missing else "")
    elif qa.outcome == "find":
        found = {p for p in (r.path.replace("\\", "/").lower() for r in (turn.result_set or []))}
        missing = [f for f in qa.find if not any(f.lower() in p for p in found)]
        result.terminated = "that's all" in text or "best" in text
        result.correct = turn.kind == "find" and not missing and bool(result.terminated)
        if not result.correct:
            result.detail = f"kind={turn.kind} missing results={missing}"
    elif qa.outcome == "absence":
        result.honest = _honest_absence(turn) if turn.kind == "absence" else None
        forbidden = [w for w in qa.forbid if _norm(w) in text]
        result.correct = turn.kind == "absence" and bool(result.honest) and not forbidden
        if not result.correct:
            result.detail = f"kind={turn.kind}" + (f" forbidden present={forbidden}" if forbidden else "")
    return result


@dataclass
class ChatReport:
    """Every case, and the numbers that decide whether Chat ships."""

    model: str
    real_model: bool
    results: list[CaseResult] = field(default_factory=list)
    machine: str = ""
    note: str = ""
    roles: dict = field(default_factory=dict)

    # -- helpers ------------------------------------------------------------------

    def _of(self, predicate: Callable[[CaseResult], bool]) -> list[CaseResult]:
        return [r for r in self.results if predicate(r)]

    @staticmethod
    def _rate(items: Sequence[CaseResult], test: Callable[[CaseResult], bool]) -> Optional[float]:
        return (sum(1 for r in items if test(r)) / len(items)) if items else None

    # -- the measures -----------------------------------------------------------------

    @property
    def citation_validity(self) -> Optional[float]:
        total = sum(r.sentences for r in self.results)
        return (sum(r.valid_sentences for r in self.results) / total) if total else None

    @property
    def unreceipted_sentences(self) -> int:
        return sum(r.unreceipted for r in self.results)

    @property
    def extractive(self) -> Optional[float]:
        """Lookup and follow-up questions that are not traps and not synthesis."""
        items = self._of(lambda r: r.qa.outcome == "answer" and r.qa.cls in ("LOOKUP", "FOLLOWUP")
                         and not r.qa.trap)
        return self._rate(items, lambda r: r.correct)

    @property
    def aggregate_exactness(self) -> Optional[float]:
        return self._rate(self._of(lambda r: r.qa.outcome == "aggregate"), lambda r: r.correct)

    @property
    def absence_honesty(self) -> Optional[float]:
        """Of every absence answer given, the share that are honest."""
        items = self._of(lambda r: r.turn.kind == "absence")
        return self._rate(items, lambda r: bool(_honest_absence(r.turn)))

    @property
    def absence_recall(self) -> Optional[float]:
        """Of the planted absences, the share answered with the protocol."""
        return self._rate(self._of(lambda r: r.qa.planted), lambda r: r.correct)

    @property
    def refusal(self) -> Optional[float]:
        """Trap questions whose right answer is "I can't say from your files"."""
        return self._rate(self._of(lambda r: r.qa.trap and r.qa.outcome == "absence"),
                          lambda r: r.correct)

    @property
    def traps(self) -> Optional[float]:
        return self._rate(self._of(lambda r: r.qa.trap), lambda r: r.correct)

    @property
    def router(self) -> Optional[float]:
        return self._rate(self.results, lambda r: r.router_ok)

    @property
    def find(self) -> Optional[float]:
        return self._rate(self._of(lambda r: r.qa.outcome == "find"), lambda r: r.correct)

    @property
    def synthesis(self) -> Optional[float]:
        return self._rate(self._of(lambda r: r.qa.cls == "SYNTHESIS"), lambda r: r.correct)

    def latency(self, attribute: str) -> dict[str, Optional[float]]:
        values = sorted(v for v in (getattr(r, attribute) for r in self.results) if v is not None)
        if not values:
            return {"n": 0, "p50": None, "p95": None, "max": None}

        def rank(p: float) -> float:
            return values[min(len(values) - 1, max(0, int(round(p * len(values) + 0.4999)) - 1))]

        return {"n": len(values), "p50": round(rank(0.50), 3), "p95": round(rank(0.95), 3),
                "max": round(values[-1], 3)}

    # -- floors --------------------------------------------------------------------------

    def measures(self) -> dict[str, Optional[float]]:
        return {
            "citation_validity": self.citation_validity,
            "extractive": self.extractive,
            "aggregate_exactness": self.aggregate_exactness,
            "absence_honesty": self.absence_honesty,
            "absence_recall": self.absence_recall,
            "refusal": self.refusal,
            "traps": self.traps,
            "router": self.router,
            "find": self.find,
            "synthesis": self.synthesis,
        }

    def below_floor(self) -> list[str]:
        """The ship floors (4b) this run is under. Empty means it clears them."""
        measured = self.measures()
        return [f"{name} {measured[name]:.1%} is below {floor:.0%}"
                for name, floor in FLOORS.items()
                if measured[name] is not None and measured[name] < floor - 1e-9]

    def as_dict(self) -> dict[str, Any]:
        return {
            "model": self.model, "real_model": self.real_model, "machine": self.machine,
            "roles": self.roles, "questions": len(self.results),
            "measures": {k: (None if v is None else round(v, 4)) for k, v in self.measures().items()},
            "unreceipted_sentences": self.unreceipted_sentences,
            "sentences": sum(r.sentences for r in self.results),
            "latency": {
                "first_narration_s": self.latency("first_narration_s"),
                "first_token_s": self.latency("first_token_s"),
                "total_s": self.latency("total_s"),
            },
            "below_floor": self.below_floor(),
            "failures": [
                {"id": r.qa.id, "question": r.qa.question, "detail": r.detail,
                 "routed": r.routed, "expected_class": r.qa.cls, "answer": r.turn.text[:300]}
                for r in self.results if not r.correct or not r.router_ok],
            "note": self.note,
        }

    def lines(self) -> list[str]:
        def pct(value: Optional[float]) -> str:
            return "   n/a" if value is None else f"{value:>6.1%}"

        kind = "a real local model" if self.real_model else \
            "the deterministic FakeLLM - NOT a language model"
        out = [
            f"Chat evaluation  ({len(self.results)} questions)",
            "=" * 62,
            f"  model      {self.model}  ({kind})",
            f"  machine    {self.machine}",
        ]
        if self.roles:
            out.append("  roles      " + ", ".join(f"{k}={v}" for k, v in self.roles.items()))
        if not self.real_model:
            out += ["", "  No language model was used. These numbers describe the router, the",
                    "  loop, the verification and the counting - not how any model writes."]
        out += [
            "",
            f"  citation validity     {pct(self.citation_validity)}   floor 98%"
            f"   ({sum(r.valid_sentences for r in self.results)} of "
            f"{sum(r.sentences for r in self.results)} sentences; "
            f"{self.unreceipted_sentences} without a receipt)",
            f"  extractive            {pct(self.extractive)}   floor 85%",
            f"  aggregate exactness   {pct(self.aggregate_exactness)}   floor 100%",
            f"  absence honesty       {pct(self.absence_honesty)}   floor 100%",
            f"  absence recall        {pct(self.absence_recall)}   (planted absences answered as absence)",
            f"  refusal on thin       {pct(self.refusal)}",
            f"  trap questions        {pct(self.traps)}",
            f"  router                {pct(self.router)}",
            f"  find                  {pct(self.find)}",
            f"  synthesis             {pct(self.synthesis)}   no floor at v1",
        ]
        for label, key in (("first narration", "first_narration_s"),
                           ("first answer text", "first_token_s"), ("whole answer", "total_s")):
            lat = self.latency(key)
            if lat["n"]:
                out.append(f"  {label:<21} p50 {lat['p50']:.2f}s   p95 {lat['p95']:.2f}s   "
                           f"max {lat['max']:.2f}s   (n={lat['n']})")
        floors = self.below_floor()
        out += ["", "  " + ("Clears every ship floor." if not floors
                            else "BELOW THE SHIP FLOOR: " + "; ".join(floors))]
        misses = [r for r in self.results if not r.correct or not r.router_ok]
        if misses:
            out += ["", f"  Not right ({len(misses)}):"]
            for r in misses:
                why = r.detail or ("routed as " + r.routed + f", expected {r.qa.cls}")
                if not r.router_ok and r.detail:
                    why += f"; routed {r.routed}, expected {r.qa.cls}"
                out.append(f"    {r.qa.id}  {r.qa.question}")
                out.append(f"          {why}")
        if self.note:
            out += ["", f"  {self.note}"]
        return out


def run_chat_eval(
    chat: Any,
    questions: Sequence[Any],
    store: Any,
    *,
    model: str = "fake",
    real_model: bool = False,
    note: str = "",
    on_result: Optional[Callable[[CaseResult], None]] = None,
) -> ChatReport:
    """Ask every question through `chat` and score it.

    A follow-up's `history` questions are asked first, on the same engine, and
    their turns are the conversation it continues - so a follow-up is measured
    against the answers this engine actually gave, not a script.
    """
    report = ChatReport(model=model, real_model=real_model, machine=machine_description(),
                        note=note)
    try:
        report.roles = {k: v for k, v in chat.roles().__dict__.items()
                        if k in ("router", "planner", "answerer")}
    except Exception:                                    # noqa: BLE001
        report.roles = {}

    for qa in questions:
        history: list[ChatTurn] = []
        for earlier in getattr(qa, "history", ()):
            turn = chat.ask(earlier, list(history), lambda _e: None, lambda: False)
            history += [ChatTurn("user", earlier), turn]

        stamps: dict[str, float] = {}
        started = time.perf_counter()
        count = {"n": 0}

        def emit(event: Any, _s: dict = stamps, _t0: float = started, _c: dict = count) -> None:
            _c["n"] += 1
            now = time.perf_counter() - _t0
            if isinstance(event, NarrationEvent):
                _s.setdefault("narration", now)
            elif isinstance(event, TokenEvent):
                _s.setdefault("token", now)
            elif isinstance(event, ShelfEvent):
                _s.setdefault("shelf", now)

        turn = chat.ask(qa.question, list(history), emit, lambda: False)
        result = score_case(qa, turn, store)
        result.total_s = time.perf_counter() - started
        result.first_narration_s = stamps.get("narration")
        result.first_token_s = stamps.get("token")
        result.events = count["n"]
        report.results.append(result)
        if on_result is not None:
            on_result(result)
    return report


# ---------------------------------------------------------------------------
# the command line: `python -m app.cli evaluate --chat`
# ---------------------------------------------------------------------------

def _pick_models(settings: Any, requested: str, force_fake: bool) -> tuple[Any, str, bool, str]:
    """`(llm, label, real, note)`: real Ollama models when reachable, else FakeLLM.

    **The fallback is announced, never silent** - a run that quietly measured a
    stub while looking like it measured a model would be a benchmark that lies.
    """
    from app.chat.testing import FakeLLM

    if force_fake:
        return FakeLLM("extractive"), "FakeLLM (extractive)", False, "Forced with --chat-fake."
    from app.chat.llm import OllamaLLM
    from app.chat.roles import resolve_roles
    from app.llm.ollama import OllamaClient

    url = str(getattr(settings, "ollama_url", "") or "http://127.0.0.1:11434")
    configured = requested or str(getattr(settings, "chat_model", "") or "") \
        or str(getattr(settings, "ollama_model", "") or "mistral")
    probe = OllamaLLM(OllamaClient(url, configured))
    if not probe.health(force=True):
        return (FakeLLM("extractive"), "FakeLLM (extractive)", False,
                f"Nothing answered at {url}, so the deterministic FakeLLM stood in. Start Ollama "
                "and run again to measure a real model.")
    installed = probe.available_models()
    roles = resolve_roles(
        installed, configured=configured, answerer=requested,
        router=str(getattr(settings, "chat_router_model", "") or ""),
        planner=str(getattr(settings, "chat_planner_model", "") or ""))
    clients: dict[str, Any] = {}

    def client(name: str) -> Any:
        if name not in clients:
            clients[name] = OllamaLLM(OllamaClient(url, name, timeout=180.0))
        return clients[name]

    llm = {"router": client(roles.router), "planner": client(roles.planner),
           "answerer": client(roles.answerer)}
    if not llm["answerer"].has_model():
        return (FakeLLM("extractive"), "FakeLLM (extractive)", False,
                f"Ollama is running but '{roles.answerer}' is not installed "
                f"(ollama pull {roles.answerer}), so the FakeLLM stood in.")
    for name in roles.distinct():
        client(name).warm()
    return llm, roles.answerer, True, "; ".join(roles.notes)


def run_cli(args: Any) -> int:
    """Run the fixture QA set and print the report. Returns a process exit code."""
    import json
    import tempfile
    from pathlib import Path

    from app.chat.config import ChatSettings
    from app.chat.engine import ChatEngine
    from app.chat.testing import keyword_engine
    from app.storage.sqlite_store import SqliteStore

    try:
        from tests.fixtures.chat_eval import QUESTIONS, TODAY, load_into
    except ImportError:
        print("The chat fixture ships with the tests, which are not installed here.")
        print("Run from a source checkout.")
        return 1

    try:
        from app.cli._common import _load

        settings = _load(args)
    except Exception:                                    # noqa: BLE001 - defaults are fine
        settings = None

    questions = list(QUESTIONS)
    only = str(getattr(args, "chat_ids", "") or "").strip()
    if only:
        wanted = {part.strip().upper() for part in only.split(",") if part.strip()}
        questions = [q for q in questions if q.id.upper() in wanted]
        if not questions:
            print(f"None of {sorted(wanted)} is a question id in the fixture.")
            return 1

    llm, label, real, note = _pick_models(
        settings, str(getattr(args, "chat_model", "") or ""), bool(getattr(args, "chat_fake", False)))

    folder = Path(tempfile.mkdtemp(prefix="leasha-chat-eval-"))
    with SqliteStore(folder / "chat-eval.db") as store:
        load_into(store)
        search = keyword_engine(store)
        chat = ChatEngine(search, store, llm, ChatSettings.from_settings(settings, today=TODAY))

        def progress(result: CaseResult) -> None:
            mark = "ok " if result.correct and result.router_ok else "NOT"
            print(f"  {mark} {result.qa.id}  {result.total_s:5.1f}s  {result.qa.question}",
                  flush=True)

        print(f"Asking {len(questions)} questions of {label}"
              f"{'' if real else ' (no real model - see the note in the report)'} ...")
        report = run_chat_eval(chat, questions, store, model=label, real_model=real,
                               note=note, on_result=progress)
        search.close()

    print()
    for line in report.lines():
        print(line)
    if getattr(args, "json", False):
        print()
        print(json.dumps(report.as_dict(), indent=2))
    return 0
