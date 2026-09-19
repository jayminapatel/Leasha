r"""Video and audio, end to end, on a machine with no ffmpeg and no faster-whisper.

Work order 202626270515. Layer: L2 / L3.

**Nothing here needs FFmpeg or faster-whisper installed**, which is the machine
this was written on and the machine most people will have. ffprobe and ffmpeg
are faked at `converter.run_media_tool` - the one place a process is started -
with canned JSON and a few generated pictures; speech is faked at the
`TranscriberEngine` seam; reading a picture is faked at `media.read_frame`. What
is real is everything Leasha owns: the parsers, the extractors, the journals,
the pipeline, the store.

Two properties are the point of the file:

  * **Off means off.** With both switches off a `.mp4` is exactly what it was
    before this order - findable by name, no tool ever started.
  * **Absent means said.** With the switches on and the tools missing, the run
    finishes, one file is one skip-ledger line, and the line names the command
    that fixes it.
"""

from __future__ import annotations

import ast
import json
import math
import os
import subprocess
import time
import wave
from pathlib import Path

import pytest

from app.core.errors import AppErrorException
from app.extract import converter, media, media_tools, transcribe
from app.extract.base import extract, extractor_for
from app.extract.timecode import describe_timecode, format_timecode, parse_timecode
from app.index.embedder import Embedder, l2_normalise
from app.index.pipeline import Pipeline, PipelineConfig
from app.index.walker import WalkConfig
from app.storage.sqlite_store import FileStatus, SqliteStore

ROOT = Path(__file__).resolve().parents[2]
LONG_AGO = 3600

# --------------------------------------------------------------------------
# Canned tool output
# --------------------------------------------------------------------------

def probe_json(*, video=True, audio=True, created="2019-07-03T14:22:11.000000Z",
               duration="761.4", location="+51.5074-000.1278/", cover_art=False) -> str:
    streams = []
    if video:
        streams.append({
            "codec_type": "video", "codec_name": "h264", "width": 1920, "height": 1080,
            "avg_frame_rate": "30000/1001", "disposition": {"attached_pic": 0}})
    if cover_art:
        streams.append({
            "codec_type": "video", "codec_name": "mjpeg", "width": 500, "height": 500,
            "avg_frame_rate": "0/0", "disposition": {"attached_pic": 1}})
    if audio:
        streams.append({"codec_type": "audio", "codec_name": "aac"})
    tags = {"title": "Ben's birthday", "location": location}
    if created:
        tags["creation_time"] = created
    return json.dumps({
        "streams": streams,
        "format": {"format_name": "mov,mp4", "duration": duration, "bit_rate": "5000000",
                   "tags": tags},
    })


SHOWINFO = "\n".join(
    f"[Parsed_showinfo_2 @ 0x1] n:   {i} pts: {int(t * 1000)} pts_time:{t} pos: 1 fmt:yuv420p"
    for i, t in enumerate((0.0, 30.5, 95.25)))


class FakeTools:
    """Stands in for `converter.run_media_tool` and records what was run."""

    def __init__(self, *, probe=None, frames=3):
        self.probe = probe if probe is not None else probe_json()
        self.frames = frames
        self.calls: list[tuple[str, list[str]]] = []

    def __call__(self, name, args, *, timeout_s, source=None, cwd=None):
        self.calls.append((name, list(args)))
        if name == "ffprobe":
            return subprocess.CompletedProcess(args, 0, self.probe.encode(), b"")
        outdir = Path(args[-1]).parent
        for i in range(1, self.frames + 1):
            (outdir / f"kf_{i:05d}.jpg").write_bytes(b"\xff\xd8\xff" + bytes([i]) * 200)
        return subprocess.CompletedProcess(args, 0, b"", SHOWINFO.encode())


@pytest.fixture()
def tools(monkeypatch):
    fake = FakeTools()
    monkeypatch.setattr(converter, "run_media_tool", fake)
    return fake


@pytest.fixture(autouse=True)
def _clean_state():
    """No test leaks its switches or its engine into the next."""
    media.configure(None)
    transcribe.reset_engine()
    yield
    media.configure(None)
    transcribe.set_engine_factory(None)


@pytest.fixture()
def frames(monkeypatch):
    """Reading a picture, faked: a slide with words, a scene, another slide."""
    readings = {
        "kf_00001": media.FrameReading(text="Welcome to Ben's party"),
        "kf_00002": media.FrameReading(caption="A birthday cake on a table", tags=("cake", "candles")),
        "kf_00003": media.FrameReading(text="Goodbye and thank you"),
    }
    monkeypatch.setattr(media, "read_frame", lambda path: readings[Path(path).stem])
    return readings


