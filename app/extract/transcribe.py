r"""Speech to text: what was said, and when. faster-whisper, offline, resumable.

Layer: L2

Turns the sound in a recording - a voice memo, a Teams call, the audio track of a
holiday film - into timestamped segments that the ordinary text pipeline chunks,
embeds and searches. `app/extract/media.py` is the caller; this module knows
nothing about video, files rows or search.

**Absent is a state, not a crash.** faster-whisper is optional and so is its
model. `available()` answers without importing it; loading raises
`ERR_TRANSCRIBE_UNAVAILABLE` or `ERR_TRANSCRIBE_MODEL_MISSING`, each naming the
exact command that fixes it, and the caller turns that into one skipped file and
a line in the skip ledger. Nothing here can stop a run.

**Nothing is ever downloaded while indexing.** The model is loaded with
`local_files_only=True`. Leasha is an offline application, a first-time model
fetch is hundreds of megabytes of somebody's bandwidth, and "the index run
quietly went to the internet" is precisely the surprise this project exists not
to give. The fix for a missing model is a command a person chooses to run.

**Resumable, because nothing else is affordable.** A two-hour recording is
roughly an hour of a processor. `TranscriptJournal` writes every finished
segment to a small append-only file the moment it exists; a run killed at
minute forty finds the journal, resumes from where the last segment ended and
does the remaining eighty minutes, not all of them. The journal is a cache: it
is keyed by the file's path, size, modified time and the model, so a changed
file or a different model starts clean, and deleting the folder costs only time.

**CPU only, on purpose.** CTranslate2 - what faster-whisper runs on - has CUDA
and CPU back ends and no DirectML one, so on the machines this application
targets there is no graphics-card path to serialise against
(`app/core/gpu_serialize.py`). A CUDA machine is a future decision, not a
half-built one.

**Run against the real package on 2026-09-20** (faster-whisper 1.2.1, model
`base`, CPU): `python -m app.cli media --measure` transcribed a 199 s clip in
three runs at 11.6x-12.0x real time on a quiet machine, and the real engine
resumed from a journal (`tests/unit/test_media_real.py`). The engine seam
(`TranscriberEngine`) is still what most tests drive, with a fake, because a
model is 148 MB; the real-model tests skip cleanly when it is not downloaded.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator, Optional, Protocol

from app.core.errors import AppErrorException, make_error
from app.core.logging import logger

__all__ = [
    "MEASURED_REALTIME_FACTOR",
    "MEASURED_REALTIME_FACTOR_BUSY",
    "MEASURED_ON",
    "cost_sentence",
    "SpeechSegment",
    "Transcript",
    "TranscriberEngine",
    "TranscriptJournal",
    "MODELS",
    "DEFAULT_MODEL",
    "available",
    "model_present",
    "download_command",
    "transcribe",
    "prune_journals",
    "set_engine_factory",
    "reset_engine",
]

log = logger.bind(component="extract.transcribe")

#: The model sizes offered, smallest first. English-only `.en` variants are left
#: out: this is a family and workplace archive, and a family holds more than one
#: language.
#:
#: Dated note, 2026-09-29 (owner: "if there are other options add them"): the
#: two large multilingual models faster-whisper 1.2.1 names in its own table
#: (`faster_whisper.utils._MODELS`) are offered too, after `medium`.
#: `large-v3-turbo` (mobiuslabsgmbh/faster-whisper-large-v3-turbo, about
#: 1.6 GB) is nearly as accurate as `large-v3` (Systran, about 3 GB) and much
#: faster. Neither is measured here; on a processor both are slow.
MODELS = ("tiny", "base", "small", "medium", "large-v3-turbo", "large-v3")

#: `base` is the size that is usually good enough for clear speech and cheap
#: enough for a processor. **Measured 2026-09-20** (Systran/faster-whisper-base,
#: 148 MB, MIT per its model card; int8, 4 threads on 12 logical cores): about 12x
#: real time on a quiet machine. `tiny` and `small` were not measured; the two
#: settings say only what is known.
DEFAULT_MODEL = "base"

#: How fast `base` transcribes on the CPU-only machine it was measured on, as a
#: multiple of the recording's own length: **12x on a quiet machine** (three runs
#: on a 199 s clip, 11.6x-12.0x, and 13.7x on a 600 s slice of a real recording)
#: and **about 2.3x when other programs were using every core** (two runs,
#: 2.26x and 2.46x, same clip). Each run includes the model load. The Settings
#: cost line and `media --status` quote these, so a change here is a change to
#: what people are told; re-measure with `python -m app.cli media --measure`.
MEASURED_REALTIME_FACTOR = 12.0
MEASURED_REALTIME_FACTOR_BUSY = 2.3
MEASURED_ON = "2026-09-20, model base, CPU only"

#: Journals older than this are removed. A journal for a recording that has not
#: been touched for six weeks belongs to a file that is finished or gone.
JOURNAL_MAX_AGE_DAYS = 45


@dataclass(frozen=True)
class SpeechSegment:
    """A stretch of speech: when it started and ended (seconds), and the words."""

    start: float
    end: float
    text: str


@dataclass
class Transcript:
    """Everything said in one file, and what it cost to find out."""

    segments: list[SpeechSegment] = field(default_factory=list)
    language: str = ""
    #: True when the whole file has been read. False only for a value handed
    #: back part-way, which `transcribe` never does - it raises instead.
    complete: bool = True
    #: Where a resumed run picked up, in seconds. 0 for a fresh one.
    resumed_from_s: float = 0.0
    #: Seconds spent transcribing in *this* call (0 when everything came from
    #: the journal).
    elapsed_s: float = 0.0
    #: True when the transcript was already complete on disk and no speech
    #: engine ran at all.
    from_cache: bool = False

    @property
    def text(self) -> str:
        return " ".join(s.text for s in self.segments if s.text)


class TranscriberEngine(Protocol):
    """The seam. Anything with this shape can stand in for faster-whisper."""

    #: Set by the engine once it knows, usually after the first segment.
    language: str

    def transcribe(self, path: str, start_s: float = 0.0) -> Iterator[SpeechSegment]:
        """Segments of `path` from `start_s` on, with **absolute** timestamps."""
        ...


# ---------------------------------------------------------------------------
# Is it here?
# ---------------------------------------------------------------------------

def available() -> bool:
    """Is faster-whisper installed? Never raises, never imports it.

    `find_spec` only: importing it loads CTranslate2 and PyAV, which is a
    second's work to answer a yes/no question Settings asks every time it opens.
    """
    try:
        return importlib.util.find_spec("faster_whisper") is not None
    except (ImportError, ValueError):
        return False


def cost_sentence() -> str:
    """What transcribing costs, in words, from the measured figures. Pure."""
    quiet = 60.0 / MEASURED_REALTIME_FACTOR
    busy = 60.0 / MEASURED_REALTIME_FACTOR_BUSY
    return (f"An hour of recordings takes about {quiet:.0f} minutes of this "
            f"computer's processor when nothing else is running, and up to "
            f"about {busy:.0f} minutes when it is busy (measured "
            f"{MEASURED_ON}). It is done slowly in the background, and can be "
            f"stopped and carried on.")


def download_command(model: str) -> str:
    """The one-off command that fetches `model`. Shown, never run by Leasha."""
    safe = model if model in MODELS else DEFAULT_MODEL
    return ("venv\\Scripts\\python.exe -c \"from faster_whisper import "
            f"download_model; download_model('{safe}')\"")


def model_present(model: str, model_dir: Optional[Path]) -> bool:
    """Does `model_dir` look like it already holds this model? Never raises.

    A directory check, not a load: faster-whisper caches under the Hugging Face
    layout (`models--Systran--faster-whisper-<size>/snapshots/<rev>/model.bin`),
    and finding that file is enough to tell Settings "downloaded". The real
    answer is still the load, which is why a false positive here costs a clear
    `ERR_TRANSCRIBE_MODEL_MISSING` on the first file and nothing worse.
    """
    if not model_dir:
        return False
    try:
        root = Path(model_dir)
        for repo in root.glob(f"models--*faster-whisper-{model}"):
            if any(repo.glob("snapshots/*/model.bin")):
                return True
        return (root / model / "model.bin").is_file()
    except OSError:
        return False


# ---------------------------------------------------------------------------
# The real engine
# ---------------------------------------------------------------------------

class FasterWhisperEngine:
    """faster-whisper behind the `TranscriberEngine` seam. **Unverified live** -
    see the module docstring."""

    def __init__(self, model: Any) -> None:
        self._model = model
        self.language = ""

    def transcribe(self, path: str, start_s: float = 0.0) -> Iterator[SpeechSegment]:
        options: dict[str, Any] = {
            # Greedy decoding: the speed/accuracy trade a processor needs.
            "beam_size": 1,
            # Off, because a hallucinated sentence otherwise conditions the
            # next thirty seconds - the failure mode that turns silence into a
            # page of "thank you for watching".
            "condition_on_previous_text": False,
        }
        if start_s > 0:
            # Resuming. faster-whisper documents `clip_timestamps` as
            # `start,end,...` with the last end defaulting to the end of the
            # file, so a lone start means "from here to the end".
            options["clip_timestamps"] = f"{start_s:.2f}"
        else:
            # Skip silence on a fresh run - the largest single saving on a
            # recorded meeting. Not combined with `clip_timestamps`, which
            # faster-whisper does not promise to honour together.
            options["vad_filter"] = True
        segments, info = self._model.transcribe(path, **options)
        self.language = str(getattr(info, "language", "") or "")
        for segment in segments:
            yield SpeechSegment(
                start=float(segment.start), end=float(segment.end),
                text=(segment.text or "").strip(),
            )


_engine: Optional[TranscriberEngine] = None
_engine_key: tuple = ()
_engine_error: Optional[AppErrorException] = None
_engine_lock = threading.Lock()
_engine_factory: Optional[Callable[[str, Optional[Path]], TranscriberEngine]] = None


def set_engine_factory(
    factory: Optional[Callable[[str, Optional[Path]], TranscriberEngine]],
) -> None:
    """Replace how the engine is built. For tests, and only for tests."""
    global _engine_factory
    _engine_factory = factory
    reset_engine()


def reset_engine() -> None:
    """Forget the loaded engine and any remembered load failure."""
    global _engine, _engine_key, _engine_error
    with _engine_lock:
        _engine, _engine_key, _engine_error = None, (), None


def _classify_load_failure(exc: BaseException, model: str, path: str) -> AppErrorException:
    """A missing model and a broken install are different fixes."""
    text = f"{type(exc).__name__}: {exc}"
    lowered = text.lower()
    missing = (
        isinstance(exc, FileNotFoundError)
        or "localentrynotfound" in lowered
        or "cannot find" in lowered
        or "not found" in lowered
        or "snapshot" in lowered
        or "local_files_only" in lowered
    )
    if missing:
        return AppErrorException(make_error(
            "ERR_TRANSCRIBE_MODEL_MISSING", "extract.transcribe",
            model=model, path=path, details=text[:400],
            action_payload=download_command(model),
        ))
    return AppErrorException(make_error(
        "ERR_TRANSCRIBE_FAILED", "extract.transcribe", path=path,
        details=f"the speech model could not be loaded: {text[:400]}",
    ))


def load_engine(model: str, model_dir: Optional[Path], *, path: str = "") -> TranscriberEngine:
    """The engine for `model`, loaded once per process and remembered.

    A failed load is remembered too, and re-raised: without that, ten thousand
    recordings would each pay to discover the same missing model.
    """
    global _engine, _engine_key, _engine_error
    key = (model, str(model_dir or ""))
    with _engine_lock:
        if _engine is not None and _engine_key == key:
            return _engine
        if _engine_error is not None and _engine_key == key:
            raise _engine_error

        _engine_key = key
        if _engine_factory is not None:
            try:
                _engine = _engine_factory(model, model_dir)
                return _engine
            except AppErrorException as exc:
                _engine_error = exc
                raise

        if not available():
            _engine_error = AppErrorException(make_error(
                "ERR_TRANSCRIBE_UNAVAILABLE", "extract.transcribe", path=path))
            raise _engine_error

        try:
            from faster_whisper import WhisperModel

            threads = min(4, max(1, (os.cpu_count() or 2) // 2))
            loaded = WhisperModel(
                model, device="cpu", compute_type="int8", cpu_threads=threads,
                download_root=str(model_dir) if model_dir else None,
                # **The line that keeps this offline.** See the module docstring.
                local_files_only=True,
            )
        except Exception as exc:                  # noqa: BLE001 - classified below
            _engine_error = _classify_load_failure(exc, model, path)
            raise _engine_error from exc

        _engine = FasterWhisperEngine(loaded)
        return _engine


# ---------------------------------------------------------------------------
# The journal
# ---------------------------------------------------------------------------

def default_journal_dir() -> Path:
    return Path(tempfile.gettempdir()) / "leasha-transcripts"


class TranscriptJournal:
    """An append-only record of finished segments for one file and one model.

    One JSON object per line. A line cut short by a kill is simply not parsed,
    so a crash mid-write loses one segment, never the file.
    """

    def __init__(self, directory: Path, source: Path, model: str) -> None:
        stat = source.stat()
        raw = f"{source.resolve()}|{stat.st_size}|{stat.st_mtime_ns}|{model}"
        self.path = Path(directory) / (hashlib.sha1(raw.encode("utf-8")).hexdigest()[:24] + ".jsonl")
        self.model = model

    def load(self) -> tuple[list[SpeechSegment], str, bool]:
        """`(segments, language, done)`. Missing or unreadable means empty."""
        segments: list[SpeechSegment] = []
        language, done = "", False
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue                  # a torn last line
                    if not isinstance(row, dict):
                        continue
                    if "t" in row:
                        segments.append(SpeechSegment(
                            float(row.get("s", 0.0)), float(row.get("e", 0.0)),
                            str(row.get("t", ""))))
                    elif row.get("done"):
                        done = True
                        language = str(row.get("lang", "") or language)
        except (OSError, ValueError, TypeError):
            return [], "", False
        return segments, language, done

    def append(self, segment: SpeechSegment) -> None:
        self._write({"s": round(segment.start, 2), "e": round(segment.end, 2),
                     "t": segment.text})

    def finish(self, language: str) -> None:
        self._write({"done": True, "lang": language})

    def discard(self) -> None:
        try:
            self.path.unlink()
        except OSError:
            pass

    def _write(self, row: dict[str, Any]) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            # A journal that cannot be written costs resumability, not the
            # transcript: keep going and say so once per failure at debug.
            log.debug("transcript journal not written ({}): {}", self.path.name, exc)


def prune_journals(directory: Path, *, max_age_days: int = JOURNAL_MAX_AGE_DAYS) -> int:
    """Delete journals untouched for `max_age_days`. Returns how many. Never raises."""
    removed = 0
    cutoff = time.time() - max_age_days * 86400
    try:
        for entry in Path(directory).glob("*.jsonl"):
            try:
                if entry.stat().st_mtime < cutoff:
                    entry.unlink()
                    removed += 1
            except OSError:
                continue
    except OSError:
        return removed
    return removed


# ---------------------------------------------------------------------------
# The call
# ---------------------------------------------------------------------------

def transcribe(
    path: Path,
    *,
    model: str = DEFAULT_MODEL,
    model_dir: Optional[Path] = None,
    journal_dir: Optional[Path] = None,
    engine: Optional[TranscriberEngine] = None,
    pacer: Optional[Callable[[], bool]] = None,
    should_stop: Optional[Callable[[], bool]] = None,
    on_segment: Optional[Callable[[SpeechSegment], Any]] = None,
) -> Transcript:
    """Transcribe one file, resuming from its journal if one exists.

    `pacer` is called after every segment - it is where the resource governor
    pauses a run on battery or a busy machine, and it returns True if the run
    was told to stop while it waited. `should_stop` is checked at the same moment; when it says stop, the segments so far are already on disk and
    `ERR_MEDIA_INTERRUPTED` is raised, which the pipeline treats as *queued*, not
    *failed*.

    Raises `ERR_TRANSCRIBE_UNAVAILABLE`, `ERR_TRANSCRIBE_MODEL_MISSING`,
    `ERR_TRANSCRIBE_FAILED` or `ERR_MEDIA_INTERRUPTED`. Never anything else, and
    never leaves a half transcript looking finished.
    """
    directory = Path(journal_dir) if journal_dir else default_journal_dir()
    try:
        journal = TranscriptJournal(directory, path, model)
    except OSError as exc:
        raise AppErrorException(make_error(
            "ERR_TRANSCRIBE_FAILED", "extract.transcribe", path=str(path),
            details=f"could not read the file to transcribe it: {exc}")) from exc

    done_before, language, complete = journal.load()
    if complete:
        return Transcript(segments=done_before, language=language, complete=True,
                          resumed_from_s=0.0, elapsed_s=0.0, from_cache=True)

    resume_from = max((s.end for s in done_before), default=0.0)
    segments = list(done_before)
    if resume_from:
        log.info("resuming {} from {:.0f}s ({} segments already kept)",
                 path.name, resume_from, len(segments))

    runner = engine or load_engine(model, model_dir, path=str(path))
    started = time.monotonic()
    try:
        for segment in runner.transcribe(str(path), resume_from):
            # An engine asked to resume may repeat the last words it saw.
            if resume_from and segment.end <= resume_from + 0.01:
                continue
            if not segment.text.strip():
                continue
            journal.append(segment)
            segments.append(segment)
            if on_segment is not None:
                on_segment(segment)
            # `pacer` may pause (battery, a busy machine) and answers True when
            # the run was told to stop while it waited.
            stopped = bool(pacer()) if pacer is not None else False
            if stopped or (should_stop is not None and should_stop()):
                raise AppErrorException(make_error(
                    "ERR_MEDIA_INTERRUPTED", "extract.transcribe", path=str(path)))
    except AppErrorException:
        raise
    except Exception as exc:                       # noqa: BLE001 - one file, not the run
        raise AppErrorException(make_error(
            "ERR_TRANSCRIBE_FAILED", "extract.transcribe", path=str(path),
            details=f"{type(exc).__name__}: {exc}"[:400])) from exc

    language = str(getattr(runner, "language", "") or language)
    journal.finish(language)
    return Transcript(
        segments=segments, language=language, complete=True,
        resumed_from_s=resume_from, elapsed_s=time.monotonic() - started,
    )
