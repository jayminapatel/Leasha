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
    context_window()          how many tokens it will actually read

`OllamaClient` has the first three (it is what `QueryTranslator` is handed).
`OllamaLLM` adds the last two around it, without editing the shared client:
streaming is a chat-only need, and the client's own docstring promises it is
"a deliberately small Ollama client".

**Nothing here may raise anything but `AppErrorException`.** A stopped Ollama,
a timeout, a model that has not been pulled - all of it arrives as the
`ERR_OLLAMA_*` error the rest of the application already knows how to say in
plain words. The engine turns that into a `ChatTurn(kind="error")`.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterator, Optional, Protocol

from app.core.errors import AppErrorException, make_error
from app.core.logging import logger

__all__ = [
    "LLM",
    "Completion",
    "OllamaLLM",
    "as_llm",
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
    def context_window(self) -> int: ...


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
        return bool(self.client.warm(**kwargs))

    def generate(self, prompt: str, **kwargs: Any) -> Any:
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
        if not self.client.health():
            raise AppErrorException(self.client.down_error(
                "Ollama is not responding at " + self.client.url))

        payload: dict[str, Any] = {
            "model": self.model, "prompt": prompt, "stream": True,
            "keep_alive": "30m",
            "options": {"temperature": temperature, "num_ctx": self.num_ctx},
        }
        if max_tokens:
            payload["options"]["num_predict"] = int(max_tokens)
        if stop:
            payload["options"]["stop"] = list(stop)

        budget = float(timeout or getattr(self.client, "timeout", 120.0))
        started = time.monotonic()
        closer: Optional[Callable[[], None]] = None
        try:
            if self._stream_transport is not None:
                lines: Any = self._stream_transport(self.client.url + "/api/generate",
                                                    payload, budget)
            else:
                import requests  # noqa: PLC0415

                response = requests.post(
                    self.client.url + "/api/generate", json=payload, stream=True,
                    timeout=(getattr(self.client, "connect_timeout", 3.0), budget))
                response.raise_for_status()
                closer = response.close
                lines = (json.loads(raw) for raw in response.iter_lines() if raw)

            for body in lines:
                if should_stop is not None and should_stop():
                    return
                piece = str(body.get("response", ""))
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
