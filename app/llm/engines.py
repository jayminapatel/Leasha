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

__all__ = ["ONNX", "OLLAMA", "engine_of", "text_model", "vision_model", "reset_shared",
           "chat_key", "ollama_context", "use_model_host", "model_host", "clip_text_embedder"]

ONNX = "onnx"
OLLAMA = "ollama"

_shared: dict[tuple, Any] = {}
#: 2026-10-04: models picked by name on the Chat tab or for Interpret.
#: Dated note, 2026-10-04, code review: these were held weakly "only while something
#: uses them" - but the chat engine asks here afresh for every call, so nothing held
#: one between the router and the answer, and a picked model could be built and
#: loaded again within a question; warming it ahead was lost the same way. Held
#: strongly now: an unloaded client is a few attributes, and what is *loaded* is
#: limited process-wide by `app.ort.llm.MAX_RESIDENT` (the default and one picked).
_chosen: dict[tuple, Any] = {}
# One lock over both dictionaries: Interpret, Chat and Describe ask from
# different worker threads at once, and two of them building the same 1.5 GB
# model on the same key would be exactly the double load this module exists
# to prevent. Held only while a client is looked up or constructed (not
# loaded), so it is never contended for long.
_shared_lock = threading.Lock()

#: 2026-10-09. The window turns this on (`use_model_host`): the models that would
#: otherwise load inside it - the ONNX chat model, Florence-2 for Describe, the
#: picture-search text encoder - run in host processes of their own, because
#: building an ONNX Runtime session holds Python's lock for the whole load (20 s
#: measured) and froze the window. Everything else - the command line, the
#: indexer, tests - keeps the model in the calling process, where nothing is
#: waiting on a window and a second process would be a cost.
_host_enabled = False
_host_env: Optional[Path] = None
_hosts: dict[str, Any] = {}
_host_lock = threading.Lock()

#: One host per kind of model. A host's own lock is held for the whole of any model
#: load in it, so a Describe loading Florence-2 must not share a process with a chat
#: reply that is being written.
CHAT_HOST = "chat"
VISION_HOST = "vision"


def model_host(name: str = CHAT_HOST) -> Any:
    """The host process of that kind, made on first use (no process starts until
    a model in it is first asked for something)."""
    from app.llm.remote_onnx import ModelHost

    with _host_lock:
        host = _hosts.get(name)
        if host is None:
            host = _hosts[name] = ModelHost(env_file=_host_env)
        return host


def use_model_host(enabled: bool = True, env_file: Optional[Path] = None) -> None:
    """Run the window's models in host processes, or in the calling process."""
    global _host_enabled, _host_env
    with _host_lock:
        _host_enabled = bool(enabled)
        _host_env = Path(env_file) if env_file else None
        retired = list(_hosts.values())
        _hosts.clear()
    for host in retired:
        host.close()
    from app.extract import florence_tagger

    if enabled:
        from app.llm.remote_models import RemoteFlorence

        florence_tagger.set_engine_process(RemoteFlorence(model_host(VISION_HOST)))
    else:
        florence_tagger.set_engine_process(None)


def clip_text_embedder(settings: Any) -> Any:
    """The picture-search text encoder: a proxy to the vision host in the window,
    the plain `Embedder` anywhere else."""
    if _host_enabled:
        from app.llm.remote_models import RemoteEmbedder

        return RemoteEmbedder(model_host(VISION_HOST), _host_env)
    from app.search import vector

    return vector.clip_text_embedder_from_settings(settings)


