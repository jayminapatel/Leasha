r"""What a video or audio file contains, and pictures taken from it. PyAV, in-process.

Layer: L2

Two questions, both answered by one library that is already on the machine
whenever a recording can be transcribed:

  * `probe`             duration, resolution, codecs, the date the file was
                        *recorded*, and GPS if a phone left it
  * `extract_keyframes` a picture at every scene change, plus at least one every
                        N seconds, capped

**No ffmpeg or ffprobe program is run, and none is needed.** The first build of
this order ran both as subprocesses (2026-09-19). On 2026-09-20 that was
replaced, on the owner's delegation ("video audio do"), for three measured
reasons:

  1. **Nothing to install.** `faster-whisper` cannot decode audio without PyAV
     (`av`), so the FFmpeg libraries are in-process on every machine that
     transcribes. A winget install of a second copy bought nothing.
  2. **It is fast enough.** PyAV read the container of a 7-minute and of an
     87-minute mp4 (h264/aac, with `creation_time`) in 0.11 s and 0.13 s.
     Decoding only the encoder's own keyframes, at most one a second, scanned
     the 87-minute 1080p meeting in 13.9 s (377x real time) and the 7-minute
     1080p30 demo in 1.5 s (285x). Decoding every frame ran 0.5x-6.5x real time
     under CPU contention, which is why the scan does not.
  3. **It is a library where one exists** (non-negotiable 12): pinned, a wheel,
     failing in a way the error contract can describe, and no second disk write.

The licence reasoning that used to forbid a binding (LGPL aggregation) is
recorded in `docs/THIRD_PARTY_NOTICES.md`: the same libraries are linked
whenever speech is transcribed, so a subprocess-only rule separated nothing.
`tests/unit/test_media.py::test_only_the_media_modules_import_pyav` keeps the
binding in the two modules that own it.

**PyAV is imported inside the functions**, never at module top level: importing
it loads FFmpeg's DLLs, and `import app.extract` must stay cheap for the many
runs that never see a video.

**Every parser is pure** - `parse_probe` reads the ffprobe-shaped dict that
`_probe_payload` builds from a container - so each one is tested with a canned
dict and no media file on the machine.
"""

from __future__ import annotations

import importlib.metadata
import importlib.util
import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from app.core.errors import AppErrorException, make_error
from app.core.logging import logger

__all__ = [
    "MediaInfo",
    "Keyframe",
    "available",
    "version",
    "probe",
    "parse_probe",
    "parse_iso6709",
    "extract_keyframes",
    "scene_score",
    "tools_status",
    "SCENE_THRESHOLD",
    "SCENE_MIN_GAP_S",
    "HARD_CUT_THRESHOLD",
]

log = logger.bind(component="extract.media_tools")

#: How different two pictures must be to count as a new scene: the mean absolute
#: difference of two 160x90 grey thumbnails, 0-1. **Measured, not guessed**
#: (2026-09-20, real recordings, keyframes-only scan): 0.03 gave 124 pictures for
#: an 87-minute 1080p meeting and 66 for a 7-minute 1080p30 demo, both counting
#: the once-a-minute fill below. A screen-share deck changes a few percent of
#: its pixels between slides, so anything much above 0.05 misses slide changes;
#: anything much below 0.02 starts to fire on compression noise.
#:
#: **Then measured again on the owner's real family videos** (2026-09-20, 40 of
#: the 1,463 clips in `D:\Data\_Media\VideosMaster`, median 20 s, handheld phone
#: footage): 0.03 alone gave a mean of **24.6 pictures per clip, and 120 for one
#: 29-second clip** - a hand-held camera changes a few percent of its pixels
#: between any two keyframes, so almost every keyframe "was a new scene". Read at
#: 1-7 seconds a picture that is 10-70 processor hours for the folder, so a
#: second rule was added: a small change only counts once `SCENE_MIN_GAP_S` has
#: passed since the last picture kept, and only a *big* change
#: (`HARD_CUT_THRESHOLD`, a real cut) counts at once. A static-camera meeting
#: and a slide deck lose nothing: their slide changes are rare, well over five
#: seconds apart.
#:
#: **Not a setting.** Nobody can be asked to choose these numbers, and the two
#: things that do matter - how many pictures at most, and how long a gap at
#: most - are settings.
SCENE_THRESHOLD = 0.03
SCENE_MIN_GAP_S = 5.0
HARD_CUT_THRESHOLD = 0.15

