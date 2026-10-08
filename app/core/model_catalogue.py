r"""Every model Leasha needs, and a way to download each one or all of them.

Layer: L1 (files and settings; no Qt). `is_present` reads the disk and
`download` blocks until the files are here, so a worker calls them, never the
window.

**The owner, 2026-10-08:** *"do all the models needed to run the system ship
with the release or during the install can you put the download buttons to
download all needed models individually and a button for all"*. Only RapidOCR
ships inside the build. Everything else is fetched once, from this list: by the
installer (one tick-box per model), by Settings, Models (one Download button per
model and one for all), and by `python -m app.cli models download`.

**One list, in one order, for all three.** Each entry names what it is for and
what stops working without it, so a person deciding whether to tick it does
not need to know what a reranker is. The names follow the settings in force -
`EMBED_MODEL`, `RERANK_MODEL`, `TRANSCRIBE_MODEL`, `CHAT_MODEL` and the copy a
person picked with "Use this" in Settings, Models - so the model downloaded is
the model that will be loaded.

**Every download goes through `model_fetch.fetch`**: the same child process,
the same folder, the same Stop, the same checksum as Settings' existing
Download buttons. Nothing here runs by itself; offline stays the rule.

========== ============================================================ =================
key        fetched as (`model_fetch` kind, name)                          default
========== ============================================================ =================
search     embed, `EMBED_MODEL`                                           ticked
rerank     rerank, `RERANK_MODEL`                                         ticked
pictures   image, `CLIP_IMAGE_MODEL`; embed, `CLIP_TEXT_MODEL`            not ticked
photo-tags onnx, the photo copy chosen, else the catalogue's recommended  not ticked
speech     speech, `TRANSCRIBE_MODEL` (or the speech copy chosen)         not ticked
chat       onnx, `CHAT_MODEL`, else the chat copy chosen, else the        not ticked
           catalogue's recommended (the 4-bit Qwen 2.5 1.5B)
faces      faces, insightface's `buffalo_l` (needs insightface)           not ticked
========== ============================================================ =================
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from app.core import model_fetch
from app.core.errors import AppErrorException, make_error

__all__ = [
    "NeededModel", "KEYS", "needed_models", "is_present", "download", "download_all",
    "PRESENT", "FAILED",
]

#: Every key, in the order the list is shown and `download_all` works through.
KEYS: tuple[str, ...] = ("search", "rerank", "pictures", "photo-tags", "speech", "chat",
                         "faces")

#: `download_all`'s word for a model that was already here.
PRESENT = "present"
#: The start of `download_all`'s word for one that did not download.
FAILED = "failed"

#: The catalogue job (`app/ort/catalogue.json`) behind each ONNX key.
_JOB = {"photo-tags": "photo", "speech": "speech", "chat": "chat"}

#: What a person reads, per key: the title and what stops working without it.
_WORDS: dict[str, tuple[str, str]] = {
    "search": ("Meaning search",
               "Finds files by what they mean, not only by the words in them; without it "
               "search matches words only."),
    "rerank": ("Best results first",
               "Reads the top results again and puts the closest matches first; without it "
               "results keep the order search found them in."),
    "pictures": ("Picture search",
                 "Finds photos and pictures from a description of what is in them; without it "
                 "pictures are found only by their names and any text in them."),
    "photo-tags": ("Photo tags and captions",
                   "Writes tags and a caption for each photo, and the paragraph Describe "
                   "shows; without it photos get no tags or captions."),
    "speech": ("Speech in recordings",
               "Turns what is said in audio and video files into text you can search; "
               "without it recordings are found by their names only."),
    "chat": ("Chat and Interpret",
             "Answers questions about your files in Chat and turns a sentence into a "
             "search; without it Chat and Interpret do not work."),
    "faces": ("People in photos",
              "Finds the faces in photos so you can name the people in them; without it "
              "photos cannot be grouped by person."),
}

#: The UI label of the switch faces depend on (`settings_view.py`).
_FACES_NOTE = 'Only used when "Recognise people in photos on this computer" is on.'


@dataclass(frozen=True)
class NeededModel:
    """One model Leasha needs, as the installer, Settings and the CLI show it."""

    key: str            # "search", "rerank", "pictures", "photo-tags", "speech", "chat", "faces"
    title: str          # e.g. "Meaning search"
    purpose: str        # one plain sentence: what stops working without it
    model: str          # the model name(s) shown, e.g. "BAAI/bge-small-en-v1.5"
    approx_mb: int      # published size, for display only
    install_default: bool   # ticked by default in the installer (search, rerank only)
    optional_note: str = ""  # e.g. faces: only used when people are recognised


# ---------------------------------------------------------------------------
# Which model each key means, under these settings
# ---------------------------------------------------------------------------

def _settings(settings: Any) -> Any:
    """The settings given, or the ones in force - read without creating folders."""
    if settings is not None:
        return settings
    from app.core.config import load_settings

    return load_settings(create_dirs=False, check_writable=False)


def _state_dir(settings: Any) -> Optional[Path]:
    state = getattr(settings, "state_path", None)
    return Path(str(state)) if state else None


def _catalogue(settings: Any) -> Any:
    """The ONNX model catalogue, or None when it cannot be read."""
    try:
        from app.ort import catalogue

        return catalogue.load(_state_dir(settings))
    except Exception:                              # noqa: BLE001 - built-ins then
        return None


def _chosen(job: str, settings: Any) -> str:
    try:
        from app.ort import catalogue

        return catalogue.chosen(job, _state_dir(settings))
    except Exception:                              # noqa: BLE001 - nothing chosen
        return ""


def _onnx_key(job: str, settings: Any, *, asked: str = "") -> str:
    """The catalogue key to download for `job`: one a setting names, else the copy
    picked with "Use this", else the catalogue's recommended copy - the order
    `OnnxLLM._copies` and `catalogue.best` load them in."""
    from app.ort import hub

    cat = _catalogue(settings)
    for key in (asked, _chosen(job, settings)):
        if not key:
            continue
        entry = cat.by_key(key) if cat is not None else None
        if entry is not None and entry.job == job:
            return entry.key
    if cat is not None:
        entry = cat.recommended(job)
        if entry is not None:
            return entry.key
    return {"photo": hub.FLORENCE.key, "chat": hub.QWEN_1_5B_Q4.key}.get(job, "")


def _speech_size(settings: Any) -> str:
    return str(getattr(settings, "transcribe_model", "") or "base").strip().lower() or "base"


def _parts(key: str, settings: Any) -> list[tuple[str, str]]:
    """What `key` downloads, as `model_fetch` (kind, name) pairs. [] when unknown."""
    if key == "search":
        return [("embed", str(getattr(settings, "embed_model", "") or "BAAI/bge-small-en-v1.5"))]
    if key == "rerank":
        return [("rerank", str(getattr(settings, "rerank_model", "")
                               or "Xenova/ms-marco-MiniLM-L-6-v2"))]
    if key == "pictures":
        from app.index.clip_embedder import CLIP_IMAGE_MODEL
        from app.search.vector import CLIP_TEXT_MODEL

        return [("image", CLIP_IMAGE_MODEL), ("embed", CLIP_TEXT_MODEL)]
    if key == "photo-tags":
        return [("onnx", _onnx_key("photo", settings))]
    if key == "speech":
        # A language-tuned Whisper picked with "Use this" is what transcribe loads
        # first (`transcribe._chosen_speech`); otherwise the size in the settings.
        picked = _chosen("speech", settings)
        cat = _catalogue(settings) if picked else None
        entry = cat.by_key(picked) if cat is not None else None
        if entry is not None and entry.job == "speech" and entry.runner == "whisper":
            return [("onnx", entry.key)]
        return [("speech", _speech_size(settings))]
    if key == "chat":
        return [("onnx", _onnx_key("chat", settings,
                                   asked=str(getattr(settings, "chat_model", "") or "").strip()))]
    if key == "faces":
        from app.extract.face_detect import MODEL_PACK

        return [("faces", MODEL_PACK)]
    return []


def _onnx_spec(kind: str, name: str) -> Any:
    if kind == "onnx":
        from app.ort import hub

        return hub.by_key(name)
    if kind == "speech":
        from app.ort.whisper import SIZES

        return SIZES.get(name)
    return None


def _copy_words(suffix: str) -> str:
    return {"_q4": "4-bit", "_q4f16": "4-bit", "_int8": "int8", "_fp16": "half size"}.get(
        suffix, "")


def _shown_name(kind: str, name: str) -> str:
    """The name a person reads: the repository, with the copy when it is not the
    full-precision one ("onnx-community/Qwen2.5-1.5B-Instruct, 4-bit")."""
    if kind == "faces":
        return f"insightface {name}"
    spec = _onnx_spec(kind, name)
    if spec is None:
        return name
    words = _copy_words(getattr(spec, "suffix", ""))
    return f"{spec.repo}, {words}" if words else spec.repo


def _approx_mb(kind: str, name: str) -> int:
    spec = _onnx_spec(kind, name)
    if spec is not None:
        return int(getattr(spec, "approx_mb", 0) or 0)
    return int(model_fetch.APPROX_MB.get(name, 0))


def _note(key: str, settings: Any) -> str:
    if key == "faces":
        return _FACES_NOTE
    if key == "chat" and str(getattr(settings, "chat_engine", "onnx") or "onnx") == "ollama":
        return "Not used while Chat runs on Ollama (Settings, Chat)."
    return ""


def needed_models(settings: Any = None) -> list[NeededModel]:
    """Every model Leasha needs, in `KEYS` order, named as these settings name them.

    Reads the settings and the small catalogue files, never the model folder, so
    it is quick; `is_present` is the question that reads the disk.
    """
    settings = _settings(settings)
    out: list[NeededModel] = []
    for key in KEYS:
        parts = _parts(key, settings)
        title, purpose = _WORDS[key]
        out.append(NeededModel(
            key=key, title=title, purpose=purpose,
            model=" + ".join(_shown_name(kind, name) for kind, name in parts),
            approx_mb=sum(_approx_mb(kind, name) for kind, name in parts),
            install_default=key in ("search", "rerank"),
            optional_note=_note(key, settings)))
    return out


# ---------------------------------------------------------------------------
# On this computer?
# ---------------------------------------------------------------------------

def _part_present(kind: str, name: str, settings: Any) -> bool:
    return model_fetch.present(kind, name, model_cache=getattr(settings, "model_cache", None))


def _job_on_disk(job: str, settings: Any) -> bool:
    """Is any copy for `job` on disk that the application would load? A person who
    downloaded the smaller Florence-2 has photo tags; asking them to fetch the
    larger one as well would be wrong."""
    cache = getattr(settings, "model_cache", None)
    if not cache:
        return False
    from app.ort import catalogue

    size = _speech_size(settings) if job == "speech" else ""
    return catalogue.best(job, Path(str(cache)), size=size,
                          catalogue=_catalogue(settings)) is not None


def is_present(key: str, settings: Any = None) -> bool:
    """Is the model behind `key` on this computer, ready to load? Never raises;
    False when unknown or unsure. Reads the disk - a worker calls this."""
    try:
        settings = _settings(settings)
        if key == "faces" and not model_fetch.faces_installed():
            return False
        parts = _parts(key, settings)
        if not parts:
            return False
        if all(_part_present(kind, name, settings) for kind, name in parts):
            return True
        job = _JOB.get(key)
        return job is not None and _job_on_disk(job, settings)
    except Exception:                              # noqa: BLE001 - see docstring
        return False


# ---------------------------------------------------------------------------
# Downloading
# ---------------------------------------------------------------------------

class _StopWhen(threading.Event):
    """A `threading.Event` that is also set when `should_stop()` says so - what
    `model_fetch.fetch` polls every half second while a child downloads."""

    def __init__(self, should_stop: Optional[Callable[[], bool]]) -> None:
        super().__init__()
        self._should_stop = should_stop

    def is_set(self) -> bool:
        """Set once either `set()` was called or `should_stop()` says so.

        Latched: once the callback answers True the event stays set, so a
        caller that stops answering (a closed window) still stops the fetch.
        """
        if not super().is_set() and self._should_stop is not None:
            try:
                if self._should_stop():
                    self.set()
            except Exception:                      # noqa: BLE001 - a question, never a failure
                pass
        return super().is_set()

    isSet = is_set                                 # noqa: N815 - threading's old alias


def _unknown(key: str) -> AppErrorException:
    return AppErrorException(make_error(
        "ERR_MODEL_DOWNLOAD", "core.model_catalogue", model=key or "(none)",
        details=f"{key!r} is not one of the models Leasha needs: {', '.join(KEYS)}"))


def download(key: str, settings: Any = None, *,
             on_progress: Callable[[str], None] = print,
             should_stop: Optional[Callable[[], bool]] = None) -> str:
    """Download the model behind `key`. Returns `model_fetch.DONE` (also when it was
    already here) or `model_fetch.STOPPED`; raises `ERR_MODEL_DOWNLOAD`.

    Blocks until it ends, so it runs on a worker. "pictures" fetches both halves
    of CLIP; a half already here is not fetched again. Faces need insightface:
    without it this raises, saying how to install it.
    """
    settings = _settings(settings)
    parts = _parts(key, settings)
    if not parts:
        raise _unknown(key)
    if key == "faces" and not model_fetch.faces_installed():
        raise AppErrorException(make_error(
            "ERR_MODEL_DOWNLOAD", "core.model_catalogue", model=parts[0][1],
            details="faces need the insightface package, which is not installed here",
            suggestion=model_fetch.FACES_NEED_INSIGHTFACE))
    say = on_progress or (lambda _text: None)
    stop = _StopWhen(should_stop)
    cache = getattr(settings, "model_cache", None)
    for kind, name in parts:
        if stop.is_set():
            return model_fetch.STOPPED
        if _part_present(kind, name, settings):
            say(f"{name} is already here.")
            continue
        if model_fetch.fetch(kind, name, model_cache=cache, on_progress=say,
                             stop=stop) == model_fetch.STOPPED:
            return model_fetch.STOPPED
        say(f"{name} is ready.")
    return model_fetch.DONE


def _sentence(exc: BaseException) -> str:
    error = getattr(exc, "error", None)
    if error is not None:
        text = str(getattr(error, "message", "") or "")
        details = str(getattr(error, "details", "") or "")
        text = f"{text} ({details})" if details else text
        if getattr(error, "suggestion", "") == model_fetch.FACES_NEED_INSIGHTFACE:
            text += " " + model_fetch.FACES_NEED_INSIGHTFACE   # how to install it
        return text
    return str(exc) or type(exc).__name__


def download_all(settings: Any = None, *,
                 on_progress: Callable[[str], None] = print,
                 should_stop: Optional[Callable[[], bool]] = None,
                 keys: Optional[list[str]] = None) -> dict[str, str]:
    """Download each missing model in `keys` (default: all, in `KEYS` order).

    Returns one word per key: "done", "present", "stopped" or "failed: <sentence>".
    **Never raises**: one model that will not download is said and the next is
    tried. Once `should_stop()` is true the rest are "stopped" without starting.
    """
    say = on_progress or (lambda _text: None)
    wanted = list(KEYS if keys is None else keys)
    results: dict[str, str] = {}
    try:
        settings = _settings(settings)
    except Exception as exc:                       # noqa: BLE001 - never raises
        reason = f"{FAILED}: Leasha's settings could not be read: {_sentence(exc)}"
        return {key: reason for key in wanted}

    def stopping() -> bool:
        try:
            return bool(should_stop and should_stop())
        except Exception:                          # noqa: BLE001
            return False

    for key in wanted:
        if stopping():
            results[key] = model_fetch.STOPPED
            continue
        title = _WORDS.get(key, (key, ""))[0]
        try:
            if is_present(key, settings):
                say(f"{title}: already here.")
                results[key] = PRESENT
                continue
            say(f"{title}: downloading...")
            outcome = download(key, settings, on_progress=say, should_stop=should_stop)
            results[key] = outcome
            say(f"{title}: {'ready' if outcome == model_fetch.DONE else 'stopped'}.")
        except Exception as exc:                   # noqa: BLE001 - never raises
            results[key] = f"{FAILED}: {_sentence(exc)}"
            say(f"{title}: did not download - {_sentence(exc)}")
    return results
