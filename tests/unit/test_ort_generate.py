"""`app/ort/generate.py` - the key/value-cache loop the ONNX models share.

Layer: L2. No model files: a fake session with the exported models' naming
(`past_key_values.N[.decoder|.encoder].key`, `present.N...`, `use_cache_branch`)
that returns a scripted token and grows its cache like a real decoder.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pytest

from app.ort.generate import (CacheSpec, Decoder, force_first, generate, greedy,
                              no_repeat_ngram, sample, suppress)

VOCAB = 12


@dataclass
class Arg:
    name: str
    type: str = "tensor(float)"
    shape: tuple = ()


class FakeDecoderSession:
    """Two layers; merged (use_cache_branch); encoder-decoder cache parts."""

    def __init__(self, script: list[int], *, encoder_decoder: bool = True,
                 merged: bool = True) -> None:
        self.script = script
        self.encoder_decoder = encoder_decoder
        self.merged = merged
        self.calls: list[dict] = []
        parts = ("decoder", "encoder") if encoder_decoder else (None,)
        self.cache = [f"{layer}.{p}.{kv}" if p else f"{layer}.{kv}"
                      for layer in range(2) for p in parts for kv in ("key", "value")]

    def get_inputs(self):
        args = [Arg("input_ids", "tensor(int64)", ("batch", "seq"))]
        args += [Arg(f"past_key_values.{c}", shape=("batch", 4, "past", 8)) for c in self.cache]
        if self.merged:
            args.append(Arg("use_cache_branch", "tensor(bool)", (1,)))
        return args

    def get_outputs(self):
        return [Arg("logits")] + [Arg(f"present.{c}") for c in self.cache]

    def run(self, names, feeds):
        self.calls.append(feeds)
        ids = feeds["input_ids"]
        seen = feeds[f"past_key_values.{self.cache[0]}"].shape[2]
        position = seen + ids.shape[1] - 1
        logits = np.zeros((1, ids.shape[1], VOCAB), dtype=np.float32)
        logits[0, -1, self.script[min(position, len(self.script) - 1)]] = 5.0
        out = [logits]
        for c in self.cache:
            past = feeds[f"past_key_values.{c}"]
            if ".encoder." in f".{c}." and seen > 0:
                out.append(np.zeros((1, 4, 0, 8), np.float32))    # what a real one may send
            else:
                grow = np.full((1, 4, ids.shape[1], 8), float(position), np.float32)
                out.append(np.concatenate([past, grow], axis=2))
        return out


def run_loop(session, prompt, **kw):
    decoder = Decoder(session, heads=4, head_dim=8)

    def step(new_ids, _position):
        return decoder.step({"input_ids": np.array([new_ids], dtype=np.int64)})

    return decoder, list(generate(step, prompt=prompt, **kw))


def test_the_cache_is_read_from_the_session_and_starts_empty():
    spec = CacheSpec.from_session(FakeDecoderSession([1]), heads=4, head_dim=8)
    assert spec.merged and len(spec.names) == 8 and len(spec.encoder_names) == 4
    empty = spec.empty()
    assert all(v.shape == (1, 4, 0, 8) for v in empty.values())


def test_tokens_come_out_in_order_and_stop_at_the_end_token():
    session = FakeDecoderSession([5, 6, 7, 2, 9])
    _decoder, tokens = run_loop(session, [0], max_new_tokens=10, eos=[2])
    # prompt [0] is position 0 -> 5; then 6, 7, then 2 ends it (not yielded)
    assert tokens == [5, 6, 7]


def test_the_cache_is_carried_and_use_cache_branch_flips():
    session = FakeDecoderSession([5, 6, 7, 2])
    decoder, _ = run_loop(session, [0, 1], max_new_tokens=10, eos=[2])
    first, second = session.calls[0], session.calls[1]
    assert bool(first["use_cache_branch"][0]) is False
    assert bool(second["use_cache_branch"][0]) is True
    assert first["input_ids"].shape == (1, 2) and second["input_ids"].shape == (1, 1)
    # the decoder cache grows one position per step
    assert second["past_key_values.0.decoder.key"].shape[2] == 2
    assert session.calls[2]["past_key_values.0.decoder.key"].shape[2] == 3


def test_the_encoder_part_of_the_cache_is_kept_from_the_first_step():
    session = FakeDecoderSession([5, 6, 7, 8, 2])
    run_loop(session, [0, 1, 3], max_new_tokens=10, eos=[2])
    later = session.calls[-1]["past_key_values.1.encoder.value"]
    assert later.shape[2] == 3, "the encoder cache is the first step's, not the empty reply"


def test_a_decoder_only_model_without_a_merged_branch():
    session = FakeDecoderSession([4, 4, 3, 2], encoder_decoder=False, merged=False)
    decoder, tokens = run_loop(session, [0], max_new_tokens=10, eos=[2])
    assert tokens == [4, 4, 3] and not decoder.spec.merged
    assert "use_cache_branch" not in session.calls[0]


def test_the_limit_is_respected_and_stopping_is_honoured():
    session = FakeDecoderSession([5] * 50)
    _, tokens = run_loop(session, [0], max_new_tokens=4, eos=[2])
    assert len(tokens) == 4
    _, stopped = run_loop(FakeDecoderSession([5] * 50), [0], max_new_tokens=40, eos=[2],
                          should_stop=lambda: True)
    assert stopped == []


def test_force_first_suppress_and_no_repeat_ngram():
    logits = np.zeros(VOCAB, np.float32)
    logits[7] = 9.0
    force_first(3)([], logits)
    assert greedy(logits) == 3

    logits = np.zeros(VOCAB, np.float32)
    logits[7] = 9.0
    suppress([7])([], logits)
    assert greedy(logits) != 7

    # generated ... 4 5 6 ... 4 5 -> 6 would repeat the 3-gram (4, 5, 6)
    logits = np.zeros(VOCAB, np.float32)
    logits[6] = 9.0
    no_repeat_ngram(3)([4, 5, 6, 1, 4, 5], logits)
    assert greedy(logits) != 6


def test_sampling_with_no_temperature_is_greedy_and_top_p_stays_in_the_nucleus():
    logits = np.array([0.0, 10.0, 9.9, -5.0], np.float32)
    assert sample(0.0)(logits) == 1
    rng = np.random.default_rng(0)
    picks = {sample(1.0, top_p=0.5, rng=rng)(logits) for _ in range(50)}
    assert picks <= {1, 2}


@pytest.mark.parametrize("dtype,expected", [("tensor(float16)", np.float16),
                                            ("tensor(float)", np.float32)])
def test_the_cache_dtype_follows_the_model(dtype, expected):
    session = FakeDecoderSession([1])
    original = session.get_inputs

    def inputs():
        return [Arg(a.name, dtype if a.name.startswith("past") else a.type, a.shape)
                for a in original()]
    session.get_inputs = inputs
    spec = CacheSpec.from_session(session, heads=4, head_dim=8)
    assert all(v.dtype == expected for v in spec.empty().values())
