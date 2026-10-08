r"""Whisper speech to text on plain ONNX Runtime: audio in, timestamped segments out.

Layer: L2 (over `app.ort.session`, `app.ort.generate`, `app.ort.hub`).

**Why** (owner, 2026-09-29: "all should be onnx by default"): faster-whisper
runs on CTranslate2, an unsigned native library that Windows Smart App Control
can block. This file does the same job with the `onnxruntime` Leasha already
loads everywhere, on the `onnx-community` exports of OpenAI's Whisper (MIT).
`app/extract/transcribe.py` is the only caller; it knows nothing of what is in
here beyond `OnnxWhisperEngine` and `resolve_size`.

What each part is, and what it copies:

* **Audio** - any file PyAV reads, decoded to mono 16 kHz. Kept as 16-bit
  samples (half the memory of floats; a two-hour recording is 230 MB, not
  460) and turned into floats one 30 s window at a time. Same decode as
  faster-whisper's `decode_audio` (MIT), which is where the resampler settings
  come from.
* **Features** - Whisper's log-mel spectrogram in numpy: periodic Hann window,
  `n_fft` 400, hop 160, Slaney mel filters (librosa's formula, `htk=False,
  norm="slaney"`), log10 clamped at 1e-10, floored at max - 8, then
  `(x + 4) / 4`. `tests/unit/test_ort_whisper.py` holds it to faster-whisper's
  own `FeatureExtractor` on a synthetic signal. **One difference, on
  purpose:** faster-whisper and OpenAI take the "max - 8" floor over the whole
  file; here it is taken per 30 s window, because the whole file's
  spectrogram is never held. It only changes bins more than 80 dB below the
  window's loudest, and each window is what Whisper was trained on anyway.
  The last window is padded with silent *audio* (as OpenAI and Hugging Face
  do), not with zeros in the spectrogram (as faster-whisper does).
* **Decoding** - per window, `<|startoftranscript|><|lang|><|transcribe|>`
  then greedy generation **with** timestamps, through `generate.py`'s loop.
  The rules are OpenAI whisper's `SuppressBlank`, `SuppressTokens` and
  `ApplyTimestampRules` (`whisper/decoding.py`), in that order. No beam, no
  temperature fallback and no conditioning on the previous window's text -
  beam 1 and `condition_on_previous_text=False` are what the faster-whisper
  engine ran with; the fallback it also had (re-decode hotter when the text
  compresses too well) is not here, which is the one accuracy trade in this
  file and the first thing to add if the real recordings show loops.
* **Language** - detected once per file, from the first window with sound
  in it: one decoder step after `<|startoftranscript|>`, argmax over the
  language tokens.
* **Segments and seeking** - OpenAI's `transcribe()` logic: consecutive
  timestamp pairs close segments; the next window starts at the last
  complete timestamp (or a full window on when the text ended on a single
  timestamp). Times are absolute: window start + timestamp.
* **Silence** - a window with no 100 ms block louder than -50 dBFS is skipped
  without running the model, and a window that opens on a second or more of
  quiet is moved up to where the sound starts (Whisper's first timestamp in
  a window may be no later than 1 s, so leading quiet shifts every time).
  The faster-whisper engine used Silero VAD, which also cut silence *inside*
  a window and could tell speech from music; this check only saves the
  model's time on stretches of true quiet (the start and end of a recorded
  call, a paused memo). Whisper still sees music and noise, and may write
  words for them.

Every token id (start, end, task, timestamps, languages, what to suppress)
comes from the model's own `generation_config.json`, `config.json` and
`tokenizer.json`. Where one can be missing, the fallback says where its value
comes from.

**Measured 2026-09-29:** on a 12 s synthetic signal the log-mel is identical
to faster-whisper 1.2.1's `FeatureExtractor` (largest difference 0.0, at 80
and at 128 mel bins; the test allows 1e-5). On the owner's laptop, with
whisper-base and a clip spoken by Windows' speech synthesiser (13.4 s, three
sentences, repeated to make 80 s with 40 s of silence in the middle), full
precision and int8 both gave every word right, language "en", and the
third copy stamped at 66.7 s against a true 66.8 s. Two runs each, model load
not included: full precision with the encoder on DirectML 4.1-4.2 s, all on
the processor 5.9-6.2 s; int8 (processor) 7.0-9.5 s - so int8 is smaller,
not faster. Another test run was using the machine at the time. **Not
measured:** real recordings (accents, noise, music, hours), and every size
but base. The real-model test in `tests/unit/test_ort_whisper.py` skips
cleanly where no export is downloaded.
"""

