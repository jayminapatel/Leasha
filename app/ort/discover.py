r"""Update the model list from Hugging Face: every model Leasha's runners can load today.

Layer: L1 (network metadata and a JSON file; no Qt, no model loading). Runs only
when a person presses "Update the list from Hugging Face" in Settings, Models -
never by itself. Nothing is downloaded but file lists and small settings files.

Owner, 2026-09-30: "can this list not be dynamic from hugging face ... there is
a copy but a button to update local list". The shipped `catalogue.json` holds the
**verified** models; this finds the rest and saves them beside it
(`catalogue.discovered_path`), marked "huggingface" and never "verified", so they
are offered with "not checked on a real machine" and never recommended.

**Compatible means a runner can load it**, decided from the files, not the name:

* `florence` - the four Florence-2 graphs (full and/or int8).
* `whisper` - `encoder_model` and `decoder_model_merged` (full and/or int8);
  English-only `.en` models are left out, as they always were.
* `decoder-chat` - one `onnx/model_q4.onnx` graph and a chat template in a format
  `app/ort/llm.py` speaks (ChatML, Llama 3, Gemma, Phi-3). **4-bit only**: the int8
  copy of Qwen answered Interpret wrongly on the owner's laptop and the 4-bit
  one did not; fp16 copies are slow on a processor.

Each entry is pinned to the revision seen now, so a later change upstream is a
new entry, never a silent swap. Only `onnx-community` is searched - the exports
the runners were written against.
"""

from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Optional

from app.core.logging import logger

__all__ = ["discover", "entry_for", "prompt_format_of", "AUTHOR"]

_log = logger.bind(component="ort.discover")

AUTHOR = "onnx-community"
API = "https://huggingface.co/api"
_FLORENCE = ("vision_encoder", "embed_tokens", "encoder_model", "decoder_model_merged")
_WHISPER = ("encoder_model", "decoder_model_merged")
_SIDE = ("config.json", "generation_config.json", "tokenizer.json")
_CHAT_SIDE = _SIDE + ("tokenizer_config.json",)


def _get_json(url: str, timeout: float) -> Any:
    with urllib.request.urlopen(url, timeout=timeout) as response:      # noqa: S310 - fixed host
        return json.load(response)


def _get_text(url: str, timeout: float) -> str:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310
            return response.read().decode("utf-8", "replace")
    except Exception:                                   # noqa: BLE001 - absent file
        return ""


def prompt_format_of(tokenizer_config: str) -> str:
    """Which chat format a model's own template uses, or "" if none we speak."""
    if "<|im_start|>" in tokenizer_config:
        return "chatml"
    if "<|start_header_id|>" in tokenizer_config:
        return "llama3"
    if "<start_of_turn>" in tokenizer_config:
        return "gemma"
    if "<|user|>" in tokenizer_config and "<|assistant|>" in tokenizer_config:
        return "phi3"
    return ""


def _describe(job: str, repo: str, card: dict, mb: int, downloads: int) -> str:
    base = card.get("base_model")
    base = base[0] if isinstance(base, list) and base else base
    languages = card.get("language")
    languages = [languages] if isinstance(languages, str) else (languages or [])
    what = {"photo": "Photo tags and captions (Florence-2)",
            "speech": "Speech to text (Whisper)",
            "chat": "Chat and Interpret"}[job]
    parts = [f"{what}: {repo.split('/', 1)[1]}"]
    if base:
        parts.append(f"built on {base}")
    if languages and job != "chat":
        parts.append("for " + ", ".join(str(x) for x in languages[:4]))
    text = ", ".join(parts) + f". About {mb:,} MB"
    if card.get("license"):
        text += f"; licence {card['license']}"
    return text + f"; {downloads:,} downloads. Not checked on a real machine."