class FakeEngine:
    """`TranscriberEngine`, with no model. Can be killed part-way."""

    language = "en"
    SEGMENTS = (
        transcribe.SpeechSegment(0.0, 9.0, "Happy birthday to you"),
        transcribe.SpeechSegment(10.0, 19.0, "Now we cut the cake"),
        transcribe.SpeechSegment(20.0, 29.0, "Thank you all for coming"),
        transcribe.SpeechSegment(761.0, 769.0, "The treasure is under the oak tree"),
    )

    class Killed(BaseException):
        """Not an Exception: what a killed process looks like from inside."""

    def __init__(self, *, die_after=None):
        self.die_after = die_after
        self.starts: list[float] = []
        self.yielded = 0

    def transcribe(self, path, start_s=0.0):
        self.starts.append(start_s)
        for segment in self.SEGMENTS:
            if segment.end <= start_s:
                continue
            if self.die_after is not None and self.yielded >= self.die_after:
                raise self.Killed()
            self.yielded += 1
            yield segment


def use_engine(engine):
    transcribe.set_engine_factory(lambda model, model_dir: engine)


def enabled(tmp_path, **over) -> media.MediaConfig:
    return media.MediaConfig(
        video_enabled=True, audio_enabled=True, journal_dir=tmp_path / "journals", **over)


def make_video(directory: Path, name="holiday.mp4") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"x" * 400)
    stamp = time.time() - LONG_AGO
    os.utime(path, (stamp, stamp))
    return path


def make_wav(directory: Path, name="memo.wav", seconds=1) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(8000)
        handle.writeframes(b"\x00\x00" * 8000 * seconds)
    return path


# ==========================================================================
# Timestamps
# ==========================================================================

def test_a_timestamp_reads_the_way_a_player_shows_it():
    assert format_timecode(0) == "0:00"
    assert format_timecode(7.9) == "0:07"                # truncated, never after the words
    assert format_timecode(761) == "12:41"
    assert format_timecode(3725) == "1:02:05"
    assert format_timecode(-4) == "0:00"
    assert format_timecode("nonsense") == "0:00"


def test_a_timestamp_round_trips_and_a_cell_address_is_not_one():
    for seconds in (0, 59, 61, 761, 3599, 3600, 7325):
        assert parse_timecode(format_timecode(seconds)) == seconds
    assert parse_timecode("Q3!A14") is None
    assert parse_timecode("12:99") is None
    assert parse_timecode(None) is None


def test_a_result_says_when_the_words_were_said():
    from app.ui.presenter.results import to_row

    class Hit:
        rank, chunk_id, file_id, path, text, page = 1, 1, 1, "D:/v/holiday.mp4", "the oak tree", None
        label = "12:41"

    assert describe_timecode("12:41") == "at 12:41"
    assert to_row(Hit(), ["oak"]).location == "at 12:41"
    Hit.label = "Q3!D14"                                 # a spreadsheet is untouched
    assert to_row(Hit(), ["oak"]).location.startswith("Sheet")


# ==========================================================================
# Layer 0: ffprobe's output, parsed
# ==========================================================================

def test_the_container_yields_length_picture_and_the_date_it_was_recorded():
    info = media_tools.parse_probe(probe_json())
    assert info.has_video and info.has_audio
    assert (info.width, info.height, info.video_codec) == (1920, 1080, "h264")
    assert info.duration_s == pytest.approx(761.4)
    assert info.frame_rate == pytest.approx(29.97)
    assert info.created.year == 2019 and info.created.month == 7
    assert info.tags["title"] == "Ben's birthday"


def test_a_phones_gps_becomes_coordinates_and_no_fix_is_no_place():
    info = media_tools.parse_probe(probe_json())
    assert (info.latitude, info.longitude) == pytest.approx((51.5074, -0.1278))
    assert media_tools.parse_probe(probe_json(location="+00.0000+000.0000/")).latitude is None
    assert media_tools.parse_iso6709("garbage") is None
    assert media_tools.parse_iso6709("+95.0000+000.0000/") is None


def test_a_dead_clock_battery_is_not_a_recording_date():
    assert media_tools.parse_probe(probe_json(created="1970-01-01T00:00:00Z")).created is None
    assert media_tools.parse_probe(probe_json(created=None)).created is None


def test_cover_art_inside_an_mp3_does_not_make_it_a_video():
    info = media_tools.parse_probe(probe_json(video=False, cover_art=True))
    assert info.has_audio and not info.has_video


@pytest.mark.parametrize("junk", ["", "not json", "[]", "null", '{"streams": 4}'])
def test_unreadable_probe_output_is_an_empty_answer_not_a_crash(junk):
    info = media_tools.parse_probe(junk)
    assert not info.has_video and not info.has_audio


def test_showinfo_timestamps_are_read_in_order():
    assert media_tools.parse_showinfo_times(SHOWINFO) == [0.0, 30.5, 95.25]
    assert media_tools.parse_showinfo_times("") == []


def test_the_scene_filter_takes_the_first_frame_and_one_every_interval():
    expression = media_tools._select_expression(45)
    assert "gt(scene," in expression and "isnan(prev_selected_t)" in expression
    assert "gte(t-prev_selected_t,45)" in expression


