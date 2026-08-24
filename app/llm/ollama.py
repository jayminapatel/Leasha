"""A deliberately small Ollama client. Optional everywhere it is used.

Layer: L8 - built early because Layer 6's entity enrichment needs it, and it is
a thin, self-contained seam with no dependency on layers 6 or 7. The same
argument that let `fusion.py` and `query.py` be built ahead of Layer 4 applies:
data in, data out.

**The contract every caller relies on: this never raises anything except
`AppErrorException(ERR_OLLAMA_DOWN)`, and never blocks longer than its timeout.**
Ollama is a separate process a person starts and stops at will, so "not running"
is a normal state rather than an error condition. Everything that uses this must
degrade to working-without-it, and it can only do that if the failure is one
predictable shape.

**Why `requests` and not the `ollama` package.** One dependency the project
already has, for two HTTP calls. The official client adds a package to pin, its
own exception hierarchy to translate, and streaming machinery this does not use.

The transport is injectable, so every path here - healthy, refused, timed out,
truncated JSON, wrong model - is tested with no Ollama installed anywhere.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

from app.core.errors import AppError, AppErrorException, make_error
from app.core.logging import logger

__all__ = ["OllamaClient", "OllamaResponse", "HEALTH_CACHE_S"]

log = logger.bind(component="llm.ollama")

#: How long a health check is trusted. Long enough that a batch job does not
#: re-probe before every call; short enough that starting Ollama is noticed
#: within a few seconds rather than needing a restart of the app.
HEALTH_CACHE_S = 10.0


@dataclass(frozen=True, slots=True)
class OllamaResponse:
    text: str
    model: str
    elapsed_s: float

    def json(self) -> Any:
        """Parse the reply as JSON, or raise ERR_OLLAMA_DOWN with what came back.

        A local model returning prose where JSON was asked for is not a
        transport failure, but it is failure of exactly the same kind from the
        caller's point of view: nothing usable arrived. Folding it into the same
        error keeps every call site to one except clause instead of two.
        """
        try:
            return json.loads(self.text)
        except (ValueError, TypeError) as exc:
            raise AppErrorException(make_error(
                "ERR_OLLAMA_DOWN", "llm.ollama",
                details=f"The model replied with something that is not JSON: {self.text[:200]!r}",
                suggestion=(
                    f"The model '{self.model}' may not follow JSON instructions well. "
                    "Try a different OLLAMA_MODEL, or continue without LLM enrichment - "
                    "the co-occurrence graph does not need it."
                ),
            )) from exc


class OllamaClient:
    """Health check, then generate. Nothing else."""

    def __init__(
        self,
        url: str = "http://127.0.0.1:11434",
        model: str = "mistral",
        *,
        timeout: float = 120.0,
        transport: Optional[Callable[..., Any]] = None,
    ) -> None:
        self.url = url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self._transport = transport
        self._healthy_until = 0.0
        self._last_health = False

    # -- transport seam ------------------------------------------------------

    def _post(self, path: str, payload: dict, timeout: float) -> dict:
        if self._transport is not None:
            return self._transport("POST", self.url + path, payload, timeout)
        import requests  # noqa: PLC0415 - lazy so importing this module is free

        response = requests.post(self.url + path, json=payload, timeout=timeout)
        response.raise_for_status()
        return response.json()

    def _get(self, path: str, timeout: float) -> dict:
        if self._transport is not None:
            return self._transport("GET", self.url + path, None, timeout)
        import requests  # noqa: PLC0415

        response = requests.get(self.url + path, timeout=timeout)
        response.raise_for_status()
        return response.json()

    # -- public --------------------------------------------------------------

    def health(self, *, force: bool = False) -> bool:
        """Is Ollama answering? Cached for `HEALTH_CACHE_S`.

        Never raises. A health check that can throw is not a health check - every
        caller would have to wrap it, and the first one to forget turns "Ollama
        is off" into a crash.
        """
        now = time.monotonic()
        if not force and now < self._healthy_until:
            return self._last_health
        try:
            self._get("/api/tags", timeout=min(self.timeout, 5.0))
            self._last_health = True
        except Exception as exc:  # noqa: BLE001 - see docstring
            log.debug("ollama health check failed: {}", exc)
            self._last_health = False
        self._healthy_until = now + HEALTH_CACHE_S
        return self._last_health

    def available_models(self) -> list[str]:
        try:
            payload = self._get("/api/tags", timeout=min(self.timeout, 5.0))
        except Exception:  # noqa: BLE001
            return []
        return [str(entry.get("name", "")) for entry in payload.get("models", [])]

    def generate(
        self,
        prompt: str,
        *,
        json_mode: bool = False,
        temperature: float = 0.0,
        timeout: Optional[float] = None,
    ) -> OllamaResponse:
        """One completion. Raises `AppErrorException(ERR_OLLAMA_DOWN)` on anything.

        `temperature=0.0` by default because every current caller is doing
        extraction rather than writing. Extraction that varies between runs makes
        a graph that changes when nothing changed, and nothing downstream can
        tell that apart from the corpus having changed.
        """
        if not self.health():
            raise AppErrorException(self.down_error("Ollama is not responding at " + self.url))

        payload: dict[str, Any] = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": temperature},
        }
        if json_mode:
            payload["format"] = "json"

        started = time.monotonic()
        try:
            body = self._post("/api/generate", payload, timeout or self.timeout)
        except AppErrorException:
            raise
        except Exception as exc:  # noqa: BLE001 - deliberately one shape out
            # A failure here also invalidates the cached health, so the next
            # call re-probes instead of trusting a check from before the crash.
            self._healthy_until = 0.0
            raise AppErrorException(self.down_error(str(exc))) from exc

        return OllamaResponse(
            text=str(body.get("response", "")),
            model=self.model,
            elapsed_s=time.monotonic() - started,
        )

    def down_error(self, details: str) -> AppError:
        return make_error(
            "ERR_OLLAMA_DOWN", "llm.ollama",
            details=details,
            action_payload=f"ollama serve  (then: ollama pull {self.model})",
        )
