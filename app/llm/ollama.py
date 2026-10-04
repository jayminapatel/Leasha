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
from typing import Any, Callable, Iterator, Optional

from app.core.errors import AppError, AppErrorException, make_error
from app.core.logging import logger

__all__ = ["OllamaClient", "OllamaResponse", "HEALTH_CACHE_S", "KEEP_ALIVE"]

log = logger.bind(component="llm.ollama")

#: How long a health check is trusted. Long enough that a batch job does not
#: re-probe before every call; short enough that starting Ollama is noticed
#: within a few seconds rather than needing a restart of the app.
HEALTH_CACHE_S = 10.0

#: How long Ollama holds the model in memory after a request.
#:
#: **Its default is five minutes, and that is the wrong number here.** This
#: application asks the model one short question at a time, minutes or hours
#: apart, and the load costs 8.2s against a translate budget of five seconds -
#: so every interpretation after a break timed out while the service was
#: working perfectly, and the failure looked like a broken model rather than a
#: cold one. Thirty minutes covers a working session at the price of some VRAM
#: that is only committed once somebody has switched Interpret on.
KEEP_ALIVE = "30m"

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
    """Health check, then generate. Nothing else.

    Dated note, 2026-09-29: and `pull`, which fetches a model - only ever
    because somebody pressed Download beside a model list in Settings.
    """

    def __init__(
        self,
        url: str = "http://127.0.0.1:11434",
        model: str = "mistral",
        *,
        timeout: float = 120.0,
        connect_timeout: float = CONNECT_TIMEOUT_S,
        transport: Optional[Callable[..., Any]] = None,
        num_ctx: Optional[int] = None,
    ) -> None:
        self.url = url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.connect_timeout = connect_timeout
        #: The window sent on every `generate` and `warm` that names none (2026-10-04,
        #: code review). Ollama reloads a model whose `num_ctx` changes, so it is held
        #: here, once, rather than remembered by each caller: Interpret's generate,
        #: its warm-up and Chat sent three different values to one model.
        #: `app.llm.engines.text_model` sets it to Chat's window; `None` sends none.
        self.num_ctx = int(num_ctx) if num_ctx else None
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

    def set_model(self, name: str) -> None:
        """Point this client at a different model, now.

        **The health cache must go with it.** `health()` caches for
        HEALTH_CACHE_S, and that answer was about the *old* model. A stale "yes"
        lets `generate` proceed against a model that is not installed, so the
        failure arrives seconds later from a call that had already been told
        everything was fine.
        """
        self.model = (name or "").strip() or self.model
        self._healthy_until = 0.0

    def available_models(self) -> list[str]:
        try:
            payload = self._get("/api/tags", timeout=min(self.timeout, 5.0))
        except Exception:  # noqa: BLE001
            return []
        return [str(entry.get("name", "")) for entry in payload.get("models", [])]

    def _stream(self, path: str, payload: dict, timeout: float) -> Iterator[dict]:
        """POST and yield each JSON line of a streamed reply. Closed when done.

        `timeout` is the wait *between* lines, not for the whole reply: a pull
        of a 5GB model takes as long as the line does, and says so every few
        hundred milliseconds while it works.
        """
        if self._transport is not None:
            yield from self._transport("STREAM", self.url + path, payload, timeout)
            return
        import requests  # noqa: PLC0415

        response = requests.post(self.url + path, json=payload, stream=True,
                                 timeout=self._budget(timeout))
        try:
            response.raise_for_status()
            for line in response.iter_lines():
                if line:
                    yield json.loads(line)
        finally:
            # Closing the connection is also how a Stop reaches Ollama: it
            # abandons a pull whose caller has gone and keeps what it already
            # has, so pressing Download again carries on rather than restarts.
            response.close()

    def pull(
        self,
        name: str,
        *,
        on_status: Optional[Callable[[dict], None]] = None,
        should_stop: Optional[Callable[[], bool]] = None,
        timeout: float = 120.0,
    ) -> bool:
        """Download `name` into Ollama. True when it finished, False if stopped.

        **Only called because a person pressed Download.** Nothing in search,
        indexing or start-up reaches this; the application stays offline until
        somebody asks for a model by name. Blocks for as long as the download
        takes, so it runs on a worker - see `app/core/model_fetch.py`.

        Each status line Ollama sends (`{"status", "total", "completed"}`) is
        handed to `on_status`. Raises `AppErrorException(ERR_MODEL_DOWNLOAD)`
        when Ollama reports an error (an unknown name, a full disk) or cannot
        be reached, with its own words in the details.
        """
        wanted = (name or "").strip()
        try:
            # `model` is the field's name today; `name` is what older Ollama
            # builds read. Unknown fields are ignored, so both are sent.
            for event in self._stream("/api/pull",
                                      {"model": wanted, "name": wanted, "stream": True},
                                      timeout):
                if should_stop is not None and should_stop():
                    return False
                if not isinstance(event, dict):
                    continue
                if event.get("error"):
                    raise AppErrorException(make_error(
                        "ERR_MODEL_DOWNLOAD", "llm.ollama", model=wanted,
                        details=str(event.get("error"))))
                if on_status is not None:
                    on_status(event)
                if str(event.get("status", "")).lower() == "success":
                    return True
        except AppErrorException:
            raise
        except Exception as exc:  # noqa: BLE001 - one shape out, like generate
            self._healthy_until = 0.0
            raise AppErrorException(make_error(
                "ERR_MODEL_DOWNLOAD", "llm.ollama", model=wanted,
                details=f"Ollama at {self.url}: {exc}")) from exc
        if should_stop is not None and should_stop():
            return False
        raise AppErrorException(make_error(
            "ERR_MODEL_DOWNLOAD", "llm.ollama", model=wanted,
            details="Ollama ended the download without saying it had finished."))

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

    def warm(self, *, timeout: float = 30.0, num_ctx: Optional[int] = None) -> bool:
        """Load the model into memory without asking it anything.

        **Called when Interpret is switched on, never at startup.** Ollama is
        optional and off by default, and loading a model into VRAM for somebody
        who never presses the button is a cost they did not ask for. But the
        first press after that pays 8.2s of load against a five-second budget
        and reports a timeout - which reads as a broken model rather than a
        cold one.

        An empty prompt with `num_predict: 0` loads and generates nothing; that
        is Ollama's own documented way to preload. Returns whether it worked,
        and never raises: warming is an optimisation, and an optimisation that
        can fail a search is not one.
        """
        # `num_ctx` (2026-10-04): Chat loads the model with its own window, and Ollama
        # reloads a model whose window changes - so a warm without it is undone by the
        # first question (`app.chat.llm.OllamaLLM`).
        options: dict[str, Any] = {"num_predict": 0}
        num_ctx = num_ctx or self.num_ctx
        if num_ctx:
            options["num_ctx"] = int(num_ctx)
        try:
            self._post("/api/generate",
                       {"model": self.model, "prompt": "", "stream": False,
                        "keep_alive": KEEP_ALIVE,
                        "options": options},
                       timeout)
            log.debug("warmed {} (keep_alive {})", self.model, KEEP_ALIVE)
            return True
        except Exception as exc:                 # noqa: BLE001 - see docstring
            log.debug("could not warm {}: {}", self.model, exc)
            return False

    def generate(
        self,
        prompt: str,
        *,
        json_mode: bool = False,
        temperature: float = 0.0,
        timeout: Optional[float] = None,
        max_tokens: Optional[int] = None,
        stop: Optional[list[str]] = None,
        images: Optional[list[str]] = None,
        num_ctx: Optional[int] = None,
    ) -> OllamaResponse:
        """One completion. Raises `AppErrorException(ERR_OLLAMA_DOWN)` on anything.

        `temperature=0.0` by default because every current caller is doing
        extraction rather than writing. Extraction that varies between runs makes
        a graph that changes when nothing changed, and nothing downstream can
        tell that apart from the corpus having changed.

        `images`, when given, is a list of base64-encoded image bytes (no data
        URI prefix - Ollama's own `/api/generate` shape). Work order 0i section
        3: the "Describe" button and the caption trickle both need a vision
        model (llava/qwen-vl class) to look at a photo, not just read text: a
        model with no vision head accepts the field and answers as if it were
        never sent, so the caller (`app.extract.vision_caption`) is what
        decides whether the configured model is vision-capable, not this
        client - this stays a thin, model-agnostic transport exactly as the
        module docstring promises.
        """
        if not self.health():
            raise AppErrorException(self.down_error("Ollama is not responding at " + self.url))

        payload: dict[str, Any] = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            # **How long Ollama keeps the model in memory after this call.**
            # The default is five minutes, after which the next request pays
            # the load again - 8.2s measured here, against a translate budget
            # of five seconds, which is how every interpretation after a coffee
            # break timed out while the service was working perfectly.
            #
            # Sent on every request rather than configured once: `keep_alive`
            # is a property of the request, and a server restarted underneath
            # us would otherwise silently go back to the default.
            "keep_alive": KEEP_ALIVE,
            "options": {"temperature": temperature},
        }
        if max_tokens:
            # **Ollama generates without limit by default.** `num_predict` is -1
            # for /api/generate, so a model asked for a one-line answer is free
            # to write three paragraphs explaining itself - and every token of
            # that is paid for at the caller's timeout.
            #
            # This was not theoretical: mistral took over thirty seconds on a
            # prompt whose useful answer is about ten tokens, and `clean_output`
            # then discarded everything after the first line. The wait was for
            # text that was thrown away.
            payload["options"]["num_predict"] = int(max_tokens)
        if stop:
            # Cheaper still: stop the moment the answer is complete rather than
            # generating up to the cap and truncating afterwards.
            payload["options"]["stop"] = list(stop)
        num_ctx = num_ctx or self.num_ctx
        if num_ctx:
            # 2026-10-04: the window Chat streams with. Ollama reloads a model whose
            # `num_ctx` changes - measured 3.9-4.9 s a time on qwen2.5:1.5b - so the
            # router's call without it and the answer's with it reloaded the model
            # twice a question. `None` (Interpret, the graph) is Ollama's default.
            # Dated note, 2026-10-04, code review: Interpret's client now carries
            # Chat's window too (`self.num_ctx`, set by `engines.text_model`).
            payload["options"]["num_ctx"] = int(num_ctx)
        if json_mode:
            payload["format"] = "json"
        if images:
            payload["images"] = list(images)

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