def test_keyframes_come_back_with_their_times_and_the_cap_reaches_ffmpeg(tmp_path, tools):
    found = media_tools.extract_keyframes(
        tmp_path / "v.mp4", tmp_path, interval_s=30, cap=7, duration_s=761)
    assert [round(f.seconds, 2) for f in found] == [0.0, 30.5, 95.25]
    assert all(f.path.is_file() for f in found)
    name, args = tools.calls[-1]
    assert name == "ffmpeg" and args[args.index("-frames:v") + 1] == "7"


# ==========================================================================
# The one place a process starts
# ==========================================================================

def test_ffmpeg_and_ffprobe_are_on_the_allow_list_by_name_and_deliberately():
    assert {"ffmpeg", "ffprobe"} <= converter.ALLOWED_BINARIES
    assert {"ffmpeg", "ffprobe"} <= set(converter._WINDOWS_LOCATIONS)
    assert set(converter._WINDOWS_LOCATIONS) <= converter.ALLOWED_BINARIES


def test_a_name_off_the_list_is_refused_before_anything_is_resolved(monkeypatch):
    looked_up = []
    monkeypatch.setattr(converter, "resolve_binary", lambda name: looked_up.append(name))
    with pytest.raises(AppErrorException) as caught:
        converter.run_media_tool("curl", ["http://example.com"], timeout_s=5)
    assert caught.value.error.code == "ERR_CONVERTER_BLOCKED"
    assert looked_up == []


def test_a_missing_tool_says_what_to_install(monkeypatch):
    monkeypatch.setattr(converter, "resolve_binary", lambda name: None)
    with pytest.raises(AppErrorException) as caught:
        converter.run_media_tool("ffprobe", ["-i", "x.mp4"], timeout_s=5)
    error = caught.value.error
    assert error.code == "ERR_MEDIA_TOOLS_MISSING"
    assert "winget install --id Gyan.FFmpeg" in (error.action_payload or "")
    assert error.suggestion


def test_the_process_is_started_without_a_shell_and_with_a_list(monkeypatch):
    seen = {}

    def fake_run(command, **kwargs):
        seen.update(command=command, **kwargs)
        return subprocess.CompletedProcess(command, 0, b"", b"")

    monkeypatch.setattr(converter, "resolve_binary", lambda name: rf"C:\ff\{name}.exe")
    monkeypatch.setattr(converter.subprocess, "run", fake_run)
    converter.run_media_tool("ffprobe", ["-i", "a file; rm -rf.mp4"], timeout_s=10_000_000)
    assert seen["shell"] is False
    assert isinstance(seen["command"], list) and seen["command"][-1] == "a file; rm -rf.mp4"
    assert seen["timeout"] == converter.MAX_MEDIA_TIMEOUT_S


def test_no_module_imports_an_ffmpeg_binding():
    """FFmpeg's licence depends on how a build was configured. Running the program
    is aggregation; linking it would not be, and the application is MIT."""
    banned = {"av", "ffmpeg", "imageio_ffmpeg", "moviepy", "pydub", "ffmpeg_python", "pyav"}
    offenders = []
    for path in (ROOT / "app").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module.split(".")[0]]
            offenders += [f"{path.name}: {n}" for n in names if n in banned]
    assert not offenders


# ==========================================================================
# The extractors
# ==========================================================================

def test_both_extractors_are_registered_and_typescript_is_left_alone():
    assert extractor_for(Path("holiday.mp4")).name == "video"
    assert extractor_for(Path("memo.m4a")).name == "audio"
    assert extractor_for(Path("app.ts")).name != "video"
    assert extractor_for(Path("mod.mts")).name != "video"


def test_a_video_is_its_container_its_screen_and_its_speech(tmp_path, tools, frames):
    use_engine(FakeEngine())
    media.configure(enabled(tmp_path))
    path = make_video(tmp_path)

    (document,) = list(extract(path))

    text = document.text
    assert "Video: holiday.mp4" in text and "1920x1080" in text
    assert "12 minutes 41 seconds" in text and "Recorded: 03 July 2019" in text
    assert "On screen: Welcome to Ben's party" in text
    assert "Scene: A birthday cake on a table" in text
    assert "The treasure is under the oak tree" in text
    assert document.meta["layers"] == ["metadata", "keyframes", "speech"]
    assert document.meta["read_by"] == "whisper"
    assert document.date.year == 2019
    assert document.meta["media_created_ns"] > 0
    # Held for the pipeline's CLIP step, which is what releases them.
    assert len(document.meta["keyframes"]) == 3
    assert Path(document.meta["keyframe_dir"]).is_dir()
    media.release_keyframes(document.meta)
    assert "keyframe_dir" not in document.meta


def test_a_transcript_hit_carries_the_time_it_was_said(tmp_path, tools, frames):
    use_engine(FakeEngine())
    media.configure(enabled(tmp_path))
    (document,) = list(extract(make_video(tmp_path)))
    locate = document.anchor_lookup()
    offset = document.text.index("The treasure is under the oak tree")
    assert locate(offset) == "12:41"
    assert locate(document.text.index("Now we cut the cake")) == "0:10"
    # The words on screen are timestamped from the picture's own time.
    assert locate(document.text.index("Welcome to Ben")) == "0:00"
    assert locate(document.text.index("Goodbye and thank you")) == "1:35"
    media.release_keyframes(document.meta)