from __future__ import annotations

import dataclasses
import json
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Optional, Sequence

import numpy as np

from app.core.logging import logger
from app.ort import hub
from app.ort.generate import Decoder, LogitsRule, generate, greedy, suppress

__all__ = [
    "SAMPLE_RATE", "N_FFT", "HOP_LENGTH", "CHUNK_S", "N_SAMPLES", "N_FRAMES",
    "SIZES", "variants", "resolve_size",
    "decode_audio", "mel_filters", "log_mel", "window_features", "is_silent",
    "leading_quiet_frames",
    "WhisperTokens", "begin_suppress", "timestamp_rules", "parse_window",
    "OnnxWhisperEngine",
]

_log = logger.bind(component="ort.whisper")

# -- Whisper's fixed audio front end (`preprocessor_config.json` repeats them) --
SAMPLE_RATE = 16_000
N_FFT = 400
HOP_LENGTH = 160
CHUNK_S = 30
N_SAMPLES = CHUNK_S * SAMPLE_RATE          # 480 000 samples in a window
N_FRAMES = N_SAMPLES // HOP_LENGTH         # 3 000 mel frames in a window

#: A window is silence when no 100 ms block in it is louder than this (RMS of
#: float samples in [-1, 1]; 0.00316 is -50 dBFS). Chosen, not measured: a
#: quiet voice memo sits well above it, a room with nobody talking below.
SILENCE_RMS = 10 ** (-50 / 20)
_SILENCE_BLOCK = SAMPLE_RATE // 10

#: A tail shorter than this (in mel frames, 10 ms each) is not decoded: a
#: fifth of a second holds no word, and Whisper writes words for it anyway.
MIN_TAIL_FRAMES = 20

#: A window that opens on at least this much quiet (mel frames) is moved up
#: to where the sound starts, less `LEAD_KEEP_FRAMES`. Whisper's first
#: timestamp may be no later than 1 s into a window (`max_initial_timestamp`),
#: so speech 7 s into a window would otherwise be stamped as starting at 1 s.
#: Measured on 2026-09-29 with whisper-base int8: speech at 66.8 s after 40 s
#: of silence was stamped 60.0 s without this.
LEAD_TRIM_FRAMES = 100
LEAD_KEEP_FRAMES = 20


# ---------------------------------------------------------------------------
# The models: which repository, which files
# ---------------------------------------------------------------------------

_SIDE = ("config.json", "generation_config.json", "tokenizer.json")


def _whisper(size: str, repo: str, mb: int, *, extra: tuple[str, ...] = ()) -> hub.OnnxModel:
    return hub.OnnxModel(
        key=f"whisper-{size}", repo=repo, label=f"Whisper {size} (speech to text)",
        graphs=("encoder_model", "decoder_model_merged"), suffix="",
        required=_SIDE, approx_mb=mb, licence="MIT", extra=extra)


#: One entry per size in `transcribe.MODELS`. Repositories and sizes read from
#: the Hugging Face API listing (`/api/models/<repo>/tree/main`) on 2026-09-29;
#: medium and large-v3 are published as `...-ONNX`. Full precision (MB is the
#: two graphs, plus external weights where the export has them).
#: `hub.WHISPER_BASE` is the same repository as `base` here; it is kept as it
#: is for whatever else refers to it.
SIZES: dict[str, hub.OnnxModel] = {
    "tiny": _whisper("tiny", "onnx-community/whisper-tiny", 152),
    "base": _whisper("base", "onnx-community/whisper-base", 291),
    "small": _whisper("small", "onnx-community/whisper-small", 968),
    "medium": _whisper("medium", "onnx-community/whisper-medium-ONNX", 3057),
    "large-v3-turbo": _whisper("large-v3-turbo", "onnx-community/whisper-large-v3-turbo",
                               3236, extra=("onnx/encoder_model.onnx_data",)),
    "large-v3": _whisper("large-v3", "onnx-community/whisper-large-v3-ONNX", 6177,
                         extra=("onnx/encoder_model.onnx_data",
                                "onnx/decoder_model_merged.onnx_data")),
}

