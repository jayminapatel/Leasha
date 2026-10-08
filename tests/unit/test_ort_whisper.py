r"""`app/ort/whisper.py` - Whisper speech to text on ONNX Runtime - and its seam in
`app/extract/transcribe.py`.

Layer: L2. Most of this needs no model: the log-mel is held to faster-whisper's
own feature extractor (imported here only as a reference), the timestamp rules
and the segment parser are pure functions, and the engine runs end to end on a
fake encoder and a fake merged decoder (the naming `test_ort_generate.py` uses)
that speak a scripted transcript in a twenty-two-timestamp toy vocabulary.

The last test is the real thing and skips cleanly unless a Whisper export is in
the model cache (`LEASHA_TEST_MODEL_CACHE`, default `D:\Leasha\Data\models`).
Its speech is `LEASHA_TEST_SPEECH_WAV` when set, otherwise a clip spoken by
Windows' own speech synthesiser; without either it skips too.
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pytest

from app.core.errors import AppErrorException
from app.extract import transcribe
from app.ort import whisper
from app.ort.whisper import (N_FRAMES, SAMPLE_RATE, OnnxWhisperEngine, WhisperTokens,
                             begin_suppress, is_silent, leading_quiet_frames, log_mel,
                             parse_window, timestamp_rules, window_features)

# -- a toy vocabulary with Whisper's layout -----------------------------------
# 0-9 words, 10 end, 11 start, 12-13 languages, 14 translate, 15 transcribe,
# 16 startofprev, 17 notimestamps, 18-39 timestamps <|0.00|> .. <|0.42|>.
WORDS = ["hello", "world", "again", "cake", "tree", "oak", "under", "the", "is", "treasure"]
EOT, SOT, EN, FR, TRANSLATE, TRANSCRIBE, PREV, NOTS, TS = 10, 11, 12, 13, 14, 15, 16, 17, 18
VOCAB = 40


def ts(index: int) -> int:
    return TS + index


def toy_tokens(**overrides) -> WhisperTokens:
    values = dict(sot=SOT, eot=EOT, transcribe=TRANSCRIBE, no_timestamps=NOTS,
                  timestamp_begin=TS, languages={EN: "en", FR: "fr"},
                  suppress=(TRANSLATE, PREV), begin_suppress=(EOT,),
                  max_initial_timestamp_index=None)
    values.update(overrides)
    return WhisperTokens(**values)


def logits_with(**peaks) -> np.ndarray:
    logits = np.zeros(VOCAB, np.float32)
    for index, value in peaks.items():
        logits[int(index.lstrip("t"))] = value
    return logits


def allowed(logits: np.ndarray) -> set[int]:
    return {int(i) for i in np.flatnonzero(np.isfinite(logits))}


# ==========================================================================
# Features
# ==========================================================================

@pytest.mark.parametrize("n_mels", [80, 128])
def test_the_log_mel_matches_faster_whispers_feature_extractor(n_mels):
    reference = pytest.importorskip("faster_whisper.feature_extractor")
    rng = np.random.default_rng(0)
    t = np.arange(SAMPLE_RATE * 12) / SAMPLE_RATE
    signal = (0.3 * np.sin(2 * np.pi * 440 * t)
              + 0.2 * np.sin(2 * np.pi * 1234.5 * t) * np.sin(2 * np.pi * 0.7 * t)
              + 0.05 * rng.standard_normal(t.size)).astype(np.float32)
    extractor = reference.FeatureExtractor(feature_size=n_mels)
    expected = extractor(signal, padding=160)
    got = log_mel(signal, n_mels=n_mels, padding=160)
    assert got.shape == expected.shape
    # Measured 2026-09-29: 0.0 at both sizes. The margin is for another numpy.
    assert float(np.abs(got - expected).max()) < 1e-5
    assert float(np.abs(whisper.mel_filters(n_mels=n_mels) - extractor.mel_filters).max()) < 1e-6


def test_any_recording_is_decoded_to_mono_16_khz_int16(tmp_path):
    pytest.importorskip("av")
    import wave

    rate, seconds = 8000, 2
    left = (10000 * np.sin(np.arange(rate * seconds) * 2 * np.pi * 440 / rate)).astype(np.int16)
    target = tmp_path / "stereo_8k.wav"
    with wave.open(str(target), "wb") as out:
        out.setnchannels(2)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(np.stack([left, left], axis=1).tobytes())
    samples = whisper.decode_audio(str(target))
    assert samples.dtype == np.int16
    assert abs(samples.shape[0] - SAMPLE_RATE * seconds) <= 64
    assert 5000 < int(np.abs(samples).max()) < 12000


def test_a_window_is_always_three_thousand_frames():
    short = np.full(SAMPLE_RATE * 3, 0.1, np.float32)
    long = np.full(SAMPLE_RATE * 40, 0.1, np.float32)
    assert window_features(short).shape == (80, N_FRAMES)
    assert window_features(long, n_mels=128).shape == (128, N_FRAMES)


def test_silence_and_leading_quiet_are_measured_in_100_ms_blocks():
    quiet = np.zeros(SAMPLE_RATE * 2, np.float32)
    loud = (0.2 * np.sin(np.arange(SAMPLE_RATE) * 0.3)).astype(np.float32)
    assert is_silent(quiet) and is_silent(np.zeros(0, np.float32))
    assert not is_silent(np.concatenate([quiet, loud]))
    assert leading_quiet_frames(np.concatenate([quiet, loud])) == 200   # 2 s = 200 frames
    assert leading_quiet_frames(loud) == 0
    assert leading_quiet_frames(quiet) == 200


# ==========================================================================
# Tokens and rules
# ==========================================================================

def test_token_ids_come_from_the_models_files():
    generation = {"decoder_start_token_id": 50258, "eos_token_id": 50257,
                  "task_to_id": {"transcribe": 50359, "translate": 50358},
                  "no_timestamps_token_id": 50363, "lang_to_id": {"<|en|>": 50259, "<|de|>": 50261},
                  "suppress_tokens": [1, 2], "begin_suppress_tokens": [220, 50257],
                  "max_initial_timestamp_index": 50}
    names = {"<|0.00|>": 50364}
    tokens = WhisperTokens.from_configs(generation, {}, names.get)
    assert (tokens.sot, tokens.eot, tokens.transcribe, tokens.no_timestamps,
            tokens.timestamp_begin) == (50258, 50257, 50359, 50363, 50364)
    assert tokens.languages == {50259: "en", 50261: "de"}
    assert tokens.suppress == (1, 2) and tokens.begin_suppress == (220, 50257)
    assert tokens.max_initial_timestamp_index == 50
    # A tokenizer without timestamp tokens: OpenAI's numbering, after <|notimestamps|>.
    assert WhisperTokens.from_configs(generation, {}, lambda _n: None).timestamp_begin == 50364


def test_a_model_that_does_not_name_its_tokens_is_refused():
    with pytest.raises(ValueError, match="transcribe"):
        WhisperTokens.from_configs({"decoder_start_token_id": 1, "eos_token_id": 2,
                                    "no_timestamps_token_id": 3}, {}, lambda _n: None)


def test_the_first_token_is_a_timestamp_within_the_initial_limit():
    rule = timestamp_rules(toy_tokens(max_initial_timestamp_index=5))
    logits = logits_with(t3=9.0)
    rule([], logits)
    assert allowed(logits) == {ts(i) for i in range(6)}


def test_no_timestamps_is_never_chosen():
    logits = logits_with(**{f"t{NOTS}": 50.0})
    timestamp_rules(toy_tokens())([ts(0), 1], logits)
    assert NOTS not in allowed(logits)


def test_timestamps_come_in_pairs_and_never_go_back():
    rule = timestamp_rules(toy_tokens())

    after_open = logits_with()
    rule([ts(0)], after_open)                           # just opened: text next
    assert allowed(after_open) <= set(range(TS)) and not any(t >= TS for t in allowed(after_open))

    after_text = logits_with(t2=6.0)                    # a clear word, so timestamps do not win
    rule([ts(5), 1], after_text)                        # text: more text, or a later close
    assert ts(5) not in allowed(after_text) and ts(6) in allowed(after_text)
    assert 2 in allowed(after_text)

    after_close = logits_with(t10=6.0)                  # the end likely, so timestamps do not win
    rule([ts(5), 1, ts(8)], after_close)                # closed: a timestamp or the end
    assert EOT in allowed(after_close) and ts(8) in allowed(after_close)
    assert not any(t < EOT for t in allowed(after_close)) and ts(7) not in allowed(after_close)

    after_pair = logits_with()
    rule([ts(5), 1, ts(8), ts(8)], after_pair)          # a new segment opened: text
    assert not any(t >= TS for t in allowed(after_pair))


def test_timestamps_together_likelier_than_any_word_force_a_timestamp():
    rule = timestamp_rules(toy_tokens())
    logits = np.zeros(VOCAB, np.float32)
    logits[1] = 2.5                                     # the best single word
    logits[TS:] = 1.0                                   # 22 timestamps, e^1 each
    rule([ts(0), 1], logits)
    assert not any(t < TS for t in allowed(logits))

    logits = np.zeros(VOCAB, np.float32)
    logits[1] = 6.0                                     # now the word wins
    rule([ts(0), 1], logits)
    assert 1 in allowed(logits)


def test_the_first_token_may_not_be_blank():
    logits = logits_with()
    begin_suppress([EOT])([], logits)
    assert EOT not in allowed(logits)
    logits = logits_with()
    begin_suppress([EOT])([ts(0)], logits)
    assert EOT in allowed(logits)


# ==========================================================================
# Segments and seeking
# ==========================================================================

def test_pairs_split_segments_and_a_single_final_timestamp_reads_the_whole_window():
    segments, consumed = parse_window([ts(0), 0, 1, ts(10), ts(10), 2, ts(20)],
                                      timestamp_begin=TS, window_frames=3000)
    assert segments == [(0.0, 0.2, [0, 1]), (0.2, 0.4, [2])]
    assert consumed == 3000


def test_an_unfinished_segment_is_left_for_the_next_window():
    segments, consumed = parse_window([ts(0), 0, ts(10), ts(10), 1],
                                      timestamp_begin=TS, window_frames=3000)
    assert segments == [(0.0, 0.2, [0])]
    assert consumed == 20                               # 0.2 s = timestamp 10 x 2 frames


def test_text_without_a_pair_is_one_segment():
    segments, consumed = parse_window([ts(0), 0, 1, ts(7)], timestamp_begin=TS,
                                      window_frames=1500)
    assert segments == [(0.0, pytest.approx(0.14), [0, 1])] and consumed == 1500
    segments, consumed = parse_window([ts(0), 0, 1], timestamp_begin=TS, window_frames=1500)
    assert segments == [(0.0, 15.0, [0, 1])] and consumed == 1500
    segments, _ = parse_window([], timestamp_begin=TS, window_frames=3000)
    assert segments == [(0.0, 30.0, [])]


# ==========================================================================
# The engine, end to end, on fake sessions
# ==========================================================================

@dataclass
class Arg:
    name: str
    type: str = "tensor(float)"
    shape: tuple = ()


class FakeEncoder:
    """Returns a hidden state whose every value is the window's number."""

    def __init__(self) -> None:
        self.calls: list[np.ndarray] = []

    def get_inputs(self):
        return [Arg("input_features", shape=("batch", 80, 3000))]

    def get_outputs(self):
        return [Arg("last_hidden_state")]

    def run(self, names, feeds):
        features = feeds["input_features"]
        self.calls.append(features)
        return [np.full((1, 4, 32), float(len(self.calls) - 1), np.float32)]


