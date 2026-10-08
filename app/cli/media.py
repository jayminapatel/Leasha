r"""`media`: what Leasha can do with videos and recordings on this machine.

Layer: L2 (the CLI every layer ships before its UI)

    python -m app.cli media --status
    python -m app.cli media "D:\Family\holiday.mp4"
    python -m app.cli media "D:\Family\holiday.mp4" --transcribe --model tiny
    python -m app.cli media "D:\Voice\memo.m4a" --json
    python -m app.cli media --measure "D:\Voice\memo.m4a" --model tiny
    python -m app.cli media --find "a birthday cake with candles"

Read-only: it opens no index and writes nothing but a temporary folder it
removes. `--status` is the answer to "why are my videos only found by name?" -
PyAV (which reads the video), faster-whisper and the speech model, each with the
command that fixes it. No ffmpeg program is needed - see `app/extract/media_tools.py`. A path shows what an index run would write for that one file, through the
same extractor, whichever switches are on in `.env`: naming a file is
deliberate, so the switches do not apply to it.

`--find` asks the per-picture index (work order 202626270515, "per-frame CLIP")
which *moments* of which videos look like the words: the film and the time in it,
nearest first. It is the headless way to prove that lane, as `--status` is for the
switches; it needs videos to have been indexed with the picture model available.

`--measure` is the one-off throughput measurement the work order asks for. It
needs faster-whisper and a downloaded model, refuses politely without them, and
prints minutes of audio per hour of processor so the pacing defaults can be set
from a number.
"""

from __future__ import annotations

import argparse
import json
import time
import wave
from dataclasses import replace
from pathlib import Path
from typing import Any, Optional

from app.cli._common import EXIT_ERROR, EXIT_OK, _load, _report
from app.core.errors import AppErrorException, make_error
from app.core.logging import setup_logging


def _duration_of(path: Path) -> Optional[float]:
    """Seconds of audio in `path`: PyAV if there, the standard library for a
    WAV, otherwise unknown. Never raises."""
    try:
        from app.extract import media_tools

        return media_tools.probe(path).duration_s
    except Exception:                              # noqa: BLE001 - a number, not a verdict
        pass
    if path.suffix.lower() == ".wav":
        try:
            with wave.open(str(path), "rb") as handle:
                return handle.getnframes() / float(handle.getframerate() or 1)
        except (wave.Error, OSError, EOFError):
            return None
    return None


def moments_for(
    text: str, embedder: Any, frames: Any, store: Any, *, limit: int = 10,
) -> list[dict[str, Any]]:
    """The moments in videos that look like `text`, nearest first. Pure enough to test.

    `embedder` is the CLIP **text** tower (`.embed([text])`), `frames` the
    `VideoFrameVectorStore`, `store` the SQLite store that turns a file id back
    into a path. A file that has since been removed from the index is left out.
    """
    from app.extract.timecode import format_timecode

    vector = embedder.embed([text])[0]
    found: list[dict[str, Any]] = []
    for hit in frames.search_frames(vector, k=max(1, limit)):
        record = store.get_file_by_id(hit.file_id)
        if record is None:
            continue
        found.append({
            "path": record.path, "seconds": hit.seconds,
            "at": format_timecode(hit.seconds), "distance": round(hit.distance, 4)})
    return found


def _find(text: str, args: argparse.Namespace, settings: Any) -> int:
    """`--find TEXT`: the per-frame CLIP lane, headless. Exits 0 with a sentence
    when nothing is indexed - an empty lane is a state, not a fault."""
    from app.search import vector as search_vector
    from app.storage.sqlite_store import SqliteStore
    from app.storage.vector_store import ImageVectorStore

    embedder = search_vector.clip_text_embedder_from_settings(settings)
    with SqliteStore(settings.fts_db) as store, \
            ImageVectorStore(settings.vector_path) as images:
        found = moments_for(text, embedder, images.video_frames(), store,
                            limit=int(args.limit))
    if args.json:
        print(json.dumps(found, indent=2))
        return EXIT_OK
    if not found:
        print("No video pictures are indexed yet - switch on 'Read videos on this "
              "computer' and index a folder with a film in it.")
        return EXIT_OK
    for item in found:
        print(f"  {item['at']:>8}  {item['path']}")
    return EXIT_OK