def test_the_same_words_held_on_screen_are_one_line_at_the_first_moment(tmp_path, tools, monkeypatch):
    monkeypatch.setattr(media, "read_frame", lambda path: media.FrameReading(text="Agenda"))
    media.configure(media.MediaConfig(video_enabled=True, journal_dir=tmp_path / "j"))
    (document,) = list(extract(make_video(tmp_path)))
    assert document.text.count("On screen: Agenda") == 1
    media.release_keyframes(document.meta)


def test_speech_is_left_out_when_its_switch_is_off(tmp_path, tools, frames):
    engine = FakeEngine()
    use_engine(engine)
    media.configure(media.MediaConfig(video_enabled=True, audio_enabled=False,
                                      journal_dir=tmp_path / "j"))
    (document,) = list(extract(make_video(tmp_path)))
    assert "treasure" not in document.text and engine.starts == []
    assert document.meta["layers"] == ["metadata", "keyframes"]
    assert document.meta["read_by"] == "ocr"
    media.release_keyframes(document.meta)


def test_an_audio_file_is_layer_two_with_no_pictures(tmp_path, monkeypatch):
    fake = FakeTools(probe=probe_json(video=False, created="2020-02-02T10:00:00Z", location=""))
    monkeypatch.setattr(converter, "run_media_tool", fake)
    use_engine(FakeEngine())
    media.configure(enabled(tmp_path))
    (document,) = list(extract(make_wav(tmp_path)))
    assert "Audio recording: memo.wav" in document.text
    assert "The treasure is under the oak tree" in document.text
    assert document.meta["layers"] == ["metadata", "speech"]
    assert document.meta["read_by"] == "whisper"
    assert not any(name == "ffmpeg" for name, _ in fake.calls)      # no pictures asked for
    assert "keyframes" not in document.meta


def test_a_recording_is_transcribed_even_when_ffprobe_is_not_installed(tmp_path, monkeypatch):
    monkeypatch.setattr(converter, "resolve_binary", lambda name: None)
    use_engine(FakeEngine())
    media.configure(enabled(tmp_path))
    (document,) = list(extract(make_wav(tmp_path)))
    assert "Happy birthday to you" in document.text
    assert document.meta["layers"] == ["metadata", "speech"]


# ==========================================================================
# Absent is a state, not a crash
# ==========================================================================

def test_a_video_with_no_ffmpeg_is_one_skip_that_names_the_fix(tmp_path, monkeypatch):
    monkeypatch.setattr(converter, "resolve_binary", lambda name: None)
    media.configure(enabled(tmp_path))
    with pytest.raises(AppErrorException) as caught:
        list(extract(make_video(tmp_path)))
    error = caught.value.error
    assert error.code == "ERR_MEDIA_TOOLS_MISSING"
    assert "ffprobe" in error.message
    assert error.action_payload == "winget install --id Gyan.FFmpeg -e"


def test_a_recording_with_no_speech_package_and_no_ffprobe_says_what_to_install(tmp_path, monkeypatch):
    monkeypatch.setattr(converter, "resolve_binary", lambda name: None)
    monkeypatch.setattr(transcribe, "available", lambda: False)
    media.configure(enabled(tmp_path))
    with pytest.raises(AppErrorException) as caught:
        list(extract(make_wav(tmp_path)))
    error = caught.value.error
    assert error.code == "ERR_TRANSCRIBE_UNAVAILABLE"
    assert "pip install faster-whisper" in error.action_payload


def test_a_missing_model_is_named_and_never_downloaded(tmp_path, monkeypatch):
    def refuse(model, model_dir):
        raise FileNotFoundError("Cannot find an appropriate cached snapshot folder")

    def factory(model, model_dir):
        try:
            refuse(model, model_dir)
        except FileNotFoundError as exc:
            raise transcribe._classify_load_failure(exc, model, "memo.wav") from exc

    monkeypatch.setattr(converter, "resolve_binary", lambda name: None)
    transcribe.set_engine_factory(factory)
    media.configure(enabled(tmp_path, model="tiny"))
    with pytest.raises(AppErrorException) as caught:
        list(extract(make_wav(tmp_path)))
    error = caught.value.error
    assert error.code == "ERR_TRANSCRIBE_MODEL_MISSING"
    assert "download_model('base')" in error.action_payload or "tiny" in error.message
    # The real loader is offline by construction.
    source = (ROOT / "app" / "extract" / "transcribe.py").read_text(encoding="utf-8")
    assert "local_files_only=True" in source


