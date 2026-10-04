r"""Which engine writes text: the model inside Leasha (ONNX) or Ollama.

Layer: L1 (reads settings, builds a client; no Qt, no I/O at construction).

`CHAT_ENGINE` (owner, 2026-09-29): `onnx` by default - the chat model runs in
this process (`app/ort/llm.py`), with nothing else to install - or `ollama`.
Every place that used to build an `OllamaClient` for Interpret, Chat or Describe
asks here instead, and gets an object with the same methods either way.

**One ONNX model per process.** Interpret and Chat both want the chat model;
loading 1.5 GB twice would cost the memory and the load time twice, so the
ONNX client is shared, keyed by model folder and device.
"""

from __future__ import annotations

import threading
import weakref
from pathlib import Path
from typing import Any, Optional

__all__ = ["ONNX", "OLLAMA", "engine_of", "text_model", "vision_model", "reset_shared",
           "chat_key"]

ONNX = "onnx"
OLLAMA = "ollama"

_shared: dict[tuple, Any] = {}
#: 2026-10-04: models picked by name on the Chat tab or for Interpret. Held only
#: while something uses them, so a model picked and then left frees its memory.
_chosen: "weakref.WeakValueDictionary[tuple, Any]" = weakref.WeakValueDictionary()
_shared_lock = threading.Lock()


def engine_of(settings: Any) -> str:
    """`onnx` unless the settings say `ollama` - the registry default."""
    value = str(getattr(settings, "chat_engine", ONNX) or ONNX).strip().lower()
    return OLLAMA if value == OLLAMA else ONNX


def chat_key(name: str) -> str:
    """`name` when it is an ONNX chat model's catalogue key, else "". Reads the
    catalogue: worker threads only."""
    from app.ort import hub

    wanted = str(name or "").strip()
    if not wanted:
        return ""
    try:
        from app.ort import catalogue

        entry = catalogue.load().by_key(wanted)
        if entry is not None:
            return wanted if entry.job == "chat" else ""
    except Exception:                                   # noqa: BLE001 - the built-ins below
        pass
    return wanted if wanted in (hub.QWEN_1_5B.key, hub.QWEN_1_5B_Q4.key) else ""


def _shared_onnx(settings: Any, timeout: Optional[float], onnx_model: str = "") -> Any:
    from app.ort.llm import OnnxLLM

    cache = getattr(settings, "model_cache", None)
    device = str(getattr(settings, "embed_device", "auto") or "auto")
    key = (str(cache or ""), device)
    chosen = chat_key(onnx_model) if onnx_model else ""
    with _shared_lock:
        client = _shared.get(key)
        if client is None:
            client = OnnxLLM(Path(cache) if cache else None, device=device,
                             timeout=float(timeout or 120.0))
            _shared[key] = client
        if not chosen:
            return client
        # 2026-10-04: a model picked by name. The shared client when it is the one
        # that serves anyway - one copy in memory, never two of the same model.
        if client.serves(chosen):
            return client
        picked = _chosen.get(key + (chosen,))
        if picked is None:
            picked = OnnxLLM(Path(cache) if cache else None, chosen, device=device,
                             timeout=float(timeout or 120.0))
            _chosen[key + (chosen,)] = picked
        return picked


def reset_shared() -> None:
    """Forget the shared ONNX client (tests; after the engine setting changes)."""
    with _shared_lock:
        _shared.clear()
        _chosen.clear()


def text_model(settings: Any, model: str = "", *, timeout: Optional[float] = None,
               url: Optional[str] = None, onnx_model: str = "") -> Any:
    """The client Interpret and Chat talk to. `model` is an Ollama name; the ONNX
    engine ignores names it does not know and uses its own chat model.

    `onnx_model` (2026-10-04) is a catalogue key picked on the Chat tab or for
    Interpret: that ONNX model rather than the one Settings would choose. It reads
    the catalogue, so a caller passing it is on a worker."""
    if engine_of(settings) == ONNX:
        return _shared_onnx(settings, timeout, onnx_model)
    from app.llm.ollama import OllamaClient

    kwargs = {} if timeout is None else {"timeout": timeout}
    return OllamaClient(url or getattr(settings, "ollama_url", "http://127.0.0.1:11434"),
                        model or getattr(settings, "ollama_model", "mistral"), **kwargs)


def vision_model(settings: Any, *, url: Optional[str] = None, model: str = "") -> Any:
    """The client Describe hands to `vision_caption`. For ONNX that is the shared
    chat client, whose `engine` attribute sends `vision_caption` to Florence-2."""
    if engine_of(settings) == ONNX:
        return _shared_onnx(settings, None)
    from app.llm.ollama import OllamaClient

    return OllamaClient(url or getattr(settings, "ollama_url", "http://127.0.0.1:11434"),
                        model or getattr(settings, "ollama_vision_model", "llava"))