class FakeDecoder:
    """A merged decoder that says `scripts[window][position]`, where the
    window is read from the encoder's hidden state."""

    def __init__(self, scripts: list[list[int]]) -> None:
        self.scripts = scripts
        self.cache = [f"{layer}.{part}.{kv}" for layer in range(2)
                      for part in ("decoder", "encoder") for kv in ("key", "value")]
        self.fed: list[list[int]] = []

    def get_inputs(self):
        args = [Arg("input_ids", "tensor(int64)"), Arg("encoder_hidden_states")]
        args += [Arg(f"past_key_values.{c}", shape=("batch", 4, "past", 8)) for c in self.cache]
        return args + [Arg("use_cache_branch", "tensor(bool)")]

    def get_outputs(self):
        return [Arg("logits")] + [Arg(f"present.{c}") for c in self.cache]

    def run(self, names, feeds):
        ids = feeds["input_ids"]
        self.fed.append(ids[0].tolist())
        window = int(feeds["encoder_hidden_states"][0, 0, 0])
        seen = feeds[f"past_key_values.{self.cache[0]}"].shape[2]
        position = seen + ids.shape[1] - 1
        script = self.scripts[min(window, len(self.scripts) - 1)]
        logits = np.zeros((1, ids.shape[1], VOCAB), np.float32)
        logits[0, -1, script[min(position, len(script) - 1)]] = 8.0
        out = [logits]
        for c in self.cache:
            past = feeds[f"past_key_values.{c}"]
            out.append(np.concatenate([past, np.zeros((1, 4, ids.shape[1], 8), np.float32)], 2))
        return out


