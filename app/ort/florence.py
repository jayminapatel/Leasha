r"""Florence-2 on ONNX Runtime: a caption and object tags for one photo.

Layer: L2.

The same model `florence_tagger.py` ran on torch (`microsoft/Florence-2-base`,
MIT), from the `onnx-community/Florence-2-base` export, in four graphs checked
on the owner's laptop on 2026-09-29:

    vision_encoder   pixel_values [b,3,768,768]         -> image_features [b,577,768]
    embed_tokens     input_ids                          -> inputs_embeds
    encoder_model    inputs_embeds, attention_mask      -> last_hidden_state
    decoder_model_merged  inputs_embeds, encoder_hidden_states, encoder_attention_mask,
                          past_key_values.N.{decoder,encoder}.{key,value} (6 layers,
                          12 heads x 64), use_cache_branch -> logits, present.*

**What the torch path did, kept exactly.** The image is resized to 768x768
bicubic, scaled to 0..1 and normalised with the ImageNet mean and spread
(`preprocessor_config.json`). The task token becomes its sentence prompt
(`<DETAILED_CAPTION>` -> "Describe in detail what is shown in the image.").
Encoder input is the image features followed by the embedded prompt.
Generation is greedy (the torch path passed `num_beams=1`) and keeps the rest
of the model's own generation config: the decoder starts at id 2, the first
token is forced to id 0, and no 3-gram may repeat.
"""

from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import numpy as np

from app.core.logging import logger
from app.ort import hub
from app.ort.generate import Decoder, force_first, generate, no_repeat_ngram
from app.ort.session import load_session

__all__ = ["OnnxFlorence", "TASK_PROMPTS", "clean_text", "od_labels", "pixel_values"]

_log = logger.bind(component="ort.florence")

#: From the export's own `preprocessor_config.json`; used when it is missing.
TASK_PROMPTS = {
    "<CAPTION>": "What does the image describe?",
    "<DETAILED_CAPTION>": "Describe in detail what is shown in the image.",
    "<MORE_DETAILED_CAPTION>": "Describe with a paragraph what is shown in the image.",
    "<OD>": "Locate the objects with category name in the image.",
    "<OCR>": "What is the text in the image?",
}
_MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32)
_STD = np.array([0.229, 0.224, 0.225], dtype=np.float32)
_SIDE = 768
_OD = re.compile(r"([^<>]+?)((?:<loc_\d+>){4,})")


def pixel_values(image: Any, *, side: int = _SIDE, mean: np.ndarray = _MEAN,
                 std: np.ndarray = _STD) -> np.ndarray:
    """`[1, 3, side, side]` float32, as Florence-2's CLIP image processor makes it."""
    from PIL import Image

    rgb = image.convert("RGB").resize((side, side), Image.Resampling.BICUBIC)
    array = np.asarray(rgb, dtype=np.float32) / 255.0
    array = (array - mean) / std
    return np.ascontiguousarray(array.transpose(2, 0, 1)[None, ...], dtype=np.float32)


def clean_text(text: str) -> str:
    """Florence-2's `pure_text` post-processing: drop the special tokens."""
    for token in ("</s>", "<s>", "<pad>"):
        text = text.replace(token, "")
    return text.strip()


def od_labels(text: str) -> tuple[str, ...]:
    """The object names in an `<OD>` answer, lowercased, first-seen order, no repeats.

    The answer is `label<loc_a><loc_b><loc_c><loc_d>label2<loc_...>...`; only
    the labels are kept - Leasha stores tags, not boxes.
    """
    labels = (match.group(1).strip().lower() for match in _OD.finditer(clean_text(text)))
    return tuple(dict.fromkeys(label for label in labels if label))


@dataclass
class _Config:
    heads: int
    head_dim: int
    decoder_start: int
    forced_bos: int
    eos: int
    no_repeat: int
    prompts: dict


