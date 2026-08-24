r"""Measure what embedding actually costs on this machine, and why.

Layer: L3

Written because an estimate was wrong. The observed 1.53 passages/second was
called "20 to 60 times too slow" on the assumption that a 33M-parameter model
should manage tens per second - which is true for short sentences and false for
the 512-token passages this application actually embeds. The arithmetic for a
512-token transformer predicts 1.5/sec on a typical CPU, so nothing was broken.

**The lesson is the module.** A throughput number without the sequence length
beside it means nothing, and a projection built on the wrong number sends
somebody optimising the wrong thing for a week. So this measures rather than
predicts, and it reports the three facts that decide what to do next:

* **Is the model already quantised?** fastembed ships `model_optimized.onnx`,
  and if its weights are already int8 then "switch to a quantised model" is
  advice worth nothing. This reads the weight dtypes out of the file.
* **What can onnxruntime see?** A DirectML or CUDA provider present but unused
  is the largest single win available, and it is invisible from the outside.
* **How many threads is it using?** The default is usually every core, but a
  session pinned to one would look exactly like a slow model.

Then it times real embedding at several sequence lengths, so the cost curve is
measured on this machine rather than inferred from FLOP counts.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

__all__ = [
    "BenchResult", "inspect_model", "providers", "run_benchmark",
    "project", "precision_of",
]


@dataclass
class BenchResult:
    """Everything measured, in one object that can be printed or serialised."""

    model_name: str = ""
    model_file: str = ""
    model_mb: float = 0.0
    #: Weight dtype -> how many values are stored in it. The question this
    #: answers is "is it already int8", which decides a whole line of work.
    weight_types: dict[str, int] = field(default_factory=dict)
    quantised: Optional[bool] = None
    #: "fp32" | "fp16" | "int8" | None. More useful than the flag above,
    #: because fp16 is a real answer and "not int8" is not.
    precision: Optional[str] = None
    available_providers: list[str] = field(default_factory=list)
    threads: Optional[int] = None
    #: tokens -> chunks per second. The **median** of several passes, because a
    #: single pass swung 44% between two runs on the same machine and there was
    #: no way to tell a real number from four seconds of background load.
    throughput: dict[int, float] = field(default_factory=dict)
    #: tokens -> (slowest, fastest) seen. A wide spread means the machine was
    #: busy and the number should not be trusted, which the report says out loud
    #: rather than leaving somebody to compare two runs by eye.
    spread: dict[int, tuple[float, float]] = field(default_factory=dict)
    error: str = ""

    @property
    def unstable(self) -> list[int]:
        """Sequence lengths whose measurement varied by more than a quarter."""
        return [
            tokens for tokens, (low, high) in self.spread.items()
            if high > 0 and (high - low) / high > 0.25
        ]

    def as_dict(self) -> dict[str, Any]:
        return {
            "model": self.model_name,
            "file": self.model_file,
            "size_mb": round(self.model_mb, 1),
            "weight_types": self.weight_types,
            "quantised": self.quantised,
            "precision": self.precision,
            "spread": {k: [round(v[0], 2), round(v[1], 2)] for k, v in self.spread.items()},
            "unstable": self.unstable,
            "providers": self.available_providers,
            "threads": self.threads,
            "throughput_per_sec": {k: round(v, 2) for k, v in self.throughput.items()},
            "error": self.error,
        }


def inspect_model(cache_dir: Path, result: BenchResult) -> BenchResult:
    """Read the ONNX file's weight dtypes. Never raises.

    A 33M-parameter model stored in 67MB is not fp32 - fp32 would be 133MB - so
    the size alone is a hint, but only the initializer dtypes are proof. Getting
    this wrong in either direction wastes real time: recommending int8 when it is
    already int8 achieves nothing, and ruling it out when it is fp32 leaves a
    two-to-four-times speed-up on the table.
    """
    try:
        candidates = list(Path(cache_dir).rglob("*.onnx"))
    except OSError as exc:
        result.error = f"could not read the model cache: {exc}"
        return result

    if not candidates:
        result.error = (
            f"no .onnx model found under {cache_dir}. "
            "Run a search or an index once so the model downloads."
        )
        return result

    # **Matched to the model being timed, not simply the largest file.**
    #
    # The first version took the biggest `.onnx` under the cache, and the cache
    # also holds the *reranker* - a 109M-parameter model beside a 33M one. So it
    # confidently reported "model.onnx 1112MB" for an embedder whose fp32 build
    # is 133MB, and then timed a completely different file. A diagnostic that
    # measures one thing and describes another is worse than no diagnostic,
    # because it is believed.
    slug = result.model_name.split("/")[-1].lower().replace("-", "").replace("_", "")
    matched = [
        p for p in candidates
        if slug in str(p.parent).lower().replace("-", "").replace("_", "")
    ]
    if not matched:
        result.error = (
            f"found {len(candidates)} model file(s) under {cache_dir}, but none in a "
            f"folder naming {result.model_name}. Sizes are not comparable across "
            "models, so nothing is reported rather than reporting the wrong one."
        )
        return result

    # Within the right model's folder, the weights are the largest file.
    path = max(matched, key=lambda p: p.stat().st_size)
    result.model_file = str(path)
    result.model_mb = path.stat().st_size / 1e6

    # Size against the known parameter count answers the question well enough
    # without a dependency: fp32 is four bytes per weight, int8 is one. The
    # dtypes below are exact when `onnx` happens to be installed, but the
    # inference is what makes this useful on a machine where it is not - and
    # "should I switch to a quantised model" is a question worth answering
    # without asking anybody to install anything.
    result.precision = precision_of(result.model_name, result.model_mb)
    result.quantised = None if result.precision is None else result.precision == "int8"

    try:
        import collections

        import onnx
    except ImportError:
        return result                            # the inference above stands

    try:
        model = onnx.load(str(path), load_external_data=False)
    except Exception as exc:                     # noqa: BLE001 - a diagnostic never fails
        result.error = f"could not parse the model: {type(exc).__name__}: {exc}"
        return result

    names = {getattr(onnx.TensorProto, n): n for n in
             ("FLOAT", "FLOAT16", "BFLOAT16", "INT8", "UINT8", "INT32", "INT64")}
    counts: collections.Counter = collections.Counter()
    for initializer in model.graph.initializer:
        label = names.get(initializer.data_type, f"type_{initializer.data_type}")
        size = 1
        for dimension in initializer.dims:
            size *= dimension
        counts[label] += size

    result.weight_types = dict(counts)
    quantised_values = counts.get("INT8", 0) + counts.get("UINT8", 0)
    float_values = counts.get("FLOAT", 0) + counts.get("FLOAT16", 0)
    # Judged by weight *volume*, not tensor count: a mostly-int8 model still has
    # a handful of float scales and biases, and counting tensors would call it
    # float.
    if quantised_values or float_values:
        result.quantised = quantised_values > float_values
    return result


#: Parameter counts for the models this application ships with, so a file size
#: can be read as a precision. Written out rather than derived, because deriving
#: it needs the model config, which needs the model loaded, which is the cost
#: this avoids.
_PARAMS_M = {
    "bge-small-en-v1.5": 33,
    "bge-small-en": 33,
    "bge-base-en-v1.5": 109,
    "bge-base-en": 109,
    "bge-reranker-base": 109,
}


def precision_of(model_name: str, size_mb: float) -> Optional[str]:
    """Read a file size as a precision: "fp32", "fp16", "int8", or None.

    A *label* rather than a quantised/not flag, because the middle case is real
    and the flag could not express it. This project's own model turned out to be
    a 66MB build of a 33M-parameter network - two bytes a weight, so fp16 - and
    a tri-state boolean reported that as "unclear", which is exactly the wrong
    answer: it is perfectly clear, and it means int8 is still available and worth
    roughly another two times.

    Four bytes a weight is fp32, two is fp16, one is int8. The bands are generous
    because an ONNX file carries a graph and metadata as well as weights, but the
    gaps between 133MB, 66MB and 33MB are far wider than any overhead.
    """
    key = model_name.split("/")[-1].lower()
    params_m = _PARAMS_M.get(key)
    if not params_m or size_mb <= 0:
        return None
    bytes_per_weight = size_mb / params_m
    if bytes_per_weight >= 3.0:
        return "fp32"
    if bytes_per_weight >= 1.7:
        return "fp16"
    return "int8"


def _infer_quantised(model_name: str, size_mb: float) -> Optional[bool]:
    """`precision_of` as a flag, for callers that only need "is it int8"."""
    precision = precision_of(model_name, size_mb)
    return None if precision is None else precision == "int8"


def providers(result: BenchResult) -> BenchResult:
    """What onnxruntime can actually use here.

    A DirectML or CUDA provider installed but unused is the single largest
    available win, and there is no way to see it from the outside - the app
    would simply be slow with no explanation.
    """
    try:
        import onnxruntime

        result.available_providers = list(onnxruntime.get_available_providers())
    except ImportError:
        result.error = result.error or "onnxruntime is not importable"
    return result


#: Roughly four characters per token for English prose. Close enough to hit a
#: target sequence length, and the tokeniser truncates anything over.
_CHARS_PER_TOKEN = 4


def run_benchmark(
    model_name: str,
    cache_dir: Path,
    *,
    lengths: tuple[int, ...] = (128, 256, 512),
    batch: int = 32,
    passes: int = 3,
    result: Optional[BenchResult] = None,
) -> BenchResult:
    """Time real embedding at several sequence lengths.

    The model load is deliberately excluded: it happens once per run and would
    otherwise be smeared across the first batch, which is exactly how the
    original "59 passages/min" figure came to look worse than the steady state.
    """
    result = result or BenchResult(model_name=model_name)
    result.model_name = model_name

    try:
        from fastembed import TextEmbedding
    except ImportError:
        result.error = "fastembed is not installed"
        return result

    try:
        model = TextEmbedding(model_name=model_name, cache_dir=str(cache_dir))
        list(model.embed(["warm up the session and pay the load cost once"]))
    except Exception as exc:                     # noqa: BLE001
        result.error = f"could not load the model: {type(exc).__name__}: {exc}"
        return result

    result.threads = _session_threads(model)

    word = "settlement "                          # a real-ish token, not "aaa"
    for tokens in lengths:
        text = (word * (tokens * _CHARS_PER_TOKEN // len(word) + 1))[:tokens * _CHARS_PER_TOKEN]
        texts = [text] * batch

        # **Several passes, and the median.** One pass gave 4.42 and then 2.47
        # for the same length on the same machine - a 44% swing, because a
        # background task for four seconds is indistinguishable from a slow
        # model when you only look once. The median ignores a single bad pass;
        # the spread says whether to believe any of it.
        rates: list[float] = []
        for _ in range(passes):
            started = time.perf_counter()
            list(model.embed(texts))
            elapsed = time.perf_counter() - started
            rates.append(batch / elapsed if elapsed else 0.0)

        rates.sort()
        result.throughput[tokens] = rates[len(rates) // 2]
        result.spread[tokens] = (rates[0], rates[-1])

    return result


def _session_threads(model: Any) -> Optional[int]:
    """The intra-op thread count onnxruntime settled on, if it can be found.

    Dug out rather than assumed, because a session pinned to one thread looks
    exactly like a slow model from the outside, and the two have completely
    different fixes.
    """
    for attribute in ("model", "_model"):
        inner = getattr(model, attribute, None)
        session = getattr(inner, "session", None) or getattr(inner, "model", None)
        options = getattr(session, "get_session_options", None)
        if callable(options):
            try:
                return int(options().intra_op_num_threads) or os.cpu_count()
            except Exception:                    # noqa: BLE001
                return None
    return None


def project(chunks: int, per_second: float) -> dict[str, float]:
    """Hours for a corpus of `chunks` at a measured rate.

    Separated from the measurement so the projection can be recomputed for any
    corpus size without re-running the benchmark - and so the number that gets
    quoted always has a measured rate behind it rather than an estimate.
    """
    if per_second <= 0:
        return {"chunks": chunks, "hours": float("inf")}
    return {"chunks": chunks, "hours": chunks / per_second / 3600}
