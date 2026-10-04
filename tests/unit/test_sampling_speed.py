"""Nucleus sampling without sorting the whole vocabulary (2026-10-04, code review).

Layer: L2.

`app.ort.generate.sample` sorted all 151,936 of Qwen's tokens in float64 for every
token it wrote - 20-32 ms a token, against 0.03 ms for greedy. It now sorts only the
likeliest few, widened until they hold the `top_p` mass. These tests hold it to the
old behaviour: the same token for the same random seed, and never a token outside
the nucleus.
"""

from __future__ import annotations

import time
from typing import Optional

import numpy as np
import pytest

from app.ort.generate import sample

VOCAB = 151_936


def _reference(temperature: float, top_p: float = 0.9,
               rng: Optional[np.random.Generator] = None):
    """The sampler as it was before 2026-10-04 - the full sort, in float64."""
    generator = rng or np.random.default_rng()

    def pick(logits: np.ndarray) -> int:
        scaled = logits.astype(np.float64) / temperature
        scaled -= np.max(scaled[np.isfinite(scaled)])
        probs = np.where(np.isfinite(scaled), np.exp(scaled), 0.0)
        order = np.argsort(-probs)
        cumulative = np.cumsum(probs[order]) / probs.sum()
        keep = order[: int(np.searchsorted(cumulative, top_p) + 1)]
        chosen = probs[keep] / probs[keep].sum()
        return int(generator.choice(keep, p=chosen))
    return pick


def _nucleus(logits: np.ndarray, temperature: float, top_p: float) -> set[int]:
    scaled = logits.astype(np.float64) / temperature
    scaled -= np.max(scaled[np.isfinite(scaled)])
    probs = np.where(np.isfinite(scaled), np.exp(scaled), 0.0)
    order = np.argsort(-probs)
    cumulative = np.cumsum(probs[order]) / probs.sum()
    return {int(t) for t in order[: int(np.searchsorted(cumulative, top_p) + 1)]}


def _logits(seed: int, *, peaked: bool = True) -> np.ndarray:
    """Shaped like a language model's: a wide low background and a few strong tokens."""
    rng = np.random.default_rng(seed)
    out = rng.normal(0.0, 2.5 if peaked else 0.3, VOCAB).astype(np.float32)
    if peaked:
        heads = rng.choice(VOCAB, 40, replace=False)
        out[heads] += rng.uniform(6.0, 14.0, 40).astype(np.float32)
    return out


@pytest.mark.parametrize("temperature", [0.2, 0.4, 0.7, 1.0, 1.5])
@pytest.mark.parametrize("seed", range(6))
def test_the_same_token_as_the_full_sort_for_the_same_seed(temperature, seed):
    logits = _logits(seed)
    new = sample(temperature, rng=np.random.default_rng(100 + seed))
    old = _reference(temperature, rng=np.random.default_rng(100 + seed))
    for _ in range(25):
        assert new(logits.copy()) == old(logits.copy())


@pytest.mark.parametrize("top_p", [0.5, 0.9, 0.99])
def test_the_token_is_always_inside_the_nucleus(top_p):
    logits = _logits(7)
    allowed = _nucleus(logits, 0.7, top_p)
    pick = sample(0.7, top_p, rng=np.random.default_rng(3))
    for _ in range(200):
        assert pick(logits.copy()) in allowed


def test_a_flat_distribution_still_matches_the_full_sort():
    """The nucleus spans most of the vocabulary: the widening reaches the full sort."""
    logits = _logits(11, peaked=False)
    new = sample(1.0, rng=np.random.default_rng(5))
    old = _reference(1.0, rng=np.random.default_rng(5))
    for _ in range(5):
        assert new(logits.copy()) == old(logits.copy())


def test_forbidden_tokens_are_never_picked():
    """A rule's `-inf` (no repeat, suppress) still means never."""
    logits = _logits(2)
    best = int(np.argmax(logits))
    logits[best] = -np.inf
    pick = sample(0.7, rng=np.random.default_rng(9))
    assert all(pick(logits.copy()) != best for _ in range(100))


def test_zero_temperature_is_greedy():
    logits = _logits(4)
    assert sample(0.0)(logits) == int(np.argmax(logits))


def test_a_token_takes_a_few_milliseconds_not_tens():
    """Measured 2026-10-04 on the owner's laptop: 20-26 ms a token before, 1.6-2.3 ms
    after. The bound here is loose - a busy test machine - but the full sort is not."""
    logits = _logits(1)
    pick = sample(0.7, rng=np.random.default_rng(0))
    pick(logits.copy())
    started = time.perf_counter()
    for _ in range(40):
        pick(logits.copy())
    per_token_ms = (time.perf_counter() - started) / 40 * 1000
    assert per_token_ms < 12.0, f"{per_token_ms:.1f} ms a token"
