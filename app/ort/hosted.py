r"""The other models the window used to load itself, as objects the model host can run.

Layer: L2 (`app/ort`), beside `llm_host.py`, which builds these by name.

Two models still loaded inside the window after it was shown, each holding
Python's lock for its whole load (the cause of the chat model's 14 s freeze,
measured 2026-10-09):

* the **picture-search text encoder** (CLIP's text tower), loaded by the first
  search that reaches the picture lane; and
* **Florence-2**, loaded by the first Describe.

Each is wrapped here in a small adapter with the few methods the window uses,
named in `HOSTED_METHODS` - the only calls the host will forward. The adapters
build the same objects the window built (`vector.clip_text_embedder_from_settings`,
`florence_tagger`), from the same `.env`, so what they compute does not change;
only the process it happens in.

A download-progress callback cannot cross a pipe, so the host sets
`progress_sink` before a call and the adapter passes it on; the frame it sends
reaches the window's own callback (`RemoteEmbedder._on_progress`).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional, Sequence

__all__ = ["HostedEmbedder", "HostedFlorence"]


def _settings(env_file: str) -> Any:
    from app.core.config import load_settings

    return load_settings(Path(env_file) if env_file else None,
                         create_dirs=False, check_writable=False)


class HostedEmbedder:
    """The CLIP text embedder: `embed`, `warm_up`."""

    HOSTED_METHODS = frozenset({"embed", "warm_up"})

    def __init__(self, kind: str = "clip-text", env_file: str = "") -> None:
        if kind != "clip-text":
            raise ValueError(f"no hosted embedder called {kind!r}")
        #: Set by the host for the call in progress: takes a percent.
        self.progress_sink: Optional[Any] = None
        self._env_file = env_file
        self._embedder: Any = None

    def _built(self) -> Any:
        if self._embedder is None:
            from app.search import vector

            self._embedder = vector.clip_text_embedder_from_settings(_settings(self._env_file))
        return self._embedder

    def embed(self, texts: Sequence[str]) -> Any:
        embedder = self._built()
        if self.progress_sink is not None:
            embedder._on_progress = self.progress_sink      # noqa: SLF001 - the download hook
        return embedder.embed(list(texts))

    def warm_up(self) -> None:
        self._built().warm_up()


class HostedFlorence:
    """Florence-2: `describe`, `tag_image`."""

    HOSTED_METHODS = frozenset({"describe", "tag_image"})

    def __init__(self) -> None:
        self.progress_sink: Optional[Any] = None

    def describe(self, path: str) -> Optional[str]:
        from app.extract import florence_tagger

        return florence_tagger.describe(Path(path))

    def tag_image(self, path: str) -> Any:
        from app.extract import florence_tagger

        return florence_tagger.tag_image(Path(path))