#: Never look at two encoder keyframes closer together than this. A film cut into
#: half-second GOPs would otherwise be decoded almost in full for no gain: a
#: scene that lasts under a second is not one a person searches for.
MIN_SAMPLE_GAP_S = 1.0

#: Pictures are scaled down before being written. CLIP looks at 224 pixels and
#: OCR needs legible text, not a 4K frame: 800 wide keeps a slide readable at a
#: fraction of the disk and decode cost.
FRAME_WIDTH = 800
JPEG_QUALITY = 82

#: The size a picture is shrunk to for the scene comparison.
_SIGNATURE_SIZE = (160, 90)

#: FFmpeg's `AV_DISPOSITION_ATTACHED_PIC`: the album art inside an mp3.
_ATTACHED_PIC = 1 << 10

#: A `creation_time` earlier than this is a camera with a dead clock battery or
#: an editor that wrote zero, not a recording date. Epoch-0 (1970) is the
#: common one; the same reasoning as EXIF's "0000:00:00".
_EARLIEST_PLAUSIBLE = datetime(1990, 1, 1, tzinfo=timezone.utc)


@dataclass
class MediaInfo:
    """What the container said. Every field may be absent; nothing here is required."""

    duration_s: Optional[float] = None
    width: Optional[int] = None
    height: Optional[int] = None
    video_codec: str = ""
    audio_codec: str = ""
    has_video: bool = False
    has_audio: bool = False
    bit_rate: Optional[int] = None
    frame_rate: Optional[float] = None
    #: When it was *recorded*. The video equivalent of an EXIF date: it survives
    #: copying from a phone to a laptop to a backup drive, where mtime does not.
    created: Optional[datetime] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    #: Container tags a person typed or a recorder wrote: title, artist, album,
    #: comment. Lower-cased keys, only the ones worth searching.
    tags: dict[str, str] = field(default_factory=dict)
    container: str = ""

    @property
    def created_ns(self) -> Optional[int]:
        if self.created is None:
            return None
        return int(self.created.timestamp() * 1_000_000_000)


@dataclass(frozen=True)
class Keyframe:
    """One picture taken from a video, and where in the video it came from."""

    seconds: float
    path: Path


# ---------------------------------------------------------------------------
# Is it here?
# ---------------------------------------------------------------------------

def available() -> bool:
    """Is PyAV installed? Never raises, never imports it (importing loads FFmpeg)."""
    try:
        return importlib.util.find_spec("av") is not None
    except (ImportError, ValueError):
        return False


def version() -> Optional[str]:
    """PyAV's installed version, or None. Reads package metadata, not the DLLs."""
    try:
        return importlib.metadata.version("av")
    except importlib.metadata.PackageNotFoundError:
        return None
    except Exception:                              # noqa: BLE001 - a label, not a verdict
        return "installed" if available() else None


def tools_status() -> dict[str, Optional[str]]:
    """`{"av": version-or-None}`. Never raises.

    A dict rather than a bool so Settings, `doctor` and `media --status` keep the
    one shape every optional dependency answers in.
    """
    return {"av": version() if available() else None}


def _import_av(path: Optional[Path] = None) -> Any:
    try:
        import av
    except ImportError as exc:
        raise AppErrorException(make_error(
            "ERR_MEDIA_TOOLS_MISSING", "extract.media_tools",
            binary="av (PyAV)", path=str(path or ""), details=str(exc)[:200])) from exc
    return av


def _failed(path: Path, detail: str) -> AppErrorException:
    return AppErrorException(make_error(
        "ERR_MEDIA_PROBE_FAILED", "extract.media_tools", path=str(path),
        details=detail[:400]))


# ---------------------------------------------------------------------------
# The container, as text tolerant of every recorder
# ---------------------------------------------------------------------------

