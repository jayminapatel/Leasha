r"""Video and audio, end to end, on a machine with no faster-whisper and no model.

Work order 202626270515. Layer: L2 / L3.

**PyAV is real here** (it is what reads a container and takes the pictures), and
the tests that need it skip cleanly when it is absent. What is faked is what is
heavy or absent: a speech model is faked at the `TranscriberEngine` seam, reading
a picture (OCR, Florence-2) at `media.read_frame`, and - for the wiring tests that
do not care about pixels - `media_tools.probe` and `extract_keyframes` with
canned answers. What is real is everything Leasha owns: the parsers, the
extractors, the journals, the pipeline, the store. `test_media_real.py` runs the
real speech model on a real recording when both are present.

Two properties are the point of the file:

  * **Off means off.** With both switches off a `.mp4` is exactly what it was
    before this order - findable by name, no tool ever started.
  * **Absent means said.** With the switches on and PyAV missing, the run
    finishes, one file is one skip-ledger line, and the line names the command
    that fixes it.
"""

from __future__ import annotations

import ast
import json
import math
import os
import time
import wave
from pathlib import Path

import pytest

from app.core.errors import AppErrorException, make_error
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


SCENES = (0.0, 30.5, 95.25)


def probe_info(**kwargs) -> media_tools.MediaInfo:
    return media_tools.parse_probe(probe_json(**kwargs))


class FakeMedia:
    """Stands in for the two PyAV calls and records what was asked of them."""

    def __init__(self, *, info=None, frames=3):
        self.info = info if info is not None else probe_info()
        self.frames = frames
        self.calls: list[tuple[str, object]] = []

    def probe(self, path):
        self.calls.append(("probe", str(path)))
        return self.info

    def extract_keyframes(self, path, outdir, *, interval_s, cap, duration_s=None,
                          should_stop=None):
        self.calls.append(("keyframes", {"interval_s": interval_s, "cap": cap}))
        found = []
        for i in range(1, self.frames + 1):
            target = Path(outdir) / f"kf_{i:05d}.jpg"
            target.write_bytes(b"\xff\xd8\xff" + bytes([i]) * 200)
            found.append(media_tools.Keyframe(SCENES[i - 1], target))
        return found


@pytest.fixture()
def tools(monkeypatch):
    fake = FakeMedia()
    monkeypatch.setattr(media_tools, "probe", fake.probe)
    monkeypatch.setattr(media_tools, "extract_keyframes", fake.extract_keyframes)
    return fake


def no_pyav(monkeypatch):
    """PyAV is not installed: the one thing `probe` and `extract_keyframes` do first."""
    def refuse(path=None):
        raise AppErrorException(make_error(
            "ERR_MEDIA_TOOLS_MISSING", "extract.media_tools",
            binary="av (PyAV)", path=str(path or "")))

    monkeypatch.setattr(media_tools, "_import_av", refuse)


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


def test_scene_score_is_zero_for_the_same_picture_and_large_for_a_cut():
    np = pytest.importorskip("numpy")
    dark = np.zeros((90, 160), dtype=np.uint8)
    light = np.full((90, 160), 200, dtype=np.uint8)
    assert media_tools.scene_score(dark, dark) == 0.0
    assert media_tools.scene_score(dark, light) > media_tools.SCENE_THRESHOLD * 10
    assert media_tools.scene_score(dark, np.zeros((9, 16), dtype=np.uint8)) == 1.0


def test_thinning_keeps_the_first_and_spreads_the_rest_evenly():
    picked = media_tools._spread(list(range(10)), 3)
    assert picked[0] == 0 and picked[-1] == 9 and len(picked) == 3
    assert media_tools._spread(list(range(10)), 1) == [0]
    assert media_tools._spread([1, 2], 5) == [1, 2]


# ==========================================================================
# Layer 0 and 1 against a real file, made by PyAV itself
# ==========================================================================

