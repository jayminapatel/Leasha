r"""The picture-search text encoder and Describe, as the window sees them.

Layer: L1 (`app/llm`).

`RemoteEmbedder` and `RemoteFlorence` have the few methods the window calls on
the two models that used to load inside it, and run them in the model host
(`app/ort/llm_host.py`) on the adapters in `app/ort/hosted.py`. The reason is
the chat model's, measured 2026-10-09: building an ONNX Runtime session holds
Python's lock for the whole load, so the window froze at the first picture
search (the CLIP text tower) and the first Describe (Florence-2, 12 s).

Both give back what the local object gave back, and `RemoteFlorence` keeps the
local object's promise that it never raises: a host that has ended, or a model
that is absent, is `None` - the same answer as "nothing could be described".
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Optional, Sequence

from app.core.errors import AppErrorException
from app.core.logging import logger
from app.llm.remote_onnx import ModelHost

log = logger.bind(component="llm.remote_models")

__all__ = ["RemoteEmbedder", "RemoteFlorence"]

EMBEDDER = "app.ort.hosted:HostedEmbedder"
FLORENCE = "app.ort.hosted:HostedFlorence"


class RemoteEmbedder:
    """`Embedder`'s `embed`, `warm_up` and `_on_progress`, with the model in the host."""

    def __init__(self, host: ModelHost, env_file: Optional[Path] = None, *,
                 factory: str = EMBEDDER) -> None:
        self._host = host
        #: Set by `vector.search_images` while a download may be under way; takes a percent.
        self._on_progress: Optional[Callable[[float], None]] = None
        self._iid = host.register_object(
            factory, (), {"kind": "clip-text", "env_file": str(env_file) if env_file else ""})

    def _progress(self, value: Any) -> None:
        callback = self._on_progress
        if callback is not None:
            callback(value)

    def embed(self, texts: Sequence[str]) -> Any:
        return self._host.call_object(self._iid, "embed", (list(texts),),
                                      progress=self._progress)

    def warm_up(self) -> None:
        self._host.call_object(self._iid, "warm_up", progress=self._progress)


class RemoteFlorence:
    """Florence-2's `describe` and `tag_image`, with the model in the host."""

    def __init__(self, host: ModelHost, *, factory: str = FLORENCE) -> None:
        self._host = host
        self._iid = host.register_object(factory)

    def describe(self, path: Path) -> Optional[str]:
        try:
            return self._host.call_object(self._iid, "describe", (str(path),))
        except AppErrorException as exc:
            log.debug("Describe could not reach the model process: {}", exc.error.message)
            return None

    def tag_image(self, path: Path) -> Any:
        try:
            return self._host.call_object(self._iid, "tag_image", (str(path),))
        except AppErrorException as exc:
            log.debug("tagging could not reach the model process: {}", exc.error.message)
            return None
