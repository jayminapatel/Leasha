r"""What ffprobe and ffmpeg tell us about a video or audio file.

Layer: L2

Two questions, two programs, both run through
`converter.run_media_tool` - the one place in the application that starts a
process, so the allow-list, `shell=False`, the timeout ceiling and the logged
absolute path all apply here without this module having to say so.

  * `probe`            (ffprobe)  duration, resolution, codecs, the date the
                                  file was *recorded*, and GPS if a phone left it
  * `extract_keyframes` (ffmpeg)  a frame at every scene change, plus at least
                                  one every N seconds, capped

**Nothing here imports an FFmpeg binding.** PyAV, `ffmpeg-python` and the rest
either link FFmpeg's libraries or wrap them, and FFmpeg's licence depends on
how a given build was configured. Running the program is aggregation; linking
it is not. `tests/unit/test_media.py` walks this package's imports and fails on
any of them.

**Everything parses text the tool printed, and every parser is pure**, so each
one is tested with a canned string and no FFmpeg on the machine - which is the
machine this was written on.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from app.core.errors import AppErrorException, make_error
from app.core.logging import logger
from app.extract import converter

__all__ = [
    "MediaInfo",
    "Keyframe",
    "probe",
    "parse_probe",
    "parse_iso6709",
    "extract_keyframes",
    "parse_showinfo_times",
    "tools_status",
    "SCENE_THRESHOLD",
]

log = logger.bind(component="extract.media_tools")

#: How different two frames must be to count as a new scene, 0-1. ffmpeg's own
#: documentation suggests 0.3-0.5 for "a clear cut"; the lower end is used
#: because the fixed-interval fallback below already covers a static talk, and a
#: missed cut in a holiday film is worse than one extra frame.
#:
#: **Not a setting.** Nobody can be asked to choose this number, and the two
#: things that do matter - how many pictures at most, and how long a gap at
#: most - are settings.
SCENE_THRESHOLD = 0.30

#: ffprobe is quick - it reads headers - so a generous ceiling here means a
#: hung network drive, not a slow file.
PROBE_TIMEOUT_S = 60

#: Pictures are scaled down before being written. CLIP looks at 224 pixels and
#: OCR needs legible text, not a 4K frame: 800 wide keeps a slide readable at a
#: fraction of the disk and decode cost.
FRAME_WIDTH = 800

#: A `creation_time` earlier than this is a camera with a dead clock battery or
#: an editor that wrote zero, not a recording date. Epoch-0 (1970) is the
#: common one; the same reasoning as EXIF's "0000:00:00".
_EARLIEST_PLAUSIBLE = datetime(1990, 1, 1, tzinfo=timezone.utc)


@dataclass
class MediaInfo:
    """What ffprobe found. Every field may be absent; nothing here is required."""

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
# Tools
# ---------------------------------------------------------------------------

def tools_status() -> dict[str, Optional[str]]:
    """`{"ffmpeg": path-or-None, "ffprobe": path-or-None}`. Never raises.

    The same `resolve_binary` the run itself uses, so Settings and `doctor`
    cannot say "found" while a real run says "missing" - the bug the converter
    module's own history records.
    """
    return {name: converter.resolve_binary(name) for name in ("ffmpeg", "ffprobe")}


# ---------------------------------------------------------------------------
# ffprobe
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
    """An ffprobe `creation_time` (ISO 8601, usually `...Z`), or None."""
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
    """ffprobe's `-print_format json` output as a `MediaInfo`. Never raises.

    Tolerant on purpose: ffprobe's output varies by container and version, and a
    missing field is the normal case, not an error. What it cannot read it
    leaves at the default.
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


def probe(path: Path) -> MediaInfo:
    """Run ffprobe on one file.

    Raises `ERR_MEDIA_TOOLS_MISSING` if ffprobe is not installed, and
    `ERR_MEDIA_PROBE_FAILED` if it ran and found nothing it could call media - a
    truncated download, a renamed text file. Both are per-file skips.
    """
    finished = converter.run_media_tool(
        "ffprobe",
        ["-v", "error", "-print_format", "json", "-show_format", "-show_streams",
         "-i", str(path)],
        timeout_s=PROBE_TIMEOUT_S, source=path,
    )
    stdout = (finished.stdout or b"").decode("utf-8", "replace")
    info = parse_probe(stdout)
    if finished.returncode != 0 or not (info.has_video or info.has_audio):
        detail = (finished.stderr or b"").decode("utf-8", "replace").strip()[:300]
        raise AppErrorException(make_error(
            "ERR_MEDIA_PROBE_FAILED", "extract.media_tools", path=str(path),
            details=(detail or f"ffprobe exit {finished.returncode} and no audio or "
                               f"video stream in what it printed"),
        ))
    return info


