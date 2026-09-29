r"""Video and audio files: what they are, what is on screen, and what was said.

Layer: L2

Work order 202626270515. Three layers, each optional and each degrading to the
one below it, so the feature is useful the day PyAV is installed and better
with every dependency added after:

  * **Layer 0 - the container** (PyAV, in-process). Duration, resolution, codecs and,
    above all, the date it was *recorded*: the video equivalent of an EXIF date,
    which survives copying from a phone to a laptop to a backup drive where the
    file's own date does not. GPS from a phone becomes a place.
  * **Layer 1 - scene-change pictures** (PyAV, keyframes only). A frame at every cut, and at
    least one every N seconds, capped. Each is read by **the same code that
    reads a photograph** - the OCR ladder, then Florence-2 for a picture with no
    text - and the frames are handed to the pipeline for the CLIP lane. Nothing
    is re-implemented: a video is a photo album with a clock.
  * **Layer 2 - speech** (faster-whisper). What was said, with a timestamp per
    passage, flowing through the ordinary text pipeline. A search result for a
    transcript hit says *at 12:41*.

**Audio files** (`.mp3`, `.m4a`, `.wav`, a voice memo) are Layer 2 with no
picture: the same transcriber, the same timestamps.

**Off by default, and gracefully absent.** `VIDEO_INDEXING_ENABLED` and
`AUDIO_TRANSCRIPTION_ENABLED` decide whether the walker even offers these
extensions (`disabled_extensions`), so switched off the files are exactly what
they were before this order: findable by name, nothing read. Switched on with
PyAV missing, each file is one skip-ledger line saying what to install. Every
failure is a per-file `AppErrorException`; none can stop a run.

**Transcripts are labelled `read_by=whisper`**, as OCR text is `read_by=ocr` -
a person deciding whether to trust a sentence deserves to know a model wrote it.

**Nothing is read out of the file's bytes here.** `reads_externally` is True:
hashing a 4GB film to notice it changed would be the cost this whole design is
built to avoid, and mtime and size are enough to tell a video was replaced.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from app.core.errors import AppError, AppErrorException, make_error
from app.core.format_health import Requirement
from app.core.logging import logger
from app.extract import media_tools, transcribe
from app.extract.base import Document, DocumentBuilder, SourceKind, register
from app.extract.timecode import format_timecode

__all__ = [
    "MediaConfig",
    "VideoExtractor",
    "AudioExtractor",
    "FrameReading",
    "configure",
    "current",
    "disabled_extensions",
    "media_extensions",
    "release_keyframes",
    "sweep_stale_keyframes",
    "describe_container",
    "VIDEO_EXTENSIONS",
    "AUDIO_EXTENSIONS",
]

log = logger.bind(component="extract.media")

#: **`.ts` and `.mts` are not here**: both are TypeScript (`.mts` is its ES-module
#: form, and `plaintext` claims it), and claiming either would send source files
#: to ffprobe. An AVCHD camcorder's `.mts` is the casualty; its `.m2ts` is here.
VIDEO_EXTENSIONS = frozenset({
    ".mp4", ".m4v", ".mov", ".mkv", ".avi", ".wmv", ".webm", ".mpg", ".mpeg",
    ".3gp", ".flv", ".m2ts",
})
AUDIO_EXTENSIONS = frozenset({
    ".mp3", ".m4a", ".wav", ".flac", ".ogg", ".oga", ".opus", ".aac", ".wma",
})

#: Prefix of the temporary folders that hold a video's pictures between
#: extraction and the pipeline's CLIP step. Swept when stale - see
#: `sweep_stale_keyframes`.
KEYFRAME_DIR_PREFIX = "leasha-keyframes-"

#: A keyframe folder older than this belongs to a run that died.
STALE_KEYFRAME_HOURS = 6


# ---------------------------------------------------------------------------
# Configuration: module state, set once by whoever starts a run
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class MediaConfig:
    """The five settings, and two folders. Defaults are **off**."""

    video_enabled: bool = False
    audio_enabled: bool = False
    model: str = transcribe.DEFAULT_MODEL
    #: The longest gap between pictures taken from one video, in seconds.
    keyframe_interval_s: int = 60
    #: The most pictures taken from one video.
    keyframe_cap: int = 200
    model_dir: Optional[Path] = None
    journal_dir: Optional[Path] = None

    @classmethod
    def from_settings(cls, settings: Any) -> "MediaConfig":
        """Read the five registry settings off a `Settings`. Never raises."""
        def _int(name: str, default: int, low: int, high: int) -> int:
            try:
                value = int(getattr(settings, name, default))
            except (TypeError, ValueError):
                value = default
            return max(low, min(high, value))

        model = str(getattr(settings, "transcribe_model", "") or "").strip().lower()
        data_path = getattr(settings, "data_path", None)
        model_cache = getattr(settings, "model_cache", None)
        return cls(
            video_enabled=bool(getattr(settings, "video_indexing_enabled", False)),
            audio_enabled=bool(getattr(settings, "audio_transcription_enabled", False)),
            model=model if model in transcribe.MODELS else transcribe.DEFAULT_MODEL,
            keyframe_interval_s=_int("video_keyframe_interval_s", 60, 5, 600),
            keyframe_cap=_int("video_keyframe_cap", 200, 10, 1000),
            model_dir=(Path(model_cache) / "whisper") if model_cache else None,
            journal_dir=(Path(data_path) / "transcripts") if data_path else None,
        )


_config = MediaConfig()
_pacer: Optional[Callable[[], bool]] = None
_should_stop: Optional[Callable[[], bool]] = None
_state_lock = threading.Lock()


def configure(
    config: Optional[MediaConfig] = None,
    *,
    pacer: Optional[Callable[[], bool]] = None,
    should_stop: Optional[Callable[[], bool]] = None,
) -> None:
    """Set this module's settings for the run about to start.

    **Module state rather than a parameter**, for the reason `ocr.
    configure_device` gives: an extractor is called from a worker with a path
    and nothing else. `None` restores the defaults - both layers off - which is
    what a `Pipeline` built without media settings must mean, so one run's
    switches can never leak into the next.

    `pacer` is the resource governor's pause, called between pictures and
    between spoken passages; it returns True if the run was told to stop while
    it waited. `should_stop` is the run's stop flag.
    """
    global _config, _pacer, _should_stop
    with _state_lock:
        _config = config or MediaConfig()
        _pacer, _should_stop = pacer, should_stop
    if _config.journal_dir is not None:
        try:
            transcribe.prune_journals(_config.journal_dir)
        except Exception:                          # noqa: BLE001 - housekeeping only
            pass
    sweep_stale_keyframes()


def current() -> MediaConfig:
    return _config


def media_extensions() -> frozenset[str]:
    return VIDEO_EXTENSIONS | AUDIO_EXTENSIONS


def disabled_extensions() -> frozenset[str]:
    """Extensions the walker must not offer, because their switch is off.

    This is the whole of "off by default": with both switches off, this is every
    media extension, and the walk is byte-for-byte what it was before this
    order existed.
    """
    off: set[str] = set()
    if not _config.video_enabled:
        off |= VIDEO_EXTENSIONS
    if not _config.audio_enabled:
        off |= AUDIO_EXTENSIONS
    return frozenset(off)


def _pace() -> bool:
    """Wait if the governor says so; True if the run should stop."""
    stop = False
    if _pacer is not None:
        try:
            stop = bool(_pacer())
        except Exception:                          # noqa: BLE001 - pacing must not cost a file
            stop = False
    if _should_stop is not None:
        try:
            stop = stop or bool(_should_stop())
        except Exception:                          # noqa: BLE001
            pass
    return stop


def _interrupted(path: Path) -> AppErrorException:
    return AppErrorException(make_error(
        "ERR_MEDIA_INTERRUPTED", "extract.media", path=str(path)))


# ---------------------------------------------------------------------------
# Pictures: the folders, and reading a frame
# ---------------------------------------------------------------------------

def release_keyframes(meta: Optional[dict[str, Any]]) -> None:
    """Delete a video's temporary pictures. Safe to call twice, never raises.

    Called by the pipeline once the CLIP step has used them, and by this module
    on every failure path before the pictures could ever be handed on.
    """
    if not meta:
        return
    folder = meta.pop("keyframe_dir", None)
    meta.pop("keyframes", None)
    if folder:
        shutil.rmtree(str(folder), ignore_errors=True)


def sweep_stale_keyframes(*, max_age_hours: float = STALE_KEYFRAME_HOURS) -> int:
    """Remove keyframe folders left by a run that was killed. Returns how many."""
    removed = 0
    cutoff = time.time() - max_age_hours * 3600
    try:
        for entry in Path(tempfile.gettempdir()).glob(KEYFRAME_DIR_PREFIX + "*"):
            try:
                if entry.is_dir() and entry.stat().st_mtime < cutoff:
                    shutil.rmtree(entry, ignore_errors=True)
                    removed += 1
            except OSError:
                continue
    except OSError:
        pass
    return removed


@dataclass(frozen=True)
class FrameReading:
    """What one picture from a video turned out to say."""

    text: str = ""          # words on screen (OCR)
    caption: str = ""       # what it shows (Florence-2), when there was no text
    tags: tuple[str, ...] = ()

    @property
    def empty(self) -> bool:
        return not (self.text.strip() or self.caption.strip() or self.tags)


def read_frame(path: Path) -> FrameReading:
    """Read one picture **exactly as a photograph is read**. Never raises.

    The OCR ladder first (`ocr.ocr_image`, which routes on its own); Florence-2
    only when there is no text, the same order `OcrExtractor` uses. Both are
    soft dependencies - a machine with neither yields an empty reading and the
    video is still indexed by its container and its speech.
    """
    text = caption = ""
    tags: tuple[str, ...] = ()
    try:
        from app.extract import ocr

        if ocr.available():
            result = ocr.ocr_image(path)
            if not result.engine_missing:
                text = result.text.strip()
    except Exception as exc:                       # noqa: BLE001 - one picture
        log.debug("no OCR for a keyframe: {}: {}", type(exc).__name__, exc)

    if not text:
        try:
            from app.extract import florence_tagger

            if florence_tagger.available():
                tagged = florence_tagger.tag_image(path)
                if tagged is not None:
                    caption = (tagged.caption or "").strip()
                    tags = tuple(tagged.tags or ())
        except Exception as exc:                   # noqa: BLE001 - one picture
            log.debug("no tags for a keyframe: {}: {}", type(exc).__name__, exc)
    return FrameReading(text=text, caption=caption, tags=tags)


class _FrameJournal:
    """Per-picture readings, kept so a killed run does not re-read what it read.

    Same idea and same failure rules as `transcribe.TranscriptJournal`: append
    only, torn last line ignored, keyed by the file and the two settings that
    change which pictures exist.
    """

    def __init__(self, directory: Optional[Path], source: Path,
                 interval_s: int, cap: int) -> None:
        self.path: Optional[Path] = None
        if directory is None:
            return
        try:
            stat = source.stat()
        except OSError:
            return
        raw = f"{source.resolve()}|{stat.st_size}|{stat.st_mtime_ns}|frames|{interval_s}|{cap}"
        self.path = Path(directory) / (hashlib.sha1(raw.encode()).hexdigest()[:24] + ".frames.jsonl")

    def load(self) -> dict[int, FrameReading]:
        found: dict[int, FrameReading] = {}
        if self.path is None:
            return found
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    try:
                        row = json.loads(line)
                        found[int(row["i"])] = FrameReading(
                            text=str(row.get("o", "")), caption=str(row.get("c", "")),
                            tags=tuple(str(t) for t in row.get("g", ())))
                    except (ValueError, KeyError, TypeError):
                        continue
        except OSError:
            pass
        return found

    def append(self, index: int, reading: FrameReading) -> None:
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(
                    {"i": index, "o": reading.text, "c": reading.caption,
                     "g": list(reading.tags)}, ensure_ascii=False) + "\n")
                handle.flush()
        except OSError:
            pass

    def discard(self) -> None:
        if self.path is not None:
            try:
                self.path.unlink()
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Writing the document
# ---------------------------------------------------------------------------

def _spoken_length(seconds: Optional[float]) -> str:
    if not seconds:
        return ""
    whole = int(seconds)
    hours, rest = divmod(whole, 3600)
    minutes, secs = divmod(rest, 60)
    parts = []
    if hours:
        parts.append(f"{hours} hour" + ("s" if hours != 1 else ""))
    if minutes:
        parts.append(f"{minutes} minute" + ("s" if minutes != 1 else ""))
    if secs and not hours:
        parts.append(f"{secs} second" + ("s" if secs != 1 else ""))
    return " ".join(parts) or "under a second"


def describe_container(
    path: Path, info: Optional[media_tools.MediaInfo], *, kind: str,
    place: Optional[str] = None,
) -> str:
    """Layer 0 as the words that make the file findable. Pure.

    Written as sentences a person would type a query from - "video", "recorded
    July 2019", "12 minutes" - rather than a field dump, because the same text
    feeds both the keyword index and the meaning model.
    """
    lines = [f"{'Video' if kind == 'video' else 'Audio recording'}: {path.name}"]
    if info is None:
        return "\n".join(lines)
    if info.duration_s:
        lines.append(f"Length: {_spoken_length(info.duration_s)} "
                     f"({format_timecode(info.duration_s)})")
    if info.has_video and info.width and info.height:
        picture = f"Picture: {info.width}x{info.height}"
        if info.video_codec:
            picture += f", {info.video_codec}"
        if info.frame_rate:
            picture += f", {info.frame_rate:g} frames a second"
        lines.append(picture)
    if kind == "video":
        lines.append(f"Sound: {info.audio_codec or 'audio'}" if info.has_audio
                     else "Sound: none")
    elif info.audio_codec:
        lines.append(f"Format: {info.audio_codec}")
    if info.created is not None:
        lines.append(f"Recorded: {info.created:%d %B %Y} ({info.created:%Y-%m-%d})")
    if place:
        lines.append(f"Place: {place}")
    for key in ("title", "artist", "album", "comment", "description", "genre"):
        if info.tags.get(key):
            lines.append(f"{key.capitalize()}: {info.tags[key]}")
    return "\n".join(lines)


def _place_for(info: Optional[media_tools.MediaInfo]) -> Optional[str]:
    """A phone's GPS as a town name, offline. None when there is no fix or the
    geocoder is not installed - the places lane's own contract."""
    if info is None or info.latitude is None or info.longitude is None:
        return None
    try:
        from app.extract import places

        if not places.available():
            return None
        return places.reverse_geocode(info.latitude, info.longitude)
    except Exception:                              # noqa: BLE001 - a place, not the file
        return None


