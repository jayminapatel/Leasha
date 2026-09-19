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

import inspect
import time
from typing import Any, Callable, Optional

from PyQt6.QtCore import QObject, QThreadPool, pyqtSignal

from app.chat.types import ChatTurn
from app.core.logging import logger
from app.ui.chat_sessions import (
    ChatSession, ChatSessions, new_session, session_to_dict,
)
from app.ui.chat_view import ChatView
from app.ui.presenter.chat import FAILED_LINE, title_from_question
from app.ui.workers import CallableWorker, run

__all__ = ["ChatController", "SPEED_KEY"]

_log = logger.bind(component="ui.chat")

#: Keyed window state: which of Fast / Thoughtful was last chosen.
SPEED_KEY = "ui:chat_speed"


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
        view.shelf_changed.connect(lambda: self._persist(self.session))
        view.open_requested.connect(lambda path: self._w._open_path(path))
        view.result_opened.connect(lambda row: self._w._open_result(row))
        view.result_revealed.connect(lambda row: self._w._open_result(row, reveal=True))
        view.sessions.selected.connect(self._select)
        view.sessions.new_requested.connect(self._new)
        view.sessions.renamed.connect(self._rename)
        view.sessions.deleted.connect(self._delete)
        view.shelf.set_shelf(self.session.shelf)
        self._w.rail.currentChanged.connect(self._tab_changed)
        return view

    def attach_settings(self) -> None:
        """Listen to Settings once that page exists. Settings is built a beat
        after first paint (order 0r 2b), so `build` cannot connect to it."""
        settings_view = getattr(self._w, "settings_view", None)
        if settings_view is not None:
            settings_view.settings_changed.connect(self._settings_changed)

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
        return ChatEngine(self._w._engine, self._w._store, settings=settings)

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
        return (True, bool(ok), str(reason or ""))

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
        built, ok, reason = result
        self._available = bool(ok)
        if self.view is not None:
            self.view.show_available(ok, reason, built=built)

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
        self._refresh_list()

    def _new(self) -> None:
        self.session = new_session()
        self.view.show_turns([])
        self.view.shelf.set_shelf(self.session.shelf)
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

    def _speed_changed(self, value: str) -> None:
        self._w._store.set_state(SPEED_KEY, value)

    # -- asking ---------------------------------------------------------------------
    def ask(self, question: str) -> None:
        view = self.view
        if view is None or self._ask is not None or not question.strip():
            return
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
        extra = {"scope": session.shelf.paths(),
                 "style": str(view.speed.currentData() or "fast")}
        worker = CallableWorker(self._answer, ask, question, history, extra,
                                component="ui.chat")
        worker.signals.finished.connect(lambda turn: self._answered(ask, turn))
        worker.signals.failed.connect(lambda error: self._answer_failed(ask, error))
        run(QThreadPool.globalInstance(), worker)

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
        self._close_answer(ask, None, stopped=True)

    def _close_answer(self, ask: _Ask, turn: Any, *, stopped: bool = False) -> None:
        ask.finalised = True
        ask.run.finish(turn, stopped=stopped)
        session = self.session
        if turn is not None:
            session.turns.append(turn)
        elif ask.run.bubble.raw.strip():
            session.turns.append(ChatTurn("assistant", ask.run.bubble.raw,
                                          notes=["stopped"]))
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
        if self._ask is ask:
            self._ask = None
        if self.view is not None:
            self.view.set_busy(False)
            self.view.focus()