# ---------------------------------------------------------------------------
# ffmpeg: keyframes
# ---------------------------------------------------------------------------

_SHOWINFO_TIME = re.compile(r"pts_time:\s*(-?\d+(?:\.\d+)?)")


def parse_showinfo_times(stderr: str) -> list[float]:
    """The `pts_time` of every frame `showinfo` reported, in order.

    ffmpeg writes one `showinfo` line per frame that reaches it - here, per
    *selected* frame - and the numbered files it writes come out in the same
    order, so element N of this list is the timestamp of `kf_00000N+1.jpg`.
    """
    return [max(0.0, float(m.group(1))) for m in _SHOWINFO_TIME.finditer(stderr or "")]


def _select_expression(interval_s: int) -> str:
    """Scene change, or the first frame, or `interval_s` since the last one.

    The **first frame** clause (`isnan(prev_selected_t)`) guarantees a video with
    no cuts at all still yields a picture. The **interval** clause is what makes
    a two-hour recorded meeting - one static slide, then another - produce a
    frame per slide rather than one in total. Single quotes are ffmpeg's own
    filtergraph quoting, so the commas inside survive without a shell.
    """
    return (f"select='gt(scene,{SCENE_THRESHOLD})"
            f"+isnan(prev_selected_t)"
            f"+gte(t-prev_selected_t,{int(interval_s)})'")


def extract_keyframes(
    path: Path,
    outdir: Path,
    *,
    interval_s: int,
    cap: int,
    duration_s: Optional[float] = None,
) -> list[Keyframe]:
    """Write scene-change pictures into `outdir`; return them with their times.

    At most `cap` pictures. **A non-zero exit that still produced pictures is
    success**: ffmpeg complains about a truncated tail or an unreadable last GOP
    on files that are otherwise fine, and throwing away 200 good frames because
    the 201st was damaged is the wrong trade.
    """
    cap = max(1, int(cap))
    pattern = str(outdir / "kf_%05d.jpg")
    args = [
        "-nostdin", "-hide_banner", "-v", "info", "-i", str(path),
        "-an", "-sn",
        "-vf", f"{_select_expression(interval_s)},scale='min({FRAME_WIDTH},iw)':-2,showinfo",
        "-fps_mode", "vfr", "-frames:v", str(cap), "-q:v", "5", pattern,
    ]
    # Decoding every frame is the cost, and it scales with length: give it about
    # the file's own running time (real-time decode is the slow floor), never
    # less than five minutes.
    timeout = int(max(300, (duration_s or 0) * 1.0))
    finished = converter.run_media_tool(
        "ffmpeg", args, timeout_s=timeout, source=path)

    stderr = (finished.stderr or b"").decode("utf-8", "replace")
    times = parse_showinfo_times(stderr)
    files = sorted(p for p in outdir.glob("kf_*.jpg") if p.is_file() and p.stat().st_size)
    if not files:
        if finished.returncode != 0:
            raise AppErrorException(make_error(
                "ERR_MEDIA_PROBE_FAILED", "extract.media_tools", path=str(path),
                details=f"ffmpeg exit {finished.returncode}, no pictures written. "
                        f"{stderr.strip()[-300:]}",
            ))
        return []

    frames: list[Keyframe] = []
    for index, file in enumerate(files):
        # If `showinfo` printed fewer lines than there are files (a build that
        # words it differently), fall back to spreading them evenly rather than
        # claiming every picture is from second zero.
        if index < len(times):
            seconds = times[index]
        elif duration_s:
            seconds = duration_s * index / max(len(files), 1)
        else:
            seconds = 0.0
        frames.append(Keyframe(seconds=seconds, path=file))
    return frames