#: The int8 graphs' sizes in MB (same listing). int8 is used only when it is
#: what is on disk: `load_session` runs any `_int8` graph on the processor.
_INT8_MB = {"tiny": 41, "base": 77, "small": 249, "medium": 986,
            "large-v3-turbo": 1085, "large-v3": 1822}


def variants(size: str) -> tuple[hub.OnnxModel, ...]:
    """Full precision first, then int8. Empty for a size with no export."""
    full = SIZES.get(size)
    if full is None:
        return ()
    int8 = dataclasses.replace(full, key=full.key + "-int8", suffix="_int8", extra=(),
                               approx_mb=_INT8_MB.get(size, 0))
    return (full, int8)


def resolve_size(size: str, cache_dirs: Iterable[Optional[Path]]
                 ) -> Optional[tuple[hub.OnnxModel, Path]]:
    """The first complete copy of `size` in `cache_dirs`, full precision
    preferred. Offline (`hub.resolve`); never raises."""
    for folder in cache_dirs:
        if not folder:
            continue
        for model in variants(size):
            found = hub.resolve(model, Path(folder))
            if found is not None:
                return model, found
    return None


# ---------------------------------------------------------------------------
# Audio and features
# ---------------------------------------------------------------------------

def decode_audio(path: str, sampling_rate: int = SAMPLE_RATE) -> np.ndarray:
    """Every sample, mono, as int16: `app.extract.media_tools.decode_audio`,
    because that is the one module allowed to import PyAV."""
    from app.extract.media_tools import decode_audio as decode

    return decode(path, sampling_rate)


def _hz_to_mel(freq: np.ndarray) -> np.ndarray:
    """librosa's Slaney mel scale: linear below 1 kHz, logarithmic above."""
    freq = np.asarray(freq, dtype=np.float64)
    f_sp = 200.0 / 3
    mels = freq / f_sp
    min_log_hz, min_log_mel, logstep = 1000.0, 1000.0 / f_sp, np.log(6.4) / 27.0
    return np.where(freq >= min_log_hz,
                    min_log_mel + np.log(np.maximum(freq, 1e-10) / min_log_hz) / logstep, mels)


def _mel_to_hz(mels: np.ndarray) -> np.ndarray:
    mels = np.asarray(mels, dtype=np.float64)
    f_sp = 200.0 / 3
    freqs = f_sp * mels
    min_log_hz, min_log_mel, logstep = 1000.0, 1000.0 / f_sp, np.log(6.4) / 27.0
    return np.where(mels >= min_log_mel,
                    min_log_hz * np.exp(logstep * (mels - min_log_mel)), freqs)


def mel_filters(sr: int = SAMPLE_RATE, n_fft: int = N_FFT, n_mels: int = 80) -> np.ndarray:
    """`[n_mels, n_fft // 2 + 1]` triangular filters, as `librosa.filters.mel(sr,
    n_fft, n_mels)` computes them (Slaney scale and area normalisation)."""
    fft_freqs = np.fft.rfftfreq(n=n_fft, d=1.0 / sr)
    mel_points = np.linspace(_hz_to_mel(0.0), _hz_to_mel(sr / 2.0), n_mels + 2)
    hz_points = _mel_to_hz(mel_points)
    widths = np.diff(hz_points)
    ramps = hz_points[:, None] - fft_freqs[None, :]
    lower = -ramps[:-2] / widths[:-1, None]
    upper = ramps[2:] / widths[1:, None]
    weights = np.maximum(0.0, np.minimum(lower, upper))
    weights *= (2.0 / (hz_points[2:n_mels + 2] - hz_points[:n_mels]))[:, None]
    return weights.astype(np.float32)


_FILTERS: dict[tuple[int, int, int], np.ndarray] = {}


