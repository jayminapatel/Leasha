"""The Chat tab's engine, worker and saving, for one `MainWindow`.

Layer: L5

Work order 202626270611 section 3. `ChatView` only draws; this owns everything
that waits:

* **The engine runs on a worker.** `ChatEngine.ask` blocks for as long as the
  question takes, so it is handed to a `CallableWorker`; what it says on the
  way (`NarrationEvent`, `TokenEvent`, `ShelfEvent`) comes back to the window
  thread through one Qt signal, tagged with which question it belongs to so a
  stopped answer that is still winding down can never write into the next one.
* **Stop is a flag the engine polls**, plus an immediate change on screen: the
  bubble is closed with what had arrived and says it stopped, without waiting
  for the engine to notice.
* **The message actions** - Regenerate, Try again, Edit the last message - trim the
  conversation back to the person's last message and ask again (or put the message
  back in the box); a short **title** is made by the fast model after the first
  answer, on a worker, and the first words of the question stand in until it comes.
* **Sessions are saved on a worker**, one at a time, through `ChatSessions`
  (which asks the store for `save_session` and friends, and keeps the same
  records under a keyed-state entry until it has them).
* **Nothing here touches the store or the network on the window thread.**
  `available()` pings Ollama; building the engine may import a model runtime;
  both are worker bodies (`_probe`, `_load`).

The engine is looked up lazily (`app.chat.engine.ChatEngine`) so a build
without it says so in plain words rather than failing to start.
"""

from __future__ import annotations

import dataclasses
import inspect
import threading
import time
from typing import Any, Callable, Optional

from PyQt6.QtCore import QObject, QThreadPool, QTimer, pyqtSignal

from app.chat.types import ChatTurn, WebAskEvent
from app.core.logging import logger
from app.ui.later import later
from app.ui.chat_sessions import (
    ChatSession, ChatSessions, new_session, session_to_dict,
)
from app.ui.chat_view import ChatView
from app.ui.presenter.chat import (
    FAILED_LINE, plain_answer_text, speed_note, title_from_question,
)
from app.ui.tasks import first_chunk_id
from app.ui.workers import CallableWorker, run

__all__ = ["ChatController", "SPEED_KEY"]

_log = logger.bind(component="ui.chat")

#: Keyed window state: which of Fast / Thoughtful was last chosen.
SPEED_KEY = "ui:chat_speed"

#: How long a stopped answer may take to wind down before the box is handed back anyway.
#: The engine stops between pieces, but a model that is still *loading* sends nothing to stop
#: between (a big model on a CPU takes a minute and more), and a Stop that leaves the person
#: staring at a disabled box for that long is a Stop that does not work.
STOP_GRACE_MS = 4000


class _Bridge(QObject):
    """Carries `(question token, event)` from the worker thread to this one."""

    event = pyqtSignal(object)


class _Ask:
    """One question in flight."""

    def __init__(self, token: int, run_view: Any) -> None:
        self.token = token
        self.run = run_view
        self.stopped = False
        self.finalised = False
        #: Stop was pressed and the worker did not come back in time: the box was handed back
        #: and this worker's late results are ignored.
        self.released = False
        #: The person's answer to "Search the web for ...?", and the wait for it.
        self.decision = threading.Event()
        self.allowed = False


def _machine() -> Any:
    """What the envelope needs to know about this computer (its memory), or `None`.

    Read on the worker that builds the engine, never on the window thread."""
    try:
        import psutil

        from types import SimpleNamespace

        return SimpleNamespace(ram_mb=int(psutil.virtual_memory().total / 1024 ** 2))
    except Exception as exc:                              # noqa: BLE001 - the envelope copes with unknown
        _log.debug("chat: this computer's memory is unknown ({})", exc)
        return None


def supported_kwargs(engine: Any, **wanted: Any) -> dict:
    """Only the keywords `engine.ask` actually accepts.

    `scope` (the shelf) and `style` (Fast / Thoughtful) are things this tab
    hands over when the engine can take them; an engine that cannot simply
    does not receive them, rather than raising on a keyword it never declared.
    """
    try:
        params = inspect.signature(engine.ask).parameters
    except (TypeError, ValueError, AttributeError):
        return {}
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return dict(wanted)
    return {k: v for k, v in wanted.items() if k in params}


