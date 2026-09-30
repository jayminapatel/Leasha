r"""What is in the model folder, what each thing is for, whether it is in use - and removal.

Layer: L1 (files and settings; no Qt). Everything here reads the disk, so a
worker calls it, never the window.

Two kinds of thing live in `MODEL_CACHE`:

* **Catalogue copies** (`catalogue.json`) - the ONNX models Leasha runs itself.
  Several copies of one model can share one snapshot folder (Florence-2's full
  and int8 graphs sit side by side), so a copy is its *files*, and removing one
  copy never touches another's.
* **Everything else** - fastembed's meaning, rerank and CLIP models, the locally
  quantised meaning model, the old faster-whisper folder, unfinished downloads.
  Each is a whole folder.

**In use** follows the settings, not a guess: the photo and chat copies are the
ones `catalogue.best` would load; speech is `TRANSCRIBE_MODEL`'s size; the
meaning and rerank models are `EMBED_MODEL` / `RERANK_MODEL`; CLIP is always in
use while pictures are indexed. A model in use is removed only when the caller
says it knows (`force=True`) - the Models box asks first and says what stops.

**Removal is permanent.** These files are downloaded again from the same
Download button; a Recycle Bin full of gigabyte files frees no space. The box
says so before anything is removed.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from app.core.logging import logger

__all__ = ["Item", "scan", "unused", "remove", "JOB_WORDS"]

_log = logger.bind(component="ort.inventory")

JOB_WORDS = {
    "photo": "photo tags and Describe",
    "speech": "speech in recordings",
    "chat": "Chat and Interpret",
    "meaning": "meaning search",
    "rerank": "re-ranking results",
    "clip": "finding pictures by meaning",
    "leftover": "nothing - left over",
}

#: fastembed's CLIP pair: fixed in `clip_embedder.py`, used whenever pictures are.
_CLIP_REPOS = ("Qdrant/clip-ViT-B-32-vision", "Qdrant/clip-ViT-B-32-text")


@dataclass
class Item:
    """One model (or leftover) on disk, or a verified copy not downloaded yet."""

    key: str
    label: str
    job: str
    copy: str = ""                        # "full", "4-bit", "int8", ""
    bytes: int = 0
    present: bool = True
    in_use: bool = False
    verified: bool = False
    recommended: bool = False
    paths: list[Path] = field(default_factory=list)     # what removal deletes
    whole_folder: bool = False
    note: str = ""
    downloadable: str = ""                # model_fetch kind:name, when Download applies

    @property
    def job_words(self) -> str:
        return JOB_WORDS.get(self.job, self.job)

    @property
    def size_words(self) -> str:
        mb = self.bytes / 2 ** 20
        return f"{mb / 1024:.1f} GB" if mb >= 1024 else f"{mb:.0f} MB"


def _copy_words(suffix: str) -> str:
    return {"": "full", "_q4": "4-bit", "_int8": "int8", "_q4f16": "4-bit"}.get(suffix, suffix)


def _folder_bytes(path: Path) -> int:
    total = 0
    for entry in path.rglob("*"):
        try:
            if entry.is_file():
                total += entry.stat().st_size
        except OSError:
            pass
    return total


def _repo_dir(cache: Path, repo: str) -> Path:
    return cache / ("models--" + repo.replace("/", "--"))


def _fastembed_repo(kind: str, name: str) -> Optional[str]:
    try:
        from app.core.model_fetch import _fastembed_source

        return _fastembed_source(kind, name)
    except Exception:                                   # noqa: BLE001
        return None


def scan(cache_dir: Optional[Path], settings: Any = None, *, catalogue: Any = None) -> list[Item]:
    """Every model thing in `cache_dir`, plus verified copies not yet downloaded."""
    from app.ort import catalogue as catalogue_module
    from app.ort import hub

    items: list[Item] = []
    if not cache_dir:
        return items
    cache = Path(cache_dir)
    cat = catalogue or catalogue_module.load(getattr(settings, "state_path", None))
    engine = str(getattr(settings, "chat_engine", "onnx") or "onnx")
    speech_size = str(getattr(settings, "transcribe_model", "base") or "base")
    best = {job: catalogue_module.best(job, cache, catalogue=cat) for job in ("photo", "chat")}
    best_speech = catalogue_module.best("speech", cache, size=speech_size, catalogue=cat)
    claimed: set[Path] = set()

    for entry in cat.entries:
        model = entry.model()
        folder = hub.resolve(model, cache)
        if folder is None:
            if entry.is_verified and entry.offered and entry is cat.recommended(
                    entry.job, entry.size if entry.job == "speech" else ""):
                items.append(Item(key=entry.key, label=entry.label, job=entry.job,
                                  copy=_copy_words(entry.suffix), present=False,
                                  verified=True, recommended=True,
                                  bytes=entry.approx_mb * 2 ** 20,
                                  downloadable=("speech:" + entry.size if entry.job == "speech"
                                                else "onnx:" + entry.key)))
            continue
        files = [folder / name for name in model.files() if name.startswith("onnx/")]
        size = sum(p.stat().st_size for p in files if p.is_file())
        if entry.job == "speech":
            in_use = best_speech is not None and best_speech[0].key == entry.key
        elif entry.job == "chat":
            in_use = engine == "onnx" and best["chat"] is not None and best["chat"][0].key == entry.key
        else:
            in_use = best.get(entry.job) is not None and best[entry.job][0].key == entry.key
        items.append(Item(
            key=entry.key, label=entry.label, job=entry.job, copy=_copy_words(entry.suffix),
            bytes=size, in_use=in_use, verified=entry.is_verified,
            recommended=entry is cat.recommended(entry.job, entry.size if entry.job == "speech" else ""),
            paths=files,
            note=entry.note or ("" if entry.is_verified else "not checked on a real machine")))
        claimed.add(_repo_dir(cache, entry.repo))

    embed = str(getattr(settings, "embed_model", "") or "")
    rerank = str(getattr(settings, "rerank_model", "") or "")
    embed_repo = _fastembed_repo("embed", embed) if embed else None
    rerank_repo = _fastembed_repo("rerank", rerank) if rerank else None
    quantised = embed.replace("/", "--") + "-int8-local" if embed else ""

    try:
        folders = sorted(p for p in cache.iterdir() if p.is_dir() and p.name != ".locks")
    except OSError:
        folders = []
    for folder in folders:
        if folder in claimed:
            continue
        name, job, in_use, note = folder.name, "leftover", False, ""
        if folder.name.startswith("models--"):
            repo = folder.name[len("models--"):].replace("--", "/", 1)
            name = repo
            if repo in _CLIP_REPOS:
                job, in_use = "clip", True
            elif embed_repo and repo.lower() == embed_repo.lower():
                job, in_use = "meaning", True
            elif rerank_repo and repo.lower() == rerank_repo.lower():
                job, in_use = "rerank", True
            elif any(repo == e.repo for e in cat.entries):
                job, note = "leftover", "other copies of a model Leasha runs"
            else:
                job, note = "leftover", "not the meaning or rerank model chosen now"
        elif folder.name == quantised:
            job, in_use = "meaning", bool(getattr(settings, "embed_quantised", False))
            name = f"{embed} (smaller copy made on this computer)"
        elif folder.name == "whisper":
            note = "the old speech engine's files (faster-whisper) - not used any more"
            name = "whisper (old speech engine)"
        elif folder.name.endswith("-int8-local"):
            note = "a smaller copy of a meaning model not chosen now"
        items.append(Item(key="folder:" + folder.name, label=name, job=job,
                          bytes=_folder_bytes(folder), in_use=in_use, paths=[folder],
                          whole_folder=True, note=note))

    partial = [p for p in cache.rglob("*.incomplete") if p.is_file()]
    if partial:
        items.append(Item(key="partial", label="Unfinished downloads", job="leftover",
                          bytes=sum(p.stat().st_size for p in partial), paths=partial,
                          note="pieces of downloads that stopped; Download starts again"))
    return items


def unused(items: list[Item]) -> list[Item]:
    """What "Remove copies nothing uses" removes: on disk, not in use."""
    return [i for i in items if i.present and not i.in_use and i.paths]


def remove(item: Item, *, force: bool = False) -> int:
    """Delete `item`'s files. Returns the bytes freed. Permanent.

    Refuses an item in use unless `force` - the caller has asked the person. For a
    catalogue copy only its own files go; its snapshot and repository folders
    are removed only when nothing else is left in them."""
    if item.in_use and not force:
        raise PermissionError(f"{item.label} is in use for {item.job_words}")
    freed = 0
    for path in item.paths:
        try:
            if path.is_dir():
                freed += _folder_bytes(path)
                shutil.rmtree(path)
            elif path.is_file():
                freed += path.stat().st_size
                path.unlink()
        except OSError as exc:
            _log.warning("could not remove {}: {}", path, exc)
            raise
    if not item.whole_folder:
        for path in item.paths:
            _prune_empty(path.parent)
    _log.info("removed {} ({} MB)", item.label, freed // 2 ** 20)
    return freed


def _prune_empty(folder: Path) -> None:
    """Remove empty folders upward, stopping at a `models--` repository's parent,
    and remove the repository when its snapshot has no model graphs left."""
    current = folder
    while current.name and not current.name.startswith("models--"):
        try:
            if any(current.iterdir()):
                break
            current.rmdir()
        except OSError:
            break
        current = current.parent
    repo = next((p for p in [folder, *folder.parents] if p.name.startswith("models--")), None)
    if repo is not None and repo.is_dir() and not any(repo.rglob("*.onnx")):
        shutil.rmtree(repo, ignore_errors=True)