def _cached_filters(n_mels: int) -> np.ndarray:
    key = (SAMPLE_RATE, N_FFT, n_mels)
    if key not in _FILTERS:
        _FILTERS[key] = mel_filters(SAMPLE_RATE, N_FFT, n_mels)
    return _FILTERS[key]


def log_mel(audio: np.ndarray, *, n_mels: int = 80, padding: int = 0) -> np.ndarray:
    """Whisper's log-mel spectrogram of float `audio`, `[n_mels, frames]`.

    `padding` zero samples are added at the end first (faster-whisper adds
    160). The floor is `max - 8` over what is given - see the module docstring.
    """
    audio = np.asarray(audio, dtype=np.float32).reshape(-1)
    if padding:
        audio = np.pad(audio, (0, padding))
    half = N_FFT // 2
    if audio.shape[0] <= half:
        # Reflect padding needs more samples than half a frame; a clip this
        # short is silence to Whisper anyway.
        audio = np.pad(audio, (0, half + 1 - audio.shape[0]))
    padded = np.pad(audio, (half, half), mode="reflect")
    frames = np.lib.stride_tricks.sliding_window_view(padded, N_FFT)[::HOP_LENGTH]
    window = np.hanning(N_FFT + 1)[:-1].astype(np.float32)   # periodic Hann
    spectrum = np.fft.rfft(frames * window, n=N_FFT, axis=-1)
    power = (np.abs(spectrum) ** 2).T[:, :-1]                 # drop the last frame
    mel = _cached_filters(n_mels) @ power.astype(np.float32)
    log_spec = np.log10(np.clip(mel, 1e-10, None))
    log_spec = np.maximum(log_spec, log_spec.max() - 8.0)
    return ((log_spec + 4.0) / 4.0).astype(np.float32)


def window_features(samples: np.ndarray, n_mels: int = 80) -> np.ndarray:
    """One 30 s window's encoder input, `[n_mels, 3000]`: short audio padded
    with silence, long audio cut."""
    samples = np.asarray(samples, dtype=np.float32).reshape(-1)[:N_SAMPLES]
    if samples.shape[0] < N_SAMPLES:
        samples = np.pad(samples, (0, N_SAMPLES - samples.shape[0]))
    return log_mel(samples, n_mels=n_mels)[:, :N_FRAMES]


def leading_quiet_frames(samples: np.ndarray, threshold: float = SILENCE_RMS) -> int:
    """How many mel frames (10 ms) of float `samples` pass before the first
    100 ms block louder than `threshold`. All of them when none is."""
    samples = np.asarray(samples, dtype=np.float32).reshape(-1)
    blocks = samples.size // _SILENCE_BLOCK
    if blocks:
        body = samples[: blocks * _SILENCE_BLOCK].astype(np.float64).reshape(blocks, -1)
        loud = np.flatnonzero(np.sqrt(np.mean(body ** 2, axis=1)) >= threshold)
        if loud.size:
            return int(loud[0]) * _SILENCE_BLOCK // HOP_LENGTH
    return samples.size // HOP_LENGTH


def is_silent(samples: np.ndarray, threshold: float = SILENCE_RMS) -> bool:
    """True when no 100 ms block of float `samples` is louder than `threshold` RMS."""
    samples = np.asarray(samples, dtype=np.float32).reshape(-1)
    if samples.size == 0:
        return True
    blocks = samples.size // _SILENCE_BLOCK
    if blocks == 0:
        return float(np.sqrt(np.mean(samples.astype(np.float64) ** 2))) < threshold
    body = samples[: blocks * _SILENCE_BLOCK].astype(np.float64).reshape(blocks, -1)
    loudest = float(np.sqrt(np.mean(body ** 2, axis=1)).max())
    tail = samples[blocks * _SILENCE_BLOCK:]
    if tail.size:
        loudest = max(loudest, float(np.sqrt(np.mean(tail.astype(np.float64) ** 2))))
    return loudest < threshold


