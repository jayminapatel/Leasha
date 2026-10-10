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

**2026-10-10: the meaning model and the reranker too** (owner: "take the other
models into the helper too"). They loaded in the window at start-up - warmed on
a worker once the window was shown, still holding the window's lock for each
load. `RemoteEmbedder.for_meaning` is the window search engine's query embedder
and `RemoteReranker` its reranker, both in the search host
(`engines.SEARCH_HOST`). Their call interfaces are the local classes': search
calls them exactly as before. Built only by `engines.meaning_embedder` and
`engines.search_reranker` while the window has turned the hosts on; the indexer
and the command line keep the local objects.

**What a query costs on the pipe** (UNCONFIRMED - not measured): one short text
out, one 384-wide float32 row back (about 1.5 KB pickled), two frames each way
through an anonymous pipe and a queue hand-off on each side. Expected well under
a millisecond, set against tens of milliseconds for the model call itself. The
reranker sends thirty cut passages (`RERANK_WINDOW_CHARS` each, some 10 to 20 KB)
and gets thirty floats back - again small against a cross-encoder pass.

**A host that will not start degrades exactly as a failed load did.** Each
proxy raises, or answers, what the local object raised or answered when its
model would not load: `RemoteEmbedder.embed` raises `AppErrorException`
(`ERR_MODEL_HOST_ENDED`), which `vector.search` already turns into keyword-only
results with the notice; `RemoteReranker` latches unavailable and warns once,
so results keep their fused order.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Iterator, Optional, Sequence

from app.core.errors import AppErrorException
from app.core.logging import logger
from app.llm.remote_onnx import ModelHost
from app.search.rerank import Reranker

log = logger.bind(component="llm.remote_models")

__all__ = ["RemoteEmbedder", "RemoteFlorence", "RemoteReranker"]

EMBEDDER = "app.ort.hosted:HostedEmbedder"
FLORENCE = "app.ort.hosted:HostedFlorence"
RERANKER = "app.ort.hosted:HostedReranker"

#: `Embedder.batch_size`'s default (`app.index.embedder.EMBED_BATCH`), for
#: `embed_all` when the window's own embedder named none.
_BATCH = 256


class RemoteEmbedder:
    """`Embedder`'s `embed`, `embed_all`, `warm_up` and `_on_progress`, with the
    model in the host."""

    def __init__(self, host: ModelHost, env_file: Optional[Path] = None, *,
                 factory: str = EMBEDDER, kind: str = "clip-text",
                 spec: Optional[dict] = None,
                 on_progress: Optional[Callable[[float], None]] = None) -> None:
        self._host = host
        spec = dict(spec or {})
        #: Set by `vector.search_images` while a download may be under way, or
        #: given at construction (the meaning model's splash hook); takes a percent.
        self._on_progress: Optional[Callable[[float], None]] = on_progress
        # The attributes code written for `Embedder` reads, copied from the object
        # the window would have built. `choice` stays `None` here for good: the
        # processor is the host's, and nothing in this process may take the
        # graphics-card gate on the strength of it (`gpu_serialize`).
        self.model_name = str(spec.get("model_name", ""))
        self.dim = int(spec.get("dim", 0) or 0)
        self.batch_size = max(1, int(spec.get("batch_size", _BATCH) or _BATCH))
        self.device = str(spec.get("device", "auto") or "auto")
        self.cache_dir = spec.get("cache_dir")
        self.choice: Any = None
        self._answered = False
        kwargs: dict = {"kind": kind, "env_file": str(env_file) if env_file else ""}
        if spec:
            kwargs["spec"] = spec
        self._iid = host.register_object(factory, (), kwargs)

    @classmethod
    def for_meaning(cls, host: ModelHost, local: Any, *,
                    factory: str = EMBEDDER) -> "RemoteEmbedder":
        """The meaning model, from the `Embedder` the window would have used.

        `local` is built by `Embedder.from_settings` and never loads anything (its
        model is lazy); only its arguments travel. `on_progress` stays here - a
        callable cannot cross a pipe - and the host's progress frames reach it."""
        spec = {
            "model_name": local.model_name,
            "dim": int(local.dim),
            "cache_dir": local.cache_dir,
            "batch_size": int(local.batch_size),
            "device": local.device,
            "threads": int(getattr(local, "threads", 0) or 0),
            "quantised": bool(getattr(local, "quantised", False)),
        }
        return cls(host, None, factory=factory, kind="meaning", spec=spec,
                   on_progress=getattr(local, "_on_progress", None))

    @property
    def loaded(self) -> bool:
        """What this proxy has seen: an answer came back from a host still running."""
        return self._answered and self._host.alive

    def _progress(self, value: Any) -> None:
        callback = self._on_progress
        if callback is not None:
            callback(value)

    def embed(self, texts: Sequence[str]) -> Any:
        # `Embedder.embed`'s first promise, kept on this side: no texts, no model
        # touched - and here, no host started to say so.
        if not texts:
            return []
        vectors = self._host.call_object(self._iid, "embed", (list(texts),),
                                         progress=self._progress)
        self._answered = True
        return vectors

    def embed_all(self, texts: Sequence[str]) -> Iterator[Any]:
        """`Embedder.embed_all`: `batch_size` at a time, one row per text."""
        for start in range(0, len(texts), self.batch_size):
            yield from self.embed(texts[start:start + self.batch_size])

    def warm_up(self) -> None:
        self._host.call_object(self._iid, "warm_up", progress=self._progress)
        self._answered = True


class RemoteReranker(Reranker):
    """`Reranker`, with its model in the host (2026-10-10).

    A subclass, not a stand-in: `rerank`, `available`, `enabled`, `top_n`,
    `window_chars`, `model_name`, the textless rows, the failure budget and the
    warnings are all `Reranker`'s own, in this process, so the Rerank switch and
    the Settings values still apply live and the search flow sees the same
    object it always did. Only the step that loads and runs the cross-encoder is
    replaced: `_ensure_scorer` asks the host to load it (`HostedReranker.load`)
    and hands back a scorer that sends the cut passages across.

    **No gate on this side.** `self.choice` stays `None`, so `Reranker.rerank`'s
    `gpu_exclusive(...)` around the scorer is a no-op here; the host takes the
    gate itself around the real call. Every hand-off to the other process
    happens with the gate free (`gpu_serialize`).

    **A failed load is today's failed load.** A host that cannot start, or a
    model that will not load in it, latches `_unavailable` and warns once with
    the cause, as `Reranker._ensure_scorer` does - results keep their fused
    order. A host that dies while scoring is a scoring failure, counted against
    `RERANK_FAILURE_BUDGET`; the next search starts a fresh host.
    """

    def __init__(self, model_name: str = "BAAI/bge-reranker-base", *,
                 host: ModelHost, factory: str = RERANKER, **fields: Any) -> None:
        fields.pop("scorer", None)              # the scorer is the host's
        super().__init__(model_name, **fields)
        self._host = host
        self._iid = host.register_object(
            factory, (), {"model_name": self.model_name, "cache_dir": self.cache_dir,
                          "device": self.device})

    def _ensure_scorer(self) -> Optional[Any]:
        if self._scorer is not None:
            return self._scorer
        if self._unavailable or not self.enabled:
            return None
        with self._lock:
            if self._scorer is not None:
                return self._scorer
            try:
                self._host.call_object(self._iid, "load")
            except Exception as exc:            # noqa: BLE001 - absent, broken, or no host
                self._unavailable = True
                self._warn_once(_cause(exc))
                return None
            self._scorer = self._score_in_host
            return self._scorer

    def _score_in_host(self, query: str, passages: Sequence[str]) -> list[float]:
        return list(self._host.call_object(self._iid, "score", (query, list(passages))))


def _cause(exc: BaseException) -> BaseException:
    """The host's own words for a failure, for `Reranker._warn_once`'s "Cause:".

    An `AppErrorException` prints its code; the sentence worth logging is in
    the error's details (the host's `RuntimeError: ValueError: ...`)."""
    if isinstance(exc, AppErrorException):
        error = exc.error
        text = str(getattr(error, "details", "") or getattr(error, "message", "") or exc)
        return RuntimeError(text)
    return exc


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
