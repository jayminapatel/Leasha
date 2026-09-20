r"""The real speech model on a real recording. Skipped unless both are present.

Work order 202626270515. Layer: L2.

`test_media.py` drives the transcriber through a fake engine because a model is
148 MB and a machine without one must still run the suite. This is the other half:
what the fake cannot show - that `FasterWhisperEngine` is written against the real
package correctly, that it loads offline, that the timestamps it yields are
absolute, and that resuming from a journal with `clip_timestamps` carries on rather
than starting over.

Two environment variables say where the real things are, and the file skips
cleanly (not fails) without either:

    LEASHA_TEST_WHISPER_DIR   a folder holding `models--Systran--faster-whisper-base`
                              (what `download_model('base', cache_dir=...)` makes)
    LEASHA_TEST_SPEECH_WAV    a wav of clear English speech, at least 30 seconds

Measured with them on 2026-09-20: a 45 s slice transcribed in 22 s on a busy
machine, the model load included.
"""

from __future__ import annotations

import os
import wave
from pathlib import Path

import pytest

from app.core.errors import AppErrorException
from app.extract import transcribe

MODEL_DIR = Path(os.environ.get("LEASHA_TEST_WHISPER_DIR", "") or ".")
SPEECH = Path(os.environ.get("LEASHA_TEST_SPEECH_WAV", "") or ".")

pytestmark = [
    pytest.mark.skipif(not transcribe.available(), reason="faster-whisper is not installed"),
    pytest.mark.skipif(not transcribe.model_present("base", MODEL_DIR),
                       reason="LEASHA_TEST_WHISPER_DIR does not hold the base model"),
    pytest.mark.skipif(not SPEECH.is_file(), reason="LEASHA_TEST_SPEECH_WAV is not set"),
]


@pytest.fixture(autouse=True)
def _real_engine():
    transcribe.set_engine_factory(None)
    transcribe.reset_engine()
    yield
    transcribe.reset_engine()


@pytest.fixture()
def clip(tmp_path) -> Path:
    """The first 45 seconds of the recording: enough passages to interrupt between."""
    target = tmp_path / "clip.wav"
    with wave.open(str(SPEECH), "rb") as source:
        frames = source.readframes(source.getframerate() * 45)
        with wave.open(str(target), "wb") as out:
            out.setparams(source.getparams())
            out.writeframes(frames)
    return target


def test_the_real_engine_hears_words_with_absolute_timestamps(clip, tmp_path):
    result = transcribe.transcribe(
        clip, model="base", model_dir=MODEL_DIR, journal_dir=tmp_path / "j")
    assert result.language == "en" and len(result.segments) >= 4
    starts = [s.start for s in result.segments]
    assert starts == sorted(starts) and 0.0 <= starts[0] < 5.0 and starts[-1] > 20.0
    assert all(s.end > s.start for s in result.segments)
    spoken = result.text.lower()
    assert "walkthrough" in spoken.replace(" ", "") or "walk through" in spoken
    assert "fire exit" in spoken or "invoice" in spoken


def test_an_interrupted_real_transcription_resumes_and_finishes(clip, tmp_path):
    journal = tmp_path / "j"
    seen: list[float] = []

    def stop_after_two() -> bool:
        seen.append(1.0)
        return len(seen) >= 2

    with pytest.raises(AppErrorException) as caught:
        transcribe.transcribe(clip, model="base", model_dir=MODEL_DIR,
                              journal_dir=journal, pacer=stop_after_two)
    assert caught.value.error.code == "ERR_MEDIA_INTERRUPTED"
    kept, _lang, done = transcribe.TranscriptJournal(journal, clip, "base").load()
    assert len(kept) == 2 and not done

    result = transcribe.transcribe(clip, model="base", model_dir=MODEL_DIR, journal_dir=journal)
    assert result.resumed_from_s == pytest.approx(kept[-1].end, abs=0.01)
    assert result.resumed_from_s > 5.0                       # it did not start over
    assert [s.text for s in result.segments[:2]] == [s.text for s in kept]
    later = result.segments[2:]
    assert later and all(s.start >= result.resumed_from_s - 1.0 for s in later)
    assert result.segments[-1].start > 30.0                  # and reached the end


def test_a_finished_real_transcript_is_not_run_twice(clip, tmp_path):
    first = transcribe.transcribe(clip, model="base", model_dir=MODEL_DIR, journal_dir=tmp_path / "j")
    second = transcribe.transcribe(clip, model="base", model_dir=MODEL_DIR, journal_dir=tmp_path / "j")
    assert second.from_cache and second.elapsed_s == 0.0
    assert [s.text for s in second.segments] == [s.text for s in first.segments]


def test_the_real_engine_refuses_to_download_a_missing_model(clip, tmp_path):
    """Leasha is offline: a model that is not there is a clear error, never a fetch."""
    empty = tmp_path / "no-models"
    empty.mkdir()
    with pytest.raises(AppErrorException) as caught:
        transcribe.transcribe(clip, model="base", model_dir=empty, journal_dir=tmp_path / "j")
    assert caught.value.error.code == "ERR_TRANSCRIBE_MODEL_MISSING"
