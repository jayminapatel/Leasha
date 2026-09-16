r"""Locally quantising the embedding model - order 0b section 6h.

Layer: L3 (index) - reads the already-downloaded, already-trusted model
fastembed cached; never fetches anyone else's build.

fastembed's own catalogue has one precision for `BAAI/bge-small-en-v1.5`:
`qdrant/bge-small-en-v1.5-onnx-q`, which despite its name turned out to be
fp16 - `app/index/embed_bench.py`'s own measurement (66MB against a
33M-parameter network, two bytes a weight) found this, not an assumption.
int8 is therefore still a real, roughly-two-times gain on a processor, and
the only two ways to get it are downloading somebody else's build (an
untrusted source this project's own rules refuse) or quantising the file
already downloaded and trusted, locally, with `onnxruntime.quantization` -
the path taken here.

**fastembed's own seam, not a workaround.** `specific_model_path` is a
documented constructor parameter of every fastembed text-embedding class:
"the specific path to the onnx model dir if it should be imported from
somewhere else." Handing it a directory shaped exactly like fastembed's own
cache entry - the same tokenizer files, the same weight filename, int8
weights instead of fp16 - means fastembed's own tokenization, pooling and
normalisation code runs unchanged; only the file backing the ONNX session
differs. Nothing here reimplements what a correctness bug could silently
break in every future search.

Cached once per model, under a sibling directory next to fastembed's own
cache entry, and reused after: quantising a 33M-parameter graph takes real
seconds, not something to repeat on every launch. Built into a temporary
directory and renamed into place only once it is complete, so a crash
mid-quantisation cannot leave a half-built directory that looks finished.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import Optional

from app.core.logging import logger

__all__ = ["quantised_model_dir"]

_log = logger.bind(component="index.quantize_model")

#: `fastembed`'s own convention (`OnnxTextEmbedding.__init__`'s
#: `model_file` default): the marker a completed quantised directory
#: carries, checked before quantising again and before handing the
#: directory back as ready.
_DIR_SUFFIX = "-int8-local"


def quantised_model_dir(model_name: str, cache_dir: str) -> Optional[Path]:
    """The directory of a locally-quantised int8 copy of `model_name`,
    quantising it once from the already-cached fp16/fp32 file if a
    quantised copy does not already exist.

    Returns `None` rather than raising when quantisation cannot happen -
    the `onnx` package is not installed, the model is not one fastembed's
    own catalogue describes, the source file is not yet cached, or anything
    else goes wrong. A caller falls back to the ordinary file, which is
    correct and merely not as fast: never a reason to fail to embed.
    """
    try:
        from fastembed import TextEmbedding
    except ImportError:
        return None

    description = _find_description(TextEmbedding, model_name)
    if description is None:
        _log.debug("{} is not in fastembed's own catalogue - nothing to quantise", model_name)
        return None

    target = Path(cache_dir) / (_slug(model_name) + _DIR_SUFFIX)
    weight_path = target / description.model_file
    if weight_path.is_file() and weight_path.stat().st_size > 0:
        return target

    try:
        source_dir = TextEmbedding.download_model(description, cache_dir=str(cache_dir))
    except Exception as exc:                          # noqa: BLE001 - never blocks embedding
        _log.debug("could not resolve {}'s own cached file to quantise: {}", model_name, exc)
        return None

    source_weight = Path(source_dir) / description.model_file
    if not source_weight.is_file():
        _log.debug("{} not found at {} - nothing to quantise yet", description.model_file,
                  source_weight)
        return None

    try:
        return _quantise_into(source_dir=Path(source_dir), weight_relpath=description.model_file,
                              target=target)
    except ImportError:
        _log.debug("the onnx package is not installed - the smaller model file stays off")
        return None
    except Exception as exc:                          # noqa: BLE001 - never blocks embedding
        _log.warning("quantising {} failed, falling back to the ordinary file: {}",
                    model_name, exc)
        return None


def _find_description(text_embedding_cls, model_name: str):
    """`TextEmbedding.list_supported_models()` - the public, documented
    listing - returns plain dicts (JSON-serialisable, presumably for
    display), but `download_model` needs the dataclass shape (attribute
    access: `model.sources.hf`, `model.model_file`, ...). Rebuilt from the
    dict's own fields rather than reaching for the private per-class
    listing method that happens to return the dataclass directly, so this
    keeps working if fastembed changes which internal class backs
    `TextEmbedding`.
    """
    from fastembed.common.model_description import DenseModelDescription, ModelSource

    for raw in text_embedding_cls.list_supported_models():
        if raw.get("model") != model_name:
            continue
        return DenseModelDescription(
            model=raw["model"],
            sources=ModelSource(**raw["sources"]),
            model_file=raw["model_file"],
            description=raw.get("description", ""),
            license=raw.get("license", ""),
            size_in_GB=raw.get("size_in_GB", 0.0),
            additional_files=raw.get("additional_files", []),
            dim=raw.get("dim", 0),
            tasks=raw.get("tasks", {}),
        )
    return None


def _slug(model_name: str) -> str:
    return model_name.replace("/", "--")


def _quantise_into(*, source_dir: Path, weight_relpath: str, target: Path) -> Path:
    """Copy every file `source_dir` has, quantise the one weight file, and
    rename the result into `target` only once it is whole.

    A plain directory copy first, rather than quantising in place and
    copying after, because `quantize_dynamic` needs a real path to read
    from and a real path to write to - it cannot quantise into the copy
    destination directly without a source file already sitting there.
    """
    import onnx
    from onnxruntime.quantization import QuantType, quantize_dynamic

    with tempfile.TemporaryDirectory(prefix="leasha-quantise-") as scratch:
        staging = Path(scratch) / "staged"
        shutil.copytree(source_dir, staging)
        weight_path = staging / weight_relpath
        # **fp32 first, always.** `app/index/embed_bench.py`'s own
        # measurement found this project's cached file is fp16, not fp32 -
        # and `quantize_dynamic` run directly on an fp16 graph produced an
        # invalid one here (a `DequantizeLinear` node whose output ONNX
        # still typed `float16`, over data that was now `float32`,
        # because the graph's own `value_info` had cached the old dtype
        # and ONNX shape inference does not overwrite an entry that is
        # already present). Clearing `value_info` rather than trying to
        # refresh it - proven directly, both ways, against this model -
        # forces every consumer to re-derive each tensor's type from the
        # (now-correct) data instead of trusting a stale annotation.
        _upconvert_fp16_initializers_to_fp32(weight_path)
        quantize_dynamic(
            model_input=str(weight_path),
            model_output=str(weight_path),
            weight_type=QuantType.QInt8,
            # The embedding tables' own `Gather` output has no static
            # shape-inferred type once `value_info` is cleared; this is
            # the documented escape hatch quantize_dynamic's own error
            # names for exactly that case, not a guess.
            extra_options={"DefaultTensorType": onnx.TensorProto.FLOAT},
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            shutil.rmtree(target)
        shutil.move(str(staging), str(target))

    _log.info("quantised {} to int8, cached at {}", weight_relpath, target)
    return target


def _upconvert_fp16_initializers_to_fp32(weight_path: Path) -> None:
    """Rewrite every fp16 weight in the ONNX file at `weight_path` to fp32,
    in place, and drop the graph's own `value_info` - proven directly
    against this project's cached model: shape inference (`onnx.shape_
    inference.infer_shapes`) leaves an existing `value_info` entry alone
    rather than refreshing it, so a node whose output was declared
    `float16` before this conversion stays declared `float16` after it,
    while the data flowing through it is now `float32` - a mismatch
    onnxruntime's loader refuses outright. Clearing the stale annotations
    instead of trying to update them forces every consumer to re-derive
    each tensor's type from the corrected data.
    """
    import onnx
    from onnx import numpy_helper

    model = onnx.load(str(weight_path), load_external_data=True)
    changed = False
    for initializer in model.graph.initializer:
        if initializer.data_type == onnx.TensorProto.FLOAT16:
            array = numpy_helper.to_array(initializer).astype("float32")
            initializer.CopyFrom(numpy_helper.from_array(array, name=initializer.name))
            changed = True
    if changed:
        del model.graph.value_info[:]
        onnx.save(model, str(weight_path))