def test_a_video_whose_speech_cannot_be_read_still_indexes_its_container(tmp_path, tools, frames, monkeypatch):
    monkeypatch.setattr(transcribe, "available", lambda: False)
    media.configure(enabled(tmp_path))
    (document,) = list(extract(make_video(tmp_path)))
    assert "Video: holiday.mp4" in document.text
    assert [w.code for w in document.warnings] == ["ERR_TRANSCRIBE_UNAVAILABLE"]
    assert "speech" not in document.meta["layers"]
    media.release_keyframes(document.meta)


def test_a_damaged_file_is_one_skip(tmp_path, monkeypatch):
    monkeypatch.setattr(converter, "run_media_tool",
                        lambda *a, **k: subprocess.CompletedProcess([], 1, b"", b"moov atom not found"))
    media.configure(enabled(tmp_path))
    with pytest.raises(AppErrorException) as caught:
        list(extract(make_video(tmp_path)))
    assert caught.value.error.code == "ERR_MEDIA_PROBE_FAILED"
    assert "moov atom" in (caught.value.error.details or "")


def test_a_failed_video_leaves_no_pictures_behind(tmp_path, tools, monkeypatch):
    monkeypatch.setattr(media, "read_frame", lambda path: (_ for _ in ()).throw(RuntimeError("boom")))
    media.configure(media.MediaConfig(video_enabled=True, journal_dir=tmp_path / "j"))
    before = set(Path(os.environ.get("TEMP", "/tmp")).glob(media.KEYFRAME_DIR_PREFIX + "*"))
    with pytest.raises(RuntimeError):
        list(extract(make_video(tmp_path)))
    after = set(Path(os.environ.get("TEMP", "/tmp")).glob(media.KEYFRAME_DIR_PREFIX + "*"))
    assert after == before


# ==========================================================================
# Resumable: kill it mid-transcription, run it again, it carries on
# ==========================================================================

def test_a_killed_transcription_resumes_from_the_last_kept_passage(tmp_path):
    journal_dir = tmp_path / "journals"
    audio = make_wav(tmp_path)

    first = FakeEngine(die_after=2)
    with pytest.raises(FakeEngine.Killed):
        transcribe.transcribe(audio, engine=first, journal_dir=journal_dir)
    assert first.yielded == 2                                # two passages were kept

    second = FakeEngine()
    result = transcribe.transcribe(audio, engine=second, journal_dir=journal_dir)

    assert second.starts == [19.0]                           # carried on, did not start over
    assert result.resumed_from_s == 19.0
    assert [s.text for s in result.segments] == [s.text for s in FakeEngine.SEGMENTS]
    assert len({s.start for s in result.segments}) == len(result.segments)   # no doubles


def test_a_finished_transcript_is_not_transcribed_twice(tmp_path):
    journal_dir = tmp_path / "journals"
    audio = make_wav(tmp_path)
    transcribe.transcribe(audio, engine=FakeEngine(), journal_dir=journal_dir)
    again = FakeEngine()
    result = transcribe.transcribe(audio, engine=again, journal_dir=journal_dir)
    assert again.starts == [] and result.from_cache and result.language == "en"


def test_a_changed_file_or_model_starts_clean(tmp_path):
    journal_dir = tmp_path / "journals"
    audio = make_wav(tmp_path)
    transcribe.transcribe(audio, engine=FakeEngine(die_after=None), journal_dir=journal_dir, model="tiny")
    other_model = FakeEngine()
    transcribe.transcribe(audio, engine=other_model, journal_dir=journal_dir, model="base")
    assert other_model.starts == [0.0]
    audio.write_bytes(audio.read_bytes() + b"\x00\x00" * 100)      # the file changed
    changed = FakeEngine()
    transcribe.transcribe(audio, engine=changed, journal_dir=journal_dir, model="base")
    assert changed.starts == [0.0]


def test_a_torn_last_line_costs_one_passage_not_the_file(tmp_path):
    audio = make_wav(tmp_path)
    journal = transcribe.TranscriptJournal(tmp_path / "j", audio, "base")
    journal.append(transcribe.SpeechSegment(0.0, 9.0, "kept"))
    with journal.path.open("a", encoding="utf-8") as handle:
        handle.write('{"s": 10.0, "e": 19.0, "t": "torn ha')       # a kill mid-write
    segments, _language, done = journal.load()
    assert [s.text for s in segments] == ["kept"] and not done


def test_a_stop_request_keeps_what_was_read_and_says_it_is_queued(tmp_path):
    audio = make_wav(tmp_path)
    with pytest.raises(AppErrorException) as caught:
        transcribe.transcribe(audio, engine=FakeEngine(), journal_dir=tmp_path / "j",
                              should_stop=lambda: True)
    assert caught.value.error.code == "ERR_MEDIA_INTERRUPTED"
    segments, _l, done = transcribe.TranscriptJournal(tmp_path / "j", audio, "base").load()
    assert len(segments) == 1 and not done
    assert "ERR_MEDIA_INTERRUPTED" in Pipeline.DEFERRED_SKIP_CODES


# ==========================================================================
# Settings
# ==========================================================================