_ISO6709 = re.compile(r"^\s*([+-]\d+(?:\.\d+)?)([+-]\d+(?:\.\d+)?)")

#: Container tags worth making searchable. `comment` and `description` are where
#: a phone or a screen recorder puts the free text a person would recognise.
_WANTED_TAGS = ("title", "artist", "album", "album_artist", "comment",
                "description", "genre", "composer", "show", "episode_id")


def parse_iso6709(text: object) -> Optional[tuple[float, float]]:
    """`+51.5074-000.1278/` -> `(51.5074, -0.1278)`. None for anything else.

    The format QuickTime and Android write into `location` and
    `com.apple.quicktime.location.ISO6709`. Zero-zero is refused: it is what a
    phone writes when it had no fix, and "a video filmed off the coast of
    Africa" is worse than no place.
    """
    if not isinstance(text, str):
        return None
    found = _ISO6709.match(text)
    if found is None:
        return None
    try:
        latitude, longitude = float(found.group(1)), float(found.group(2))
    except ValueError:
        return None
    if not (-90.0 <= latitude <= 90.0 and -180.0 <= longitude <= 180.0):
        return None
    if latitude == 0.0 and longitude == 0.0:
        return None
    return latitude, longitude


def _parse_when(text: object) -> Optional[datetime]:
    """A container `creation_time` (ISO 8601, usually `...Z`), or None."""
    if not isinstance(text, str) or not text.strip():
        return None
    raw = text.strip().replace("Z", "+00:00")
    try:
        when = datetime.fromisoformat(raw)
    except ValueError:
        # `2019-07-03 14:22:11` and other near-misses some recorders write.
        try:
            when = datetime.strptime(raw[:19], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    if when < _EARLIEST_PLAUSIBLE or when > datetime.now(timezone.utc):
        return None
    return when


def _rate(text: object) -> Optional[float]:
    """`30000/1001` -> 29.97. None for `0/0`, which is what a still image says."""
    if not isinstance(text, str) or "/" not in text:
        return None
    top, _, bottom = text.partition("/")
    try:
        numerator, denominator = float(top), float(bottom)
    except ValueError:
        return None
    if denominator == 0 or numerator == 0:
        return None
    return round(numerator / denominator, 2)


def _as_int(value: object) -> Optional[int]:
    try:
        number = int(float(str(value)))
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def parse_probe(payload: str | bytes | dict) -> MediaInfo:
    """A probe-shaped `{"format": {...}, "streams": [...]}` as a `MediaInfo`. Never raises.

    The shape is ffprobe's JSON, kept because it is the one vocabulary every
    container tool shares and because its tolerance is the point: output varies
    by container and library version, and a missing field is the normal case, not
    an error. What it cannot read it leaves at the default.
    """
    if isinstance(payload, dict):
        data = payload
    else:
        try:
            data = json.loads(payload)
        except (ValueError, TypeError):
            return MediaInfo()
    if not isinstance(data, dict):
        return MediaInfo()

    info = MediaInfo()
    fmt = data.get("format") if isinstance(data.get("format"), dict) else {}
    raw_streams = data.get("streams")
    streams = ([s for s in raw_streams if isinstance(s, dict)]
               if isinstance(raw_streams, list) else [])

    try:
        seconds = float(fmt.get("duration"))
        info.duration_s = seconds if seconds > 0 else None
    except (TypeError, ValueError):
        info.duration_s = None
    info.bit_rate = _as_int(fmt.get("bit_rate"))
    info.container = str(fmt.get("format_name") or "")

    tag_sources = [fmt.get("tags") or {}]
    for stream in streams:
        kind = stream.get("codec_type")
        # **A cover-art picture inside an mp3 is a "video" stream** with
        # `disposition.attached_pic` set. Counting it made every song a video.
        attached = bool((stream.get("disposition") or {}).get("attached_pic"))
        if kind == "video" and not attached and not info.has_video:
            info.has_video = True
            info.video_codec = str(stream.get("codec_name") or "")
            info.width = _as_int(stream.get("width"))
            info.height = _as_int(stream.get("height"))
            info.frame_rate = _rate(stream.get("avg_frame_rate"))
            if info.duration_s is None:
                try:
                    info.duration_s = float(stream.get("duration")) or None
                except (TypeError, ValueError):
                    pass
            tag_sources.append(stream.get("tags") or {})
        elif kind == "audio" and not info.has_audio:
            info.has_audio = True
            info.audio_codec = str(stream.get("codec_name") or "")
            if info.duration_s is None:
                try:
                    info.duration_s = float(stream.get("duration")) or None
                except (TypeError, ValueError):
                    pass

    lowered: dict[str, str] = {}
    for source in tag_sources:
        if not isinstance(source, dict):
            continue
        for key, value in source.items():
            lowered.setdefault(str(key).lower(), str(value))

    info.created = (
        _parse_when(lowered.get("creation_time"))
        or _parse_when(lowered.get("com.apple.quicktime.creationdate"))
        or _parse_when(lowered.get("date"))
    )
    for key in ("location", "com.apple.quicktime.location.iso6709",
                "location-eng"):
        found = parse_iso6709(lowered.get(key))
        if found is not None:
            info.latitude, info.longitude = found
            break
    info.tags = {key: lowered[key].strip() for key in _WANTED_TAGS
                 if lowered.get(key, "").strip()}
    return info


# ---------------------------------------------------------------------------
# The container, read by PyAV
# ---------------------------------------------------------------------------

def _fraction_text(value: Any) -> str:
    try:
        if value is None:
            return "0/0"
        return f"{int(value.numerator)}/{int(value.denominator)}"
    except (AttributeError, TypeError, ValueError):
        return "0/0"


def _stream_seconds(stream: Any) -> Optional[float]:
    try:
        if stream.duration is None or stream.time_base is None:
            return None
        return float(stream.duration * stream.time_base)
    except (TypeError, ValueError, ZeroDivisionError, AttributeError):
        return None


def _probe_payload(container: Any) -> dict[str, Any]:
    """A PyAV container as the dict `parse_probe` reads. Never raises for a field."""
    streams: list[dict[str, Any]] = []
    for stream in container.streams:
        entry: dict[str, Any] = {"codec_type": str(getattr(stream, "type", "") or "")}
        try:
            entry["codec_name"] = stream.codec_context.name
        except Exception:                          # noqa: BLE001 - a codec name, not the file
            entry["codec_name"] = ""
        if entry["codec_type"] == "video":
            entry["width"] = getattr(stream.codec_context, "width", None)
            entry["height"] = getattr(stream.codec_context, "height", None)
            entry["avg_frame_rate"] = _fraction_text(getattr(stream, "average_rate", None))
        seconds = _stream_seconds(stream)
        if seconds is not None:
            entry["duration"] = seconds
        try:
            entry["disposition"] = {
                "attached_pic": 1 if int(stream.disposition) & _ATTACHED_PIC else 0}
        except Exception:                          # noqa: BLE001
            entry["disposition"] = {}
        try:
            entry["tags"] = dict(stream.metadata)
        except Exception:                          # noqa: BLE001
            entry["tags"] = {}
        streams.append(entry)
    fmt: dict[str, Any] = {"format_name": str(getattr(container.format, "name", "") or "")}
    duration = getattr(container, "duration", None)
    if duration:
        # AV_TIME_BASE is microseconds.
        fmt["duration"] = float(duration) / 1_000_000.0
    if getattr(container, "bit_rate", None):
        fmt["bit_rate"] = container.bit_rate
    try:
        fmt["tags"] = dict(container.metadata)
    except Exception:                              # noqa: BLE001
        fmt["tags"] = {}
    return {"format": fmt, "streams": streams}


def probe(path: Path) -> MediaInfo:
    """Read one file's container.

    Raises `ERR_MEDIA_TOOLS_MISSING` if PyAV is not installed, and
    `ERR_MEDIA_PROBE_FAILED` if it opened nothing it could call media - a
    truncated download, a renamed text file. Both are per-file skips.
    """
    av = _import_av(path)
    try:
        container = av.open(str(path))
    except Exception as exc:                       # noqa: BLE001 - any decoder complaint is one skip
        raise _failed(path, f"{type(exc).__name__}: {exc}") from exc
    try:
        info = parse_probe(_probe_payload(container))
    except Exception as exc:                       # noqa: BLE001
        raise _failed(path, f"{type(exc).__name__}: {exc}") from exc
    finally:
        try:
            container.close()
        except Exception:                          # noqa: BLE001
            pass
    if not (info.has_video or info.has_audio):
        raise _failed(path, "it opened but held no audio or video stream")
    return info


# ---------------------------------------------------------------------------
# Pictures
# ---------------------------------------------------------------------------

def scene_score(previous: Any, current: Any) -> float:
    """0-1: how different two grey thumbnails are. Pure. 1.0 for mismatched shapes."""
    import numpy as np

    a = np.asarray(previous, dtype=np.int16)
    b = np.asarray(current, dtype=np.int16)
    if a.shape != b.shape:
        return 1.0
    return float(np.abs(a - b).mean() / 255.0)


def _spread(items: list, keep: int) -> list:
    """`keep` of `items`, evenly spaced and always including the first."""
    if keep >= len(items):
        return list(items)
    if keep <= 1:
        return list(items[:1])
    step = (len(items) - 1) / (keep - 1)
    picked, seen = [], set()
    for k in range(keep):
        index = int(round(k * step))
        if index not in seen:
            seen.add(index)
            picked.append(items[index])
    return picked


def _thumbnail(frame: Any) -> Any:
    return frame.reformat(width=_SIGNATURE_SIZE[0], height=_SIGNATURE_SIZE[1],
                          format="gray").to_ndarray()


def _write_frame(frame: Any, target: Path) -> None:
    width = min(FRAME_WIDTH, int(frame.width))
    height = max(2, int(round(frame.height * width / max(1, frame.width))))
    image = frame.reformat(width=width, height=height, format="rgb24").to_image()
    # A phone held upright stores a sideways picture and a rotation flag.
    turn = int(getattr(frame, "rotation", 0) or 0) % 360
    if turn:
        image = image.rotate(turn, expand=True)
    image.save(target, format="JPEG", quality=JPEG_QUALITY)


def _drop(kept: list, keep: int) -> list:
    """Thin `kept` to `keep` entries, deleting the pictures of those dropped."""
    survivors = _spread(kept, keep)
    chosen = {id(entry) for entry in survivors}
    for entry in kept:
        if id(entry) not in chosen:
            try:
                entry[2].unlink()
            except OSError:
                pass
    return survivors


def extract_keyframes(
    path: Path,
    outdir: Path,
    *,
    interval_s: int,
    cap: int,
    duration_s: Optional[float] = None,
    should_stop: Optional[Callable[[], bool]] = None,
) -> list[Keyframe]:
    """Write scene-change pictures into `outdir`; return them with their times.

    **Only the encoder's own keyframes are decoded**, and no two closer than
    `MIN_SAMPLE_GAP_S`: a keyframe is a complete picture, so the ones between are
    never touched. That is the whole speed of this function (377x real time on an
    87-minute 1080p film, against 0.5x-6.5x for decoding every frame).

    A picture is kept when it is the first; when it differs from the previously
    *looked at* one by more than `HARD_CUT_THRESHOLD` (a real cut); when it
    differs by more than `SCENE_THRESHOLD` *and* `SCENE_MIN_GAP_S` have passed
    since the last one kept (so hand-held shake is not a scene); or when
    `interval_s` seconds have passed since the last one kept - the last clause is
    what makes a two-hour meeting that is one static slide after another yield a
    picture per minute rather than none. At most `cap` are returned: past twice the cap the
    pool is thinned to evenly spaced survivors, so a three-hour film is covered
    end to end rather than only its opening.

    **A decode error that still produced pictures is success**: a truncated tail
    or one unreadable GOP must not cost 200 good frames. Errors with no pictures
    at all raise `ERR_MEDIA_PROBE_FAILED`. `should_stop` is asked before each
    picture is decoded and raises `ERR_MEDIA_INTERRUPTED` - a queue, not a failure.
    """
    av = _import_av(path)
    cap = max(1, int(cap))
    interval = max(1, int(interval_s))
    # A ceiling, not a target: a hung network drive must not hold a worker for
    # ever. The scan runs hundreds of times faster than the film plays.
    budget_s = max(300.0, float(duration_s or 0.0))
    started = time.monotonic()
    try:
        container = av.open(str(path))
    except Exception as exc:                       # noqa: BLE001
        raise _failed(path, f"{type(exc).__name__}: {exc}") from exc

    kept: list = []                                # (seconds, score, file)
    errors: list[str] = []
    state: dict[str, Any] = {"number": 0, "previous": None, "last_kept_t": None,
                             "next_look": 0.0}

    def take(frames: Any, fallback_s: float) -> None:
        nonlocal kept
        for frame in frames:
            seconds = frame.time
            if seconds is None:
                seconds = fallback_s
            seconds = max(0.0, float(seconds))
            state["next_look"] = seconds + MIN_SAMPLE_GAP_S
            try:
                signature = _thumbnail(frame)
                previous = state["previous"]
                score = 1.0 if previous is None else scene_score(previous, signature)
                state["previous"] = signature
                last = state["last_kept_t"]
                if not (last is None
                        or score > HARD_CUT_THRESHOLD
                        or (score > SCENE_THRESHOLD and seconds - last >= SCENE_MIN_GAP_S)
                        or seconds - last >= interval):
                    continue
                state["number"] += 1
                target = outdir / f"kf_{state['number']:05d}.jpg"
                _write_frame(frame, target)
            except Exception as exc:                # noqa: BLE001 - one picture
                errors.append(f"{type(exc).__name__}: {exc}")
                continue
            state["last_kept_t"] = seconds
            kept.append((seconds, score, target))
            if len(kept) > 2 * cap:
                kept = _drop(kept, cap)

    try:
        if not container.streams.video:
            return []
        stream = container.streams.video[0]
        # Frame threading is what makes a 1080p scan fast, and it has a cost this
        # loop must honour: the decoder holds back as many pictures as it has
        # threads, and hands them over only when it is flushed. The flush is the
        # final, empty packet demux ends with (`dts is None`) - **skipping it,
        # as the first version did, returned no pictures at all for a short
        # mpeg4 file** (found by decoding a generated one).
        stream.thread_type = "AUTO"
        stream.codec_context.skip_frame = "NONKEY"
        for packet in container.demux(stream):
            if packet.dts is None:
                try:
                    take(packet.decode(), 0.0)
                except Exception as exc:            # noqa: BLE001 - the flush of a damaged tail
                    errors.append(f"{type(exc).__name__}: {exc}")
                continue
            if not packet.is_keyframe:
                continue
            at = (float(packet.pts * packet.time_base)
                  if packet.pts is not None and packet.time_base is not None else 0.0)
            if at < state["next_look"]:
                continue
            if time.monotonic() - started > budget_s:
                log.warning("stopped taking pictures from {} after {:.0f}s; keeping {}",
                            path.name, budget_s, len(kept))
                break
            if should_stop is not None and should_stop():
                raise AppErrorException(make_error(
                    "ERR_MEDIA_INTERRUPTED", "extract.media_tools", path=str(path)))
            try:
                frames = packet.decode()
            except Exception as exc:                # noqa: BLE001 - one damaged GOP
                errors.append(f"{type(exc).__name__}: {exc}")
                continue
            take(frames, at)
    except AppErrorException:
        raise
    except Exception as exc:                        # noqa: BLE001 - keep what was read
        errors.append(f"{type(exc).__name__}: {exc}")
    finally:
        try:
            container.close()
        except Exception:                           # noqa: BLE001
            pass
    if len(kept) > cap:
        kept = _drop(kept, cap)
    if not kept:
        if errors:
            raise _failed(path, "no pictures could be read: " + errors[0])
        return []
    return [Keyframe(seconds=s, path=p) for s, _score, p in kept]