def make_real_video(path: Path, *, colours=((250, 0, 0), (0, 250, 0), (0, 0, 250)),
                    seconds_each=2, fps=10, audio=True,
                    created="2019-07-03T14:22:11.000000Z",
                    location="+51.5074-000.1278/") -> Path:
    """A small real mp4: solid-colour scenes, a keyframe every second, a tone."""
    import fractions

    av = pytest.importorskip("av")
    np = pytest.importorskip("numpy")
    path.parent.mkdir(parents=True, exist_ok=True)
    out = av.open(str(path), "w")
    out.metadata["creation_time"] = created
    if location:
        out.metadata["location"] = location
    out.metadata["title"] = "Ben's birthday"
    video = out.add_stream("mpeg4", rate=fps)
    video.width, video.height, video.pix_fmt = 96, 64, "yuv420p"
    video.codec_context.gop_size = fps
    tone = None
    if audio:
        tone = out.add_stream("aac", rate=16000)
        tone.layout = "mono"
    n = 0
    for colour in colours:
        for _ in range(seconds_each * fps):
            image = np.zeros((64, 96, 3), dtype=np.uint8)
            image[:] = colour
            frame = av.VideoFrame.from_ndarray(image, format="rgb24")
            frame.pts, frame.time_base = n, fractions.Fraction(1, fps)
            for packet in video.encode(frame):
                out.mux(packet)
            n += 1
    for packet in video.encode():
        out.mux(packet)
    if tone is not None:
        total = 16000 * seconds_each * len(colours)
        sound = (np.sin(2 * np.pi * 440 * np.arange(total) / 16000) * 8000).astype(np.int16)
        for pos in range(0, total, 1024):
            frame = av.AudioFrame.from_ndarray(
                sound[pos:pos + 1024].reshape(1, -1), format="s16", layout="mono")
            frame.sample_rate, frame.pts = 16000, pos
            for packet in tone.encode(frame):
                out.mux(packet)
        for packet in tone.encode():
            out.mux(packet)
    out.close()
    return path


def test_a_real_file_yields_length_picture_date_and_place(tmp_path):
    path = make_real_video(tmp_path / "birthday.mp4")
    info = media_tools.probe(path)
    assert info.has_video and info.has_audio
    assert (info.width, info.height) == (96, 64) and info.video_codec == "mpeg4"
    assert info.duration_s == pytest.approx(6.0, abs=0.3)
    assert info.created.year == 2019 and info.created.month == 7
    assert (info.latitude, info.longitude) == pytest.approx((51.5074, -0.1278))
    assert info.tags["title"] == "Ben's birthday"


def test_a_real_file_gives_one_picture_per_scene_with_its_time(tmp_path):
    """Three flat colours, two seconds each: the first frame and the two cuts are
    kept, the keyframes between them (one a second) are the same scene and are not.
    This is also the regression test for the decoder flush: with frame threading
    the first version returned **no pictures at all** for a file this short."""
    path = make_real_video(tmp_path / "scenes.mp4")
    out = tmp_path / "pics"
    out.mkdir()
    found = media_tools.extract_keyframes(path, out, interval_s=60, cap=50, duration_s=6.0)
    assert [round(f.seconds) for f in found] == [0, 2, 4]
    assert all(f.path.is_file() and f.path.stat().st_size > 100 for f in found)


def test_hand_held_shake_is_not_a_scene_but_a_real_cut_still_is(tmp_path):
    """Found on the owner's real family clips, and the reason `SCENE_MIN_GAP_S` and
    `HARD_CUT_THRESHOLD` exist: at `SCENE_THRESHOLD` alone, 40 sampled real phone
    clips gave 24.6 pictures each and 120 for one 29-second clip, because a shaking
    hand changes a few percent of the picture between every keyframe. A small change
    now counts only once `SCENE_MIN_GAP_S` has passed since the last picture kept; a
    real cut counts at once. Remove either constant from `extract_keyframes` and the
    first half of this fails."""
    shake = tuple((100, 100, 100) if i % 2 == 0 else (115, 115, 115) for i in range(20))
    path = make_real_video(tmp_path / "shake.mp4", colours=shake, seconds_each=1)
    out = tmp_path / "shake_pics"
    out.mkdir()
    found = media_tools.extract_keyframes(path, out, interval_s=600, cap=100, duration_s=20.0)
    times = [round(f.seconds) for f in found]
    assert times[0] == 0 and len(times) <= 4, times
    assert all(b - a >= media_tools.SCENE_MIN_GAP_S - 1 for a, b in zip(times, times[1:]))

    cuts = tuple((0, 0, 0) if i % 2 == 0 else (255, 255, 255) for i in range(12))
    path = make_real_video(tmp_path / "cuts.mp4", colours=cuts, seconds_each=1)
    out2 = tmp_path / "cut_pics"
    out2.mkdir()
    found = media_tools.extract_keyframes(path, out2, interval_s=600, cap=100, duration_s=12.0)
    assert len(found) >= 10                                # every real cut is a picture


def test_the_gap_setting_adds_a_picture_when_nothing_changes(tmp_path):
    path = make_real_video(tmp_path / "still.mp4", colours=((90, 90, 90),), seconds_each=6)
    out = tmp_path / "pics"
    out.mkdir()
    found = media_tools.extract_keyframes(path, out, interval_s=2, cap=50, duration_s=6.0)
    # First frame, then one at least every 2 seconds: 0, 2, 4.
    assert [round(f.seconds) for f in found] == [0, 2, 4]
    one = tmp_path / "one"
    one.mkdir()
    assert len(media_tools.extract_keyframes(path, one, interval_s=600, cap=50)) == 1


