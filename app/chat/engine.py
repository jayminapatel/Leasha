"""The chat engine: a search agent that narrates its searching and cannot lie.

Layer: L8b - no Qt (the tab runs `ask` on a worker and forwards `emit` by signal).
Work order `202626270611-chat-tab`, sections 1-2.

    question -> route -> plan -> search (<=3 rounds) -> assess -> answer -> verify

**The design in one paragraph.** Naive "chat with your documents" stuffs retrieved
text into a prompt and ships whatever comes back. This does not. The *router*
decides whether the question is a database question (counting: computed, never
generated), a search (FIND: the answer is the results themselves), an existence
question (ABSENCE: what was searched, honestly scoped) or a question about what
documents say (LOOKUP/SYNTHESIS). Only the last needs a model to write anything -
and what it writes passes through `verify.py` a sentence at a time: no sentence
reaches the screen without a source marker that points at a retrieved passage
which supports it, with every figure, date and name found in that passage and
every quotation verbatim. **The guarantee is structural**: `ChatTurn.text` of an
answer is built only from `AnswerAssembler.accepted`. A model that hallucinates
produces an empty answer and an honest refusal, never a confident wrong one.

**Search is separate and untouched.** Nothing here runs in the search path; the
engine calls `SearchEngine.search` like any other caller. With Ollama stopped,
counts, searches and "do I have..." still answer (none needs a model), and
questions about content fall back to quoting the best-matching passages.

**Contract with the tab** (`app/chat/types.py`): `ask(question, history, emit,
should_stop) -> ChatTurn`. It blocks, never raises, honours `should_stop()`
between every step and between every streamed piece, and emits `NarrationEvent`
(first one before any model is consulted), `TokenEvent` (verified sentences only)
and `ShelfEvent` (a document the answer now stands on).
"""

from __future__ import annotations

import time
from datetime import date
from typing import Any, Callable, Optional, Sequence

from app.chat import absence, prompts
from app.chat.aggregate import parse_aggregate, run_aggregate
from app.chat.config import ChatSettings
from app.chat.context import Source, build_sources
from app.chat.llm import OllamaLLM, as_llm
from app.chat.plan import Assessment, Plan, assess, make_plan, planner_queries, widen
from app.chat.roles import RoleModels, resolve_roles, suggest_modes
from app.chat.router import (
    ABSENCE, AGGREGATE, FIND, LOOKUP, SYNTHESIS, Route, route_question,
)
from app.chat.types import ChatTurn, NarrationEvent, Receipt, ShelfEvent, TokenEvent
from app.chat.verify import Accepted, AnswerAssembler, Verifier
from app.core.errors import AppErrorException
from app.core.logging import logger
from app.search.policy import SearchPolicy

__all__ = ["ChatEngine", "CHAT_POLICY", "ANSWER_MAX_TOKENS"]

log = logger.bind(component="chat.engine")

#: What search may do on chat's behalf. **Nothing that rewrites the question**
#: silently - relaxing, filter chips, folding - because the loop does its own
#: widening, visibly, and the absence answer must be able to say exactly what was
#: searched. Spelling correction stays on (a typo should not read as absence) and
#: so does the mild preference for recent documents: people ask about how things
#: stand now, and an expired agreement should not outrank the current one.
CHAT_POLICY = SearchPolicy(
    relax_on_empty=False, auto_chips=False, recency_blend=True,
    version_folding=False, explain_results=False,
)

#: Tokens an answer may run to. A verified answer is a few sentences; anything
#: longer is a model that has stopped answering and started talking.
ANSWER_MAX_TOKENS = 260

#: Passages fetched per search round. More than the model is shown, because the
#: ranking, dedupe by document and the sufficiency check all want a wider net.
_RETRIEVE_LIMIT = 24

#: Documents a FIND answer shows a receipt (a quote) for.
_FIND_RECEIPTS = 20

#: Documents put on the shelf by a FIND / list answer, so a follow-up can start
#: from them without the shelf filling with fifty chips.
_FIND_SHELF = 5

_NO_MODEL_NOTE = ("No AI model is running, so these are the passages that best match your "
                  "question, quoted exactly. Start Ollama to get written answers.")


class _Hit:
    """A search-result-shaped record made from a stored chunk, for the shelf."""

    def __init__(self, chunk: Any, path: str, rank: int, score: float) -> None:
        self.chunk_id = chunk.id
        self.file_id = chunk.file_id
        self.path = path
        self.text = chunk.text
        self.page = chunk.page
        self.label = ""
        self.score = score
        self.rank = rank


