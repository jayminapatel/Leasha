r"""Order 202626270114 (0b) section 6h - quantising the embedding model
locally.

Layer: L3

`quantize_model.py`'s own docstring has the reasoning: fastembed's own
catalogue ships one precision for `BAAI/bge-small-en-v1.5` and it is fp16,
not int8; int8 is a real further gain, and getting it from the file
already downloaded and trusted - never someone else's build - is the whole
point of this module.

Mechanics are tested here against a tiny synthetic ONNX graph, not the real
33M-parameter model: quantising a linear layer and quantising BGE exercise
the same `onnxruntime.quantization.quantize_dynamic` call, and a synthetic
graph is fast and needs no network. `test_embedder_quantised_wiring.py`
covers the real model, marked slow.
"""

from __future__ import annotations

import pytest

pytest.importorskip("onnx")
# Not `importorskip`: that skips on `ImportError` only. `onnxruntime.quantization`
# imports torch when torch is installed, and a torch whose DLL Windows' Smart App
# Control blocks raises `OSError` - which made this whole file a collection error
# on the owner's laptop (2026-09-29) instead of a skip that says why.
try:
    import onnxruntime.quantization  # noqa: F401
except (ImportError, OSError) as exc:
    pytest.skip(f"onnxruntime.quantization cannot load here: {exc}",
                allow_module_level=True)

from app.index.quantize_model import quantised_model_dir


def _fake_description(model: str, model_file: str) -> dict:
    """The shape `TextEmbedding.list_supported_models()` actually returns:
    a plain dict, not the dataclass `download_model` itself needs -
    `quantize_model.py`'s own `_find_description` bridges the two, and this
    fixture exercises that bridge rather than bypassing it."""
    return {
        "model": model, "model_file": model_file,
        "sources": {"hf": model, "url": None, "_deprecated_tar_struct": False},
        "description": "", "license": "", "size_in_GB": 0.0,
        "additional_files": [], "dim": 4, "tasks": {},
    }


def _write_tiny_onnx_model(path) -> None:
    """A one-node linear graph, float32 weights - small enough to quantise
    in milliseconds, real enough that `quantize_dynamic` has an actual
    weight tensor to convert."""
    import numpy as np
    import onnx
    from onnx import TensorProto, helper

    weight = helper.make_tensor(
        "W", TensorProto.FLOAT, [4, 4], np.eye(4, dtype=np.float32).flatten().tolist())
    node = helper.make_node("MatMul", ["X", "W"], ["Y"])
    graph = helper.make_graph(
        [node], "tiny",
        [helper.make_tensor_value_info("X", TensorProto.FLOAT, [1, 4])],
        [helper.make_tensor_value_info("Y", TensorProto.FLOAT, [1, 4])],
        [weight],
    )
    model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 13)])
    onnx.save(model, str(path))


def _has_an_int8_weight(path) -> bool:
    """`quantize_dynamic` adds its own scale/zero-point initializers
    alongside the converted weight and may rename it, so this checks the
    graph as a whole for any 8-bit tensor rather than one name."""
    import onnx

    model = onnx.load(str(path), load_external_data=False)
    dtypes = {onnx.TensorProto.DataType.Name(i.data_type) for i in model.graph.initializer}
    return bool(dtypes & {"INT8", "UINT8"})


@pytest.fixture
def fake_catalogue(tmp_path, monkeypatch):
    """Stand in for `TextEmbedding.list_supported_models`/`download_model`
    with a tiny local model, so this exercises `quantize_model.py`'s own
    logic without fastembed's real catalogue or a download."""
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    (source_dir / "config.json").write_text("{}", encoding="utf-8")
    (source_dir / "tokenizer.json").write_text("{}", encoding="utf-8")
    _write_tiny_onnx_model(source_dir / "model.onnx")

    description = _fake_description("test/tiny-model", "model.onnx")

    from fastembed import TextEmbedding

    monkeypatch.setattr(TextEmbedding, "list_supported_models",
                        staticmethod(lambda: [description]))
    monkeypatch.setattr(TextEmbedding, "download_model",
                        classmethod(lambda cls, desc, cache_dir, **kw: source_dir))
    return source_dir, description


def test_an_unknown_model_name_returns_none(tmp_path):
    assert quantised_model_dir("not/a/real-model", str(tmp_path)) is None


def test_a_quantised_copy_is_produced_with_int8_weights(tmp_path, fake_catalogue):
    source_dir, description = fake_catalogue

    result = quantised_model_dir("test/tiny-model", str(tmp_path))

    assert result is not None
    weight_path = result / description["model_file"]
    assert weight_path.is_file()
    # int8 or uint8 depending on onnxruntime's own calibration choice for
    # this tiny graph - either is "no longer float", which is the claim.
    assert _has_an_int8_weight(weight_path)


def test_the_tokenizer_and_config_files_travel_with_the_weights(tmp_path, fake_catalogue):
    result = quantised_model_dir("test/tiny-model", str(tmp_path))

    assert (result / "config.json").is_file()
    assert (result / "tokenizer.json").is_file()


def test_a_second_call_reuses_the_cached_directory_rather_than_requantising(
    tmp_path, fake_catalogue, monkeypatch
):
    first = quantised_model_dir("test/tiny-model", str(tmp_path))
    assert first is not None

    calls: list = []
    import app.index.quantize_model as module
    monkeypatch.setattr(module, "_quantise_into",
                        lambda **kw: calls.append(kw) or first)

    second = quantised_model_dir("test/tiny-model", str(tmp_path))

    assert second == first
    assert calls == [], "a cached, non-empty weight file must skip quantising again"


def test_missing_onnx_falls_back_to_none_rather_than_raising(tmp_path, fake_catalogue, monkeypatch):
    import builtins

    real_import = builtins.__import__

    def blocked(name, *args, **kwargs):
        if name == "onnxruntime.quantization" or name.startswith("onnxruntime.quantization"):
            raise ImportError("stand-in for a machine without onnx installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked)

    assert quantised_model_dir("test/tiny-model", str(tmp_path)) is None


def test_a_source_file_that_never_downloaded_returns_none(tmp_path, monkeypatch):
    description = _fake_description("test/never-downloaded", "model.onnx")
    missing_dir = tmp_path / "never-existed"

    from fastembed import TextEmbedding

    monkeypatch.setattr(TextEmbedding, "list_supported_models",
                        staticmethod(lambda: [description]))
    monkeypatch.setattr(TextEmbedding, "download_model",
                        classmethod(lambda cls, desc, cache_dir, **kw: missing_dir))

    assert quantised_model_dir("test/never-downloaded", str(tmp_path)) is None