def status_report(settings: Any) -> dict[str, Any]:
    """Everything `--status` prints, as data. Pure enough to test."""
    from app.extract import media, media_tools, transcribe

    config = media.MediaConfig.from_settings(settings)
    tools = media_tools.tools_status()
    return {
        "tools": tools,
        "faster_whisper_installed": transcribe.available(),
        "model": config.model,
        "model_downloaded": transcribe.model_present(config.model, config.model_dir),
        "model_dir": str(config.model_dir) if config.model_dir else "",
        "settings": {
            "VIDEO_INDEXING_ENABLED": config.video_enabled,
            "AUDIO_TRANSCRIPTION_ENABLED": config.audio_enabled,
            "TRANSCRIBE_MODEL": config.model,
            "VIDEO_KEYFRAME_INTERVAL_S": config.keyframe_interval_s,
            "VIDEO_KEYFRAME_CAP": config.keyframe_cap,
        },
        "fix": {
            "av": r"venv\Scripts\python.exe -m pip install av==18.1.0",
            "faster_whisper": r"venv\Scripts\python.exe -m pip install av==18.1.0",   # 2026-09-29: PyAV reads the sound
            "model": transcribe.download_command(config.model),
        },
        "video_ready": all(tools.values()),
        "audio_ready": transcribe.available(),
        # The measured figure the pacing defaults and the Settings cost line come
        # from - see `transcribe.MEASURED_REALTIME_FACTOR` for where it is from.
        "speech_throughput": {
            "realtime_factor": transcribe.MEASURED_REALTIME_FACTOR,
            "measured": transcribe.MEASURED_ON,
        },
    }


def _print_status(report: dict[str, Any]) -> None:
    print("Videos and recordings")
    print("=" * 68)
    for name, where in report["tools"].items():
        print(f"  {name:<14} {where or 'NOT FOUND'}")
    if not all(report["tools"].values()):
        print(f"      fix: {report['fix']['av']}")
    print(f"  {'speech engine':<14} "     # 2026-09-29: ONNX Runtime; was faster-whisper
          f"{'installed' if report['faster_whisper_installed'] else 'NOT INSTALLED'}")
    if not report["faster_whisper_installed"]:
        print(f"      fix: {report['fix']['faster_whisper']}")
    print(f"  {'model ' + report['model']:<14} "
          f"{'downloaded' if report['model_downloaded'] else 'NOT DOWNLOADED'}"
          f"   ({report['model_dir'] or 'no model folder'})")
    if not report["model_downloaded"]:
        print(f"      fix: {report['fix']['model']}")
    print()
    print("Switches in force (both are off unless you turned them on):")
    for key, value in report["settings"].items():
        print(f"  {key:<30} {value}")
    print()
    print(f"  Videos read here:      {'yes' if report['video_ready'] else 'no - PyAV missing'}")
    print(f"  Speech transcribed:    "
          f"{'yes' if report['audio_ready'] and report['model_downloaded'] else 'no'}")
    speed = report["speech_throughput"]
    print(f"  Speech throughput:     about {speed['realtime_factor']:g}x real time "
          f"({speed['measured']}); run `media --measure FILE` to check this machine")


def _show_file(path: Path, args: argparse.Namespace, settings: Any) -> int:
    """What an index run would write for this one file, through the same
    extractor. The switches are forced on for the duration (`media.configure`)
    and put back in `finally`, because naming a file is the instruction."""
    from app.extract import extractor_for, media

    extractor = extractor_for(path)
    if extractor is None or getattr(extractor, "name", "") not in ("video", "audio"):
        print(f"{path.name} is not a video or audio type Leasha reads.")
        return EXIT_ERROR

    base = media.MediaConfig.from_settings(settings)
    config = replace(
        base, video_enabled=True,
        audio_enabled=bool(args.transcribe),
        model=args.model or base.model)
    media.configure(config)
    try:
        documents = list(extractor.extract(path))
    finally:
        media.configure(None)

    try:
        payload = []
        for document in documents:
            payload.append({
                "path": str(document.path),
                "layers": document.meta.get("layers", []),
                "read_by": document.meta.get("read_by"),
                "duration_s": document.meta.get("duration_s"),
                "keyframes": len(document.meta.get("keyframes", [])),
                "warnings": [w.render() for w in document.warnings],
                "anchors": len(document.anchors),
                "text": document.text,
            })
        if args.json:
            print(json.dumps(payload, indent=2, default=str))
        else:
            for item in payload:
                print(f"{path.name}")
                print(f"  layers    {', '.join(item['layers'])}   (read_by {item['read_by']})")
                print(f"  pictures  {item['keyframes']}   timestamps {item['anchors']}")
                for warning in item["warnings"]:
                    print(f"  WARNING   {warning}")
                print()
                print(item["text"] if len(item["text"]) < 4000
                      else item["text"][:4000] + "\n...")
    finally:
        for document in documents:
            media.release_keyframes(document.meta)
    return EXIT_OK


