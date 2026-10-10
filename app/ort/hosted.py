r"""The other models the window used to load itself, as objects the model host can run.

Layer: L2 (`app/ort`), beside `llm_host.py`, which builds these by name.

Two models still loaded inside the window after it was shown, each holding
Python's lock for its whole load (the cause of the chat model's 14 s freeze,
measured 2026-10-09):

* the **picture-search text encoder** (CLIP's text tower), loaded by the first
  search that reaches the picture lane; and
* **Florence-2**, loaded by the first Describe.

2026-10-10 (owner: "take the other models into the helper too"): and the two
the window loaded at start-up, which the 2026-10-09 entry left in it -

* the **meaning model** (`app/index/embedder.py`, the query embedder of every
  search: `HostedEmbedder(kind="meaning")`); and
* the **reranker** (`app/search/rerank.py`, FastEmbed's cross-encoder:
  `HostedReranker`).

Both warm on a worker after the window is shown (`MainWindow._warm_models`),
so their loads held the window's lock in the first seconds of every session -
the same freeze, only shorter and earlier. They run in a third host of their
own (`engines.SEARCH_HOST`): a query must never wait behind a chat model or
Florence-2 loading.

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

__all__ = ["HostedEmbedder", "HostedFlorence", "HostedReranker"]


def _settings(env_file: str) -> Any:
    from app.core.config import load_settings

    return load_settings(Path(env_file) if env_file else None,
                         create_dirs=False, check_writable=False)


class HostedEmbedder:
    """A text embedder: `embed`, `warm_up`.

    `kind` is `clip-text` (the picture-search text encoder, built from the
    `.env` as before) or `meaning` (2026-10-10: the meaning model the window's
    search engine embeds every query with). For `meaning`, `spec` is the
    constructor arguments of the `Embedder` the window would have built -
    model, width, cache, processor, quantised file - resolved there by
    `Embedder.from_settings` from the window's own settings and sent across, so
    the host builds exactly that object rather than re-reading a `.env` that a
    Settings change in this session may have moved past. Everything `Embedder`
    does - the graphics-card gate around its load and every batch, the retry on
    the processor after a driver reset, the unit-length and width guards - runs
    here unchanged, in this process, which is the one that holds the session.
    """

    HOSTED_METHODS = frozenset({"embed", "warm_up"})
    KINDS = frozenset({"clip-text", "meaning"})

    def __init__(self, kind: str = "clip-text", env_file: str = "",
                 spec: Optional[dict] = None) -> None:
        if kind not in self.KINDS:
            raise ValueError(f"no hosted embedder called {kind!r}")
        #: Set by the host for the call in progress: takes a percent.
        self.progress_sink: Optional[Any] = None
        self._kind = kind
        self._env_file = env_file
        self._spec = dict(spec or {})
        self._embedder: Any = None

    def _built(self) -> Any:
        if self._embedder is None:
            if self._kind == "meaning":
                from app.index.embedder import Embedder

                if self._spec:
                    fields = dict(self._spec)
                    self._embedder = Embedder(str(fields.pop("model_name")), **fields)
                else:
                    self._embedder = Embedder.from_settings(_settings(self._env_file))
            else:
                from app.search import vector

                self._embedder = vector.clip_text_embedder_from_settings(
                    _settings(self._env_file))
        return self._embedder

    def _hand_on_progress(self, embedder: Any) -> None:
        if self.progress_sink is not None:
            embedder._on_progress = self.progress_sink      # noqa: SLF001 - the download hook

    def embed(self, texts: Sequence[str]) -> Any:
        embedder = self._built()
        self._hand_on_progress(embedder)
        return embedder.embed(list(texts))

    def warm_up(self) -> None:
        # 2026-10-10: the progress hook is handed on here too. The meaning model is
        # the one a first run downloads, and that download happens in the warm-up,
        # not in a search - before this date only `embed` passed it on.
        embedder = self._built()
        self._hand_on_progress(embedder)
        embedder.warm_up()


def _host_reranker_class() -> Any:
    """`Reranker`, told to keep a failed load's cause rather than log it.

    Made on first use, so this module imports nothing from `app/search` until a
    reranker is actually hosted."""
    from app.search.rerank import Reranker

    class _HostReranker(Reranker):
        #: The exception the last load failed with; the window logs it.
        last_error: Optional[BaseException] = None

        def _warn_once(self, exc: BaseException, *, transient: bool = False) -> None:
            # The warning is the window's to give (`RemoteReranker`), once, in the
            # window's log and with its own wording; a second copy from here would
            # land in a log nobody reads.
            self.last_error = exc

    return _HostReranker


class HostedReranker:
    """The reranker's model (2026-10-10): `load`, `score`.

    Only the model crosses. The window keeps its own `Reranker` (as
    `app.llm.remote_models.RemoteReranker`), with everything about *which*
    passages are scored - the Rerank switch, `top_n`, the window of text around
    the match, the textless photo rows, the failure budget, the warning - so a
    Settings change still takes effect on the next search with nothing sent
    here. The window sends the passages already cut; this scores them.

    **The graphics-card gate is taken here, not there** (`gpu_serialize`): the
    load is gated inside `Reranker._ensure_scorer`, and each scoring call below
    on what actually loaded. The window holds no gate while it waits on the
    pipe - a hand-off to another process while holding it would keep every other
    graphics-card user on the machine waiting on a pipe, and the host would then
    wait on a gate its own caller holds.
    """

    HOSTED_METHODS = frozenset({"load", "score"})

    def __init__(self, model_name: str = "BAAI/bge-reranker-base",
                 cache_dir: Optional[str] = None, device: str = "auto") -> None:
        self.progress_sink: Optional[Any] = None
        # Always enabled here: whether to rerank is the window's decision, and a
        # call only arrives once it has made it.
        self._reranker = _host_reranker_class()(
            model_name, cache_dir=cache_dir, device=device, enabled=True)

    def _scorer(self) -> Any:
        reranker = self._reranker
        scorer = reranker._ensure_scorer()                  # noqa: SLF001 - the gated load
        if scorer is None:
            cause = reranker.last_error
            reranker.last_error = None
            # A failed load latches `_unavailable` in `Reranker`. Cleared here, so a
            # window that asks again tries again rather than meeting a stale latch:
            # the window keeps its own latch, and that is the one that decides.
            reranker._unavailable = False                    # noqa: SLF001
            if cause is None:
                raise RuntimeError("the reranker did not load")
            raise RuntimeError(f"{type(cause).__name__}: {cause}") from cause
        return scorer

    def load(self) -> bool:
        """Load the model now. Raises with the cause when it cannot be."""
        self._scorer()
        return True

    def score(self, query: str, passages: Sequence[str]) -> list[float]:
        """One score per passage, higher is better - `rerank.Scorer`'s contract."""
        from app.core.gpu_serialize import (
            gpu_exclusive,
            is_transient_gpu_error,
            mark_gpu_unreliable,
        )

        scorer = self._scorer()
        reranker = self._reranker
        try:
            # Gated on what actually ran, as `Reranker.rerank` is in one process.
            with gpu_exclusive(bool(reranker.choice and reranker.choice.is_gpu)):
                return [float(value) for value in scorer(query, list(passages))]
        except Exception as exc:
            if is_transient_gpu_error(exc):
                # The half of `Reranker.rerank`'s transient path that belongs to the
                # process holding the session: the next load here lands on the
                # processor, and the dead session is not called again. The failure
                # budget is the window's, and still counts this attempt.
                mark_gpu_unreliable(f"{type(exc).__name__}: {exc}"[:200])
                reranker._scorer = None                      # noqa: SLF001
                reranker.choice = None
            raise


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