def _add_screen_text(
    builder: DocumentBuilder, timeline: list[tuple[float, FrameReading]],
) -> int:
    """Layer 1's words, one anchored line per picture that said something new.

    Consecutive pictures with the same words (a slide held for ten minutes)
    become one line at the first timestamp. Returns how many lines were written.
    """
    lines: list[str] = []
    anchors: list[tuple[int, str]] = []
    cursor = 0
    previous = ""
    for seconds, reading in timeline:
        parts = []
        if reading.text.strip():
            parts.append("On screen: " + " ".join(reading.text.split()))
        if reading.caption.strip():
            parts.append("Scene: " + reading.caption.strip())
        if reading.tags:
            parts.append("Tags: " + ", ".join(reading.tags))
        line = ". ".join(parts)
        if not line or line == previous:
            continue
        previous = line
        anchors.append((cursor, format_timecode(seconds)))
        lines.append(line)
        cursor += len(line) + 1                       # the newline between lines
    if not lines:
        return 0
    builder.add("\n".join(lines), label="Seen in the video", prefix_label=True,
                anchors=anchors)
    return len(lines)


def _add_transcript(builder: DocumentBuilder, transcript: transcribe.Transcript) -> int:
    """Layer 2's words, one anchored line per spoken passage."""
    lines: list[str] = []
    anchors: list[tuple[int, str]] = []
    cursor = 0
    for segment in transcript.segments:
        text = " ".join(segment.text.split())
        if not text:
            continue
        anchors.append((cursor, format_timecode(segment.start)))
        lines.append(text)
        cursor += len(text) + 1
    if not lines:
        return 0
    builder.add("\n".join(lines), label="Transcript", prefix_label=True, anchors=anchors)
    return len(lines)


