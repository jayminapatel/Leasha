"""The chat engine: an assistant you talk to, that reads your files before it answers.

Layer: L8b - no Qt (the tab runs `ask` on a worker and forwards `emit` by signal).
Work order `202626270611-chat-tab`, sections 1-2, as changed by the owner on
2026-09-20 ("the chat has to behave like i am talking to ai chat like in claude",
"the chat should use local source though").

    greeting / thanks / "shorter"  ->  conversation (no retrieval)
    anything else                  ->  route -> plan -> search (<=3 rounds) -> assess -> answer

**The design in one paragraph.** The person's own files come first. The *router*
decides whether the turn is social or an instruction about the last answer (CHAT: the
model answers from the conversation, streamed), a database question (counting:
computed, never generated), a search (FIND: the answer is the results themselves), an
existence question (ABSENCE: what was searched, honestly scoped) or a question that
files answer (LOOKUP/SYNTHESIS). For that last kind the model writes flowing prose
from the numbered passages, **streamed as it is written** with the conversation
behind it (`memory.py`), and when it is complete `reconcile.py` judges every sentence
against the passages: a claim about the files that no passage supports is taken out
(never the whole answer), what is left is renumbered, and if nothing survives the
turn is the plain "I couldn't find that in your files" - followed, for a question
that is not about the person's own affairs, by a short answer labelled as general
knowledge. **The guarantee moved, honestly**: from "no unverified word is ever on
screen" to "no unverified claim about the files is in the answer that is kept".

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

import re
import time
from dataclasses import dataclass
from datetime import date
from typing import Any, Callable, Optional, Sequence

from app.chat import absence, memory, prompts
from app.chat.aggregate import parse_aggregate, run_aggregate
from app.chat.config import ChatSettings
from app.chat.context import Piece, Source, build_sources
from app.chat.llm import OllamaLLM, as_llm
from app.chat.plan import Assessment, Plan, assess, make_plan, planner_queries, widen
from app.chat.roles import RoleModels, resolve_roles, suggest_modes
from app.chat.reconcile import reconcile
from app.chat.router import (
    ABSENCE, AGGREGATE, ARCHIVE_FIND, CHAT, FIND, FOLLOWUP, LOOKUP, SYNTHESIS, Route,
    route_question,
)
from app.chat.types import (
    ChatTurn, NarrationEvent, Receipt, ShelfEvent, SourcesEvent, TokenEvent,
)
from app.chat.verify import Accepted, AnswerAssembler, Verifier
from app.chat.webrule import WebNeed, decide as decide_web
from app.core.errors import AppErrorException, make_error
from app.core.logging import logger
from app.search.policy import SearchPolicy

__all__ = ["ChatEngine", "CHAT_POLICY", "ANSWER_MAX_TOKENS", "CHAT_MAX_TOKENS",
           "NOT_IN_FILES_LEAD"]

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

#: Tokens an answer from the files may run to. It is prose, not a quotation, so it
#: needs room - but it is checked sentence by sentence, and a model that has stopped
#: answering and started talking only adds sentences that are then dropped.
ANSWER_MAX_TOKENS = 700

#: Tokens a plain conversational reply may run to ("continue" asks for more).
CHAT_MAX_TOKENS = 1200

#: The short general answer offered when the files have nothing.
GENERAL_MAX_TOKENS = 320

#: The longest one reply may take, start to finish, whatever keeps arriving. Every read from
#: Ollama already has its own timeout (a model that goes silent is cut off after
#: `ChatSettings.timeout_s`), but a model that trickles a word a minute never trips that: this is
#: the wall clock over the whole reply. Generous - a big model on a CPU writes a long answer at
#: a few words a second - and it ends the reply *with what had arrived*, like any other failure.
REPLY_DEADLINE_S = 900.0

#: What the engine says - in its own words, not the model's - when the files have
#: nothing on a general question. The label after it is what keeps the general answer
#: visibly apart from anything the files said.
NOT_IN_FILES_LEAD = "I couldn't find that in your files.\n\n**Not from your files:** "

#: Appended when some sentences of an answer were taken out because no passage
#: supports them - the plain "here is what I could not confirm".
_PARTIAL_LINE = "I could not confirm the rest of that from your files."

#: A question about the person's own affairs. A general answer to one of these would
#: be a guess about their life, so the files-only account is given instead.
_PERSONAL = re.compile(r"\b(?:my|our|mine|ours|we|we've|we'd|i|i've|i'd|i'm|me|us)\b", re.I)

#: The verbs of correspondence. With `COLLECTION_NOUNS` they mark a question about
#: the archive itself even when it has no pronoun in it.
_CORRESPONDENCE = re.compile(r"\b(?:sent|received|forwarded|attached|replied|wrote)\b", re.I)


def _about_the_archive(text: str) -> bool:
    """Whether a question asks about the person's own mail or files.

    **1 October 2026.** "mail about holiday from maya" has no pronoun, so
    `_PERSONAL` let it through to the general fill, which answered "Maya has
    sent you a holiday email in May" under *Not from your files* - a sentence
    about their own mail that no source supports. A question naming mail,
    files or attachments is about the archive whether or not it says "my".

    A noun alone is not enough: "what is a PST file?" is general knowledge. A
    noun followed by what it is about or who it is from is the archive.
    """
    return bool(ARCHIVE_FIND.search(text or "") or _CORRESPONDENCE.search(text or ""))

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

#: Said beside a quoted answer the model could not write itself (1 October 2026).
_QUOTED_NOTE = ("The AI model could not write an answer from your files, so these are the "
                "passages that best match your question, quoted exactly.")


@dataclass
class _Streamed:
    """What one streamed conversation call produced."""

    text: str = ""
    #: The model answered NOT FOUND (only watched for in an answer from the files).
    not_found: bool = False
    stopped: bool = False
    #: The `AppError` that ended it early, if one did. `text` is whatever arrived first.
    error: Any = None


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
        #: What the tab handed the *current* question (`ask(scope=, removed=)`):
        #: the documents pinned on its shelf and the ones taken off it. Reset at
        #: the start of every question, so an old answer's shelf never leaks
        #: into the next one - unlike `set_shelf`, which is standing state.
        self._scope: set[int] = set()
        self._removed: set[int] = set()
        #: "fast" or "thoughtful" for the current question, and how many times this
        #: same question has been asked again (Regenerate) - both set by `ask`.
        self._style = "fast"
        self._variant = 0
        #: The Web switch for the current question, the tab's gate for "Allow this
        #: search?", and - for tests - the transport every web call goes through.
        self._web_on = False
        self._web_gate: Optional[Callable[[str], bool]] = None
        self.web_transport: Optional[Callable] = None

    # ------------------------------------------------------------------ models

    def _base_client(self) -> OllamaLLM:
        return self._client_for(self.cfg.ollama_model)

    def _client_for(self, name: str) -> Any:
        if self.cfg.engine == "onnx":
            # One model inside Leasha serves every role; the name is Ollama's
            # and means nothing to it (2026-09-29). Shared with Interpret.
            from types import SimpleNamespace

            from app.llm.engines import text_model

            return text_model(SimpleNamespace(
                chat_engine="onnx", model_cache=self.cfg.model_cache or None,
                embed_device=self.cfg.device), timeout=self.cfg.timeout_s,
                onnx_model=self.cfg.onnx_model)      # 2026-10-04: the tab's pick
        if name not in self._clients:
            from app.llm.ollama import OllamaClient

            # 2026-10-04, code review: the window on the client as well, so whatever
            # reaches it unwrapped sends the same one (`OllamaClient.num_ctx`).
            self._clients[name] = OllamaLLM(
                OllamaClient(self.cfg.ollama_url, name, timeout=self.cfg.timeout_s,
                             num_ctx=self.cfg.context_tokens),
                num_ctx=self.cfg.context_tokens)
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

    def warm(self) -> bool:
        """Load the answering model now, so the first question does not wait for it
        (2026-10-04, the owner: "the chat is really slow"). Blocks - a worker calls it.
        **Never raises**: warming is an optimisation."""
        try:
            llm = self._role("answerer")
            warmer = getattr(llm, "warm", None)
            return bool(warmer()) if warmer is not None and self._reachable(llm) else False
        except Exception as exc:                        # noqa: BLE001
            log.debug("chat: the model was not warmed ({})", exc)
            return False

    def available(self) -> tuple[bool, str]:
        """`(ok, why_not)`: is a model reachable for answering? The reason is
        plain words with the fix in them - the tab shows it as it is."""
        llm = self._role("answerer")
        if getattr(llm, "engine", "") == "onnx":
            # The model inside Leasha: its own sentences - "Ollama is not
            # running" would name a program it does not use (2026-09-29).
            try:
                if not llm.has_model():
                    return False, ("The chat model is not downloaded yet, so Chat can only "
                                   "count, find and check what is indexed. Open Settings, "
                                   "Models, and press Download beside the chat model.")
                if not llm.health():
                    return False, ("The chat model could not be started. Restart Leasha, or "
                                   "switch the chat engine to Ollama in Settings, Models.")
            except Exception:                           # noqa: BLE001 - a probe never raises
                return False, "The chat model could not be checked. Try again."
            return True, ""
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
            emit: Callable[[Any], None], should_stop: Callable[[], bool], *,
            scope: Optional[Sequence[str]] = None, removed: Optional[Sequence[str]] = None,
            style: Optional[str] = None, variant: int = 0, web: bool = False,
            web_gate: Optional[Callable[[str], bool]] = None) -> ChatTurn:
        """Answer `question`. **Blocks. Never raises.**

        The three keywords are what the tab knows and the engine does not, handed
        over per question so nothing is left set behind:

        * `scope` - paths of the documents the person **pinned**. They are looked
          at first for *every* question, not only follow-ups ("Keep" on the shelf
          says "look here first"); when they do not answer it, the corpus is
          searched as usual.
        * `removed` - paths the person took off the shelf. Never used, in any
          round, as a source.
        * `style` - `"fast"` or `"thoughtful"`: which model writes the answer.
          An explicit `CHAT_MODEL` setting wins over it, and `debug["style"]`
          says so, because a control that silently does nothing is worse than
          none.
        * `variant` - how many times this same question has been asked again
          (Regenerate): a little warmer each time, so the reply is a different one.
        * `web` - the conversation's Web switch. **Only with this on, and only when
          Settings allows the web at all, does anything leave this computer**, and then
          only a short search phrase (`app/chat/web.py`). `web_gate(query) -> bool` is
          the tab's "Allow this search?" - it blocks until the person answers; without
          one, an ask-first search is skipped rather than sent.
        """
        started = time.perf_counter()
        debug: dict[str, Any] = {"timings": {}}
        say_stop = should_stop or (lambda: False)
        self._style = style if style in ("fast", "thoughtful") else "fast"
        self._variant = max(0, int(variant or 0))
        debug["variant"] = self._variant
        self._web_on = bool(web) and bool(self.cfg.web_enabled)
        self._web_gate = web_gate
        try:
            self._scope = self._ids_for(scope)
            self._removed = self._ids_for(removed)
            self._apply_style(style, debug)
        except Exception as exc:                        # noqa: BLE001 - the contract: never raises
            log.debug("chat: could not apply the shelf or style: {}", exc)

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

        if eff == CHAT:
            debug["route_explained"] = route.explain()
            return self._chat_turn(text, history, send, say, stop, debug, started, route=route)
        if eff == AGGREGATE:
            return self._aggregate(route, say, send, stop, debug)

        plan = make_plan(route.question, self.store, today=self.today)
        debug["plan"] = {"filters": list(plan.filters), "terms": list(plan.terms)}
        say(f"Looking for {plan.summary()}...")

        if route.by == "default":
            router_llm = self._role("router")
            if router_llm is not None and self._reachable(router_llm):
                refined = route_question(text, history, llm=router_llm, should_stop=stop)
                if refined.by == "model":
                    route = refined
                    debug["route"] = route.as_dict()
                    eff = route.effective
                    if eff == AGGREGATE:
                        return self._aggregate(route, say, send, stop, debug)
        debug["route_explained"] = route.explain()
        if stop():
            return self._stopped(debug)

        shelf = self._shelf_ids(history) if route.kind == "FOLLOWUP" else self._pinned_ids()
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
            # No passage holds half of what was asked, or the index has no document with
            # its words at all: there is nothing to write from. (A *partial* answer - some
            # of the question supported, some not - is made after the model writes, by
            # `reconcile`: the unsupported sentences go, the rest stays, and it says so.)
            return self._nothing_turn(route, plan, retrieval, history, text, say, send, stop,
                                      debug, started)
        return self._answer_turn(route, plan, retrieval, history, text, say, send, stop,
                                 debug, started)

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
        # `rerank=False` on purpose, not an oversight of the Rerank setting
        # (noted 2026-10-04): one chat turn runs up to `max_rounds` of these,
        # widening as it goes, and judges the passages itself (`assess`, then
        # the verifier). A cross-encoder pass per round - measured at 0.46s to
        # 8s per search - would multiply the wait on every round, before the
        # model has written a word. Chat keeps its own policy (`CHAT_POLICY`).
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
        for pinned in sorted(self._pinned | self._scope):
            if pinned not in ids:
                ids.append(pinned)
        blocked = self._excluded | self._removed
        return [i for i in ids if i not in blocked][:12]

    def _pinned_ids(self) -> list[int]:
        """Only the pinned documents - what every question, follow-up or not, looks
        at first. Empty when nothing is pinned, which is the usual case."""
        return sorted((self._pinned | self._scope) - self._excluded - self._removed)[:12]

    def _ids_for(self, paths: Optional[Sequence[str]]) -> set[int]:
        """File ids for paths the tab holds. A path the index no longer knows is
        simply out of scope - one stale entry never fails a question."""
        found: set[int] = set()
        for path in paths or ():
            try:
                record = self.store.get_file(str(path))
            except Exception:                           # noqa: BLE001
                continue
            if record is not None:
                found.add(int(record.id))
        return found

    def _apply_style(self, style: Optional[str], debug: dict) -> None:
        """Fast / Thoughtful -> which model answers, from what is installed."""
        if style not in ("fast", "thoughtful") or self._fixed is not None:
            return
        if self.cfg.answer_model:
            debug["style"] = {"style": style, "ignored": "CHAT_MODEL is set, so it decides"}
            return
        name = self.suggest_modes().get(style)
        if name:
            self.set_model("answerer", name)
            debug["style"] = {"style": style, "model": name}

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
        blocked = self._excluded | self._removed

        def add(results: Sequence[Any]) -> None:
            for r in results:
                key = int(getattr(r, "chunk_id", 0) or 0) or id(r)
                fid = getattr(r, "file_id", None)
                if key in seen or (fid is not None and int(fid) in blocked):
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
                    queue = planner_queries(planner, plan, timeout=min(self.cfg.timeout_s, 30.0),
                                            should_stop=stop)
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

    # ------------------------------------------------------------------ conversation

    def _window_of(self, llm: Any) -> int:
        """Tokens the model will read: its real window, never a guess above it."""
        try:
            return max(1024, int(llm.context_window()))
        except Exception:                               # noqa: BLE001
            return 4096

    def _think_mode(self) -> str:
        """`"off"` for Fast, `"medium"` for Thoughtful: how long a model that reasons
        first may reason. A model that cannot ignores it (`OllamaLLM.chat_stream`)."""
        return "medium" if self._style == "thoughtful" else "off"

    def _stream_chat(self, llm: Any, messages: list, *, stop: Callable[[], bool],
                     send: Callable, max_tokens: int, temperature: float,
                     hold_sentinel: bool = False, debug: dict, started: float,
                     lead: str = "", think: str = "off") -> "_Streamed":
        """One conversation call, streamed to the screen as it is written.

        Returns everything that arrived - text, whether the model said NOT FOUND (only
        when `hold_sentinel`), whether it was stopped, and the error that ended it, if
        one did. **Nothing here raises**: a failure part-way keeps the text so far,
        which is what lets the tab offer Retry beside a partial answer.

        `lead` is text the engine has already put on screen (its own words); it is
        sent first and is not part of `.text`.
        """
        out = _Streamed()
        if lead:
            send(TokenEvent(lead))
        held = ""
        released = not hold_sentinel

        def emit(text: str) -> None:
            if text and "first_token_s" not in debug["timings"]:
                debug["timings"]["first_token_s"] = round(time.perf_counter() - started, 3)
            send(TokenEvent(text))

        deadline = time.monotonic() + REPLY_DEADLINE_S
        try:
            pieces = self._pieces(llm, messages, stop, max_tokens, temperature, think)
            for piece in pieces:
                if time.monotonic() > deadline:
                    # The engine's own code for a model inside Leasha (2026-09-29):
                    # "Ollama did not finish" would name a program it does not use.
                    code = ("ERR_LOCAL_MODEL_TIMEOUT" if getattr(llm, "engine", "") == "onnx"
                            else "ERR_OLLAMA_TIMEOUT")
                    out.error = make_error(
                        code, "chat.engine", timeout_s=f"{REPLY_DEADLINE_S:g}",
                        details="the reply ran past its wall-clock limit")
                    break
                out.text += piece
                if not released:
                    held += piece
                    head = held.lstrip()
                    if len(head) < len(prompts.NOT_FOUND) and \
                            prompts.NOT_FOUND.startswith(head.upper()):
                        continue                        # it could still be the sentinel
                    released = True
                    if head.upper().startswith(prompts.NOT_FOUND):
                        out.not_found = True
                        break
                    emit(held)
                    continue
                emit(piece)
                if stop():
                    break
            if not released and held and not out.not_found:
                emit(held)                              # a reply shorter than the sentinel
        except AppErrorException as exc:
            out.error = exc.error
        out.stopped = bool(stop())
        return out

    @staticmethod
    def _pieces(llm: Any, messages: list, stop: Callable[[], bool], max_tokens: int,
                temperature: float, think: str = "off") -> Any:
        """The reply's pieces: `chat_stream` where the model has it, and - for a
        model object that only knows single prompts - the same conversation
        flattened into one."""
        chat = getattr(llm, "chat_stream", None)
        kwargs = {"temperature": temperature, "max_tokens": max_tokens, "should_stop": stop}
        if chat is not None:
            return chat(messages, think=think, **kwargs)
        flat = "\n\n".join(f"{m['role'].title()}: {m['content']}" for m in messages) + "\n\nAssistant:"
        return llm.stream(flat, **kwargs)

    def _chat_turn(self, text: str, history: list, send: Callable, say: Callable,
                   stop: Callable[[], bool], debug: dict, started: float, *,
                   route: Route) -> ChatTurn:
        """A turn that needs no retrieval: the model answers from the conversation."""
        llm = self._role("answerer")
        if llm is None or not self._reachable(llm):
            return self._unavailable_turn(debug)
        window = self._window_of(llm)
        reserve = min(CHAT_MAX_TOKENS, max(256, window // 3))
        packed = memory.pack(prompts.chat_system(style_note=self.cfg.style_note, today=self.today),
                             history, text, window_tokens=window, reserve_tokens=reserve)
        debug["memory"] = packed.as_dict()
        debug["mode"] = "conversation"
        debug["models"] = {"answerer": getattr(llm, "model", "")}
        say("Thinking...")
        streamed = self._stream_chat(
            llm, packed.messages, stop=stop, send=send, max_tokens=CHAT_MAX_TOKENS,
            temperature=self._temperature(0.7), debug=debug, started=started,
            think=self._think_mode())
        return self._finish_conversation(streamed, kind="chat", llm=llm, debug=debug)

    def _finish_conversation(self, streamed: "_Streamed", *, kind: str, llm: Any, debug: dict,
                             text: Optional[str] = None, notes: Sequence[str] = (),
                             receipts: Optional[list] = None) -> ChatTurn:
        body = (streamed.text if text is None else text).strip()
        model = str(getattr(llm, "model", "") or "")
        notes = list(notes)
        if streamed.error is not None:
            message = f"{streamed.error.message} {streamed.error.suggestion}".strip()
            if not body:
                return self._error_turn(streamed.error.message, streamed.error.suggestion, debug)
            notes.append(message)
            return ChatTurn("assistant", body, receipts=receipts or [], kind=kind, notes=notes,
                            debug=debug, model=model, partial=True)
        if streamed.stopped:
            if not body:
                return self._stopped(debug)
            notes.append("stopped")
            return ChatTurn("assistant", body, receipts=receipts or [], kind=kind, notes=notes,
                            debug=debug, model=model, partial=True)
        if not body:
            return self._error_turn("The model did not write anything back.",
                                    "Ask again. If it keeps happening, try another model in Settings.",
                                    debug)
        return ChatTurn("assistant", body, receipts=receipts or [], kind=kind, notes=notes,
                        debug=debug, model=model)

    def _temperature(self, base: float) -> float:
        """A little warmer each time the same question is asked again (Regenerate),
        so the answer is a different one and not the same words."""
        return min(1.0, base + 0.2 * self._variant)

    def _unavailable_turn(self, debug: dict) -> ChatTurn:
        ok, why = self.available()
        text = why or "No AI model is answering right now. Check that Ollama is running, then try again."
        return ChatTurn("assistant", text, kind="error", debug=dict(debug))

    def title(self, question: str, answer: str = "") -> str:
        """A short name for a conversation, from the small model. `""` when it cannot
        be made - the tab then uses the first words of the question. Never raises."""
        llm = self._role("router") or self._role("answerer")
        if llm is None:
            return ""
        try:
            if not self._reachable(llm):
                return ""
            reply = llm.generate(prompts.title_prompt(question, answer), temperature=0.2,
                                 max_tokens=16, timeout=min(self.cfg.timeout_s, 30.0), stop=["\n"])
            raw = str(getattr(reply, "text", reply) or "").strip().splitlines()[0:1]
            title = re.sub(r"^[\"'`*#\s]+|[\"'`*.\s]+$", "", raw[0] if raw else "")
            words = title.split()
            if not 1 <= len(words) <= 8 or len(title) > 60:
                return ""
            return title
        except Exception as exc:                        # noqa: BLE001 - a courtesy only
            log.debug("chat: no generated title ({})", exc)
            return ""

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

    def _answer_turn(self, route: Route, plan: Plan, retrieval: _Retrieval, history: list,
                     text: str, say: Callable, send: Callable, stop: Callable[[], bool],
                     debug: dict, started: float) -> ChatTurn:
        """An answer built from the person's own files, written as prose and streamed.

        The model writes it once, as it would in any conversation, and it reaches the
        screen as it is written. When it is complete every sentence is judged against
        the passages (`app/chat/reconcile.py`): what a passage does not support is taken
        out, what is left is renumbered, and if nothing at all is supported the turn is
        the plain "I couldn't find that in your files" instead."""
        llm = self._role("answerer")
        model_ok = llm is not None and self._reachable(llm)
        try:
            has_model = model_ok and bool(llm.has_model())
        except Exception:                               # noqa: BLE001
            has_model = False
        window = self._window_of(llm) if has_model else 4096

        results = sorted(retrieval.results, key=lambda r: getattr(r, "rank", 0))
        sources = build_sources(
            results, plan.terms, max_sources=self.cfg.max_sources,
            window_tokens=int(window * 0.55), question=route.question,
            metas=self._metas(results))
        debug["sources"] = [s.name for s in sources]
        debug["models"] = {r: getattr(self._role(r), "model", "") for r in ("router", "planner", "answerer")}
        if not sources:
            return self._nothing_turn(route, plan, retrieval, history, text, say, send, stop,
                                      debug, started)

        if not has_model:
            verifier = Verifier(sources, threshold=self.cfg.verify_threshold,
                                similarity=self._similarity())
            return self._extractive_turn(sources, plan, verifier, debug, send)
        # The web may add what the files do not say - after they have been searched,
        # never instead of them, and never for a question about the person's own affairs.
        assessed = retrieval.assessment
        need = decide_web(text, sources=len(sources), thin=assessed is None or assessed.thin,
                          coverage=None if assessed is None else assessed.coverage)
        web_sources, web_note = self._consult_web(text, sources, say, stop, debug, need)
        if web_note:
            debug["web_note"] = web_note
        verifier = Verifier(list(sources) + web_sources, threshold=self.cfg.verify_threshold,
                            similarity=self._similarity())

        # Every passage the model is shown, as a receipt, before it writes a word: a
        # source number is a live link from the moment it appears.
        shown: list[Receipt] = []
        for source in list(sources) + web_sources:
            try:
                piece, start, end = source.best_quote(plan.terms)
                shown.append(source.receipt(piece, start, end))
            except Exception:                           # noqa: BLE001 - one bad file never halts anything
                shown.append(Receipt(source.file_id, source.path, source.name, "", "", None))
        send(SourcesEvent(tuple(shown)))
        say("Reading " + self._names(sources) + "...")

        system = prompts.archive_system(
            sources, style_note=self.cfg.style_note, today=self.today,
            combine=self.cfg.synthesis_combine or route.effective != SYNTHESIS,
            web=[(s.n, s.name, s.path, s.passage) for s in web_sources])
        if route.kind == FOLLOWUP:
            system += f"\n\nQuestion: {route.question}"     # the follow-up, made standalone
        reserve = min(ANSWER_MAX_TOKENS, max(256, window // 3))
        packed = memory.pack(system, history, text, window_tokens=window, reserve_tokens=reserve)
        debug["memory"] = packed.as_dict()
        debug["mode"] = "answer (conversational, streamed, then checked)"
        streamed = self._stream_chat(
            llm, packed.messages, stop=stop, send=send, max_tokens=ANSWER_MAX_TOKENS,
            temperature=self._temperature(0.2), hold_sentinel=True, debug=debug, started=started,
            think=self._think_mode())

        debug["model_output"] = streamed.text[:800]
        if streamed.not_found or (not streamed.text.strip() and streamed.error is None
                                  and not streamed.stopped):
            quoted = self._quoted_turn(sources, plan, verifier, debug, results)
            if quoted is not None:
                return quoted
            return self._nothing_turn(route, plan, retrieval, history, text, say, send, stop,
                                      debug, started, unhelpful=True, results=results)
        known = " ".join(str(getattr(t, "text", "")) for t in history
                         if getattr(t, "role", "") == "user") + " " + text
        rec = reconcile(streamed.text, verifier, known_text=known)
        debug["dropped"] = [{"sentence": s, "reason": r} for s, r in rec.dropped]
        debug["supported"], debug["general_sentences"] = rec.supported, rec.general
        if not rec.has_support:
            if streamed.error is not None:
                return self._error_turn(streamed.error.message, streamed.error.suggestion, debug)
            if streamed.stopped:
                return self._stopped(debug)
            quoted = self._quoted_turn(sources, plan, verifier, debug, results)
            if quoted is not None:
                return quoted
            return self._nothing_turn(route, plan, retrieval, history, text, say, send, stop,
                                      debug, started, unhelpful=True, results=results)
        body = rec.text
        if rec.partial and not streamed.stopped and streamed.error is None:
            body += "\n\n" + _PARTIAL_LINE
        if web_note and not web_sources:
            body += "\n\n" + web_note                     # the web failed: say so, once
        return self._finish_conversation(streamed, kind="answer", llm=llm, debug=debug,
                                         text=body, receipts=rec.receipts)

    def _extractive_turn(self, sources: Sequence[Source], plan: Plan, verifier: Verifier,
                         debug: dict, send: Callable) -> ChatTurn:
        """No model is running: the best-matching sentences, verbatim, each with its source."""
        assembler = AnswerAssembler(verifier)
        debug["mode"] = "extractive"
        self._extractive(sources, plan, assembler, lambda accepted: [
            send(TokenEvent(item.text + " ")) for item in accepted])
        if not assembler.accepted:
            return self._absence_turn(plan, _Retrieval(), debug, unhelpful=True)
        return ChatTurn("assistant", assembler.text(), receipts=assembler.receipts(),
                        kind="answer", notes=[_NO_MODEL_NOTE], debug=debug)

    def _quoted_turn(self, sources: Sequence[Source], plan: Plan, verifier: Verifier,
                     debug: dict, results: Optional[list] = None) -> Optional[ChatTurn]:
        """The model found nothing to state, but the files may still hold it.

        **1 October 2026.** The chat order's own measurements put most lookup
        misses here: the answer *was* in a passage, a small model said NOT
        FOUND (or every sentence it wrote failed the checks), and the person
        was told "I couldn't find that in your files" - wrong the other way
        round. "mail about holiday from maya" did exactly this with the right
        email among the sources. So the passages that best match the question
        are quoted instead, through `_extractive` - the same verbatim picker,
        with the same bar (a third of the question's words) and the same
        checks, that answers when no model runs. None qualifies: None, and the
        caller says it found nothing, as before.
        """
        assembler = AnswerAssembler(verifier)
        # **A higher bar than the no-model answer's.** First measured on the
        # test corpus: at a third of the question's words, a deposit question
        # was "answered" with a signature line and another with a bare title.
        # Here the model has already read these passages and found nothing, so
        # a quote must share half the question's words and be a sentence.
        self._extractive(sources, plan, assembler, lambda _accepted: None,
                         min_overlap=0.5, min_words=5)
        if not assembler.accepted:
            return None
        debug["mode"] = "quoted: the model found nothing it could state"
        return ChatTurn("assistant", assembler.text(), receipts=assembler.receipts(),
                        kind="answer", notes=[_QUOTED_NOTE], debug=debug,
                        result_set=list({r.file_id: r for r in (results or [])}.values())[:10] or None)

    def _nothing_turn(self, route: Route, plan: Plan, retrieval: _Retrieval, history: list,
                      text: str, say: Callable, send: Callable, stop: Callable[[], bool],
                      debug: dict, started: float, *, unhelpful: bool = False,
                      results: Optional[list] = None) -> ChatTurn:
        """The files have nothing usable. Say so in one plain sentence, and - for a
        question that is not about the person's own affairs, and only then - offer a
        short general answer, labelled as not from the files.

        A question about *their* files ("what did we agree with the landlord") gets the
        searched-and-found-nothing account instead: a general answer to it would be a
        guess about their life."""
        llm = self._role("answerer")
        # Documents that mention it were found, so the question is about them: a
        # general answer would be a guess at what they say. The list is the answer.
        found_some = bool(unhelpful and results)
        offer = (not _PERSONAL.search(text) and not _about_the_archive(text)
                 and not found_some and llm is not None and self._reachable(llm))
        if offer:
            try:
                offer = bool(llm.has_model())
            except Exception:                           # noqa: BLE001
                offer = False
        if not offer:
            return self._absence_turn(plan, retrieval, debug, unhelpful=unhelpful,
                                      results=(list({r.file_id: r for r in results}.values())[:10]
                                               if unhelpful and results else None))
        web_sources, web_note = self._consult_web(
            text, [], say, stop, debug, decide_web(text, sources=0, thin=True))
        if web_sources:
            return self._web_turn(llm, web_sources, history, text, say, send, stop, debug, started)
        lead = NOT_IN_FILES_LEAD if not web_note else NOT_IN_FILES_LEAD.replace(
            "\n\n", f" {web_note}\n\n", 1)
        window = self._window_of(llm)
        packed = memory.pack(prompts.general_system(style_note=self.cfg.style_note, today=self.today),
                             history, text, window_tokens=window,
                             reserve_tokens=min(400, max(200, window // 4)))
        debug["mode"] = "not in the files: short general answer"
        debug["memory"] = packed.as_dict()
        say("Nothing in your files - answering from general knowledge...")
        streamed = self._stream_chat(
            llm, packed.messages, stop=stop, send=send, max_tokens=GENERAL_MAX_TOKENS,
            temperature=self._temperature(0.5), debug=debug, started=started, lead=lead,
            think=self._think_mode())
        notes = [f"Searched for: {q}" for q in plan.tried]
        if not streamed.text.strip() and streamed.error is None and not streamed.stopped:
            return self._absence_turn(plan, retrieval, debug, unhelpful=unhelpful)
        turn = self._finish_conversation(
            streamed, kind="general", llm=llm, debug=debug, notes=notes,
            text=lead + streamed.text.strip())
        if turn.kind == "error":                        # stopped or failed before any word
            turn = ChatTurn("assistant", lead.strip(), kind="general", notes=notes,
                            debug=debug, partial=True)
        return turn

    # ------------------------------------------------------------------ the web (optional)

    def _consult_web(self, text: str, sources: Sequence[Source], say: Callable,
                     stop: Callable[[], bool], debug: dict,
                     need: WebNeed) -> tuple[list[Source], str]:
        """`(web passages as Sources numbered after `sources`, a plain sentence about
        what happened)`. **Empty and silent unless the Web switch is on - and, with it on,
        unless `need` (`app/chat/webrule.py`) says the files came up thin or the person
        asked for the web.** A question the files answered is never offered to the web:
        no prompt, no connection.

        What leaves the computer is one short keyword phrase, composed by the local
        model from the question alone, cleaned of anything that names a file of the
        person's (`app/chat/web.py`), **shown before it is sent**, and - unless the person
        turned that off - sent only after they press Allow. Never a passage, a path, a
        file name or the conversation."""
        if not self._web_on:
            return [], ""
        debug.setdefault("web_need", need.as_dict())
        if not need.wanted or stop() or "web" in debug:      # "web" in debug: already asked once
            return [], ""
        from app.chat import web as webmod

        settings = webmod.WebSettings(
            enabled=True, provider=self.cfg.web_provider, searxng_url=self.cfg.web_searxng_url,
            brave_key=self.cfg.web_brave_key, ask_first=self.cfg.web_ask_first,
            show_query=self.cfg.web_show_query)
        avoid: list[str] = []
        for source in sources:
            avoid += [source.name, source.path, *source.path.replace("\\", "/").split("/")]
            avoid += [piece.text[:160] for piece in source.pieces[:2]]
        llm = self._role("router") or self._role("answerer")
        query = webmod.compose_query(text, llm, avoid=avoid, should_stop=stop)
        if not query:
            return [], "There was nothing safe to search the web for, so I did not."
        debug["web"] = {"query": query, "provider": settings.provider, "asked": settings.ask_first}
        if settings.ask_first:
            say(f'Waiting for your answer: search the web for "{query}"?')
            allowed = False
            try:
                allowed = bool(self._web_gate(query)) if self._web_gate is not None else False
            except Exception:                           # noqa: BLE001 - no answer is a no
                allowed = False
            debug["web"]["allowed"] = allowed
            if not allowed:
                return [], "I did not search the web."
        elif settings.show_query:
            say(f'Searching the web for: "{query}"')
        else:
            say("Searching the web...")
        if stop():
            return [], ""
        result = webmod.run_web(query, settings, transport=self.web_transport, avoid=avoid,
                                should_stop=stop)
        debug["web"].update(error=result.error, hits=len(result.hits), pages=len(result.pages),
                            provider=result.provider or settings.provider)
        if not result.ok:
            return [], result.error or "The web search found nothing."
        first = len(sources) + 1
        found: list[Source] = []
        for offset, (_n, title, url, body) in enumerate(webmod.web_context(result, max_chars=1400)):
            found.append(Source(
                n=first + offset, file_id=None, path=url, name=title,
                pieces=[Piece(None, body, None, "Web")], passage=body, meta=title))
        return found, ""

    def _web_turn(self, llm: Any, web_sources: list[Source], history: list, text: str,
                  say: Callable, send: Callable, stop: Callable[[], bool], debug: dict,
                  started: float) -> ChatTurn:
        """The files had nothing and the web had something: a short answer built from web
        passages, every sentence checked against them, after the plain "I couldn't find
        that in your files."."""
        numbered = [Source(n=i, file_id=None, path=s.path, name=s.name, pieces=s.pieces,
                           passage=s.passage, meta=s.meta)
                    for i, s in enumerate(web_sources, start=1)]
        receipts = [Receipt(None, s.path, s.name, " ".join(s.passage.split())[:240], "Web", None)
                    for s in numbered]
        send(SourcesEvent(tuple(receipts)))
        verifier = Verifier(numbered, threshold=self.cfg.verify_threshold,
                            similarity=self._similarity())
        window = self._window_of(llm)
        packed = memory.pack(
            prompts.web_system(numbered, style_note=self.cfg.style_note, today=self.today),
            history, text, window_tokens=window, reserve_tokens=min(500, max(200, window // 4)))
        debug["mode"] = "not in the files: answered from the web"
        say("Reading what the web says...")
        lead = NOT_IN_FILES_LEAD.split("\n\n")[0] + "\n\n"
        streamed = self._stream_chat(
            llm, packed.messages, stop=stop, send=send, max_tokens=GENERAL_MAX_TOKENS + 200,
            temperature=self._temperature(0.2), hold_sentinel=True, debug=debug, started=started,
            lead=lead, think=self._think_mode())
        rec = reconcile(streamed.text, verifier, known_text=text)
        debug["dropped"] = [{"sentence": s, "reason": r} for s, r in rec.dropped]
        if streamed.not_found or not rec.has_support:
            note = "The web did not have a clear answer either."
            return ChatTurn("assistant", lead.strip() + " " + note, kind="general",
                            debug=debug, model=str(getattr(llm, "model", "") or ""))
        return self._finish_conversation(streamed, kind="answer", llm=llm, debug=debug,
                                         text=lead + rec.text, receipts=rec.receipts)

    @staticmethod
    def _names(sources: Sequence[Source]) -> str:
        names = [s.name for s in sources[:2]]
        rest = len(sources) - len(names)
        return " and ".join(names) + (f" and {rest} more" if rest > 0 else "")

    # -- extractive fallback -------------------------------------------------------------

    def _extractive(self, sources: Sequence[Source], plan: Plan, assembler: AnswerAssembler,
                    deliver: Callable, *, min_overlap: float = 0.34, min_words: int = 0) -> None:
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
            if wanted and len(have & wanted) / len(wanted) < min_overlap:
                continue
            body = sentence.rstrip(".!? ")
            if not body or len(body.split()) < min_words:
                continue
            deliver(assembler.feed(f"{body} [{source.n}]. "))
            chosen += 1
            if chosen >= 3:
                break
        deliver(assembler.flush())

    # ------------------------------------------------------------------ small turns

    def _error_turn(self, message: str, suggestion: str, debug: dict, *, detail: str = "") -> ChatTurn:
        text = f"{message} {suggestion}".strip()
        if detail:
            debug = {**debug, "error_detail": detail}
        return ChatTurn("assistant", text, kind="error", debug=dict(debug))

    def _stopped(self, debug: dict) -> ChatTurn:
        return ChatTurn("assistant", "Stopped.", kind="error", debug=dict(debug))