def test_the_cap_thins_evenly_and_deletes_what_it_drops(tmp_path):
    # Neighbours differ a lot in brightness, so every change counts as a scene.
    colours = ((250, 0, 0), (0, 250, 0), (0, 0, 250), (250, 250, 0),
               (0, 250, 250), (250, 0, 250), (255, 255, 255), (0, 0, 0))
    path = make_real_video(tmp_path / "many.mp4", colours=colours, seconds_each=2)
    out = tmp_path / "pics"
    out.mkdir()
    found = media_tools.extract_keyframes(path, out, interval_s=60, cap=3, duration_s=16.0)
    assert len(found) == 3 and found[0].seconds == 0.0
    assert found[-1].seconds > 8                                 # covers the end, not just the start
    assert len(list(out.glob("kf_*.jpg"))) == 3                  # the rest were removed from disk


def test_a_stop_request_during_the_scan_is_interrupted_not_failed(tmp_path):
    path = make_real_video(tmp_path / "scenes.mp4")
    out = tmp_path / "pics"
    out.mkdir()
    with pytest.raises(AppErrorException) as caught:
        media_tools.extract_keyframes(path, out, interval_s=60, cap=5, should_stop=lambda: True)
    assert caught.value.error.code == "ERR_MEDIA_INTERRUPTED"


def test_a_real_audio_file_is_audio_and_takes_no_pictures(tmp_path):
    pytest.importorskip("av")
    path = make_wav(tmp_path)
    info = media_tools.probe(path)
    assert info.has_audio and not info.has_video
    out = tmp_path / "pics"
    out.mkdir()
    assert media_tools.extract_keyframes(path, out, interval_s=60, cap=5) == []


def test_a_text_file_renamed_mp4_is_one_skip_with_the_reason(tmp_path):
    pytest.importorskip("av")
    path = tmp_path / "fake.mp4"
    path.write_text("this is not a video, it is a note to self", encoding="utf-8")
    with pytest.raises(AppErrorException) as caught:
        media_tools.probe(path)
    assert caught.value.error.code == "ERR_MEDIA_PROBE_FAILED"
    assert str(path) in caught.value.error.message


def test_the_status_says_which_version_of_the_reader_is_here():
    pytest.importorskip("av")
    status = media_tools.tools_status()
    assert list(status) == ["av"] and status["av"]
    assert media_tools.available()


# ==========================================================================
# No ffmpeg program, one library
# ==========================================================================

def test_ffmpeg_is_not_an_allowed_program():
    """Decided and measured 2026-09-20: PyAV reads the container in 0.1 s and scans
    an 87-minute film's keyframes in 14 s, and `faster-whisper` needs PyAV anyway,
    so a second copy of FFmpeg as a program bought nothing. Putting either name back
    on the list is a decision with a diff, and this is what makes it visible."""
    assert not {"ffmpeg", "ffprobe"} & converter.ALLOWED_BINARIES
    assert not {"ffmpeg", "ffprobe"} & set(converter._WINDOWS_LOCATIONS)
    assert not hasattr(converter, "run_media_tool")
    assert converter.resolve_binary("ffmpeg") is None


def test_a_missing_reader_says_what_to_install(monkeypatch):
    no_pyav(monkeypatch)
    with pytest.raises(AppErrorException) as caught:
        media_tools.probe(Path("x.mp4"))
    error = caught.value.error
    assert error.code == "ERR_MEDIA_TOOLS_MISSING"
    assert "pip install av==18.1.0" in (error.action_payload or "")
    assert error.suggestion and "PyAV" in error.message


def _python_files():
    for path in (ROOT / "app").rglob("*.py"):
        yield path, ast.parse(path.read_text(encoding="utf-8"))