# ---------------------------------------------------------------------------
# The extractors
# ---------------------------------------------------------------------------

class _MediaBase:
    #: See the module docstring: hashing a film is the cost being avoided.
    reads_externally = True

    def supports(self, path: Path) -> bool:
        return path.suffix.lower() in self.extensions      # type: ignore[attr-defined]

    def _transcribe(self, path: Path, cfg: MediaConfig) -> transcribe.Transcript:
        return transcribe.transcribe(
            path, model=cfg.model, model_dir=cfg.model_dir,
            journal_dir=cfg.journal_dir, pacer=_pace, should_stop=_should_stop)


class VideoExtractor(_MediaBase):
    """Layers 0, 1 and (when switched on) 2 for a video file."""

    name = "video"
    extensions = VIDEO_EXTENSIONS
    #: Declared so Settings and `doctor` can say what is missing and why it
    #: matters, in the words `format_health` already uses for every extractor.
    # 2026-09-29: speech runs on ONNX Runtime (always installed); what it still
    # needs is PyAV, which reads the sound. Was `faster_whisper`.
    requires = (
        Requirement("av", "av",
                    provides="what was said in the video (speech to text)",
                    hard=False),
    )
    supports_resume = False

    def extract(self, path: Path) -> Iterable[Document]:
        cfg = current()
        info = media_tools.probe(path)             # missing tool / unreadable -> raises
        place = _place_for(info)

        builder = DocumentBuilder(path, source_kind=SourceKind.FILE)
        # An anchor at 0:00 on the details, so the first chunk of a video - which
        # begins with them, before any picture or passage - still says *where*
        # it is from. Without it a short video's one chunk had no locator at all,
        # because a chunk is located by the last anchor at or before its start.
        builder.add(describe_container(path, info, kind="video", place=place),
                    label="Video", anchors=[(0, "0:00")])
        layers = ["metadata"]
        if info.created is not None:
            builder.date = info.created

        keyframe_dir: Optional[Path] = None
        frames: list[media_tools.Keyframe] = []
        try:
            if info.has_video:
                keyframe_dir = Path(tempfile.mkdtemp(prefix=KEYFRAME_DIR_PREFIX))
                frames = media_tools.extract_keyframes(
                    path, keyframe_dir, interval_s=cfg.keyframe_interval_s,
                    cap=cfg.keyframe_cap, duration_s=info.duration_s,
                    should_stop=_pace)
                timeline = self._read_frames(path, frames, cfg)
                if _add_screen_text(builder, timeline):
                    layers.append("keyframes")

            if cfg.audio_enabled and info.has_audio:
                try:
                    transcript = self._transcribe(path, cfg)
                except AppErrorException as exc:
                    if exc.error.code == "ERR_MEDIA_INTERRUPTED":
                        raise
                    # **Not fatal to the video**: its container and its pictures
                    # are still worth indexing. The missing speech is reported
                    # as a warning the pipeline counts by code, so "412 videos
                    # had no transcript because the speech package is missing"
                    # is a number rather than a mystery.
                    builder.warn(exc.error)
                else:
                    if _add_transcript(builder, transcript):
                        layers.append("speech")
                        builder.meta["transcript_language"] = transcript.language
                        builder.meta["transcript_seconds"] = round(transcript.elapsed_s, 1)
                        builder.meta["transcript_resumed_from_s"] = transcript.resumed_from_s
        except BaseException:
            # Failed before the pictures could be handed on: nobody else knows
            # the folder exists.
            if keyframe_dir is not None:
                shutil.rmtree(str(keyframe_dir), ignore_errors=True)
            raise

        builder.meta.update({
            "format": "video",
            "media_kind": "video",
            "layers": layers,
            # `read_by` names the most expensive reader that contributed, the way
            # `pdf.py` writes "ocr" for a scanned page.
            "read_by": ("whisper" if "speech" in layers
                        else "ocr" if "keyframes" in layers else "ffprobe"),
            "duration_s": info.duration_s,
        })
        if info.created_ns is not None:
            builder.meta["media_created_ns"] = info.created_ns
        if place:
            builder.meta["media_place"] = place
        if keyframe_dir is not None and frames:
            # Handed on to the pipeline, which embeds them and then calls
            # `release_keyframes`. The extractor must not clean up after the
            # `yield`: the consumer is on another thread and has not used them.
            builder.meta["keyframe_dir"] = str(keyframe_dir)
            builder.meta["keyframes"] = [(f.seconds, str(f.path)) for f in frames]
        elif keyframe_dir is not None:
            shutil.rmtree(str(keyframe_dir), ignore_errors=True)
        yield builder.build()

    def _read_frames(
        self, path: Path, frames: list[media_tools.Keyframe], cfg: MediaConfig,
    ) -> list[tuple[float, FrameReading]]:
        journal = _FrameJournal(cfg.journal_dir, path, cfg.keyframe_interval_s,
                                cfg.keyframe_cap)
        kept = journal.load()
        timeline: list[tuple[float, FrameReading]] = []
        for index, frame in enumerate(frames):
            reading = kept.get(index)
            if reading is None:
                reading = read_frame(frame.path)
                journal.append(index, reading)
                if _pace():
                    raise _interrupted(path)
            timeline.append((frame.seconds, reading))
        # Finished: the journal exists to survive a kill, and the document is
        # about to carry everything in it.
        journal.discard()
        return timeline


