r"""Where each ONNX model's files are, and how they are fetched.

Layer: L2.

**Looking is offline; fetching is a person's choice.** `resolve` only reads the
model cache (`MODEL_CACHE`), in the same Hugging Face layout fastembed already
uses there (`models--org--name/snapshots/<rev>/...`), so indexing never goes to
the internet - the rule `transcribe.py` states for the speech model. `fetch` is
what a Download button calls, on a worker, and nothing else calls it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from app.core.logging import logger

__all__ = ["OnnxModel", "FLORENCE", "WHISPER_BASE", "QWEN_1_5B", "MODELS", "by_key",
           "resolve", "present", "fetch"]

_log = logger.bind(component="ort.hub")

#: Small files every export carries; fetched with whichever weights are chosen.
_SIDE_FILES = ("config.json", "generation_config.json", "preprocessor_config.json",
               "processor_config.json", "tokenizer.json", "tokenizer_config.json",
               "special_tokens_map.json", "added_tokens.json", "normalizer.json")


@dataclass(frozen=True)
class OnnxModel:
    """One model: where it comes from, and the ONNX files it runs."""

    key: str                       # what settings store, e.g. "florence-2-base"
    repo: str                      # onnx-community/...
    label: str                     # what a person reads
    graphs: tuple[str, ...]        # onnx/<graph><suffix>.onnx
    suffix: str = "_int8"          # which precision; "" is full precision
    required: tuple[str, ...] = ("config.json", "tokenizer.json")
    approx_mb: int = 0
    licence: str = ""
    extra: tuple[str, ...] = field(default_factory=tuple)

    def graph_file(self, graph: str) -> str:
        return f"onnx/{graph}{self.suffix}.onnx"

    def files(self) -> tuple[str, ...]:
        return tuple(self.graph_file(g) for g in self.graphs) + self.required + self.extra


#: Florence-2 photo tags. int8: the full-precision graphs are four times the
#: size, and on a processor int8 is the faster of the two (see `florence.py`
#: for what was measured on the owner's laptop).
FLORENCE = OnnxModel(
    key="florence-2-base", repo="onnx-community/Florence-2-base",
    label="Florence-2 base (photo tags and captions)",
    graphs=("vision_encoder", "embed_tokens", "encoder_model", "decoder_model_merged"),
    required=("config.json", "generation_config.json", "tokenizer.json"),
    approx_mb=260, licence="MIT")

#: Whisper speech. Full precision by default: the int8 decoder is the one part
#: of Whisper most sensitive to quantisation, and the whole model is 280 MB.
WHISPER_BASE = OnnxModel(
    key="whisper-base", repo="onnx-community/whisper-base",
    label="Whisper base (speech to text)",
    graphs=("encoder_model", "decoder_model_merged"), suffix="",
    required=("config.json", "generation_config.json", "tokenizer.json"),
    approx_mb=280, licence="MIT")

#: The chat model: the same model Interpret already used through Ollama
#: (qwen2.5:1.5b), Apache-2.0, so it can ship with a product that is sold.
QWEN_1_5B = OnnxModel(
    key="qwen2.5-1.5b-instruct", repo="onnx-community/Qwen2.5-1.5B-Instruct",
    label="Qwen 2.5 1.5B Instruct (chat, Interpret)",
    graphs=("model",), suffix="_int8",
    required=("config.json", "generation_config.json", "tokenizer.json",
              "tokenizer_config.json"),
    approx_mb=1510, licence="Apache-2.0")

MODELS: tuple[OnnxModel, ...] = (FLORENCE, WHISPER_BASE, QWEN_1_5B)


def by_key(key: str) -> Optional[OnnxModel]:
    return next((m for m in MODELS if m.key == str(key or "").strip()), None)


def _snapshots(repo: str, cache_dir: Path) -> list[Path]:
    root = Path(cache_dir) / ("models--" + repo.replace("/", "--")) / "snapshots"
    try:
        return sorted((p for p in root.iterdir() if p.is_dir()),
                      key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return []


def resolve(model: OnnxModel, cache_dir: Optional[Path]) -> Optional[Path]:
    """The folder holding every file `model` needs, or None. Offline; never raises."""
    if not cache_dir:
        return None
    for snapshot in _snapshots(model.repo, Path(cache_dir)):
        try:
            if all((snapshot / name).is_file() for name in model.files()):
                return snapshot
        except OSError:
            continue
    return None


def present(model: OnnxModel, cache_dir: Optional[Path]) -> bool:
    return resolve(model, cache_dir) is not None


def fetch(model: OnnxModel, cache_dir: Path, *,
          should_stop: Optional[Callable[[], bool]] = None) -> Path:
    """Download `model` into the cache. For a Download button's worker only.

    Fetches only this model's graphs at its precision, plus the small side
    files - not the dozen other precisions each repository also holds.
    """
    from huggingface_hub import snapshot_download

    if should_stop is not None and should_stop():
        raise InterruptedError("stopped before the download started")
    patterns = list(model.files()) + list(_SIDE_FILES)
    _log.info("downloading {} ({} files, about {} MB) from {}", model.key, len(patterns),
              model.approx_mb, model.repo)
    return Path(snapshot_download(model.repo, cache_dir=str(cache_dir),
                                  allow_patterns=patterns))
