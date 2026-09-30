r"""The model catalogue: which ONNX models Leasha runs, which copies are verified, best first.

Layer: L1 (data and files; no Qt, no model loading).

**Data, not code** (owner, 2026-09-30: "a mechanism to manage onnx models ...
the models we know and have tested which work and are best make them default").
`catalogue.json` beside this file lists every copy Leasha can run: its job
(`photo`, `speech`, `chat`), the runner that loads it, the repository and a
**pinned revision**, the files with their **sha256**, and a `verified` record -
when, on what machine, and what was measured. `rank` orders the copies of one
job, best first. A new model of a kind Leasha already runs (another chat model
exported the same way, another Whisper size, a newer Florence) is a new entry,
not a new release. A new *kind* of model still needs a runner in code.

**Only verified entries are recommended.** 2026-09-30 showed why: the int8 chat
copy downloaded and ran and gave wrong answers; the 4-bit copy gave Ollama's.

**A newer catalogue can be fetched** - `check_for_update`, from a person pressing
"Check for new models", never by itself - and is kept in the state folder. The
newer of the shipped and the fetched catalogue is used; a fetched one that does
not parse or is not newer is ignored, so a bad download can never make things
worse than what shipped.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

from app.core.logging import logger

__all__ = ["Entry", "Catalogue", "BUNDLED", "load", "best", "verify_files",
           "check_for_update", "fetched_path", "DEFAULT_URL"]

_log = logger.bind(component="ort.catalogue")

BUNDLED = Path(__file__).with_name("catalogue.json")

#: The Hugging Face list as it was on the day this was shipped (2026-09-30: 220
#: models). "Update the list from Hugging Face" writes a fresh one to the state
#: folder, which is used instead from then on.
BUNDLED_HF = Path(__file__).with_name("catalogue_hf.json")

#: Where "Check for new models" looks by default: this file on the project's
#: `main`. Replaceable in Settings; nothing is fetched unless a person asks.
DEFAULT_URL = ("https://raw.githubusercontent.com/jayminapatel/Leasha/main/"
               "app/ort/catalogue.json")

FORMAT = 1


@dataclass(frozen=True)
class Entry:
    """One copy of one model, as the catalogue describes it."""

    key: str
    job: str
    runner: str
    rank: int
    label: str
    repo: str
    revision: str
    graphs: tuple[str, ...]
    suffix: str
    required: tuple[str, ...]
    extra: tuple[str, ...]
    approx_mb: int
    licence: str
    sha256: dict = field(default_factory=dict)
    verified: Optional[dict] = None
    offered: bool = True
    size: str = ""
    prompt_format: str = ""
    #: What a person should know, e.g. that it was checked and failed.
    note: str = ""
    #: One or two plain sentences: what the model is and what it is good for.
    description: str = ""
    #: "verified" (shipped, checked on a real machine) or "huggingface" (found by
    #: "Update the list from Hugging Face", not checked).
    source: str = "verified"
    downloads: int = 0

    @property
    def is_verified(self) -> bool:
        return bool(self.verified)

    def model(self) -> Any:
        """The `hub.OnnxModel` the loaders and `hub.resolve` work with."""
        from app.ort.hub import OnnxModel

        return OnnxModel(key=self.key, repo=self.repo, label=self.label,
                         graphs=self.graphs, suffix=self.suffix, required=self.required,
                         approx_mb=self.approx_mb, licence=self.licence, extra=self.extra,
                         revision=self.revision)

    @classmethod
    def from_row(cls, row: dict) -> "Entry":
        return cls(
            key=str(row["key"]), job=str(row["job"]), runner=str(row["runner"]),
            rank=int(row.get("rank", 5)), label=str(row.get("label") or row["key"]),
            repo=str(row["repo"]), revision=str(row.get("revision") or ""),
            graphs=tuple(row.get("graphs") or ()), suffix=str(row.get("suffix") or ""),
            required=tuple(row.get("required") or ()), extra=tuple(row.get("extra") or ()),
            approx_mb=int(row.get("approx_mb") or 0), licence=str(row.get("licence") or ""),
            sha256=dict(row.get("sha256") or {}), verified=row.get("verified") or None,
            offered=bool(row.get("offered", True)), size=str(row.get("size") or ""),
            prompt_format=str(row.get("prompt_format") or ""),
            note=str(row.get("note") or ""),
            description=str(row.get("description") or ""),
            source=str(row.get("source") or "verified"),
            downloads=int(row.get("downloads") or 0),
        )


@dataclass(frozen=True)
class Catalogue:
    version: str
    entries: tuple[Entry, ...]
    source: str = "shipped"

    def by_key(self, key: str) -> Optional[Entry]:
        return next((e for e in self.entries if e.key == key), None)

    def for_job(self, job: str) -> list[Entry]:
        """Every copy for `job`, best first."""
        return sorted((e for e in self.entries if e.job == job), key=lambda e: (e.rank, e.key))

    def recommended(self, job: str, size: str = "") -> Optional[Entry]:
        """The best verified, offered copy for `job` (and speech `size`)."""
        return next((e for e in self.for_job(job)
                     if e.is_verified and e.offered and (not size or e.size == size)), None)


def _parse(text: str, source: str) -> Catalogue:
    data = json.loads(text)
    if int(data.get("format", 0)) != FORMAT:
        raise ValueError(f"catalogue format {data.get('format')!r}, expected {FORMAT}")
    entries = tuple(Entry.from_row(row) for row in data.get("models") or ())
    if not entries:
        raise ValueError("the catalogue lists no models")
    return Catalogue(version=str(data.get("version") or ""), entries=entries, source=source)


def fetched_path(state_dir: Optional[Path]) -> Optional[Path]:
    return Path(state_dir) / "model_catalogue.json" if state_dir else None


def discovered_path(state_dir: Optional[Path]) -> Optional[Path]:
    """Where "Update the list from Hugging Face" keeps what it found (`discover.py`)."""
    return Path(state_dir) / "model_catalogue_hf.json" if state_dir else None


def choices_path(state_dir: Optional[Path]) -> Optional[Path]:
    return Path(state_dir) / "model_choices.json" if state_dir else None


_state_cache: list = []


def default_state_dir() -> Optional[Path]:
    """`STATE_PATH`, read once - callers such as the photo tagger are handed a path
    and nothing else, the way `ocr.py` reads its own settings."""
    if not _state_cache:
        try:
            from app.core.config import load_settings

            _state_cache.append(Path(load_settings(create_dirs=False,
                                                   check_writable=False).state_path))
        except Exception:                               # noqa: BLE001
            _state_cache.append(None)
    return _state_cache[0]


def load(state_dir: Optional[Path] = None) -> Catalogue:
    """The shipped catalogue (or a fetched newer one), plus what "Update the list from
    Hugging Face" found. Never raises for a bad saved copy; the shipped file failing
    to parse is a real defect."""
    state = state_dir if state_dir is not None else default_state_dir()
    shipped = _parse(BUNDLED.read_text(encoding="utf-8"), "shipped")
    path = fetched_path(state)
    if path is not None and path.is_file():
        try:
            fetched = _parse(path.read_text(encoding="utf-8"), "fetched")
            if fetched.version > shipped.version:
                shipped = fetched
        except Exception as exc:                        # noqa: BLE001 - fall back to shipped
            _log.warning("the fetched model catalogue at {} was not used: {}", path, exc)
    found = discovered_path(state)
    if found is None or not found.is_file():
        found = BUNDLED_HF if BUNDLED_HF.is_file() else None
    if found is not None and found.is_file():
        try:
            extra = _parse(found.read_text(encoding="utf-8"), "huggingface")
            # The same copy of the same repository is one model: the curated entry
            # (pinned, checksummed, perhaps verified) wins over the found one.
            known = {e.key for e in shipped.entries} | {
                (e.repo.lower(), e.suffix) for e in shipped.entries}
            merged = shipped.entries + tuple(
                e for e in extra.entries
                if e.key not in known and (e.repo.lower(), e.suffix) not in known)
            return Catalogue(version=shipped.version, entries=merged, source=shipped.source)
        except Exception as exc:                        # noqa: BLE001
            _log.warning("the Hugging Face model list at {} was not used: {}", found, exc)
    return shipped


def chosen(job: str, state_dir: Optional[Path] = None) -> str:
    """The key a person picked with "Use this" for `job`, or ""."""
    path = choices_path(state_dir if state_dir is not None else default_state_dir())
    try:
        return str(json.loads(path.read_text(encoding="utf-8")).get(job) or "")
    except Exception:                                   # noqa: BLE001 - nothing chosen
        return ""


def choose(job: str, key: str, state_dir: Optional[Path] = None) -> None:
    """Remember `key` for `job` ("" goes back to the recommended copy)."""
    path = choices_path(state_dir if state_dir is not None else default_state_dir())
    if path is None:
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:                                   # noqa: BLE001
        data = {}
    if key:
        data[job] = key
    else:
        data.pop(job, None)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def best(job: str, cache_dir: Optional[Path], *, size: str = "",
         catalogue: Optional[Catalogue] = None) -> Optional[tuple[Entry, Path]]:
    """The best copy for `job` that is on disk, as `(entry, folder)`. Offline.

    Verified copies first by rank, then anything else on disk by rank - a copy
    somebody already has is still used rather than nothing."""
    from app.ort import hub

    cat = catalogue or load()
    rows = [e for e in cat.for_job(job) if not size or e.size == size]
    ordered = [e for e in rows if e.is_verified] + [e for e in rows if not e.is_verified]
    picked = chosen(job)
    if picked:
        # "Use this" wins when that model is on disk (any size, for speech).
        entry = cat.by_key(picked)
        if entry is not None and entry.job == job:
            ordered = [entry] + [e for e in ordered if e.key != picked]
    for entry in ordered:
        folder = hub.resolve(entry.model(), cache_dir)
        if folder is not None:
            return entry, folder
    return None


def verify_files(entry: Entry, folder: Path) -> list[str]:
    """Files whose sha256 does not match the catalogue (empty when all do, or when
    the entry records no checksums). Reads every byte, so a worker calls this."""
    wrong = []
    for name, expected in entry.sha256.items():
        path = Path(folder) / name
        digest = hashlib.sha256()
        try:
            with open(path, "rb") as handle:
                for chunk in iter(lambda: handle.read(1 << 22), b""):
                    digest.update(chunk)
        except OSError:
            wrong.append(name)
            continue
        if digest.hexdigest() != expected:
            wrong.append(name)
    return wrong


def check_for_update(state_dir: Path, url: str = DEFAULT_URL, *, timeout: float = 20.0,
                     current: Optional[Catalogue] = None) -> tuple[bool, str]:
    """Fetch the catalogue at `url`; keep it if it is valid and newer. For a button.

    Returns `(updated, sentence)`. Never raises: a network failure, a missing file
    or a malformed catalogue is a sentence, and what was there stays in use."""
    import urllib.request

    now = current or load(state_dir)
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:    # noqa: S310 - a URL the person set
            text = response.read().decode("utf-8")
        fetched = _parse(text, "fetched")
    except Exception as exc:                            # noqa: BLE001 - said plainly
        return False, (f"Could not check for new models ({type(exc).__name__}). The models "
                       f"you have keep working. Address used: {url}")
    if fetched.version <= now.version:
        return False, f"No new models: this computer already has the latest list ({now.version})."
    path = fetched_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    added = sorted({e.key for e in fetched.entries} - {e.key for e in now.entries})
    tail = f" New: {', '.join(added)}." if added else ""
    return True, f"The list of models was updated to {fetched.version}.{tail}"


def keys(entries: Iterable[Entry]) -> list[str]:
    return [e.key for e in entries]