class AudioExtractor(_MediaBase):
    """Layer 0 (if PyAV is there) and Layer 2 for an audio file."""

    name = "audio"
    extensions = AUDIO_EXTENSIONS
    # 2026-09-29: PyAV reads the sound for the ONNX speech engine. Was `faster_whisper`.
    requires = (
        Requirement("av", "av",
                    provides="what was said in the recording (speech to text)",
                    hard=True),
    )
    supports_resume = False

    def extract(self, path: Path) -> Iterable[Document]:
        cfg = current()
        info: Optional[media_tools.MediaInfo] = None
        try:
            info = media_tools.probe(path)
        except AppErrorException as exc:
            # A recording is transcribable without a probe of its own -
            # faster-whisper reads the file itself - so a missing PyAV costs the
            # metadata, not the file. A *damaged* file is still a skip.
            if exc.error.code != "ERR_MEDIA_TOOLS_MISSING":
                raise

        builder = DocumentBuilder(path, source_kind=SourceKind.FILE)
        builder.add(describe_container(path, info, kind="audio"), label="Recording",
                    anchors=[(0, "0:00")])
        if info is not None and info.created is not None:
            builder.date = info.created

        spoken = 0
        speech_error: Optional[AppError] = None
        transcript: Optional[transcribe.Transcript] = None
        try:
            transcript = self._transcribe(path, cfg)
        except AppErrorException as exc:
            if exc.error.code == "ERR_MEDIA_INTERRUPTED":
                raise
            speech_error = exc.error
        else:
            spoken = _add_transcript(builder, transcript)

        if speech_error is not None:
            if info is None:
                # Nothing at all to index: the skip ledger says why and how to fix.
                raise AppErrorException(speech_error)
            builder.warn(speech_error)

        layers = ["metadata"] + (["speech"] if spoken else [])
        builder.meta.update({
            "format": "audio",
            "media_kind": "audio",
            "layers": layers,
            "read_by": "whisper" if spoken else "ffprobe",
            "duration_s": info.duration_s if info is not None else None,
        })
        if transcript is not None and spoken:
            builder.meta["transcript_language"] = transcript.language
            builder.meta["transcript_seconds"] = round(transcript.elapsed_s, 1)
            builder.meta["transcript_resumed_from_s"] = transcript.resumed_from_s
        if info is not None and info.created_ns is not None:
            builder.meta["media_created_ns"] = info.created_ns
        yield builder.build()


register(VideoExtractor())
register(AudioExtractor())
