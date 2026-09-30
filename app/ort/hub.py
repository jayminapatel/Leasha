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

__all__ = ["OnnxModel", "FLORENCE", "FLORENCE_INT8", "WHISPER_BASE", "QWEN_1_5B", "QWEN_1_5B_Q4",
           "MODELS", "by_key",
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
    #: The pinned repository revision (2026-09-30, `catalogue.json`); "" means
    #: whatever snapshot is on disk. Downloads fetch exactly this revision.
    revision: str = ""

    def graph_file(self, graph: str) -> str:
        return f"onnx/{graph}{self.suffix}.onnx"

    def files(self) -> tuple[str, ...]:
        return tuple(self.graph_file(g) for g in self.graphs) + self.required + self.extra


#: Florence-2 photo tags. **Full precision** (2026-09-30, measured on the owner's
#: laptop, same four photos): 3.6-5.8 s a photo with the vision graph on the
#: graphics card and 7.8-8.7 s all on the processor, against 11.4-13.6 s for
#: int8 - which runs on the processor only (`session.py`). Captions on the
#: graphics card matched the processor's word for word. About 1 GB to fetch.
FLORENCE = OnnxModel(
    key="florence-2-base", repo="onnx-community/Florence-2-base",
    label="Florence-2 base (photo tags and captions)",
    graphs=("vision_encoder", "embed_tokens", "encoder_model", "decoder_model_merged"),
    suffix="",
    required=("config.json", "generation_config.json", "tokenizer.json"),
    approx_mb=1035, licence="MIT")

#: The int8 copy (260 MB) - used when it is what is on disk.
FLORENCE_INT8 = OnnxModel(
    key="florence-2-base-int8", repo="onnx-community/Florence-2-base",
    label="Florence-2 base, smaller copy (photo tags and captions)",
    graphs=FLORENCE.graphs, suffix="_int8", required=FLORENCE.required,
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

#: The 4-bit copy (weights only, activations in full precision - how Ollama's
#: `qwen2.5:1.5b` is quantised). 2026-09-30: the int8 copy above quantises the
#: activations on every step, and on the owner's laptop it answered Interpret
#: with prose where Ollama's copy of the same model gave a query. Preferred when
#: it is on disk; **not yet measured** - order 1b item 7 decides whether it stays.
QWEN_1_5B_Q4 = OnnxModel(
    key="qwen2.5-1.5b-instruct-q4", repo="onnx-community/Qwen2.5-1.5B-Instruct",
    label="Qwen 2.5 1.5B Instruct, 4-bit (chat, Interpret)",
    graphs=("model",), suffix="_q4", required=QWEN_1_5B.required,
    approx_mb=1705, licence="Apache-2.0")

MODELS: tuple[OnnxModel, ...] = (FLORENCE, FLORENCE_INT8, WHISPER_BASE, QWEN_1_5B, QWEN_1_5B_Q4)


def resolve_any(models: tuple[OnnxModel, ...], cache_dir: Optional[Path]
                ) -> Optional[tuple[OnnxModel, Path]]:
    """The first of `models` (best first) that is complete in the cache. Offline."""
    for model in models:
        folder = resolve(model, cache_dir)
        if folder is not None:
            return model, folder
    return None


def by_key(key: str) -> Optional[OnnxModel]:
    """A model by key: the catalogue's entry (with its pinned revision) when it has
    one, else the built-in definition above."""
    wanted = str(key or "").strip()
    try:
        from app.ort.catalogue import load

        entry = load().by_key(wanted)
        if entry is not None:
            return entry.model()
    except Exception:                                  # noqa: BLE001 - fall back to built-ins
        pass
    return next((m for m in MODELS if m.key == wanted), None)


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
    snapshots = _snapshots(model.repo, Path(cache_dir))
    if model.revision:
        # The pinned revision first; another complete snapshot still serves.
        snapshots.sort(key=lambda p: p.name != model.revision)
    for snapshot in snapshots:
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
                                  allow_patterns=patterns, revision=model.revision or None))