def test_both_switches_default_off_and_the_registry_says_why():
    from app.core import settings_registry as reg

    for key in ("VIDEO_INDEXING_ENABLED", "AUDIO_TRANSCRIPTION_ENABLED"):
        setting = reg.by_key(key)
        assert setting.default is False and "Off by default" in setting.help
    assert media.MediaConfig().video_enabled is False
    assert media.MediaConfig().audio_enabled is False


def test_the_five_settings_are_declared_with_ranges_and_choices():
    from app.core import settings_registry as reg

    assert reg.by_key("TRANSCRIBE_MODEL").choices == transcribe.MODELS
    assert reg.by_key("TRANSCRIBE_MODEL").default in transcribe.MODELS
    assert (reg.by_key("VIDEO_KEYFRAME_INTERVAL_S").minimum,
            reg.by_key("VIDEO_KEYFRAME_INTERVAL_S").maximum) == (5, 600)
    assert (reg.by_key("VIDEO_KEYFRAME_CAP").minimum,
            reg.by_key("VIDEO_KEYFRAME_CAP").maximum) == (10, 1000)


def test_settings_are_clamped_and_a_bad_model_falls_back():
    class Loose:
        video_indexing_enabled = True
        audio_transcription_enabled = False
        transcribe_model = "GIGANTIC"
        video_keyframe_interval_s = 1
        video_keyframe_cap = 999_999
        data_path = Path("D:/Data")
        model_cache = Path("D:/Data/models")

    config = media.MediaConfig.from_settings(Loose())
    assert config.model == transcribe.DEFAULT_MODEL
    assert (config.keyframe_interval_s, config.keyframe_cap) == (5, 1000)
    assert config.model_dir == Path("D:/Data/models") / "whisper"


def test_a_media_box_holds_all_five_and_says_what_is_missing(monkeypatch):
    pytest.importorskip("PyQt6")
    from PyQt6.QtWidgets import QApplication, QWidget

    from app.ui.widgets.media_box import MediaBox, model_sentence, tools_sentence

    monkeypatch.setattr(converter, "resolve_binary", lambda name: None)
    monkeypatch.setattr(transcribe, "available", lambda: False)
    QApplication.instance() or QApplication([])

    class Stored:
        video_indexing_enabled = True
        audio_transcription_enabled = False
        transcribe_model = "small"
        video_keyframe_interval_s = 30
        video_keyframe_cap = 50
        model_cache = None

    box = MediaBox(Stored())
    for key in ("VIDEO_INDEXING_ENABLED", "AUDIO_TRANSCRIPTION_ENABLED", "TRANSCRIBE_MODEL",
                "VIDEO_KEYFRAME_INTERVAL_S", "VIDEO_KEYFRAME_CAP"):
        assert box.findChild(QWidget, key) is not None, key
    assert box.values() == {
        "VIDEO_INDEXING_ENABLED": True, "AUDIO_TRANSCRIPTION_ENABLED": False,
        "TRANSCRIBE_MODEL": "small", "VIDEO_KEYFRAME_INTERVAL_S": 30, "VIDEO_KEYFRAME_CAP": 50}
    assert "winget install --id Gyan.FFmpeg -e" in box.tools_note.text()
    assert "pip install faster-whisper" in box.speech_note.text()
    assert "download_model('small')" in box.model_note.text()
    assert "restart" in box.restart_note.text().lower() or "starts" in box.restart_note.text()
    assert "found" in tools_sentence({"ffmpeg": "C:/x/ffmpeg.exe", "ffprobe": "C:/x/ffprobe.exe"})
    assert "downloaded" in model_sentence("base", True)


def test_opening_the_box_writes_nothing():
    pytest.importorskip("PyQt6")
    from PyQt6.QtWidgets import QApplication

    from app.ui.widgets.media_box import MediaBox

    QApplication.instance() or QApplication([])
    emitted = []
    box = MediaBox(None)
    box.changed.connect(emitted.append)
    box.load(type("S", (), {"video_indexing_enabled": True, "transcribe_model": "tiny"})())
    assert emitted == []


def test_the_status_report_is_honest_on_a_bare_machine(monkeypatch, tmp_path):
    from app.cli.media import status_report

    monkeypatch.setattr(converter, "resolve_binary", lambda name: None)
    monkeypatch.setattr(transcribe, "available", lambda: False)

    class S:
        model_cache = tmp_path
        data_path = tmp_path

    report = status_report(S())
    assert report["video_ready"] is False and report["audio_ready"] is False
    assert report["model_downloaded"] is False
    assert report["settings"]["VIDEO_INDEXING_ENABLED"] is False
    assert "winget" in report["fix"]["ffmpeg"] and "faster-whisper" in report["fix"]["faster_whisper"]


def test_the_media_command_is_registered():
    from app.cli import build_parser

    args = build_parser().parse_args(["media", "--status"])
    assert args.command == "media" and args.status


# ==========================================================================
# The pipeline
# ==========================================================================

