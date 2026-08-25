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

#: How long to wait for the TCP connection itself, as opposed to the model's
#: reply. These are different questions and were previously answered with one
#: number.
#:
#: A generate call needs a long *read* timeout - a local model on a busy machine
#: genuinely takes a minute to produce a paragraph. It needs no connect timeout
#: at all: Ollama is a process on this machine, so the socket either opens in
#: milliseconds or is not going to open. `requests` applies a single float to
#: both, so the 120-second read budget was also being spent on the connect, and
#: a host that drops packets rather than refusing them burned the whole two
#: minutes before reporting that Ollama was down. A graph enrichment run in the
#: window did exactly that: 120.09 seconds, zero chunks.
CONNECT_TIMEOUT_S = 3.0

#: The cap on the diagnostic's own trial completion. Deliberately short: this is
#: the command somebody runs *because* something is hanging, and a diagnostic
#: that hangs is worse than no diagnostic. A local model that cannot manage one
#: word in this long is a finding in its own right.
PROBE_TIMEOUT_S = 30.0


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
        connect_timeout: float = CONNECT_TIMEOUT_S,
        transport: Optional[Callable[..., Any]] = None,
    ) -> None:
        self.url = url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.connect_timeout = connect_timeout
        self._transport = transport
        self._healthy_until = 0.0
        self._last_health = False

    # -- transport seam ------------------------------------------------------

    def _post(self, path: str, payload: dict, timeout: float) -> dict:
        if self._transport is not None:
            return self._transport("POST", self.url + path, payload, timeout)
        import requests  # noqa: PLC0415 - lazy so importing this module is free

        response = requests.post(
            self.url + path, json=payload, timeout=self._budget(timeout)
        )
        response.raise_for_status()
        return response.json()

    def _get(self, path: str, timeout: float) -> dict:
        if self._transport is not None:
            return self._transport("GET", self.url + path, None, timeout)
        import requests  # noqa: PLC0415

        response = requests.get(self.url + path, timeout=self._budget(timeout))
        response.raise_for_status()
        return response.json()

    def _budget(self, read_timeout: float) -> tuple[float, float]:
        """`(connect, read)` - the pair `requests` accepts, never one number.

        Given a single float, `requests` uses it for both phases. That is the
        difference between "the model is thinking, give it two minutes" and
        "nothing is listening, wait two minutes to find out".
        """
        return (min(self.connect_timeout, read_timeout), read_timeout)

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

    def has_model(self) -> bool:
        """Is the configured model actually installed?

        Ollama answers `/api/tags` as soon as the service is up, whether or not
        any model has been pulled. So "the port is open" and "this will work" are
        different questions, and treating the first as the second is how a run
        gets as far as `generate` before finding out - by which point it is
        holding a connection open with a long read timeout on it.

        Matched on the bare name as well as the full tag, because `mistral` and
        `mistral:latest` are the same model and people write the short form.
        """
        installed = self.available_models()
        wanted = self.model.split(":")[0]
        return any(name == self.model or name.split(":")[0] == wanted for name in installed)

    def diagnose(self) -> dict[str, Any]:
        """Answer "why is Ollama not working" in one call, without raising.

        Four separate questions, reported separately, because each has a
        different fix and a single true/false conflates them:

        1. Is anything listening at the URL?
        2. Which models are installed?
        3. Is the configured one among them?
        4. Does a trivial completion actually come back, and how fast?

        Step 4 is capped hard. This is the command somebody runs *because*
        something is hanging, so it must not hang too.
        """
        report: dict[str, Any] = {
            "url": self.url, "model": self.model,
            "reachable": False, "models": [], "model_installed": False,
            "generated": False, "elapsed_s": None, "error": None,
        }

        report["reachable"] = self.health(force=True)
        if not report["reachable"]:
            report["error"] = f"Nothing answered at {self.url}"
            return report

        report["models"] = self.available_models()
        report["model_installed"] = self.has_model()
        if not report["model_installed"]:
            report["error"] = f"Ollama is running but '{self.model}' is not installed"
            return report

        started = time.monotonic()
        try:
            answer = self.generate("Reply with the single word: ok", timeout=PROBE_TIMEOUT_S)
            report["generated"] = bool(answer.text.strip())
            report["reply"] = answer.text.strip()[:80]
        except Exception as exc:  # noqa: BLE001 - a diagnostic never raises
            report["error"] = str(exc)
        report["elapsed_s"] = round(time.monotonic() - started, 2)
        return report

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

        # Imported here, not in the `except` clause below: `requests` is loaded
        # lazily by `_post`, so naming `requests.exceptions.Timeout` in a handler
        # without this raises NameError *while handling the timeout* - turning a
        # slow model into a crash, which is worse than the bug being fixed.
        import requests  # noqa: PLC0415 - lazy so importing this module is free

        budget = timeout or self.timeout
        started = time.monotonic()
        try:
            body = self._post("/api/generate", payload, budget)
        except AppErrorException:
            raise
        except requests.exceptions.Timeout as exc:
            # **A timeout is not "Ollama is down".**
            #
            # It answered - it just did not finish inside a budget this
            # application chose. Reporting the two the same way sent a real
            # diagnosis in exactly the wrong direction: `ollama --translate`
            # showed a trial question answered in 0.59s and then said "Ollama is
            # not answering", so the obvious next move was to go and check a
            # service that was working perfectly.
            #
            # The health cache is deliberately *not* cleared here: the server is
            # demonstrably up, and re-probing it would be work to confirm
            # something already known.
            raise AppErrorException(make_error(
                "ERR_OLLAMA_TIMEOUT", "llm.ollama",
                timeout_s=f"{budget:g}",
                details=(
                    f"{self.model} did not reply within {budget:g}s to a "
                    f"{len(prompt):,}-character prompt."
                ),
            )) from exc
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