def _read_config(folder: Path) -> _Config:
    def load(name: str) -> dict:
        try:
            return json.loads((folder / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    config = load("config.json")
    text = config.get("text_config", config)
    generation = load("generation_config.json")
    prompts = dict(TASK_PROMPTS)
    prompts.update(load("preprocessor_config.json").get("task_prompts_without_inputs", {}))
    heads = int(text.get("decoder_attention_heads", 12))
    return _Config(
        heads=heads, head_dim=int(text.get("d_model", 768)) // heads,
        decoder_start=int(generation.get("decoder_start_token_id",
                                         text.get("decoder_start_token_id", 2))),
        forced_bos=int(generation.get("forced_bos_token_id", 0)),
        eos=int(generation.get("eos_token_id", text.get("eos_token_id", 2))),
        no_repeat=int(generation.get("no_repeat_ngram_size", 3)),
        prompts=prompts,
    )


class OnnxFlorence:
    """Florence-2's four sessions and tokenizer, loaded once. Not thread-safe per call;
    `lock` is held around a whole image."""

    def __init__(self, folder: Path, *, model: hub.OnnxModel = hub.FLORENCE,
                 device: str = "auto") -> None:
        from tokenizers import Tokenizer

        self.folder = Path(folder)
        self.config = _read_config(self.folder)
        self.tokenizer = Tokenizer.from_file(str(self.folder / "tokenizer.json"))
        self.lock = threading.Lock()

        def open_graph(name: str) -> Any:
            return load_session(self.folder / model.graph_file(name), what="photo tags",
                                device=device)

        self.vision = open_graph("vision_encoder")
        self.embed = open_graph("embed_tokens")
        self.encoder = open_graph("encoder_model")
        decoder = open_graph("decoder_model_merged")
        self.decoder = Decoder(decoder.session, heads=self.config.heads,
                               head_dim=self.config.head_dim)
        # The vision graph is the part the graphics card can take (the decoder
        # stays on the processor, `session.py`), so this says where it ran.
        self.on_gpu = self.vision.on_gpu

    @classmethod
    def from_cache(cls, cache_dir: Optional[Path], *, device: str = "auto") -> Optional["OnnxFlorence"]:
        """Full precision when it is downloaded, the int8 copy otherwise."""
        found = hub.resolve_any((hub.FLORENCE, hub.FLORENCE_INT8), cache_dir)
        if found is None:
            return None
        model, folder = found
        return cls(folder, model=model, device=device)

    def _embed(self, ids: list[int]) -> np.ndarray:
        return self.embed.session.run(None, {"input_ids": np.array([ids], dtype=np.int64)})[0]

    def run_task(self, pixels: np.ndarray, task: str, *, max_new_tokens: int = 128) -> str:
        """The raw decoded answer (special tokens kept) for one task on one image."""
        prompt = self.config.prompts.get(task, task)
        prompt_ids = self.tokenizer.encode(prompt).ids
        image = self.vision.session.run(None, {"pixel_values": pixels})[0]
        joined = np.concatenate([image, self._embed(prompt_ids)], axis=1)
        mask = np.ones(joined.shape[:2], dtype=np.int64)
        encoded = self.encoder.session.run(None, {"inputs_embeds": joined,
                                                  "attention_mask": mask})[0]
        self.decoder.reset()

        def step(new_ids: list[int], _position: int) -> np.ndarray:
            return self.decoder.step({"inputs_embeds": self._embed(new_ids),
                                      "encoder_hidden_states": encoded,
                                      "encoder_attention_mask": mask})

        rules = [force_first(self.config.forced_bos)]
        if self.config.no_repeat > 0:
            rules.append(no_repeat_ngram(self.config.no_repeat))
        tokens = list(generate(step, prompt=[self.config.decoder_start],
                               max_new_tokens=max_new_tokens, eos=[self.config.eos],
                               rules=rules))
        return self.tokenizer.decode(tokens, skip_special_tokens=False)

    def caption_and_tags(self, image: Any, *, max_new_tokens: int = 128) -> tuple[str, tuple[str, ...]]:
        """`<DETAILED_CAPTION>` and `<OD>`, the two calls the torch path made."""
        pixels = pixel_values(image)
        with self.lock:
            caption = clean_text(self.run_task(pixels, "<DETAILED_CAPTION>",
                                               max_new_tokens=max_new_tokens))
            tags = od_labels(self.run_task(pixels, "<OD>", max_new_tokens=max_new_tokens))
        return caption, tags

    def describe(self, image: Any, *, max_new_tokens: int = 256) -> str:
        """A paragraph about the image - what Describe asks a vision model for."""
        pixels = pixel_values(image)
        with self.lock:
            return clean_text(self.run_task(pixels, "<MORE_DETAILED_CAPTION>",
                                            max_new_tokens=max_new_tokens))