def _measure(path: Path, args: argparse.Namespace, settings: Any) -> int:
    """Transcribe one file into a scratch journal and print the rate."""
    import tempfile

    from app.extract import media, transcribe

    config = media.MediaConfig.from_settings(settings)
    model = args.model or config.model
    if not transcribe.available():
        raise AppErrorException(make_error(
            "ERR_TRANSCRIBE_UNAVAILABLE", "cli.media", path=str(path)))

    duration = _duration_of(path)
    with tempfile.TemporaryDirectory(prefix="leasha-measure-") as scratch:
        started = time.monotonic()
        result = transcribe.transcribe(
            path, model=model, model_dir=config.model_dir, journal_dir=Path(scratch))
        elapsed = time.monotonic() - started

    if args.json:
        print(json.dumps({
            "model": model, "audio_seconds": duration, "elapsed_seconds": round(elapsed, 2),
            "segments": len(result.segments), "language": result.language,
            "realtime_factor": round(duration / elapsed, 2) if duration and elapsed else None,
            "audio_minutes_per_cpu_hour": (
                round(duration / elapsed * 60, 1) if duration and elapsed else None),
        }, indent=2))
        return EXIT_OK

    print(f"Model {model}: {len(result.segments)} passages, language "
          f"{result.language or 'unknown'}, {elapsed:.1f}s elapsed")
    if duration and elapsed:
        print(f"  {duration:.0f}s of audio in {elapsed:.0f}s = {duration / elapsed:.2f}x real time")
        print(f"  = {duration / elapsed * 60:.0f} minutes of audio per hour of processor")
    else:
        print("  audio length unknown (install PyAV, or use a .wav), so no rate")
    print("  Record this number in the work order's promotion checklist.")
    return EXIT_OK


def cmd_media(args: argparse.Namespace) -> int:
    """Dispatch on the one verb given; `--status` is the default. Every branch
    reports through `AppErrorException` so a missing library is a sentence with
    the install command, never a traceback (non-negotiable 2)."""
    settings = _load(args)
    setup_logging(settings.log_path)
    try:
        if args.find:
            return _find(args.find, args, settings)
        if args.measure:
            return _measure(Path(args.measure).expanduser(), args, settings)
        if args.path:
            return _show_file(Path(args.path).expanduser(), args, settings)

        report = status_report(settings)
        if args.json:
            print(json.dumps(report, indent=2))
        else:
            _print_status(report)
        return EXIT_OK
    except AppErrorException as exc:
        return _report(exc.error, args.json)


def add_media_parser(sub: argparse._SubParsersAction, common: argparse.ArgumentParser) -> None:
    p = sub.add_parser(
        "media", parents=[common],
        help="videos and recordings: what is installed, and what one file yields")
    p.add_argument("path", nargs="?", help="a video or audio file to read and show")
    p.add_argument("--status", action="store_true",
                   help="what is installed and what is switched on (the default "
                        "when no file is given)")
    p.add_argument("--transcribe", action="store_true",
                   help="also transcribe the speech (needs a downloaded speech model)")
    p.add_argument("--model", choices=("tiny", "base", "small", "medium"),
                   help="speech model to use instead of TRANSCRIBE_MODEL")
    p.add_argument("--find", metavar="TEXT",
                   help="which moments of which indexed videos look like TEXT "
                        "(the per-picture index)")
    p.add_argument("--limit", type=int, default=10,
                   help="with --find: how many moments to show (default 10)")
    p.add_argument("--measure", metavar="FILE",
                   help="transcribe FILE once and print minutes of audio per hour "
                        "of processor")
    p.set_defaults(func=cmd_media)
