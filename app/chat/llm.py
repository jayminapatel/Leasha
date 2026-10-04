"""The one seam between the chat engine and a language model.

Layer: L8b - no Qt. This is the only module under `app/chat/` that talks HTTP,
and it does so through the same `OllamaClient` the rest of the application uses
(`app/llm/ollama.py`), which remains the one place that knows Ollama's URL,
its keep-alive and its error contract.

**What the engine asks of a model** is deliberately small - four things - so a
test double is twenty lines (`app/chat/testing.py`) and so any local model that
can follow an instruction can play any role:

    model                     the name, for messages that must name it
    health() / has_model()    is it reachable, is it installed
    generate(prompt, ...)     one completion -> an object with `.text`
    stream(prompt, ...)       the same, as an iterator of text pieces
    chat_stream(messages, ...) a *conversation* (role-tagged messages) as text pieces
    context_window()          how many tokens it will actually read

`OllamaClient` has the first three (it is what `QueryTranslator` is handed).
`OllamaLLM` adds the rest around it, without editing the shared client:
streaming is a chat-only need, and the client's own docstring promises it is
"a deliberately small Ollama client".

**Conversation goes through `/api/chat`**, not `/api/generate` with a flattened
prompt: the model sees `system` / `user` / `assistant` turns as its own template
expects them, which is what makes "shorter", "and the second one?" and "translate
that" work. `chat_stream` is the one call the Chat tab's conversation uses; the
single-shot `generate`/`stream` stay for the small jobs (routing, planning, titles).

**Nothing here may raise anything but `AppErrorException`.** A stopped Ollama,
a timeout, a model that has not been pulled - all of it arrives as the
`ERR_OLLAMA_*` error the rest of the application already knows how to say in
plain words. The engine turns that into a `ChatTurn(kind="error")`.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterator, Mapping, Optional, Protocol, Sequence

from app.core.errors import ActionType, AppError, AppErrorException, make_error
from app.core.logging import logger

__all__ = [
    "LLM",
    "Completion",
    "OllamaLLM",
    "as_llm",
    "probe_installed",
    "DEFAULT_WINDOW",
    "OLLAMA_DEFAULT_CONTEXT",
]

log = logger.bind(component="chat.llm")

#: What Ollama gives a model when nobody asks for more: **4096 tokens**, whatever
#: the model's own training window is. A model advertising 128k still reads 4k
#: unless `num_ctx` is raised - and raising it makes Ollama reload the model with
#: a bigger cache, which on a CPU machine is the difference between a first token
#: in seconds and one in a minute. So the budget is worked out from the *smaller*
#: of the two numbers, and the request states it explicitly.
OLLAMA_DEFAULT_CONTEXT = 4096

#: When nothing can be asked: a conservative window every current model has.
DEFAULT_WINDOW = 2048


@dataclass(frozen=True)
class Completion:
    """What `generate` returns - just enough, and compatible with `OllamaResponse`."""

    text: str
    model: str = ""
    elapsed_s: float = 0.0


class LLM(Protocol):
    """The four things the engine asks of a model. See the module docstring."""

    model: str

    def health(self, *, force: bool = ...) -> bool: ...
    def has_model(self) -> bool: ...
    def generate(self, prompt: str, *, json_mode: bool = ..., temperature: float = ...,
                 timeout: Optional[float] = ..., max_tokens: Optional[int] = ...,
                 stop: Optional[list[str]] = ...) -> Any: ...
    def stream(self, prompt: str, *, temperature: float = ..., timeout: Optional[float] = ...,
               max_tokens: Optional[int] = ..., stop: Optional[list[str]] = ...,
               should_stop: Optional[Callable[[], bool]] = ...) -> Iterator[str]: ...
    def chat_stream(self, messages: Sequence[Mapping[str, str]], *, temperature: float = ...,
                    timeout: Optional[float] = ..., max_tokens: Optional[int] = ...,
                    stop: Optional[list[str]] = ..., should_stop: Optional[Callable[[], bool]] = ...,
                    think: Optional[str] = ...) -> Iterator[str]: ...
    def context_window(self) -> int: ...


def _takes(method: Any, keyword: str) -> bool:
    """Whether `method` accepts `keyword` (a test double's `generate` may not)."""
    import inspect  # noqa: PLC0415

    try:
        params = inspect.signature(method).parameters
    except (TypeError, ValueError):
        return False
    return keyword in params or any(p.kind is inspect.Parameter.VAR_KEYWORD
                                    for p in params.values())


class OllamaLLM:
    """`OllamaClient`, plus streaming and the model's real context window.

    Wraps rather than subclasses: the client is shared with the translator and
    the graph enricher, and its `set_model` / health cache must stay one object.
    """

    def __init__(
        self,
        client: Any,
        *,
        num_ctx: int = OLLAMA_DEFAULT_CONTEXT,
        stream_transport: Optional[Callable[..., Iterator[dict]]] = None,
        show_transport: Optional[Callable[..., dict]] = None,
    ) -> None:
        self.client = client
        self.num_ctx = int(num_ctx)
        self._stream_transport = stream_transport
        self._show_transport = show_transport
        self._window_cache: dict[str, int] = {}
        self._think_cache: dict[str, bool] = {}

    # -- delegated -----------------------------------------------------------

    @property
    def model(self) -> str:
        return str(getattr(self.client, "model", ""))

    def set_model(self, name: str) -> None:
        self.client.set_model(name)

    def health(self, *, force: bool = False) -> bool:
        return bool(self.client.health(force=force))

    def has_model(self) -> bool:
        return bool(self.client.has_model())

    def available_models(self) -> list[str]:
        return list(self.client.available_models())

    def warm(self, **kwargs: Any) -> bool:
        if _takes(self.client.warm, "num_ctx"):
            kwargs.setdefault("num_ctx", self.num_ctx)
        return bool(self.client.warm(**kwargs))

    def generate(self, prompt: str, **kwargs: Any) -> Any:
        # 2026-10-04: with the window `chat_stream` uses. Without it the router's and
        # the planner's calls ran at Ollama's default and the answer at `num_ctx`, and
        # Ollama reloads a model every time the window changes (3.9-4.9 s each,
        # measured on qwen2.5:1.5b) - twice a question.
        if _takes(self.client.generate, "num_ctx"):
            kwargs.setdefault("num_ctx", self.num_ctx)
        return self.client.generate(prompt, **kwargs)

    # -- the window ----------------------------------------------------------

    def context_window(self) -> int:
        """Tokens this model will actually read: the smaller of what it was
        trained for (asked of Ollama's `/api/show`) and what Ollama will give it
        (`num_ctx`). Cached per model; never raises."""
        name = self.model
        if name in self._window_cache:
            return self._window_cache[name]
        native = 0
        try:
            body = self._show(name)
            info = body.get("model_info") or {}
            for key, value in info.items():
                if str(key).endswith(".context_length"):
                    native = int(value)
                    break
        except Exception as exc:                        # noqa: BLE001 - never raises
            log.debug("could not read the context window of {}: {}", name, exc)
        window = min(native, self.num_ctx) if native > 0 else min(DEFAULT_WINDOW * 2, self.num_ctx)
        self._window_cache[name] = window
        return window

    def _show(self, name: str) -> dict:
        if self._show_transport is not None:
            return self._show_transport(name)
        import requests  # noqa: PLC0415 - lazy: importing this module stays free

        response = requests.post(
            self.client.url + "/api/show", json={"model": name},
            timeout=(getattr(self.client, "connect_timeout", 3.0), 10.0))
        response.raise_for_status()
        return response.json()

    # -- streaming -----------------------------------------------------------

    def stream(
        self,
        prompt: str,
        *,
        temperature: float = 0.0,
        timeout: Optional[float] = None,
        max_tokens: Optional[int] = None,
        stop: Optional[list[str]] = None,
        should_stop: Optional[Callable[[], bool]] = None,
    ) -> Iterator[str]:
        """The completion as it is produced, a piece of text at a time.

        `should_stop` is asked between pieces; when it says yes the connection is
        closed (which is what tells Ollama to stop generating - a Stop button
        that only hides the rest of the text leaves the CPU busy for a minute).
        """
        payload: dict[str, Any] = {
            "model": self.model, "prompt": prompt, "stream": True,
            "keep_alive": "30m",
            "options": self._options(temperature, max_tokens, stop),
        }
        return self._post_stream("/api/generate", payload, timeout, should_stop,
                                 lambda body: str(body.get("response", "")))

    def chat_stream(
        self,
        messages: Sequence[Mapping[str, str]],
        *,
        temperature: float = 0.4,
        timeout: Optional[float] = None,
        max_tokens: Optional[int] = None,
        stop: Optional[list[str]] = None,
        should_stop: Optional[Callable[[], bool]] = None,
        think: Optional[str] = None,
    ) -> Iterator[str]:
        """A conversation's next reply as it is produced (`/api/chat`).

        `messages` are `{"role": "system"|"user"|"assistant", "content": ...}`.
        `think` is `"off"`, `"low"`, `"medium"`, `"high"` or `None` (leave the
        model's own default): it is sent **only to a model that says it can think**
        (`/api/show` capabilities), because Ollama answers a model that cannot with an
        error. A thinking model's reasoning is never yielded - only the reply is - but
        it counts as progress for `should_stop`, so Stop works during the long silent
        stretch before the first word.
        """
        payload: dict[str, Any] = {
            "model": self.model, "messages": [dict(m) for m in messages], "stream": True,
            "keep_alive": "30m",
            "options": self._options(temperature, max_tokens, stop),
        }
        value = self._think_value(think)
        if value is not None:
            payload["think"] = value
        return self._post_stream(
            "/api/chat", payload, timeout, should_stop,
            lambda body: str((body.get("message") or {}).get("content", "")))

    def chat(self, messages: Sequence[Mapping[str, str]], **kwargs: Any) -> Completion:
        """`chat_stream`, collected. For the small jobs that want a whole reply."""
        started = time.monotonic()
        text = "".join(self.chat_stream(messages, **kwargs))
        return Completion(text=text, model=self.model, elapsed_s=time.monotonic() - started)

    def _options(self, temperature: float, max_tokens: Optional[int],
                 stop: Optional[list[str]]) -> dict[str, Any]:
        options: dict[str, Any] = {"temperature": temperature, "num_ctx": self.num_ctx}
        if max_tokens:
            options["num_predict"] = int(max_tokens)
        if stop:
            options["stop"] = list(stop)
        return options

    # -- thinking ---------------------------------------------------------------

    def can_think(self) -> bool:
        """Does this model reason before it answers? Cached; never raises."""
        name = self.model
        if name not in self._think_cache:
            found = False
            try:
                found = "thinking" in [str(c).lower()
                                       for c in (self._show(name).get("capabilities") or [])]
            except Exception as exc:                    # noqa: BLE001 - never raises
                log.debug("could not read the capabilities of {}: {}", name, exc)
            self._think_cache[name] = found
        return self._think_cache[name]

    def _think_value(self, mode: Optional[str]) -> Any:
        """What to send as `think`, or `None` to send nothing.

        The gpt-oss family takes a level and cannot be switched off (`"off"` becomes
        its lowest); every other thinking model takes a boolean."""
        if mode is None or not self.can_think():
            return None
        mode = str(mode).lower()
        if "gpt-oss" in self.model.lower():
            return mode if mode in ("low", "medium", "high") else "low"
        return mode in ("low", "medium", "high")

    # -- the one place that talks HTTP --------------------------------------------

    def _post_stream(self, path: str, payload: dict, timeout: Optional[float],
                     should_stop: Optional[Callable[[], bool]],
                     pick: Callable[[dict], str]) -> Iterator[str]:
        if not self.client.health():
            raise AppErrorException(self.client.down_error(
                "Ollama is not responding at " + self.client.url))

        budget = float(timeout or getattr(self.client, "timeout", 120.0))
        started = time.monotonic()
        closer: Optional[Callable[[], None]] = None
        try:
            if self._stream_transport is not None:
                lines: Any = self._stream_transport(self.client.url + path, payload, budget)
            else:
                import requests  # noqa: PLC0415

                response = requests.post(
                    self.client.url + path, json=payload, stream=True,
                    timeout=(getattr(self.client, "connect_timeout", 3.0), budget))
                if response.status_code >= 400:
                    raise _HttpFailure(response.status_code, _error_text(response))
                closer = response.close
                lines = (json.loads(raw) for raw in response.iter_lines() if raw)

            for body in lines:
                if should_stop is not None and should_stop():
                    return
                if body.get("error"):
                    raise _HttpFailure(500, str(body["error"]))
                piece = pick(body)
                if piece:
                    yield piece
                if body.get("done"):
                    return
                if time.monotonic() - started > budget * 4:
                    return
        except AppErrorException:
            raise
        except GeneratorExit:
            raise
        except _HttpFailure as exc:
            raise self._failure(exc) from exc
        except Exception as exc:                        # noqa: BLE001 - one shape out
            name = type(exc).__name__
            if "Timeout" in name:
                raise AppErrorException(make_error(
                    "ERR_OLLAMA_TIMEOUT", "chat.llm", timeout_s=f"{budget:g}",
                    details=f"{self.model} stopped answering mid-reply.")) from exc
            self.client._healthy_until = 0.0
            raise AppErrorException(self.client.down_error(str(exc))) from exc
        finally:
            if closer is not None:
                try:
                    closer()
                except Exception:                       # noqa: BLE001
                    pass

    def _failure(self, exc: "_HttpFailure") -> AppErrorException:
        """A reply Ollama refused, in the application's own error shape."""
        text = str(exc.text or "")
        if exc.status == 404 or "not found" in text.lower():
            return AppErrorException(AppError(
                code="ERR_OLLAMA_MODEL_MISSING", component="chat.llm",
                message=f"The model '{self.model}' is not installed in Ollama.",
                suggestion=f"Install it with: ollama pull {self.model}",
                details=text, action_type=ActionType.AUTO_FIX,
                action_payload=f"ollama pull {self.model}"))
        return AppErrorException(self.client.down_error(text or f"HTTP {exc.status}"))


class _HttpFailure(Exception):
    """Ollama answered, and the answer was an error (a status code or an `error` line)."""

    def __init__(self, status: int, text: str) -> None:
        super().__init__(f"{status}: {text}")
        self.status = status
        self.text = text


def _error_text(response: Any) -> str:
    try:
        return str((response.json() or {}).get("error", "") or response.text)
    except Exception:                                   # noqa: BLE001
        return str(getattr(response, "text", "") or "")


def as_llm(candidate: Any) -> Any:
    """Whatever the caller has, as something with all of `LLM`.

    A test double that already has `stream` and `context_window` is used as it
    is; a bare `OllamaClient` is wrapped. `None` stays `None` - "no model" is a
    normal state the engine answers in its own way.
    """
    if candidate is None:
        return None
    if hasattr(candidate, "stream") and hasattr(candidate, "context_window"):
        return candidate
    return OllamaLLM(candidate)


def probe_installed(url: str, *, timeout: float = 5.0,
                    transport: Optional[Callable[..., dict]] = None) -> Any:
    """What Ollama has installed, and which of it can read pictures.

    For the roles grid (work order 4d): the dropdowns list *installed* models, the
    Describe role only vision-capable ones, and the memory line needs file sizes.
    Blocks on the network, so callers run it on a worker. **Never raises**: an
    Ollama that is not answering gives `InstalledModels(reachable=False)`.

    `transport(method, path, payload) -> dict` is for tests. A model's
    `capabilities` (Ollama >= 0.6) are used when present; the name is the fallback
    for older builds.
    """
    from app.chat.roles import InstalledModels, is_vision_name
    from app.llm.models import is_embedding_model

    base = str(url or "http://127.0.0.1:11434").rstrip("/")

    def call(method: str, path: str, payload: Optional[dict] = None) -> dict:
        if transport is not None:
            return transport(method, path, payload)
        import requests  # noqa: PLC0415 - lazy: importing this module stays free

        response = requests.request(method, base + path, json=payload, timeout=(3.0, timeout))
        response.raise_for_status()
        return response.json()

    ram_mb = 0
    try:
        import psutil  # noqa: PLC0415 - optional

        ram_mb = int(psutil.virtual_memory().total / 1024 ** 2)
    except Exception:                                   # noqa: BLE001
        pass
    try:
        tags = call("GET", "/api/tags")
    except Exception as exc:                            # noqa: BLE001 - the probe never raises
        log.debug("could not list the installed models: {}", exc)
        return InstalledModels(reachable=False, ram_mb=ram_mb)

    names: list[str] = []
    sizes: dict[str, int] = {}
    for entry in tags.get("models", []) or []:
        name = str(entry.get("name", "") or "").strip()
        if not name or is_embedding_model(name):
            continue
        names.append(name)
        try:
            sizes[name] = int(entry.get("size", 0) or 0)
        except (TypeError, ValueError):
            sizes[name] = 0
    vision: list[str] = []
    for name in names:
        try:
            capabilities = call("POST", "/api/show", {"model": name}).get("capabilities")
        except Exception as exc:                        # noqa: BLE001 - one model never stops the list
            log.debug("could not read the capabilities of {}: {}", name, exc)
            capabilities = None
        if capabilities is None:
            if is_vision_name(name):
                vision.append(name)
        elif "vision" in [str(c).lower() for c in capabilities]:
            vision.append(name)
    return InstalledModels(reachable=True, names=tuple(names), vision=tuple(vision),
                           sizes=sizes, ram_mb=ram_mb)