class NullVectors:
    def delete_by_file_ids(self, file_ids):
        pass

    def add(self, **kwargs):
        return len(kwargs.get("chunk_ids") or ())

    def __getattr__(self, name):
        return lambda *args, **kwargs: None


class FakeImageVectors:
    def __init__(self):
        self.rows: list[tuple[int, list[float]]] = []

    def ensure_table(self):
        pass

    def delete_by_file_ids(self, ids):
        pass

    def add_images(self, file_ids, vectors, exts=None, mtimes_ns=None):
        self.rows += list(zip(file_ids, vectors))
        return len(file_ids)

    def __getattr__(self, name):
        return lambda *args, **kwargs: None


class FakeImageEmbedder:
    dim = 4

    def __init__(self):
        self.seen: list[list[str]] = []

    def embed(self, paths):
        self.seen.append(list(paths))
        return [[1.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0], [0.0, 0.0, 1.0, 0.0]][: len(paths)]


def _embedder(dim: int = 8) -> Embedder:
    return Embedder(dim=dim, encoder=lambda texts: [
        l2_normalise([math.sin(abs(hash(t)) % 100 + i) for i in range(dim)]) for t in texts])


def _corpus(tmp_path) -> Path:
    root = tmp_path / "family"
    root.mkdir()
    (root / "notes.txt").write_text("Shopping list: milk and eggs.", encoding="utf-8")
    stamp = time.time() - LONG_AGO
    os.utime(root / "notes.txt", (stamp, stamp))
    make_video(root)
    return root


def _pipeline(store, root, *, config=None, image_embedder=None, image_vectors=None, **extra):
    return Pipeline(
        store, NullVectors(), _embedder(),
        PipelineConfig(walk=WalkConfig(roots=[root]), workers=1, media=config, **extra),
        image_embedder=image_embedder, image_vectors=image_vectors)


def test_switched_off_a_video_is_found_by_name_and_no_tool_ever_starts(tmp_path, tools):
    root = _corpus(tmp_path)
    with SqliteStore(tmp_path / "index.db") as store:
        stats = _pipeline(store, root).run()            # no media settings at all
        record = store.get_file(str(root / "holiday.mp4"))
    assert record.status == FileStatus.NAME_ONLY
    assert tools.calls == []
    assert stats.indexed == 1                           # the text file only


def test_each_switch_admits_only_its_own_extensions(tmp_path):
    media.configure(media.MediaConfig(video_enabled=True))
    walked = WalkConfig(roots=[]).resolved_extensions()
    assert ".mp4" in walked and ".mp3" not in walked
    media.configure(media.MediaConfig(audio_enabled=True))
    walked = WalkConfig(roots=[]).resolved_extensions()
    assert ".mp3" in walked and ".mp4" not in walked


def test_a_video_goes_through_the_whole_pipeline(tmp_path, tools, frames):
    use_engine(FakeEngine())
    root = _corpus(tmp_path)
    clip, image_vectors = FakeImageEmbedder(), FakeImageVectors()
    with SqliteStore(tmp_path / "index.db") as store:
        stats = _pipeline(store, root, config=enabled(tmp_path),
                          image_embedder=clip, image_vectors=image_vectors).run()
        path = str(root / "holiday.mp4")
        record = store.get_file(path)
        rows = store.conn.execute(
            "SELECT c.text, c.label FROM chunks c JOIN files f ON f.id = c.file_id "
            "WHERE f.path = ?", (path,)).fetchall()
        record_id = record.id

    assert stats.indexed == 2 and not stats.skipped_by_code
    assert record.status == FileStatus.INDEXED
    joined = " ".join(r["text"] for r in rows)
    assert "The treasure is under the oak tree" in joined
    assert "Welcome to Ben's party" in joined
    # Located: a chunk says where in the video it starts.
    assert {r["label"] for r in rows} <= {"0:00", "0:10", "0:20", "12:41"} and         all(r["label"] for r in rows)
    # Recorded date from the container, not the copy date.
    assert record.taken_at_ns is not None
    assert time.gmtime(record.taken_at_ns / 1e9).tm_year == 2019
    # One CLIP vector per video, the mean of its pictures, written by the same lane.
    assert [fid for fid, _v in image_vectors.rows] == [record_id]
    assert len(clip.seen[0]) == 3
    assert math.isclose(sum(v * v for v in image_vectors.rows[0][1]), 1.0, rel_tol=1e-6)
    # And the pictures are gone: the pipeline released them after the CLIP step.
    assert not any(Path(p).exists() for p in clip.seen[0])


def test_a_transcript_passage_is_findable_by_keyword(tmp_path, tools, frames):
    use_engine(FakeEngine())
    root = _corpus(tmp_path)
    with SqliteStore(tmp_path / "index.db") as store:
        _pipeline(store, root, config=enabled(tmp_path)).run()
        hits = store.conn.execute(
            "SELECT c.label FROM chunks c WHERE c.text LIKE '%treasure%'").fetchall()
    assert hits and hits[0]["label"] in {"0:00", "0:10", "0:20", "12:41"}