# ---------------------------------------------------------------------------
# Tokens and the rules of decoding
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class WhisperTokens:
    """The special token ids one Whisper model uses, read from its own files."""

    sot: int
    eot: int
    transcribe: int
    no_timestamps: int
    timestamp_begin: int
    languages: dict[int, str]                      # token id -> "en"
    suppress: tuple[int, ...] = ()
    begin_suppress: tuple[int, ...] = ()
    max_initial_timestamp_index: Optional[int] = None

    @classmethod
    def from_configs(cls, generation: dict, config: dict,
                     token_to_id: Callable[[str], Optional[int]]) -> "WhisperTokens":
        """Read every special token from `generation_config.json`, then
        `config.json`, then the tokenizer, in that order of trust. Raises
        `ValueError` naming any token no source can supply."""
        def first(*values: Any) -> Any:
            return next((v for v in values if v is not None), None)

        sot = first(generation.get("decoder_start_token_id"),
                    config.get("decoder_start_token_id"), token_to_id("<|startoftranscript|>"))
        eot = first(generation.get("eos_token_id"), config.get("eos_token_id"),
                    token_to_id("<|endoftext|>"))
        if isinstance(eot, list):
            eot = eot[0]
        task = (generation.get("task_to_id") or {}).get("transcribe")
        transcribe = first(task, token_to_id("<|transcribe|>"))
        no_ts = first(generation.get("no_timestamps_token_id"), token_to_id("<|notimestamps|>"))
        ts_begin = token_to_id("<|0.00|>")
        if ts_begin is None and no_ts is not None:
            # OpenAI's tokenizer (`whisper/tokenizer.py`) puts `<|0.00|>` directly
            # after `<|notimestamps|>`; an export whose tokenizer.json leaves the
            # timestamp tokens out still numbers them that way.
            ts_begin = int(no_ts) + 1
        missing = [name for name, value in (("sot", sot), ("eot", eot), ("transcribe", transcribe),
                                            ("notimestamps", no_ts), ("timestamp_begin", ts_begin))
                   if value is None]
        if missing:
            raise ValueError(f"the model's files do not name these tokens: {', '.join(missing)}")
        languages: dict[int, str] = {}
        for token, index in (generation.get("lang_to_id") or {}).items():
            code = str(token).strip("<|>")
            if code:
                languages[int(index)] = code
        return cls(
            sot=int(sot), eot=int(eot), transcribe=int(transcribe), no_timestamps=int(no_ts),
            timestamp_begin=int(ts_begin), languages=languages,
            suppress=tuple(int(t) for t in (first(generation.get("suppress_tokens"),
                                                  config.get("suppress_tokens")) or ())),
            begin_suppress=tuple(int(t) for t in (first(generation.get("begin_suppress_tokens"),
                                                        config.get("begin_suppress_tokens")) or ())),
            max_initial_timestamp_index=generation.get("max_initial_timestamp_index"),
        )


def begin_suppress(token_ids: Iterable[int]) -> LogitsRule:
    """OpenAI's `SuppressBlank`: these may not be the first generated token
    (a bare space, or the end of text before anything was said)."""
    ids = [int(t) for t in token_ids]

    def rule(generated: Sequence[int], logits: np.ndarray) -> None:
        if not generated:
            valid = [t for t in ids if 0 <= t < logits.shape[0]]
            logits[valid] = -np.inf
    return rule


def _log_softmax(logits: np.ndarray) -> np.ndarray:
    values = logits.astype(np.float64)
    finite = np.isfinite(values)
    if not finite.any():
        return np.full_like(values, -np.inf)
    top = values[finite].max()
    total = np.log(np.exp(values[finite] - top).sum()) + top
    return np.where(finite, values - total, -np.inf)