class FakeTokenizer:
    def decode(self, ids, skip_special_tokens=True):
        return " ".join(WORDS[i] for i in ids if i < len(WORDS))


def loud(seconds: float) -> np.ndarray:
    n = int(seconds * SAMPLE_RATE)
    return (8000 * np.sin(np.arange(n) * 0.05)).astype(np.int16)


def quiet(seconds: float) -> np.ndarray:
    return np.zeros(int(seconds * SAMPLE_RATE), np.int16)


#: Position 0 answers language detection; 1 is the language slot of the
#: prompt; generation starts at position 2.
WINDOW_ONE = [EN, 0, ts(0), 0, 1, ts(10), ts(10), 2, ts(20), EOT]
WINDOW_TWO = [FR, 0, ts(0), 9, 8, ts(5), EOT]


def make_engine(audio: np.ndarray, scripts=(WINDOW_ONE, WINDOW_TWO)):
    encoder, decoder = FakeEncoder(), FakeDecoder(list(scripts))
    engine = OnnxWhisperEngine(
        encoder=encoder, decoder=decoder, tokenizer=FakeTokenizer(), tokens=toy_tokens(),
        config={"decoder_attention_heads": 4, "d_model": 32, "max_source_positions": 1500,
                "max_target_positions": 448},
        audio_reader=lambda _path: audio)
    return engine, encoder, decoder


