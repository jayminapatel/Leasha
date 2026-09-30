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
from pathlib import Path
from typing import Any, Optional

__all__ = ["ONNX", "OLLAMA", "engine_of", "text_model", "vision_model", "reset_shared"]

ONNX = "onnx"
OLLAMA = "ollama"

_shared: dict[tuple, Any] = {}
_shared_lock = threading.Lock()


def engine_of(settings: Any) -> str:
    """`onnx` unless the settings say `ollama` - the registry default."""
    value = str(getattr(settings, "chat_engine", ONNX) or ONNX).strip().lower()
    return OLLAMA if value == OLLAMA else ONNX


def _shared_onnx(settings: Any, timeout: Optional[float]) -> Any:
    from app.ort.llm import OnnxLLM

    cache = getattr(settings, "model_cache", None)
    device = str(getattr(settings, "embed_device", "auto") or "auto")
    key = (str(cache or ""), device)
    with _shared_lock:
        client = _shared.get(key)
        if client is None:
            client = OnnxLLM(Path(cache) if cache else None, device=device,
                             timeout=float(timeout or 120.0))
            _shared[key] = client
        return client


def reset_shared() -> None:
    """Forget the shared ONNX client (tests; after the engine setting changes)."""
    with _shared_lock:
        _shared.clear()


def text_model(settings: Any, model: str = "", *, timeout: Optional[float] = None,
               url: Optional[str] = None) -> Any:
    """The client Interpret and Chat talk to. `model` is an Ollama name; the ONNX
    engine ignores names it does not know and uses its own chat model."""
    if engine_of(settings) == ONNX:
        return _shared_onnx(settings, timeout)
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