def _new_onnx(cache: Any, model: str, device: str, timeout: float) -> Any:
    """A chat client: a proxy to the host process when the window asked for one."""
    if _host_enabled:
        from app.llm.remote_onnx import RemoteOnnxLLM

        return RemoteOnnxLLM(model_host(CHAT_HOST), Path(cache) if cache else None, model,
                             device=device, timeout=timeout)
    from app.ort.llm import OnnxLLM

    return OnnxLLM(Path(cache) if cache else None, model, device=device, timeout=timeout)


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
    cache = getattr(settings, "model_cache", None)
    device = str(getattr(settings, "embed_device", "auto") or "auto")
    key = (str(cache or ""), device)
    chosen = chat_key(onnx_model) if onnx_model else ""
    with _shared_lock:
        client = _shared.get(key)
        if client is None:
            client = _new_onnx(cache, "", device, float(timeout or 120.0))
            client.keep_resident = True          # Settings' model is unloaded last
            _shared[key] = client
        if not chosen:
            return client
        # 2026-10-04: a model picked by name. The shared client when it is the one
        # that serves anyway - one copy in memory, never two of the same model.
        if client.serves(chosen):
            return client
        picked = _chosen.get(key + (chosen,))
        if picked is None:
            picked = _new_onnx(cache, chosen, device, float(timeout or 120.0))
            _chosen[key + (chosen,)] = picked
        return picked


def reset_shared() -> None:
    """Forget the shared ONNX client (tests; after the engine setting changes).
    2026-10-04, code review: and unload them - a forgotten client that stayed loaded
    would hold its memory with nothing left to use it."""
    with _shared_lock:
        clients = list(_shared.values()) + list(_chosen.values())
        _shared.clear()
        _chosen.clear()
    for client in clients:
        unload = getattr(client, "unload", None)
        if unload is not None:
            unload()


def ollama_context(settings: Any) -> int:
    """The `num_ctx` every request to Ollama carries (2026-10-04, code review): Chat's
    window, `ChatSettings.context_tokens`, from the same settings and this computer's
    memory. Ollama reloads a model whose window changes, so Interpret, its warm-up and
    Chat sending different ones made one model serving both reload at every switch.
    `CHAT_CONTEXT_TOKENS` lives with Chat's settings, so they are asked."""
    from types import SimpleNamespace

    profile = None
    try:
        import psutil  # noqa: PLC0415 - optional

        profile = SimpleNamespace(ram_mb=int(psutil.virtual_memory().total / 1024 ** 2))
    except Exception:                                   # noqa: BLE001 - unknown: the defaults
        pass
    try:
        from app.chat.config import ChatSettings  # noqa: PLC0415 - read lazily

        return int(ChatSettings.from_settings(settings, profile=profile).context_tokens)
    except Exception:                                   # noqa: BLE001 - Chat's own default
        return 8192


def text_model(settings: Any, model: str = "", *, timeout: Optional[float] = None,
               url: Optional[str] = None, onnx_model: str = "",
               num_ctx: Optional[int] = None) -> Any:
    """The client Interpret and Chat talk to. `model` is an Ollama name; the ONNX
    engine ignores names it does not know and uses its own chat model.

    `onnx_model` (2026-10-04) is a catalogue key picked on the Chat tab or for
    Interpret: that ONNX model rather than the one Settings would choose. It reads
    the catalogue, so a caller passing it is on a worker.

    `num_ctx` (2026-10-04, code review) is the window an Ollama client sends on every
    call; by default `ollama_context(settings)`, the one Chat uses."""
    if engine_of(settings) == ONNX:
        return _shared_onnx(settings, timeout, onnx_model)
    from app.llm.ollama import OllamaClient

    kwargs: dict[str, Any] = {} if timeout is None else {"timeout": timeout}
    return OllamaClient(url or getattr(settings, "ollama_url", "http://127.0.0.1:11434"),
                        model or getattr(settings, "ollama_model", "mistral"),
                        num_ctx=int(num_ctx or ollama_context(settings)), **kwargs)


def vision_model(settings: Any, *, url: Optional[str] = None, model: str = "") -> Any:
    """The client Describe hands to `vision_caption`. For ONNX that is the shared
    chat client, whose `engine` attribute sends `vision_caption` to Florence-2."""
    if engine_of(settings) == ONNX:
        return _shared_onnx(settings, None)
    from app.llm.ollama import OllamaClient

    return OllamaClient(url or getattr(settings, "ollama_url", "http://127.0.0.1:11434"),
                        model or getattr(settings, "ollama_vision_model", "llava"))
