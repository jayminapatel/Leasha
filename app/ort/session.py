r"""An ONNX Runtime session on the right processor, the way every other model gets one.

Layer: L2.

The embedder, the reranker and OCR already share one decision about the
graphics card (`app.index.backends.choose`), one fall-back when DirectML
refuses a graph (`with_fallback`), one process-wide gate so two DirectML
sessions are never built at once (`gpu_serialize.gpu_exclusive`), and one line
in the run log saying what actually ran (`record_provider`). The models in
this package use exactly those, so a graphics driver that drops out - twice on
the owner's laptop on 2026-09-29 - is handled the same way for all of them.
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from app.core.gpu_serialize import gpu_exclusive
from app.core.logging import logger

__all__ = ["Loaded", "is_cached_decoder", "is_quantised", "load_session", "machine_profile"]

_log = logger.bind(component="ort.session")

_profile: Any = None
_profile_lock = threading.Lock()


def machine_profile() -> Any:
    """This machine, detected once per process. Never raises."""
    global _profile
    with _profile_lock:
        if _profile is None:
            try:
                from app.core.compute_profile import detect

                _profile = detect()
            except Exception:                        # noqa: BLE001 - detection never fatal
                _profile = object()
        return _profile


@dataclass
class Loaded:
    """A session and the processor it actually runs on."""

    session: Any
    choice: Any

    @property
    def on_gpu(self) -> bool:
        return bool(self.choice is not None and self.choice.is_gpu)


def _options(providers: tuple[str, ...], threads: int, optimise: str = "all") -> Any:
    import onnxruntime as ort

    options = ort.SessionOptions()
    options.graph_optimization_level = (ort.GraphOptimizationLevel.ORT_ENABLE_BASIC
                                        if optimise == "basic"
                                        else ort.GraphOptimizationLevel.ORT_ENABLE_ALL)
    if threads > 0:
        options.intra_op_num_threads = threads
    if providers and providers[0] == "DmlExecutionProvider":
        # DirectML's documented requirements: no memory-pattern planning and
        # sequential execution, or session creation fails on some graphs.
        options.enable_mem_pattern = False
        options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
    return options


def default_threads() -> int:
    """Half the logical processors, at most four - the speech model's measured
    setting (`transcribe.py`), so these models leave room for the indexer."""
    return min(4, max(1, (os.cpu_count() or 2) // 2))


#: File-name endings of the exports' dynamically quantised graphs.
_QUANTISED = ("_int8", "_uint8", "_quantized", "_q4", "_q4f16", "_bnb4")


def is_quantised(path: Path) -> bool:
    return Path(path).stem.endswith(_QUANTISED)


def is_cached_decoder(path: Path) -> bool:
    """A decoder that carries a key/value cache between steps (the exports' merged
    and with-past decoders)."""
    stem = Path(path).stem
    return stem.startswith(("decoder_model_merged", "decoder_with_past"))


def _processor_only(path: Path) -> str:
    """Why this graph must run on the processor, or "" when it need not."""
    if is_quantised(path):
        return "it is quantised"
    if is_cached_decoder(path):
        return "it is a decoder with a cache"
    return ""


def load_session(path: Path, *, what: str, device: str = "auto",
                 threads: Optional[int] = None, profile: Any = None,
                 optimise: str = "all") -> Loaded:
    """Open `path` on the processor `device` asks for, falling back to the CPU.

    `what` names the model in the run log ("photo tags", "speech", ...).
    Raises whatever onnxruntime raises when even the CPU cannot open the file;
    the caller turns that into its own plain-words error.

    **A quantised graph always runs on the processor.** Measured on the owner's
    laptop, 2026-09-29: Florence-2's int8 graphs opened on DirectML without an
    error and then produced nonsense ("wouldn are The", a page of random
    words) at 17-48 s a photo, where the same graphs on the processor gave the
    torch path's captions and identical tags at 11-14 s. DirectML does not
    carry the dynamic-quantisation operators faithfully, and a wrong answer
    that looks like an answer is worse than a slow one.

    **So does a decoder with a cache.** Measured the same day on whisper-base,
    full precision: on DirectML the merged decoder's first step (empty cache)
    matched the processor to 6e-5, and every later step returned NaN and
    values near 1e38 - a page of symbols. Encoders and Florence-2's vision
    graph have no cache and may use the graphics card; the decoders cannot.

    `optimise="basic"` is for the chat model: at ONNX Runtime's full graph
    optimisation its cached step chose a different word from recomputing the
    same text from scratch ("phrase" for "three", 2026-09-30, Qwen2.5-1.5B int8,
    processor) - a fused attention kernel that mishandles the cache. At
    `basic` the two agree. Florence-2 and Whisper were checked at `all`.
    """
    import onnxruntime as ort

    from app.index import backends

    reason = _processor_only(path)
    if reason and str(device or "auto").lower() != backends.CPU:
        device = backends.CPU
        _log.debug("{}: {} runs on the processor because {}", what, Path(path).name, reason)
    wanted = backends.choose(profile if profile is not None else machine_profile(), device,
                             available=ort.get_available_providers())
    count = default_threads() if threads is None else threads

    def build(providers: tuple[str, ...]) -> Any:
        return ort.InferenceSession(str(path), sess_options=_options(providers, count, optimise),
                                    providers=list(providers))

    with gpu_exclusive(wanted.is_gpu):
        session, choice = backends.with_fallback(build, wanted)
    backends.record_provider(what, choice)
    _log.debug("{} opened {} on {}", what, Path(path).name, choice.device)
    return Loaded(session=session, choice=choice)
