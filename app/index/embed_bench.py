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

__all__ = ["BenchResult", "inspect_model", "providers", "run_benchmark", "project"]


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
    available_providers: list[str] = field(default_factory=list)
    threads: Optional[int] = None
    #: tokens -> chunks per second, measured.
    throughput: dict[int, float] = field(default_factory=dict)
    error: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "model": self.model_name,
            "file": self.model_file,
            "size_mb": round(self.model_mb, 1),
            "weight_types": self.weight_types,
            "quantised": self.quantised,
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
        candidates = sorted(
            Path(cache_dir).rglob("*.onnx"), key=lambda p: p.stat().st_size, reverse=True
        )
    except OSError as exc:
        result.error = f"could not read the model cache: {exc}"
        return result

    if not candidates:
        result.error = (
            f"no .onnx model found under {cache_dir}. "
            "Run a search or an index once so the model downloads."
        )
        return result

    path = candidates[0]
    result.model_file = str(path)
    result.model_mb = path.stat().st_size / 1e6

    try:
        import collections

        import onnx
    except ImportError:
        result.error = ("onnx is not installed, so the weight types cannot be read. "
                        "The size above is still a strong hint: fp32 bge-small is ~133MB.")
        return result

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
        started = time.perf_counter()
        list(model.embed(texts))
        elapsed = time.perf_counter() - started
        result.throughput[tokens] = batch / elapsed if elapsed else 0.0

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