def test_the_run_finishes_when_the_tools_are_missing_and_the_ledger_names_the_fix(tmp_path, monkeypatch):
    monkeypatch.setattr(converter, "resolve_binary", lambda name: None)
    root = _corpus(tmp_path)
    with SqliteStore(tmp_path / "index.db") as store:
        stats = _pipeline(store, root, config=enabled(tmp_path)).run()
        record = store.get_file(str(root / "holiday.mp4"))
        notes = store.get_file(str(root / "notes.txt"))
    assert stats.skipped_by_code == {"ERR_MEDIA_TOOLS_MISSING": 1}
    # FAILED, not SKIPPED: its action is RUN_COMMAND (install something), which
    # `AppError.is_fatal` treats as needing attention - the same status as
    # ERR_OCR_UNAVAILABLE. Both are settled rows that `--retry-skipped` reopens.
    assert record.status in (FileStatus.SKIPPED, FileStatus.FAILED)
    assert record.skip_code == "ERR_MEDIA_TOOLS_MISSING"
    assert notes.status == FileStatus.INDEXED            # one bad file never halts a run


def test_a_text_only_pass_holds_video_like_a_picture_and_the_images_pass_reads_it(tmp_path, tools, frames):
    use_engine(FakeEngine())
    root = _corpus(tmp_path)
    with SqliteStore(tmp_path / "index.db") as store:
        first = _pipeline(store, root, config=enabled(tmp_path), ocr_mode="text").run()
        held = store.get_file(str(root / "holiday.mp4"))
        assert first.skipped_by_code == {"ERR_MEDIA_HELD": 1} and tools.calls == []
        assert held.skip_code == "ERR_MEDIA_HELD"
        second = _pipeline(store, root, config=enabled(tmp_path), ocr_mode="images").run()
        done = store.get_file(str(root / "holiday.mp4"))
    assert second.indexed == 1 and done.status == FileStatus.INDEXED


def test_stop_mid_transcription_then_rerun_continues_and_finishes(tmp_path, tools, frames):
    """The acceptance test for 'resumable': a run stopped inside a recording keeps
    its passages, the file is queued rather than failed, and the next run carries
    on from where the last one stopped instead of starting over."""
    root = _corpus(tmp_path)
    config = enabled(tmp_path)

    class StopsItself(FakeEngine):
        def transcribe(self, path, start_s=0.0):
            for segment in super().transcribe(path, start_s):
                yield segment
                if self.yielded == 2 and not self.starts[1:]:
                    pipeline.request_stop()             # the window closes here

    first_engine = StopsItself()
    use_engine(first_engine)
    with SqliteStore(tmp_path / "index.db") as store:
        pipeline = _pipeline(store, root, config=config)
        pipeline.run()
        row = store.get_file(str(root / "holiday.mp4"))
        assert row is None or row.status != FileStatus.INDEXED    # not marked done

        second_engine = FakeEngine()
        use_engine(second_engine)
        stats = _pipeline(store, root, config=config).run()
        done = store.get_file(str(root / "holiday.mp4"))
        texts = " ".join(r["text"] for r in store.conn.execute(
            "SELECT c.text FROM chunks c JOIN files f ON f.id = c.file_id WHERE f.path = ?",
            (str(root / "holiday.mp4"),)))

    kept = first_engine.yielded                           # passages the first run journalled
    assert 2 <= kept < len(FakeEngine.SEGMENTS)
    assert second_engine.starts == [FakeEngine.SEGMENTS[kept - 1].end]   # resumed, not restarted
    assert second_engine.yielded == len(FakeEngine.SEGMENTS) - kept      # and did only the rest
    assert done.status == FileStatus.INDEXED
    assert "Happy birthday to you" in texts and "oak tree" in texts
    assert stats.indexed >= 1


# ==========================================================================
# Findability from day one: type, badge, size
# ==========================================================================

def test_type_video_finds_the_family_films_before_anything_is_read():
    from app.search.query import parse_query

    assert {"mp4", "mov", "mkv"} <= set(parse_query("type:video birthday").ext)
    assert "mp3" in parse_query("type:audio").ext
    assert parse_query("type:video birthday").text == "birthday"


def test_a_hit_in_a_video_or_recording_is_badged_as_one():
    from app.ui.presenter.results import kind_tag

    assert kind_tag("mp4") == "VID" and kind_tag("m4a") == "AUD"


def test_a_film_is_not_too_big_to_read_but_a_big_text_file_still_is(tmp_path):
    from app.index.walker import walk

    make_video(tmp_path, "big.mp4")                       # 400+ bytes, over the cap below
    (tmp_path / "huge.txt").write_text("x" * 500, encoding="utf-8")
    media.configure(media.MediaConfig(video_enabled=True))
    found = {c.path.name: c for c in walk(WalkConfig(roots=[tmp_path], max_file_bytes=100))}
    assert found["big.mp4"].readable is True
    assert found["huge.txt"].readable is False