def test_a_scripted_transcript_comes_out_with_absolute_times():
    engine, encoder, decoder = make_engine(loud(45))
    segments = list(engine.transcribe("memo.wav"))
    assert [(s.start, s.end, s.text) for s in segments] == [
        (0.0, 0.2, "hello world"), (0.2, 0.4, "again"),
        (30.0, 30.1, "treasure is"),
    ]
    assert engine.language == "en"                      # from the first window only
    assert len(encoder.calls) == 2 and encoder.calls[0].shape == (1, 80, 3000)
    # language detection is one step after the start token; then the prompt
    assert decoder.fed[0] == [SOT] and decoder.fed[1] == [SOT, EN, TRANSCRIBE]


def test_a_silent_window_never_reaches_the_model():
    engine, encoder, _ = make_engine(np.concatenate([quiet(30), loud(10)]))
    segments = list(engine.transcribe("memo.wav"))
    assert len(encoder.calls) == 1
    assert segments[0].start == 30.0


def test_leading_quiet_moves_the_window_up_to_the_sound():
    engine, _, _ = make_engine(np.concatenate([quiet(5), loud(10)]))
    segments = list(engine.transcribe("memo.wav"))
    # 5 s of quiet, less the 0.2 s kept in front of the sound
    assert segments[0].start == pytest.approx(4.8)