class _Retrieval:
    def __init__(self) -> None:
        self.results: list[Any] = []
        self.assessment: Optional[Assessment] = None
        self.rounds = 0
        self.dropped_filters = False
        self.from_shelf = False
        self.unmatched: tuple[str, ...] = ()


class ChatEngine:
    """Ask questions of the archive. See the module docstring."""

    def __init__(self, search_engine: Any, store: Any, llm: Any = None,
                 settings: Any = None) -> None:
        self.search_engine = search_engine
        self.store = store
        self.cfg = settings if isinstance(settings, ChatSettings) else ChatSettings.from_settings(settings)
        self.today: Optional[date] = self.cfg.today

        #: `llm` may be one model for every role, a `{"router"|"planner"|"answerer": model}`
        #: mapping, or `None` (built from settings, roles chosen from what is installed).
        self._fixed: Optional[dict[str, Any]] = None
        if isinstance(llm, dict):
            fallback = as_llm(llm.get("answerer")) or as_llm(next(iter(llm.values()), None))
            self._fixed = {role: as_llm(llm.get(role)) or fallback
                           for role in ("router", "planner", "answerer")}
        elif llm is not None:
            shared = as_llm(llm)
            self._fixed = {"router": shared, "planner": shared, "answerer": shared}

        self._overrides: dict[str, str] = {}
        self._roles: Optional[RoleModels] = None
        self._clients: dict[str, OllamaLLM] = {}
        self._pinned: set[int] = set()
        self._excluded: set[int] = set()

    # ------------------------------------------------------------------ models

    def _base_client(self) -> OllamaLLM:
        return self._client_for(self.cfg.ollama_model)

    def _client_for(self, name: str) -> OllamaLLM:
        if name not in self._clients:
            from app.llm.ollama import OllamaClient

            self._clients[name] = OllamaLLM(
                OllamaClient(self.cfg.ollama_url, name, timeout=self.cfg.timeout_s))
        return self._clients[name]

    def installed_models(self) -> list[str]:
        """Models Ollama reports, or `[]` when it is not answering."""
        if self._fixed is not None:
            lister = getattr(self._fixed["answerer"], "available_models", None)
            return list(lister()) if lister else []
        try:
            return self._base_client().available_models()
        except Exception:                               # noqa: BLE001 - never raises
            return []

    def roles(self, *, refresh: bool = False) -> RoleModels:
        """Which model plays which role (work order 4d)."""
        if self._roles is None or refresh:
            if self._fixed is not None:
                names = {r: str(getattr(m, "model", "") or "") for r, m in self._fixed.items()}
                self._roles = RoleModels(names["router"], names["planner"], names["answerer"])
            else:
                self._roles = resolve_roles(
                    self.installed_models(), configured=self.cfg.ollama_model,
                    router=self._overrides.get("router", self.cfg.router_model),
                    planner=self._overrides.get("planner", self.cfg.planner_model),
                    answerer=self._overrides.get("answerer", self.cfg.answer_model))
        return self._roles

    def suggest_modes(self) -> dict[str, str]:
        """`{"fast": ..., "thoughtful": ...}` for the tab's plain-words control."""
        return suggest_modes(self.installed_models(), self.cfg.ollama_model)

    def set_model(self, role: str, name: str) -> None:
        """Choose the model for a role ("answerer" is what the tab's
        Fast/Thoughtful control writes). Takes effect on the next question."""
        if role not in ("router", "planner", "answerer"):
            raise ValueError(f"unknown role {role!r}")
        if self._fixed is not None:
            setter = getattr(self._fixed[role], "set_model", None)
            if setter:
                setter(name)
            return
        self._overrides[role] = str(name or "").strip()
        self._roles = None

    def set_shelf(self, pinned: Sequence[int] = (), excluded: Sequence[int] = ()) -> None:
        """The shelf's own state, if the tab keeps one: pinned documents are
        always in scope, excluded ones never are. Optional - without it the shelf
        is whatever the conversation's receipts say."""
        self._pinned = {int(i) for i in pinned}
        self._excluded = {int(i) for i in excluded}

    def _role(self, role: str) -> Any:
        if self._fixed is not None:
            return self._fixed.get(role)
        return self._client_for(getattr(self.roles(), role) or self.cfg.ollama_model)

    def available(self) -> tuple[bool, str]:
        """`(ok, why_not)`: is a model reachable for answering? The reason is
        plain words with the fix in them - the tab shows it as it is."""
        llm = self._role("answerer")
        if llm is None:
            return False, ("No AI model is set up, so Chat can only count, find and check what "
                           "is indexed. Install Ollama from ollama.com, then run: "
                           f"ollama pull {self.cfg.ollama_model}")
        try:
            if not llm.health():
                return False, ("Ollama is not running, so Chat cannot write answers. Search still "
                               "works normally. Start it by opening the Ollama app, or run: "
                               "ollama serve")
            if not llm.has_model():
                name = getattr(llm, "model", "") or self.cfg.ollama_model
                return False, (f"Ollama is running, but the model '{name}' is not installed. "
                               f"Install it with: ollama pull {name}")
        except Exception:                               # noqa: BLE001 - a probe never raises
            return False, "Ollama did not answer. Check that it is running, then try again."
        return True, ""

    # ------------------------------------------------------------------ ask

    def ask(self, question: str, history: Sequence[ChatTurn],
            emit: Callable[[Any], None], should_stop: Callable[[], bool]) -> ChatTurn:
        """Answer `question`. **Blocks. Never raises.**"""
        started = time.perf_counter()
        debug: dict[str, Any] = {"timings": {}}
        say_stop = should_stop or (lambda: False)

        def stop() -> bool:
            try:
                return bool(say_stop())
            except Exception:                           # noqa: BLE001
                return False

        def send(event: Any) -> None:
            try:
                emit(event)
            except Exception as exc:                    # noqa: BLE001 - a slot must not kill the answer
                log.debug("emit failed: {}", exc)

        first = {"done": False}

        def say(text: str) -> None:
            if not first["done"]:
                first["done"] = True
                debug["timings"]["first_narration_s"] = round(time.perf_counter() - started, 3)
            send(NarrationEvent(text))

        try:
            turn = self._ask(str(question or ""), list(history or ()), send, say, stop, debug, started)
        except AppErrorException as exc:
            turn = self._error_turn(exc.error.message, exc.error.suggestion, debug)
        except Exception as exc:                        # noqa: BLE001 - the contract: never raises
            log.exception("chat: unexpected failure")
            turn = self._error_turn(
                "Something went wrong while answering, so nothing was shown.",
                "Try asking again. Search is unaffected. If it keeps happening, the log has the details.",
                debug, detail=f"{type(exc).__name__}: {exc}")
        debug["timings"]["total_s"] = round(time.perf_counter() - started, 3)
        turn.debug = {**turn.debug, **{k: v for k, v in debug.items() if k != "timings"},
                      "timings": {**turn.debug.get("timings", {}), **debug["timings"]}}
        return turn

    # ------------------------------------------------------------------ the flow

    def _ask(self, question: str, history: list, send: Callable, say: Callable,
             stop: Callable[[], bool], debug: dict, started: float) -> ChatTurn:
        text = " ".join(question.split())
        if not text:
            return ChatTurn("assistant", "Type a question and I will look through your files.",
                            kind="clarify")

        route = route_question(text, history)                      # rules only: instant
        debug["route"] = route.as_dict()
        eff = route.effective

        if eff == AGGREGATE:
            return self._aggregate(route, say, send, stop, debug)

        plan = make_plan(route.question, self.store, today=self.today)
        debug["plan"] = {"filters": list(plan.filters), "terms": list(plan.terms)}
        say(f"Looking for {plan.summary()}...")

        if route.by == "default":
            router_llm = self._role("router")
            if router_llm is not None and self._reachable(router_llm):
                refined = route_question(text, history, llm=router_llm)
                if refined.by == "model":
                    route = refined
                    debug["route"] = route.as_dict()
                    eff = route.effective
                    if eff == AGGREGATE:
                        return self._aggregate(route, say, send, stop, debug)
        debug["route_explained"] = route.explain()
        if stop():
            return self._stopped(debug)

        shelf = self._shelf_ids(history) if route.kind == "FOLLOWUP" else []
        from app.chat.text import content_tokens

        retrieval = self._retrieve(plan, say, stop, debug, shelf=shelf,
                                   focus=list(dict.fromkeys(content_tokens(text))),
                                   lenient=eff in (LOOKUP, SYNTHESIS))
        debug["queries"] = list(plan.tried)
        debug["rounds"] = retrieval.rounds
        if retrieval.assessment is not None:
            a = retrieval.assessment
            debug["assessment"] = {"files": a.files, "coverage": round(a.coverage, 2),
                                   "thin": a.thin, "reason": a.reason}
        if stop():
            return self._stopped(debug)

        thin = retrieval.assessment is None or retrieval.assessment.thin
        if eff == FIND:
            if not retrieval.results or thin:
                return self._absence_turn(plan, retrieval, debug)
            return self._find_turn(plan, retrieval, send, debug, lead="find")
        if eff == ABSENCE:
            if thin:
                return self._absence_turn(plan, retrieval, debug)
            return self._find_turn(plan, retrieval, send, debug, lead="have")
        if thin:
            return self._absence_turn(plan, retrieval, debug)
        return self._answer_turn(route, plan, retrieval, say, send, stop, debug, started)

    # ------------------------------------------------------------------ aggregate

    def _aggregate(self, route: Route, say: Callable, send: Callable, stop: Callable[[], bool],
                   debug: dict) -> ChatTurn:
        spec = parse_aggregate(route.question, self.store, today=self.today)
        say(f"Counting {spec.summary()}...")
        debug["aggregate"] = {"op": spec.op, "filters": spec.filters, "terms": list(spec.terms)}
        try:
            result = run_aggregate(self.store, spec, limit=self.cfg.result_limit)
        except TimeoutError as exc:
            return self._error_turn(
                f"That count took too long ({exc}), so it was stopped.",
                "Narrow the question - a person, a year or a file type - and ask again.", debug)
        debug["aggregate"]["count"] = result.count
        debug["aggregate"]["sql"] = result.sql
        if stop():
            return self._stopped(debug)

        results: Optional[list] = None
        receipts: list[Receipt] = []
        if result.rows:
            results = self._results_for_rows(result.rows)
            receipts = self._receipts_for(results, [], limit=_FIND_RECEIPTS)
            for receipt in receipts[:_FIND_SHELF]:
                send(ShelfEvent(receipt))
        notes = [f"Counted: {spec.summary()}"]
        if spec.filters:
            notes.append(f"Filters: {spec.filters}")
        notes.extend(result.notes)
        return ChatTurn("assistant", result.text, receipts=receipts, result_set=results,
                        kind="aggregate", notes=notes, debug={"count": result.count})

    def _results_for_rows(self, rows: Sequence[tuple]) -> list:
        """`SearchResult`s for aggregate rows `(file_id, path, mtime_ns, taken_at_ns)`,
        each with its first chunk, so the result delegate can draw them."""
        from app.search.engine import SearchResult

        ids = [r[0] for r in rows]
        marks = ", ".join("?" for _ in ids)
        chunks = {}
        try:
            for row in self.store.conn.execute(
                    f"SELECT c.file_id, c.id, c.text, c.page, c.char_start, c.char_end, c.label "
                    f"FROM chunks c WHERE c.ordinal = 0 AND c.file_id IN ({marks})", ids):
                chunks[int(row[0])] = row
        except Exception as exc:                        # noqa: BLE001 - a listing without text is still a listing
            log.debug("could not load first chunks: {}", exc)
        ext = {}
        try:
            for row in self.store.conn.execute(
                    f"SELECT id, ext FROM files WHERE id IN ({marks})", ids):
                ext[int(row[0])] = str(row[1] or "")
        except Exception:                               # noqa: BLE001
            pass
        out = []
        for rank, (fid, path, mtime_ns, taken_ns) in enumerate(rows, start=1):
            c = chunks.get(fid)
            out.append(SearchResult(
                chunk_id=int(c[1]) if c else 0, file_id=fid, path=path,
                text=str(c[2]) if c else "", score=1.0, rank=rank,
                page=c[3] if c else None, char_start=c[4] if c else None,
                char_end=c[5] if c else None, label=str(c[6] or "") if c else "",
                ext=ext.get(fid, ""), mtime_ns=mtime_ns, taken_at_ns=taken_ns))
        return out

    # ------------------------------------------------------------------ retrieval

    def _search(self, query: str, limit: int) -> Any:
        return self.search_engine.search(query, limit=limit, rerank=False, use_cache=True,
                                         policy=CHAT_POLICY)

    def _shelf_ids(self, history: Sequence[ChatTurn]) -> list[int]:
        """Documents the conversation has touched (its receipts), plus pinned,
        minus excluded - **exactly what the shelf shows**."""
        ids: list[int] = []
        for turn in reversed(list(history)):
            for receipt in getattr(turn, "receipts", None) or []:
                if receipt.file_id is not None and receipt.file_id not in ids:
                    ids.append(int(receipt.file_id))
        for pinned in sorted(self._pinned):
            if pinned not in ids:
                ids.append(pinned)
        return [i for i in ids if i not in self._excluded][:12]

    def _shelf_hits(self, ids: Sequence[int], plan: Plan) -> list:
        wanted = set(plan.stems())
        from app.chat.text import content_tokens

        hits: list[_Hit] = []
        for file_id in ids:
            try:
                record = self.store.get_file_by_id(file_id)
                chunks = self.store.chunks_for_file(file_id)
            except Exception:                           # noqa: BLE001 - one bad file never halts anything
                continue
            if record is None or not chunks:
                continue
            scored = sorted(((len(set(content_tokens(c.text)) & wanted) / (len(wanted) or 1), c)
                             for c in chunks), key=lambda pair: -pair[0])
            score, chunk = scored[0]
            hits.append(_Hit(chunk, record.path, 0, score))
        hits.sort(key=lambda h: -h.score)
        for rank, hit in enumerate(hits, start=1):
            hit.rank = rank
        return hits

    @staticmethod
    def _shelf_answers(hits: Sequence[Any], focus: Sequence[str]) -> bool:
        """Does the shelf hold what the *follow-up itself* asks about?

        The resolved question carries the earlier subject along ("who approved it"
        becomes "...regarding Chris's licence quote"), and that subject is on the
        shelf by construction - so testing the resolved question would always pass.
        `focus` is the follow-up's own words: if none of them is in a shelf
        passage, the shelf does not answer it and the corpus is searched.
        """
        from app.chat.text import content_tokens

        wanted = set(focus)
        if not wanted:
            return True
        for hit in hits:
            have = set(content_tokens(str(getattr(hit, "text", ""))))
            if len(have & wanted) / len(wanted) >= 0.5:
                return True
        return False

    def _retrieve(self, plan: Plan, say: Callable, stop: Callable[[], bool], debug: dict, *,
                  shelf: Sequence[int] = (), focus: Sequence[str] = (),
                  lenient: bool = True) -> _Retrieval:
        """The bounded loop: search, assess, and - only if it was thin - widen.

        `lenient` lets a passage that covers one of the *other* searches tried (the
        planner's synonyms) count as enough. **Only for questions answered from
        passages**, where verification is the backstop. For "do I have..." and
        "find...", a synonym search matching some passage is not evidence that what
        was asked for exists - "passport scan" must not be answered by a folder
        note about "scans" - so there the original question's own words decide.
        """
        out = _Retrieval()
        seen: set[int] = set()

        def add(results: Sequence[Any]) -> None:
            for r in results:
                key = int(getattr(r, "chunk_id", 0) or 0) or id(r)
                fid = getattr(r, "file_id", None)
                if key in seen or (fid is not None and int(fid) in self._excluded):
                    continue
                seen.add(key)
                out.results.append(r)

        if shelf:
            say("Checking the documents we already have open...")
            hits = self._shelf_hits(shelf, plan)
            assessed = assess(hits, plan)
            if hits and not assessed.thin and self._shelf_answers(hits, focus):
                out.results, out.assessment, out.from_shelf = list(hits), assessed, True
                debug["scope"] = "shelf"
                return out
            add(hits)

        queue = [plan.query()]
        planner_used = False
        primary_stems = plan.stems()
        alternatives: list[list[str]] = []
        while queue and out.rounds < self.cfg.max_rounds:
            query = queue.pop(0)
            if not query or query in plan.tried:
                continue
            out.rounds += 1
            plan.tried.append(query)
            say("Searching..." if out.rounds == 1 else f"That was thin - trying: {query}")
            if out.rounds > 1 and plan.filters and "type:" not in query and \
                    not any(f in query for f in plan.filters):
                out.dropped_filters = True
            response = self._search(query, _RETRIEVE_LIMIT)
            if out.rounds == 1:
                out.unmatched = tuple(getattr(response, "unmatched", ()) or ())
            else:
                from app.chat.text import content_tokens as _ct
                alternatives.append(list(dict.fromkeys(_ct(query))))
            add(list(response.results))
            files = len({int(getattr(r, "file_id", 0) or 0) for r in out.results})
            say(f"Searching... found {files} document{'s' if files != 1 else ''}")
            out.assessment = self._assess(out.results, plan, out.unmatched,
                                          alternatives if lenient else [], primary_stems)
            if not out.assessment.thin or stop():
                break
            queue = widen(plan) + queue
            if not queue and not planner_used:
                planner_used = True
                planner = self._role("planner")
                if planner is not None and self._reachable(planner):
                    queue = planner_queries(planner, plan, timeout=min(self.cfg.timeout_s, 30.0))
        if out.assessment is None:
            out.assessment = assess([], plan)
        return out

    @staticmethod
    def _assess(results: list, plan: Plan, unmatched: tuple, alternatives: list,
                primary: list) -> Assessment:
        first = assess(results, plan, unmatched)
        if not first.thin or not alternatives:
            return first
        # A passage that fully covers one of the *other* searches tried (the
        # planner's synonyms, the widened query) is a real candidate.
        from app.chat.text import content_tokens

        best = 0.0
        for result in results:
            have = set(content_tokens(str(getattr(result, "text", ""))))
            for stems in alternatives:
                if stems:
                    best = max(best, len(have & set(stems)) / len(stems))
        if best >= 0.5:
            return Assessment(first.files, max(first.coverage, best), first.unmatched, False,
                              "a widened search found a passage that covers it")
        return first

    def _reachable(self, llm: Any) -> bool:
        try:
            return bool(llm.health())
        except Exception:                               # noqa: BLE001
            return False

    # ------------------------------------------------------------------ FIND / ABSENCE-with-hits

    def _receipts_for(self, results: Sequence[Any], terms: Sequence[str], *, limit: int) -> list[Receipt]:
        """One receipt per document, its quote the best-matching sentence(s)."""
        if not results:
            return []
        sources = build_sources(list(results), terms, max_sources=limit, window_tokens=4096,
                                chunks_per_file=1)
        out: list[Receipt] = []
        for source in sources:
            try:
                piece, start, end = source.best_quote(terms)
                out.append(source.receipt(piece, start, end))
            except Exception:                           # noqa: BLE001 - one bad file never halts anything
                out.append(Receipt(source.file_id, source.path, source.name, "", "", None))
        return out

    def _find_turn(self, plan: Plan, retrieval: _Retrieval, send: Callable, debug: dict, *,
                   lead: str) -> ChatTurn:
        limit = self.cfg.result_limit
        by_file: dict[int, Any] = {}
        for r in retrieval.results:
            by_file.setdefault(int(getattr(r, "file_id", 0) or 0), r)
        results = list(by_file.values())[:limit]
        n = len(results)
        noun = "document" if n == 1 else "documents"
        what = plan.summary()
        full = n >= limit
        if lead == "have":
            text = f"Yes - Leasha found {n} {noun} matching {what}. "
        elif n == 1:
            text = f"Here is the one {noun} matching {what}. "
        else:
            text = f"Here are the {'best ' if full else ''}{n} {noun} matching {what}. "
        text += f"These are the best {n}." if full else (f"That's all {n}." if n != 1 else "That's all.")
        receipts = self._receipts_for(results, plan.terms, limit=_FIND_RECEIPTS)
        for receipt in receipts[:_FIND_SHELF]:
            send(ShelfEvent(receipt))
        return ChatTurn("assistant", text, receipts=receipts, result_set=results, kind="find",
                        notes=[f"Searched for: {q}" for q in plan.tried], debug=debug)

    # ------------------------------------------------------------------ absence

    def _absence_turn(self, plan: Plan, retrieval: _Retrieval, debug: dict, *,
                      unhelpful: bool = False, results: Optional[list] = None) -> ChatTurn:
        text, notes = absence.absence_text(
            plan.summary() or plan.question, plan.tried or [plan.query()], self.store,
            dropped_filters=retrieval.dropped_filters, found_something_unhelpful=unhelpful)
        return ChatTurn("assistant", text, kind="absence", notes=notes,
                        result_set=results if unhelpful else None, debug=debug)

    # ------------------------------------------------------------------ the answer

    def _similarity(self) -> Optional[Callable[[str, str], float]]:
        embedder = getattr(self.search_engine, "embedder", None)
        if embedder is None or not hasattr(embedder, "embed_all"):
            return None

        def similarity(a: str, b: str) -> float:
            try:
                va, vb = list(embedder.embed_all([a, b]))
                dot = sum(float(x) * float(y) for x, y in zip(va, vb))
                na = sum(float(x) ** 2 for x in va) ** 0.5
                nb = sum(float(y) ** 2 for y in vb) ** 0.5
                return dot / (na * nb) if na and nb else 0.0
            except Exception:                           # noqa: BLE001 - a rescue only
                return 0.0
        return similarity

    def _metas(self, results: Sequence[Any]) -> dict[int, dict]:
        try:
            return self.store.messages_for(sorted({int(r.file_id) for r in results}))
        except Exception:                               # noqa: BLE001
            return {}

    def _answer_turn(self, route: Route, plan: Plan, retrieval: _Retrieval, say: Callable,
                     send: Callable, stop: Callable[[], bool], debug: dict,
                     started: float) -> ChatTurn:
        llm = self._role("answerer")
        model_ok = llm is not None and self._reachable(llm)
        try:
            has_model = model_ok and bool(llm.has_model())
        except Exception:                               # noqa: BLE001
            has_model = False
        window = 4096
        if has_model:
            try:
                window = int(llm.context_window())
            except Exception:                           # noqa: BLE001
                window = 4096

        results = sorted(retrieval.results, key=lambda r: getattr(r, "rank", 0))
        sources = build_sources(
            results, plan.terms, max_sources=self.cfg.max_sources, window_tokens=window,
            question=route.question, metas=self._metas(results))
        debug["sources"] = [s.name for s in sources]
        debug["models"] = {r: getattr(self._role(r), "model", "") for r in ("router", "planner", "answerer")}
        if not sources:
            return self._absence_turn(plan, retrieval, debug)

        verifier = Verifier(sources, threshold=self.cfg.verify_threshold,
                            similarity=self._similarity())
        assembler = AnswerAssembler(verifier)
        stream_out = not (route.effective == SYNTHESIS and self.cfg.synthesis_combine and has_model)
        first_token: dict[str, Any] = {}

        def deliver(accepted: Sequence[Accepted]) -> None:
            for item in accepted:
                if stream_out:
                    if "at" not in first_token:
                        first_token["at"] = round(time.perf_counter() - started, 3)
                        debug["timings"]["first_token_s"] = first_token["at"]
                    send(TokenEvent(item.text + " "))
                    for receipt in item.new_receipts:
                        send(ShelfEvent(receipt))

        notes: list[str] = []
        stopped = False
        try:
            if not has_model:
                debug["mode"] = "extractive"
                notes.append(_NO_MODEL_NOTE)
                self._extractive(sources, plan, assembler, deliver)
            elif route.effective == SYNTHESIS:
                debug["mode"] = "synthesis: extract-and-quote" + (" + combine" if not stream_out else "")
                stopped = self._synthesise(route, sources, llm, assembler, deliver, say, stop)
            else:
                debug["mode"] = "answer"
                say("Reading " + self._names(sources) + "...")
                stopped = self._generate(route.question, sources, llm, assembler, deliver, stop,
                                         strict=False)
                if not assembler.accepted and not stopped and not self._said_not_found(assembler):
                    say("Checking that again more carefully...")
                    stopped = self._generate(route.question, sources[:3], llm, assembler, deliver,
                                             stop, strict=True)
        except AppErrorException as exc:
            if not assembler.accepted:
                return self._error_turn(exc.error.message, exc.error.suggestion, debug)
            notes.append(f"The model stopped answering part-way: {exc.error.message}")

        if not stream_out and assembler.accepted:
            assembler = self._combine(route, assembler, sources, llm, debug)
            for item in assembler.accepted:
                send(TokenEvent(item.text + " "))
                for receipt in item.new_receipts:
                    send(ShelfEvent(receipt))

        debug["model_output"] = assembler.raw[:800]
        debug["dropped"] = [{"sentence": s, "reason": r} for s, r in assembler.dropped
                            if not s.upper().startswith(prompts.NOT_FOUND)]
        if assembler.accepted:
            if stopped:
                notes.append("Stopped early - this is only part of the answer.")
            return ChatTurn("assistant", assembler.text(), receipts=assembler.receipts(),
                            kind="answer", notes=notes, debug=debug)
        if stopped:
            return self._stopped(debug)
        # Retrieval was fine and the model produced nothing that could be
        # checked. Say so - and offer the documents - instead of guessing.
        return self._absence_turn(plan, retrieval, debug, unhelpful=True,
                                  results=list({r.file_id: r for r in results}.values())[:10])

    @staticmethod
    def _names(sources: Sequence[Source]) -> str:
        names = [s.name for s in sources[:2]]
        rest = len(sources) - len(names)
        return " and ".join(names) + (f" and {rest} more" if rest > 0 else "")

    @staticmethod
    def _said_not_found(assembler: AnswerAssembler) -> bool:
        return assembler.raw.strip().upper().startswith(prompts.NOT_FOUND)

    def _generate(self, question: str, sources: Sequence[Source], llm: Any,
                  assembler: AnswerAssembler, deliver: Callable, stop: Callable[[], bool], *,
                  strict: bool) -> bool:
        """One streamed generation, judged as it arrives. Returns whether it was stopped."""
        prompt = prompts.answer_prompt(question, sources, strict=strict)
        for piece in llm.stream(prompt, temperature=0.0, max_tokens=ANSWER_MAX_TOKENS,
                                timeout=self.cfg.timeout_s, should_stop=stop):
            deliver(assembler.feed(piece))
            if stop():
                deliver(assembler.flush())
                return True
        deliver(assembler.flush())
        # A stream that ended because `should_stop` was asked and said yes (the
        # model wrapper honours it too) looks the same as one that finished.
        return bool(stop())

    # -- extractive fallback -------------------------------------------------------------

    def _extractive(self, sources: Sequence[Source], plan: Plan, assembler: AnswerAssembler,
                    deliver: Callable) -> None:
        """No model: the best-matching sentences, verbatim, each with its source.
        They go through the same verifier as anything else - verbatim text passes
        it trivially, and that it does is the point."""
        from app.chat.text import content_tokens, snap_span

        wanted = set(plan.stems())
        chosen = 0
        for source in sources:
            piece_index, start, end = source.best_quote(plan.terms)
            text = source.pieces[piece_index].text
            start, end = snap_span(text, start, end, max_chars=240)
            sentence = " ".join(text[start:end].split())
            have = set(content_tokens(sentence))
            if wanted and len(have & wanted) / len(wanted) < 0.34:
                continue
            body = sentence.rstrip(".!? ")
            if not body:
                continue
            deliver(assembler.feed(f"{body} [{source.n}]. "))
            chosen += 1
            if chosen >= 3:
                break
        deliver(assembler.flush())

    # -- synthesis (extract-and-quote, optionally combined) ----------------------------------

    def _synthesise(self, route: Route, sources: Sequence[Source], llm: Any,
                    assembler: AnswerAssembler, deliver: Callable, say: Callable,
                    stop: Callable[[], bool]) -> bool:
        """The map step: each document, alone, asked what it says about the
        question. Its sentences are verified against that document and joined in
        document order - "extract-and-quote", the work order's honest downgrade
        for a small model (its 4b: synthesis has no floor at v1)."""
        for source in sources:
            if stop():
                return True
            say(f"Reading {source.name}...")
            try:
                reply = llm.generate(prompts.extract_prompt(route.question, source),
                                     temperature=0.0, max_tokens=140, timeout=self.cfg.timeout_s)
            except AppErrorException:
                if assembler.accepted:
                    break
                raise
            text = str(getattr(reply, "text", reply) or "")
            if text.strip().upper().startswith(prompts.NOT_FOUND):
                continue
            deliver(assembler.feed(text.strip() + " "))
            deliver(assembler.flush())
        return False

    def _combine(self, route: Route, extracts: AnswerAssembler, sources: Sequence[Source],
                 llm: Any, debug: dict) -> AnswerAssembler:
        """The reduce step (`synthesis_combine` only): the verified extracts,
        renumbered, are combined by the model - and the result is verified again
        against the documents. If nothing survives, the extracts stand."""
        from dataclasses import replace

        order = extracts.source_order()
        renumbered = [replace(next(s for s in sources if s.n == n), n=i)
                      for i, n in enumerate(order, start=1)]
        notes = [(i, a.body) for i, a in enumerate(extracts.accepted, start=1)]
        pairs = []
        for item in extracts.accepted:
            for number in item.display:
                pairs.append((number, item.body))
        try:
            reply = llm.generate(prompts.combine_prompt(route.question, pairs or notes),
                                 temperature=0.0, max_tokens=ANSWER_MAX_TOKENS,
                                 timeout=self.cfg.timeout_s)
            combined = AnswerAssembler(Verifier(renumbered, threshold=self.cfg.verify_threshold))
            combined.feed(str(getattr(reply, "text", reply) or "") + " ")
            combined.flush()
            if combined.accepted:
                debug["combined"] = True
                return combined
        except Exception as exc:                        # noqa: BLE001 - the extracts still stand
            log.debug("combine step failed: {}", exc)
        return extracts

    # ------------------------------------------------------------------ small turns

    def _error_turn(self, message: str, suggestion: str, debug: dict, *, detail: str = "") -> ChatTurn:
        text = f"{message} {suggestion}".strip()
        if detail:
            debug = {**debug, "error_detail": detail}
        return ChatTurn("assistant", text, kind="error", debug=dict(debug))

    def _stopped(self, debug: dict) -> ChatTurn:
        return ChatTurn("assistant", "Stopped.", kind="error", debug=dict(debug))

