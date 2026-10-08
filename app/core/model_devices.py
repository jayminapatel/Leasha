r"""Which processor each model runs on - chosen per model, measured per machine.

Layer: L0

2026-10-04, the owner: "put the gpu flag in the settings and also have a test
button so this can be tested on this machine and a new machine". Measured the
same night: Florence-2 took 8.9 s a photo on this laptop's processor and 3.9 s
on its Intel Iris Xe through DirectML, with the same description - while the
decoder inside it is kept on the processor anyway. A graphics card that helps
one model can be no faster, or wrong, for another, so each model has its own
choice:

* **Automatic** (`auto`) - what the last test on *this machine* measured as
  faster and correct (`device_test.json` in `STATE_PATH`, keyed by the
  machine's fingerprint, so an index folder carried to another computer does
  not keep this one's answer); until a test has run, whatever "Run models on"
  (`EMBED_DEVICE`) says, exactly as before.
* **Processor** / **Graphics card** - as chosen, always.

The models' own constructors still decide the details (`backends.choose`
refuses a graphics card the machine cannot use, and a driver that failed this
session is not asked again); this only says which one each model asks for.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

__all__ = ["MODELS", "CHOICES", "device_for", "load_results", "save_results",
           "results_path", "needs_test", "machine_fingerprint", "setting_key"]

#: `(model, setting key, what the person reads)`, in the order the table shows.
MODELS: tuple[tuple[str, str, str], ...] = (
    ("meaning", "DEVICE_MEANING", "Meaning model"),
    ("rerank", "DEVICE_RERANK", "Search reranker"),
    ("ocr", "DEVICE_OCR", "Text in pictures (OCR)"),
    ("faces", "DEVICE_FACES", "Faces"),
    ("pictures", "DEVICE_PICTURES", "Picture search (CLIP)"),
    ("describe", "DEVICE_DESCRIBE", "Photo descriptions (Florence-2)"),
)
CHOICES: tuple[str, ...] = ("auto", "cpu", "gpu")
RESULTS_FILE = "device_test.json"


def setting_key(model: str) -> str:
    """The `.env` key for one model ("meaning" -> "DEVICE_MEANING").

    Raises `StopIteration` for a name not in `MODELS`: every caller passes a
    name from that table, so an unknown one is a programming error.
    """
    return next(key for name, key, _label in MODELS if name == model)


#: Each setting read by its own name, so a reader of this file - and the check
#: that every setting is read by something - can see which is which.
_READ = {
    "meaning": lambda s: getattr(s, "device_meaning", "auto"),
    "rerank": lambda s: getattr(s, "device_rerank", "auto"),
    "ocr": lambda s: getattr(s, "device_ocr", "auto"),
    "faces": lambda s: getattr(s, "device_faces", "auto"),
    "pictures": lambda s: getattr(s, "device_pictures", "auto"),
    "describe": lambda s: getattr(s, "device_describe", "auto"),
}


def _chosen(settings: Any, model: str) -> str:
    value = str(_READ[model](settings) or "auto").strip().lower()
    return value if value in CHOICES else "auto"


def device_for(settings: Any, model: str) -> str:
    """`auto`, `cpu` or `gpu` for one model - what its constructor is asked for."""
    chosen = _chosen(settings, model)
    if chosen != "auto":
        return chosen
    measured = measured_winner(settings, model)
    if measured:
        return measured
    return str(getattr(settings, "embed_device", "auto") or "auto").strip().lower()


def results_path(settings: Any) -> Optional[Path]:
    """`<STATE_PATH>/device_test.json`, or None when no state folder is set.

    Beside the index rather than in `.env`: the result is a fact about this
    machine's hardware, not a choice anybody maintains.
    """
    state = getattr(settings, "state_path", None)
    return Path(state) / RESULTS_FILE if state else None


def load_results(settings: Any) -> dict:
    """The stored test, or `{}`. Never raises: an unreadable file is "untested"."""
    path = results_path(settings)
    if path is None or not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_results(settings: Any, results: dict) -> Optional[Path]:
    """Written whole, to a temporary file first - a half-written result would
    read as a test of some other machine."""
    path = results_path(settings)
    if path is None:
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(results, indent=2, sort_keys=True), encoding="utf-8")
    temporary.replace(path)
    return path


@lru_cache(maxsize=1)
def machine_fingerprint() -> str:
    """This computer, as the tuning numbers already know it. Asked once per
    process: finding the graphics adapters costs a PowerShell call."""
    try:
        from app.core.compute_profile import detect

        return detect().fingerprint()
    except Exception:                                # noqa: BLE001 - "unknown" is a machine too
        return "unknown"


def measured_winner(settings: Any, model: str) -> Optional[str]:
    """`cpu` or `gpu` from this machine's last test, else None."""
    results = load_results(settings)
    if not results:
        return None
    if results.get("fingerprint") != machine_fingerprint():
        return None                                  # another computer's answer
    entry = (results.get("models") or {}).get(model) or {}
    winner = entry.get("winner")
    return winner if winner in ("cpu", "gpu") else None


def needs_test(settings: Any) -> bool:
    """Whether an index run should test this machine first: some model is on
    Automatic and this machine has never been tested (or is a new machine)."""
    if not any(_chosen(settings, name) == "auto" for name, _key, _label in MODELS):
        return False
    results = load_results(settings)
    return not results or results.get("fingerprint") != machine_fingerprint()