class ChatController(QObject):
    def __init__(self, window: Any) -> None:
        super().__init__(window)
        self._w = window
        self.view: Optional[ChatView] = None
        #: Set to a zero-argument callable returning an engine to replace the
        #: real one (the tests do this); otherwise `ChatEngine` is imported.
        self.engine_factory: Optional[Callable[[], Any]] = None
        self.engine: Any = None
        self._sessions = ChatSessions(window._store)
        self.sessions: list[ChatSession] = []
        self.session: ChatSession = new_session()
        self._bridge = _Bridge(self)
        self._bridge.event.connect(self._on_event)
        self._token = 0
        self._ask: Optional[_Ask] = None
        self._opened = False
        self._available: Optional[bool] = None
        self._closing = False
        self._saving = False
        self._pending: dict[str, dict] = {}
        #: Times the same question has been asked again (Regenerate), for a fresh reply.
        self._again = 0
        #: Whether Settings lets Chat use the web at all (the Web chip's presence).
        self._web_allowed = False

    # -- construction ---------------------------------------------------------
    def build(self) -> ChatView:
        view = ChatView()
        self.view = view
        view.error.connect(self._w._show_error)
        view.closing.connect(self.shutdown)
        view.question_submitted.connect(self.ask)
        view.stop_requested.connect(self.stop)
        view.recheck_requested.connect(self.recheck)
        view.speed_changed.connect(self._speed_changed)
        view.regenerate_requested.connect(self.regenerate)
        view.retry_requested.connect(self.retry)
        view.edit_requested.connect(self.edit_last)
        view.web_toggled.connect(self._web_toggled)
        view.web_decided.connect(self._web_decided)
        view.shelf_changed.connect(lambda: self._persist(self.session))
        view.open_requested.connect(lambda path: self._w._open_path(path))
        view.result_opened.connect(lambda row: self._w._open_result(row))
        view.result_revealed.connect(lambda row: self._w._open_result(row, reveal=True))
        view.pin_requested.connect(self._pin)
        view.reindex_requested.connect(lambda row: self._w._reindex_for(row))
        view.similar_requested.connect(self._similar)
        view.sessions.selected.connect(self._select)
        view.sessions.new_requested.connect(self._new)
        view.sessions.renamed.connect(self._rename)
        view.sessions.deleted.connect(self._delete)
        view.shelf.set_shelf(self.session.shelf)
        self._w.rail.currentChanged.connect(self._tab_changed)
        self._web_allowed = bool(getattr(getattr(self._w, "_settings", None),
                                         "chat_web_enabled", False))
        view.set_web(self._web_allowed, self.session.web)
        return view

    def attach_settings(self) -> None:
        """Listen to Settings once that page exists. Settings is built a beat
        after first paint (order 0r 2b), so `build` cannot connect to it."""
        settings_view = getattr(self._w, "settings_view", None)
        if settings_view is not None:
            settings_view.settings_changed.connect(self._settings_changed)
            vision = getattr(settings_view, "vision_model", None)
            grid = getattr(getattr(settings_view, "chat_box", None), "_describe", None)
            if vision is not None and grid is not None:
                # Two controls name the same setting (the photo description model: the
                # text box on the Models page, and the Describe row of the roles grid).
                # Each says what the other was set to, so they never disagree on screen.
                grid.activated.connect(lambda _i, v=vision, g=grid: v.setText(g.value()))
                vision.editingFinished.connect(
                    lambda g=grid, v=vision: g.select(v.text().strip()))
        # Chat follows the Index Tuning mode (work order 3e): its settings are
        # invisible outside Manual and the engine ignores them there. The mode is
        # changed on the Indexing page, whose box does not go through `settings_view`.
        tuning = getattr(getattr(self._w, "indexing_view", None), "tuning", None)
        if tuning is not None:
            tuning.changed.connect(self._tuning_changed)

    def shutdown(self) -> None:
        self._closing = True
        if self._ask is not None:
            self._ask.stopped = True

    # -- the tab coming forward ------------------------------------------------
    def _tab_changed(self, index: int) -> None:
        if self.view is None or index != self._w._tab_index.get(self.view):
            return
        # Not while somebody is arrowing down the rail: the composer taking the
        # focus after the first Down left Reports, Indexing and Settings
        # unreachable by the arrow keys. `shell._tab_changed` has the same guard
        # for the pages it focuses (found by test_ui_redesign_scenarios.py).
        if not self._w.rail.column.hasFocus():
            self.view.focus()
        if not self._opened:
            self._opened = True
            self._load_sessions()
            self._check()
        elif self._available is False:
            self._check()

    def recheck(self) -> None:
        self.engine = None
        self._check()

    def _settings_changed(self, values: dict) -> None:
        if any(str(key).startswith("CHAT_") for key in values):
            self.engine = None        # rebuilt from `.env` on the next question
        if "CHAT_WEB_ENABLED" in values and self.view is not None:
            self._web_allowed = bool(values["CHAT_WEB_ENABLED"])
            self.view.set_web(self._web_allowed, self.session.web)

    def _web_toggled(self, on: bool) -> None:
        """The Web chip: per conversation, remembered with it, off until turned on."""
        self.session.web = bool(on)
        self._persist(self.session)

    def _web_decided(self, allowed: bool) -> None:
        """Allow or Skip on "Search the web for ...?": lets the waiting engine go on."""
        ask = self._ask
        if ask is not None:
            ask.allowed = bool(allowed)
            ask.decision.set()

    def _tuning_changed(self, values: dict) -> None:
        """The Indexing page's tuning mode changed: show or hide Chat's settings, and
        rebuild the engine so the very next question is answered under the new mode."""
        mode = values.get("INDEX_TUNING_MODE") if isinstance(values, dict) else None
        if mode is None:
            return
        box = getattr(getattr(self._w, "settings_view", None), "chat_box", None)
        if box is not None:
            box.set_manual(str(mode).strip().lower() == "manual")
        self.engine = None
        if self._opened:
            self._check()             # the speed note depends on whether a model is forced

    # -- worker bodies -----------------------------------------------------------
    def _make_engine(self) -> Any:
        if self.engine is None:
            factory = self.engine_factory or self._default_engine
            self.engine = factory()
        return self.engine

    def _default_engine(self) -> Any:
        from app.chat.engine import ChatEngine            # absent in older builds

        settings = self._w._settings
        try:
            from app.core.config import load_settings

            settings = load_settings(settings.env_file)
        except Exception as exc:                          # noqa: BLE001
            _log.debug("chat: using the window's settings ({})", exc)
        from app.chat.config import ChatSettings

        return ChatEngine(self._w._engine, self._w._store,
                          settings=ChatSettings.from_settings(settings, profile=_machine()))

    def _probe(self) -> tuple:
        """`(built, available, reason)`. Pings Ollama - never on the window thread."""
        try:
            engine = self._make_engine()
        except ImportError:
            return (False, False, "")
        except Exception as exc:                          # noqa: BLE001
            _log.warning("chat: the engine could not start: {}", exc)
            return (True, False, "")
        try:
            ok, reason = engine.available()
        except Exception as exc:                          # noqa: BLE001
            _log.warning("chat: availability check failed: {}", exc)
            return (True, False, "")
        note = ""
        try:
            chosen = str(getattr(getattr(engine, "cfg", None), "answer_model", "") or "")
            note = speed_note(engine.suggest_modes(), chosen)
        except Exception as exc:                          # noqa: BLE001 - the note is a courtesy
            _log.debug("chat: no speed note ({})", exc)
        return (True, bool(ok), str(reason or ""), note)

    def _load(self) -> tuple:
        """Saved conversations and the remembered Fast / Thoughtful choice."""
        speed = ""
        try:
            speed = self._w._store.get_state(SPEED_KEY, "") or ""
        except Exception as exc:                          # noqa: BLE001
            _log.debug("chat: no remembered speed ({})", exc)
        return (self._sessions.load(), speed)

    # -- availability and loading -------------------------------------------------
    def _check(self) -> None:
        worker = CallableWorker(self._probe, component="ui.chat")
        worker.signals.finished.connect(self._probed)
        worker.signals.failed.connect(lambda _e: self._probed((True, False, "")))
        run(QThreadPool.globalInstance(), worker)

    def _probed(self, result: tuple) -> None:
        built, ok, reason = result[:3]
        self._available = bool(ok)
        if self.view is not None:
            self.view.show_available(ok, reason, built=built)
            self.view.show_speed_note(result[3] if len(result) > 3 else "")

    def _load_sessions(self) -> None:
        worker = CallableWorker(self._load, component="ui.chat")
        worker.signals.finished.connect(self._loaded)
        worker.signals.failed.connect(lambda _e: None)
        run(QThreadPool.globalInstance(), worker)

    def _loaded(self, result: tuple) -> None:
        found, speed = result
        if self.view is None:
            return
        if speed:
            self.view.set_speed(speed)
        if self.session.turns or self.session.shelf.items:
            found = [s for s in found if s.id != self.session.id]
            self.sessions = [self.session] + found        # something was begun already
            self._refresh_list()
            return
        self.sessions = found
        if found:
            self._open_session(found[0])
        else:
            self._refresh_list()

    # -- sessions ------------------------------------------------------------------
    def _refresh_list(self) -> None:
        if self.view is not None:
            active = self.session.id if self.session in self.sessions else ""
            self.view.sessions.set_sessions(self.sessions, active)

    def _open_session(self, session: ChatSession) -> None:
        self.session = session
        self.view.show_turns(session.turns)
        self.view.shelf.set_shelf(session.shelf)
        self.view.set_web(self._web_allowed, session.web)
        self._refresh_list()

    def _new(self) -> None:
        self.session = new_session()
        self.view.show_turns([])
        self.view.shelf.set_shelf(self.session.shelf)
        self.view.set_web(self._web_allowed, False)
        self._refresh_list()
        self.view.focus()

    def _select(self, session_id: str) -> None:
        if session_id == self.session.id or self._ask is not None:
            return
        found = next((s for s in self.sessions if s.id == session_id), None)
        if found is not None:
            self._open_session(found)

    def _rename(self, session_id: str, title: str) -> None:
        found = next((s for s in self.sessions if s.id == session_id), None)
        if found is not None and found.title != title:
            found.title, found.titled = title, True
            self._persist(found)

    def _delete(self, session_id: str) -> None:
        self.sessions = [s for s in self.sessions if s.id != session_id]
        worker = CallableWorker(self._sessions.delete, session_id, component="ui.chat")
        worker.signals.failed.connect(lambda error: _log.warning("chat: {}", error))
        run(QThreadPool.globalInstance(), worker)
        self._pending.pop(session_id, None)
        if session_id == self.session.id:
            self._new()
        else:
            self._refresh_list()

    def _persist(self, session: ChatSession) -> None:
        """Queue a save. One at a time, newest wins, never on the window thread."""
        if not (session.turns or session.shelf.items or session.titled):
            return
        session.updated = time.time()
        if session not in self.sessions:
            self.sessions.insert(0, session)
            self._refresh_list()
        self._pending[session.id] = session_to_dict(session)
        if not self._saving:
            self._flush()

    def _flush(self) -> None:
        if not self._pending:
            return
        session_id = next(iter(self._pending))
        record = self._pending.pop(session_id)
        self._saving = True
        worker = CallableWorker(self._sessions.save, record, component="ui.chat")
        worker.signals.finished.connect(lambda _r: self._saved(None))
        worker.signals.failed.connect(self._saved)
        run(QThreadPool.globalInstance(), worker)

    def _saved(self, error: Any) -> None:
        self._saving = False
        if error is not None:
            _log.warning("chat: a conversation could not be saved: {}", error)
        self._flush()

    # -- the Sources pane's right-click menu ---------------------------------------
    def _pin(self, row: Any) -> None:
        """Keep this source: pinned, so every later question looks at it first."""
        path = str(getattr(row, "path", "") or "")
        if self.view is None or not path:
            return
        self.session.shelf.pin(path, "", getattr(row, "file_id", None))
        self.view.shelf.set_shelf(self.session.shelf)
        self._persist(self.session)

    def _similar(self, row: Any) -> None:
        """Documents like this one, shown on the Search page."""
        chunk_id = int(getattr(row, "chunk_id", 0) or 0)
        if chunk_id > 0:
            self._show_similar(row, chunk_id)
            return
        # A source built from a receipt may carry no passage id of its own.
        worker = CallableWorker(first_chunk_id, self._w._store, str(getattr(row, "path", "")),
                                component="ui.chat")
        worker.signals.finished.connect(lambda found, r=row: self._show_similar(r, int(found or 0)))
        worker.signals.failed.connect(lambda _e: None)
        run(QThreadPool.globalInstance(), worker)

    def _show_similar(self, row: Any, chunk_id: int) -> None:
        if chunk_id <= 0:
            return
        found = dataclasses.replace(row, chunk_id=chunk_id)
        self._w._show(self._w.search_view)
        self._w.search_view.results.similar_requested.emit(found)

    def _speed_changed(self, value: str) -> None:
        self._w._store.set_state(SPEED_KEY, value)

    # -- the message actions -----------------------------------------------------------
    def _rewind_to_last_question(self) -> str:
        """Take the conversation back to just before the person's last message and
        return that message ("" when there is none). What was answered is dropped."""
        turns = self.session.turns
        index = next((i for i in range(len(turns) - 1, -1, -1) if turns[i].role == "user"), -1)
        if index < 0 or self.view is None:
            return ""
        question = turns[index].text
        del turns[index:]
        self.view.show_turns(turns)
        return question

    def regenerate(self) -> None:
        """Ask the last question again and get a fresh answer (a little warmer each time)."""
        if self._ask is not None:
            return
        question = self._rewind_to_last_question()
        if question:
            self._again += 1
            self.ask(question, again=self._again)

    def retry(self) -> None:
        """Ask again after an answer that stopped part-way or failed."""
        if self._ask is not None:
            return
        question = self._rewind_to_last_question()
        if question:
            self.ask(question, again=0)

    def edit_last(self) -> None:
        """The last message comes back into the box to be changed and sent again."""
        if self._ask is not None or self.view is None:
            return
        question = self._rewind_to_last_question()
        if question:
            self._persist(self.session)
            self.view.box.set_text(question)

    # -- asking ---------------------------------------------------------------------
    def ask(self, question: str, *, again: int = 0) -> None:
        view = self.view
        if view is None or self._ask is not None or not question.strip():
            return
        if not again:
            self._again = 0
        session = self.session
        history = [t for t in session.turns if t.kind != "error"]
        session.turns.append(ChatTurn("user", question))
        if not session.titled and len([t for t in session.turns if t.role == "user"]) == 1:
            session.title = title_from_question(question)
        view.add_user(question)
        self._token += 1
        ask = _Ask(self._token, view.begin_answer())
        self._ask = ask
        view.set_busy(True)
        self._persist(session)
        # A snapshot taken here, on the window thread, and handed to the worker: the
        # pinned documents are looked at first, the removed ones never used.
        extra = {"scope": [i.path for i in session.shelf.items if i.pinned],
                 "removed": sorted(session.shelf.removed),
                 "style": str(view.speed.currentData() or "fast"),
                 "variant": again,
                 "web": bool(session.web and self._web_allowed),
                 "web_gate": self._gate(ask)}
        worker = CallableWorker(self._answer, ask, question, history, extra,
                                component="ui.chat")
        worker.signals.finished.connect(lambda turn: self._answered(ask, turn))
        worker.signals.failed.connect(lambda error: self._answer_failed(ask, error))
        run(QThreadPool.globalInstance(), worker)

    def _gate(self, ask: _Ask) -> Callable[[str], bool]:
        """What the engine calls, on its worker, before a web search: shows the exact
        phrase with Allow / Skip and **waits** for the person. Skips on Stop, on the
        window closing, or after five minutes without an answer."""
        def gate(query: str) -> bool:
            ask.decision.clear()
            ask.allowed = False
            self._bridge.event.emit((ask.token, WebAskEvent(query)))
            deadline = time.monotonic() + 300
            while not ask.decision.wait(0.1):
                if ask.stopped or self._closing or time.monotonic() > deadline:
                    return False
            return ask.allowed
        return gate

    def _answer(self, ask: _Ask, question: str, history: list, extra: dict) -> Any:
        """The blocking call. Runs on a worker; everything it says is queued."""
        engine = self._make_engine()
        emit = lambda event: self._bridge.event.emit((ask.token, event))   # noqa: E731
        stop = lambda: ask.stopped or self._closing                          # noqa: E731
        return engine.ask(question, history, emit, stop,
                          **supported_kwargs(engine, **extra))

    def _on_event(self, pair: tuple) -> None:
        token, event = pair
        ask = self._ask
        if ask is not None and ask.token == token and not ask.finalised:
            ask.run.event(event)

    def stop(self) -> None:
        ask = self._ask
        if ask is None or ask.finalised:
            return
        ask.stopped = True
        ask.decision.set()                       # a search waiting on Allow / Skip lets go
        self._close_answer(ask, None, stopped=True)
        if self.view is not None:
            self.view.box.stop_button.setEnabled(False)
            self.view.box.stop_button.setText("Stopping...")
        # `later`, not `QTimer.singleShot`: this waits four seconds, and a person who
        # presses Stop and then closes the window is doing an ordinary thing. A bare
        # single-shot lambda has no owner and would fire into a destroyed controller.
        later(self, STOP_GRACE_MS, lambda a=ask: self._release(a))

    def _release(self, ask: _Ask) -> None:
        """Stop was pressed and the engine has not come back: give the box back now.

        The worker cannot be killed, so it is *orphaned*: its events and its result are
        ignored from here on, and the next question gets a **fresh engine** so nothing it
        still holds (per-question state, a half-read stream) can touch the new answer."""
        if self._ask is not ask or self._closing:
            return
        _log.warning("chat: the stopped answer did not finish in {} ms; handing the box back",
                     STOP_GRACE_MS)
        ask.released = True
        self._ask = None
        self.engine = None
        if self.view is not None:
            self.view.set_busy(False)
            self.view.end_answer()
            self.view.focus()
        self._maybe_title(self.session)

    def _close_answer(self, ask: _Ask, turn: Any, *, stopped: bool = False) -> None:
        ask.finalised = True
        ask.run.finish(turn, stopped=stopped)
        session = self.session
        if turn is not None:
            session.turns.append(turn)
        elif ask.run.bubble.raw.strip():
            # What arrived before Stop. Its source numbers were the model's own and are
            # not checked yet, so they are not kept - the words are.
            session.turns.append(ChatTurn("assistant", plain_answer_text(ask.run.bubble.raw),
                                          kind="chat", notes=["stopped"], partial=True))
        self._persist(session)

    def _answered(self, ask: _Ask, turn: Any) -> None:
        if not ask.finalised:
            if isinstance(turn, ChatTurn):
                self._close_answer(ask, turn)
            else:
                self._close_answer(ask, ChatTurn("assistant", FAILED_LINE, kind="error"))
        self._finished_asking(ask)

    def _answer_failed(self, ask: _Ask, error: Any) -> None:
        _log.warning("chat: the answer failed: {}", error)
        if not ask.finalised:
            self._close_answer(ask, ChatTurn("assistant", FAILED_LINE, kind="error"))
        self._finished_asking(ask)

    def _finished_asking(self, ask: _Ask) -> None:
        if ask.released:
            return                               # the box was handed back already; this is the orphan
        if self._ask is ask:
            self._ask = None
        if self.view is not None:
            self.view.set_busy(False)
            self.view.end_answer()
            self.view.focus()
        self._maybe_title(self.session)

    # -- the conversation's name ------------------------------------------------------
    def _maybe_title(self, session: ChatSession) -> None:
        """After the first exchange, ask the fast model for a short title - once, on a
        worker. The first words of the question stand in until it arrives, and if the
        person renamed the conversation meanwhile theirs wins."""
        if session.titled or session.auto_titled or self.engine is None:
            return
        users = [t for t in session.turns if t.role == "user"]
        last = session.turns[-1] if session.turns else None
        if len(users) != 1 or last is None or last.role != "assistant" or last.kind == "error" \
                or not getattr(self.engine, "title", None):
            return
        session.auto_titled = True                # asked for once, whatever comes back
        worker = CallableWorker(self.engine.title, users[0].text, plain_answer_text(last.text),
                                component="ui.chat")
        worker.signals.finished.connect(lambda title, s=session: self._titled(s, title))
        worker.signals.failed.connect(lambda _e: None)
        run(QThreadPool.globalInstance(), worker)

    def _titled(self, session: ChatSession, title: Any) -> None:
        title = " ".join(str(title or "").split())
        if not title or session.titled:
            self._persist(session)                # keeps `auto_titled`, so it is not asked again
            return
        session.title = title
        self._persist(session)
        self._refresh_list()