def test_resuming_starts_where_it_is_told():
    engine, encoder, _ = make_engine(loud(45))
    segments = list(engine.transcribe("memo.wav", 30.0))
    assert len(encoder.calls) == 1
    assert segments[0].start == 30.0 and segments[0].text == "hello world"


def test_the_engine_goes_through_transcribe_and_its_journal(tmp_path):
    audio_file = tmp_path / "memo.wav"
    audio_file.write_bytes(b"not read - the fake reader supplies the samples")
    engine, _, _ = make_engine(loud(45))
    result = transcribe.transcribe(audio_file, engine=engine, journal_dir=tmp_path / "j")
    assert result.text == "hello world again treasure is"
    assert result.language == "en"


# ==========================================================================
# The seam in transcribe.py
# ==========================================================================

def fake_snapshot(root: Path, size: str, *, int8: bool = False, drop: str = "") -> Path:
    model = whisper.variants(size)[1 if int8 else 0]
    folder = root / ("models--" + model.repo.replace("/", "--")) / "snapshots" / "abc"
    for name in model.files():
        if name != drop:
            (folder / name).parent.mkdir(parents=True, exist_ok=True)
            (folder / name).write_bytes(b"x")
    return folder


def test_every_offered_size_has_an_onnx_export():
    assert set(transcribe.MODELS) == set(whisper.SIZES)


def test_a_model_is_present_only_when_every_file_is(tmp_path):
    assert not transcribe.model_present("base", tmp_path)
    fake_snapshot(tmp_path, "base", drop="onnx/decoder_model_merged.onnx")
    assert not transcribe.model_present("base", tmp_path)
    fake_snapshot(tmp_path, "tiny", int8=True)
    assert transcribe.model_present("tiny", tmp_path)
    # large-v3-turbo's encoder keeps its weights beside it
    fake_snapshot(tmp_path, "large-v3-turbo", drop="onnx/encoder_model.onnx_data")
    assert not transcribe.model_present("large-v3-turbo", tmp_path)
    assert not transcribe.model_present("base", None)


def test_the_whisper_folder_also_finds_the_model_cache_above_it(tmp_path):
    fake_snapshot(tmp_path, "small")
    assert transcribe.model_present("small", tmp_path / "whisper")
    assert not transcribe.model_present("small", tmp_path / "elsewhere")


def test_full_precision_is_preferred_to_int8(tmp_path):
    fake_snapshot(tmp_path, "base", int8=True)
    fake_snapshot(tmp_path, "base")
    model, _folder = whisper.resolve_size("base", [tmp_path])
    assert model.suffix == ""


def test_a_missing_onnx_model_is_named_and_the_fix_is_the_download_button(tmp_path, monkeypatch):
    transcribe.set_engine_factory(None)
    monkeypatch.setattr(transcribe, "available", lambda: True)
    try:
        with pytest.raises(AppErrorException) as caught:
            transcribe.load_engine("small", tmp_path / "whisper", path="memo.wav")
    finally:
        transcribe.reset_engine()
    error = caught.value.error
    assert error.code == "ERR_TRANSCRIBE_MODEL_MISSING"
    assert "onnx-community/whisper-small" in error.action_payload
    assert "Download" in error.action_payload


def test_available_asks_for_the_onnx_engines_packages(monkeypatch):
    import importlib.util

    real = importlib.util.find_spec
    monkeypatch.setattr(importlib.util, "find_spec",
                        lambda name, *a: None if name == "av" else real(name, *a))
    assert transcribe.available() is False


def test_nothing_in_the_speech_path_imports_faster_whisper():
    root = Path(__file__).resolve().parents[2]
    for relative in ("app/ort/whisper.py", "app/extract/transcribe.py"):
        code = [line for line in (root / relative).read_text(encoding="utf-8").splitlines()
                if line.strip().startswith(("import ", "from "))]
        assert not any("faster_whisper" in line or "ctranslate2" in line for line in code), relative