def timestamp_rules(tokens: WhisperTokens) -> LogitsRule:
    """OpenAI whisper's `ApplyTimestampRules` (`whisper/decoding.py`), for one
    sequence. `generated` is what came after the prompt."""
    begin, eot = tokens.timestamp_begin, tokens.eot
    initial = tokens.max_initial_timestamp_index

    def rule(generated: Sequence[int], logits: np.ndarray) -> None:
        # <|notimestamps|> would switch timestamps off.
        logits[tokens.no_timestamps] = -np.inf

        seq = list(generated)
        last_was_ts = len(seq) >= 1 and seq[-1] >= begin
        penultimate_was_ts = len(seq) < 2 or seq[-2] >= begin
        # Timestamps come in pairs, except directly before the end.
        if last_was_ts:
            if penultimate_was_ts:
                logits[begin:] = -np.inf          # a pair just closed: text next
            else:
                logits[:eot] = -np.inf            # an open pair: a timestamp or the end

        stamps = [t for t in seq if t >= begin]
        if stamps:
            # Never backwards, and a segment has a length: after a text token
            # the closing timestamp is later than the opening one.
            floor = stamps[-1] if (last_was_ts and not penultimate_was_ts) else stamps[-1] + 1
            logits[begin:floor] = -np.inf

        if not seq:
            logits[:begin] = -np.inf              # it starts with a timestamp
            if initial is not None:
                logits[begin + int(initial) + 1:] = -np.inf

        # If the timestamps together are likelier than any one text token, a
        # timestamp it is.
        logprobs = _log_softmax(logits)
        stamp_part = logprobs[begin:]
        finite = stamp_part[np.isfinite(stamp_part)]
        if finite.size:
            top = finite.max()
            stamp_logprob = top + np.log(np.exp(finite - top).sum())
            text_logprob = logprobs[:begin].max()
            if stamp_logprob > text_logprob:
                logits[:begin] = -np.inf
    return rule


def parse_window(tokens: Sequence[int], *, timestamp_begin: int, window_frames: int,
                 time_precision: float = 0.02, input_stride: int = 2
                 ) -> tuple[list[tuple[float, float, list[int]]], int]:
    """Split one window's generated tokens (end token excluded) into segments.

    Returns `([(start_s, end_s, text_ids), ...], frames_consumed)`; times are
    relative to the window's start, frames are mel frames (10 ms). OpenAI
    whisper's `transcribe()` without word timestamps.
    """
    toks = [int(t) for t in tokens]
    is_ts = [t >= timestamp_begin for t in toks]
    single_ending = len(is_ts) >= 2 and is_ts[-2:] == [False, True]
    consecutive = [i + 1 for i in range(len(toks) - 1) if is_ts[i] and is_ts[i + 1]]
    segments: list[tuple[float, float, list[int]]] = []

    if consecutive:
        slices = list(consecutive)
        if single_ending:
            slices.append(len(toks))
        last = 0
        for current in slices:
            piece = toks[last:current]
            start = (piece[0] - timestamp_begin) * time_precision
            end = (piece[-1] - timestamp_begin) * time_precision
            segments.append((start, end, [t for t in piece if t < timestamp_begin]))
            last = current
        if single_ending:
            consumed = window_frames
        else:
            consumed = (toks[last - 1] - timestamp_begin) * input_stride
    else:
        duration = window_frames * HOP_LENGTH / SAMPLE_RATE
        stamps = [t for t in toks if t >= timestamp_begin]
        if stamps and stamps[-1] != timestamp_begin:
            duration = (stamps[-1] - timestamp_begin) * time_precision
        segments.append((0.0, duration, [t for t in toks if t < timestamp_begin]))
        consumed = window_frames
    return segments, int(consumed)


# ---------------------------------------------------------------------------
# The engine
# ---------------------------------------------------------------------------

_ORT_TYPES = {"tensor(float)": np.float32, "tensor(float16)": np.float16,
              "tensor(double)": np.float64}


def _read_json(path: Path) -> dict:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


