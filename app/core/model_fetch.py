r"""Fetching a model, only because somebody pressed Download.

Layer: L0 (no Qt; blocks, so it is called from a worker)

**The owner, 2026-09-29:** *"where there are models it has to be dropdown only
no manual entry for models, if there are other options add them and put a
mechanism to download"*. Every model list in Settings is now a drop-down of
names this application can actually load, and beside it a Download button for
the one chosen when it is not on this computer yet. This module is what that
button runs.

**Offline is still the rule.** Nothing here runs by itself: not at start-up,
not while indexing, not while searching. A download starts when a person
presses Download next to a named model, and the button says so before it is
pressed. Four kinds of model, each fetched the way the code that *loads* it
expects to find it:

* ``ollama`` - the Interpret, Chat and photo description models. Ollama keeps
  its own store; `OllamaClient.pull` asks it to fetch one and reports the bytes
  as they arrive.
* ``embed`` - the meaning model (`EMBED_MODEL`), loaded by fastembed's
  `TextEmbedding(model_name, cache_dir=MODEL_CACHE)` in `app/index/embedder.py`.
* ``rerank`` - the reranker (`RERANK_MODEL`), loaded by fastembed's
  `TextCrossEncoder(model_name, cache_dir=MODEL_CACHE)` in `app/search/rerank.py`.
* ``speech`` - the speech model (`TRANSCRIBE_MODEL`), loaded by faster-whisper
  from `MODEL_CACHE\whisper` with `local_files_only=True`
  (`app/extract/transcribe.py`), which is why it is never fetched by itself.
* ``onnx`` - 2026-09-29: the models Leasha runs itself on ONNX Runtime
  (`app/ort/hub.py`: Florence-2 photo tags, Whisper speech, the chat model),
  named by their `hub` key. Only that model's graphs at its precision and its
  small side files are fetched - each repository holds a dozen precisions.
  Plain HTTPS with a 30 s stall limit: on the owner's link on 2026-09-29 the
  Hugging Face "xet" transfer stalled for good at 445 MB, and plain HTTPS
  stalled for minutes at a time; a download that gives up can be pressed
  again and carries on.

**The three file-based kinds download in a child process**, running exactly
the constructor the application uses, into exactly the folder it reads. That
is what makes Stop honest: the child is ended and the download with it, where a
thread inside a library call cannot be stopped at all. What was fetched stays
in the Hugging Face cache and the next Download carries on from it. Progress is
the growth of the folder against the model's published size, the same
outside-in measure `embedder._DownloadProgressWatcher` already uses, because
neither library forwards a progress hook this far.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any, Callable, Optional

from app.core.errors import AppErrorException, make_error

__all__ = [
    "KINDS", "APPROX_MB", "present", "fetch", "target_dir", "child_code",
    "STOPPED", "DONE",
]

KINDS = ("ollama", "embed", "rerank", "speech", "onnx")

#: What `fetch` returns.
DONE = "done"
STOPPED = "stopped"

#: Published download sizes in MB, for the progress line only - never for
#: correctness. fastembed's own catalogue (`size_in_GB`, fastembed 0.8.0) for
#: the meaning and rerank models; the model cards for faster-whisper's. A model
#: missing here still downloads; its progress is shown in MB so far instead.
APPROX_MB: dict[str, int] = {
    "BAAI/bge-small-en-v1.5": 67,
    "BAAI/bge-base-en-v1.5": 210,
    "BAAI/bge-large-en-v1.5": 1200,
    "sentence-transformers/all-MiniLM-L6-v2": 90,
    "snowflake/snowflake-arctic-embed-s": 130,
    "snowflake/snowflake-arctic-embed-m": 430,
    "mixedbread-ai/mxbai-embed-large-v1": 640,
    "jinaai/jina-embeddings-v2-small-en": 120,
    "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2": 220,
    "Xenova/ms-marco-MiniLM-L-6-v2": 80,
    "Xenova/ms-marco-MiniLM-L-12-v2": 120,
    "jinaai/jina-reranker-v1-tiny-en": 130,
    "jinaai/jina-reranker-v1-turbo-en": 150,
    "BAAI/bge-reranker-base": 1040,
    "jinaai/jina-reranker-v2-base-multilingual": 1110,
    "tiny": 75,
    "base": 145,
    "small": 484,
    "medium": 1530,
    "large-v3-turbo": 1620,
    "large-v3": 3090,
}

#: How often a child download is looked at, in seconds.
_POLL_S = 0.5

#: The child's whole program, per kind: the loader the application uses, with
#: the model name and the folder as arguments so nothing is quoted into code.
_CHILD_CODE = {
    "embed": ("import sys; from fastembed import TextEmbedding; "
              "TextEmbedding(model_name=sys.argv[1], cache_dir=sys.argv[2])"),
    "rerank": ("import sys; from fastembed.rerank.cross_encoder import TextCrossEncoder; "
               "TextCrossEncoder(model_name=sys.argv[1], cache_dir=sys.argv[2])"),
    # "speech" had its own faster-whisper program here until 2026-09-30; speech has
    # fetched the ONNX export through "onnx" since 2026-09-29 (`child_code` and the
    # download both map it), so that program was never run and was removed.
    "onnx": ("import sys, json, os; os.environ.setdefault('HF_HUB_DISABLE_XET', '1'); "
             "os.environ.setdefault('HF_HUB_DOWNLOAD_TIMEOUT', '30'); "
             "from huggingface_hub import snapshot_download; "
             "snapshot_download(sys.argv[1], cache_dir=sys.argv[2], "
             "allow_patterns=json.loads(sys.argv[3]), "
             "revision=(sys.argv[4] if len(sys.argv) > 4 and sys.argv[4] else None))"),
}


def _onnx_model(name: str) -> Any:
    from app.ort import hub

    return hub.by_key(name)


def _speech_model(name: str) -> Any:
    """2026-09-29: speech runs Whisper on ONNX Runtime (`app/ort/whisper.py`), so a
    size ("base") is fetched as that size's ONNX export - the faster-whisper
    files the old child fetched are not what the engine reads any more."""
    from app.ort.whisper import SIZES

    return SIZES.get(name)


def child_code(kind: str) -> str:
    """The one-line program a file-based download runs. For the tests and the CLI.

    Speech is fetched as its ONNX export, so it runs the "onnx" program - the
    same mapping the download itself makes."""
    return _CHILD_CODE["onnx" if kind == "speech" else kind]


def target_dir(kind: str, model_cache: Any) -> Optional[Path]:
    r"""The folder the application loads this kind of model from, or None.

    `MODEL_CACHE` for fastembed's two, `MODEL_CACHE\whisper` for speech -
    exactly what `Embedder.from_settings`, `Reranker.from_settings` and
    `MediaBox.load` hand their loaders.
    """
    if not model_cache:
        return None
    root = Path(str(model_cache))
    # 2026-09-29: speech lands in MODEL_CACHE like every ONNX export; the engine
    # also looks in MODEL_CACHE\whisper, where the old downloads went.
    return root


def _folder_bytes(path: Path) -> int:
    total = 0
    try:
        for entry in path.rglob("*"):
            try:
                if entry.is_file():
                    total += entry.stat().st_size
            except OSError:
                pass
    except OSError:
        pass
    return total


def _fastembed_source(kind: str, name: str) -> Optional[str]:
    """The Hugging Face repository fastembed fetches `name` from, or None."""
    try:
        if kind == "embed":
            from fastembed import TextEmbedding as loader
        else:
            from fastembed.rerank.cross_encoder import TextCrossEncoder as loader
        for entry in loader.list_supported_models():
            if str(entry.get("model", "")).lower() == name.lower():
                return str((entry.get("sources") or {}).get("hf") or "") or None
    except Exception:                              # noqa: BLE001 - a lookup, never a failure
        return None
    return None


def present(kind: str, name: str, *, model_cache: Any = None,
            client: Any = None) -> bool:
    """Is `name` already on this computer? Never raises; False when unsure.

    Disk (or, for Ollama, one loopback request) - so a worker calls this, never
    the window.
    """
    name = str(name or "").strip()
    if not name:
        return False
    try:
        if kind == "ollama":
            installed = list(client.available_models()) if client is not None else []
            # `llava` is installed as `llava:latest`; `qwen2.5vl:7b` must match
            # itself exactly, not any other size of the same family.
            return any(n == name or (":" not in name and n.split(":")[0] == name)
                       for n in installed)
        folder = target_dir(kind, model_cache)
        if folder is None or not folder.is_dir():
            return False
        if kind == "speech":
            from app.extract.transcribe import model_present

            return model_present(name, folder)
        if kind == "onnx":
            from app.ort import hub

            model = _onnx_model(name)
            return model is not None and hub.present(model, folder)
        source = _fastembed_source(kind, name)
        if source:
            repo = folder / ("models--" + source.replace("/", "--"))
            if any(repo.glob("snapshots/*/**/*.onnx")):
                return True
        # fastembed's older download layout: a `fast-<name>` folder.
        tail = name.split("/")[-1].lower()
        return any(p.is_dir() and p.name.lower().startswith("fast-")
                   and tail in p.name.lower() for p in folder.iterdir())
    except Exception:                              # noqa: BLE001 - see docstring
        return False


def _describe_bytes(done: int, total: int) -> str:
    def mb(n: int) -> str:
        return f"{n / 1024 ** 3:.1f} GB" if n >= 1024 ** 3 else f"{n / 1024 ** 2:.0f} MB"
    if total > 0:
        percent = min(99, int(100 * done / total))
        return f"{percent}% ({mb(done)} of about {mb(total)})"
    return f"{mb(done)} so far"


def _fetch_ollama(name: str, client: Any, on_progress: Callable[[str], None],
                  stop: threading.Event) -> str:
    def status(event: dict) -> None:
        total = int(event.get("total") or 0)
        done = int(event.get("completed") or 0)
        words = str(event.get("status") or "").strip()
        if total:
            on_progress(f"Downloading {name}: {_describe_bytes(done, total)}")
        elif words:
            on_progress(f"{name}: {words}")

    finished = client.pull(name, on_status=status, should_stop=stop.is_set)
    return DONE if finished else STOPPED


def _no_window_flags() -> int:
    if os.name != "nt":
        return 0
    # CREATE_NO_WINDOW: the window is a GUI program, and a console flashing up
    # for a download reads as something having gone wrong.
    return getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)


def _child_args(kind: str, name: str, folder: Path) -> list[str]:
    """What the child program gets after `-c code`: always the name and the folder;
    for `onnx` and `speech`, the repository instead of the name, and the files."""
    if kind not in ("onnx", "speech"):
        return [name, str(folder)]
    import json

    from app.ort.hub import _SIDE_FILES

    model = _onnx_model(name) if kind == "onnx" else _speech_model(name)
    # The pinned revision (catalogue.json) when there is one - 2026-09-30.
    revision = getattr(model, "revision", "") or _catalogue_revision(model.key)
    return [model.repo, str(folder), json.dumps(list(model.files()) + list(_SIDE_FILES)),
            revision]


def _catalogue_entry(key: str) -> Any:
    try:
        from app.ort.catalogue import load

        return load().by_key(key)
    except Exception:                              # noqa: BLE001 - no catalogue, no pin
        return None


def _catalogue_revision(key: str) -> str:
    entry = _catalogue_entry(key)
    return entry.revision if entry is not None else ""


def _check_download(kind: str, name: str, folder: Path) -> None:
    """After an ONNX download: every file matches the catalogue's sha256, or the
    download is refused - a changed or tampered file never becomes the model."""
    if kind not in ("onnx", "speech"):
        return
    model = _onnx_model(name) if kind == "onnx" else _speech_model(name)
    entry = _catalogue_entry(model.key) if model is not None else None
    if entry is None or not entry.sha256:
        return
    from app.ort import hub
    from app.ort.catalogue import verify_files

    snapshot = hub.resolve(entry.model(), folder)
    wrong = verify_files(entry, snapshot) if snapshot is not None else list(entry.sha256)
    if wrong:
        raise AppErrorException(make_error(
            "ERR_MODEL_DOWNLOAD", "core.model_fetch", model=name,
            details=("the downloaded files do not match the checked copy: "
                     + ", ".join(wrong[:3]))))


def _fetch_in_child(kind: str, name: str, folder: Path,
                    on_progress: Callable[[str], None], stop: threading.Event,
                    popen: Callable[..., Any]) -> str:
    folder.mkdir(parents=True, exist_ok=True)
    start = _folder_bytes(folder)
    model = (_onnx_model(name) if kind == "onnx"
             else _speech_model(name) if kind == "speech" else None)
    total = (model.approx_mb if model is not None else APPROX_MB.get(name, 0)) * 1024 ** 2
    # The child's messages go to a file, never a pipe: the download libraries
    # draw progress bars on stderr, and a pipe nobody reads fills up and
    # stops the child dead partway through.
    said = tempfile.TemporaryFile()
    try:
        child = popen(
            [sys.executable, "-c", _CHILD_CODE["onnx" if kind == "speech" else kind],
             *_child_args(kind, name, folder)],
            stdout=subprocess.DEVNULL, stderr=said,
            creationflags=_no_window_flags())
    except OSError as exc:
        said.close()
        raise AppErrorException(make_error(
            "ERR_MODEL_DOWNLOAD", "core.model_fetch", model=name,
            details=f"could not start the download: {exc}")) from exc
    on_progress(f"Downloading {name}...")
    try:
        while child.poll() is None:
            if stop.is_set():
                child.kill()
                child.wait(timeout=10)
                said.close()
                return STOPPED
            grown = max(0, _folder_bytes(folder) - start)
            on_progress(f"Downloading {name}: {_describe_bytes(grown, total)}")
            stop.wait(_POLL_S)
    finally:
        if child.poll() is None:
            child.kill()
    try:
        said.seek(0)
        text = said.read().decode("utf-8", "replace")
    except Exception:                              # noqa: BLE001 - only for the message
        text = ""
    finally:
        said.close()
    if child.returncode != 0:
        # Progress bars end in carriage returns; the last real line is the error.
        lines = [line for line in text.replace("\r", "\n").splitlines() if line.strip()]
        last = lines[-1:] or [f"exit code {child.returncode}"]
        raise AppErrorException(make_error(
            "ERR_MODEL_DOWNLOAD", "core.model_fetch", model=name, details=last[0][:400]))
    on_progress(f"Checking {name}...")
    _check_download(kind, name, folder)
    return DONE


def fetch(kind: str, name: str, *, model_cache: Any = None, client: Any = None,
          on_progress: Optional[Callable[[str], None]] = None,
          stop: Optional[threading.Event] = None,
          popen: Optional[Callable[..., Any]] = None) -> str:
    """Download `name`. Returns `DONE` or `STOPPED`; raises `ERR_MODEL_DOWNLOAD`.

    Blocks until the download ends, so it runs on a worker. `on_progress` gets
    one plain sentence at a time ("Downloading llava: 42% (1.9 GB of about
    4.4 GB)"); `stop` ends it early. `popen` is for tests.
    """
    name = str(name or "").strip()
    say = on_progress or (lambda _text: None)
    stop = stop or threading.Event()
    if kind not in KINDS or not name:
        raise AppErrorException(make_error(
            "ERR_MODEL_DOWNLOAD", "core.model_fetch", model=name or "(none)",
            details=f"nothing to download for {kind!r} {name!r}"))
    if kind == "ollama":
        if client is None:
            raise AppErrorException(make_error(
                "ERR_MODEL_DOWNLOAD", "core.model_fetch", model=name,
                details="no Ollama address to download from"))
        return _fetch_ollama(name, client, say, stop)
    if kind == "speech" and _speech_model(name) is None:
        raise AppErrorException(make_error(
            "ERR_MODEL_DOWNLOAD", "core.model_fetch", model=name,
            details=f"{name!r} is not a speech model size Leasha offers"))
    if kind == "onnx" and _onnx_model(name) is None:
        raise AppErrorException(make_error(
            "ERR_MODEL_DOWNLOAD", "core.model_fetch", model=name,
            details=f"{name!r} is not one of the models Leasha runs itself"))
    folder = target_dir(kind, model_cache)
    if folder is None:
        raise AppErrorException(make_error(
            "ERR_MODEL_DOWNLOAD", "core.model_fetch", model=name,
            details="MODEL_CACHE is not set, so there is nowhere to put it"))
    return _fetch_in_child(kind, name, folder, say, stop, popen or subprocess.Popen)