# ==========================================================================
# The real model - skipped unless it is in the model cache
# ==========================================================================

MODEL_CACHE = Path(os.environ.get("LEASHA_TEST_MODEL_CACHE", "") or r"D:\Leasha\Data\models")
SPOKEN = ("The treasure is buried under the old oak tree at the bottom of the garden. "
          "Please remember to bring the homework on Tuesday morning.")


def _speech(tmp_path: Path) -> Path:
    given = os.environ.get("LEASHA_TEST_SPEECH_WAV", "")
    if given and Path(given).is_file():
        return Path(given)
    if sys.platform != "win32":
        pytest.skip("no LEASHA_TEST_SPEECH_WAV and no Windows speech synthesiser")
    target = tmp_path / "spoken.wav"
    script = ("Add-Type -AssemblyName System.Speech; "
              "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
              f"$s.SetOutputToWaveFile('{target}'); $s.Speak('{SPOKEN}'); $s.Dispose()")
    try:
        subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                       check=True, timeout=60, capture_output=True)
    except (OSError, subprocess.SubprocessError):
        pytest.skip("the Windows speech synthesiser did not produce a clip")
    if not target.is_file() or target.stat().st_size < 10_000:
        pytest.skip("the Windows speech synthesiser did not produce a clip")
    return target


@pytest.mark.skipif(whisper.resolve_size("base", [MODEL_CACHE]) is None,
                    reason="no onnx-community/whisper-base export in the model cache")
def test_the_real_base_model_transcribes_real_speech(tmp_path):
    clip = _speech(tmp_path)
    transcribe.set_engine_factory(None)
    transcribe.reset_engine()
    try:
        # `MediaConfig` hands over MODEL_CACHE\whisper; the export is in MODEL_CACHE.
        result = transcribe.transcribe(clip, model="base", model_dir=MODEL_CACHE / "whisper",
                                       journal_dir=tmp_path / "j")
    finally:
        transcribe.reset_engine()
    text = result.text.lower()
    assert result.language == "en"
    if "LEASHA_TEST_SPEECH_WAV" not in os.environ:
        for word in ("treasure", "oak tree", "garden", "homework", "tuesday"):
            assert word in text, (word, result.text)
    starts = [s.start for s in result.segments]
    assert starts == sorted(starts) and all(s.end >= s.start for s in result.segments)


# --- one recording at a time per engine (review 2026-10-08) ------------------------------

def test_the_engine_is_locked_for_the_whole_of_one_transcription():
    """`self.language` and the decoder's KV cache are rewritten by every window,
    and the media tail's sub-pipeline can run more than one worker, so two
    recordings could reach one engine together and interleave their caches
    into garbage text. The lock is held from the first `next()` until the
    generator is exhausted."""
    engine, _, _ = make_engine(loud(45))
    assert not engine.lock.locked()

    segments = engine.transcribe("memo.wav")
    assert not engine.lock.locked(), "a generator that has not started holds nothing"
    first = next(segments)
    assert engine.lock.locked(), "held while windows are being decoded"
    rest = list(segments)
    assert not engine.lock.locked(), "released once the recording is done"
    assert first.text == "hello world" and len(rest) == 2


def test_a_second_recording_waits_for_the_first():
    import threading

    engine, _, _ = make_engine(loud(45))
    first = engine.transcribe("one.wav")
    next(first)                                  # holds the lock mid-file

    started = threading.Event()
    finished = threading.Event()

    def second():
        started.set()
        list(engine.transcribe("two.wav"))
        finished.set()

    thread = threading.Thread(target=second, daemon=True)
    thread.start()
    assert started.wait(2.0)
    assert not finished.wait(0.3), "the second caller must block while the first is mid-file"
    first.close()                                # abandoning the first releases the lock
    assert finished.wait(5.0), "and then the second one runs"
    thread.join(5.0)