class OnnxWhisperEngine:
    """Whisper behind `transcribe.TranscriberEngine`: `language`, and
    `transcribe(path, start_s)` yielding `SpeechSegment`s with absolute times."""

    def __init__(self, *, encoder: Any, decoder: Any, tokenizer: Any, tokens: WhisperTokens,
                 config: dict, n_mels: int = 80,
                 audio_reader: Callable[[str], np.ndarray] = decode_audio,
                 name: str = "whisper") -> None:
        self.language = ""
        self.name = name
        #: Held for the whole of one `transcribe()`. `self.language` and the
        #: decoder's KV cache are rewritten by every window, and the media tail
        #: runs a sub-pipeline whose worker count can exceed one, so two
        #: recordings could reach one engine at once and interleave their
        #: caches into garbage text. Found in review 2026-10-08; `OnnxFlorence`
        #: holds `self.lock` per image for the same reason.
        self.lock = threading.Lock()
        self._encoder = encoder
        # 8 heads and d_model 512 are whisper-base's values - the fallback only
        # for a config.json missing the fields; every real export states them.
        heads = int(config.get("decoder_attention_heads") or 8)
        head_dim = int(config.get("d_model") or 512) // heads
        self._decoder = Decoder(decoder, heads=heads, head_dim=head_dim)
        self._tokenizer = tokenizer
        self.tokens = tokens
        self._n_mels = int(n_mels)
        self._read_audio = audio_reader
        enc_in = encoder.get_inputs()[0]
        self._encoder_input = enc_in.name
        self._encoder_dtype = _ORT_TYPES.get(getattr(enc_in, "type", ""), np.float32)
        self._encoder_output = encoder.get_outputs()[0].name
        # Each timestamp is `input_stride` mel frames: 3000 frames go in, the
        # encoder gives `max_source_positions` (1500) positions.
        source = int(config.get("max_source_positions") or N_FRAMES // 2)
        self.input_stride = max(1, N_FRAMES // source)
        self.time_precision = self.input_stride * HOP_LENGTH / SAMPLE_RATE
        # OpenAI's `sample_len`: half the decoder's context (224 for 448).
        self.sample_len = int(config.get("max_target_positions") or 448) // 2
        self.rules: list[LogitsRule] = [
            begin_suppress(tokens.begin_suppress),
            suppress(tokens.suppress),
            timestamp_rules(tokens),
        ]

    # -- building --------------------------------------------------------

    @classmethod
    def from_folder(cls, folder: Path, *, model: Optional[hub.OnnxModel] = None,
                    device: str = "auto", threads: Optional[int] = None) -> "OnnxWhisperEngine":
        """Open the two graphs in `folder` (a `hub.resolve` snapshot).
        Raises what onnxruntime or tokenizers raise."""
        from tokenizers import Tokenizer

        from app.ort.session import load_session

        folder = Path(folder)
        spec = model or SIZES["base"]
        config = _read_json(folder / "config.json")
        generation = _read_json(folder / "generation_config.json")
        preprocessor = _read_json(folder / "preprocessor_config.json")
        tokenizer = Tokenizer.from_file(str(folder / "tokenizer.json"))
        tokens = WhisperTokens.from_configs(generation, config, tokenizer.token_to_id)
        n_mels = int(preprocessor.get("feature_size") or config.get("num_mel_bins") or 80)
        encoder = load_session(folder / spec.graph_file("encoder_model"), what="speech",
                               device=device, threads=threads)
        # **The decoder always runs on the processor.** Measured 2026-09-29 on
        # the owner's laptop, whisper-base full precision: on DirectML the
        # merged decoder's first step (empty cache) matched the processor to
        # 6e-5, and every later step (`use_cache_branch` true) returned NaN
        # and values near 1e38 - a page of symbols instead of words. The
        # encoder on DirectML matched the processor (largest difference
        # 0.003), so it keeps whatever `device` asks for.
        decoder = load_session(folder / spec.graph_file("decoder_model_merged"), what="speech",
                               device="cpu", threads=threads)
        _log.info("speech model {} ready ({} mel bins, encoder on {}, decoder on {})",
                  spec.key, n_mels, encoder.choice.device, decoder.choice.device)
        return cls(encoder=encoder.session, decoder=decoder.session, tokenizer=tokenizer,
                   tokens=tokens, config=config, n_mels=n_mels, name=spec.key)

    # -- one window --------------------------------------------------------

    def _encode(self, features: np.ndarray) -> np.ndarray:
        feeds = {self._encoder_input: features[None].astype(self._encoder_dtype)}
        return self._encoder.run([self._encoder_output], feeds)[0]

    def _step(self, hidden: np.ndarray) -> Callable[[list[int], int], np.ndarray]:
        def step(new_ids: list[int], _position: int) -> np.ndarray:
            inputs = {"input_ids": np.array([new_ids], dtype=np.int64)}
            if self._decoder.accepts("encoder_hidden_states"):
                inputs["encoder_hidden_states"] = hidden
            return self._decoder.step(inputs)
        return step

    def detect_language(self, hidden: np.ndarray) -> str:
        """The likeliest language token after `<|startoftranscript|>`, as a code."""
        if not self.tokens.languages:
            return ""
        self._decoder.reset()
        logits = np.asarray(self._step(hidden)([self.tokens.sot], 0), dtype=np.float32)
        ids = np.array([i for i in self.tokens.languages if 0 <= i < logits.shape[0]])
        if ids.size == 0:
            return ""
        return self.tokens.languages[int(ids[int(np.argmax(logits[ids]))])]

    def decode_window(self, hidden: np.ndarray, language: str) -> list[int]:
        """Greedy tokens for one window, with timestamps; the end token not included."""
        prompt = [self.tokens.sot]
        lang_id = next((i for i, code in self.tokens.languages.items() if code == language), None)
        if lang_id is not None:
            prompt.append(lang_id)
        prompt.append(self.tokens.transcribe)
        self._decoder.reset()
        return list(generate(self._step(hidden), prompt=prompt, max_new_tokens=self.sample_len,
                             eos=[self.tokens.eot], rules=self.rules, pick=greedy))

    def _text(self, ids: list[int]) -> str:
        ids = [i for i in ids if i < self.tokens.eot]
        if not ids:
            return ""
        return str(self._tokenizer.decode(ids, skip_special_tokens=True) or "").strip()

    # -- the seam --------------------------------------------------------

    def transcribe(self, path: str, start_s: float = 0.0) -> Iterator[Any]:
        """Segments of `path` from `start_s` on, absolute times, one 30 s window
        at a time; `self.language` is set after the first window with sound.

        The whole decoded audio is held in memory (int16, 230 MB for two hours).
        **One call at a time per engine**: `self.language` and the decoder's
        cache are rewritten by every window, and there is no lock here - the
        caller must not transcribe two files on one engine concurrently
        (2026-10-08 review; `OnnxFlorence` holds `self.lock` for the same reason).

        2026-10-08, later: `self.lock` is now held from the first `next()` to
        exhaustion (or `close()`), so a second caller waits rather than
        corrupting the first. A generator abandoned mid-file releases it when
        it is garbage-collected, which is when its `with` block unwinds.
        """
        with self.lock:
            yield from self._transcribe_unlocked(path, start_s)

    def _transcribe_unlocked(self, path: str, start_s: float) -> Iterator[Any]:
        """`transcribe` without the lock - the body as it was before the lock
        was added, so the diff that added it is one `with` and a `yield from`."""
        from app.extract.transcribe import SpeechSegment

        self.language = ""
        audio = self._read_audio(path)
        content_frames = int(audio.shape[0]) // HOP_LENGTH
        seek = max(0, int(round(float(start_s) * SAMPLE_RATE / HOP_LENGTH)))
        while seek < content_frames:
            window_frames = min(N_FRAMES, content_frames - seek)
            if window_frames < MIN_TAIL_FRAMES:
                break
            first = seek * HOP_LENGTH
            samples = audio[first: first + window_frames * HOP_LENGTH]
            samples = samples.astype(np.float32) / (32768.0 if samples.dtype == np.int16 else 1.0)
            if is_silent(samples):
                seek += window_frames
                continue
            quiet = leading_quiet_frames(samples)
            if quiet >= LEAD_TRIM_FRAMES and quiet < window_frames:
                seek += quiet - LEAD_KEEP_FRAMES
                continue
            hidden = self._encode(window_features(samples, self._n_mels))
            if not self.language:
                self.language = self.detect_language(hidden)
            generated = self.decode_window(hidden, self.language)
            segments, consumed = parse_window(
                generated, timestamp_begin=self.tokens.timestamp_begin,
                window_frames=window_frames, time_precision=self.time_precision,
                input_stride=self.input_stride)
            offset = seek * HOP_LENGTH / SAMPLE_RATE
            for start, end, ids in segments:
                text = self._text(ids)
                if text:
                    yield SpeechSegment(start=round(offset + start, 3),
                                        end=round(offset + min(end, window_frames * HOP_LENGTH
                                                               / SAMPLE_RATE), 3),
                                        text=text)
            # Always forward: a window that consumed nothing is read once.
            seek += consumed if consumed > 0 else window_frames