def entry_for(info: dict, tokenizer_config: str = "") -> list[dict]:
    """Catalogue rows for one repository's metadata (`/api/models/<repo>?blobs=true`).
    Pure, so the rules above are testable without the network."""
    repo = str(info.get("id") or info.get("modelId") or "")
    sizes = {s["rfilename"]: int(s.get("size") or (s.get("lfs") or {}).get("size") or 0)
             for s in info.get("siblings") or [] if "rfilename" in s}
    card = info.get("cardData") or {}
    downloads = int(info.get("downloads") or 0)
    revision = str(info.get("sha") or "")
    name = repo.lower()
    rows: list[dict] = []

    def row(job: str, runner: str, graphs: tuple, suffix: str, required: tuple,
            extra: tuple = (), **more: Any) -> None:
        files = [f"onnx/{g}{suffix}.onnx" for g in graphs] + list(extra)
        if not all(f in sizes for f in files) or not all(r in sizes for r in required):
            return
        mb = round(sum(sizes[f] for f in files) / 2 ** 20)
        copy = {"": "full", "_int8": "int8", "_q4": "4-bit"}.get(suffix, suffix)
        rows.append({
            "key": f"hf:{repo}:{copy}", "job": job, "runner": runner, "rank": 5,
            "label": f"{repo.split('/', 1)[1]} ({copy})", "repo": repo, "revision": revision,
            "graphs": list(graphs), "suffix": suffix, "required": list(required),
            "extra": list(extra), "approx_mb": mb, "licence": str(card.get("license") or ""),
            "sha256": {}, "verified": None, "source": "huggingface", "downloads": downloads,
            "description": _describe(job, repo, card, mb, downloads), **more})

    if "florence-2" in name:
        for suffix in ("", "_int8"):
            row("photo", "florence", _FLORENCE, suffix, _SIDE)
    elif "whisper" in name:
        if ".en" in name:
            return []
        for suffix in ("", "_int8"):
            data = tuple(f for f in sizes if f.endswith(".onnx_data")
                         and f.startswith(tuple(f"onnx/{g}{suffix}.onnx" for g in _WHISPER)))
            row("speech", "whisper", _WHISPER, suffix, _SIDE, data)
    elif "onnx/model_q4.onnx" in sizes:
        fmt = prompt_format_of(tokenizer_config)
        if fmt:
            data = tuple(f for f in sizes if f.startswith("onnx/model_q4.onnx_data"))
            row("chat", "decoder-chat", ("model",), "_q4", _CHAT_SIDE, data, prompt_format=fmt)
    return rows


def _candidates(timeout: float) -> list[str]:
    found: set[str] = set()
    queries = ({"search": "whisper"}, {"search": "Florence-2"},
               {"pipeline_tag": "text-generation"})
    for query in queries:
        params = urllib.parse.urlencode({"author": AUTHOR, "limit": 1000, **query})
        for model in _get_json(f"{API}/models?{params}", timeout):
            found.add(str(model.get("id") or model.get("modelId")))
    return sorted(found)


def discover(state_dir: Path, *, timeout: float = 30.0,
             progress: Optional[Callable[[str], None]] = None,
             should_stop: Optional[Callable[[], bool]] = None) -> tuple[int, str]:
    """Find every compatible model now; save the list. Returns `(count, sentence)`.

    Never raises: a network failure is a sentence and the previous list is kept."""
    from app.ort.catalogue import FORMAT, discovered_path

    say = progress or (lambda _text: None)
    started = time.monotonic()
    try:
        say("Asking Hugging Face which models exist...")
        repos = _candidates(timeout)
    except Exception as exc:                            # noqa: BLE001 - said plainly
        return 0, (f"Could not reach Hugging Face ({type(exc).__name__}); the list you "
                   "already have is unchanged.")
    rows: list[dict] = []
    for index, repo in enumerate(repos, 1):
        if should_stop is not None and should_stop():
            return 0, "Stopped; the list you already have is unchanged."
        say(f"Looking at {index} of {len(repos)}: {repo}")
        try:
            info = _get_json(f"{API}/models/{repo}?blobs=true", timeout)
            template = ""
            if any(s.get("rfilename") == "onnx/model_q4.onnx" for s in info.get("siblings") or []):
                template = _get_text(f"https://huggingface.co/{repo}/resolve/main/"
                                     "tokenizer_config.json", timeout)
            rows.extend(entry_for(info, template))
        except Exception as exc:                        # noqa: BLE001 - one repository, not the list
            _log.debug("skipped {}: {}", repo, exc)
    path = discovered_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"format": FORMAT, "version": time.strftime("%Y-%m-%d"),
                                "note": "Found by 'Update the list from Hugging Face'. "
                                        "Not checked on a real machine.",
                                "models": rows}, indent=1), encoding="utf-8")
    jobs = {job: sum(1 for r in rows if r["job"] == job) for job in ("photo", "speech", "chat")}
    return len(rows), (f"Found {len(rows)} models Leasha can run ({jobs['chat']} chat, "
                       f"{jobs['speech']} speech, {jobs['photo']} photo) in "
                       f"{time.monotonic() - started:.0f} s. None of them is checked; the "
                       "recommended ones stay the defaults.")
