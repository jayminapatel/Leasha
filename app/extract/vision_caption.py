r"""On-demand and trickled photo descriptions via a vision-capable Ollama model.

Layer: L2/L3 (the corpus-wide trickle drain) + L5 (the Describe button,
wired in `app/ui/widgets/preview_window.py`)

Work order 0i (`202626270511`) section 3. Florence-2 (`florence_tagger.py`)
already gives every photo-class image a fast, automatic caption+tags pass
(section 1); this is the different thing section 3 asks for - a slower,
richer description from whichever vision model the owner has actually
pulled into Ollama (llava/qwen-vl class), fetched only when somebody presses
Describe on the one photo they are looking at, or trickled across the whole
corpus as its own enrichment-backlog kind (section 3b). Never in the search
hot path (non-negotiable #1): nothing here runs during a search, only on a
button press or during the idle/backlog drain the same way every other
enrichment kind does.

**A distinct label, deliberately.** Florence's automatic pass already writes
a chunk labelled "AI description" (`ocr.py`). Reusing that label here would
mean the cache check below ("has this file already been described?")
answers yes for a photo Florence tagged automatically, and Describe would
silently never call Ollama at all - wrong, because Florence's short caption
and a vision model's fuller, on-demand description are different things a
person might want either or both of. `AI_CAPTION_LABEL = "AI caption"` keeps
them apart while both still satisfy the standing rule that AI-written text
is always marked as AI-written, never confused with the file's own words.

**Detection follows `OllamaClient`'s own health/model questions, not
`florence_tagger`'s "is a package importable".** Availability here is "is
Ollama up, and is the configured model actually installed" - exactly what
every other Ollama-backed feature already asks. `available()` never raises
and never blocks longer than the client's own cached health check
(`HEALTH_CACHE_S`), so a greyed Describe button costs nothing to keep
checking.
"""

from __future__ import annotations

import base64
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from app.core.logging import logger
from app.llm.ollama import OllamaClient

log = logger.bind(component="extract.vision_caption")

__all__ = [
    "AI_CAPTION_LABEL", "DEFAULT_VISION_MODEL", "VisionCaptionResult",
    "available", "unavailable_reason", "describe_image",
]

#: Distinct from florence_tagger's "AI description" - see the module docstring.
AI_CAPTION_LABEL = "AI caption"

#: `app.core.settings_registry`'s `OLLAMA_VISION_MODEL` default. The owner
#: pulls the actual model; a text-only model simply ignores the `images`
#: field and answers as if it were never sent, so this name only ever
#: matters for what a person is told to install.
DEFAULT_VISION_MODEL = "llava"

_PROMPT = (
    "Describe this photo in one or two plain, factual sentences: who or "
    "what is in it, and where it looks like it was taken. Do not guess names."
)


@dataclass(frozen=True, slots=True)
class VisionCaptionResult:
    """One description, and what it cost."""

    caption: str
    model: str
    elapsed_s: float


def _inside_leasha(client: Any) -> bool:
    """2026-09-29: with `CHAT_ENGINE=onnx` Describe is Florence-2 on ONNX Runtime
    (`florence_tagger.describe`, its `<MORE_DETAILED_CAPTION>` task) - the chat
    model inside Leasha reads text, not pictures. `app.llm.engines.vision_model`
    hands this module the ONNX chat client, whose `engine` attribute says so."""
    return getattr(client, "engine", "") == "onnx"


def available(client: OllamaClient) -> bool:
    """Is Describe usable right now? Never raises.

    Two questions, both already answered by the client for every other
    Ollama-backed feature: is anything listening, and is the configured
    model actually pulled. A vision-incapable model still passes both - this
    function cannot know what a model can see, only that it exists; a wrong
    choice here is a Settings problem (`OLLAMA_VISION_MODEL`), not a crash.
    """
    try:
        if _inside_leasha(client):
            from app.extract import florence_tagger

            return florence_tagger.available()
        return bool(client.health() and client.has_model())
    except Exception:                                 # noqa: BLE001 - a check, never a crash
        return False


def unavailable_reason(client: OllamaClient) -> str:
    """One plain sentence a greyed button's tooltip can show verbatim.

    Only meaningful when `available()` has already returned False - this
    does not re-derive that answer, it explains the one already given, the
    same two-question shape `available()` itself uses.
    """
    if _inside_leasha(client):
        return ("The photo model (Florence-2) is not downloaded yet - "
                "Settings, Models, photo tags, Download.")
    try:
        reachable = client.health()
    except Exception:                                 # noqa: BLE001
        reachable = False
    if not reachable:
        return f"Ollama is not running at {client.url}."
    return (f"The '{client.model}' vision model is not installed. "
            f"Run: ollama pull {client.model}")


def describe_image(path: Path, client: OllamaClient, *, timeout: float = 60.0
                    ) -> Optional[VisionCaptionResult]:
    """One description for one image. Never raises.

    A bad photo, a model that refuses, or Ollama going down mid-request
    costs this one Describe click, not a crashed worker - the same contract
    `florence_tagger.tag_image` already gives its own callers.
    """
    started = time.monotonic()
    if _inside_leasha(client):
        from app.extract import florence_tagger

        caption = florence_tagger.describe(path) or ""
        if not caption.strip():
            return None
        return VisionCaptionResult(caption=caption.strip(), model="Florence-2",
                                   elapsed_s=time.monotonic() - started)
    try:
        data = base64.b64encode(path.read_bytes()).decode("ascii")
        response = client.generate(_PROMPT, images=[data], timeout=timeout)
    except Exception as exc:                          # noqa: BLE001 - one image, not the run
        log.debug("vision caption failed on {}: {}: {}",
                  path.name, type(exc).__name__, exc)
        return None

    caption = response.text.strip()
    if not caption:
        return None
    return VisionCaptionResult(
        caption=caption, model=client.model, elapsed_s=time.monotonic() - started)