def _imports(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            yield from (a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            yield node.module


def test_only_the_media_modules_import_pyav():
    """**The rule changed on 2026-09-20 and this is the new one.** It used to ban
    every FFmpeg binding, so that FFmpeg would only ever be aggregated. That bought
    no licence separation once `faster-whisper` (which needs `av` to decode audio)
    was in the picture - see `docs/THIRD_PARTY_NOTICES.md` - so PyAV is now the one
    way in, kept in one module so its use is one place to read, test and replace.
    Every *other* FFmpeg wrapper stays banned: a second binding is a second
    dependency and a second set of DLLs for nothing."""
    other = {"ffmpeg", "imageio_ffmpeg", "moviepy", "pydub", "ffmpeg_python", "pyav",
             "vlc", "mpv"}
    offenders, av_users = [], set()
    for path, tree in _python_files():
        for name in _imports(tree):
            top = name.split(".")[0]
            if top == "av":
                av_users.add(path.name)
            elif top in other:
                offenders.append(f"{path.name}: {name}")
    assert not offenders
    assert av_users <= {"media_tools.py"}, av_users


def test_no_module_encodes_media():
    """Leasha only decodes, which is what keeps the libx264/libx265 question in
    `docs/THIRD_PARTY_NOTICES.md` a question about a wheel rather than a question
    about Leasha. Opening a stream for writing, or asking for an encoder, is the
    change that would make it Leasha's."""
    for path, _tree in _python_files():
        source = path.read_text(encoding="utf-8")
        for needle in ("add_stream(", '"libx264"', "'libx264'", '"libx265"',
                       "'libx265'", 'av.open(str(path), "w")'):
            assert needle not in source, f"{path.name} mentions {needle}"


def test_the_bundled_ffmpeg_still_reports_lgpl():
    """The licence statement in `docs/THIRD_PARTY_NOTICES.md` is read from the wheel
    itself: `avutil_license()` in its own DLL. If a future `av` bundles a GPL build
    this fails, and the notice has to be revisited before anything else."""
    import ctypes

    av = pytest.importorskip("av")
    if os.name != "nt":
        pytest.skip("reads the wheel's Windows DLL")
    libs = Path(av.__file__).resolve().parent.parent / "av.libs"
    dlls = sorted(libs.glob("avutil-*.dll"))
    if not dlls:
        pytest.skip("this av build does not bundle its libraries in av.libs")
    handle = ctypes.CDLL(str(dlls[0]))
    handle.avutil_license.restype = ctypes.c_char_p
    licence = handle.avutil_license().decode()
    assert licence.startswith("LGPL"), licence


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
    fake = FakeMedia(info=probe_info(video=False, created="2020-02-02T10:00:00Z", location=""))
    monkeypatch.setattr(media_tools, "probe", fake.probe)
    monkeypatch.setattr(media_tools, "extract_keyframes", fake.extract_keyframes)
    use_engine(FakeEngine())
    media.configure(enabled(tmp_path))
    (document,) = list(extract(make_wav(tmp_path)))
    assert "Audio recording: memo.wav" in document.text
    assert "The treasure is under the oak tree" in document.text
    assert document.meta["layers"] == ["metadata", "speech"]
    assert document.meta["read_by"] == "whisper"
    assert not any(name == "keyframes" for name, _ in fake.calls)      # no pictures asked for
    assert "keyframes" not in document.meta


def test_a_recording_is_transcribed_even_when_pyav_is_not_installed(tmp_path, monkeypatch):
    no_pyav(monkeypatch)
    use_engine(FakeEngine())
    media.configure(enabled(tmp_path))
    (document,) = list(extract(make_wav(tmp_path)))
    assert "Happy birthday to you" in document.text
    assert document.meta["layers"] == ["metadata", "speech"]


# ==========================================================================
# Absent is a state, not a crash
# ==========================================================================

def test_a_video_with_no_pyav_is_one_skip_that_names_the_fix(tmp_path, monkeypatch):
    no_pyav(monkeypatch)
    media.configure(enabled(tmp_path))
    with pytest.raises(AppErrorException) as caught:
        list(extract(make_video(tmp_path)))
    error = caught.value.error
    assert error.code == "ERR_MEDIA_TOOLS_MISSING"
    assert "PyAV" in error.message
    assert error.action_payload == r"venv\Scripts\python.exe -m pip install av==18.1.0"


def test_a_recording_with_no_speech_package_and_no_pyav_says_what_to_install(tmp_path, monkeypatch):
    no_pyav(monkeypatch)
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

    no_pyav(monkeypatch)
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


def test_a_damaged_file_is_one_skip(tmp_path):
    """Not faked: `make_video` writes an `ftyp` box and 400 junk bytes, which is
    what a download cut off before its `moov` atom looks like to the real reader."""
    pytest.importorskip("av")
    media.configure(enabled(tmp_path))
    with pytest.raises(AppErrorException) as caught:
        list(extract(make_video(tmp_path)))
    assert caught.value.error.code == "ERR_MEDIA_PROBE_FAILED"
    assert caught.value.error.details


def test_a_real_video_is_read_end_to_end_by_the_real_reader(tmp_path, monkeypatch):
    """The whole of Layers 0 and 1 on a real file: PyAV opens it, takes the
    pictures, and the words come from the (faked) OCR - only the speech model and
    the OCR engine are stand-ins."""
    pytest.importorskip("av")
    seen: list[Path] = []
    colour_words = {0: "Red slide", 1: "Green slide", 2: "Blue slide"}

    def read(path):
        seen.append(Path(path))
        return media.FrameReading(text=colour_words[len(seen) - 1])

    monkeypatch.setattr(media, "read_frame", read)
    media.configure(media.MediaConfig(video_enabled=True, journal_dir=tmp_path / "j"))
    path = make_real_video(tmp_path / "party.mp4")
    (document,) = list(extract(path))
    text = document.text
    assert "Video: party.mp4" in text and "96x64" in text
    assert "Recorded: 03 July 2019" in text and "Title: Ben's birthday" in text
    assert "On screen: Red slide" in text and "On screen: Blue slide" in text
    assert document.meta["layers"] == ["metadata", "keyframes"]
    assert [round(t) for t, _p in document.meta["keyframes"]] == [0, 2, 4]
    locate = document.anchor_lookup()
    assert locate(text.index("Blue slide")) == "0:04"
    media.release_keyframes(document.meta)
    assert not any(p.exists() for p in seen)


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

    monkeypatch.setattr(media_tools, "available", lambda: False)
    monkeypatch.setattr(media_tools, "version", lambda: None)
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
    assert "pip install av==18.1.0" in box.tools_note.text()
    # The cost, in minutes and from the measured figures, is always on show.
    assert "What it costs" in box.cost_note.text()
    assert transcribe.cost_sentence() in box.cost_note.text()
    assert "pip install faster-whisper" in box.speech_note.text()
    assert "download_model('small')" in box.model_note.text()
    assert "restart" in box.restart_note.text().lower() or "starts" in box.restart_note.text()
    assert "ready" in tools_sentence({"av": "18.1.0"}) and "18.1.0" in tools_sentence({"av": "18.1.0"})
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

    monkeypatch.setattr(media_tools, "available", lambda: False)
    monkeypatch.setattr(media_tools, "version", lambda: None)
    monkeypatch.setattr(transcribe, "available", lambda: False)

    class S:
        model_cache = tmp_path
        data_path = tmp_path

    report = status_report(S())
    assert report["video_ready"] is False and report["audio_ready"] is False
    assert report["model_downloaded"] is False
    assert report["settings"]["VIDEO_INDEXING_ENABLED"] is False
    assert "install av==18.1.0" in report["fix"]["av"] and "faster-whisper" in report["fix"]["faster_whisper"]
    assert report["speech_throughput"]["realtime_factor"] == transcribe.MEASURED_REALTIME_FACTOR


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
    no_pyav(monkeypatch)
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


# ==========================================================================
# The backlog: videos and recordings are read after everything else
# ==========================================================================

def test_a_normal_run_reads_the_video_only_after_every_document(tmp_path, tools, frames):
    """The point of the backlog kind: the film does not stand in front of the
    spreadsheets. Asked from inside the speech engine - the most expensive thing
    the run does - whether the ordinary file is already indexed."""
    root = _corpus(tmp_path)
    seen: dict[str, object] = {}

    class Watcher(FakeEngine):
        def transcribe(self, path, start_s=0.0):
            record = store_ref[0].get_file(str(root / "notes.txt"))
            seen["notes_when_speech_started"] = record.status if record else None
            yield from super().transcribe(path, start_s)

    store_ref: list = []
    use_engine(Watcher())
    with SqliteStore(tmp_path / "index.db") as store:
        store_ref.append(store)
        stats = _pipeline(store, root, config=enabled(tmp_path)).run()
        video = store.get_file(str(root / "holiday.mp4"))
    assert seen["notes_when_speech_started"] == FileStatus.INDEXED
    assert video.status == FileStatus.INDEXED
    assert stats.enrichment_counts["media_transcript"] == 1
    assert stats.indexed == 2 and not stats.skipped_by_code
    assert any("read in the background" in note for note in stats.notices)


def test_the_main_pass_only_queues_the_video_and_says_so_in_the_ledger(tmp_path, tools, monkeypatch):
    from app.index import media_backlog

    monkeypatch.setattr(media_backlog, "drain", lambda *a, **k: None)   # the tail never comes
    root = _corpus(tmp_path)
    with SqliteStore(tmp_path / "index.db") as store:
        stats = _pipeline(store, root, config=enabled(tmp_path)).run()
        row = store.get_file(str(root / "holiday.mp4"))
    assert tools.calls == []                                # nothing was opened
    assert row.skip_code == "ERR_MEDIA_BACKLOG"
    assert stats.skipped_by_code == {"ERR_MEDIA_BACKLOG": 1}
    assert "ERR_MEDIA_BACKLOG" in Pipeline.DEFERRED_SKIP_CODES


def test_a_queued_recording_is_read_by_the_next_run_even_from_another_folder(tmp_path, tools, frames, monkeypatch):
    """The queue is the ledger, not the walk: a run over some *other* folder still
    empties the backlog, so a night's stopped run is finished by any later run."""
    from app.index import media_backlog

    root = _corpus(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "a.txt").write_text("nothing to do with films", encoding="utf-8")
    use_engine(FakeEngine())
    real_drain = media_backlog.drain
    monkeypatch.setattr(media_backlog, "drain", lambda *a, **k: None)
    with SqliteStore(tmp_path / "index.db") as store:
        _pipeline(store, root, config=enabled(tmp_path)).run()
        assert store.get_file(str(root / "holiday.mp4")).skip_code == "ERR_MEDIA_BACKLOG"

        monkeypatch.setattr(media_backlog, "drain", real_drain)
        stats = _pipeline(store, elsewhere, config=enabled(tmp_path)).run()
        done = store.get_file(str(root / "holiday.mp4"))
    assert done.status == FileStatus.INDEXED and done.skip_code is None
    assert stats.enrichment_counts["media_transcript"] == 1


def test_a_stopped_run_leaves_the_queue_alone_and_says_what_is_waiting(tmp_path, tools, frames):
    root = _corpus(tmp_path)
    use_engine(FakeEngine())
    with SqliteStore(tmp_path / "index.db") as store:
        pipeline = _pipeline(store, root, config=enabled(tmp_path))
        original = pipeline._drain_media_backlog
        pipeline._drain_media_backlog = lambda stats, on_progress=None: (
            pipeline.request_stop(), original(stats, on_progress))[1]
        stats = pipeline.run()
        row = store.get_file(str(root / "holiday.mp4"))
    assert row.skip_code == "ERR_MEDIA_BACKLOG" and row.status != FileStatus.INDEXED
    assert stats.enrichment_counts["media_transcript"] == 0
    assert stats.skipped_by_code == {"ERR_MEDIA_BACKLOG": 1}


def test_a_switched_off_kind_is_not_read_from_the_queue(tmp_path, tools, frames):
    """A `.mp4` queued while videos were on stays queued, not read, once they are off."""
    from app.index import media_backlog

    root = _corpus(tmp_path)
    with SqliteStore(tmp_path / "index.db") as store:
        file_id = store.upsert_file(str(root / "holiday.mp4"), size_bytes=400, mtime_ns=1)
        store.mark_skipped(file_id, make_error(
            "ERR_MEDIA_BACKLOG", "test", path=str(root / "holiday.mp4")))
        media.configure(media.MediaConfig(video_enabled=False, audio_enabled=True))
        assert media_backlog.queued_paths(store) == []
        media.configure(media.MediaConfig(video_enabled=True))
        assert media_backlog.queued_paths(store) == [root / "holiday.mp4"]


def test_only_a_default_run_defers_and_only_media_whose_switch_is_on():
    from app.index import media_backlog

    on = PipelineConfig(walk=WalkConfig(roots=[]), media=media.MediaConfig(video_enabled=True))
    media.configure(on.media)
    assert media_backlog.defers(on, Path("a.mp4"))
    assert not media_backlog.defers(on, Path("a.mp3"))          # recordings are off
    assert not media_backlog.defers(on, Path("a.txt"))
    assert not media_backlog.defers(
        PipelineConfig(walk=WalkConfig(roots=[]), media=on.media, ocr_mode="text"), Path("a.mp4"))
    assert not media_backlog.defers(
        PipelineConfig(walk=WalkConfig(roots=[]), media=on.media, ocr_mode="images"), Path("a.mp4"))
    assert not media_backlog.defers(PipelineConfig(walk=WalkConfig(roots=[])), Path("a.mp4"))


# ==========================================================================
# Per-frame CLIP: which minute, not only which film
# ==========================================================================

def _unit(*values):
    norm = math.sqrt(sum(v * v for v in values)) or 1.0
    return [v / norm for v in values]


def test_each_picture_of_a_film_is_searchable_by_file_and_second(tmp_path):
    pytest.importorskip("lancedb")
    from app.storage.vector_store import ImageVectorStore

    with ImageVectorStore(tmp_path / "vec", dim=4) as images:
        frames_table = images.video_frames()
        rows = frames_table.replace_frames(
            7, [(0.0, _unit(1, 0, 0, 0)), (30.5, _unit(0, 1, 0, 0)), (95.25, _unit(0, 0, 1, 0))],
            ext="mp4", mtime_ns=5)
        assert rows == 3
        best = frames_table.search_frames(_unit(0, 1, 0.05, 0), k=3)
        assert (best[0].file_id, best[0].seconds) == (7, 30)
        assert len(best) == 3

        # A re-read film has different pictures: the old ones must not linger.
        frames_table.replace_frames(7, [(10.0, _unit(0, 0, 0, 1))])
        after = frames_table.search_frames(_unit(0, 1, 0, 0), k=10)
        assert {(h.file_id, h.seconds) for h in after} == {(7, 10)}


def test_two_films_do_not_share_a_moment_and_deleting_a_file_deletes_its_frames(tmp_path):
    pytest.importorskip("lancedb")
    from app.storage.vector_store import ImageVectorStore

    with ImageVectorStore(tmp_path / "vec", dim=4) as images:
        images.add_images([7, 8], [_unit(1, 0, 0, 0), _unit(0, 1, 0, 0)])
        table = images.video_frames()
        table.replace_frames(7, [(5.0, _unit(1, 0, 0, 0))])
        table.replace_frames(8, [(5.0, _unit(0, 1, 0, 0))])
        found = {(h.file_id, h.seconds) for h in table.search_frames(_unit(1, 1, 0, 0), k=10)}
        assert found == {(7, 5), (8, 5)}                    # same second, different films

        images.delete_by_file_ids([7])                      # the one call every deletion makes
        left = {(h.file_id, h.seconds) for h in table.search_frames(_unit(1, 1, 0, 0), k=10)}
        assert left == {(8, 5)}


def test_the_pipeline_writes_a_row_per_picture_beside_the_mean(tmp_path, tools, frames):
    pytest.importorskip("lancedb")
    from app.storage.vector_store import ImageVectorStore

    use_engine(FakeEngine())
    root = _corpus(tmp_path)
    clip = FakeImageEmbedder()
    with ImageVectorStore(tmp_path / "vec", dim=4) as images, \
            SqliteStore(tmp_path / "index.db") as store:
        _pipeline(store, root, config=enabled(tmp_path),
                  image_embedder=clip, image_vectors=images).run()
        video_id = store.get_file(str(root / "holiday.mp4")).id
        table = images.video_frames()
        hits = table.search_frames([0.0, 1.0, 0.0, 0.0], k=10)
        mean = images.vector_for(video_id)
    assert {(h.file_id, h.seconds) for h in hits} == {(video_id, 0), (video_id, 30), (video_id, 95)}
    assert (hits[0].file_id, hits[0].seconds) == (video_id, 30)     # the 2nd picture is [0,1,0,0]
    assert mean is not None and len(mean) == 4                      # and the mean is still written


def test_a_failure_writing_frames_never_costs_the_video_its_mean(tmp_path, tools, frames):
    use_engine(FakeEngine())
    root = _corpus(tmp_path)
    vectors = FakeImageVectors()

    def refuse():
        raise RuntimeError("lance is unhappy")

    vectors.video_frames = refuse
    with SqliteStore(tmp_path / "index.db") as store:
        stats = _pipeline(store, root, config=enabled(tmp_path),
                          image_embedder=FakeImageEmbedder(), image_vectors=vectors).run()
        record = store.get_file(str(root / "holiday.mp4"))
    assert record.status == FileStatus.INDEXED and stats.indexed == 2
    assert len(vectors.rows) == 1                                   # the mean was written


# ==========================================================================
# Faces on video frames, behind the people-recognition switch
# ==========================================================================

class Detection:
    def __init__(self, embedding, bbox=(10.0, 10.0, 40.0, 40.0)):
        import numpy as np

        self.embedding = np.asarray(embedding, dtype="float32").tobytes()
        self.bbox = bbox
        self.confidence = 0.99


def _people(tmp_path, *, on, monkeypatch, detector=None):
    """A video run with a fake face detector; returns (faces, scan rows, detector calls)."""
    from app.extract import face_detect

    calls: list[str] = []

    def fake(path):
        calls.append(Path(path).name)
        return detector(len(calls)) if detector else []

    monkeypatch.setattr(face_detect, "available", lambda: True)
    monkeypatch.setattr(face_detect, "detect_faces", fake)
    use_engine(FakeEngine())
    root = _corpus(tmp_path)
    with SqliteStore(tmp_path / "index.db") as store:
        _pipeline(store, root, config=enabled(tmp_path), people_recognition_enabled=on,
                  image_embedder=FakeImageEmbedder(), image_vectors=FakeImageVectors()).run()
        video_id = store.get_file(str(root / "holiday.mp4")).id
        faces = store.faces_for_file(video_id)
        scanned = store.conn.execute(
            "SELECT COUNT(*) FROM face_scans WHERE file_id = ?", (video_id,)).fetchone()[0]
    return faces, scanned, calls


def test_off_by_default_no_face_code_touches_a_video(tmp_path, tools, frames, monkeypatch):
    faces, scanned, calls = _people(tmp_path, on=False, monkeypatch=monkeypatch,
                                    detector=lambda n: [Detection([1, 0, 0, 0])])
    assert calls == [] and faces == [] and scanned == 0


def test_switched_on_each_picture_is_scanned_and_a_person_seen_twice_is_one_face(
        tmp_path, tools, frames, monkeypatch):
    """Three pictures: person A, person A again (a little different), and person B."""
    seen = {1: [1, 0, 0, 0], 2: [0.98, 0.15, 0, 0], 3: [0, 1, 0, 0]}
    faces, scanned, calls = _people(
        tmp_path, on=True, monkeypatch=monkeypatch,
        detector=lambda n: [Detection(_unit(*seen[n]))])
    assert calls == ["kf_00001.jpg", "kf_00002.jpg", "kf_00003.jpg"]
    assert len(faces) == 2
    assert scanned == 1                          # "looked" is recorded, so nobody re-asks


def test_a_picture_the_detector_chokes_on_costs_only_that_picture(tmp_path, tools, frames, monkeypatch):
    def detector(n):
        if n == 1:
            raise RuntimeError("bad frame")
        return [Detection(_unit(0, 0, 1, 0))]

    faces, scanned, calls = _people(tmp_path, on=True, monkeypatch=monkeypatch, detector=detector)
    assert len(calls) == 3 and len(faces) == 1 and scanned == 1


def test_a_film_cannot_write_an_unbounded_number_of_faces():
    from app.index import video_frames

    class Store:
        scanned = False

        def __init__(self):
            self.rows = []

        def add_face(self, file_id, bbox, embedding):
            self.rows.append(embedding)

        def mark_face_scanned(self, file_id):
            self.scanned = True

    store = Store()
    # 40 mutually different "people": orthogonal unit vectors.
    crowd = [Detection([1.0 if i == j else 0.0 for j in range(40)]) for i in range(40)]
    written = video_frames.detect_faces(store, 1, ["a.jpg"], detector=lambda p: crowd)
    assert written == video_frames.MAX_FACES_PER_VIDEO == len(store.rows) and store.scanned


def test_a_stopped_scan_is_not_marked_as_finished():
    from app.index import video_frames

    class Store:
        marked = False

        def add_face(self, *args):
            pass

        def mark_face_scanned(self, file_id):
            self.marked = True

    store = Store()
    video_frames.detect_faces(store, 1, ["a.jpg", "b.jpg"], detector=lambda p: [],
                              should_stop=lambda: True)
    assert store.marked is False


def test_a_re_read_film_forgets_the_faces_of_its_old_pictures(tmp_path):
    with SqliteStore(tmp_path / "i.db") as store:
        file_id = store.upsert_file("D:/v/a.mp4", size_bytes=1, mtime_ns=1)
        store.add_face(file_id, (0.0, 0.0, 1.0, 1.0), b"\x00" * 16)
        store.mark_face_scanned(file_id)
        assert store.clear_faces_for_file(file_id) == 1
        assert store.faces_for_file(file_id) == []
        assert store.conn.execute("SELECT COUNT(*) FROM face_scans").fetchone()[0] == 0


def test_media_find_says_which_film_and_which_minute(tmp_path):
    """`python -m app.cli media --find`: the headless proof of the per-picture lane."""
    pytest.importorskip("lancedb")
    from app.cli.media import moments_for
    from app.storage.vector_store import ImageVectorStore

    class Text:
        def embed(self, texts):
            return [_unit(0, 1, 0, 0) for _ in texts]

    with ImageVectorStore(tmp_path / "vec", dim=4) as images, \
            SqliteStore(tmp_path / "i.db") as store:
        cake = store.upsert_file("D:/v/party.mp4", size_bytes=1, mtime_ns=1)
        walk = store.upsert_file("D:/v/walk.mp4", size_bytes=1, mtime_ns=1)
        frames_table = images.video_frames()
        frames_table.replace_frames(cake, [(0.0, _unit(1, 0, 0, 0)), (761.0, _unit(0, 1, 0, 0))])
        frames_table.replace_frames(walk, [(65.0, _unit(0, 0.7, 0.7, 0))])
        found = moments_for("a birthday cake", Text(), frames_table, store, limit=5)
    assert (found[0]["path"], found[0]["at"]) == ("D:/v/party.mp4", "12:41")
    assert [f["path"] for f in found][1] == "D:/v/walk.mp4"
    assert found[0]["distance"] <= found[1]["distance"]


def test_the_find_option_is_registered():
    from app.cli import build_parser

    args = build_parser().parse_args(["media", "--find", "a cake", "--limit", "3"])
    assert args.find == "a cake" and args.limit == 3
